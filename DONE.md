# DONE.md

Archive of completed proxy/DV work. Stale narrative removed on 2026-04-24 after confirmation the code itself (plus `git log`) is the authoritative record for what shipped. Only forward-looking items and the still-useful deferred-items list remain.

---

## 1. Shipped

- **PR-1 reliability + security baseline** (P0/P1/P4/P5) — commit `0111a39`, merged on main as `16e7122` on 2026-04-22. Covers `_parse_range` hardening, upstream range enums, `strict_contract_mode`, `density_breaker_enabled`, credential scrubbing via `-headers`, retry ladder, zero-fill budget, clamped settings, fMP4-HLS fallback, `send_200_no_range` kill switch, and the test backfill (`536 passed, 2 deselected`). Git log has the file-by-file detail.
- **20-agent review remediation** — commit `4103f5d`. All P0/P1/P2/P3 findings fixed or intentionally deferred. 643 → 657 tests pass; lint clean on Python 3.10/3.12 CI. See Part E for the per-tier tally and the deferred-items list.

---

## 2. Not Done Yet

These items are intentionally **not** part of the completed archive — they still live in `TODO.md`:

- CoreELEC smoke validation on a clean-article release
- CoreELEC validation for `send_200_no_range=ON`
- the ≥1 week observability soak
- the Article-Health pre-submit filter epic
- the nzbdav-rs NNTP retry / timeout tuning epic

---

## Part E — Fix Verification Record (`BUG2.MD`)

> **Relevance:** skim only when merging or reviewing the 20-agent-review remediation. This Part is a historical fix-verification record, not an action list.

All P0, P1, P2, and P3 findings from the 20-agent review are fixed and verified in commit `4103f5d`. 643 → 657 tests pass; lint clean on Python 3.10/3.12 CI matrix. The deferred-items list that follows is still active and worth skimming.

### E.2 Deferred (require upstream material we don't have or large synthesis work)

These are **nice-to-have** additional coverage; none block merge.

1. **Profile 5 RPU fixture** — upstream `quietvoid/dovi_tool` doesn't ship one. We have mocked routing coverage for P5 but not a real P5 RPU parser test. Would require either a public DV P5 sample clip or a hand-crafted synthesis.
2. **`coefficient_data_type == 1` test** — the alternative fixed-point encoding branch. No real-world fixture exercises it; all three vendored dovi_tool fixtures use `coefficient_data_type == 0`. See §D.4.1 for the on-device dovi_tool preprocessing approach that would generate a test corpus covering both encoding types.
3. **`use_prev_vdr_rpu_flag=True` test** — rare mid-stream frame type; requires hand-synthesizing an RPU with the flag set. The edge-case behavior is now documented in the `dv_rpu.py` module docstring (P2.8).
4. **Real open-source DV clip + dovi_tool cross-check CI harness** — a `@pytest.mark.integration` test that compares `dv_rpu.parse_rpu_payload` output against live `dovi_tool info --frame 0` output. Would guard against upstream drift, but is out of scope for this PR.

### E.3 Deferred from P3 (intentionally)

- **P3.1** `nal_length_size=4` hardcoded — hvcC's `lengthSizeMinusOne` can be 1/2/4 but real DV muxes (both MP4 and Matroska) always use 4. The code now has a comment explaining the parameter shape for a future hvcC-aware caller.
- **P3.11** `_validated_rpu_payload`'s `len(data) < 7` gate — reviewer classified as harmless; dovi_tool defers size validation similarly.

### E.4 Deferred from P2 (soft mitigations applied)

- **P2.4** MEL field validation breadth — only one MEL fixture. The soft mitigation landed: every `prepare_stream` DV probe now logs the full structured result at `LOGDEBUG`, so field testing can confirm whether real P7 MEL sources decode through fmp4 HLS. The in-code comment at the MEL branch explicitly notes "if field testing shows MEL also hangs, tighten this branch to match P8" — a one-line code change to restore the "any confirmed DV → matroska" behavior.
- **P2.3** `_validate_url` not called inside `_http_range` — would require moving `_validate_url` out of `stream_proxy.py` to avoid a circular import. Defense-in-depth only; current callers all validate upstream. CRLF stripping in the auth header DID land.
