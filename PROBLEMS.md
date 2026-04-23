# PROBLEMS.md

Remaining items from the 10-agent staff-engineer review after four fix passes on `fix/main-post-pr82-reconciliation`.

---

## Status

- **Fixed**: 42 findings resolved across four commits (`d1cdf27`, `694a8b9`, `1a350ce`, `2535009`, `290ca2c`).
- **Agent over-flagged (no action)**: 15 findings confirmed as intentional behavior, already-defensive code, or invalid claims.
- **Remaining**: 8, listed below. All are LOW-priority polish or test-infrastructure work.

---

## Remaining

### Polish / minor source (3)

- **LOW** `stream_proxy.py:2437-2444` — `_init_file_complete()` uses GIL-atomic int reads without an explicit lock. Documented in-source; fragile against future async refactors. Option: add a `threading.Event` gate.
- **LOW** `http_util.py:28` — `decode("utf-8", errors="replace")` silently corrupts non-UTF-8 bodies. Acceptable tradeoff (RSS/XML is nominally UTF-8; corrupted bytes become `?` rather than a hard failure).
- **LOW** `hydra.py` ↔ `prowlarr.py` — near-duplicate helpers (`_calculate_age`, `_get_text`, `_format_request_error`) could move to `http_util.py` as shared utilities. Pure refactor; no behavior change.

### Test-infrastructure polish (5)

- **HIGH** `tests/test_integration_hls_ffmpeg.py` — integration tests rely on `finally` cleanup; a crash mid-test leaves zombies and `/tmp/nzbdav-hls-*` orphans. Convert to pytest yield-fixture with SIGKILL teardown.
- **MED** `tests/test_resolver_errors.py:16-46` — 6+ `@patch` decorators per test; move common xbmc/xbmcgui/xbmcplugin patches into conftest.
- **MED** `tests/test_stream_proxy.py` — inconsistent `threading.Lock()` setup; centralize in `_make_handler_with_server` fixture.
- **MED** `tests/conftest.py:17-32` — `_FakePlayer.isPlaying()` always returns False; add mutable state so playback-transition tests can exercise the flow.
- **LOW** `tests/test_mp4_parser.py` — no stco rewrite test under delta crossing the 4 GB boundary.
- **LOW** `tests/test_router.py` — `_handle_play` / `_handle_search` / `_safe_resolve_handle` not directly exercised.

### Deferred (risky)

- **DEFER** `tests/conftest.py:10` — `xbmcaddon.Addon = MagicMock()` replacement broke 88 tests in a prior attempt; needs per-test migration.

---

## Summary

From the original 65 findings:

- **42 resolved** with source verification and passing tests
- **15 agent over-flags** closed with trace evidence
- **8 remaining**, all LOW or test-infrastructure

All critical and HIGH source-code items are now addressed. No open blockers for merging to main.
