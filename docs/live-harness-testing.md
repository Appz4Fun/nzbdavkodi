# Live Harness Testing Status

Status date: 2026-05-14

This document tracks the live Docker Compose tests that still need to be run
after the `harness: add no-kodi live extreme runner` milestone. The indexer
quota issue is now resolved, but the last Codex attempt could not execute the
stack because the current sandbox could not access `/var/run/docker.sock`.

## Already Verified Locally

These non-live checks passed after the no-Kodi live extreme harness was added:

```bash
just lint
just test
python3 -m py_compile orchestrator/tests/harness/live/test_runner/live_common.py orchestrator/tests/harness/live/test_runner/test_live_extreme.py
ruff format --check tests/test_tooling_scripts.py tests/test_orchestrator_sse_integration.py orchestrator/tests/harness/live/test_runner/live_common.py orchestrator/tests/harness/live/test_runner/test_live_extreme.py
ruff check tests/test_tooling_scripts.py tests/test_orchestrator_sse_integration.py orchestrator/tests/harness/live/test_runner/live_common.py orchestrator/tests/harness/live/test_runner/test_live_extreme.py
git diff --check
```

`just test` result: `1427 passed, 5 skipped, 12 deselected`.

## Blocked In Current Codex Session

The live harness did not reach pytest in the current session. The attempts
failed before the stack could run tests:

```bash
just harness-live-extreme
```

First failure:

```text
failed to update builder last activity time: open /home/sprooty/.docker/buildx/activity/.tmp-...: read-only file system
```

Retrying with Docker config redirected to `/tmp` avoided the read-only home
write, but Docker itself was unavailable:

```bash
DOCKER_CONFIG=/tmp/nzbdav-docker-config BUILDX_CONFIG=/tmp/nzbdav-buildx just harness-live-extreme
```

Second failure:

```text
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
```

Run the outstanding tests from a normal shell with Docker socket access.

## Prerequisites

Before running the live tests:

- `.env` exists at the repo root with live Hydra, nzbdav-rs, WebDAV, and NNTP
  credentials.
- Docker can access `/var/run/docker.sock`.
- The Hydra2 volume already contains the configured Newznab indexer.
- The Newznab indexer quota has enough remaining hits for the search corpus.
- No old live stack is still running, or it is intentionally being reused.

Useful preflight:

```bash
docker compose version
docker ps
just harness-live-down
```

`just harness-live-down` preserves Hydra2 and nzbdav-rs volumes.

## Outstanding Required Runs

### 1. No-Kodi Live Extreme Search/Report

Run:

```bash
LIVE_EXTREME_SAMPLE_SIZE=3 just harness-live-extreme
```

Expected result:

- Docker builds `test-runner`.
- The stack starts `hydra2`, `nzbdav-rs`, and `orchestrator`.
- `test_live_extreme_search_corpus_reports_candidates` passes when at least
  one sampled IMDb title returns candidates.
- `test_live_extreme_resolve_validates_candidate_peers` is skipped unless
  `LIVE_EXTREME_FULL_RESOLVE=1`.
- A report is written to `docs/reports/live-harness-*/`.

Review:

```bash
latest="$(find docs/reports -maxdepth 1 -type d -name 'live-harness-*' | sort | tail -1)"
sed -n '1,160p' "$latest/summary.md"
sed -n '1,220p' "$latest/summary.json"
```

If the search test skips instead of passing, inspect `search_results.json`.
Provider outcomes are recorded there. A skip with zero candidates means the
live provider returned no usable results; it is not enough to validate the
post-quota path.

### 2. Existing Fast Live Harness

Run:

```bash
just harness-live-test
```

Expected result:

- `test_health` passes.
- `test_hydra_search_returns_candidates` passes.
- `test_hydra_search_candidate_shape` passes.
- `test_hydra_search_includes_large_releases` passes.
- `test_full_resolve_returns_stream_url` remains skipped unless
  `LIVE_FULL_RESOLVE=1`.

This run verifies the older fast live harness now works with the renewed
indexer quota.

## Optional Full Resolve Runs

These tests submit real NZBs and can take tens of minutes. Run them only when
you want to spend provider/API/Usenet resources.

No-Kodi extreme resolve and peer validation:

```bash
LIVE_EXTREME_SAMPLE_SIZE=1 LIVE_EXTREME_FULL_RESOLVE=1 just harness-live-extreme
```

Expected result:

- Search finds usable candidates.
- Resolve returns `stream_url`.
- Resolve returns peer metadata.
- At least `LIVE_EXTREME_MIN_VALIDATED_PEERS` peers reach
  `byte_sample_validated_phase_3`.
- `resolve_events.jsonl` includes `resolve.completed`.

Existing full live resolve:

```bash
LIVE_FULL_RESOLVE=1 just harness-live-test
```

Expected result:

- The top Hydra candidate is submitted to nzbdav-rs.
- The orchestrator waits for completion.
- The returned `stream_url` serves bytes.

## Failure Triage

If a live run fails, keep the stack up for inspection. The recipes use `&&`, so
the stack usually remains running after a failed test.

Collect service logs:

```bash
cd orchestrator/tests/harness/live
docker compose --env-file ../../../../.env logs --no-color hydra2 orchestrator nzbdav-rs test-runner
```

Check the latest no-Kodi report:

```bash
latest="$(find ../../../../docs/reports -maxdepth 1 -type d -name 'live-harness-*' | sort | tail -1)"
sed -n '1,240p' "$latest/search_results.json"
```

Common interpretations:

- `Daily Hits Limit Reached`: indexer quota is exhausted again.
- Zero candidates with no provider error: Hydra returned HTTP 200 but no usable
  releases for the sampled title.
- `test-runner` cannot reach `hydra2`: verify `test-runner.depends_on.hydra2`
  is present and Hydra startup completed.
- `nzbdav-rs` seed fails: verify `NNTP_*`, `NZBDAV_API_KEY`,
  `WEBDAV_USERNAME`, and `WEBDAV_PASSWORD` in `.env`.

Clean up when done:

```bash
just harness-live-down
```

Use `just harness-live-reset` only when intentionally deleting all live harness
volumes and reinitializing Hydra2 from scratch.
