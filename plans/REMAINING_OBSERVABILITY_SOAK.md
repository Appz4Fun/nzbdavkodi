# Observability Soak (≥1 week)

**Status:** gated on PR-1 merge.

**Goal:** collect at least 7 days of real-world playback telemetry from the merged code to inform:
- whether to flip `density_breaker_enabled` default-ON
- whether to flip `strict_contract_mode` from `warn` to `enforce`
- threshold calibration for the Article-Health epic (`PROXY_EPIC_ARTICLE_HEALTH.md`)

Without this data, every remaining flag-flip and epic-kickoff decision is a guess.

---

## Entry criteria

- PR-1 merged and installed on the primary CoreELEC box.
- Normal daily viewing resumes (no synthetic load required).
- `kodi.log` rotation at default size (usually 1 MB) — do NOT set to unlimited; rotation means old logs roll to `kodi.old.log` and you have a window to grab them before they're lost.

---

## Setup

- Daily cron / manual step: pull `kodi.log` and `kodi.old.log` off the box into `docs/soak-data/<date>.log`. Script stub:
  ```bash
  DATE=$(date +%Y-%m-%d)
  scp root@coreelec.local:/storage/.kodi/temp/kodi.log docs/soak-data/${DATE}.log
  scp root@coreelec.local:/storage/.kodi/temp/kodi.old.log docs/soak-data/${DATE}.old.log 2>/dev/null || true
  ```

---

## What to extract each day

Grep targets. Counts matter more than details.

| Signal | Grep | What it tells you |
|---|---|---|
| session count | `Stream summary` | how many streams happened |
| bytes zero-filled | `zero_fill_bytes=` | per-session recovery volume |
| reason code mix | `reason=` | `UPSTREAM_OPEN_TIMEOUT` vs `SHORT_BODY` vs `PROTOCOL_MISMATCH` vs `BUDGET_EXHAUSTED` |
| strict-contract `warn` lines | `strict_contract warn` | would-have-been-rejected count at `warn` setting |
| density breaker would-trip | `density_would_trip` | dry-run count (breaker is OFF by default) |

Note: if those exact log tokens don't exist in the PR-1 code, adjust greps to match actual format. The principle is one-line grep + daily count.

---

## Analysis after 7 days

Record results inline in this file before making decisions.

1. **Zero-fill distribution.** 90th percentile bytes-per-session? If > 10 MB, a significant fraction of streams would hit the per-session cap.
2. **Reason-code mix.** Dominant cause of recoveries? Informs whether the retry ladder (P1.5) or upstream-side work (`PROXY_EPIC_NNTP_TUNING.md`) is the better next lever.
3. **strict_contract warn counts.** If 0 across 7 days → flip to `enforce` is safe. If > 0 → investigate each case before flipping.
4. **density_would_trip counts.** If 0 across 7 days → flip default-ON is safe. If > 0 → inspect the cases: are they bad releases the breaker SHOULD catch, or false positives?

---

## Exit criteria

- At least 7 distinct calendar days of logs captured in `docs/soak-data/`.
- Decisions recorded on:
  - [ ] `strict_contract_mode` → keep `warn` | flip to `enforce`
  - [ ] `density_breaker_enabled` → keep OFF | flip default ON
  - [ ] start `PROXY_EPIC_ARTICLE_HEALTH.md` — go / no-go
  - [ ] start `PROXY_EPIC_NNTP_TUNING.md` — go / no-go
- Move the completed decisions to `DONE.md` §5 → §2 style migration.
