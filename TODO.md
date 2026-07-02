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
- NZBGet Smart Duplicates (#372) round 2 — SHIPPED (poll follows the DupeKey group, cancel cleans it up, succeeded members are reused, candidates widened). Remaining nuances worth a later pass: (a) the poll's group-follow uses a fixed `_PROMOTION_GRACE` (20s) window rather than reading NZBGet's hidden `Kind=DUP` history to know precisely when the group is exhausted; (b) `cancel_dupekey_group` is best-effort — a backup submitted in the tiny window between `cancel_event.set()` and the group delete can survive (parked, not downloading); (c) a SUCCESS reached via a promoted backup completes under the backup's own name, so the picker's exact-name DL-tag won't recognize it on a later replay (NZBGet's own dupe check still maps a re-submit onto it).

## Backburner

- nzbdav-rs provider retry/timeout tuning. Revisit only if fallback telemetry shows backend/provider behavior is still the limiting factor.

## Not Doing

- CoreELEC-from-source builds or PANI/piXBMC source patching.
- `send_200_no_range` default-flip work; fallback switching supersedes this track.
- Strict-contract/density-breaker rollout gates unless fallback code produces a new reason to revisit them.
