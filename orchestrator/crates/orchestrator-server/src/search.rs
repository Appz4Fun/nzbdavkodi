//! `POST /v1/search` — composes provider clients + filter into a
//! single HTTP-shaped pipeline that the addon's `router.py` can drive
//! when the `use_orchestrator` setting is on.
//!
//! Plan §5 sketches this route. The plan also describes
//! `/v1/admin/indexers` as the production source for provider
//! configuration; pending that store, this route accepts the same
//! information inline in the request body. The Python addon already
//! has the values in `settings.xml` and can forward them on each
//! search until the admin store lands.

use std::time::Instant;

use axum::{http::StatusCode, Json};
use futures::future::join_all;
use orchestrator_filter::filter::{filter_results, FilterInput, FilterOutput};
use orchestrator_filter::settings::FilterSettings;
use orchestrator_providers::{
    direct::{DirectIndexer, DirectIndexerClient},
    hydra::{HydraClient, HydraConfig},
    prowlarr::{ProwlarrClient, ProwlarrConfig},
    Candidate, SearchRequest,
};
use serde::{Deserialize, Serialize};
use tracing::info;
use ulid::Ulid;

use crate::logging::Outcome;

/// Inline provider configuration for the request. Pre-admin-API
/// shape — the Python addon serialises its `settings.xml` indexer
/// section into this and hands it over on every search.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct ProvidersConfig {
    pub hydra: Option<HydraConfig>,
    pub prowlarr: Option<ProwlarrConfig>,
    /// Direct Newznab indexers (nzbgeek, drunkenslug, ...). Same
    /// shape the Python `direct_indexers.py` reads out of the
    /// addon's settings.
    pub direct: Vec<DirectIndexer>,
    /// Per-indexer cap on results returned by the direct fan-out.
    /// Defaults to 100 to mirror the Python `_read_max_results`
    /// default. Cap stays under 10_000 to match the provider's
    /// internal clamp.
    pub direct_max_results: Option<u32>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct SearchBody {
    pub search: SearchRequest,
    #[serde(default)]
    pub settings: FilterSettings,
    #[serde(default)]
    pub providers: ProvidersConfig,
}

#[derive(Debug, Serialize)]
pub struct SearchResponse {
    pub search_id: String,
    pub total_candidates: usize,
    pub filtered: FilterOutput,
    /// Per-provider outcome — error reasons surface here so the
    /// caller can render a clear "Hydra unavailable" notice without
    /// having to scrape Loki.
    pub providers: Vec<ProviderOutcome>,
}

#[derive(Debug, Serialize)]
pub struct ProviderOutcome {
    pub provider: String,
    pub candidate_count: usize,
    pub error: Option<String>,
    pub duration_ms: u64,
}

pub async fn search(
    Json(body): Json<SearchBody>,
) -> Result<Json<SearchResponse>, (StatusCode, String)> {
    let request_id = Ulid::new().to_string();
    let started = Instant::now();

    info!(
        event = "search.requested",
        request_id = %request_id,
        outcome = Outcome::Started.as_str(),
        title = %body.search.title,
        kind = ?body.search.kind,
        "search received"
    );

    // Fan out across configured providers in parallel — same pattern
    // as `_run_indexer_fanout` in the Python source but expressed via
    // tokio + futures::join_all.
    let mut provider_tasks: Vec<ProviderTask> = Vec::new();
    if let Some(cfg) = body.providers.hydra.as_ref() {
        provider_tasks.push(ProviderTask::Hydra(HydraClient::new(cfg.clone())));
    }
    if let Some(cfg) = body.providers.prowlarr.as_ref() {
        provider_tasks.push(ProviderTask::Prowlarr(ProwlarrClient::new(cfg.clone())));
    }
    if !body.providers.direct.is_empty() {
        provider_tasks.push(ProviderTask::Direct(DirectIndexerClient::new(
            body.providers.direct.clone(),
            body.providers.direct_max_results.unwrap_or(100),
        )));
    }

    if provider_tasks.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            "no providers configured: pass at least one of hydra / prowlarr / direct".to_string(),
        ));
    }

    let request_for_search = body.search.clone();
    let futures = provider_tasks
        .into_iter()
        .map(|task| run_provider(task, request_for_search.clone()));
    let provider_results: Vec<RanProvider> = join_all(futures).await;

    let mut all_candidates: Vec<Candidate> = Vec::new();
    let mut outcomes: Vec<ProviderOutcome> = Vec::with_capacity(provider_results.len());
    for ran in provider_results {
        outcomes.push(ProviderOutcome {
            provider: ran.provider,
            candidate_count: ran.candidates.len(),
            error: ran.error,
            duration_ms: ran.duration_ms,
        });
        all_candidates.extend(ran.candidates);
    }

    let total_candidates = all_candidates.len();

    // Map providers' Candidate → filter's FilterInput. Only the
    // title / size / pubdate fields move into the filter; everything
    // else is preserved on the response side via the per-input echo
    // (FilterOutput::filtered carries the FilterInput verbatim).
    let inputs: Vec<FilterInput> = all_candidates
        .iter()
        .map(|c| FilterInput {
            title: c.title.clone(),
            size_bytes: c.size,
            pubdate: c.pubdate.clone(),
        })
        .collect();

    let filtered = filter_results(inputs, &body.settings);

    let duration_ms = started.elapsed().as_millis() as u64;
    info!(
        event = "search.candidate_returned",
        request_id = %request_id,
        outcome = Outcome::Ok.as_str(),
        duration_ms = duration_ms,
        total_candidates = total_candidates as u64,
        filtered_in = filtered.filtered.len() as u64,
        "search completed"
    );

    Ok(Json(SearchResponse {
        search_id: request_id,
        total_candidates,
        filtered,
        providers: outcomes,
    }))
}

enum ProviderTask {
    Hydra(HydraClient),
    Prowlarr(ProwlarrClient),
    Direct(DirectIndexerClient),
}

struct RanProvider {
    provider: String,
    candidates: Vec<Candidate>,
    error: Option<String>,
    duration_ms: u64,
}

async fn run_provider(task: ProviderTask, request: SearchRequest) -> RanProvider {
    let started = Instant::now();
    match task {
        ProviderTask::Hydra(client) => {
            let label = "nzbhydra2".to_string();
            // Pass `None` caps for now; the legacy missing-caps
            // fallback in HydraClient kicks in. The admin-API crate
            // takes over the caps cache in a follow-up.
            match client.search(&request, None).await {
                Ok(candidates) => RanProvider {
                    provider: label,
                    candidates,
                    error: None,
                    duration_ms: started.elapsed().as_millis() as u64,
                },
                Err(e) => RanProvider {
                    provider: label,
                    candidates: Vec::new(),
                    error: Some(e.to_string()),
                    duration_ms: started.elapsed().as_millis() as u64,
                },
            }
        }
        ProviderTask::Prowlarr(client) => {
            let label = "prowlarr".to_string();
            match client.search(&request).await {
                Ok(candidates) => RanProvider {
                    provider: label,
                    candidates,
                    error: None,
                    duration_ms: started.elapsed().as_millis() as u64,
                },
                Err(e) => RanProvider {
                    provider: label,
                    candidates: Vec::new(),
                    error: Some(e.to_string()),
                    duration_ms: started.elapsed().as_millis() as u64,
                },
            }
        }
        ProviderTask::Direct(client) => {
            let outcomes = client.search(&request).await;
            // Each `IndexerOutcome` contains candidates + per-indexer
            // error string. Flatten into one Direct provider with a
            // merged error report.
            let mut candidates = Vec::new();
            let mut errors: Vec<String> = Vec::new();
            for o in outcomes {
                let id = o.indexer_id.clone();
                candidates.extend(o.candidates);
                if let Some(err) = o.error {
                    errors.push(format!("{id}: {err}"));
                }
            }
            RanProvider {
                provider: "direct".to_string(),
                candidates,
                error: if errors.is_empty() {
                    None
                } else {
                    Some(errors.join("; "))
                },
                duration_ms: started.elapsed().as_millis() as u64,
            }
        }
    }
}
#[cfg(test)]
mod tests {
    use crate::router;
    use axum::body::to_bytes;
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    fn body(value: serde_json::Value) -> axum::body::Body {
        axum::body::Body::from(value.to_string())
    }

    #[tokio::test]
    async fn search_rejects_empty_provider_config() {
        let app = router();
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/search")
                    .header("content-type", "application/json")
                    .body(body(serde_json::json!({
                        "search": {
                            "kind": "movie",
                            "title": "Inception",
                            "year": 2010
                        },
                        "settings": {},
                        "providers": {}
                    })))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::BAD_REQUEST);
        let bytes = to_bytes(res.into_body(), usize::MAX).await.unwrap();
        let text = String::from_utf8(bytes.to_vec()).unwrap();
        assert!(
            text.contains("no providers configured"),
            "expected explanatory error, got: {text}"
        );
    }

    #[tokio::test]
    async fn search_returns_provider_outcomes_when_provider_unreachable() {
        // Point Hydra at a definitely-unroutable port so the call
        // fails fast. The response should still be 200 with the
        // provider error reflected in the outcomes vector — partial
        // failure does not fail the whole search, matching the
        // Python pipeline's behaviour.
        let app = router();
        let res = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/search")
                    .header("content-type", "application/json")
                    .body(body(serde_json::json!({
                        "search": {
                            "kind": "movie",
                            "title": "Inception",
                            "year": 2010
                        },
                        "settings": {},
                        "providers": {
                            "hydra": {
                                "base_url": "http://127.0.0.1:1",
                                "api_key": "noop",
                                "max_results": 25,
                                "search_timeout_secs": 1
                            }
                        }
                    })))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(res.status(), StatusCode::OK);
        let bytes = to_bytes(res.into_body(), usize::MAX).await.unwrap();
        let parsed: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(parsed["total_candidates"], 0);
        let providers = parsed["providers"].as_array().unwrap();
        assert_eq!(providers.len(), 1);
        assert_eq!(providers[0]["provider"], "nzbhydra2");
        assert_eq!(providers[0]["candidate_count"], 0);
        assert!(
            providers[0]["error"].is_string(),
            "expected error field to carry the connection failure"
        );
    }
}
