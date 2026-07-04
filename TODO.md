# TODO - NZB-DAV Kodi Addon

Active backlog only. Completed work, old audit details, rejected designs, and long research notes live in git history.

Current addon version: see `repo/plugin.video.nzbdav/addon.xml`.

## Active Areas

Only three areas are active right now:

1. Land the complexity-campaign follow-ups (Post Fable TODO).
2. Close local/CI tooling gaps.
3. Keep a small bug-hunt seed list for the next focused review.

## Post Fable TODO

Follow-ups from the Lizard complexity campaign (PR #388, the #378-#387 rollup).
The shipped library is done (0 cloud Lizard findings); everything below is
merge logistics, tests, or scripts.

- Merge PR #388, then delete the seven stale superseded branches
  (`refactor/lizard-a1-fingerprint-cfg` … `a4`, `chore/lizard-reenable-cloud-gate`,
  `refactor/mega-simplicity-rollup`, `refactor/trim-resolver-fallback-nloc`).
- Sanity-check the first Codacy cloud analysis of `main` after the merge
  (expected clean: shipped lib gauges at 0 findings, and the two accepted
  CCN-9s sit below the cloud's >10 ccn threshold).
- C1 wave: consolidate test fixtures (43 Lizard parameter-count findings in
  `tests/`); then remove the module-level R0913 disables from
  `tests/test_resolver.py` and `tests/test_router.py`.
- C2 wave: split the giant test files (13 file-nloc findings;
  `tests/test_stream_proxy.py` alone is 12.3k lines).
- D wave: reduce the 7 Lizard findings in `scripts/`.
- De-flake the two load-sensitive picker-hint timing tests
  (`test_resolve_uses_picker_completed_job_hint_without_history_lookup`,
  `test_resolve_picker_completed_hint_skips_progress_dialog_startup_latency`)
  with the event-based pattern proven in PR #363 (block on a test-held event,
  assert return within a generous deadline, no wall-clock bounds).

## Tooling Gaps

- Add a local Python 3.10 + 3.12 test matrix, or document why CI-only is enough.
- Add a true Python 3.8 import/runtime check, or keep relying on `.pylintrc` `py-version=3.8`.

## Future Bug-Hunt Seeds

- `_retry_original_range` may retry already-written byte boundaries.
- `HlsProducer.prepare()` may accept a file before ffmpeg has fully flushed it.
- Force-quit during submit can orphan an nzbdav job.
- Metadata filters may be too permissive when PTT cannot parse a release title.
- WebDAV 401/403/5xx handling should stay typed and visible, not collapsed to "not found".
- Session/window-property races should be reviewed before larger concurrency changes.

## Backburner

- nzbdav-rs provider retry/timeout tuning. Revisit only if fallback telemetry shows backend/provider behavior is still the limiting factor.

## Not Doing

- CoreELEC-from-source builds or PANI/piXBMC source patching.
- `send_200_no_range` default-flip work; fallback switching supersedes this track.
- Strict-contract/density-breaker rollout gates unless fallback code produces a new reason to revisit them.
