// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 nzbdav contributors

//! NZB-search provider clients for the nzbdav-orchestrator.
//!
//! This crate is the Rust port of the Python provider modules that used
//! to live under `plugin.video.nzbdav/resources/lib/`:
//!
//! - [`hydra::HydraClient`] — NZBHydra2 Newznab XML client (`hydra.py`).
//! - [`prowlarr::ProwlarrClient`] — Prowlarr aggregator client (`prowlarr.py`).
//! - [`direct::DirectIndexerClient`] — direct Newznab indexer fan-out
//!   (`direct_indexers.py`).
//! - [`caps`] — Newznab caps fetch + parse (`newznab_caps.py`).
//! - [`planner`] — search-query planner (`search_planner.py`).
//!
//! The crate has **no Kodi awareness**: callers pass configuration in
//! explicitly. Per `docs/rust-migration-plan.md` §11.2, every client
//! emits the boundary events `search.provider_called`,
//! `search.provider_response`, and `search.candidate_returned` through
//! the `tracing` macros; parsers stay event-free so they remain pure
//! functions.

pub mod caps;
pub mod direct;
mod http;
pub mod hydra;
mod newznab;
pub mod planner;
pub mod prowlarr;
pub mod types;

pub use caps::{fetch_caps, parse_caps, NewznabCaps};
pub use direct::{DirectIndexer, DirectIndexerClient};
pub use hydra::HydraClient;
pub use planner::{plan_newznab_search, NewznabSearchPlan};
pub use prowlarr::ProwlarrClient;
pub use types::{Candidate, ProviderError, SearchKind, SearchRequest};

/// Test-only helpers that re-export the crate-private newznab parser
/// under each provider's [`IndexerNameMode`]. Not part of the public
/// API contract — exposed so the parity integration tests can drive
/// each mode without re-implementing the parser glue. Hidden from
/// rustdoc and gated to make it a clear "do not use".
#[doc(hidden)]
pub mod __test_helpers {
    use crate::newznab::{parse_newznab_items, IndexerNameMode};
    use crate::types::{Candidate, ProviderError};

    pub fn parse_newznab_items_hydra(xml: &str) -> Vec<Candidate> {
        parse_newznab_items("nzbhydra2", xml, &IndexerNameMode::HydraSource).expect("xml")
    }

    pub fn parse_newznab_items_prowlarr(xml: &str) -> Vec<Candidate> {
        parse_newznab_items(
            "prowlarr",
            xml,
            &IndexerNameMode::PrefixedStatic {
                prefix: "prowlarr".to_string(),
                fallback: String::new(),
            },
        )
        .expect("xml")
    }

    pub fn parse_newznab_items_direct(xml: &str, label: &str) -> Vec<Candidate> {
        parse_newznab_items("direct", xml, &IndexerNameMode::Static(label.into())).expect("xml")
    }

    pub fn try_parse_hydra(xml: &str) -> Result<Vec<Candidate>, ProviderError> {
        parse_newznab_items("nzbhydra2", xml, &IndexerNameMode::HydraSource)
    }
}
