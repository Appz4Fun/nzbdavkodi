//! Rust port of `tests/extreme/fault_proxy.py`.
//!
//! The fault-proxy sits in front of nzbdav-rs's WebDAV port during
//! harness scenarios and lets a test:
//!
//!   1. Schedule a sequence of faults via `POST /control/schedule`.
//!   2. Watch the proxy fire one per matching playback `Range:` request.
//!   3. Replay the fired events from JSONL (`EVENTS_PATH` env var) or
//!      via `GET /control/fired` for in-process assertions.
//!
//! Five fault types are supported (parity with the Python source):
//!
//!   * `connection_reset`   — forward N bytes then slam the socket.
//!   * `http_500`           — discard upstream and return 500.
//!   * `slow_upstream`      — throttle to a target byte rate for a window.
//!   * `truncated_response` — send fewer bytes than `Content-Length`.
//!   * `corrupted_bytes`    — XOR random byte positions in the head.
//!
//! Phase 0 lands the control plane + passthrough proxy + state
//! machine. Each fault function is wired in; the data-plane edges of
//! a couple of the faults (raw TCP RST for `connection_reset`,
//! byte-level XOR for `corrupted_bytes`) tighten in later phases as
//! the harness scenarios that exercise them come online.

pub mod control;
pub mod events;
pub mod proxy;
pub mod state;

pub use control::{control_router, ControlState};
pub use events::{EventSink, FiredEvent};
pub use proxy::{ProxyConfig, ProxyHandler};
pub use state::{ProxyState, ScheduledEvent, VALID_FAULT_TYPES};
