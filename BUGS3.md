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
`int()` wrapping, generate_repo repo-fanart asset). Severities below
are agent-assigned on static-analysis confidence and need re-triage
against live inputs before fix work — several items are defensive
hardening that may never fire on today's traffic.

## Critical

- **`nzbdav_api.py` `.get()` chain on non-dict json** | `nzbdav_api.py:285,333,384,412,480,554,612` | All 6 helpers do `response.get(...)` outside the try/except wrapping `json.loads`; an array/null/scalar response raises uncaught AttributeError. Impact bounded — the worst (`get_job_status`) runs in a daemon thread so it silently kills polling rather than the whole resolve, but cancel/find paths surface the error.

## High

- **`cache_prompt.yesnocustom` RuntimeError unhandled** | `cache_prompt.py:88-94` | Lifecycle / shutdown errors escape the dialog call; the session-shown flag is set first so users see no prompt next session.
- **Prowlarr XML parser missing XXE hardening** | `prowlarr.py:229` | Direct `ET.fromstring(xml_text)` while `hydra.py:254` uses `_build_xxe_safe_parser()` — parity gap for the same threat.
- **`_BitReader.read_bit` no bounds check** | `dv_rpu.py:51` | Truncated RPU payload triggers IndexError instead of ValueError; parser propagates raw exception to caller.
- **Cached `ctx["auth_header"]` survives nzbdav apikey rotation** | `stream_proxy.py` (ctx auth header) | 401/403 from rotated key is caught generically and feeds the zero-fill recovery loop, masking auth failure as data corruption.
- **Shared `self._server.stream_context` torn down by second client** | `stream_proxy.py:_get_stream_context` | Concurrent `prepare_stream` from a second player calls `clear_sessions()` mid-handler on the first.
- **`tests/conftest.py` module-level mock install with no teardown** | `tests/conftest.py:14-15,51,92` | `sys.modules["xbmc"]` patches and `xbmc.Player = _FakePlayer` persist for the entire test session, hiding pollution.

## Medium

- **`cache_prompt` Never-ask `setSetting` swallowed silently** | `cache_prompt.py:99-102` | User clicks Never ask, sees the dialog again next session, no error logged.
- **`prowlarr.py` HTTPError/URLError logged unredacted** | `prowlarr.py:149,173` | Same theme as the hydra-line entries already filed, different module / lines.
- **`stsz` parse trusts `body_end` it never reads** | `dv_source.py:210-218` | `struct.unpack_from(">I", moov, stsz_body_start + 12)` can run past the `stsz` box on a malformed track.
- **`_BitReader.read_ue` unbounded leading-zero count** | `dv_rpu.py:61` | Truncated stream returns a giant value instead of raising; downstream computations get garbage.
- **`vdr_rpu_profile > 1` silently maps to profile 0** | `dv_rpu.py:94-105` | Unknown profile collapses to 0 instead of surfacing a parse error.
- **`_validated_rpu_payload` minimum length disagrees with `_BitReader` invocation** | `dv_rpu.py:395 vs 145` | Validator rejects <7 bytes but the 1-byte `payload[0] == 25` shortcut path can still feed a tiny payload to the bit reader.
- **`mp4_parser._CONTAINERS` excludes `mvex`** | `mp4_parser.py:148` | Fragmented MP4 (`moof`/`trex`) is silently unsupported with no documented reason.
- **TODO.md §F.3.3 soak grep targets miss actual log tokens** | `TODO.md` vs `stream_proxy.py:2171,2315,2397,2417` | Playbook expects uppercase tags; production emits `upstream_open_failed`, `short_read_recoverable`, `session_zero_fill_budget_exceeded`.
- **HDR regex matches `hdr1`** | `filter.py:687,689` | `\bhdr10?\b` makes the `0` optional; HDR10+ alternation `\bhdr10\+|hdr10plus\b` lacks a leading word boundary on the second branch.
- **`_segment_complete` reads `_spawn_time` outside the lock** | `stream_proxy.py:2701,2726` vs writer under lock | TOCTOU on rapid HLS respawn; harmless on x86 but not contract-safe.
- **TTL/timeout arithmetic uses `time.time()` (wall clock)** | `cache.py:73`, `stream_proxy.py:2481-2519`, `resolver.py:1146` | NTP backward jump prematurely expires entries or extends timeouts; should be `monotonic()`.
- **ffmpeg `Popen` output captured without `text=True`/explicit encoding** | `stream_proxy.py:4262,4437` | Relies on after-the-fact `.decode(errors="replace")` instead of upfront text mode.
- **`DolbyVisionSourceResult.profile` Optional vs `DolbyVisionRpuInfo.profile` non-Optional** | `dv_source.py:50` vs `dv_rpu.py` | Annotation contract drift — `None` is reachable through one type but not the other.
- **Duplicate-job 500 doesn't try to adopt existing nzo_id** | `resolver.py:739,910` | `find_queued_by_name` exists but isn't called on the duplicate-submit error path; user sees an error dialog while the job is actually running.
- **Empty-NZB submit rejection indistinguishable from network error** | `nzbdav_api.py:204` | Returns `(None, None)` for both, so resolver retries 3× before user sees a generic failure instead of a specific message.
- **`_parse_newznab_attrs` hard-codes the 2010 namespace URI** | `hydra.py:25,179` | RSS variants using a default namespace or alternate URI silently lose `size` and `indexer`.
- **`scripts/build_zip.py` no graceful error on bad addon.xml** | `scripts/build_zip.py:18-20` | Missing file or missing `version` attribute crashes with raw `ET.parse` / `KeyError`.
- **CI matrix is Python 3.10 / 3.12; supported floor is 3.8** | `.github/workflows/ci.yml:25` | `pylint.yml` runs on 3.8 but `ci.yml` (tests + lint) does not — 3.8-specific syntax regressions can ship without CI noise.
- **`filter._normalize_metadata` type errors escape PTT try/except** | `filter.py:354-384` | The catch at line 342 wraps `parse_title` only; type-coercion failures on `hdr`/`audio`/`channels`/`year`/`container` propagate.
- **`tests/test_cache_prompt.py` mutates `Addon.return_value` without try/finally** | `tests/test_cache_prompt.py:104-105,128-129,152-153,177` | State leaks across tests; ordering bugs.

## Low

- **DV container detection ignores URL fragments** | `dv_source.py:481-486` | `url#.mp4` mis-detected.
- **Emulation-prevention byte-removal edge case** | `dv_rpu.py:174` | Consecutive `0x03` after a zero run not fully scrubbed in pathological streams.
- **`_canonical_init_bytes` read/write ABA** | `stream_proxy.py:1813,2828` | Mitigated by GIL today but not future-proof under sub-interpreters / no-GIL.
- **`_cache_bust_url` ms-precision collision on coarse clocks** | `resolver.py:151` | `int(time.time() * 1000)` collides on rapid replays where the clock resolution is >= 1 ms.
- **`router.py` bool inconsistency: `!= "false"` vs `== "true"`** | `router.py:220` | `nzbhydra_enabled` defaults on, `prowlarr_enabled` defaults off — works but confuses future readers.
- **`build_zip.py` flattens file modes to `0o644`** | `scripts/build_zip.py:26` | Executable bits / special modes lost; fine for this addon, flagged for completeness.
- **`generate_repo.py` writes addons.xml.md5 with no trailing newline** | `scripts/generate_repo.py:63` | Some clients expect line-terminated checksums.
- **`tests/test_stream_proxy.py` repeats the same mock-Addon save/restore 34×** | `tests/test_stream_proxy.py` | Refactor opportunity — extract a fixture.
- **README claims 694 unit tests; pytest collects 695** | `README.md` vs `pytest --collect-only` | One-test drift.
- **`dv_source.py` docstring says "Never raises"; `parse_unspec62_nalu` can raise UnicodeDecodeError** | `dv_source.py:476-479` | Contract-bug; tighten docstring or add the catch.
- **PTT vendored, returned-dict shape never asserted** | `plugin.video.nzbdav/resources/lib/ptt/` vs `filter.py` | Upstream PTT contract change would break filter quality gates silently.

---

Cross-reference: `QA_SCAN_20260424.md` (broad, 100-agent breadth pass), this file (depth + newer code paths + build/test/skin).
