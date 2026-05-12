//! JSONL event sink — parity with Python `EVENTS_PATH`.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct FiredEvent {
    pub fault_type: String,
    pub t_wall: f64,
    pub range: String,
    /// Per-fault optional payload (e.g. `fail_bytes`, `duration`,
    /// `scheduled_bytes`, `corruption_count`).
    #[serde(flatten)]
    pub extra: serde_json::Map<String, serde_json::Value>,
}

/// Append-only writer guarded by a `Mutex` so concurrent fault
/// handlers can't interleave bytes mid-line.
pub struct EventSink {
    path: Option<PathBuf>,
    lock: Mutex<()>,
}

impl EventSink {
    pub fn new(path: Option<PathBuf>) -> Self {
        Self {
            path,
            lock: Mutex::new(()),
        }
    }

    pub fn append(&self, event: &FiredEvent) {
        let path = match self.path.as_ref() {
            Some(p) => p,
            None => return,
        };
        // Best-effort — the harness is the canonical consumer; if the
        // log path is unwritable we surface that via tracing but don't
        // bring the proxy down.
        let line = match serde_json::to_string(event) {
            Ok(s) => s,
            Err(e) => {
                tracing::warn!(
                    event = "fault_proxy.events.serialize_failed",
                    reason = %e,
                    "could not serialise fired event"
                );
                return;
            }
        };
        let _guard = self.lock.lock().expect("EventSink mutex poisoned");
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        match OpenOptions::new().create(true).append(true).open(path) {
            Ok(mut f) => {
                let _ = writeln!(f, "{line}");
            }
            Err(e) => {
                tracing::warn!(
                    event = "fault_proxy.events.append_failed",
                    reason = %e,
                    "could not append to events JSONL"
                );
            }
        }
    }
}
