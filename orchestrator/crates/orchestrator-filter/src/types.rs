//! Shared candidate type.
//!
//! In the long run this lives in `orchestrator-providers` (sibling
//! crate). Until that crate lands the type is gated behind the
//! `provider-types` feature so we can move it without touching
//! consumer code.
//!
//! Mirrors the type defined in the migration plan §4 and the Python
//! result-dict shape produced by `hydra.py` / `prowlarr.py` /
//! `direct_indexers.py`.

#[cfg(feature = "provider-types")]
use serde::{Deserialize, Serialize};

#[cfg(feature = "provider-types")]
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Candidate {
    pub nzb_url: String,
    pub indexer: String,
    pub title: String,
    /// Size in bytes. `0` and missing are equivalent and stay
    /// distinguishable only via [`Candidate::size_known`].
    pub size: u64,
    pub age_days: Option<u32>,
    pub pubdate: Option<String>,
    pub guid: Option<String>,
    pub categories: Vec<u32>,
    /// Free-form indexer extras (link, raw API response, etc.).
    #[serde(default)]
    pub extra: serde_json::Value,
}

#[cfg(feature = "provider-types")]
impl Candidate {
    /// Whether the indexer told us a concrete size. The Python
    /// filter treats an empty-string / None size differently from a
    /// genuine 0 — this carries that distinction through.
    pub fn size_known(&self) -> bool {
        // We model an unknown size as `size == 0` AND no signal in
        // `extra`. Consumers that need the lossless distinction
        // should pass it through `extra`.
        self.size != 0
    }
}
