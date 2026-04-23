# Proxy Epic — nzbdav-rs NNTP Retry / Timeout Tuning

**Status:** gated epic. Do not start until entry criteria are satisfied.

**Goal:** tune per-provider retry budget, provider priority, and NNTP read timeout using post-merge telemetry.

---

## Entry criteria

- ≥1 week of post-merge observability data.
- Coordination with `nzbdav-rs` release cadence.
- Before/after recovery-rate measurement methodology agreed.
- Owner assigned.

---

## Risks

- Cross-subsystem scope.
- Over-tuning can either drop healthy requests or prolong dead ones.

---

## Out of scope

- Per-user provider selection.
- Dynamic provider ranking based on historical success rate.
