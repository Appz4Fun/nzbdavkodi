// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! NZBHydra2 Newznab XML client. Port of `hydra.py`.
//!
//! The Python module reaches into Kodi globals for settings. In Rust the
//! caller passes a [`HydraConfig`] in — `orchestrator-server`'s admin
//! crate is responsible for materialising it from whatever store it ends
//! up using.

use std::time::Duration;

use tracing::{debug, info, warn};

use crate::caps::{fetch_caps, NewznabCaps};
use crate::http::{http_get, redact_url};
use crate::newznab::{parse_newznab_items, IndexerNameMode};
use crate::planner::{plan_newznab_search, NewznabSearchPlan, ProviderKind};
use crate::types::{Candidate, ProviderError, SearchRequest};

const PROVIDER: &str = "nzbhydra2";

/// User-facing NZBHydra2 search timeout — long because the server fans
/// out to its own configured indexers. Matches Python `timeout=300`.
const HYDRA_SEARCH_TIMEOUT: Duration = Duration::from_secs(300);

/// Inputs the caller passes into the client. The Python version reads
/// these from `xbmcaddon.Addon().getSetting(...)`.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct HydraConfig {
    /// Base URL, e.g. `https://hydra.local:5076`. Trailing slash is OK;
    /// the client strips it.
    pub base_url: String,
    pub api_key: String,
    /// Capped to 1..=10000 (mirrors the Python clamp).
    pub max_results: u32,
}

/// NZBHydra2 client. Stateless beyond the optional caps cache the
/// caller threads in.
#[derive(Debug, Clone)]
pub struct HydraClient {
    config: HydraConfig,
}

impl HydraClient {
    pub fn new(config: HydraConfig) -> Self {
        Self { config }
    }

    /// Borrow the configured base URL with trailing slashes stripped.
    fn base(&self) -> String {
        self.config.base_url.trim_end_matches('/').to_string()
    }

    /// Fetch the provider's caps and return them. Pure pass-through —
    /// in the Python source this also persisted to addon_data; we leave
    /// persistence to the admin crate.
    pub async fn refresh_caps(&self) -> Result<NewznabCaps, ProviderError> {
        fetch_caps(PROVIDER, &self.base(), &self.config.api_key).await
    }

    /// Run a single search, with the same primary/fallback retry shape
    /// as `hydra.search_hydra`.
    ///
    /// `caps` is what the caller has cached. If `None` or the cache is
    /// empty, the planner falls back to the missing-caps default plan
    /// (the legacy behaviour Hydra users actually depended on for years).
    pub async fn search(
        &self,
        request: &SearchRequest,
        caps: Option<&NewznabCaps>,
    ) -> Result<Vec<Candidate>, ProviderError> {
        let plan = plan_newznab_search(
            ProviderKind::Hydra,
            &self.base(),
            request,
            caps,
            &self.config.api_key,
            self.clamped_max_results(),
        );
        if plan.primary.is_empty() {
            info!(
                event = "search.candidate_returned",
                provider = PROVIDER,
                outcome = "skipped",
                reason = "no_supported_query",
                total_candidates = 0,
            );
            return Ok(Vec::new());
        }

        let primary_url = search_url(&self.base(), &plan.primary);
        debug!(
            event = "search.provider_called",
            provider = PROVIDER,
            outcome = "started",
            url_redacted = %redact_url(&primary_url),
        );

        let mut results = self.fetch_and_parse(&primary_url).await?;

        if results.is_empty() {
            if let Some(fallback) = self.derive_fallback(&plan, request, caps.is_some()) {
                if fallback != plan.primary {
                    let fallback_url = search_url(&self.base(), &fallback);
                    info!(
                        event = "search.provider_called",
                        provider = PROVIDER,
                        outcome = "started",
                        reason = "primary_empty",
                        url_redacted = %redact_url(&fallback_url),
                    );
                    results = self.fetch_and_parse(&fallback_url).await?;
                }
            }
        }

        info!(
            event = "search.candidate_returned",
            provider = PROVIDER,
            outcome = "ok",
            total_candidates = results.len(),
        );
        Ok(results)
    }

    fn clamped_max_results(&self) -> u32 {
        // Mirrors the Python clamp: 1..=10_000, default 25 when 0 or
        // unset.
        let raw = if self.config.max_results == 0 {
            25
        } else {
            self.config.max_results
        };
        raw.clamp(1, 10_000)
    }

    /// Legacy title fallback for the case where caps are missing — the
    /// Python code substitutes `q=title` for `imdbid=...` so we still
    /// have a shot at landing on a search.
    fn derive_fallback(
        &self,
        plan: &NewznabSearchPlan,
        request: &SearchRequest,
        had_caps: bool,
    ) -> Option<Vec<(String, String)>> {
        if had_caps {
            return plan.fallback.clone();
        }
        if request.title.is_empty() {
            return None;
        }
        // legacy: copy primary, drop imdbid, add q=title.
        let mut fallback: Vec<(String, String)> = plan
            .primary
            .iter()
            .filter(|(k, _)| k != "imdbid")
            .cloned()
            .collect();
        // Replace any existing q with the title; otherwise append.
        if let Some(slot) = fallback.iter_mut().find(|(k, _)| k == "q") {
            slot.1 = request.title.clone();
        } else {
            fallback.push(("q".into(), request.title.clone()));
        }
        Some(fallback)
    }

    async fn fetch_and_parse(&self, url: &str) -> Result<Vec<Candidate>, ProviderError> {
        let body = match http_get(PROVIDER, url, HYDRA_SEARCH_TIMEOUT).await {
            Ok(b) => b,
            Err(e) => {
                warn!(
                    event = "search.provider_response",
                    provider = PROVIDER,
                    outcome = "error",
                    reason = e.reason(),
                    error = %e,
                );
                return Err(e);
            }
        };
        debug!(
            event = "search.provider_response",
            provider = PROVIDER,
            outcome = "ok",
            http_status = 200,
            bytes = body.len(),
        );
        parse_newznab_items(PROVIDER, &body, &IndexerNameMode::HydraSource)
    }
}

fn search_url(base_url: &str, params: &[(String, String)]) -> String {
    // Hydra's API endpoint is always `{base}/api?{query}`.
    let mut url = format!("{}/api", base_url.trim_end_matches('/'));
    if !params.is_empty() {
        url.push('?');
        let mut first = true;
        for (k, v) in params {
            if !first {
                url.push('&');
            }
            url.push_str(&url_encode(k));
            url.push('=');
            url.push_str(&url_encode(v));
            first = false;
        }
    }
    url
}

fn url_encode(value: &str) -> String {
    url::form_urlencoded::byte_serialize(value.as_bytes()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::SearchKind;

    #[test]
    fn search_url_builds_with_apikey() {
        let url = search_url(
            "https://hydra.test",
            &[
                ("apikey".into(), "K".into()),
                ("t".into(), "movie".into()),
                ("imdbid".into(), "0133093".into()),
            ],
        );
        assert_eq!(
            url,
            "https://hydra.test/api?apikey=K&t=movie&imdbid=0133093"
        );
    }

    #[test]
    fn legacy_fallback_substitutes_q_for_imdbid() {
        let client = HydraClient::new(HydraConfig {
            base_url: "h".into(),
            api_key: "K".into(),
            max_results: 25,
        });
        let plan = NewznabSearchPlan {
            primary: vec![
                ("apikey".into(), "K".into()),
                ("t".into(), "movie".into()),
                ("imdbid".into(), "0133093".into()),
            ],
            fallback: None,
            reason: "missing_caps_movie_default",
        };
        let request = SearchRequest {
            kind: SearchKind::Movie,
            title: "The Matrix".into(),
            year: None,
            imdb_id: Some("tt0133093".into()),
            season: None,
            episode: None,
        };
        let fallback = client.derive_fallback(&plan, &request, false).unwrap();
        assert!(fallback.iter().any(|(k, v)| k == "q" && v == "The Matrix"));
        assert!(!fallback.iter().any(|(k, _)| k == "imdbid"));
    }
}
