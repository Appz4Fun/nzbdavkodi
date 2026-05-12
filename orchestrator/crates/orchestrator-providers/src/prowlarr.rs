// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! Prowlarr aggregator client. Port of `prowlarr.py`.
//!
//! Prowlarr does its own indexer fan-out server-side; we just hand it a
//! list of `indexerIds` and let it merge the responses.

use std::time::Duration;

use tracing::{debug, info, warn};

use crate::http::{http_get, redact_url};
use crate::newznab::{parse_newznab_items, IndexerNameMode};
use crate::types::{Candidate, ProviderError, SearchKind, SearchRequest};

const PROVIDER: &str = "prowlarr";

/// Matches Python `timeout=300`. Prowlarr aggregates many indexers
/// behind one HTTP call so the timeout has to be generous.
const PROWLARR_SEARCH_TIMEOUT: Duration = Duration::from_secs(300);

/// Prowlarr connection config. The Python module reads this from
/// `xbmcaddon.Addon().getSetting(...)`.
#[derive(Debug, Clone)]
pub struct ProwlarrConfig {
    /// Base URL — `https://prowlarr.local`. Trailing slash is OK; the
    /// client strips it.
    pub host: String,
    pub api_key: String,
    /// Indexer IDs to query. An empty list means "skip Prowlarr" — the
    /// Python code returns `([], None)` in that case rather than
    /// erroring, and so do we.
    pub indexer_ids: Vec<String>,
    /// Capped to 1..=10000 (mirrors the Python clamp).
    pub max_results: u32,
}

#[derive(Debug, Clone)]
pub struct ProwlarrClient {
    config: ProwlarrConfig,
}

impl ProwlarrClient {
    pub fn new(config: ProwlarrConfig) -> Self {
        Self { config }
    }

    fn base(&self) -> String {
        self.config.host.trim_end_matches('/').to_string()
    }

    /// Run a search. Returns an empty `Vec` (not an error) when no
    /// indexer IDs are configured, matching the Python behaviour where
    /// "Prowlarr disabled" isn't a failure mode.
    pub async fn search(
        &self,
        request: &SearchRequest,
    ) -> Result<Vec<Candidate>, ProviderError> {
        if self.config.indexer_ids.is_empty() {
            info!(
                event = "search.candidate_returned",
                provider = PROVIDER,
                outcome = "skipped",
                reason = "provider_disabled",
                total_candidates = 0,
            );
            return Ok(Vec::new());
        }

        let mut params = self.build_params(request, /*force_title=*/ false);
        let url = self.build_url(&params);

        debug!(
            event = "search.provider_called",
            provider = PROVIDER,
            outcome = "started",
            url_redacted = %redact_url(&url),
        );

        let mut results = self.fetch_and_parse(&url).await?;

        // Title fallback: if the imdb search returned nothing, retry
        // with q=title. Only meaningful when both were present.
        let has_imdb = request.imdb_id.as_deref().map(|s| !s.is_empty()).unwrap_or(false);
        if results.is_empty() && has_imdb && !request.title.is_empty() {
            params = self.build_params(request, /*force_title=*/ true);
            let fallback_url = self.build_url(&params);
            info!(
                event = "search.provider_called",
                provider = PROVIDER,
                outcome = "started",
                reason = "primary_empty",
                url_redacted = %redact_url(&fallback_url),
            );
            results = self.fetch_and_parse(&fallback_url).await?;
        }

        info!(
            event = "search.candidate_returned",
            provider = PROVIDER,
            outcome = "ok",
            total_candidates = results.len(),
        );
        Ok(results)
    }

    /// Build the parameter list for a single Prowlarr call. Prowlarr
    /// speaks the Newznab dialect but the URL is `/api/v1/search`, not
    /// `/api`.
    fn build_params(
        &self,
        request: &SearchRequest,
        force_title: bool,
    ) -> Vec<(String, String)> {
        let mut params = vec![
            ("apikey".into(), self.config.api_key.clone()),
            ("limit".into(), self.clamped_max_results().to_string()),
        ];
        let imdb = request.imdb_id.as_deref().unwrap_or("");
        let use_imdb = !force_title && !imdb.is_empty();
        match request.kind {
            SearchKind::Tv => {
                params.push(("t".into(), "tvsearch".into()));
                if use_imdb {
                    params.push(("imdbid".into(), imdb.to_string()));
                } else {
                    params.push(("q".into(), request.title.clone()));
                }
                if let Some(season) = request.season {
                    params.push(("season".into(), season.to_string()));
                }
                if let Some(episode) = request.episode {
                    params.push(("ep".into(), episode.to_string()));
                }
            }
            SearchKind::Movie => {
                params.push(("t".into(), "movie".into()));
                if use_imdb {
                    params.push(("imdbid".into(), imdb.to_string()));
                } else {
                    params.push(("q".into(), request.title.clone()));
                }
            }
        }
        params
    }

    fn build_url(&self, params: &[(String, String)]) -> String {
        let mut url = format!("{}/api/v1/search", self.base());
        let mut q = url::form_urlencoded::Serializer::new(String::new());
        for (k, v) in params {
            q.append_pair(k, v);
        }
        for id in &self.config.indexer_ids {
            q.append_pair("indexerIds", id);
        }
        let query = q.finish();
        if !query.is_empty() {
            url.push('?');
            url.push_str(&query);
        }
        url
    }

    fn clamped_max_results(&self) -> u32 {
        let raw = if self.config.max_results == 0 {
            25
        } else {
            self.config.max_results
        };
        raw.clamp(1, 10_000)
    }

    async fn fetch_and_parse(&self, url: &str) -> Result<Vec<Candidate>, ProviderError> {
        let body = match http_get(PROVIDER, url, PROWLARR_SEARCH_TIMEOUT).await {
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
        // Prowlarr rows get prefixed by the per-row Newznab attr
        // `indexer`, so "prowlarr:NZBgeek" etc. The fallback when
        // there's no attr is the bare "prowlarr" label.
        parse_newznab_items(
            PROVIDER,
            &body,
            &IndexerNameMode::PrefixedStatic {
                prefix: PROVIDER.into(),
                fallback: String::new(),
            },
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::SearchKind;

    fn config() -> ProwlarrConfig {
        ProwlarrConfig {
            host: "https://prowlarr.test".into(),
            api_key: "K".into(),
            indexer_ids: vec!["1".into(), "5".into()],
            max_results: 25,
        }
    }

    #[test]
    fn url_includes_repeated_indexer_ids() {
        let client = ProwlarrClient::new(config());
        let url = client.build_url(&[
            ("apikey".into(), "K".into()),
            ("t".into(), "movie".into()),
            ("q".into(), "x".into()),
        ]);
        let occurrences = url.matches("indexerIds=").count();
        assert_eq!(occurrences, 2, "got {url}");
    }

    #[test]
    fn imdb_search_for_movies_when_id_present() {
        let client = ProwlarrClient::new(config());
        let req = SearchRequest {
            kind: SearchKind::Movie,
            title: "M".into(),
            year: None,
            imdb_id: Some("tt0133093".into()),
            season: None,
            episode: None,
        };
        let params = client.build_params(&req, false);
        assert!(params.contains(&("t".into(), "movie".into())));
        assert!(params.contains(&("imdbid".into(), "tt0133093".into())));
        assert!(!params.iter().any(|(k, _)| k == "q"));
    }

    #[test]
    fn force_title_falls_back_to_q() {
        let client = ProwlarrClient::new(config());
        let req = SearchRequest {
            kind: SearchKind::Movie,
            title: "M".into(),
            year: None,
            imdb_id: Some("tt0133093".into()),
            season: None,
            episode: None,
        };
        let params = client.build_params(&req, true);
        assert!(params.contains(&("q".into(), "M".into())));
        assert!(!params.iter().any(|(k, _)| k == "imdbid"));
    }
}
