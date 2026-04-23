# TODO.md

**Remaining proxy work only.**

Completed, verified proxy implementation work moved to `DONE.md`.
Dolby Vision / PANI source work remains separate in `TODO_PANI.md`.

---

## Table of Contents

1. Active Worklist
2. Out-of-Scope: DV Work
3. Completed Work Archive
4. Remaining Integration & Rollout Gates
5. Epic — Article-Health Pre-Submit Filter
6. Epic — nzbdav-rs NNTP Retry / Timeout Tuning
7. Artifact Inventory
8. Maintenance Conventions

---

## 1. Active Worklist

Only the remaining work. Completed implementation belongs in `DONE.md`.

| Pri | Item | Owner | Est | Depends | Plan |
|---|---|---|---|---|---|
| P0 | Install `plugin.video.nzbdav-1.0.0-pre-alpha.zip` on the CoreELEC box via Kodi → Add-ons → Install from zip (zip already at `/storage/` on the box) | _unassigned_ | ~2 min | — | `plans/REMAINING_COREELEC_SMOKE.md` step 3 |
| P0 | Run CoreELEC smoke validation on a clean-article release (2 h, four seeks, audio sync check) | _unassigned_ | ~2 h wall | zip installed | `plans/REMAINING_COREELEC_SMOKE.md` |
| P1 | Validate `send_200_no_range=ON` on CoreELEC before ever enabling that flag | _unassigned_ | ~1 h wall | smoke passed | `plans/REMAINING_SEND_200_VALIDATION.md` |
| P2 | Start and track the ≥1 week observability soak post-merge | _unassigned_ | 7+ days wall | smoke passed | `plans/REMAINING_OBSERVABILITY_SOAK.md` |
| P2 | Decide whether to flip `strict_contract_mode` from `warn` to `enforce` | _unassigned_ | ~10 min | soak complete | `plans/REMAINING_OBSERVABILITY_SOAK.md` (exit criteria) |
| P2 | Decide whether to enable `density_breaker_enabled` | _unassigned_ | ~10 min | soak complete | `plans/REMAINING_OBSERVABILITY_SOAK.md` (exit criteria) |

### Gated

- Article-health filter epic (§5) stays blocked until the post-merge observability soak completes.
- nzbdav-rs NNTP retry / timeout tuning epic (§6) uses the same gate as §5.

### Housekeeping

- [ ] Delete or archive `scripts/review-prompts/proxy-review.md` if it is no longer needed after integration. (Updated to reusable template on 2026-04-22; keep if any `REMAINING_*.md` or `PROXY_EPIC_*.md` will be reviewed.)
- [x] Obsolete proxy planning mirrors removed (`plans/PROXY_REMEDIATION.md`, `plans/PROXY_EXECUTION.md`, `plans/PROXY_ADJUDICATION.md`).

---

## 2. Out-of-Scope: DV Work

Dolby Vision source-level fixes in `../piXBMC` and `../piCoreElec` are tracked in `TODO_PANI.md`. They are intentionally separate from the proxy rollout and validation work here.

---

## 3. Completed Work Archive

`DONE.md` is now the archive for the completed proxy remediation work:

- completed P0 / P1 / P4 / P5 implementation
- review / adjudication state that has already landed
- verification evidence from the verified worktree branch
- key file list and diff summary

Integration status:

- main workspace branch: `spike/hls-fmp4`
- PR-1 commit: `0111a39` (`feat(proxy): PR-1 reliability + security baseline (P0/P1/P4/P5)`)
- PR-1 merge commit: `16e7122` on `spike/hls-fmp4`
- `codex/proxy-pr1-range` branch and its worktree were removed post-merge.

PR-1 is merged on `spike/hls-fmp4` locally and pushed to `origin/spike/hls-fmp4` (2026-04-22). Integration is complete; the next gate is CoreELEC smoke validation.

---

## 4. Remaining Integration & Rollout Gates

### 4.1 Current rollout posture

- PR-1 is merged onto `spike/hls-fmp4` as `16e7122` and pushed to `origin/spike/hls-fmp4` (2026-04-22).
- Addon zip `plugin.video.nzbdav-1.0.0-pre-alpha.zip` built and staged at `root@coreelec.local:/storage/` ready to install via Kodi's "Install from zip" UI.
- `just lint` + `just test` verified green on the merged state (536 passed, 2 deselected).
- Defaults shipping with PR-1:
  - `strict_contract_mode = warn`
  - `density_breaker_enabled = false`
  - `retry_ladder_enabled = true`
  - `zero_fill_budget_enabled = true`
  - `send_200_no_range = false`

### 4.2 Required pre-ship validation

- CoreELEC smoke on a clean-article release.
- CoreELEC validation of `send_200_no_range=ON` before that flag is ever enabled in the field.
- Confirm no unexpected rejection patterns while `strict_contract_mode=warn`.

### 4.3 Post-merge soak gates

- Collect ≥1 week of observability data before considering:
  - `strict_contract_mode = enforce`
  - `density_breaker_enabled = true`
  - starting §5 or §6

### 4.4 Still-manual acceptance items

- The PR-2 acceptance gate comparing zero-fill behavior on a synthetic short-read fixture remains a manual regression run, not an automated unit test.
- CoreELEC hardware validation is still the deciding signal for `send_200_no_range`.

---

## 5. Epic — Article-Health Pre-Submit Filter

**Status:** gated epic. Do not start until entry criteria are satisfied.

**Goal:** reduce mid-stream recoveries and zero-fill by filtering unhealthy releases before submit.

### Entry criteria

- ≥1 week of post-merge observability data showing zero-fill incidents keyed to specific NZBs.
- nzbdav SABnzbd-compat endpoint (or NZBHydra detail endpoint) that exposes per-NZB article-completeness metrics, verified in a local nzbdav-rs build.
- Owner assigned.
- Scope doc drafted and reviewed.

### High-level design

Query the health endpoint during `_handle_play` / `_handle_search`, then down-rank or hard-filter results below a threshold in `resources/lib/filter.py`.

### Risks

- False negatives on actually-playable releases.
- Cross-subsystem dependency on nzbdav API surface.
- Threshold calibration remains empirical.

### Out of scope

- Automatic re-search on filter rejection.
- Health-aware caching.

---

## 6. Epic — nzbdav-rs NNTP Retry / Timeout Tuning

**Status:** gated epic. Do not start until entry criteria are satisfied.

**Goal:** tune per-provider retry budget, provider priority, and NNTP read timeout using post-merge telemetry.

### Entry criteria

- ≥1 week of post-merge observability data.
- Coordination with `nzbdav-rs` release cadence.
- Before/after recovery-rate measurement methodology agreed.
- Owner assigned.

### Risks

- Cross-subsystem scope.
- Over-tuning can either drop healthy requests or prolong dead ones.

### Out of scope

- Per-user provider selection.
- Dynamic provider ranking based on historical success rate.

---

## 7. Artifact Inventory

### Planning

- `DONE.md` — completed proxy implementation archive
- `TODO.md` — remaining proxy work only (dispatch)
- `TODO_PANI.md` — DV / PANI source-fix work
- `plans/REMAINING_COREELEC_SMOKE.md` — one-pager, active P1
- `plans/REMAINING_SEND_200_VALIDATION.md` — one-pager, active P1
- `plans/REMAINING_OBSERVABILITY_SOAK.md` — one-pager, active P2 (soak + flag decisions)
- `plans/PROXY_EPIC_ARTICLE_HEALTH.md` — gated epic
- `plans/PROXY_EPIC_NNTP_TUNING.md` — gated epic

### Proxy source

- `plugin.video.nzbdav/resources/lib/stream_proxy.py`
- `plugin.video.nzbdav/resources/lib/mp4_parser.py`
- `plugin.video.nzbdav/resources/lib/resolver.py`
- `plugin.video.nzbdav/resources/lib/webdav.py`
- `plugin.video.nzbdav/service.py`
- `tests/test_stream_proxy.py`
- `tests/test_mp4_parser.py`
- `tests/test_resolver.py`
- `tests/test_webdav.py`

---

## 8. Maintenance Conventions

- Put completed, verified proxy work in `DONE.md`, not `TODO.md`.
- Keep `TODO.md` limited to outstanding integration, rollout, and gated epic work.
- Keep DV work in `TODO_PANI.md`.
- Do not recreate `DV_FIX.md`, `PLAN_FIX_PROXY.md`, `TODO_ALLEN_ASAP.md`, `plans/PROXY_REMEDIATION.md`, `plans/PROXY_EXECUTION.md`, or `plans/PROXY_ADJUDICATION.md`. Their content is preserved in `DONE.md` (completed work) and `git log`.
- No artifact paths in `/tmp`.
