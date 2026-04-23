# PROBLEMS.md

Remaining items from the 10-agent staff-engineer review. Fixes shipped in `694a8b9` and the follow-up commit in this session have been removed.

---

## Status

- **Fixed**: 29 findings across stream_proxy, resolver, cache, mp4_parser, nzbdav_api, hydra, prowlarr, webdav, router, player_installer, i18n, service. See the commit bodies on `fix/main-post-pr82-reconciliation` for per-fix breakdowns.
- **Agent over-flagged (no action)**: 9 findings (already-caught exception, intentional behavior, already-defensive code paths).
- **Remaining**: listed below. All are either complex concurrency fixes, speculative findings that need more investigation, or LOW-priority polish.

---

## 1. HLS subsystem — non-trivial concurrency (`stream_proxy.py`)

Real findings but each needs careful design. Not batched with mechanical fixes.

- **HIGH** `stream_proxy.py:1678-1706` (`_serve_hls_segment`) — TOCTOU between `wait_for_segment()` → `os.path.getsize()` → headers → `open()` → read. Fix: `os.open` + `fstat` inside the same locked section.
- **MED** `stream_proxy.py:2650` (`HlsProducer._ffmpeg_log`) — reopen log on respawn to avoid OSError on closed fd when a respawn races with `close()`.
- **MED** `stream_proxy.py:3305-3325` (matroska fallback on `prepare()` failure) — validate ctx fields before reuse.

## 2. Verify-before-fix (resolver.py / filter.py)

- **VERIFY** `resolver.py:185` — SQLite commit claim under `with sqlite3.connect(...)`.
- **VERIFY** `resolver.py:1065` / `:989` — `_existing_completed_stream()` tuple-vs-scalar return shape.
- **VERIFY** `filter.py:436-438` — case-sensitivity trace through multiselect save/load path.

## 3. Test suite (tests/)

- **HIGH** `tests/test_filter.py:32-34` — codec assertion too strict; use pattern match instead of exact tuple membership.
- **HIGH** `tests/test_stream_proxy.py:788, 812, 1226` — mock-only assertions; add argv inspection.
- **HIGH** `tests/test_integration_hls_ffmpeg.py` — pytest yield-fixture with SIGKILL teardown instead of `finally` cleanup.
- **HIGH** `tests/test_nzbdav_api.py:301-303` — decode base64 + assert components instead of exact string.
- **MED** `tests/test_prowlarr.py:187-190` — add `call_count`/`call_args_list` assertions.
- **MED** `tests/test_service.py:13-27` — function-scoped fixture with teardown.
- **MED** `tests/test_resolver_errors.py:16-46` — move common patches to conftest.
- **MED** `tests/test_stream_proxy.py` — centralize lock setup.
- **MED** `tests/conftest.py:17-32` — `_FakePlayer.isPlaying()` mutable state.
- **LOW** `tests/test_mp4_parser.py` — stco rewrite correctness under >4 GB delta.
- **LOW** `tests/test_results_dialog.py:68-78` — assert selected-index return path.
- **LOW** `tests/test_nzbdav_api.py:290+` — socket-timeout path test.
- **LOW** `tests/test_webdav.py` — null/invalid/whitespace href edge cases.
- **LOW** `tests/test_router.py` — direct coverage of `_handle_play`/`_handle_search`.

## 4. Polish / low priority

- **LOW** `stream_proxy.py:2437-2444` — document GIL-atomic-int-read assumption or switch to explicit lock.
- **LOW** `mp4_parser.py:379` — unused `payload_remote_end` sentinel.
- **LOW** `resolver.py:74-77` — `_clamp_int_setting` log spam on every play.
- **LOW** `router.py:802, 822, 844` — test-connection URLs may leak API key if urllib exception includes URL.
- **LOW** `hydra.py` ↔ `prowlarr.py` — consolidate duplicate helpers (`_calculate_age`, `_get_text`, `_format_request_error`) into `http_util.py`.
- **LOW** `nzbdav_api.py:140, 269` — narrow bare `except Exception`.
- **LOW** `http_util.py:28` — `errors="replace"` silently corrupts non-UTF-8 bodies.
- **MED** `webdav.py:138-167` — XXE mitigation (pure-Python constraint blocks `defusedxml`; use `XMLParser(resolve_entities=False)`).
- **MED** `webdav.py:148-149` — percent-encoded slash handling in `quote(..., safe="/") + lstrip("/")`.
- **LOW** `webdav.py:72` — `/content/` hardcoded.
- **LOW** `webdav.py:164` — `errors="replace"` on PROPFIND body.
- **LOW** `webdav.py:222-224` — `getcontentlength` silent parse failure.
- **MED** `player_installer.py:16-18, 51` — schema version / backup of existing `nzbdav.json`.
- **LOW** `results_dialog.py:92-146` — 40+ setProperty calls per item; consider batching or moving to XML.
- **LOW** `results_dialog.py:157-168` — unhandled `ACTION_CONTEXT_MENU`.

## 5. Deferred

- **DEFER** `tests/conftest.py:10` — `xbmcaddon.Addon = MagicMock()` replacement broke 88 tests on attempt; requires per-test migration. Out of scope for reconciliation pass.

---

## Summary

From the original 65 findings, 29 are resolved with verification-in-source. 9 were agent over-flags that don't represent real bugs. The remaining 27 are a mix of: (a) three concurrency-heavy HLS items that need design work, (b) three VERIFY items that need focused source tracing, (c) 14 test-suite improvements, and (d) ~8 low-priority polish items.
