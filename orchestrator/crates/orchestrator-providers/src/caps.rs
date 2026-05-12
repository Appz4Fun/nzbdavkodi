// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! Newznab caps fetch and parse. Port of `newznab_caps.py`.

use std::collections::BTreeMap;
use std::time::Duration;

use quick_xml::events::Event;
use quick_xml::Reader;
use serde::{Deserialize, Serialize};
use tracing::warn;

use crate::http::{http_get, redact_url};
use crate::types::ProviderError;

const CAPS_MAX_BYTES: usize = 1024 * 1024;
const CAPS_TIMEOUT: Duration = Duration::from_secs(15);

/// Map of `<search-tag>` local-name → Newznab `t=` value, mirroring
/// `newznab_caps._SEARCH_TAGS`.
fn search_tag(local: &str) -> Option<&'static str> {
    match local {
        "search" => Some("search"),
        "tv-search" => Some("tvsearch"),
        "movie-search" => Some("movie"),
        "audio-search" => Some("audio"),
        "book-search" => Some("book"),
        _ => None,
    }
}

/// Caps response parsed from a Newznab `/api?t=caps` endpoint.
///
/// In Rust the cache is in-memory only (a `BTreeMap<Provider, NewznabCaps>`
/// owned by the caller). Persistence to SQLite is the admin API crate's
/// job and lives outside this crate, as the porting brief calls out.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct NewznabCaps {
    /// Newznab search types the provider advertises as `available="yes"`.
    /// Stored as `Vec<String>` (not `HashSet`) to preserve the source
    /// ordering — useful for diff'ing cache snapshots.
    pub search_types: Vec<String>,
    /// `search_type -> [supportedParams...]`.
    pub supported_params: BTreeMap<String, Vec<String>>,
    /// Newznab category ids + names.
    pub categories: Vec<NewznabCategory>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NewznabCategory {
    pub id: u32,
    pub name: String,
}

impl NewznabCaps {
    /// `true` if the provider advertised `search_type` (e.g. `"movie"`).
    pub fn supports(&self, search_type: &str) -> bool {
        self.search_types.iter().any(|st| st == search_type)
    }

    /// `true` if `search_type` is advertised AND lists `param` in
    /// `supportedParams`. Used by the planner to know whether e.g.
    /// `imdbid` is honoured for the `movie` query.
    pub fn supports_param(&self, search_type: &str, param: &str) -> bool {
        self.supports(search_type)
            && self
                .supported_params
                .get(search_type)
                .map(|params| params.iter().any(|p| p == param))
                .unwrap_or(false)
    }
}

/// Strip credentials and normalise an api_url into a proper Newznab
/// endpoint (i.e. ensure it ends in `/api` when only a host was given).
/// Port of `newznab_caps.normalize_api_endpoint`.
pub fn normalize_api_endpoint(api_url: &str) -> String {
    let trimmed = api_url.trim();
    if trimmed.is_empty() {
        return String::new();
    }
    // Parse, trim path, re-render.
    let mut parsed = match url::Url::parse(trimmed) {
        Ok(p) => p,
        // Bare host (`api.example.com/api`) — round-trip through https://.
        Err(_) => match url::Url::parse(&format!("https://{trimmed}")) {
            Ok(p) => p,
            Err(_) => return trimmed.to_string(),
        },
    };
    let path = parsed.path().trim_end_matches('/').to_string();
    let new_path = if path.is_empty() { "/api" } else { path.as_str() };
    parsed.set_path(new_path);
    parsed.to_string()
}

/// Build the caps query URL given an api_url + key. Mirrors
/// `newznab_caps.build_caps_url` — strips any pre-existing `apikey/t/o`
/// query params and re-appends the canonical trio.
pub fn build_caps_url(api_url: &str, api_key: &str) -> String {
    let normalized = normalize_api_endpoint(api_url);
    let Ok(mut parsed) = url::Url::parse(&normalized) else {
        return normalized;
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
        q.append_pair("apikey", api_key);
        q.append_pair("t", "caps");
        q.append_pair("o", "xml");
    }
    parsed.to_string()
}

/// Parse Newznab caps XML into [`NewznabCaps`]. Returns an empty struct
/// for malformed XML — mirrors Python `parse_caps`'s defensive default
/// (the `_empty_caps()` early return on `ParseError`).
pub fn parse_caps(xml_text: &str) -> NewznabCaps {
    let mut reader = Reader::from_str(xml_text);
    reader.config_mut().trim_text(false);

    let mut search_types: Vec<String> = Vec::new();
    let mut supported_params: BTreeMap<String, Vec<String>> = BTreeMap::new();
    let mut categories: Vec<NewznabCategory> = Vec::new();

    let mut buf = Vec::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Eof) => break,
            Ok(Event::Empty(e)) | Ok(Event::Start(e)) => {
                let raw_name = e.name();
                let local = local_name(raw_name.as_ref());

                if let Some(search_type) = search_tag(local) {
                    let mut available = false;
                    let mut params: Vec<String> = Vec::new();
                    for attr in e.attributes().with_checks(false).flatten() {
                        let key_lower = attr.key.as_ref().to_ascii_lowercase();
                        if key_lower == b"available" {
                            let v = attr.unescape_value().unwrap_or_default();
                            available = v.eq_ignore_ascii_case("yes");
                        } else if key_lower == b"supportedparams" {
                            let v = attr.unescape_value().unwrap_or_default();
                            params = split_csv(&v);
                        }
                    }
                    if available {
                        let st = search_type.to_string();
                        if !search_types.iter().any(|x| x == &st) {
                            search_types.push(st.clone());
                        }
                        supported_params.insert(st, params);
                    }
                } else if local == "category" || local == "subcat" {
                    let mut id: Option<u32> = None;
                    let mut name = String::new();
                    for attr in e.attributes().with_checks(false).flatten() {
                        let key = attr.key.as_ref().to_ascii_lowercase();
                        let value = attr.unescape_value().unwrap_or_default().to_string();
                        if key == b"id" {
                            id = value.parse::<u32>().ok();
                        } else if key == b"name" {
                            name = value;
                        }
                    }
                    if let Some(id) = id {
                        categories.push(NewznabCategory { id, name });
                    }
                }
            }
            Ok(_) => {}
            Err(_) => return NewznabCaps::default(),
        }
        buf.clear();
    }

    NewznabCaps {
        search_types,
        supported_params,
        categories,
    }
}

fn split_csv(value: &str) -> Vec<String> {
    value
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

pub(crate) fn local_name(qualified: &[u8]) -> &str {
    let s = std::str::from_utf8(qualified).unwrap_or("");
    match s.rsplit_once(':') {
        Some((_, local)) => local,
        None => s,
    }
}

/// Async port of `newznab_caps.fetch_caps`. Performs one GET against
/// `/api?t=caps`, parses the XML, returns the [`NewznabCaps`] structure
/// or a [`ProviderError`].
///
/// Like the Python version, a response that's too large to be a caps
/// document (> 1 MiB) is rejected. Reasoning: caps documents are tiny;
/// a multi-MB body almost certainly means we hit a generic 404 page.
pub async fn fetch_caps(
    provider: &str,
    api_url: &str,
    api_key: &str,
) -> Result<NewznabCaps, ProviderError> {
    let url = build_caps_url(api_url, api_key);
    tracing::debug!(
        event = "caps.fetch",
        provider = provider,
        url = %redact_url(&url),
        "fetching Newznab caps"
    );
    let body = http_get(provider, &url, CAPS_TIMEOUT).await?;
    if body.len() > CAPS_MAX_BYTES {
        warn!(
            event = "caps.fetch",
            provider = provider,
            bytes = body.len(),
            "caps response exceeds CAPS_MAX_BYTES"
        );
        return Err(ProviderError::InvalidResponse {
            provider: provider.to_string(),
            message: format!("caps response exceeds {CAPS_MAX_BYTES} bytes"),
        });
    }
    Ok(parse_caps(&body))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_appends_api_path_when_absent() {
        assert_eq!(
            normalize_api_endpoint("https://api.example.com"),
            "https://api.example.com/api"
        );
    }

    #[test]
    fn normalize_preserves_existing_api_path() {
        assert_eq!(
            normalize_api_endpoint("https://api.example.com/api"),
            "https://api.example.com/api"
        );
    }

    #[test]
    fn build_caps_url_drops_prior_apikey() {
        let url = build_caps_url("https://x/api?apikey=OLD&extra=keep", "NEW");
        assert!(url.contains("apikey=NEW"), "got {url}");
        assert!(!url.contains("apikey=OLD"));
        assert!(url.contains("extra=keep"));
        assert!(url.contains("t=caps"));
        assert!(url.contains("o=xml"));
    }

    #[test]
    fn parse_caps_extracts_search_types_and_params() {
        let xml = r#"<?xml version="1.0"?>
            <caps>
              <searching>
                <search available="yes" supportedParams="q,limit"/>
                <movie-search available="yes" supportedParams="imdbid,q,year"/>
                <tv-search available="no" supportedParams="q"/>
              </searching>
              <categories>
                <category id="2000" name="Movies"/>
                <category id="5000" name="TV"/>
              </categories>
            </caps>"#;
        let caps = parse_caps(xml);
        assert!(caps.supports("search"));
        assert!(caps.supports("movie"));
        assert!(!caps.supports("tvsearch"));
        assert!(caps.supports_param("movie", "imdbid"));
        assert!(!caps.supports_param("movie", "season"));
        assert_eq!(caps.categories.len(), 2);
    }

    #[test]
    fn parse_caps_returns_empty_for_garbage() {
        let caps = parse_caps("<not really xml");
        assert!(caps.search_types.is_empty());
    }
}
