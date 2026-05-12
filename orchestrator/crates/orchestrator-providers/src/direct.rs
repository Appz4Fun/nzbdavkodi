// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! Direct Newznab indexer fan-out. Port of `direct_indexers.py`.
//!
//! In the Python source the fan-out runs on a `ThreadPoolExecutor` with
//! 4 workers and a per-call 20 s timeout. In Rust we use
//! `futures::future::join_all` over `tokio::time::timeout` per-call —
//! cleaner async equivalent that preserves the same semantics (best-
//! effort merge of all indexer responses, partial errors don't fail the
//! whole fan-out).

use std::time::Duration;

use futures::future::join_all;
use tracing::{debug, info, warn};

use crate::caps::{normalize_api_endpoint, NewznabCaps};
use crate::http::{http_get, redact_url};
use crate::newznab::{parse_newznab_items, IndexerNameMode};
use crate::planner::{plan_newznab_search, ProviderKind};
use crate::types::{Candidate, ProviderError, SearchRequest};

/// Per-indexer call timeout. Mirrors `_DIRECT_FANOUT_TIMEOUT = 20` in
/// the Python source.
const DIRECT_FANOUT_TIMEOUT: Duration = Duration::from_secs(20);

/// Per-request HTTP timeout. Mirrors Python `timeout=15` in
/// `_fetch_indexer`.
const DIRECT_FETCH_TIMEOUT: Duration = Duration::from_secs(15);

/// One configured direct Newznab indexer. Equivalent to the dicts
/// `get_configured_indexers` produces in Python.
#[derive(Debug, Clone)]
pub struct DirectIndexer {
    /// Stable id used both as a logging tag and as the per-row
    /// `Candidate.indexer` fallback when caps don't advertise one.
    /// Examples: `"nzbgeek"`, `"drunkenslug"`, `"custom1"`.
    pub id: String,
    /// User-facing display label.
    pub label: String,
    /// API URL — either the host-only form or a full `/api` endpoint.
    /// [`normalize_api_endpoint`] handles both.
    pub api_url: String,
    pub api_key: String,
    /// Optional cached caps. The Python source stores these in
    /// `indexer_store`; the Rust side gets them threaded through.
    pub caps: Option<NewznabCaps>,
}

#[derive(Debug, Clone)]
pub struct DirectIndexerClient {
    indexers: Vec<DirectIndexer>,
    max_results: u32,
}

/// Per-indexer outcome from a fan-out call. Mirrors the
/// `(indexer, value, error)` triples the Python helper yields.
#[derive(Debug, Clone)]
pub struct IndexerOutcome {
    pub indexer_id: String,
    pub indexer_label: String,
    pub candidates: Vec<Candidate>,
    pub error: Option<String>,
}

impl DirectIndexerClient {
    pub fn new(indexers: Vec<DirectIndexer>, max_results: u32) -> Self {
        Self {
            indexers,
            max_results: max_results.clamp(1, 10_000),
        }
    }

    /// Fan-out search across every configured indexer. Returns the
    /// per-indexer outcomes; the caller can merge them into a single
    /// candidate list and decide what to do with the per-indexer errors
    /// (the Python source logs them and only surfaces an error to the
    /// user when *every* indexer failed).
    pub async fn search(&self, request: &SearchRequest) -> Vec<IndexerOutcome> {
        if self.indexers.is_empty() {
            return Vec::new();
        }

        let max_results = self.max_results;
        let futures = self.indexers.iter().map(|indexer| {
            let indexer = indexer.clone();
            let request = request.clone();
            async move {
                let timed = tokio::time::timeout(
                    DIRECT_FANOUT_TIMEOUT,
                    Self::search_one(&indexer, &request, max_results),
                )
                .await;
                match timed {
                    Ok(Ok(candidates)) => IndexerOutcome {
                        indexer_id: indexer.id.clone(),
                        indexer_label: indexer.label.clone(),
                        candidates,
                        error: None,
                    },
                    Ok(Err(e)) => IndexerOutcome {
                        indexer_id: indexer.id.clone(),
                        indexer_label: indexer.label.clone(),
                        candidates: Vec::new(),
                        error: Some(format!(
                            "Direct indexer {} unavailable: {e}",
                            indexer.label
                        )),
                    },
                    Err(_) => IndexerOutcome {
                        indexer_id: indexer.id.clone(),
                        indexer_label: indexer.label.clone(),
                        candidates: Vec::new(),
                        error: Some(format!(
                            "Direct indexer {} unavailable: timed out after {}s",
                            indexer.label,
                            DIRECT_FANOUT_TIMEOUT.as_secs()
                        )),
                    },
                }
            }
        });

        let outcomes = join_all(futures).await;
        let total_candidates: usize = outcomes.iter().map(|o| o.candidates.len()).sum();
        info!(
            event = "search.candidate_returned",
            provider = "direct",
            outcome = "ok",
            total_candidates = total_candidates,
            per_provider = outcomes
                .iter()
                .map(|o| (o.indexer_id.as_str(), o.candidates.len()))
                .collect::<Vec<_>>()
                .iter()
                .map(|(k, n)| format!("{k}={n}"))
                .collect::<Vec<_>>()
                .join(","),
        );
        outcomes
    }

    /// Fetch caps for every configured indexer in parallel. The Python
    /// `test_configured_indexers` returns `(ok_count, total_count, errors)`
    /// — same shape here, but typed.
    pub async fn test_all(&self) -> TestResult {
        let futures = self.indexers.iter().map(|indexer| {
            let indexer = indexer.clone();
            async move {
                let result = tokio::time::timeout(
                    DIRECT_FANOUT_TIMEOUT,
                    Self::check_one_caps(&indexer),
                )
                .await;
                (indexer, result)
            }
        });
        let outcomes = join_all(futures).await;

        let mut ok_count = 0;
        let mut errors = Vec::new();
        let total = outcomes.len();
        for (indexer, result) in outcomes {
            match result {
                Ok(Ok(true)) => ok_count += 1,
                Ok(Ok(false)) => errors.push(format!(
                    "Direct indexer {} unexpected response",
                    indexer.label
                )),
                Ok(Err(e)) => errors.push(format!(
                    "Direct indexer {} unavailable: {e}",
                    indexer.label
                )),
                Err(_) => errors.push(format!(
                    "Direct indexer {} unavailable: timed out after {}s",
                    indexer.label,
                    DIRECT_FANOUT_TIMEOUT.as_secs()
                )),
            }
        }
        TestResult {
            ok_count,
            total_count: total,
            errors,
        }
    }

    async fn search_one(
        indexer: &DirectIndexer,
        request: &SearchRequest,
        max_results: u32,
    ) -> Result<Vec<Candidate>, ProviderError> {
        let plan = plan_newznab_search(
            ProviderKind::Direct,
            &indexer.api_url,
            request,
            indexer.caps.as_ref(),
            &indexer.api_key,
            max_results,
        );
        if plan.primary.is_empty() {
            return Ok(Vec::new());
        }

        let primary_url = build_search_url(&indexer.api_url, &plan.primary);
        debug!(
            event = "search.provider_called",
            provider = "direct",
            indexer = indexer.id.as_str(),
            outcome = "started",
            url_redacted = %redact_url(&primary_url),
        );

        let mut candidates = Self::fetch(&primary_url, indexer).await?;

        if candidates.is_empty() {
            if let Some(fallback) = &plan.fallback {
                if fallback != &plan.primary && !fallback.is_empty() {
                    let url = build_search_url(&indexer.api_url, fallback);
                    debug!(
                        event = "search.provider_called",
                        provider = "direct",
                        indexer = indexer.id.as_str(),
                        outcome = "started",
                        reason = "primary_empty",
                        url_redacted = %redact_url(&url),
                    );
                    candidates = Self::fetch(&url, indexer).await?;
                }
            }
        }

        Ok(candidates)
    }

    async fn fetch(
        url: &str,
        indexer: &DirectIndexer,
    ) -> Result<Vec<Candidate>, ProviderError> {
        let body = match http_get("direct", url, DIRECT_FETCH_TIMEOUT).await {
            Ok(b) => b,
            Err(e) => {
                warn!(
                    event = "search.provider_response",
                    provider = "direct",
                    indexer = indexer.id.as_str(),
                    outcome = "error",
                    reason = e.reason(),
                    error = %e,
                );
                return Err(e);
            }
        };
        debug!(
            event = "search.provider_response",
            provider = "direct",
            indexer = indexer.id.as_str(),
            outcome = "ok",
            http_status = 200,
            bytes = body.len(),
        );
        // Direct indexers map each row's `indexer` label to the
        // configured display label, matching the Python `parse_results
        // (xml, fallback_indexer)` shape.
        parse_newznab_items(
            "direct",
            &body,
            &IndexerNameMode::Static(indexer.label.clone()),
        )
    }

    async fn check_one_caps(indexer: &DirectIndexer) -> Result<bool, ProviderError> {
        let params = vec![
            ("apikey".to_string(), indexer.api_key.clone()),
            ("t".to_string(), "caps".to_string()),
            ("o".to_string(), "xml".to_string()),
        ];
        let url = build_search_url(&indexer.api_url, &params);
        let body = http_get("direct", &url, DIRECT_FETCH_TIMEOUT).await?;
        Ok(body.contains("<caps") || body.contains("<server") || body.contains("<rss"))
    }
}

#[derive(Debug)]
pub struct TestResult {
    pub ok_count: usize,
    pub total_count: usize,
    pub errors: Vec<String>,
}

/// Build a Newznab API URL from a user-configured api_url + params.
/// Port of `direct_indexers.build_search_url`: strips any pre-existing
/// `apikey/t/o` query params from the configured URL and replaces them
/// with the caller's set, preserving any other extras (some indexers
/// use a static path param like `?ext=yes`).
pub fn build_search_url(api_url: &str, params: &[(String, String)]) -> String {
    let normalized = normalize_api_endpoint(api_url);
    let mut parsed = match url::Url::parse(&normalized) {
        Ok(p) => p,
        Err(_) => return normalized,
    };
    let preserved: Vec<(String, String)> = parsed
        .query_pairs()
        .filter(|(k, _)| {
            let lower = k.to_ascii_lowercase();
            lower != "apikey" && lower != "t" && lower != "o"
        })
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();
    parsed.query_pairs_mut().clear();
    {
        let mut q = parsed.query_pairs_mut();
        for (k, v) in &preserved {
            q.append_pair(k, v);
        }
        for (k, v) in params {
            q.append_pair(k, v);
        }
    }
    parsed.to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn search_url_strips_pre_existing_apikey() {
        let url = build_search_url(
            "https://api.example.com/api?apikey=OLD&keep=this",
            &[
                ("apikey".into(), "NEW".into()),
                ("t".into(), "movie".into()),
            ],
        );
        assert!(url.contains("apikey=NEW"), "got {url}");
        assert!(!url.contains("apikey=OLD"));
        assert!(url.contains("keep=this"));
        assert!(url.contains("t=movie"));
    }

    #[test]
    fn search_url_normalises_host_only_input() {
        let url = build_search_url(
            "https://api.example.com",
            &[("apikey".into(), "K".into())],
        );
        assert!(url.starts_with("https://api.example.com/api"), "got {url}");
    }
}
