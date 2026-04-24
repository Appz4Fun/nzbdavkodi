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

### 2.5 DV Seek Plan Phase 1 — CFileCache-off pass-through

Addon-side components of TODO.md §D.5.1. Ships the quick-win pass-through tier for 32-bit Kodi's 4 GB seek-delta bug with a runtime gate + first-play dialog so users who have not applied `<memorysize>0</memorysize>` cannot crash Kodi.

- `stream_proxy.py`: added `force_remux_mode=2` (passthrough). Short-circuits the force-remux tier on huge MKVs and serves bytes 1:1 via `_serve_proxy` with full Content-Length + Accept-Ranges.
- `settings.xml`: dropdown value `30152` "Direct pass-through (requires advancedsettings.xml cache=0)".
- `kodi_advancedsettings.has_cache_memorysize_zero()`: read-only probe of `special://profile/advancedsettings.xml` for `<cache><memorysize>0</memorysize></cache>`. Never writes.
- Runtime gate in `stream_proxy.prepare_stream()`: if passthrough is selected but cache=0 is absent, fall back to matroska and fire a one-shot warning notification. A misconfigured user cannot crash 32-bit Kodi.
- `cache_prompt.maybe_show_cache_prompt`: first-large-file-play dialog (`Dialog.yesnocustom` with Show instructions / Not now / Never ask). Show instructions opens a textviewer with the XML snippet; addon never writes `advancedsettings.xml`. Session dedup via `nzbdav.cache_dialog.shown_this_session` window property, persistent dismissal via `cache_dialog_dismissed` setting.
- `AGENTS.md`: added "Pass-through mode (optional, recommended for large files)" section with the same snippet the dialog shows.

Relevant commits: `eefb400`, `624fe93`, `0e4b946`, `3ee019b`, `1600b06`.

Remaining hands-on work lives in TODO.md §D.5.1 step 6 (integration test on the 58 GB / 90 GB file with cache=0 actually applied on the CoreELEC box).

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


## Part E — Fix Verification Record (`BUG2.MD`)

> **Relevance:** skim only when merging or reviewing the 20-agent-review remediation. This Part is a historical fix-verification record, not an action list. Any items still actionable are lifted to §0.

All P0, P1, P2, and P3 findings from the 20-agent review are fixed and
verified in commit `4103f5d`. 643 → 657 tests pass; lint clean on
Python 3.10/3.12 CI matrix.

### E.1 Fix verification

| Tier | Count | Status |
|---|---|---|
| **P0 — merge-blockers** | 4/4 | ✅ fixed, covered by tests |
| **P1 — major** | 5/5 | ✅ fixed, covered by tests + docs |
| **P2 — important** | 9/9 | ✅ fixed or documented |
| **P3 — minor/nit** | 12/12 | ✅ fixed or intentional (P3.11) |
| **P4 — test coverage** | 14/18 | ✅ production-path tests all landed |

#### E.1.1 What was added to the test suite

- `tests/test_dv_rpu.py` — truncated RPU, wrong prefix, invalid rpu_type,
  emulation-prevention, polynomial linear-interp graceful-degradation.
- `tests/test_dv_source.py` — co64 chunk offsets, 4 GiB stsz clamp,
  SimpleBlock lacing refusal, BlockGroup/Block extraction, network error,
  unsupported extension, size-cap at `resp.read`, auth CRLF strip.
- `tests/test_stream_proxy.py` — probe-crash → matroska regression guard.

### E.2 Deferred (require upstream material we don't have or large synthesis work)

These are **nice-to-have** additional coverage; none block merge.

1. **Profile 5 RPU fixture** — upstream `quietvoid/dovi_tool` doesn't ship
   one. We have mocked routing coverage for P5 but not a real P5 RPU
   parser test. Would require either a public DV P5 sample clip or a
   hand-crafted synthesis.
2. **`coefficient_data_type == 1` test** — the alternative fixed-point
   encoding branch. No real-world fixture exercises it; all three vendored
   dovi_tool fixtures use `coefficient_data_type == 0`. See §D.4.1 for
   the on-device dovi_tool preprocessing approach that would generate
   a test corpus covering both encoding types.
3. **`use_prev_vdr_rpu_flag=True` test** — rare mid-stream frame type;
   requires hand-synthesizing an RPU with the flag set. The edge-case
   behavior is now documented in the `dv_rpu.py` module docstring (P2.8).
4. **Real open-source DV clip + dovi_tool cross-check CI harness** — a
   `@pytest.mark.integration` test that compares `dv_rpu.parse_rpu_payload`
   output against live `dovi_tool info --frame 0` output. Would guard
   against upstream drift, but is out of scope for this PR.

### E.3 Deferred from P3 (intentionally)

- **P3.1** `nal_length_size=4` hardcoded — hvcC's `lengthSizeMinusOne` can
  be 1/2/4 but real DV muxes (both MP4 and Matroska) always use 4. The
  code now has a comment explaining the parameter shape for a future hvcC-
  aware caller.
- **P3.11** `_validated_rpu_payload`'s `len(data) < 7` gate — reviewer
  classified as harmless; dovi_tool defers size validation similarly.

### E.4 Deferred from P2 (soft mitigations applied)

- **P2.4** MEL field validation breadth — only one MEL fixture. The soft
  mitigation landed: every `prepare_stream` DV probe now logs the full
  structured result at `LOGDEBUG`, so field testing can confirm whether
  real P7 MEL sources decode through fmp4 HLS. The in-code comment at the
  MEL branch explicitly notes "if field testing shows MEL also hangs,
  tighten this branch to match P8" — a one-line code change to restore
  the "any confirmed DV → matroska" behavior.
- **P2.3** `_validate_url` not called inside `_http_range` — would require
  moving `_validate_url` out of `stream_proxy.py` to avoid a circular
  import. Defense-in-depth only; current callers all validate upstream.
  CRLF stripping in the auth header DID land.

---
