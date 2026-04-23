# Validate `send_200_no_range=ON` on CoreELEC

**Status:** gated on PR-1 merge + clean smoke pass.

**Goal:** verify Kodi tolerates HTTP 200 (vs. always-206) on no-Range pass-through GETs before shipping the `send_200_no_range` flag default-ON. This is an HTTP-correctness cleanup that the addon has deliberately not shipped default-ON because Kodi's CCurlFile behavior at `Content-Length` + `200 OK` without `Accept-Ranges` was untested on the target build.

---

## Entry criteria

- Clean smoke test passed per `REMAINING_COREELEC_SMOKE.md`.
- Merged branch installed on the box.

---

## Setup

- Two back-to-back playback runs on the same clean-article release from the smoke test.
- Tail `kodi.log` for the full session in both runs.

---

## Steps

### Run A — baseline (default OFF)

1. Confirm in addon settings: `send_200_no_range = OFF` (default).
2. Play the release from start to `00:30`, seek to `01:00`, play 30 s, stop.
3. Save `kodi.log` → `soak-data/send200-off-<date>.log`.

### Run B — flag flipped ON

1. Enable `send_200_no_range = ON` via Kodi addon settings UI.
2. Repeat the same playback sequence: start → `00:30` → seek to `01:00` → play 30 s → stop.
3. Save `kodi.log` → `soak-data/send200-on-<date>.log`.

---

## What to compare

- Initial buffer time to first frame (should be within ±1 s between A and B).
- Seek-to-resume time at `01:00` (should be within ±2 s).
- Presence / absence of `Pass-through write aborted` warnings — should match A.
- Any new error-level lines in B that are not in A.

---

## Acceptance

- B behaves identically to A on all three comparisons.
- No new error lines in B.
- Optional byte-accuracy spot-check: `curl --range 0-1048575 http://<proxy>/<stream>` with the flag OFF vs ON, compare md5 — should match byte-for-byte for the first 1 MB.

---

## Decision

- **If green across both runs:** the flag is safe to ship default-ON in a follow-up PR. Update the default in `resources/settings.xml` and move the decision to `DONE.md`.
- **If any regression in Run B:** the flag stays default-OFF. Open an issue tagging the specific regression observed and defer.
