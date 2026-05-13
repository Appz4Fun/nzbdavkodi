// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! `POST /v1/resolve` — Phase 2 single-peer resolve.

use axum::{extract::State, http::StatusCode, Json};
use orchestrator_core::resolve::{
    resolve_single_peer, resolve_single_peer_with_progress, ResolveError, ResolveProgressEvent,
    ResolveRequest,
};
use serde::Serialize;
use serde_json::json;
use tracing::{error, info, warn};

use crate::logging::Outcome;
use crate::routes::PeerPoolState;

#[derive(Debug, Serialize)]
pub struct ErrorBody {
    error: String,
    reason: &'static str,
}

pub async fn resolve(
    Json(body): Json<ResolveRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ErrorBody>)> {
    resolve_impl(body, None).await
}

pub async fn resolve_with_peer_pool(
    State(state): State<PeerPoolState>,
    Json(body): Json<ResolveRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ErrorBody>)> {
    resolve_impl(body, Some(state)).await
}

async fn resolve_impl(
    body: ResolveRequest,
    state: Option<PeerPoolState>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ErrorBody>)> {
    let peer_pool_cache_key = body.peer_pool_cache_key.clone();
    let requested_resolve_id = body
        .resolve_id
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned);
    if let (Some(state), Some(cache_key)) = (state.as_ref(), peer_pool_cache_key.as_deref()) {
        match state
            .peer_pool
            .get_latest_response_by_cache_key_with_policy(cache_key, state.cache_policy)
        {
            Ok(Some(cached)) => {
                let event_resolve_id = record_cache_hit_event(
                    state,
                    &cached,
                    cache_key,
                    requested_resolve_id.as_deref(),
                );
                info!(
                    event = "resolve.cache_hit",
                    resolve_id = event_resolve_id.unwrap_or_default(),
                    outcome = Outcome::Ok.as_str(),
                    "resolve returned cached peer pool"
                );
                return Ok(Json(cached));
            }
            Ok(None) => {}
            Err(error) => {
                warn!(
                    event = "peer_pool.cache_lookup_failed",
                    outcome = Outcome::Error.as_str(),
                    reason = %error,
                    "peer-pool cache lookup failed; falling through to live resolve"
                );
            }
        }
    }

    let progress_state = state.clone();
    let resolved = match progress_state {
        Some(progress_state) => {
            resolve_single_peer_with_progress(body, move |event| {
                record_progress_event(&progress_state, event);
            })
            .await
        }
        None => resolve_single_peer(body).await,
    };

    match resolved {
        Ok(response) => {
            if let Some(state) = state {
                if let Err(error) = state
                    .peer_pool
                    .save_response_with_cache_key(&response, peer_pool_cache_key.as_deref())
                {
                    warn!(
                        event = "peer_pool.persist_failed",
                        resolve_id = %response.resolve_id,
                        outcome = Outcome::Error.as_str(),
                        reason = %error,
                        "peer-pool persistence failed"
                    );
                }
            }
            info!(
                event = "resolve.completed",
                resolve_id = %response.resolve_id,
                peer_id = %response.primary_peer_id,
                outcome = Outcome::Ok.as_str(),
                "resolve completed"
            );
            Ok(Json(
                serde_json::to_value(&response).expect("resolve response serializes"),
            ))
        }
        Err(err) => {
            let status = status_for_error(&err);
            error!(
                event = "resolve.failed",
                outcome = Outcome::Error.as_str(),
                reason = err.reason(),
                http_status = status.as_u16() as u64,
                "resolve failed"
            );
            Err((
                status,
                Json(ErrorBody {
                    error: err.to_string(),
                    reason: err.reason(),
                }),
            ))
        }
    }
}

fn status_for_error(err: &ResolveError) -> StatusCode {
    match err {
        ResolveError::InvalidRequest(_) => StatusCode::BAD_REQUEST,
        ResolveError::PollTimeout(_) => StatusCode::GATEWAY_TIMEOUT,
        ResolveError::JobFailed(_) => StatusCode::BAD_GATEWAY,
        ResolveError::Submit(_)
        | ResolveError::Webdav(_)
        | ResolveError::Nzbdav(_)
        | ResolveError::WebdavSource(_) => StatusCode::BAD_GATEWAY,
    }
}

fn record_cache_hit_event<'a>(
    state: &PeerPoolState,
    cached: &'a serde_json::Value,
    cache_key: &str,
    requested_resolve_id: Option<&'a str>,
) -> Option<String> {
    let cached_resolve_id = cached.get("resolve_id").and_then(|value| value.as_str())?;
    let event_resolve_id = requested_resolve_id
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or(cached_resolve_id);
    let primary_peer_id = cached
        .get("primary_peer_id")
        .and_then(|value| value.as_str());
    let peer_count = cached
        .get("peers")
        .and_then(|value| value.as_array())
        .map_or(0, |peers| peers.len());
    if let Err(error) = state.peer_pool.append_resolve_event(
        event_resolve_id,
        "resolve.cache_hit",
        primary_peer_id,
        "ready",
        None,
        json!({
            "cache_key": cache_key,
            "cached_resolve_id": cached_resolve_id,
            "peer_count": peer_count,
        }),
    ) {
        warn!(
            event = "resolve.event_persist_failed",
            resolve_id = %event_resolve_id,
            outcome = Outcome::Error.as_str(),
            reason = %error,
            "resolve cache-hit event persistence failed"
        );
    }
    Some(event_resolve_id.to_string())
}

fn record_progress_event(state: &PeerPoolState, event: ResolveProgressEvent) {
    if let Err(error) = state.peer_pool.append_resolve_event(
        &event.resolve_id,
        event.event,
        event.peer_id.as_deref(),
        event.state,
        event.reason.as_deref(),
        event.payload,
    ) {
        warn!(
            event = "resolve.event_persist_failed",
            resolve_id = %event.resolve_id,
            peer_id = %event.peer_id.as_deref().unwrap_or(""),
            outcome = Outcome::Error.as_str(),
            reason = %error,
            "resolve progress event persistence failed"
        );
    }
}
