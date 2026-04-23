# DONE.md

**Completed proxy implementation archive.**

This file records the proxy remediation work that has been coded and verified already. It is separate from `TODO.md`, which now tracks only the remaining integration, rollout, and gated follow-up work.

---

## 1. Verification Snapshot

Verification was run against the isolated worktree branch:

- verified branch: `codex/proxy-pr1-range`
- main workspace branch at the same time: `spike/hls-fmp4`
- date: `2026-04-22`

Fresh verification results:

- `just lint` — passed
- `just test` — passed (`536 passed, 2 deselected`)

Verified diff footprint on the worktree branch at archive time:

- `9 files changed`
- `2093 insertions`
- `279 deletions`

---

## 2. Completed Proxy Work

### 2.1 P0 — correctness

- Hardened `_parse_range` so malformed ranges reject cleanly and handler-level `416` behavior fires at all four call sites.
- Added explicit upstream range result enums in `_stream_upstream_range`: `OK`, `SHORT_READ_RECOVERABLE`, `PROTOCOL_MISMATCH`, `UPSTREAM_ERROR`.
- Added strict upstream contract validation with `strict_contract_mode = off | warn | enforce`.
- Added the density-window breaker behind `density_breaker_enabled`.

### 2.2 P1 — resilience and security

- Added minimum observability: reason-coded logs, terminal per-stream summaries, upstream metadata on short reads.
- Removed credential leakage in ffmpeg / ffprobe / temp-faststart / legacy HLS ffmpeg argv paths by switching to `-headers Authorization: ...`.
- Fixed `_get_stream_context` so session lookup and `last_access` update happen under the proxy context lock.
- Added layered zero-fill enforcement with quantified notifications and diagnostic-only global budget semantics.
- Added the retry ladder before `_find_skip_offset`.

### 2.3 P4 — cleanups

- Clamped `force_remux_threshold_mb`, `poll_interval`, and `download_timeout`.
- Added `do_POST` request-body cap at 64 KB with `413`.
- Added ffmpeg capability probing at service start and fMP4-HLS fallback to matroska when required muxer flags are absent.
- Added `send_200_no_range` as a default-OFF kill switch for no-range pass-through GET behavior.
- Added a debug-time lock-ownership assert for `_prune_sessions_locked`.
- Retired the four legacy flat-WebDAV helpers in `webdav.py`.

### 2.4 P5 — test backfill

- Added `_parse_range` caller-matrix coverage.
- Added HTTP contract validation tests for warn/enforce behavior and bad `Content-Range`.
- Added deterministic density-breaker coverage.
- Added `RangeCache` concurrency / churn coverage.
- Added HLS producer respawn-during-seek concurrency coverage.
- Added DV-on-matroska-fallback dispatch coverage.

---

## 3. Key Files Changed

- `plugin.video.nzbdav/resources/lib/stream_proxy.py`
- `plugin.video.nzbdav/resources/lib/resolver.py`
- `plugin.video.nzbdav/resources/lib/webdav.py`
- `plugin.video.nzbdav/resources/settings.xml`
- `plugin.video.nzbdav/resources/language/resource.language.en_gb/strings.po`
- `tests/test_stream_proxy.py`
- `tests/test_mp4_parser.py`
- `tests/test_resolver.py`
- `tests/test_webdav.py`

---

## 4. Review / Adjudication State Captured Here

The implemented branch reflects the adjudicated proxy plan state including:

- Track C global-budget downgrade to observability-only
- credential scrubbing moved ahead of strict-contract enforcement work
- `strict_contract_mode` rollout as `off | warn | enforce`
- `send_200_no_range` staying default `OFF`
- epic work split away from first-pass in-repo implementation

The review prompt and older mirrored plan docs are no longer the primary record for this completed work.

---

## 5. Not Done Yet

These items are intentionally **not** part of the completed archive:

- CoreELEC smoke validation on a clean-article release
- CoreELEC validation for `send_200_no_range=ON`
- the ≥1 week observability soak
- the Article-Health pre-submit filter epic
- the nzbdav-rs NNTP retry / timeout tuning epic

Those remaining items now live in `TODO.md`.

---

## 6. Integrated

- PR-1 commit: `0111a39` — `feat(proxy): PR-1 reliability + security baseline (P0/P1/P4/P5)`
- Merge commit on `spike/hls-fmp4`: `16e7122` — `Merge PR-1: proxy reliability + security baseline`
- Pushed to `origin/spike/hls-fmp4`: 2026-04-22
- Worktree `.worktrees/proxy-pr1-range/` removed; branch `codex/proxy-pr1-range` deleted post-merge.

Post-merge verification on `spike/hls-fmp4`:

- `just lint` — passed
- `just test` — passed (`536 passed, 2 deselected`)
