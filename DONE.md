# DONE.md

Forward-looking pointer list: what's still not done, and the one still-useful deferred-items list from the 20-agent review. Completed work is in `git log` (PR-1 merge `16e7122`, review remediation `4103f5d`).

---

## Not Done Yet

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
