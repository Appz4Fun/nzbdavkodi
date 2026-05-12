//! nzbdav-orchestrator server — Phase 0 skeleton.
//!
//! The product surface area defined by `docs/rust-migration-plan.md`
//! lands incrementally over Phases 1-5. This crate currently ships:
//!
//! - A single `/v1/health` route returning a static JSON body.
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
pub mod routes;
pub mod search;

pub use routes::{router, router_with_admin, HealthPayload};
