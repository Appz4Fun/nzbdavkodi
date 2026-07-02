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
- NZBGet Smart Duplicates (#372) round 2 — SHIPPED (poll follows the DupeKey group, cancel cleans it up, succeeded members are reused, candidates widened). Round-2 review hardening also landed: the promotion scan excludes the just-failed NZBID (NZBGet's non-atomic queue→history overlap no longer re-selects the failed pick as its own promotion and hangs the poll); the cancel path re-sweeps the group once the worker drains (closes the in-flight-append-survives-cancel race); and the backup widening builds its loader with the thread-safe `_get_script_setting` so the worker never calls `xbmcaddon.getSetting` off-thread. Remaining nuances worth a later pass: (a) the poll's group-follow uses a fixed `_PROMOTION_GRACE` (20s) window rather than reading NZBGet's hidden `Kind=DUP` history to know precisely when the group is exhausted; (b) a SUCCESS reached via a promoted backup completes under the backup's own name, so the picker's exact-name DL-tag won't recognize it on a later replay (NZBGet's own dupe check still maps a re-submit onto it); (c) the loader-widened backups only submit when the picker already has ≥1 same-name backup — when NZBHydra collapses every mirror into one picker row (no same-name backup) the dupe submission is skipped, so the widened pool is dead in exactly the Hydra-collapse case; allowing a loader-only submission needs care because it would give a single-result pick a DupeKey and change its dedup/replay behavior; (d) `history_success_by_dupekey` can return a stale prior SUCCESS whose files were cleaned, so a failover-follow could probe a gone `dest_dir` and report "No video file found" instead of waiting for the current set to recover.

## Backburner

- nzbdav-rs provider retry/timeout tuning. Revisit only if fallback telemetry shows backend/provider behavior is still the limiting factor.

## Not Doing

- CoreELEC-from-source builds or PANI/piXBMC source patching.
- `send_200_no_range` default-flip work; fallback switching supersedes this track.
- Strict-contract/density-breaker rollout gates unless fallback code produces a new reason to revisit them.
