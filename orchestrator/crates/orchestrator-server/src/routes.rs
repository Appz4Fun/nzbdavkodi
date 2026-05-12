//! HTTP routes — Phase 0.
//!
//! Only `/v1/health` is implemented. Future routes (`/v1/search`,
//! `/v1/resolve`, `/v1/peers/:rid`, `/v1/stream/...`, `/v1/admin/...`)
//! land in their respective migration phases per the plan §5.

use std::time::Instant;

use axum::{
    routing::{get, post},
    Json, Router,
};
use serde::Serialize;
use tracing::info;

use crate::logging::Outcome;

#[derive(Debug, Serialize)]
pub struct HealthPayload {
    pub status: &'static str,
    pub phase: &'static str,
    pub version: &'static str,
}

/// Phase 0 returns a static, non-empty payload. Phase 5+ replaces this
/// with the full health body documented in plan §5.
async fn health() -> Json<HealthPayload> {
    let started = Instant::now();
    let payload = HealthPayload {
        status: "ok",
        phase: "phase-0",
        version: env!("CARGO_PKG_VERSION"),
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::to_bytes;
    use axum::http::{Request, StatusCode};
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
}
