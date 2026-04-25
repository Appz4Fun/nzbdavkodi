# BUGS3.md — 50-agent follow-up bug hunt

Scan date 2026-04-24. Scope: 50 Explore agents run in parallel against
the same `3ee019b` tree as `QA_SCAN_20260424.md`, biased toward newer
code paths (Dolby Vision parser, cache prompt, contract-mismatch
hardening, Prowlarr search), deeper bug classes (typed-contract drift,
TOCTOU, NTP wall-clock arithmetic), more end-to-end scenarios, and the
build / CI / test scaffolding (`scripts/`, `.github/workflows/`,
`tests/conftest.py`). Items already present in `QA_SCAN_20260424.md` or
`ISSUE_REPORT.md` were dropped during dedup; spot-checks against the
source confirmed the bulk but rejected a handful (build_zip
`os.walk` symlinks, generate_repo zip-version, stream_max_retries
`int()` wrapping, generate_repo repo-fanart asset).

> **Status (2026-04-24):** ~24 of the original 36 findings have been
> fixed in commits `06a047a` and `0ae903f`. The entries below are what
> remains: three architectural items that need design before code, plus
> a handful of false positives / cosmetic items that were rejected on
> closer inspection of the live code. Severities are still agent-
> assigned and may not match real-world impact.

## High — still open (architectural)

- **Cached `ctx["auth_header"]` survives nzbdav apikey rotation** | `stream_proxy.py` (ctx auth header) | 401/403 from a rotated key is caught generically and feeds the zero-fill recovery loop, masking auth failure as data corruption. Fix needs a 401-aware exception path in `_stream_upstream_range` plus a refresh hook to re-read the WebDAV auth headers and tear down the active session, which is invasive enough to merit a design note before code.
- **Shared `self._server.stream_context` torn down by second client** | `stream_proxy.py:_get_stream_context` | Concurrent `prepare_stream` from a second player calls `clear_sessions()` mid-handler on the first. Fix needs the stream-context registry to be keyed by `session_id` rather than a singleton on the server, which touches every call site of `_get_stream_context` and the prune/eviction path.
- **`tests/conftest.py` module-level mock install with no teardown** | `tests/conftest.py:14-15,51,92` | `sys.modules["xbmc"]` patches and `xbmc.Player = _FakePlayer` persist for the entire test session. Refactoring this to a session-scoped autouse fixture with explicit teardown is a large test-scaffolding change and not tackled in this sweep.

## Medium — still open / deferred

- **`mp4_parser._CONTAINERS` excludes `mvex`** | `mp4_parser.py:148` | Fragmented MP4 (`moof`/`trex`) is silently unsupported. Out of scope for the proxy today — fragmented MP4 is what `HlsProducer` produces, not what `mp4_parser` rewrites — but worth a docstring note. Deferred until the fragmented-MP4 input case actually shows up in field traffic.

## Low — false positives / not-actionable

- **`vdr_rpu_profile > 1` silently maps to profile 0** | `dv_rpu.py:94-105` | `return 0` here is an intentional "no clear DV signal" sentinel matching the `bl_video_full_range_flag` path on line 96. Callers (`dv_source._classify_parsed_rpu`) handle profile=0 as `non_dv`. Not a bug.
- **Emulation-prevention byte-removal edge case** | `dv_rpu.py:174` | The HEVC spec defines exactly one `0x03` after `00 00` as the emulation byte; consecutive `0x03 0x03` is data + emulation, not double-emulation. Current behavior is per-spec correct.
- **`DolbyVisionSourceResult.profile` Optional vs `DolbyVisionRpuInfo.profile` non-Optional** | `dv_source.py:50` vs `dv_rpu.py` | The two layers have different invariants by design — RpuInfo is post-parse (always set), SourceResult is post-classification (None for non-DV). Annotation is correct.
- **`tests/test_cache_prompt.py` mutates `Addon.return_value` without try/finally** | `tests/test_cache_prompt.py:104+` | Each test uses `@patch("resources.lib.cache_prompt.xbmcaddon")` which creates a fresh per-test mock; the patch context restores at test exit. Mutations on the per-test mock don't leak. False positive.
- **`build_zip.py` `os.walk` follows symlinks** | `scripts/build_zip.py` | `os.walk` defaults to `followlinks=False`. False positive.
- **`build_zip.py` flattens file modes to 0o644** | `scripts/build_zip.py:26` | Cosmetic; the addon ships no executable Python files. Flagged but not fixed.
- **`tests/test_stream_proxy.py` repeats the same mock-Addon save/restore 34×** | `tests/test_stream_proxy.py` | Refactor opportunity, not a bug. Deferred — touching 34 tests is high-blast-radius for low-value cleanup.
- **`_canonical_init_bytes` read/write ABA** | `stream_proxy.py:1813,2828` | Mitigated by GIL today; the agent flagged this as "not future-proof under sub-interpreters / no-GIL" rather than a present-day bug. Not fixed.
- **PTT vendored, returned-dict shape never asserted** | `plugin.video.nzbdav/resources/lib/ptt/` vs `filter.py` | `filter.py` `parse_title_metadata` now wraps the normalisation block in `try/except (TypeError, AttributeError, KeyError)`, which gives runtime tolerance even when PTT drifts — but no formal contract assertion was added. Defensive-only, deferred.

---

Cross-reference: `QA_SCAN_20260424.md` (broad, 100-agent breadth pass), this file (depth + newer code paths + build/test/skin).
