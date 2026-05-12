//! §11 logging envelope.
//!
//! Every event emitted by the orchestrator is exactly one JSON object
//! per line on stdout, with the fixed shape:
//!
//! ```jsonc
//! {
//!   "ts":         "2026-05-12T09:34:11.123Z",
//!   "level":      "INFO|WARN|ERROR",
//!   "event":      "search.candidate_returned",
//!   "request_id": "01J...|null",
//!   "resolve_id": "01J...|null",
//!   "peer_id":    "01J...|null",
//!   "session_id": "01J...|null",
//!   "outcome":    "ok|error|timeout|rejected|skipped|started",
//!   "reason":     "...|null",
//!   "duration_ms": 123
//! }
//! ```
//!
//! Phase 0 only emits two events — `service.started` and
//! `health.served` — but every future layer reuses the same writer and
//! the same field names. Field names never change once shipped (plan
//! §11.1).

use std::io::{self, Write};
use std::sync::Mutex;

use serde::Serialize;
use time::format_description::well_known::Rfc3339;
use time::OffsetDateTime;
use tracing::field::{Field, Visit};
use tracing::{Event, Level, Subscriber};
use tracing_subscriber::layer::{Context, Layer};
use tracing_subscriber::registry::LookupSpan;

/// Common envelope fields that flow on every event. Optional ids are
/// `null` in the wire JSON when unset.
#[derive(Debug, Default, Serialize)]
struct Envelope<'a> {
    ts: String,
    level: &'a str,
    event: String,
    request_id: Option<String>,
    resolve_id: Option<String>,
    peer_id: Option<String>,
    session_id: Option<String>,
    outcome: Option<String>,
    reason: Option<String>,
    duration_ms: Option<u64>,
    #[serde(flatten)]
    extra: serde_json::Map<String, serde_json::Value>,
}

/// `tracing` layer that serialises every event as one JSON line.
///
/// The writer is wrapped in a `Mutex` so concurrent threads can't
/// interleave bytes mid-object. Stdout is the default sink — Promtail
/// scrapes it into Loki on the deploy hosts.
pub struct LogEnvelopeLayer<W: Write + Send + 'static> {
    writer: Mutex<W>,
}

impl LogEnvelopeLayer<io::Stdout> {
    /// Convenience constructor that emits to stdout.
    pub fn stdout() -> Self {
        Self {
            writer: Mutex::new(io::stdout()),
        }
    }
}

impl<W: Write + Send + 'static> LogEnvelopeLayer<W> {
    pub fn new(writer: W) -> Self {
        Self {
            writer: Mutex::new(writer),
        }
    }
}

impl<S, W> Layer<S> for LogEnvelopeLayer<W>
where
    S: Subscriber + for<'a> LookupSpan<'a>,
    W: Write + Send + 'static,
{
    fn on_event(&self, event: &Event<'_>, _ctx: Context<'_, S>) {
        let mut visitor = EnvelopeVisitor::default();
        event.record(&mut visitor);

        let level = match *event.metadata().level() {
            Level::ERROR => "ERROR",
            Level::WARN => "WARN",
            Level::INFO => "INFO",
            Level::DEBUG => "DEBUG",
            Level::TRACE => "TRACE",
        };

        let ts = OffsetDateTime::now_utc()
            .format(&Rfc3339)
            .unwrap_or_else(|_| String::from("1970-01-01T00:00:00Z"));

        let envelope = Envelope {
            ts,
            level,
            event: visitor.event.unwrap_or_else(|| {
                // Fall back to the tracing metadata target.name when the
                // caller forgot to set `event = "..."` explicitly. We
                // never want an unnamed line: §11.1 makes named events
                // a hard rule.
                format!("{}::{}", event.metadata().target(), event.metadata().name())
            }),
            request_id: visitor.request_id,
            resolve_id: visitor.resolve_id,
            peer_id: visitor.peer_id,
            session_id: visitor.session_id,
            outcome: visitor.outcome,
            reason: visitor.reason,
            duration_ms: visitor.duration_ms,
            extra: visitor.extra,
        };

        if let Ok(line) = serde_json::to_string(&envelope) {
            if let Ok(mut w) = self.writer.lock() {
                let _ = writeln!(w, "{line}");
            }
        }
    }
}

#[derive(Default)]
struct EnvelopeVisitor {
    event: Option<String>,
    request_id: Option<String>,
    resolve_id: Option<String>,
    peer_id: Option<String>,
    session_id: Option<String>,
    outcome: Option<String>,
    reason: Option<String>,
    duration_ms: Option<u64>,
    extra: serde_json::Map<String, serde_json::Value>,
}

impl EnvelopeVisitor {
    fn assign_str(&mut self, name: &str, value: String) {
        match name {
            "event" => self.event = Some(value),
            "request_id" => self.request_id = Some(value),
            "resolve_id" => self.resolve_id = Some(value),
            "peer_id" => self.peer_id = Some(value),
            "session_id" => self.session_id = Some(value),
            "outcome" => self.outcome = Some(value),
            "reason" => self.reason = Some(value),
            other => {
                self.extra
                    .insert(other.to_string(), serde_json::Value::String(value));
            }
        }
    }
}

impl Visit for EnvelopeVisitor {
    fn record_str(&mut self, field: &Field, value: &str) {
        self.assign_str(field.name(), value.to_string());
    }

    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        // `message` is the implicit field tracing emits when callers
        // write `info!("plain string")`. We keep it under `extra` so it
        // doesn't collide with the named envelope fields, and so it's
        // still visible for debugging.
        let formatted = format!("{value:?}");
        if field.name() == "message" {
            self.extra
                .insert("message".to_string(), serde_json::Value::String(formatted));
        } else {
            self.assign_str(field.name(), formatted);
        }
    }

    fn record_i64(&mut self, field: &Field, value: i64) {
        if field.name() == "duration_ms" && value >= 0 {
            self.duration_ms = Some(value as u64);
        } else {
            self.extra.insert(
                field.name().to_string(),
                serde_json::Value::Number(value.into()),
            );
        }
    }

    fn record_u64(&mut self, field: &Field, value: u64) {
        if field.name() == "duration_ms" {
            self.duration_ms = Some(value);
        } else {
            self.extra.insert(
                field.name().to_string(),
                serde_json::Value::Number(value.into()),
            );
        }
    }

    fn record_bool(&mut self, field: &Field, value: bool) {
        self.extra
            .insert(field.name().to_string(), serde_json::Value::Bool(value));
    }
}

/// Outcomes recognised by §11.1. Every non-ok terminal event MUST set
/// `reason`; emitting through this enum keeps that contract honest
/// without forcing string literals at the call sites.
#[derive(Debug, Clone, Copy)]
pub enum Outcome {
    Started,
    Ok,
    Error,
    Timeout,
    Rejected,
    Skipped,
}

impl Outcome {
    pub fn as_str(self) -> &'static str {
        match self {
            Outcome::Started => "started",
            Outcome::Ok => "ok",
            Outcome::Error => "error",
            Outcome::Timeout => "timeout",
            Outcome::Rejected => "rejected",
            Outcome::Skipped => "skipped",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use tracing::info;
    use tracing_subscriber::prelude::*;

    /// `Mutex<Vec<u8>>` sink that `Layer` can write into for tests.
    #[derive(Clone, Default)]
    struct VecSink(Arc<Mutex<Vec<u8>>>);

    impl Write for VecSink {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            self.0.lock().unwrap().write(buf)
        }

        fn flush(&mut self) -> io::Result<()> {
            self.0.lock().unwrap().flush()
        }
    }

    #[test]
    fn envelope_contains_named_fields() {
        let sink = VecSink::default();
        let captured = sink.0.clone();

        let layer = LogEnvelopeLayer::new(sink);
        let subscriber = tracing_subscriber::registry().with(layer);

        tracing::subscriber::with_default(subscriber, || {
            info!(
                event = "service.started",
                outcome = Outcome::Ok.as_str(),
                duration_ms = 42_u64,
                "service.started"
            );
        });

        let bytes = captured.lock().unwrap().clone();
        let line = String::from_utf8(bytes).unwrap();
        let parsed: serde_json::Value =
            serde_json::from_str(line.trim()).expect("envelope must be valid JSON");

        assert_eq!(parsed["event"], "service.started");
        assert_eq!(parsed["outcome"], "ok");
        assert_eq!(parsed["level"], "INFO");
        assert_eq!(parsed["duration_ms"], 42);
        assert!(parsed["ts"].as_str().unwrap().contains('T'));
    }

    #[test]
    fn unset_correlation_ids_render_as_null() {
        let sink = VecSink::default();
        let captured = sink.0.clone();

        let layer = LogEnvelopeLayer::new(sink);
        let subscriber = tracing_subscriber::registry().with(layer);

        tracing::subscriber::with_default(subscriber, || {
            info!(event = "service.started", outcome = "started", "boot");
        });

        let bytes = captured.lock().unwrap().clone();
        let line = String::from_utf8(bytes).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(line.trim()).unwrap();

        assert!(parsed["request_id"].is_null());
        assert!(parsed["resolve_id"].is_null());
        assert!(parsed["peer_id"].is_null());
        assert!(parsed["session_id"].is_null());
        assert!(parsed["reason"].is_null());
    }
}
