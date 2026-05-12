//! Control plane HTTP API — `/control/health`, `/control/schedule`,
//! `/control/fired`. Same wire format as `tests/extreme/fault_proxy.py`.

use std::sync::Arc;

use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Json},
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};

use crate::state::{FiredRecord, ProxyState, ScheduledEvent, VALID_FAULT_TYPES};

/// Shared state handle for the control plane router.
#[derive(Clone)]
pub struct ControlState {
    pub state: Arc<ProxyState>,
}

#[derive(Debug, Deserialize)]
pub struct ScheduleRequest {
    #[serde(default)]
    pub events: Vec<ScheduleRequestEvent>,
}

#[derive(Debug, Deserialize)]
pub struct ScheduleRequestEvent {
    pub at_seconds: f64,
    pub fault_type: String,
}

#[derive(Debug, Serialize)]
pub struct ScheduleResponse {
    pub scheduled: usize,
}

#[derive(Debug, Serialize)]
pub struct FiredResponse {
    pub fired: Vec<FiredRecord>,
}

#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub status: &'static str,
}

pub fn control_router(state: ControlState) -> Router {
    Router::new()
        .route("/control/health", get(health))
        .route("/control/schedule", post(schedule))
        .route("/control/fired", get(fired))
        .with_state(state)
}

async fn health() -> Json<HealthResponse> {
    Json(HealthResponse { status: "ok" })
}

async fn schedule(
    State(ctl): State<ControlState>,
    Json(req): Json<ScheduleRequest>,
) -> Result<Json<ScheduleResponse>, (StatusCode, String)> {
    let mut parsed = Vec::with_capacity(req.events.len());
    for ev in req.events {
        if !VALID_FAULT_TYPES.iter().any(|v| *v == ev.fault_type) {
            return Err((
                StatusCode::BAD_REQUEST,
                format!("unknown fault_type: {}", ev.fault_type),
            ));
        }
        parsed.push(ScheduledEvent {
            at_seconds: ev.at_seconds,
            fault_type: ev.fault_type,
        });
    }
    let count = parsed.len();
    ctl.state.replace_schedule(parsed);
    Ok(Json(ScheduleResponse { scheduled: count }))
}

async fn fired(State(ctl): State<ControlState>) -> impl IntoResponse {
    Json(FiredResponse {
        fired: ctl.state.fired_snapshot(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::http::Request;
    use tower::ServiceExt;

    fn body_json(value: serde_json::Value) -> axum::body::Body {
        axum::body::Body::from(value.to_string())
    }

    #[tokio::test]
    async fn schedule_accepts_valid_events_and_records_count() {
        let ctl = ControlState {
            state: Arc::new(ProxyState::new()),
        };
        let app = control_router(ctl.clone());
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/control/schedule")
                    .header("content-type", "application/json")
                    .body(body_json(serde_json::json!({
                        "events": [
                            {"at_seconds": 1.0, "fault_type": "http_500"},
                            {"at_seconds": 2.0, "fault_type": "slow_upstream"}
                        ]
                    })))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let body = to_bytes(res.into_body(), usize::MAX).await.unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(parsed["scheduled"], 2);
        assert_eq!(ctl.state.scheduled_count(), 2);
    }

    #[tokio::test]
    async fn schedule_rejects_unknown_fault_type() {
        let ctl = ControlState {
            state: Arc::new(ProxyState::new()),
        };
        let app = control_router(ctl);
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/control/schedule")
                    .header("content-type", "application/json")
                    .body(body_json(serde_json::json!({
                        "events": [{"at_seconds": 1.0, "fault_type": "carrier_pigeon"}]
                    })))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn fired_returns_recorded_events() {
        let ctl = ControlState {
            state: Arc::new(ProxyState::new()),
        };
        ctl.state.record_fired("http_500", "bytes=0-99");
        let app = control_router(ctl);
        let res = app
            .oneshot(
                Request::builder()
                    .uri("/control/fired")
                    .body(axum::body::Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let body = to_bytes(res.into_body(), usize::MAX).await.unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(parsed["fired"][0]["fault_type"], "http_500");
        assert_eq!(parsed["fired"][0]["range"], "bytes=0-99");
    }
}
