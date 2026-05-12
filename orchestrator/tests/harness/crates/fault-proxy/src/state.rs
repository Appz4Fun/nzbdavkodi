//! Mutable state shared between the control plane and the proxy handler.

use std::sync::Mutex;
use std::time::Instant;

use serde::{Deserialize, Serialize};

/// Five valid fault types. Order matches `tests/extreme/fault_proxy.py`.
pub const VALID_FAULT_TYPES: &[&str] = &[
    "connection_reset",
    "http_500",
    "slow_upstream",
    "truncated_response",
    "corrupted_bytes",
];

/// One scheduled fault: at `at_seconds` past the schedule's reset clock
/// the next matching playback `Range:` request will trigger
/// `fault_type`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ScheduledEvent {
    pub at_seconds: f64,
    pub fault_type: String,
}

/// Snapshot of a fired fault — what the harness asserts against.
#[derive(Debug, Clone, Serialize)]
pub struct FiredRecord {
    /// Wall clock at which the fault fired (Unix seconds).
    pub t_wall: f64,
    /// Seconds since the schedule was set (the "run clock").
    pub t_run: f64,
    pub fault_type: String,
    pub range: String,
}

pub struct ProxyState {
    inner: Mutex<Inner>,
}

struct Inner {
    scheduled: Vec<ScheduledEvent>,
    fired: Vec<FiredRecord>,
    start: Instant,
    start_wall: f64,
}

impl Default for ProxyState {
    fn default() -> Self {
        Self::new()
    }
}

impl ProxyState {
    pub fn new() -> Self {
        Self {
            inner: Mutex::new(Inner {
                scheduled: Vec::new(),
                fired: Vec::new(),
                start: Instant::now(),
                start_wall: now_wall_seconds(),
            }),
        }
    }

    /// Atomically replace the schedule and reset the run clock — same
    /// semantics as `ProxyState.replace_schedule` in the Python source.
    pub fn replace_schedule(&self, mut events: Vec<ScheduledEvent>) {
        events.sort_by(|a, b| {
            a.at_seconds
                .partial_cmp(&b.at_seconds)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        let mut guard = self.inner.lock().expect("ProxyState mutex poisoned");
        guard.scheduled = events;
        guard.start = Instant::now();
        guard.start_wall = now_wall_seconds();
        guard.fired.clear();
    }

    /// Return and remove the next event whose `at_seconds` has elapsed
    /// on the current run clock. Returns `None` when nothing is due.
    pub fn next_due(&self) -> Option<ScheduledEvent> {
        let mut guard = self.inner.lock().expect("ProxyState mutex poisoned");
        let now_run = guard.start.elapsed().as_secs_f64();
        let idx = guard
            .scheduled
            .iter()
            .position(|e| e.at_seconds <= now_run)?;
        Some(guard.scheduled.remove(idx))
    }

    pub fn record_fired(&self, fault_type: &str, range_header: &str) -> FiredRecord {
        let record = {
            let guard = self.inner.lock().expect("ProxyState mutex poisoned");
            FiredRecord {
                t_wall: now_wall_seconds(),
                t_run: guard.start.elapsed().as_secs_f64(),
                fault_type: fault_type.to_string(),
                range: range_header.to_string(),
            }
        };
        self.inner
            .lock()
            .expect("ProxyState mutex poisoned")
            .fired
            .push(record.clone());
        record
    }

    pub fn fired_snapshot(&self) -> Vec<FiredRecord> {
        self.inner
            .lock()
            .expect("ProxyState mutex poisoned")
            .fired
            .clone()
    }

    pub fn scheduled_count(&self) -> usize {
        self.inner
            .lock()
            .expect("ProxyState mutex poisoned")
            .scheduled
            .len()
    }
}

fn now_wall_seconds() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replace_schedule_sorts_and_resets_fired() {
        let s = ProxyState::new();
        s.record_fired("http_500", "bytes=0-");
        assert_eq!(s.fired_snapshot().len(), 1);

        s.replace_schedule(vec![
            ScheduledEvent {
                at_seconds: 5.0,
                fault_type: "http_500".into(),
            },
            ScheduledEvent {
                at_seconds: 1.0,
                fault_type: "http_500".into(),
            },
        ]);

        assert!(
            s.fired_snapshot().is_empty(),
            "replace_schedule must clear fired"
        );

        // First due should be the one at 1.0 — sort happened.
        let first_due = {
            let guard = s.inner.lock().unwrap();
            guard.scheduled[0].at_seconds
        };
        assert_eq!(first_due, 1.0);
    }

    #[test]
    fn next_due_returns_only_elapsed_events() {
        let s = ProxyState::new();
        s.replace_schedule(vec![
            ScheduledEvent {
                at_seconds: 0.0,
                fault_type: "http_500".into(),
            },
            ScheduledEvent {
                at_seconds: 100.0,
                fault_type: "slow_upstream".into(),
            },
        ]);

        let due = s.next_due().expect("0s event must be due immediately");
        assert_eq!(due.fault_type, "http_500");
        assert!(s.next_due().is_none(), "100s event must not be due yet");
    }
}
