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
post-merge checks, tests, or scripts.

- Sanity-check the first Codacy cloud analysis of `main` after the #388 merge
  (expected clean: shipped lib gauges at 0 findings, and the two accepted
  CCN-9s sit below the cloud's >10 ccn threshold).
- C1 wave: consolidate test fixtures (43 Lizard parameter-count findings in
  `tests/`); then remove the module-level
  `too-many-arguments,too-many-positional-arguments` pylint disables from
  `tests/test_resolver.py` and `tests/test_router.py`.
- C2 wave: split the giant test files (13 file-nloc findings;
  `tests/test_stream_proxy.py` alone is ~16.7k lines).
- D wave: reduce the 7 Lizard findings in `scripts/`.
- De-flake the two load-sensitive picker-hint timing tests
  (`test_resolve_uses_picker_completed_job_hint_without_history_lookup`,
  `test_resolve_picker_completed_hint_skips_progress_dialog_startup_latency`)
  with the event-based pattern proven in PR #363 (block on a test-held event,
  assert return within a generous deadline, no wall-clock bounds).

## Tooling Gaps

- Tests run only on Python 3.14 (locally and in CI). Add a 3.10 + 3.12 test
  matrix, or document why 3.14 plus the 3.8 floor gates is enough.
- The 3.8 floor is checked at parse level only (`vermin` in `just lint`, plus
  `compileall` in `just compat-3-8` and the CI `compat-3-8` job). Add a true
  Python 3.8 import/runtime check, or document why parse-level is enough.

## Future Bug-Hunt Seeds

- `_retry_original_range` (`stream_proxy_handler_proxyserve2.py`) may retry already-written byte boundaries.
- `HlsProducer.prepare()` (`stream_proxy_hls_ffmpeg.py`) may accept a file before ffmpeg has fully flushed it (it only checks that `init.mp4` and `seg_000000.m4s` exist).
- Force-quit during submit can orphan an nzbdav job.
- Metadata filters may be too permissive when PTT cannot parse a release title.
- WebDAV 401/403/5xx handling should stay typed and visible, not collapsed to "not found".
- Session/window-property races should be reviewed before larger concurrency changes.
- NZBGet Smart Duplicates (#372): the poll's group-follow still decides the
  DupeKey group is exhausted with a fixed time window (`_PROMOTION_GRACE`, 20 s,
  in `nzbget_resolver.py`; the `DELETED/COPY` veto path uses the shorter
  `_COPY_VETO_GRACE`) instead of reading NZBGet's hidden `Kind=DUP` history to
  know precisely when no promotion can follow.

## Backburner

- nzbdav-rs provider retry/timeout tuning. Revisit only if fallback telemetry shows backend/provider behavior is still the limiting factor.

## Not Doing

- CoreELEC-from-source builds or PANI/piXBMC source patching.
- `send_200_no_range` default-flip work; fallback switching supersedes this track.
- Strict-contract/density-breaker rollout gates unless fallback code produces a new reason to revisit them.
