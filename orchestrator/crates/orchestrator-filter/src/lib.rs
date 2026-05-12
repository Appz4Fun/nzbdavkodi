//! Rust port of `plugin.video.nzbdav/resources/lib/filter.py` —
//! release-title metadata parsing, filtering, and ranking. Pure
//! logic, no I/O, no Kodi bindings.
//!
//! Migration plan §4 + §6 Phase 1. Behaviour-equivalent to the
//! Python original; the parity tests under `tests/parity.rs` are
//! line-for-line ports of `tests/test_filter.py`.

pub mod fallback;
pub mod filter;
pub mod metadata;
pub mod rank;
pub mod settings;
pub mod types;

pub use filter::{
    filter_results, matches_filters, FilterInput, FilterOutput, FilteredCandidate, RejectionReason,
};
pub use metadata::{parse_metadata, ParsedMeta};
pub use rank::{relevance_key, sort_candidates, RelevanceKey, SortOrder};
pub use settings::{
    FilterSettings, ALL_RELEASE_GROUPS, DEFAULT_EXCLUDED_GROUPS, DEFAULT_PREFERRED_GROUPS,
};

#[cfg(feature = "provider-types")]
pub use types::Candidate;
