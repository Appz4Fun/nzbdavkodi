//! Harness runner — drives scenarios against a running orchestrator
//! (and, in scenarios that need it, fault-proxy) without involving
//! Kodi at all.
//!
//! Phase 0 ships one scenario, `golden_path`, that:
//!
//!   1. Boots the orchestrator-server stub in-process.
//!   2. Calls `GET /v1/health`.
//!   3. Asserts the response shape (`status == "ok"`).
//!
//! Later phases add `single_cutover`, `extreme_fallback`,
//! `all_peers_fail`, `peer_validation_top50`, `tier_selection_matrix`
//! per plan §12.2. They reuse the same `Harness` boot-and-drive
//! plumbing, so each new scenario is just one new file under
//! `scenarios/` and one entry in `Scenario::all()`.

pub mod harness;
pub mod scenarios;

pub use harness::Harness;
pub use scenarios::Scenario;
