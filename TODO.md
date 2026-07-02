# TODO - NZB-DAV Kodi Addon

Active backlog only. Completed work, old audit details, rejected designs, and long research notes live in git history.

Current addon version: see `repo/plugin.video.nzbdav/addon.xml`.

## Active Areas

Only two areas are active right now:

1. Close local/CI tooling gaps.
2. Keep a small bug-hunt seed list for the next focused review.

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
- NZBGet Smart Duplicates (#372) round 2 (several sub-items, from the PR #373 review):
  - **Follow the promoted backup.** The poll tracks only the pick's NZBID, so if NZBGet fails over to a backup (a different NZBID under the shared DupeKey) the current resolve reports the pick's failure instead of following the backup to completion. Track the DupeKey group / the newly-active NZBID rather than a single fixed NZBID. (Explicit DupeScores already made submission order-independent, so the prior fast-pick timing race no longer applies — a late backup after a SUCCESS is put into history as a backup, not deleted.)
  - **Cancel the backups on user-cancel.** The fire-and-forget backup worker has no stop signal and the cancel path only cancels the primary NZBID; a primary cancel/delete can make NZBGet promote a parked backup. Thread the submitted backup NZBIDs back to the cancel path.
  - **Reuse a promoted backup that succeeded.** The picker DL-tag/reuse fast path corroborates the PRIMARY only; a backup that completes after the primary fails is invisible to it. Tie into the poll-follow work above.
  - **Widen "qualifying" beyond exact same-name.** Include the same-content fallback identity and the loader-fetched NZBHydra deferred duplicate uploads (not just the picker's `filtered` rows); the release-scoped DupeKey already keeps distinct releases apart, so a same-content widening needs a content-scoped key variant.

## Backburner

- nzbdav-rs provider retry/timeout tuning. Revisit only if fallback telemetry shows backend/provider behavior is still the limiting factor.

## Not Doing

- CoreELEC-from-source builds or PANI/piXBMC source patching.
- `send_200_no_range` default-flip work; fallback switching supersedes this track.
- Strict-contract/density-breaker rollout gates unless fallback code produces a new reason to revisit them.
