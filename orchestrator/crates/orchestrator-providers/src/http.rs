// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! Crate-private HTTP + parsing helpers.
//!
//! Port of the bits of `plugin.video.nzbdav/resources/lib/http_util.py`
//! the provider clients actually use. Kept private — there's no
//! workspace-wide `orchestrator-util` crate by design (per the porting
//! brief: helpers stay local until at least two crates need them).

use std::sync::OnceLock;
use std::time::Duration;

use reqwest::{Client, StatusCode};
use time::format_description::well_known::Rfc2822;
use time::OffsetDateTime;

use crate::types::ProviderError;

/// Default request timeout, matching the Python addon (`timeout=15` for
/// direct indexer + caps, `timeout=300` for the user-facing Hydra and
/// Prowlarr search calls — the long timeout is to wait out NZBHydra2's
/// own indexer fan-out). Callers override per call site.
pub(crate) const DEFAULT_SEARCH_TIMEOUT: Duration = Duration::from_secs(15);

const HTTP_USER_AGENT: &str = "NZB-DAV Kodi Addon";

/// Shared `reqwest::Client` — saves the TLS handshake setup per call,
/// matches the warmup-rs profile (rustls only, no native-tls baggage).
pub(crate) fn shared_client() -> &'static Client {
    static CLIENT: OnceLock<Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        Client::builder()
            .user_agent(HTTP_USER_AGENT)
            .timeout(DEFAULT_SEARCH_TIMEOUT)
            .build()
            .expect("reqwest client build should not fail")
    })
}

/// Perform an HTTP GET and return the body as UTF-8 (lossy on invalid
/// sequences — matches `http_util.http_get`'s `errors="replace"`).
///
/// `timeout` overrides the per-call timeout; pass
/// [`DEFAULT_SEARCH_TIMEOUT`] for the same behaviour as the shared
/// client's default. URL scheme is checked because callers occasionally
/// hand-paste e.g. `file://` into a settings input — the Python
/// `http_get` enforces the same scheme allowlist.
pub(crate) async fn http_get(
    provider: &str,
    url: &str,
    timeout: Duration,
) -> Result<String, ProviderError> {
    let parsed = url::Url::parse(url).map_err(|e| ProviderError::Config {
        provider: provider.to_string(),
        message: format!("invalid URL {url:?}: {e}"),
    })?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(ProviderError::Config {
            provider: provider.to_string(),
            message: format!("unsupported URL scheme: {:?}", parsed.scheme()),
        });
    }

    let response = shared_client()
        .get(url)
        .timeout(timeout)
        .send()
        .await
        .map_err(|e| ProviderError::Http {
            provider: provider.to_string(),
            message: redact_text(&e.to_string()),
        })?;

    let status = response.status();
    if !status.is_success() {
        return Err(ProviderError::HttpStatus {
            provider: provider.to_string(),
            status: status_to_u16(status),
        });
    }

    // Use `bytes().await` + `String::from_utf8_lossy` so invalid UTF-8
    // produces a normalized parse error downstream instead of an opaque
    // request-level error. Matches the Python `errors="replace"`.
    let bytes = response.bytes().await.map_err(|e| ProviderError::Http {
        provider: provider.to_string(),
        message: redact_text(&e.to_string()),
    })?;
    Ok(String::from_utf8_lossy(&bytes).into_owned())
}

fn status_to_u16(status: StatusCode) -> u16 {
    status.as_u16()
}

/// Parameter names whose values redact to `REDACTED`. Mirrors the
/// Python `_REDACT_PARAM_NAMES` frozenset including the extended set
/// from TODO.md §H.2-H2c (`key`, `access_token`, `bearer`, `session`,
/// `sessionid`).
const REDACT_PARAM_NAMES: &[&str] = &[
    "apikey",
    "api_key",
    "auth",
    "token",
    "password",
    "passwd",
    "secret",
    "key",
    "access_token",
    "bearer",
    "session",
    "sessionid",
];

/// Redact API-key-style query parameters from a URL for safe logging.
///
/// Mirrors `http_util.redact_url`:
/// - case-insensitive match against the param name (not value);
/// - recursively redact values that themselves look like a URL with a
///   query (the "submit-this-URL" shape carries an inner apikey);
/// - strip `user:password@host` userinfo down to `user:REDACTED@host`.
///
/// Unknown / malformed URLs round-trip unchanged — same behaviour as
/// the Python implementation's `try: urlsplit(url) except: return url`.
pub fn redact_url(url: &str) -> String {
    let Ok(parsed) = url::Url::parse(url) else {
        return url.to_string();
    };
    let mut out = parsed.clone();
    out.query_pairs_mut().clear();

    let pairs: Vec<(String, String)> = parsed
        .query_pairs()
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();

    if !pairs.is_empty() {
        let mut serializer = out.query_pairs_mut();
        for (k, v) in pairs {
            if REDACT_PARAM_NAMES
                .iter()
                .any(|name| name.eq_ignore_ascii_case(&k))
            {
                serializer.append_pair(&k, "REDACTED");
            } else if !v.is_empty() && v.contains("://") && v.contains('=') {
                // Looks like a wrapped URL — recurse.
                let inner = redact_url(&v);
                serializer.append_pair(&k, &inner);
            } else {
                serializer.append_pair(&k, &v);
            }
        }
    }

    // Redact userinfo of the form user:password@host. The url crate
    // exposes username() + password(); we can rebuild safely.
    if !parsed.username().is_empty() {
        let username = parsed.username().to_string();
        if parsed.password().is_some() {
            let _ = out.set_username(&username);
            let _ = out.set_password(Some("REDACTED"));
        }
    }

    let rendered = out.to_string();
    // url crate strips an empty trailing `?` differently than urllib —
    // normalise so callers comparing strings don't see a spurious `?`.
    if !url.contains('?') && rendered.ends_with('?') {
        rendered.trim_end_matches('?').to_string()
    } else {
        rendered
    }
}

/// Redact apikey-style tokens in free-form text. Used for the rare
/// error-message paths that echo an underlying URL back at us
/// (`reqwest`'s `Error::Display` does this for some shapes).
pub fn redact_text(text: &str) -> String {
    // Match `(key)=value` where value runs until `&`, whitespace, or one
    // of " < > "`'`. Hand-roll instead of pulling in regex for one tiny
    // case; the regex crate is already in the workspace for the parser
    // but the providers crate doesn't otherwise need it.
    let mut out = String::with_capacity(text.len());
    let bytes = text.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        // Look for a parameter name from REDACT_PARAM_NAMES.
        let mut matched_name: Option<&'static str> = None;
        for name in REDACT_PARAM_NAMES {
            let nbytes = name.as_bytes();
            if i + nbytes.len() < bytes.len()
                && bytes[i + nbytes.len()] == b'='
                && bytes[i..i + nbytes.len()].eq_ignore_ascii_case(nbytes)
                && !is_name_char(prev_byte(bytes, i))
            {
                matched_name = Some(name);
                break;
            }
        }
        if let Some(name) = matched_name {
            out.push_str(name);
            out.push_str("=REDACTED");
            i += name.len() + 1; // past `name=`
                                 // Skip the value until terminator.
            while i < bytes.len()
                && !matches!(
                    bytes[i],
                    b'&' | b' ' | b'\t' | b'\n' | b'\r' | b'"' | b'\'' | b'<' | b'>'
                )
            {
                i += 1;
            }
        } else {
            // Push one char (UTF-8 safe via char_indices below).
            let c = match text[i..].chars().next() {
                Some(c) => c,
                None => break,
            };
            out.push(c);
            i += c.len_utf8();
        }
    }
    out
}

fn prev_byte(bytes: &[u8], i: usize) -> u8 {
    if i == 0 {
        0
    } else {
        bytes[i - 1]
    }
}

fn is_name_char(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_'
}

/// Compute a human-readable age in days from an RFC 2822 pubdate string.
///
/// Returns `None` when the input doesn't parse — same defensive
/// behaviour as `http_util.calculate_age`, just typed instead of
/// string-coded. Caller converts to "today" / "N days" / "N months"
/// rendering on the UI side.
pub(crate) fn age_days_from_pubdate(pubdate: &str) -> Option<u32> {
    let pub_dt = OffsetDateTime::parse(pubdate, &Rfc2822).ok()?;
    let now = OffsetDateTime::now_utc();
    let delta = now - pub_dt;
    let days = delta.whole_days();
    if days < 0 {
        // Future-dated posts (clock skew, RSS shenanigans) — clamp to 0.
        Some(0)
    } else if days > u32::MAX as i64 {
        Some(u32::MAX)
    } else {
        Some(days as u32)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redacts_apikey_in_query() {
        let out = redact_url("https://hydra/api?apikey=SECRET&q=foo");
        assert!(out.contains("apikey=REDACTED"), "got {out}");
        assert!(out.contains("q=foo"));
    }

    #[test]
    fn redacts_nested_url_in_value() {
        // Inner URL is percent-encoded inside the outer's name= value.
        // After redaction the inner ?apikey=SECRET is rewritten to
        // ?apikey=REDACTED, which is then re-percent-encoded back into
        // the outer value — so the literal string is apikey%3DREDACTED.
        let out = redact_url(
            "https://nzbdav/api?mode=addurl&name=http%3A%2F%2Fhydra%2Fnzb%3Fapikey%3DSECRET",
        );
        assert!(
            out.contains("apikey%3DREDACTED") || out.contains("apikey=REDACTED"),
            "got {out}"
        );
        assert!(!out.contains("SECRET"), "secret leaked: {out}");
    }

    #[test]
    fn redacts_userinfo_password() {
        let out = redact_url("http://user:pw@host/api?x=1");
        assert!(out.contains("user:REDACTED@"), "got {out}");
    }

    #[test]
    fn redacts_unknown_url_passthrough() {
        let out = redact_url("not-a-url");
        assert_eq!(out, "not-a-url");
    }

    #[test]
    fn redacts_text_embedded_apikey() {
        let out = redact_text("error fetching http://x/api?apikey=SECRET&other=z");
        assert!(out.contains("apikey=REDACTED"), "got {out}");
    }

    #[test]
    fn age_days_handles_typical_rfc2822() {
        // Pubdate well in the past — assert just that it parses to Some.
        let age = age_days_from_pubdate("Mon, 01 Jan 2024 00:00:00 +0000");
        assert!(age.is_some());
    }

    #[test]
    fn age_days_returns_none_for_garbage() {
        assert!(age_days_from_pubdate("not a date").is_none());
    }
}
