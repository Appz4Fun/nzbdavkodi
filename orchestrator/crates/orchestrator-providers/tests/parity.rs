//! Round-trip parity tests for the Newznab XML parsers.
//!
//! Each parser is fed a fabricated-but-realistic XML fixture under
//! `tests/fixtures/` and the resulting `Vec<Candidate>` is compared to a
//! JSON golden file checked in alongside. The goldens elide
//! `age_days` (it's a `now() - pubdate` calculation so it would drift
//! day over day); everything else — sizes, titles, URLs, indexer
//! resolution, Newznab attrs — is a fixed parser output.

use orchestrator_providers::caps::{parse_caps, NewznabCaps};
use orchestrator_providers::types::Candidate;

const HYDRA_XML: &str = include_str!("fixtures/hydra_search_sample.xml");
const HYDRA_GOLDEN: &str = include_str!("fixtures/hydra_search_sample.golden.json");

const PROWLARR_XML: &str = include_str!("fixtures/prowlarr_search_sample.xml");
const PROWLARR_GOLDEN: &str = include_str!("fixtures/prowlarr_search_sample.golden.json");

const DIRECT_XML: &str = include_str!("fixtures/direct_newznab_sample.xml");
const DIRECT_GOLDEN: &str = include_str!("fixtures/direct_newznab_sample.golden.json");

const CAPS_XML: &str = include_str!("fixtures/caps_sample.xml");
const CAPS_GOLDEN: &str = include_str!("fixtures/caps_sample.golden.json");

/// Strip the volatile `age_days` field before diffing against the
/// golden — it's computed against wall-clock and would flake otherwise.
fn redact_age(candidate: &mut serde_json::Value) {
    if let Some(obj) = candidate.as_object_mut() {
        obj.insert("age_days".into(), serde_json::Value::Null);
    }
}

fn parse_via_provider(provider_module: &str, xml: &str) -> Vec<Candidate> {
    // The newznab item parser is crate-private; the public path is
    // through each client's `IndexerNameMode`. The simplest way to
    // exercise it across all three modes from a test is via the public
    // re-exports each module exposes. We use a small in-test shim that
    // calls the same parse helper through each public client.
    match provider_module {
        "hydra" => parse_hydra(xml),
        "prowlarr" => parse_prowlarr(xml),
        "direct" => parse_direct(xml),
        _ => panic!("unknown provider {provider_module}"),
    }
}

fn parse_hydra(xml: &str) -> Vec<Candidate> {
    // HydraClient::parse_xml isn't exposed; we replicate by calling
    // through the same internal newznab module the client uses. The
    // newznab module is crate-private, so we reach in via the
    // `pub use` wrapper added below in the crate root for tests.
    orchestrator_providers::__test_helpers::parse_newznab_items_hydra(xml)
}

fn parse_prowlarr(xml: &str) -> Vec<Candidate> {
    orchestrator_providers::__test_helpers::parse_newznab_items_prowlarr(xml)
}

fn parse_direct(xml: &str) -> Vec<Candidate> {
    orchestrator_providers::__test_helpers::parse_newznab_items_direct(xml, "NZBgeek")
}

fn assert_parity(expected_json: &str, parsed: &[Candidate]) {
    let mut expected: serde_json::Value =
        serde_json::from_str(expected_json).expect("golden must be valid JSON");
    let mut actual = serde_json::to_value(parsed).expect("candidates must serialize");
    // Strip age_days everywhere.
    if let Some(arr) = expected.as_array_mut() {
        for c in arr {
            redact_age(c);
        }
    }
    if let Some(arr) = actual.as_array_mut() {
        for c in arr {
            redact_age(c);
        }
    }
    pretty_assert_eq(&expected, &actual);
}

fn pretty_assert_eq(expected: &serde_json::Value, actual: &serde_json::Value) {
    if expected != actual {
        let expected_pretty = serde_json::to_string_pretty(expected).unwrap_or_default();
        let actual_pretty = serde_json::to_string_pretty(actual).unwrap_or_default();
        panic!(
            "parity mismatch\n\n--- expected (golden) ---\n{expected_pretty}\n\n--- actual (parsed) ---\n{actual_pretty}\n"
        );
    }
}

#[test]
fn hydra_xml_parity() {
    let parsed = parse_via_provider("hydra", HYDRA_XML);
    assert_eq!(parsed.len(), 3, "expected 3 items");
    // Indexer resolution: row 1 uses attr `indexer`, row 2 uses
    // <source>text</source>, row 3 falls back to <source url>'s
    // hostname.
    assert_eq!(parsed[0].indexer, "NZBgeek");
    assert_eq!(parsed[1].indexer, "DrunkenSlug");
    assert_eq!(parsed[2].indexer, "omgwtfnzbs.org");
    assert_parity(HYDRA_GOLDEN, &parsed);
}

#[test]
fn prowlarr_xml_parity() {
    let parsed = parse_via_provider("prowlarr", PROWLARR_XML);
    assert_eq!(parsed.len(), 2);
    // Prowlarr mode prefixes the indexer attr with "prowlarr:".
    assert_eq!(parsed[0].indexer, "prowlarr:DrunkenSlug");
    // Row 2 has no attr indexer and no <source>, so it falls back to
    // the configured prefix on its own.
    assert_eq!(parsed[1].indexer, "prowlarr");
    assert_parity(PROWLARR_GOLDEN, &parsed);
}

#[test]
fn direct_xml_parity() {
    let parsed = parse_via_provider("direct", DIRECT_XML);
    assert_eq!(parsed.len(), 2);
    // Direct mode uses the static indexer label when no attr indexer.
    assert_eq!(parsed[0].indexer, "NZBgeek");
    assert_eq!(parsed[1].indexer, "NZBgeek");
    // Row 2's pubdate is unparseable -> age_days = None, pubdate
    // preserved as-is.
    assert_eq!(parsed[1].pubdate.as_deref(), Some("not-a-date"));
    assert!(parsed[1].age_days.is_none());
    assert_parity(DIRECT_GOLDEN, &parsed);
}

#[test]
fn caps_xml_parity() {
    let caps = parse_caps(CAPS_XML);
    let golden: NewznabCaps = serde_json::from_str(CAPS_GOLDEN).expect("golden");
    assert_eq!(caps, golden);
}

#[test]
fn invalid_xml_root_rejected() {
    let xml = "<not-rss/>";
    let parsed = orchestrator_providers::__test_helpers::try_parse_hydra(xml);
    assert!(parsed.is_err(), "non-RSS root must be rejected");
}
