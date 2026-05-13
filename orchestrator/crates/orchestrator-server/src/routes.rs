//! HTTP routes for the orchestrator API surface delivered so far.
//!
//! Phase 0 delivered `/v1/health`, Phase 1 added `/v1/search` and
//! `/v1/admin/indexers`, and Phase 2 adds `/v1/resolve` as a single-peer
//! bridge. Later phases fill in `/v1/peers/:rid` and `/v1/stream/...`.

use std::{convert::Infallible, sync::Arc, time::Instant};

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    response::sse::{Event, Sse},
    routing::{get, post},
    Json, Router,
};
use futures::{
    stream::{self, BoxStream},
    StreamExt,
};
use serde::{Deserialize, Serialize};
use tokio::sync::broadcast;
use tracing::info;

use crate::logging::Outcome;
use crate::peer_pool::{PeerPoolCachePolicy, PeerPoolStats, PeerPoolStore, ResolveEvent};

#[derive(Debug, Serialize)]
pub struct HealthPayload {
    pub status: &'static str,
    pub phase: &'static str,
    pub version: &'static str,
    pub peer_cache_size: u64,
    pub peer_ready_count: u64,
    pub peer_validated_count: u64,
    pub peer_rejected_count: u64,
}

#[derive(Clone)]
pub struct PeerPoolState {
    pub peer_pool: PeerPoolStore,
    pub cache_policy: PeerPoolCachePolicy,
}

#[derive(Debug, Default, Deserialize)]
struct ResolveEventsQuery {
    #[serde(default)]
    tail: bool,
}

/// Phase 0 returns a static, non-empty payload. Phase 5+ replaces this
/// with the full health body documented in plan §5.
async fn health() -> Json<HealthPayload> {
    health_with_stats(PeerPoolStats::default())
}

async fn health_with_peer_pool(State(state): State<PeerPoolState>) -> Json<HealthPayload> {
    let stats = state.peer_pool.stats().unwrap_or_default();
    health_with_stats(stats)
}

fn health_with_stats(stats: PeerPoolStats) -> Json<HealthPayload> {
    let started = Instant::now();
    let payload = HealthPayload {
        status: "ok",
        phase: "phase-0",
        version: env!("CARGO_PKG_VERSION"),
        peer_cache_size: stats.peer_cache_size,
        peer_ready_count: stats.peer_ready_count,
        peer_validated_count: stats.peer_validated_count,
        peer_rejected_count: stats.peer_rejected_count,
    };
    let duration_ms = started.elapsed().as_millis() as u64;
    info!(
        event = "health.served",
        outcome = Outcome::Ok.as_str(),
        duration_ms = duration_ms,
        "phase-0 stub /v1/health served"
    );
    Json(payload)
}

pub fn router() -> Router {
    Router::new()
        .route("/v1/health", get(health))
        .route("/v1/search", post(crate::search::search))
        .route("/v1/resolve", post(crate::resolve::resolve))
}

pub fn router_with_peer_pool(peer_pool: PeerPoolStore) -> Router {
    router_with_peer_pool_and_policy(peer_pool, PeerPoolCachePolicy::default())
}

pub fn router_with_peer_pool_and_policy(
    peer_pool: PeerPoolStore,
    cache_policy: PeerPoolCachePolicy,
) -> Router {
    Router::new()
        .route("/v1/health", get(health_with_peer_pool))
        .route("/v1/search", post(crate::search::search))
        .route("/v1/resolve", post(crate::resolve::resolve_with_peer_pool))
        .route("/v1/resolve/:resolve_id/events", get(resolve_events))
        .route("/v1/peers/cache/:cache_key", get(peers_by_cache_key))
        .route("/v1/peers/:resolve_id", get(peers_by_resolve_id))
        .with_state(PeerPoolState {
            peer_pool,
            cache_policy,
        })
}

/// Same as [`router`] but with the `/v1/admin/indexers` CRUD routes
/// mounted against a real on-disk store. The binary path uses this;
/// tests that only exercise the stateless routes (search, health)
/// can still use [`router`] to keep their setup free of temp dirs.
pub fn router_with_admin(admin: crate::admin::AdminState) -> Router {
    router().merge(crate::admin::admin_router(admin))
}

pub fn router_with_admin_and_peer_pool(
    admin: crate::admin::AdminState,
    peer_pool: PeerPoolStore,
) -> Router {
    router_with_peer_pool(peer_pool).merge(crate::admin::admin_router(admin))
}

pub fn router_with_admin_peer_pool_and_policy(
    admin: crate::admin::AdminState,
    peer_pool: PeerPoolStore,
    cache_policy: PeerPoolCachePolicy,
) -> Router {
    router_with_peer_pool_and_policy(peer_pool, cache_policy)
        .merge(crate::admin::admin_router(admin))
}

async fn peers_by_resolve_id(
    State(state): State<PeerPoolState>,
    Path(resolve_id): Path<String>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    match state.peer_pool.get_response(&resolve_id) {
        Ok(Some(response)) => Ok(Json(response)),
        Ok(None) => Err(StatusCode::NOT_FOUND),
        Err(error) => {
            tracing::warn!(
                event = "peer_pool.lookup_failed",
                resolve_id = %resolve_id,
                outcome = Outcome::Error.as_str(),
                reason = %error,
                "peer-pool lookup failed"
            );
            Err(StatusCode::INTERNAL_SERVER_ERROR)
        }
    }
}

async fn peers_by_cache_key(
    State(state): State<PeerPoolState>,
    Path(cache_key): Path<String>,
) -> Result<Json<serde_json::Value>, StatusCode> {
    match state
        .peer_pool
        .get_latest_response_by_cache_key_with_policy(&cache_key, state.cache_policy)
    {
        Ok(Some(response)) => Ok(Json(response)),
        Ok(None) => Err(StatusCode::NOT_FOUND),
        Err(error) => {
            tracing::warn!(
                event = "peer_pool.cache_lookup_failed",
                outcome = Outcome::Error.as_str(),
                reason = %error,
                "peer-pool cache lookup failed"
            );
            Err(StatusCode::INTERNAL_SERVER_ERROR)
        }
    }
}

async fn resolve_events(
    State(state): State<PeerPoolState>,
    Path(resolve_id): Path<String>,
    Query(query): Query<ResolveEventsQuery>,
) -> Result<Sse<BoxStream<'static, Result<Event, Infallible>>>, StatusCode> {
    let subscriber = state.peer_pool.subscribe_resolve_events();
    let events = state
        .peer_pool
        .list_resolve_events(&resolve_id)
        .map_err(|error| {
            tracing::warn!(
                event = "resolve.events_lookup_failed",
                resolve_id = %resolve_id,
                outcome = Outcome::Error.as_str(),
                reason = %error,
                "resolve event lookup failed"
            );
            StatusCode::INTERNAL_SERVER_ERROR
        })?;
    let last_sequence = events.last().map_or(0, |event| event.sequence);
    let replay = stream::iter(events.into_iter().map(sse_event));
    if query.tail {
        let live = live_resolve_events(subscriber, Arc::from(resolve_id), last_sequence);
        Ok(Sse::new(replay.chain(live).boxed()))
    } else {
        Ok(Sse::new(replay.boxed()))
    }
}

fn live_resolve_events(
    subscriber: broadcast::Receiver<ResolveEvent>,
    resolve_id: Arc<str>,
    last_replayed_sequence: u64,
) -> impl futures::Stream<Item = Result<Event, Infallible>> + Send + 'static {
    stream::unfold(
        (subscriber, resolve_id),
        move |(mut subscriber, resolve_id)| async move {
            loop {
                match subscriber.recv().await {
                    Ok(event)
                        if event.resolve_id == resolve_id.as_ref()
                            && event.sequence > last_replayed_sequence =>
                    {
                        return Some((sse_event(event), (subscriber, resolve_id)));
                    }
                    Ok(_) | Err(broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(broadcast::error::RecvError::Closed) => return None,
                }
            }
        },
    )
}

fn sse_event(resolve_event: ResolveEvent) -> Result<Event, Infallible> {
    let event_type = resolve_event.event.clone();
    let data = serde_json::to_string(&resolve_event).expect("resolve event serializes");
    Ok(Event::default().event(event_type).data(data))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::http::{Method, Request, StatusCode};
    use axum::response::IntoResponse;
    use axum::routing::any;
    use futures::StreamExt;
    use tower::ServiceExt;

    #[tokio::test]
    async fn health_returns_static_body() {
        let app = router();
        let response = app
            .oneshot(
                Request::builder()
                    .uri("/v1/health")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["status"], "ok");
        assert_eq!(json["phase"], "phase-0");
        assert!(!json["version"].as_str().unwrap().is_empty());
    }

    #[tokio::test]
    async fn resolve_returns_single_ready_peer_with_stream_url() {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };

        use axum::extract::State;

        #[derive(Clone)]
        struct FakeState {
            addurl_seen: Arc<AtomicBool>,
        }

        async fn fake_nzbdav(
            State(state): State<FakeState>,
            req: axum::http::Request<axum::body::Body>,
        ) -> impl IntoResponse {
            let path = req.uri().path().to_string();
            let query = req.uri().query().unwrap_or("").to_string();
            let method = req.method().clone();

            if path == "/api" && query.contains("mode=addurl") {
                state.addurl_seen.store(true, Ordering::SeqCst);
                return Json(serde_json::json!({
                    "status": true,
                    "nzo_ids": ["nzo-1"]
                }))
                .into_response();
            }

            if path == "/api" && query.contains("mode=history") && query.contains("search=") {
                return Json(serde_json::json!({
                    "history": { "slots": [] }
                }))
                .into_response();
            }

            if path == "/api" && query.contains("mode=history") && query.contains("nzo_ids=nzo-1") {
                return Json(serde_json::json!({
                    "history": {
                        "slots": [{
                            "nzo_id": "nzo-1",
                            "status": "Completed",
                            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Inception",
                            "name": "Inception.2010.1080p.BluRay.x264-FGT",
                            "fail_message": ""
                        }]
                    }
                }))
                .into_response();
            }

            if path == "/api" && query.contains("mode=queue") {
                return Json(serde_json::json!({
                    "queue": { "slots": [] }
                }))
                .into_response();
            }

            if method.as_str() == "PROPFIND" && path == "/content/uncategorized/Inception/" {
                return (
                    StatusCode::MULTI_STATUS,
                    [("content-type", "application/xml")],
                    r#"<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/Inception/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/Inception/Inception.mkv</D:href>
    <D:propstat><D:prop><D:getcontentlength>123456789</D:getcontentlength></D:prop></D:propstat>
  </D:response>
</D:multistatus>"#,
                )
                    .into_response();
            }

            if method == Method::HEAD && path == "/content/uncategorized/Inception/Inception.mkv" {
                return (
                    StatusCode::OK,
                    [("content-length", "123456789"), ("accept-ranges", "bytes")],
                    "",
                )
                    .into_response();
            }

            StatusCode::NOT_FOUND.into_response()
        }

        let addurl_seen = Arc::new(AtomicBool::new(false));
        let fake_app = Router::new()
            .route("/*path", any(fake_nzbdav))
            .with_state(FakeState {
                addurl_seen: addurl_seen.clone(),
            });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, fake_app).await.unwrap();
        });
        let base_url = format!("http://{addr}");

        let tmp = tempfile::tempdir().unwrap();
        let peer_pool =
            crate::peer_pool::PeerPoolStore::open(tmp.path().join("peer_pool.sqlite3")).unwrap();
        let app = router_with_peer_pool(peer_pool);

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/resolve")
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(
                        serde_json::json!({
                            "nzb_url": format!("{base_url}/api?t=get&id=abc"),
                            "title": "Inception.2010.1080p.BluRay.x264-FGT",
                            "peer_pool_cache_key": "tt1375666-1080p-fgt",
                            "fallback_count": 3,
                            "poll_interval_secs": 1,
                            "download_timeout_secs": 30,
                            "nzbdav": {
                                "base_url": base_url,
                                "api_key": "secret",
                                "webdav_url": base_url,
                                "webdav_username": "user",
                                "webdav_password": "pass",
                                "webdav_content_root": "content"
                            }
                        })
                        .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert!(addurl_seen.load(Ordering::SeqCst));
        assert!(json["resolve_id"].as_str().unwrap().starts_with("01"));
        assert_eq!(json["nzo_id"], "nzo-1");
        assert_eq!(
            json["stream_url"],
            format!("{base_url}/content/uncategorized/Inception/Inception.mkv")
        );
        assert_eq!(
            json["stream_headers"]["Authorization"],
            "Basic dXNlcjpwYXNz"
        );
        assert_eq!(json["peers"].as_array().unwrap().len(), 1);
        assert_eq!(json["peers"][0]["state"], "ready");
        assert_eq!(json["peers"][0]["content_length"], 123456789);

        let resolve_id = json["resolve_id"].as_str().unwrap();
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(format!("/v1/peers/{resolve_id}"))
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let stored: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(stored["resolve_id"], resolve_id);
        assert_eq!(stored["peers"][0]["state"], "ready");

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/v1/peers/cache/tt1375666-1080p-fgt")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let cached: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(cached["resolve_id"], resolve_id);

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/v1/health")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let health: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(health["peer_cache_size"], 1);
        assert_eq!(health["peer_ready_count"], 1);

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri(format!("/v1/resolve/{resolve_id}/events"))
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let events = String::from_utf8(body.to_vec()).unwrap();
        assert!(events.contains("event: submit.accepted"));
        assert!(events.contains(r#""state":"submitted""#));
        assert!(events.contains(r#""nzo_id":"nzo-1""#));
        assert!(events.contains("event: webdav.probe"));
        assert!(events.contains(r#""state":"ready""#));
        assert!(events.contains(r#""content_length":123456789"#));
        assert!(events.contains("event: resolve.completed"));

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/v1/peers/01MISSING")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn peers_cache_lookup_returns_latest_pool_for_key() {
        let tmp = tempfile::tempdir().unwrap();
        let peer_pool =
            crate::peer_pool::PeerPoolStore::open(tmp.path().join("peer_pool.sqlite3")).unwrap();
        peer_pool
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01CACHED",
                    "primary_peer_id": "01PRIMARY",
                    "nzo_id": "nzo-1",
                    "stream_url": "http://webdav/content/Movie.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY",
                            "state": "ready",
                            "validation_state": "single_peer_phase_2",
                            "nzo_id": "nzo-1",
                            "stream_url": "http://webdav/content/Movie.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        },
                        {
                            "peer_id": "01VALIDATED",
                            "state": "ready",
                            "validation_state": "byte_sample_validated_phase_3",
                            "nzo_id": "nzo-2",
                            "stream_url": "http://webdav/content/Movie-peer.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666-1080p-fgt"),
            )
            .unwrap();
        let app = router_with_peer_pool(peer_pool);

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/v1/peers/cache/TT1375666-1080P-FGT")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["resolve_id"], "01CACHED");
        assert_eq!(json["peers"].as_array().unwrap().len(), 2);

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/v1/peers/cache/tt1375666-2160p-fgt")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn peers_cache_lookup_returns_not_found_for_stale_pool() {
        let tmp = tempfile::tempdir().unwrap();
        let peer_pool =
            crate::peer_pool::PeerPoolStore::open(tmp.path().join("peer_pool.sqlite3")).unwrap();
        peer_pool
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01STALE",
                    "primary_peer_id": "01PRIMARY",
                    "nzo_id": "nzo-1",
                    "stream_url": "http://webdav/content/Stale.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY",
                            "state": "ready",
                            "validation_state": "single_peer_phase_2",
                            "nzo_id": "nzo-1",
                            "stream_url": "http://webdav/content/Stale.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666-1080p-fgt"),
            )
            .unwrap();
        let app = router_with_peer_pool_and_policy(
            peer_pool,
            crate::peer_pool::PeerPoolCachePolicy::disabled(),
        );

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/v1/peers/cache/tt1375666-1080p-fgt")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn resolve_returns_cached_peer_pool_for_known_cache_key() {
        let tmp = tempfile::tempdir().unwrap();
        let peer_pool =
            crate::peer_pool::PeerPoolStore::open(tmp.path().join("peer_pool.sqlite3")).unwrap();
        peer_pool
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01CACHED",
                    "primary_peer_id": "01PRIMARY",
                    "nzo_id": "nzo-1",
                    "stream_url": "http://webdav/content/Cached.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY",
                            "state": "ready",
                            "validation_state": "single_peer_phase_2",
                            "nzo_id": "nzo-1",
                            "stream_url": "http://webdav/content/Cached.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666-1080p-fgt"),
            )
            .unwrap();
        let app = router_with_peer_pool(peer_pool);

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/resolve")
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(
                        serde_json::json!({
                            "nzb_url": "http://127.0.0.1:9/nzb/primary",
                            "title": "Inception.2010.1080p.BluRay.x264-FGT",
                            "peer_pool_cache_key": "tt1375666-1080p-fgt",
                            "poll_interval_secs": 1,
                            "download_timeout_secs": 1,
                            "nzbdav": {
                                "base_url": "http://127.0.0.1:9",
                                "api_key": "secret",
                                "webdav_url": "http://127.0.0.1:9"
                            }
                        })
                        .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(json["resolve_id"], "01CACHED");
        assert_eq!(json["stream_url"], "http://webdav/content/Cached.mkv");

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/v1/resolve/01CACHED/events")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let events = String::from_utf8(body.to_vec()).unwrap();
        assert!(events.contains("event: resolve.cache_hit"));
        assert!(events.contains(r#""state":"ready""#));
        assert!(events.contains(r#""cache_key":"tt1375666-1080p-fgt""#));
    }

    #[tokio::test]
    async fn resolve_ignores_stale_cached_peer_pool_and_falls_through_to_live_resolve() {
        let tmp = tempfile::tempdir().unwrap();
        let peer_pool =
            crate::peer_pool::PeerPoolStore::open(tmp.path().join("peer_pool.sqlite3")).unwrap();
        peer_pool
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01STALE",
                    "primary_peer_id": "01PRIMARY",
                    "nzo_id": "nzo-1",
                    "stream_url": "http://webdav/content/Stale.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY",
                            "state": "ready",
                            "validation_state": "single_peer_phase_2",
                            "nzo_id": "nzo-1",
                            "stream_url": "http://webdav/content/Stale.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666-1080p-fgt"),
            )
            .unwrap();
        let app = router_with_peer_pool_and_policy(
            peer_pool,
            crate::peer_pool::PeerPoolCachePolicy::disabled(),
        );

        let response = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/resolve")
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(
                        serde_json::json!({
                            "nzb_url": "http://127.0.0.1:9/nzb/primary",
                            "title": "Inception.2010.1080p.BluRay.x264-FGT",
                            "peer_pool_cache_key": "tt1375666-1080p-fgt",
                            "poll_interval_secs": 1,
                            "download_timeout_secs": 1,
                            "nzbdav": {
                                "base_url": "http://127.0.0.1:9",
                                "api_key": "secret",
                                "webdav_url": "http://127.0.0.1:9"
                            }
                        })
                        .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    }

    #[tokio::test]
    async fn resolve_cache_hit_event_uses_request_resolve_id_for_progress_tail() {
        let tmp = tempfile::tempdir().unwrap();
        let peer_pool =
            crate::peer_pool::PeerPoolStore::open(tmp.path().join("peer_pool.sqlite3")).unwrap();
        peer_pool
            .save_response_with_cache_key(
                &serde_json::json!({
                    "resolve_id": "01CACHED",
                    "primary_peer_id": "01PRIMARY",
                    "nzo_id": "nzo-1",
                    "stream_url": "http://webdav/content/Cached.mkv",
                    "stream_headers": {},
                    "peer_cohort": [],
                    "peers": [
                        {
                            "peer_id": "01PRIMARY",
                            "state": "ready",
                            "validation_state": "byte_sample_validated_phase_3",
                            "nzo_id": "nzo-1",
                            "stream_url": "http://webdav/content/Cached.mkv",
                            "stream_headers": {},
                            "content_length": 1234
                        }
                    ]
                }),
                Some("tt1375666-1080p-fgt"),
            )
            .unwrap();
        let app = router_with_peer_pool(peer_pool);

        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/resolve")
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(
                        serde_json::json!({
                            "resolve_id": "01REQUEST",
                            "nzb_url": "http://127.0.0.1:9/nzb/primary",
                            "title": "Inception.2010.1080p.BluRay.x264-FGT",
                            "peer_pool_cache_key": "tt1375666-1080p-fgt",
                            "poll_interval_secs": 1,
                            "download_timeout_secs": 1,
                            "nzbdav": {
                                "base_url": "http://127.0.0.1:9",
                                "api_key": "secret",
                                "webdav_url": "http://127.0.0.1:9"
                            }
                        })
                        .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/v1/resolve/01REQUEST/events")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let events = String::from_utf8(body.to_vec()).unwrap();
        assert!(events.contains("event: resolve.cache_hit"));
        assert!(events.contains(r#""resolve_id":"01REQUEST""#));
        assert!(events.contains(r#""cached_resolve_id":"01CACHED""#));
    }

    #[tokio::test]
    async fn resolve_events_route_replays_stored_events_as_sse() {
        let tmp = tempfile::tempdir().unwrap();
        let peer_pool =
            crate::peer_pool::PeerPoolStore::open(tmp.path().join("peer_pool.sqlite3")).unwrap();
        peer_pool
            .append_resolve_event(
                "01RESOLVE",
                "submit.accepted",
                Some("01PRIMARY"),
                "submitted",
                None,
                serde_json::json!({"nzbdav_job_id": "nzo-1"}),
            )
            .unwrap();
        peer_pool
            .append_resolve_event(
                "01RESOLVE",
                "webdav.probe",
                Some("01PRIMARY"),
                "ready",
                Some("content_length_validated"),
                serde_json::json!({"content_length": 1234}),
            )
            .unwrap();
        let app = router_with_peer_pool(peer_pool);

        let response = app
            .oneshot(
                Request::builder()
                    .uri("/v1/resolve/01RESOLVE/events")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response
                .headers()
                .get("content-type")
                .and_then(|value| value.to_str().ok()),
            Some("text/event-stream")
        );
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let text = String::from_utf8(body.to_vec()).unwrap();
        assert!(text.contains("event: submit.accepted"));
        assert!(text.contains(r#""resolve_id":"01RESOLVE""#));
        assert!(text.contains(r#""peer_id":"01PRIMARY""#));
        assert!(text.contains(r#""state":"submitted""#));
        assert!(text.contains(r#""nzbdav_job_id":"nzo-1""#));
        assert!(text.contains("event: webdav.probe"));
        assert!(text.contains(r#""state":"ready""#));
        assert!(text.contains(r#""reason":"content_length_validated""#));
        assert!(text.contains(r#""content_length":1234"#));
    }

    #[tokio::test]
    async fn resolve_events_route_tails_new_events_when_requested() {
        let tmp = tempfile::tempdir().unwrap();
        let peer_pool =
            crate::peer_pool::PeerPoolStore::open(tmp.path().join("peer_pool.sqlite3")).unwrap();
        peer_pool
            .append_resolve_event(
                "01RESOLVE",
                "submit.accepted",
                Some("01PRIMARY"),
                "submitted",
                None,
                serde_json::json!({"nzo_id": "nzo-1"}),
            )
            .unwrap();
        let app = router_with_peer_pool(peer_pool.clone());
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });

        let response = reqwest::get(format!(
            "http://{addr}/v1/resolve/01RESOLVE/events?tail=true"
        ))
        .await
        .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let mut body = response.bytes_stream();
        let mut text = read_until_sse_contains(&mut body, "event: submit.accepted").await;

        peer_pool
            .append_resolve_event(
                "01RESOLVE",
                "webdav.probe",
                Some("01PRIMARY"),
                "ready",
                None,
                serde_json::json!({"content_length": 1234}),
            )
            .unwrap();
        text.push_str(&read_until_sse_contains(&mut body, "event: webdav.probe").await);
        assert!(text.contains(r#""state":"submitted""#));
        assert!(text.contains(r#""state":"ready""#));
        assert!(text.contains(r#""content_length":1234"#));
    }

    #[tokio::test]
    async fn resolve_route_publishes_submit_event_before_resolve_completes() {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            Arc,
        };

        use axum::extract::State;

        #[derive(Clone)]
        struct FakeState {
            complete: Arc<AtomicBool>,
        }

        async fn fake_nzbdav(
            State(state): State<FakeState>,
            req: axum::http::Request<axum::body::Body>,
        ) -> impl IntoResponse {
            let path = req.uri().path().to_string();
            let query = req.uri().query().unwrap_or("").to_string();
            let method = req.method().clone();

            if path == "/api" && query.contains("mode=addurl") {
                return Json(serde_json::json!({
                    "status": true,
                    "nzo_ids": ["nzo-live"]
                }))
                .into_response();
            }

            if path == "/api" && query.contains("mode=history") && query.contains("search=") {
                return Json(serde_json::json!({
                    "history": { "slots": [] }
                }))
                .into_response();
            }

            if path == "/api"
                && query.contains("mode=history")
                && query.contains("nzo_ids=nzo-live")
            {
                if state.complete.load(Ordering::SeqCst) {
                    return Json(serde_json::json!({
                        "history": {
                            "slots": [{
                                "nzo_id": "nzo-live",
                                "status": "Completed",
                                "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Live",
                                "name": "Live",
                                "fail_message": ""
                            }]
                        }
                    }))
                    .into_response();
                }
                return Json(serde_json::json!({
                    "history": { "slots": [] }
                }))
                .into_response();
            }

            if path == "/api" && query.contains("mode=queue") {
                return Json(serde_json::json!({
                    "queue": { "slots": [] }
                }))
                .into_response();
            }

            if method.as_str() == "PROPFIND" && path == "/content/uncategorized/Live/" {
                return (
                    StatusCode::MULTI_STATUS,
                    [("content-type", "application/xml")],
                    r#"<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/Live/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/Live/Live.mkv</D:href>
    <D:propstat><D:prop><D:getcontentlength>1234</D:getcontentlength></D:prop></D:propstat>
  </D:response>
</D:multistatus>"#,
                )
                    .into_response();
            }

            if method == Method::HEAD && path == "/content/uncategorized/Live/Live.mkv" {
                return (
                    StatusCode::OK,
                    [("content-length", "1234"), ("accept-ranges", "bytes")],
                    "",
                )
                    .into_response();
            }

            StatusCode::NOT_FOUND.into_response()
        }

        let complete = Arc::new(AtomicBool::new(false));
        let fake_app = Router::new()
            .route("/*path", any(fake_nzbdav))
            .with_state(FakeState {
                complete: complete.clone(),
            });
        let fake_listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let fake_addr = fake_listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(fake_listener, fake_app).await.unwrap();
        });
        let base_url = format!("http://{fake_addr}");

        let tmp = tempfile::tempdir().unwrap();
        let peer_pool =
            crate::peer_pool::PeerPoolStore::open(tmp.path().join("peer_pool.sqlite3")).unwrap();
        let app = router_with_peer_pool(peer_pool);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });

        let resolve_id = "01LIVEPROGRESS";
        let events_response = reqwest::get(format!(
            "http://{addr}/v1/resolve/{resolve_id}/events?tail=true"
        ))
        .await
        .unwrap();
        assert_eq!(events_response.status(), StatusCode::OK);
        let mut events = events_response.bytes_stream();

        let post = tokio::spawn({
            let base_url = base_url.clone();
            async move {
                reqwest::Client::new()
                    .post(format!("http://{addr}/v1/resolve"))
                    .json(&serde_json::json!({
                        "resolve_id": resolve_id,
                        "nzb_url": format!("{base_url}/api?t=get&id=live"),
                        "title": "Live.2026.1080p-GROUP",
                        "poll_interval_secs": 1,
                        "download_timeout_secs": 30,
                        "nzbdav": {
                            "base_url": base_url,
                            "api_key": "secret",
                            "webdav_url": base_url,
                            "webdav_content_root": "content"
                        }
                    }))
                    .send()
                    .await
                    .unwrap()
            }
        });

        let text = read_until_sse_contains(&mut events, "event: submit.accepted").await;
        assert!(text.contains(r#""resolve_id":"01LIVEPROGRESS""#));
        assert!(text.contains(r#""nzo_id":"nzo-live""#));

        complete.store(true, Ordering::SeqCst);
        let response = tokio::time::timeout(std::time::Duration::from_secs(3), post)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
    }

    async fn read_until_sse_contains<S, E>(stream: &mut S, needle: &str) -> String
    where
        S: futures::Stream<Item = Result<axum::body::Bytes, E>> + Unpin,
        E: std::fmt::Debug,
    {
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            let mut text = String::new();
            while let Some(chunk) = stream.next().await {
                let bytes = chunk.unwrap();
                text.push_str(std::str::from_utf8(&bytes).unwrap());
                if text.contains(needle) {
                    return text;
                }
            }
            panic!("SSE stream ended before {needle:?}; received {text:?}");
        })
        .await
        .unwrap_or_else(|_| panic!("timed out waiting for {needle:?}"))
    }

    #[tokio::test]
    async fn resolve_selects_candidate_peer_cohort_from_article_overlap() {
        use std::sync::{Arc, Mutex};

        use axum::extract::State;

        #[derive(Clone)]
        struct FakeState {
            submitted_nzb_urls: Arc<Mutex<Vec<String>>>,
        }

        fn query_value(query: &str, key: &str) -> Option<String> {
            query.split('&').find_map(|pair| {
                let mut parts = pair.splitn(2, '=');
                let name = parts.next()?;
                let value = parts.next().unwrap_or("");
                if name == key {
                    Some(value.replace("%3A", ":").replace("%2F", "/"))
                } else {
                    None
                }
            })
        }

        fn nzb_xml(article_ids: &[&str]) -> String {
            let segments = article_ids
                .iter()
                .enumerate()
                .map(|(index, article_id)| {
                    format!(
                        r#"<segment number="{}" bytes="1000">{}</segment>"#,
                        index + 1,
                        article_id
                    )
                })
                .collect::<Vec<_>>()
                .join("");
            format!(
                r#"<?xml version="1.0" encoding="utf-8"?>
<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">
  <file poster="poster" date="1777937305" subject="{}">
    <groups><group>alt.binaries.test</group></groups>
    <segments>{}</segments>
  </file>
</nzb>"#,
                article_ids.first().copied().unwrap_or("movie"),
                segments
            )
        }

        async fn fake_nzbdav(
            State(state): State<FakeState>,
            req: axum::http::Request<axum::body::Body>,
        ) -> impl IntoResponse {
            let path = req.uri().path().to_string();
            let query = req.uri().query().unwrap_or("").to_string();
            let method = req.method().clone();

            if path == "/api" && query.contains("mode=addurl") {
                let submitted_url = query_value(&query, "name").unwrap_or_default();
                state
                    .submitted_nzb_urls
                    .lock()
                    .unwrap()
                    .push(submitted_url.clone());
                if submitted_url.ends_with("/nzb/candidate-c") {
                    return Json(serde_json::json!({
                        "status": false,
                        "error": "out of space"
                    }))
                    .into_response();
                }
                let nzo_id = if submitted_url.ends_with("/nzb/candidate-a") {
                    "nzo-candidate-a"
                } else if submitted_url.ends_with("/nzb/candidate-d") {
                    "nzo-candidate-d"
                } else {
                    "nzo-1"
                };
                return Json(serde_json::json!({
                    "status": true,
                    "nzo_ids": [nzo_id]
                }))
                .into_response();
            }

            if path == "/api" && query.contains("mode=history") && query.contains("search=") {
                return Json(serde_json::json!({
                    "history": { "slots": [] }
                }))
                .into_response();
            }

            if path == "/api" && query.contains("mode=history") && query.contains("nzo_ids=nzo-1") {
                return Json(serde_json::json!({
                    "history": {
                        "slots": [{
                            "nzo_id": "nzo-1",
                            "status": "Completed",
                            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Inception",
                            "name": "Inception.2010.1080p.BluRay.x264-FGT",
                            "fail_message": ""
                        }]
                    }
                }))
                .into_response();
            }

            if path == "/api"
                && query.contains("mode=history")
                && query.contains("nzo_ids=nzo-candidate-a")
            {
                return Json(serde_json::json!({
                    "history": {
                        "slots": [{
                            "nzo_id": "nzo-candidate-a",
                            "status": "Completed",
                            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/CandidateA",
                            "name": "Candidate A",
                            "fail_message": ""
                        }]
                    }
                }))
                .into_response();
            }

            if path == "/api"
                && query.contains("mode=history")
                && query.contains("nzo_ids=nzo-candidate-d")
            {
                return Json(serde_json::json!({
                    "history": {
                        "slots": [{
                            "nzo_id": "nzo-candidate-d",
                            "status": "Completed",
                            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/CandidateD",
                            "name": "Candidate D",
                            "fail_message": ""
                        }]
                    }
                }))
                .into_response();
            }

            if path == "/api" && query.contains("mode=queue") {
                return Json(serde_json::json!({
                    "queue": { "slots": [] }
                }))
                .into_response();
            }

            if method == Method::GET && path == "/nzb/primary" {
                return (
                    StatusCode::OK,
                    [("content-type", "application/xml")],
                    nzb_xml(&["a@id", "b@id", "c@id", "d@id"]),
                )
                    .into_response();
            }

            if method == Method::GET && path == "/nzb/candidate-a" {
                return (
                    StatusCode::OK,
                    [("content-type", "application/xml")],
                    nzb_xml(&["a@id", "b@id", "c@id", "x@id"]),
                )
                    .into_response();
            }

            if method == Method::GET && path == "/nzb/candidate-b" {
                return (
                    StatusCode::OK,
                    [("content-type", "application/xml")],
                    nzb_xml(&["q@id", "r@id", "s@id", "t@id"]),
                )
                    .into_response();
            }

            if method == Method::GET && path == "/nzb/candidate-c" {
                return (
                    StatusCode::OK,
                    [("content-type", "application/xml")],
                    nzb_xml(&["a@id", "b@id"]),
                )
                    .into_response();
            }

            if method == Method::GET && path == "/nzb/candidate-d" {
                return (
                    StatusCode::OK,
                    [("content-type", "application/xml")],
                    nzb_xml(&["a@id", "b@id", "c@id"]),
                )
                    .into_response();
            }

            if method.as_str() == "PROPFIND" && path == "/content/uncategorized/Inception/" {
                return (
                    StatusCode::MULTI_STATUS,
                    [("content-type", "application/xml")],
                    r#"<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/Inception/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/Inception/Inception.mkv</D:href>
    <D:propstat><D:prop><D:getcontentlength>123456789</D:getcontentlength></D:prop></D:propstat>
  </D:response>
</D:multistatus>"#,
                )
                    .into_response();
            }

            if method.as_str() == "PROPFIND" && path == "/content/uncategorized/CandidateA/" {
                return (
                    StatusCode::MULTI_STATUS,
                    [("content-type", "application/xml")],
                    r#"<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/CandidateA/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/CandidateA/CandidateA.mkv</D:href>
    <D:propstat><D:prop><D:getcontentlength>123456789</D:getcontentlength></D:prop></D:propstat>
  </D:response>
</D:multistatus>"#,
                )
                    .into_response();
            }

            if method.as_str() == "PROPFIND" && path == "/content/uncategorized/CandidateD/" {
                return (
                    StatusCode::MULTI_STATUS,
                    [("content-type", "application/xml")],
                    r#"<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/CandidateD/</D:href>
    <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/CandidateD/CandidateD.mkv</D:href>
    <D:propstat><D:prop><D:getcontentlength>123456789</D:getcontentlength></D:prop></D:propstat>
  </D:response>
</D:multistatus>"#,
                )
                    .into_response();
            }

            if method == Method::GET
                && (path == "/content/uncategorized/CandidateA/CandidateA.mkv"
                    || path == "/content/uncategorized/CandidateD/CandidateD.mkv"
                    || path == "/content/uncategorized/Inception/Inception.mkv")
            {
                let range = query_value(&query, "range").or_else(|| {
                    req.headers()
                        .get("range")
                        .and_then(|value| value.to_str().ok())
                        .map(String::from)
                });
                let body_byte = if path == "/content/uncategorized/CandidateD/CandidateD.mkv" {
                    b"Z"
                } else {
                    b"A"
                };
                let body = match range.as_deref() {
                    Some(range) if range.contains("0-1") => vec![body_byte[0]; 2],
                    Some(range) if range.contains("0-99") => vec![body_byte[0]; 100],
                    Some(range) if range.contains("0-4095") => vec![body_byte[0]; 4096],
                    _ => vec![body_byte[0]; 2],
                };
                let content_range = match body.len() {
                    2 => "bytes 0-1/123456789".to_string(),
                    100 => "bytes 0-99/123456789".to_string(),
                    4096 => "bytes 0-4095/123456789".to_string(),
                    len => format!("bytes 0-{}{}", len - 1, "/123456789"),
                };
                let content_length = body.len().to_string();
                return (
                    StatusCode::PARTIAL_CONTENT,
                    [
                        ("content-type", "application/octet-stream".to_string()),
                        ("content-range", content_range),
                        ("content-length", content_length),
                    ],
                    body,
                )
                    .into_response();
            }

            if method == Method::HEAD && path == "/content/uncategorized/Inception/Inception.mkv" {
                return (
                    StatusCode::OK,
                    [("content-length", "123456789"), ("accept-ranges", "bytes")],
                    "",
                )
                    .into_response();
            }

            StatusCode::NOT_FOUND.into_response()
        }

        let submitted_nzb_urls = Arc::new(Mutex::new(Vec::new()));
        let fake_app = Router::new()
            .route("/*path", any(fake_nzbdav))
            .with_state(FakeState {
                submitted_nzb_urls: submitted_nzb_urls.clone(),
            });
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, fake_app).await.unwrap();
        });
        let base_url = format!("http://{addr}");

        let response = router()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/resolve")
                    .header("content-type", "application/json")
                    .body(axum::body::Body::from(
                        serde_json::json!({
                            "nzb_url": format!("{base_url}/nzb/primary"),
                            "title": "Inception.2010.1080p.BluRay.x264-FGT",
                            "fallback_count": 3,
                            "candidate_peers": [
                                {
                                    "nzb_url": format!("{base_url}/nzb/candidate-a"),
                                    "title": "Candidate A",
                                    "size": 123456,
                                    "indexer": "Hydra",
                                    "extra": {}
                                },
                                {
                                    "nzb_url": format!("{base_url}/nzb/candidate-b"),
                                    "title": "Candidate B",
                                    "size": 123456,
                                    "indexer": "Hydra",
                                    "extra": {}
                                },
                                {
                                    "nzb_url": format!("{base_url}/nzb/candidate-c"),
                                    "title": "Candidate C",
                                    "size": 123456,
                                    "indexer": "Hydra",
                                    "extra": {}
                                },
                                {
                                    "nzb_url": format!("{base_url}/nzb/candidate-d"),
                                    "title": "Candidate D",
                                    "size": 123456,
                                    "indexer": "Hydra",
                                    "extra": {}
                                }
                            ],
                            "poll_interval_secs": 1,
                            "download_timeout_secs": 30,
                            "nzbdav": {
                                "base_url": base_url,
                                "api_key": "secret",
                                "webdav_url": base_url,
                                "webdav_username": "user",
                                "webdav_password": "pass",
                                "webdav_content_root": "content"
                            }
                        })
                        .to_string(),
                    ))
                    .unwrap(),
            )
            .await
            .unwrap();

        assert_eq!(response.status(), StatusCode::OK);
        let body = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        let submitted_urls = submitted_nzb_urls.lock().unwrap().clone();
        assert_eq!(submitted_urls.len(), 4);
        assert_eq!(submitted_urls[0], format!("{base_url}/nzb/primary"));
        assert!(submitted_urls.contains(&format!("{base_url}/nzb/candidate-a")));
        assert!(submitted_urls.contains(&format!("{base_url}/nzb/candidate-c")));
        assert!(submitted_urls.contains(&format!("{base_url}/nzb/candidate-d")));
        assert!(!submitted_urls.contains(&format!("{base_url}/nzb/candidate-b")));

        let cohort = json["peer_cohort"].as_array().unwrap();
        assert_eq!(cohort.len(), 3);
        let candidate_a = cohort
            .iter()
            .find(|peer| peer["title"] == "Candidate A")
            .unwrap();
        let candidate_c = cohort
            .iter()
            .find(|peer| peer["title"] == "Candidate C")
            .unwrap();
        let candidate_d = cohort
            .iter()
            .find(|peer| peer["title"] == "Candidate D")
            .unwrap();
        assert_eq!(
            candidate_a["nzb_url"],
            format!("{base_url}/nzb/candidate-a")
        );
        assert_eq!(candidate_a["state"], "ready");
        assert_eq!(
            candidate_a["validation_state"],
            "byte_sample_validated_phase_3"
        );
        assert_eq!(candidate_a["nzo_id"], "nzo-candidate-a");
        assert_eq!(candidate_a["content_length"], 123456789);
        assert_eq!(
            candidate_a["stream_url"],
            format!("{base_url}/content/uncategorized/CandidateA/CandidateA.mkv")
        );
        assert!(candidate_a["jaccard"].as_f64().unwrap() > 0.0);
        assert_eq!(
            candidate_c["nzb_url"],
            format!("{base_url}/nzb/candidate-c")
        );
        assert_eq!(candidate_c["state"], "submit_failed");
        assert!(candidate_c["submit_error"]
            .as_str()
            .unwrap()
            .contains("out of space"));
        assert_eq!(candidate_d["state"], "rejected");
        assert_eq!(
            candidate_d["validation_state"],
            "byte_sample_mismatch_phase_3"
        );
        assert_eq!(candidate_d["content_length"], 123456789);

        let peers = json["peers"].as_array().unwrap();
        assert_eq!(peers.len(), 4);
        assert!(peers.iter().any(|peer| peer["state"] == "ready"
            && peer["nzo_id"] == "nzo-candidate-a"
            && peer["validation_state"] == "byte_sample_validated_phase_3"));
        assert!(peers
            .iter()
            .any(|peer| peer["state"] == "submit_failed" && peer["nzo_id"].is_null()));
        assert!(peers.iter().any(|peer| peer["state"] == "rejected"
            && peer["validation_state"] == "byte_sample_mismatch_phase_3"));
    }
}
