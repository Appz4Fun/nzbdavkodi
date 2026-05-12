// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! Newznab search-query planner. Port of `search_planner.py`.

use crate::caps::NewznabCaps;
use crate::types::{SearchKind, SearchRequest};

/// Hosts that need a generic `t=search` fallback even when the indexer
/// advertises `movie` support — they rate-limit or 500 on imdbid queries.
/// Mirrors `indexer_presets.DIRECT_FALLBACK_HOSTS`.
const DIRECT_FALLBACK_HOSTS: &[&str] = &["dognzb", "nzbplanet", "nzbgeek", "6box"];

/// DOGnzb's tvsearch returns junk on direct queries; force the generic
/// search fallback. Mirrors `DOGNZB_TVSEARCH_FALLBACK_HOSTS`.
const DOGNZB_TVSEARCH_FALLBACK_HOSTS: &[&str] = &["dognzb"];

/// Provider kind for the planner — used in conjunction with the host
/// string to apply the per-host fallback rules above.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderKind {
    /// NZBHydra2 — never applies the per-host fallbacks; Hydra normalises
    /// upstream quirks itself.
    Hydra,
    /// Prowlarr — likewise normalises upstream quirks.
    Prowlarr,
    /// Direct Newznab indexer — applies the per-host fallback list.
    Direct,
}

/// Output of [`plan_newznab_search`]. `primary` is the query we should
/// try first; `fallback` is the query to retry with if `primary` returns
/// zero results. Both are flat `t=...&q=...` parameter sets ready to URL-
/// encode.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NewznabSearchPlan {
    /// Empty when no supported query exists for this title/caps combination.
    pub primary: Vec<(String, String)>,
    pub fallback: Option<Vec<(String, String)>>,
    /// Short symbolic reason — surfaced in event logs.
    /// One of: `missing_caps_movie_default`, `missing_caps_episode_default`,
    /// `movie_imdb`, `movie_title`, `movie_title_search_fallback`,
    /// `direct_movie_title_search_fallback`, `episode_tvsearch`,
    /// `episode_search_fallback`, `no_supported_query`.
    pub reason: &'static str,
}

impl NewznabSearchPlan {
    fn empty(reason: &'static str) -> Self {
        Self {
            primary: Vec::new(),
            fallback: None,
            reason,
        }
    }
}

/// Plan the Newznab query (or queries) for a single provider call.
///
/// 1:1 port of `search_planner.plan_newznab_search`. Behaviour:
/// - When `caps` is absent or empty, fall back to the legacy default
///   queries (uses `imdbid` when present, otherwise `q=`).
/// - When caps are present, use them to pick the most specific query
///   the provider actually supports. `imdbid` beats `q`, `tvsearch`
///   beats generic `search`.
/// - Direct providers on the per-host fallback list ([`DIRECT_FALLBACK_HOSTS`]
///   et al) bypass `imdbid` queries because they're known to misbehave
///   on those.
pub fn plan_newznab_search(
    provider_kind: ProviderKind,
    host: &str,
    request: &SearchRequest,
    caps: Option<&NewznabCaps>,
    api_key: &str,
    max_results: u32,
) -> NewznabSearchPlan {
    let base = base_params(api_key, max_results);

    if missing_caps(caps) {
        return missing_caps_plan(base, request);
    }
    let caps = caps.expect("missing_caps == false");

    match request.kind {
        SearchKind::Tv => episode_plan(base, provider_kind, host, request, caps),
        SearchKind::Movie => movie_plan(base, provider_kind, host, request, caps),
    }
}

fn base_params(api_key: &str, max_results: u32) -> Vec<(String, String)> {
    vec![
        ("apikey".into(), api_key.to_string()),
        ("o".into(), "xml".into()),
        ("limit".into(), max_results.to_string()),
    ]
}

fn missing_caps(caps: Option<&NewznabCaps>) -> bool {
    match caps {
        None => true,
        Some(c) => c.search_types.is_empty(),
    }
}

fn params_with_kind(
    base: &[(String, String)],
    t: &str,
    extras: &[(&str, String)],
) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = base.to_vec();
    out.push(("t".into(), t.to_string()));
    for (k, v) in extras {
        if !v.is_empty() {
            out.push(((*k).into(), v.clone()));
        }
    }
    out
}

fn missing_caps_plan(
    base: Vec<(String, String)>,
    request: &SearchRequest,
) -> NewznabSearchPlan {
    match request.kind {
        SearchKind::Tv => {
            let fallback = if !request.title.is_empty() {
                Some(generic_search(&base, &request.title, None, None))
            } else {
                None
            };
            let imdb_digits = imdb_digits(request.imdb_id.as_deref());
            let mut primary = if !imdb_digits.is_empty() {
                params_with_kind(&base, "tvsearch", &[("imdbid", imdb_digits)])
            } else {
                params_with_kind(&base, "tvsearch", &[("q", request.title.clone())])
            };
            if let Some(s) = request.season {
                primary.push(("season".into(), s.to_string()));
            }
            if let Some(e) = request.episode {
                primary.push(("ep".into(), e.to_string()));
            }
            NewznabSearchPlan {
                primary,
                fallback,
                reason: "missing_caps_episode_default",
            }
        }
        SearchKind::Movie => {
            let fallback = if !request.title.is_empty() {
                Some(generic_search(&base, &request.title, None, None))
            } else {
                None
            };
            let imdb_digits = imdb_digits(request.imdb_id.as_deref());
            let primary = if !imdb_digits.is_empty() {
                params_with_kind(&base, "movie", &[("imdbid", imdb_digits)])
            } else {
                params_with_kind(&base, "movie", &[("q", request.title.clone())])
            };
            NewznabSearchPlan {
                primary,
                fallback,
                reason: "missing_caps_movie_default",
            }
        }
    }
}

fn generic_search(
    base: &[(String, String)],
    title: &str,
    caps: Option<&NewznabCaps>,
    year: Option<u32>,
) -> Vec<(String, String)> {
    // Mirrors Python `_generic_search` semantics: when caps are missing
    // we emit an unconditional `t=search&q=<title>`. When caps are
    // present, only attach `q`/`year` if the provider advertises them
    // as supported on `search`.
    if missing_caps(caps) {
        return params_with_kind(base, "search", &[("q", title.to_string())]);
    }
    let caps = caps.expect("missing_caps false");
    if !caps.supports("search") {
        // Caller checks for empty primary; an unsupported `search` means
        // there's no generic fallback worth issuing.
        return Vec::new();
    }
    let mut extras: Vec<(&str, String)> = Vec::new();
    if !title.is_empty() && caps.supports_param("search", "q") {
        extras.push(("q", title.to_string()));
    }
    if let Some(year) = year {
        if caps.supports_param("search", "year") {
            extras.push(("year", year.to_string()));
        }
    }
    params_with_kind(base, "search", &extras)
}

fn host_contains(host: &str, needles: &[&str]) -> bool {
    let lower = host.to_ascii_lowercase();
    needles.iter().any(|n| lower.contains(&n.to_ascii_lowercase()))
}

fn direct_movie_title_fallback(kind: ProviderKind, host: &str) -> bool {
    matches!(kind, ProviderKind::Direct) && host_contains(host, DIRECT_FALLBACK_HOSTS)
}

fn direct_episode_fallback(kind: ProviderKind, host: &str) -> bool {
    matches!(kind, ProviderKind::Direct)
        && host_contains(host, DOGNZB_TVSEARCH_FALLBACK_HOSTS)
}

fn movie_title_params(
    base: &[(String, String)],
    provider_kind: ProviderKind,
    host: &str,
    request: &SearchRequest,
    caps: &NewznabCaps,
) -> (Vec<(String, String)>, &'static str) {
    if direct_movie_title_fallback(provider_kind, host) {
        return (
            generic_search(base, &request.title, Some(caps), request.year),
            "direct_movie_title_search_fallback",
        );
    }
    let mut extras: Vec<(&str, String)> = Vec::new();
    if caps.supports_param("movie", "q") {
        extras.push(("q", request.title.clone()));
    }
    if let Some(year) = request.year {
        if caps.supports_param("movie", "year") {
            extras.push(("year", year.to_string()));
        }
    }
    if !extras.is_empty() {
        return (params_with_kind(base, "movie", &extras), "movie_title");
    }
    (
        generic_search(base, &request.title, Some(caps), request.year),
        "movie_title_search_fallback",
    )
}

fn movie_plan(
    base: Vec<(String, String)>,
    provider_kind: ProviderKind,
    host: &str,
    request: &SearchRequest,
    caps: &NewznabCaps,
) -> NewznabSearchPlan {
    let imdbid = imdb_digits(request.imdb_id.as_deref());
    let fallback = if !request.title.is_empty() {
        Some(generic_search(&base, &request.title, Some(caps), request.year))
    } else {
        None
    };
    if !imdbid.is_empty() && caps.supports_param("movie", "imdbid") {
        return NewznabSearchPlan {
            primary: params_with_kind(&base, "movie", &[("imdbid", imdbid)]),
            fallback,
            reason: "movie_imdb",
        };
    }
    let (primary, reason) = movie_title_params(&base, provider_kind, host, request, caps);
    if primary.is_empty() {
        return NewznabSearchPlan::empty("no_supported_query");
    }
    NewznabSearchPlan {
        primary,
        fallback,
        reason,
    }
}

fn episode_plan(
    base: Vec<(String, String)>,
    provider_kind: ProviderKind,
    host: &str,
    request: &SearchRequest,
    caps: &NewznabCaps,
) -> NewznabSearchPlan {
    let fallback = if !request.title.is_empty() {
        Some(generic_search(&base, &request.title, Some(caps), None))
    } else {
        None
    };
    if direct_episode_fallback(provider_kind, host) || !caps.supports("tvsearch") {
        // Python returns `(fallback, fallback, "episode_search_fallback")`
        // which becomes "no plan" if fallback is None.
        match &fallback {
            None => return NewznabSearchPlan::empty("no_supported_query"),
            Some(f) => {
                return NewznabSearchPlan {
                    primary: f.clone(),
                    fallback: Some(f.clone()),
                    reason: "episode_search_fallback",
                }
            }
        }
    }

    let mut params = params_with_kind(&base, "tvsearch", &[]);
    if !request.title.is_empty() && caps.supports_param("tvsearch", "q") {
        params.push(("q".into(), request.title.clone()));
    }
    let imdbid = imdb_digits(request.imdb_id.as_deref());
    if !imdbid.is_empty() && caps.supports_param("tvsearch", "imdbid") {
        params.push(("imdbid".into(), imdbid));
    }
    if let Some(season) = request.season {
        if caps.supports_param("tvsearch", "season") {
            params.push(("season".into(), season.to_string()));
        }
    }
    if let Some(episode) = request.episode {
        if caps.supports_param("tvsearch", "ep") {
            params.push(("ep".into(), episode.to_string()));
        }
    }
    NewznabSearchPlan {
        primary: params,
        fallback,
        reason: "episode_tvsearch",
    }
}

fn imdb_digits(imdb: Option<&str>) -> String {
    let imdb = match imdb {
        Some(s) => s,
        None => return String::new(),
    };
    let trimmed = imdb.strip_prefix("tt").unwrap_or(imdb);
    trimmed.chars().filter(|c| c.is_ascii_digit()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn caps_with(types: &[&str], params: &[(&str, &[&str])]) -> NewznabCaps {
        let mut supported_params = BTreeMap::new();
        for (t, ps) in params {
            supported_params.insert(
                (*t).to_string(),
                ps.iter().map(|p| (*p).to_string()).collect(),
            );
        }
        NewznabCaps {
            search_types: types.iter().map(|t| (*t).to_string()).collect(),
            supported_params,
            categories: vec![],
        }
    }

    fn pair(k: &str, v: &str) -> (String, String) {
        (k.to_string(), v.to_string())
    }

    #[test]
    fn movie_imdb_when_caps_advertise_it() {
        let caps = caps_with(&["movie"], &[("movie", &["imdbid"])]);
        let req = SearchRequest {
            kind: SearchKind::Movie,
            title: "The Matrix".into(),
            year: Some(1999),
            imdb_id: Some("tt0133093".into()),
            season: None,
            episode: None,
        };
        let plan = plan_newznab_search(ProviderKind::Hydra, "hydra", &req, Some(&caps), "K", 25);
        assert_eq!(plan.reason, "movie_imdb");
        assert!(plan.primary.contains(&pair("t", "movie")));
        assert!(plan.primary.contains(&pair("imdbid", "0133093")));
    }

    #[test]
    fn direct_movie_falls_back_to_search_when_no_imdb_for_listed_host() {
        // Mirrors `search_planner._movie_plan`: the per-host fallback
        // only fires on the title-search branch. When imdbid is
        // available and the provider advertises support for it, the
        // imdb query wins — even for hosts on `DIRECT_FALLBACK_HOSTS`.
        let caps = caps_with(
            &["movie", "search"],
            &[("movie", &["imdbid", "q"]), ("search", &["q"])],
        );
        let req = SearchRequest {
            kind: SearchKind::Movie,
            title: "The Matrix".into(),
            year: None,
            imdb_id: None,
            season: None,
            episode: None,
        };
        let plan = plan_newznab_search(
            ProviderKind::Direct,
            "api.nzbgeek.info/api",
            &req,
            Some(&caps),
            "K",
            25,
        );
        // No imdbid -> title path -> direct-host carve-out kicks in.
        assert_eq!(plan.reason, "direct_movie_title_search_fallback");
        assert!(plan.primary.contains(&pair("t", "search")));
    }

    #[test]
    fn missing_caps_uses_imdb_when_present() {
        let req = SearchRequest {
            kind: SearchKind::Movie,
            title: "X".into(),
            year: None,
            imdb_id: Some("tt0133093".into()),
            season: None,
            episode: None,
        };
        let plan = plan_newznab_search(ProviderKind::Hydra, "h", &req, None, "K", 25);
        assert_eq!(plan.reason, "missing_caps_movie_default");
        assert!(plan.primary.contains(&pair("imdbid", "0133093")));
    }

    #[test]
    fn tv_with_caps_emits_tvsearch_with_season_and_ep() {
        let caps = caps_with(
            &["tvsearch"],
            &[("tvsearch", &["q", "imdbid", "season", "ep"])],
        );
        let req = SearchRequest {
            kind: SearchKind::Tv,
            title: "Show".into(),
            year: None,
            imdb_id: Some("tt12345".into()),
            season: Some(1),
            episode: Some(2),
        };
        let plan = plan_newznab_search(ProviderKind::Hydra, "h", &req, Some(&caps), "K", 25);
        assert_eq!(plan.reason, "episode_tvsearch");
        assert!(plan.primary.contains(&pair("t", "tvsearch")));
        assert!(plan.primary.contains(&pair("season", "1")));
        assert!(plan.primary.contains(&pair("ep", "2")));
    }

    #[test]
    fn imdb_digits_strips_tt() {
        assert_eq!(imdb_digits(Some("tt0133093")), "0133093");
        assert_eq!(imdb_digits(Some("0133093")), "0133093");
        assert_eq!(imdb_digits(None), "");
        assert_eq!(imdb_digits(Some("")), "");
    }
}
