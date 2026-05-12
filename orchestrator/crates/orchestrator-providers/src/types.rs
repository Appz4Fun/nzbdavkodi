// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! Shared types across the provider clients.
//!
//! [`Candidate`] is the unified, indexer-agnostic shape every provider
//! emits so the orchestrator's `/v1/search` route can fan results in
//! without per-provider branching. It is the Rust counterpart of the
//! `dict` shape the Python clients produced (keys: title, link, size,
//! indexer, pubdate, age), normalised to typed fields.

use serde::{Deserialize, Serialize};

/// A single search result, normalised across all provider XML dialects.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Candidate {
    /// Download URL for the NZB file (already includes any indexer apikey).
    pub nzb_url: String,
    /// Display name for the indexer that produced this row. Examples:
    /// `"nzbhydra2"`, `"prowlarr:NZBgeek"`, or a direct indexer slug
    /// such as `"nzbgeek"`.
    pub indexer: String,
    /// Release title as advertised by the indexer.
    pub title: String,
    /// Size in bytes. `0` means the indexer didn't advertise a size.
    pub size: u64,
    /// Best-effort human-readable age — e.g. `"today"`, `"3 days"`,
    /// `"2 months"`. `None` when no pubdate was present.
    pub age_days: Option<u32>,
    /// Original RFC 2822 / ISO-8601 date string from the feed.
    pub pubdate: Option<String>,
    /// Newznab GUID where exposed.
    pub guid: Option<String>,
    /// Newznab category ids parsed from `<newznab:attr name="category">`.
    pub categories: Vec<u32>,
    /// Raw `newznab:attr` map (everything beyond the typed fields above)
    /// for downstream filters that want to read e.g. group, grabs, files.
    pub extra: serde_json::Value,
}

impl Default for Candidate {
    fn default() -> Self {
        Self {
            nzb_url: String::new(),
            indexer: String::new(),
            title: String::new(),
            size: 0,
            age_days: None,
            pubdate: None,
            guid: None,
            categories: Vec::new(),
            extra: serde_json::Value::Object(serde_json::Map::new()),
        }
    }
}

/// The two search shapes the addon supports today. Matches the Python
/// `search_type` argument convention (`"movie"` or `"episode"`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum SearchKind {
    Movie,
    Tv,
}

/// Single-call inputs across every provider. Fields not relevant to a
/// given provider call are simply ignored (e.g. `season`/`episode` for
/// a movie search).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SearchRequest {
    pub kind: SearchKind,
    pub title: String,
    pub year: Option<u32>,
    pub imdb_id: Option<String>,
    pub season: Option<u32>,
    pub episode: Option<u32>,
}

impl SearchRequest {
    pub fn movie(title: impl Into<String>) -> Self {
        Self {
            kind: SearchKind::Movie,
            title: title.into(),
            year: None,
            imdb_id: None,
            season: None,
            episode: None,
        }
    }

    pub fn tv(title: impl Into<String>, season: u32, episode: u32) -> Self {
        Self {
            kind: SearchKind::Tv,
            title: title.into(),
            year: None,
            imdb_id: None,
            season: Some(season),
            episode: Some(episode),
        }
    }
}

/// Errors surfaced by the provider client surface. Reasons enumerated in
/// `docs/rust-migration-plan.md` §11.2 layer 1.
#[derive(Debug, thiserror::Error)]
pub enum ProviderError {
    /// HTTP request failed before we could parse anything — DNS, TCP,
    /// TLS, timeout, or transport-level breakdown. Maps to the
    /// `provider_timeout` and `provider_http_error` reasons.
    #[error("{provider} HTTP error: {message}")]
    Http { provider: String, message: String },

    /// The provider returned HTTP, but the status code wasn't 2xx.
    #[error("{provider} returned HTTP {status}")]
    HttpStatus { provider: String, status: u16 },

    /// The XML body either didn't parse or didn't look like a Newznab
    /// RSS feed. Reason `provider_xml_invalid`.
    #[error("{provider} returned an invalid XML response: {message}")]
    InvalidResponse { provider: String, message: String },

    /// Caller-side misconfiguration — empty URL, malformed URL, missing
    /// API key when required. Distinct from upstream errors because we
    /// never even attempted a request.
    #[error("{provider} configuration error: {message}")]
    Config { provider: String, message: String },
}

impl ProviderError {
    /// Short enumerated reason for the §11.2 `reason` field. Adding a
    /// new variant means adding a new reason — by design.
    pub fn reason(&self) -> &'static str {
        match self {
            ProviderError::Http { .. } => "provider_http_error",
            ProviderError::HttpStatus { .. } => "provider_http_error",
            ProviderError::InvalidResponse { .. } => "provider_xml_invalid",
            ProviderError::Config { .. } => "provider_disabled",
        }
    }
}
