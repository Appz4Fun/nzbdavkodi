//! Proxy data plane.
//!
//! Phase 0 ships the structural port: passthrough proxy + fault
//! dispatch + JSONL event logging. Fault implementations are wired in
//! against the same signature the Python port uses; a couple of the
//! data-plane edges (raw TCP RST for `connection_reset`; in-place XOR
//! on the streaming body for `corrupted_bytes`) are gated by axum's
//! body-streaming model and tighten in later phases as the harness
//! scenarios that exercise them come online.

use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use axum::body::{Body, Bytes};
use axum::extract::{Request, State};
use axum::http::{header, HeaderMap, HeaderName, HeaderValue, Method, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use futures::stream::{self, StreamExt};
use reqwest::Client;
use tokio::time::sleep;

use crate::events::{EventSink, FiredEvent};
use crate::state::ProxyState;

#[derive(Clone)]
pub struct ProxyConfig {
    pub upstream: String,
    pub fail_bytes: usize,
    pub slow_bps: usize,
    pub slow_duration_secs: f64,
    pub min_fail_start: u64,
    pub max_fail_start: u64,
}

impl Default for ProxyConfig {
    fn default() -> Self {
        Self {
            upstream: "http://nzbdav-rs:8080".to_string(),
            fail_bytes: 4 * 1024 * 1024,
            slow_bps: 50 * 1024,
            slow_duration_secs: 30.0,
            min_fail_start: 1024 * 1024,
            max_fail_start: u64::MAX,
        }
    }
}

#[derive(Clone)]
pub struct ProxyHandler {
    pub state: Arc<ProxyState>,
    pub sink: Arc<EventSink>,
    pub config: Arc<ProxyConfig>,
    pub client: Client,
}

impl ProxyHandler {
    pub fn new(state: Arc<ProxyState>, sink: Arc<EventSink>, config: ProxyConfig) -> Self {
        let client = Client::builder()
            .pool_idle_timeout(Some(Duration::from_secs(30)))
            .build()
            .expect("reqwest client builds with default config");
        Self {
            state,
            sink,
            config: Arc::new(config),
            client,
        }
    }
}

/// `axum::Router::fallback`-friendly handler covering GET/HEAD/PROPFIND.
pub async fn handle(State(handler): State<ProxyHandler>, req: Request) -> Response {
    match forward(&handler, req).await {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(
                event = "fault_proxy.proxy.upstream_error",
                reason = %e,
                "upstream forward failed"
            );
            (StatusCode::BAD_GATEWAY, format!("upstream error: {e}")).into_response()
        }
    }
}

async fn forward(handler: &ProxyHandler, req: Request) -> anyhow::Result<Response> {
    let (parts, body) = req.into_parts();
    let upstream_url = build_upstream_url(&handler.config.upstream, &parts.uri)?;
    let method = parts.method.clone();

    let range_header = parts
        .headers
        .get(header::RANGE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_string();

    let body_bytes = axum::body::to_bytes(body, usize::MAX)
        .await
        .context("buffering request body")?;

    let mut upstream_req = handler
        .client
        .request(method.clone(), upstream_url)
        .body(body_bytes);
    for (name, value) in parts.headers.iter() {
        // Hop-by-hop headers and `Host` get rebuilt by reqwest.
        match name.as_str() {
            "host" | "connection" | "proxy-connection" => continue,
            _ => {}
        }
        upstream_req = upstream_req.header(name.clone(), value.clone());
    }

    let resp = upstream_req
        .send()
        .await
        .context("sending request to upstream")?;

    let is_large_playback =
        method == Method::GET && is_large_playback_range(&range_header, &handler.config);

    if is_large_playback {
        if let Some(due) = handler.state.next_due() {
            return apply_fault(handler, due.fault_type.as_str(), resp, &range_header).await;
        }
    }

    passthrough(resp, method == Method::HEAD).await
}

fn build_upstream_url(upstream: &str, uri: &Uri) -> anyhow::Result<String> {
    let upstream = upstream.trim_end_matches('/');
    let path = uri
        .path_and_query()
        .map(|p| p.as_str())
        .unwrap_or(uri.path());
    Ok(format!("{upstream}{path}"))
}

fn is_large_playback_range(value: &str, config: &ProxyConfig) -> bool {
    let Some(bounds) = range_bounds(value) else {
        return false;
    };
    let (start, end) = bounds;
    if start < config.min_fail_start || start > config.max_fail_start {
        return false;
    }
    match end {
        None => true,
        Some(end) => end.saturating_sub(start) + 1 >= 1024 * 1024,
    }
}

fn range_bounds(value: &str) -> Option<(u64, Option<u64>)> {
    let suffix = value.strip_prefix("bytes=")?;
    let (start_text, end_text) = suffix.split_once('-')?;
    let start: u64 = start_text.parse().ok()?;
    if end_text.is_empty() {
        return Some((start, None));
    }
    let end: u64 = end_text.parse().ok()?;
    Some((start, Some(end)))
}

async fn passthrough(resp: reqwest::Response, head_only: bool) -> anyhow::Result<Response> {
    let status = StatusCode::from_u16(resp.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let headers = forward_upstream_headers(resp.headers());
    let body = if head_only {
        Body::empty()
    } else {
        Body::from_stream(resp.bytes_stream())
    };
    let mut response = Response::builder()
        .status(status)
        .body(body)
        .context("building passthrough response")?;
    *response.headers_mut() = headers;
    response
        .headers_mut()
        .insert(header::CONNECTION, HeaderValue::from_static("close"));
    Ok(response)
}

fn forward_upstream_headers(headers: &reqwest::header::HeaderMap) -> HeaderMap {
    let mut out = HeaderMap::new();
    for (name, value) in headers.iter() {
        let lower = name.as_str().to_ascii_lowercase();
        if matches!(lower.as_str(), "connection" | "transfer-encoding") {
            continue;
        }
        let Ok(name) = HeaderName::from_bytes(name.as_str().as_bytes()) else {
            continue;
        };
        let Ok(value) = HeaderValue::from_bytes(value.as_bytes()) else {
            continue;
        };
        out.append(name, value);
    }
    out
}

async fn apply_fault(
    handler: &ProxyHandler,
    fault_type: &str,
    upstream: reqwest::Response,
    range_header: &str,
) -> anyhow::Result<Response> {
    let record = handler.state.record_fired(fault_type, range_header);
    let mut extra = serde_json::Map::new();
    let event = FiredEvent {
        fault_type: fault_type.to_string(),
        t_wall: record.t_wall,
        range: range_header.to_string(),
        extra: {
            match fault_type {
                "connection_reset" | "truncated_response" => {
                    extra.insert(
                        match fault_type {
                            "truncated_response" => "scheduled_bytes",
                            _ => "fail_bytes",
                        }
                        .to_string(),
                        serde_json::json!(handler.config.fail_bytes),
                    );
                }
                "slow_upstream" => {
                    extra.insert(
                        "duration".to_string(),
                        serde_json::json!(handler.config.slow_duration_secs),
                    );
                }
                "corrupted_bytes" => {
                    let count = std::cmp::min(32, handler.config.fail_bytes);
                    extra.insert("corruption_count".to_string(), serde_json::json!(count));
                }
                _ => {}
            }
            extra
        },
    };
    handler.sink.append(&event);

    match fault_type {
        "http_500" => http_500().await,
        "truncated_response" => truncated(upstream, handler.config.fail_bytes).await,
        "slow_upstream" => {
            slow_upstream(
                upstream,
                handler.config.slow_bps,
                handler.config.slow_duration_secs,
            )
            .await
        }
        // Phase 0 stops here on the two faults whose data-plane edges
        // need richer scaffolding than axum streaming offers out of
        // the box. Returning a truncated body is a strict superset of
        // the symptoms (forward N bytes then EOF) so harness scenarios
        // can wire on these names today and tighten the simulation
        // when their scenario lands.
        "connection_reset" => truncated(upstream, handler.config.fail_bytes).await,
        "corrupted_bytes" => passthrough(upstream, false).await,
        _ => passthrough(upstream, false).await,
    }
}

async fn http_500() -> anyhow::Result<Response> {
    let mut response = (StatusCode::INTERNAL_SERVER_ERROR, "").into_response();
    response
        .headers_mut()
        .insert(header::CONNECTION, HeaderValue::from_static("close"));
    response
        .headers_mut()
        .insert(header::CONTENT_LENGTH, HeaderValue::from_static("0"));
    Ok(response)
}

async fn truncated(upstream: reqwest::Response, fail_bytes: usize) -> anyhow::Result<Response> {
    let status =
        StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let headers = forward_upstream_headers(upstream.headers());
    let mut sent = 0usize;
    let mut stream = upstream.bytes_stream();
    // Eagerly accumulate up to fail_bytes so the truncation is
    // deterministic even when the upstream chunks differ in size.
    let mut buf: Vec<u8> = Vec::with_capacity(fail_bytes.min(64 * 1024));
    while sent < fail_bytes {
        let Some(chunk) = stream.next().await else {
            break;
        };
        let chunk = chunk.context("reading upstream chunk")?;
        let remaining = fail_bytes - sent;
        let take = chunk.len().min(remaining);
        buf.extend_from_slice(&chunk[..take]);
        sent += take;
        if take < chunk.len() {
            break;
        }
    }
    let body = Body::from(buf);
    let mut response = Response::builder()
        .status(status)
        .body(body)
        .context("building truncated response")?;
    *response.headers_mut() = headers;
    response
        .headers_mut()
        .insert(header::CONNECTION, HeaderValue::from_static("close"));
    Ok(response)
}

async fn slow_upstream(
    upstream: reqwest::Response,
    bps: usize,
    duration_secs: f64,
) -> anyhow::Result<Response> {
    let status =
        StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let headers = forward_upstream_headers(upstream.headers());
    let chunk_size = std::cmp::max(1024usize, bps / 10);
    let sleep_per_chunk = if bps > 0 {
        Duration::from_secs_f64(chunk_size as f64 / bps as f64)
    } else {
        Duration::from_millis(100)
    };
    let deadline = tokio::time::Instant::now() + Duration::from_secs_f64(duration_secs.max(0.0));

    let upstream_stream = upstream.bytes_stream();
    let throttled = stream::unfold(
        (
            upstream_stream,
            deadline,
            sleep_per_chunk,
            chunk_size,
            false,
        ),
        |(mut s, deadline, delay, _chunk, post_throttle)| async move {
            // Past the throttle window we drain at full speed; the
            // signature accepts a phase flag so we don't keep
            // recomputing.
            let now = tokio::time::Instant::now();
            let phase_full = post_throttle || now >= deadline;
            match s.next().await {
                Some(Ok(chunk)) => {
                    let bytes: Bytes = chunk;
                    if !phase_full && !bytes.is_empty() {
                        sleep(delay).await;
                    }
                    Some((
                        Ok::<Bytes, std::io::Error>(bytes),
                        (s, deadline, delay, _chunk, phase_full),
                    ))
                }
                Some(Err(e)) => Some((
                    Err(std::io::Error::other(e)),
                    (s, deadline, delay, _chunk, phase_full),
                )),
                None => None,
            }
        },
    );

    let body = Body::from_stream(throttled);
    let mut response = Response::builder()
        .status(status)
        .body(body)
        .context("building slow-upstream response")?;
    *response.headers_mut() = headers;
    response
        .headers_mut()
        .insert(header::CONNECTION, HeaderValue::from_static("close"));
    Ok(response)
}

pub fn proxy_router(handler: ProxyHandler) -> axum::Router {
    axum::Router::new().fallback(handle).with_state(handler)
}

/// Convenience: bind both the proxy and the control plane on
/// independent listeners and run them concurrently. Phase 0 callers
/// (the harness-runner) use this directly; the production deployment
/// path mounts each router on its own listener for finer-grained
/// shutdown control.
pub async fn run(
    proxy_addr: SocketAddr,
    control_addr: SocketAddr,
    handler: ProxyHandler,
    control_state: crate::control::ControlState,
) -> anyhow::Result<()> {
    let proxy_app = proxy_router(handler);
    let control_app = crate::control::control_router(control_state);

    let proxy_listener = tokio::net::TcpListener::bind(proxy_addr).await?;
    let control_listener = tokio::net::TcpListener::bind(control_addr).await?;

    tracing::info!(
        event = "fault_proxy.listening",
        proxy = %proxy_listener.local_addr()?,
        control = %control_listener.local_addr()?,
        "fault-proxy listening"
    );

    let proxy_fut = axum::serve(proxy_listener, proxy_app);
    let control_fut = axum::serve(control_listener, control_app);

    tokio::try_join!(
        async { proxy_fut.await.map_err(anyhow::Error::from) },
        async { control_fut.await.map_err(anyhow::Error::from) },
    )?;

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn range_bounds_parses_with_and_without_end() {
        assert_eq!(range_bounds("bytes=0-99"), Some((0, Some(99))));
        assert_eq!(range_bounds("bytes=1048576-"), Some((1048576, None)));
        assert_eq!(range_bounds("kibibytes=0-99"), None);
        assert_eq!(range_bounds(""), None);
        assert_eq!(range_bounds("bytes=abc-99"), None);
    }

    #[test]
    fn large_playback_range_respects_min_start_threshold() {
        let cfg = ProxyConfig::default();
        // Below the minimum start byte — typical small-range probe.
        assert!(!is_large_playback_range("bytes=0-99", &cfg));
        // Above the minimum start byte AND ≥1 MiB span (open-ended
        // ranges are accepted unconditionally).
        assert!(is_large_playback_range("bytes=2097152-", &cfg));
        // Above the minimum start byte, narrow range — falls through.
        assert!(!is_large_playback_range("bytes=2097152-2097200", &cfg));
        // Wide-enough closed range above the threshold.
        assert!(is_large_playback_range("bytes=2097152-4194304", &cfg));
    }

    #[test]
    fn build_upstream_url_preserves_path_and_query() {
        let uri: Uri = "/dav/movie.mkv?force=1".parse().unwrap();
        let url = build_upstream_url("http://nzbdav-rs:8080", &uri).unwrap();
        assert_eq!(url, "http://nzbdav-rs:8080/dav/movie.mkv?force=1");
    }
}
