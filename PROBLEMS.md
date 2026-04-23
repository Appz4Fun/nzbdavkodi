# PROBLEMS.md

Remaining items from the 10-agent staff-engineer review after three fix passes on `fix/main-post-pr82-reconciliation`.

---

## Status

- **Fixed**: 36 findings resolved across three commits.
- **Agent over-flagged (no action)**: 12 findings (already-caught exception, intentional behavior, already-defensive code, misnamed file references).
- **Remaining**: 17, listed below. Mostly VERIFY items or test-polish.

---

## 1. Verify-before-fix (needs focused source trace)

- **VERIFY** `resolver.py:185` — SQLite commit claim under `with sqlite3.connect(...)`.
- **VERIFY** `resolver.py:1065` / `:989` — `_existing_completed_stream()` tuple-vs-scalar return shape.
- **VERIFY** `filter.py:436-438` — case-sensitivity trace through multiselect save/load path (filter comparison already lowercases; only the dialog preselect path could still matter).

## 2. Test-suite polish

- **HIGH** `tests/test_stream_proxy.py` — several mock-only assertions (argv inspection would make them more useful). Agent flagged 3 specific sites but 2 are actually contract-level correct (`assert_not_called`, `assert_called_once_with(413)`).
- **HIGH** `tests/test_integration_hls_ffmpeg.py` — convert `finally` cleanup to a pytest yield-fixture with SIGKILL teardown so a mid-test crash doesn't leave ffmpeg zombies and orphan `/tmp/nzbdav-hls-*` dirs.
- **MED** `tests/test_resolver_errors.py:16-46` — move common xbmc/xbmcgui/xbmcplugin patches into conftest so per-test decorator stacks shrink from 6+ to 2-3.
- **MED** `tests/test_stream_proxy.py` — centralize `threading.Lock()` setup in a `_make_handler_with_server` fixture (currently inline in every test).
- **MED** `tests/conftest.py:17-32` — `_FakePlayer.isPlaying()` always returns False; add mutable state so playback-transition tests can exercise the flow.
- **LOW** `tests/test_mp4_parser.py` — no stco rewrite test under >4 GB delta; add a synthetic MP4 + verify offsets change correctly.
- **LOW** `tests/test_results_dialog.py:68-78` — assert selected-index return path, not just that `doModal` was called.
- **LOW** `tests/test_nzbdav_api.py:290+` — add a socket-timeout path test.
- **LOW** `tests/test_webdav.py` — edge cases: null href, invalid XML structure, href with leading whitespace.
- **LOW** `tests/test_router.py` — direct coverage of `_handle_play` / `_handle_search` is missing.

## 3. Low-priority polish / open design

- **LOW** `stream_proxy.py:2437-2444` — document GIL-atomic-int-read assumption in `_init_file_complete()`, or switch to an explicit `threading.Event`.
- **MED** `stream_proxy.py:3305-3325` — matroska fallback path re-reads `ctx["ffmpeg_path"]`, `ctx["duration_seconds"]` after a failed fmp4 probe; validate ctx fields before reuse.
- **LOW** `webdav.py:72` — `/content/` hardcoded in probe path (can't be configured for differently-routed nzbdav instances).
- **LOW** `webdav.py:148-149`, `:164` — percent-encoded-slash handling + `decode(errors="replace")` on PROPFIND body (silent corruption).
- **LOW** `webdav.py:222-224` — `getcontentlength` silent non-numeric parse failure.
- **MED** `player_installer.py:16-18, 51` — no schema version / backup on existing `nzbdav.json` overwrite.
- **LOW** `hydra.py` ↔ `prowlarr.py` — consolidate duplicate helpers (`_calculate_age`, `_get_text`, `_format_request_error`) into `http_util.py`.
- **LOW** `http_util.py:28` — `decode(errors="replace")` silently corrupts non-UTF-8 bodies.
- **LOW** `nzbdav_api.py:140, 269` — narrow bare `except Exception` to specific network/JSON errors.
- **LOW** `router.py:802, 822, 844` — if a urllib exception includes the URL, test-connection error notify may leak API key (60-char truncation usually saves it).
- **LOW** `resolver.py:862` — retry abort log now distinguishes shutdown, but the broader "cancel vs timeout vs shutdown" distinction still thin. Acceptable.

## 4. Deferred

- **DEFER** `tests/conftest.py:10` — `xbmcaddon.Addon = MagicMock()` replacement broke 88 tests in trial; requires per-test migration. Out of scope for reconciliation pass.

---

## Summary

From the original 65 findings: **36 resolved**, **12 over-flags**, **17 outstanding**. Of the outstanding: 3 VERIFY (focused tracing), 10 test-quality, 4 low-priority polish/design. No HIGH source-code items remain.
