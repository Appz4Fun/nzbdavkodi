# TODO - NZB-DAV Kodi Addon

Active backlog only. Completed work, old audit details, rejected designs, and long research notes live in git history.

Last reviewed: 2026-05-13
Current addon version in this checkout: 1.0.8

## Actual TODOs

Active areas:

1. Continue the Rust sidecar migration on branch `feat/rust-orchestrator`.
   Current status and next steps live in
   [`docs/rust-migration-plan.md`](docs/rust-migration-plan.md).

## Future Bug-Hunt Seeds

No current seeds. Add new entries here only after a focused review identifies a
concrete risk worth investigating.

## Backburner

- nzbdav-rs provider retry/timeout tuning. Revisit only if fallback telemetry shows backend/provider behavior is still the limiting factor.

## Not Doing

- CoreELEC-from-source builds or PANI/piXBMC source patching.
- `send_200_no_range` default-flip work; fallback switching supersedes this track.
- Strict-contract/density-breaker rollout gates unless fallback code produces a new reason to revisit them.
