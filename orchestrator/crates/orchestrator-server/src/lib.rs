//! nzbdav-orchestrator server.
//!
//! The product surface area defined by `docs/rust-migration-plan.md`
//! lands incrementally over Phases 1-5. This crate currently ships:
//!
//! - `/v1/health` returning a static JSON body.
//! - `/v1/search` for the Phase 1 provider/filter bridge.
//! - `/v1/admin/indexers` for the Phase 1 indexer store bridge.
//! - `/v1/resolve` for the Phase 2 single-peer nzbdav/WebDAV bridge.
//! - The structured-log envelope from plan §11.2 (every line is one
//!   JSON object with `ts`, `level`, `event`, correlation ids,
//!   `outcome`, `reason`, `duration_ms`). All future layers emit
//!   through the same envelope so failure paths stay quotable end to
//!   end.
//!
//! Re-exported below so binary and tests share the same `router()` and
//! `LogEnvelopeLayer` without duplication.

pub mod admin;
pub mod logging;
pub mod peer_pool;
pub mod resolve;
pub mod routes;
pub mod search;

pub use routes::{
    router, router_with_admin, router_with_admin_and_peer_pool,
    router_with_admin_peer_pool_and_policy, router_with_peer_pool,
    router_with_peer_pool_and_policy, HealthPayload,
};
