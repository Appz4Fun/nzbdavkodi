# Rust Migration Plan — nzbdavkodi → nzbdav-orchestrator + thin Kodi addon

Status: proposal, 2026-05-12. Author: review pass against current `main` (addon 1.0.8).

---

## 1. Goal

Move the service-shaped 90% of this codebase out of `plugin.video.nzbdav/` and into a Rust service, so the addon shrinks to a thin Kodi UX shim and the **peer-validated multi-NZB fallback pipeline** — the actual product — becomes testable, observable, and debuggable without driving a real Kodi-in-VNC.

Non-goals:
- Replacing NZBHydra2. It stays.
- Replacing nzbdav-rs. It stays.
- Rewriting Kodi UX in Rust. The picker, the settings dialogs, the `setResolvedUrl` handoff stay in Python.

---

## 2. What the app actually is (the model we're porting)

This isn't a player addon with a proxy bolted on. It's a **peer-validated multi-NZB fallback orchestrator**:

1. For a title, search returns N candidate NZBs across indexers.
2. At picker time (eager), the orchestrator:
   - Intersects article-ID sets across candidates to identify NZBs that **purport to be the same content**.
   - Submits the top candidates to nzbdav-rs and waits for them to reach "ready."
   - Compares WebDAV `Content-Length` across the ready peers.
   - Pulls byte samples at 2 / 100 / 4096-byte heads from each peer's WebDAV stream and compares them.
   - Only NZBs that pass all three gates enter the **validated peer pool** for the title.
3. The user picks one peer to play.
4. The stream proxy serves it through one of four tiers (direct / virtual-faststart / pass-through / force-remux).
5. **Mid-stream, on unrecoverable upstream error**, the proxy cuts over to the next validated peer at the equivalent byte offset. The byte-offset alignment is safe *because step 2 proved the peers are byte-identical at the WebDAV layer*.

Today nothing surfaces peer count, peer health, or cutover rate. That's the Cyclops-style health-check gap.

---

## 3. Target architecture

```
                           ┌─────────────────────────────────────────────┐
TMDBHelper ──plugin://──▶  │  plugin.video.nzbdav  (Python, ~1.5–2 kLOC) │
                           │  ─ router.py (URL dispatch only)            │
                           │  ─ results_dialog.py                        │
                           │  ─ resolver.py (thin: hit /resolve → SRU)   │
                           │  ─ service.py (NzbdavPlayer playback monitor)│
                           │  ─ player_installer.py, settings.xml, i18n  │
                           │  ─ NO providers, NO filter, NO proxy        │
                           └────────────┬────────────────────────────────┘
                                        │ HTTP (JSON)
                                        │ localhost or LAN
                                        ▼
                           ┌─────────────────────────────────────────────┐
                           │  nzbdav-orchestrator  (Rust, new)           │
                           │  /search       → multi-indexer fan-out      │
                           │  /resolve      → submit + peer validation   │
                           │  /peers/<rid>  → validated peer pool        │
                           │  /stream/<sid> → 4-tier proxy + cutover     │
                           │  /health       → peer counts, cutover rate  │
                           │  /admin        → indexer config             │
                           └──┬──────────────┬──────────────────────┬────┘
                              │              │                      │
                              ▼              ▼                      ▼
                          NZBHydra2     nzbdav-rs              (Prowlarr,
                          (Newznab)     (SAB API + WebDAV)      direct NN)
```

**Where it runs.** Two viable shapes:

| Shape | Pros | Cons | Recommendation |
|---|---|---|---|
| **A: sidecar per Kodi box** (one binary per CoreELEC unit, like `tmdbhelper-warmup-rs`) | Localhost proxy URL preserved → keeps `Connection: close` discipline, no `PROPFIND` risk, no auth on the wire. Matches today's process boundary. | Each box has its own peer cache + telemetry. Updates have to land on every box. | **Start here.** |
| **B: split — central service + tiny on-box proxy** | Shared peer cache + telemetry across the household. One Komodo stack to update. | Cutover decisions cross the LAN. More moving parts. | Migrate to this in a later phase once shape A is stable, *if* peer cache sharing turns out to be valuable. |

Shape A is the precedent (`tmdbhelper-warmup-rs.service`). Ship that first, defer B until you have telemetry that says it's worth it.

---

## 4. Crate layout

Workspace at `nzbdavkodi/orchestrator/` — same repo as the addon, on branch `feat/rust-orchestrator`. The orchestrator binary is bundled into the addon zip at release time (see §6 Phase 0 for the CI wiring).

```
nzbdavkodi/                         (this repo)
├── plugin.video.nzbdav/            # existing Python addon (slimmed over phases)
├── orchestrator/                   # NEW: Cargo workspace
│   ├── Cargo.toml                  # workspace
│   ├── crates/
│   │   ├── orchestrator-server/    # axum app, route handlers, main()
│   │   ├── orchestrator-core/      # PeerPool, validation pipeline, cutover state
│   │   ├── orchestrator-providers/ # Hydra/Prowlarr/Newznab clients (XML)
│   │   ├── orchestrator-filter/    # PTT-equivalent parsing + ranking
│   │   ├── orchestrator-release-parser/ # thin wrapper around `torrent-name-parser`
│   │   │                            #  with per-rule overrides where Python PTT disagreed
│   │   ├── orchestrator-proxy/     # 4-tier HTTP serving (the big one)
│   │   ├── orchestrator-mp4/       # MP4 atom parser + virtual faststart
│   │   └── orchestrator-dv/        # DV RPU classifier (port of dv_rpu/dv_source)
│   └── tests/
│       ├── harness/                # docker-compose stack + scenarios (per §12)
│       ├── integration/            # no-Kodi end-to-end against fault-proxy
│       └── fixtures/               # imdb_top_50.json, recorded Hydra XML, etc.
└── tests/                          # existing Python tests (extreme/ shrinks per §12.6)
```

Release artefact: `plugin.video.nzbdav-X.Y.Z.zip` includes a `bin/orchestrator-aarch64-musl` (and `-armv7-musl`, `-x86_64-musl` for non-CoreELEC users). The addon's `service.py` selects the right binary at startup based on `platform.machine()`.

**Reused from the existing crate family:**
- `nzb-core` — if we want shared NZB XML types (low value here; orchestrator only needs article-ID extraction).
- `nzb-nntp` — only if we add a future "NNTP-side peer pre-check" tier (article HEAD probes before submitting). Not Phase-1.

**Not reused:** `rust-yenc-simd` (no decode), `rust_par2` (no repair), `nzb-decode`, `nzb-news`, `nzb-dispatch` — those are for downloading; we delegate downloading to nzbdav-rs.

**Third-party crates (matches the `tmdbhelper-warmup-rs` profile so cross-compile is already proven on aarch64-musl):**

| Concern | Crate |
|---|---|
| HTTP server | `axum` + `tower` |
| HTTP client | `reqwest` (rustls-tls) |
| Async runtime | `tokio` |
| XML (Newznab/NZB) | `quick-xml` |
| SQLite (indexer config, peer cache, telemetry) | `rusqlite` (bundled) — already used in warmup-rs |
| MP4 box parsing | hand-rolled (port of `mp4_parser.py`); 583 lines of pure logic |
| Release-name parsing (PTT replacement) | `torrent-name-parser` crate (or hand-port the vendored ptt/) |
| Logging | `tracing` + `tracing-subscriber` |
| Cross-compile | `aarch64-unknown-linux-musl`, same target triple as warmup-rs |

---

## 5. HTTP API surface (v1)

Designed so the Python addon is essentially a JSON-over-HTTP client.

```
POST /v1/search
  body: { imdb_id, title, year, kind: "movie"|"episode", season?, episode? }
  → 200 { search_id, candidates: [{nzb_url, indexer, size, title, age, ...}] }

POST /v1/resolve
  body: { search_id, selected_nzb_url, fallback_count: 3 }
  → 200 { resolve_id, primary_peer_id, peers: [{peer_id, validation_state}] }
  → streams validation progress over SSE on /v1/resolve/<resolve_id>/events

GET  /v1/peers/<resolve_id>
  → 200 { peers: [{peer_id, state: "validating"|"ready"|"failed", reason, content_length, sample_hash}] }

POST /v1/stream/prepare
  body: { resolve_id, peer_id, auth_header? }
  → 200 { stream_url: "http://127.0.0.1:NNNN/stream/<sid>", tier, mime, duration_seconds }

DELETE /v1/stream/<sid>      # called from NzbdavPlayer.onPlayBackStopped

GET  /v1/health
  → 200 {
      active_sessions, recent_cutovers_24h, peer_cache_size,
      provider_health: { hydra: "ok", prowlarr: "..." },
      data_failure: { zero_fill_24h_bytes, cutover_success_rate }
    }

GET  /v1/admin/indexers            # ── settings UI
POST /v1/admin/indexers
PUT  /v1/admin/indexers/<id>
DELETE /v1/admin/indexers/<id>
```

**Auth:** shared-secret bearer token. Generated on first run, persisted to `/storage/.kodi/userdata/addon_data/plugin.video.nzbdav/orchestrator.token`. The Python addon reads it from settings; the orchestrator reads it from a file written at install time. No user-visible auth UI.

**Stream URLs are localhost-only** in shape A. They never leave the box. This is what preserves the existing proxy semantics.

---

## 6. Migration phases

Each phase ends with the Python addon still working end-to-end. We're cutting over module-by-module behind a settings flag (`use_orchestrator: bool`), so a regression in any phase is one toggle away from the old code path.

### Phase 0 — Skeleton + deploy story (1 week)

**Deliverables:**
- Branch `feat/rust-orchestrator` cut from `main`.
- `orchestrator/` Cargo workspace at the repo root; `orchestrator-server` with `/v1/health` returning static JSON; emits the §11 envelope on every log line from day one.
- Update `.github/workflows/` (CI) and the release workflow: add `cargo fmt --check`, `cargo clippy -D warnings`, `cargo test`, cross-compile to aarch64-musl + armv7-musl + x86_64-musl, bundle binaries into the addon zip at release-tag time.
- Addon's `service.py` gains a small bootstrap that picks the binary by `platform.machine()`, drops it at `/storage/.kodi/userdata/addon_data/plugin.video.nzbdav/bin/orchestrator`, makes it executable, and spawns it as a child process (lifecycle tied to the addon service — when Kodi stops, the orchestrator stops).
- `use_orchestrator` settings.xml flag (default `false`).
- §12 harness Phase-0 slice lands: `orchestrator/tests/harness/crates/fault-proxy/` Rust port + `harness-runner/` skeleton + `golden_path.rs` scenario hitting the stub `/v1/health`.

**Exit criteria:** addon boots → spawns orchestrator → hits `/v1/health` → logs `orchestrator.call` with `outcome=ok`. `just harness-fast` passes the golden-path scenario. No functional addon change.

---

### Phase 1 — Search + filter (2 weeks)

Port:
- `hydra.py`, `prowlarr.py`, `direct_indexers.py` → `orchestrator-providers`
- `filter.py` (885 LOC) + vendored `ptt/` → `orchestrator-filter`
- `indexer_manager.py`, `indexer_store.py`, `indexer_presets.py` → `orchestrator-server` `/v1/admin/indexers`
- `newznab_caps.py`, `search_planner.py`

**Test corpus:** capture XML responses from your live Hydra2 against the IMDb Top-50 list. Replay them in `orchestrator-providers/tests/` as fixtures. Every release-name parsing case in today's `tests/test_filter.py` (there are many) ports to Rust.

**Behind the flag:** when `use_orchestrator=true`, `router.py`'s search dispatch hits `POST /v1/search` and renders the JSON. When `false`, the old Python path runs unchanged.

**Exit criteria:**
- `POST /v1/search` returns the same ranked candidate set as today's Python pipeline for every title in the corpus (allow for stable-sort tie-breaking differences, but content + order parity).
- Settings UI for indexers works through the new HTTP admin API.

---

### Phase 2 — Resolve + WebDAV probe (1 week)

Port:
- `nzbdav_api.py` → orchestrator client of nzbdav-rs.
- `webdav.py` → `orchestrator-core::webdav`.
- The polling state machine from `resolver.py` (the `_poll_once` / `_history_status_is_terminal` cluster).

**Still NOT ported in this phase:** peer validation, stream proxy. `POST /v1/resolve` returns a single-peer pool. Cutover is disabled.

**Exit criteria:** with `use_orchestrator=true`, playing a movie end-to-end works exactly like today (Python addon still owns proxy + dialog UX, just delegates resolve to Rust).

---

### Phase 3 — Peer validation pipeline (2 weeks) — **the new functionality**

This is the part that **doesn't exist today** in a first-class form. Build it from scratch in Rust:

- **Article-ID extraction.** Parse each candidate NZB's XML, extract the `<segments>/<segment>` article IDs into a sorted set per file.
- **Article-ID intersection.** For a chosen primary, score every other candidate by Jaccard similarity over its article-ID sets. Threshold + top-K become the "submit-and-probe" cohort.
- **Submit cohort.** Submit primary + cohort to nzbdav-rs in parallel. Track each as a peer.
- **Content-length gate.** Once a peer reaches "ready," fetch its WebDAV `Content-Length`. Peers whose length differs from the primary by more than ε are rejected.
- **Byte-sample gate.** For each surviving peer, fetch byte ranges `[0,2)`, `[0,100)`, `[0,4096)` over WebDAV and SHA256-hash each. Compare against the primary's hashes. Mismatches are rejected.
- **Persistence.** Validated peer pool persists to SQLite keyed by `(imdb_id, resolution, release_group, ...)` so repeat plays skip re-validation.
- **SSE progress.** `/v1/resolve/<rid>/events` streams `{peer_id, state, reason}` so the picker dialog can show "peer 2/3 validated" in real time.

**Tests:**
- Unit: feed crafted NZB XML, assert article-ID extraction.
- Integration: against the IMDb Top-50 corpus, assert ≥2 validated peers for ≥40 of 50 titles. The 10 misses become a data point for "do we need a wider candidate net?"
- Negative: feed two NZBs with deliberately disjoint article-IDs, assert second is rejected.

**Exit criteria:** picker dialog shows live "N peers validated" count. `/v1/health` reports peer-pool stats.

---

### Phase 4 — Stream proxy (3 weeks)

This is the biggest port: `stream_proxy.py` (7,290 LOC), `mp4_parser.py` (583 LOC), `dv_rpu.py` (449), `dv_source.py` (500). The good news is the architecture doc (`docs/proxy-architecture.md`) already describes the design with surgical precision — it's a port, not a redesign.

Per tier:
- **Tier 0 (direct redirect)** — trivial. Return the WebDAV URL directly when the file is already faststart and small.
- **Tier 1 (virtual MP4 faststart)** — port `mp4_parser.py`. Pure logic, no I/O concerns. Unit-testable with byte-fixture MP4s.
- **Tier 2 (pass-through + zero-fill recovery)** — port `_serve_proxy`. The 64 KB read chunk and `Connection: close` discipline aren't needed in Rust (no 32-bit address space), but keep `Connection: close` for now to preserve Kodi behavior parity. Validate that with `--variant=python-buffers` vs `--variant=rust-buffers` flag.
- **Tier 3 (force-remux matroska + HLS fmp4)** — spawn ffmpeg via `tokio::process::Command`. Port `HlsProducer` to a `tokio::sync::watch`-driven state machine for segment readiness gates.
- **DV classifier** — port `dv_rpu.py` and `dv_source.py`. Both pure logic with HTTP range probes; clean Rust ports.

**Tests:**
- Unit tests for tier selection, `Range` parsing, `mp4_parser` faststart rewrite, DV classification.
- Integration tests against a **fault-proxy** (port `tests/extreme/fault_proxy.py` to Rust, or reuse the Python one — both are fine; the fault-proxy is test scaffolding, not product code).
- Same test corpus as Python today: every fixture in `tests/test_stream_proxy.py` and `tests/test_mp4_parser.py` becomes a Rust integration test.

**Exit criteria:**
- All four tiers pass parity tests against the existing Python fixtures.
- 32-bit Kodi can still play a 58 GB file (the canonical force-remux case) through the new proxy. Validate on the actual UGOOS AM6B.
- `tests/extreme/` shrinks: the extreme suite can now point the addon at the Rust proxy and assert end-to-end play.

---

### Phase 5 — Cutover (1 week) — **the integration win**

With peer pool (Phase 3) and proxy (Phase 4) both in Rust and in the same process, the cutover logic becomes a clean addition:

- The proxy tracks `current_byte_pos` per session.
- On upstream unrecoverable error (today: zero-fill kicks in), the proxy instead calls `PeerPool::next_validated_peer(resolve_id)`.
- If a peer is available, switch the upstream WebDAV URL to the peer at `current_byte_pos` and continue streaming. **Safe because validation proved the peers are byte-identical at the bytes we've already served.**
- If no peer available, fall back to today's zero-fill recovery as the safety net.
- Cutover events emitted to `/v1/health` and to a `cutover.jsonl` event log.

**Tests:**
- The fault-proxy schedules `connection_reset` mid-stream.
- Assert: stream continues without Kodi-side decoder error. Cutover event logged.
- Telemetry assertion: `recent_cutovers_24h` increments.

**Exit criteria:**
- A scripted IMDb Top-50 smoke run where the fault-proxy injects 1 mid-stream RST per title yields 100% successful playback (today's expected behavior would be ~0%).

---

### Phase 6 — Strip the Python addon (1 week)

Now that every service-shaped module is duplicated in Rust and `use_orchestrator=true` works end-to-end:

- Flip default to `use_orchestrator=true` for one release. Watch field telemetry.
- Next release: delete the old Python code paths.

What stays in Python (target ~1.5–2 kLOC):
- `router.py` — slim to URL dispatch only.
- `resolver.py` — slim to "hit `/v1/resolve`, render dialog, call `/v1/stream/prepare`, `setResolvedUrl`."
- `service.py` — boots the orchestrator binary, runs `NzbdavPlayer` playback monitor.
- `results_dialog.py`, `cache_prompt.py`, `player_installer.py`, `script_player.py`, `i18n.py`, `kodi_advancedsettings.py`, `settings.xml`.

What's deleted from Python:
- `stream_proxy.py`, `mp4_parser.py`, `dv_rpu.py`, `dv_source.py` (~8.8 kLOC).
- `fallback_streams.py` (~1.75 kLOC).
- `nzbdav_api.py`, `webdav.py` (~1.46 kLOC).
- `hydra.py`, `prowlarr.py`, `direct_indexers.py`, `filter.py`, `newznab_caps.py`, `indexer_*.py`, `search_planner.py`, `nzb_manifest.py` (~4.3 kLOC).
- Vendored `ptt/`.

---

## 7. Test strategy

Three tiers, replacing today's two:

| Tier | Where | Stack | Runtime | Purpose |
|---|---|---|---|---|
| **Unit (Rust)** | `crates/*/tests/` | `cargo test` | <5s | Pure-logic coverage. Filter parsing, MP4 atom rewrite, DV classification, peer scoring. |
| **Integration (no Kodi)** | `nzbdav-orchestrator/tests/integration/` | docker-compose: hydra2 + nzbdav-rs + fault-proxy + orchestrator | ~30s | Full peer-validation + cutover pipeline. **This is the new tier the user asked for.** Drives the HTTP API directly. |
| **Smoke (no Kodi)** | `nzbdav-orchestrator/tests/smoke/` | same + IMDb Top-50 corpus | ~5 min | The "huge amount of NZBs from Top 50" run. Asserts ≥2 validated peers per title, end-to-end stream, cutover survival under injected faults. |
| **Extreme (with Kodi)** | `tests/extreme/` (existing, drastically shrunk) | full stack + Xvfb + Kodi | ~5 min, one smoke title | One golden-path E2E: real Kodi plays real stream. Everything else moves up the pyramid. |

Today's `tests/extreme/` does the smoke-test job from inside a Kodi-in-VNC harness. After this migration, the smoke tier exists in pure HTTP and `tests/extreme/` shrinks to "Kodi is wired up correctly."

---

## 8. Observability / the health-check gap

The orchestrator emits structured logs + a Prometheus `/metrics` endpoint. Critical metrics:

| Metric | Type | Why it matters |
|---|---|---|
| `peer_pool_size{resolve_id}` | gauge | Did peer validation produce a useful pool? |
| `peer_validation_duration_seconds` | histogram | Is eager validation blocking the picker too long? |
| `peer_validation_rejected_total{reason="content_length"\|"byte_sample"\|"article_id"}` | counter | **The data-failure signal that's missing today.** |
| `stream_cutover_total{outcome="success"\|"no_peer"\|"failed"}` | counter | Is fallback actually working in the field? |
| `stream_zero_fill_bytes_total` | counter | Pre-cutover safety net usage. |
| `provider_request_duration_seconds{provider}` | histogram | Which indexer is slow. |
| `provider_error_total{provider, kind}` | counter | Which indexer is broken. |

Wire these to the existing Grafana board at `grafana.internal/d/api-latency` (or a new orchestrator board next to it).

---

## 9. Risks + mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Cross-compile to 32-bit Kodi platform required | Low — `tmdbhelper-warmup-rs` already targets aarch64-musl and works | Reuse its Woodpecker pipeline + target triple. |
| `ffmpeg` lifecycle bugs harder in Rust than Python | Medium | Port `HlsProducer` test fixtures one-for-one. The eager-spawn-and-poll-500ms pattern from `proxy-architecture.md` §C.5.4.2 carries over. |
| PTT parity (release-name parsing) | Medium | Audit `torrent-name-parser` crate against the vendored Python ptt. If it diverges, hand-port the rules we depend on; they're not that many. |
| Peer-validation false positives (two different cuts pass byte-sample but diverge later) | Low for REMUXes, real for re-encodes | Document that peer validation is conservative — only matches identical content (same release, same encoder pass). User-visible peer count is the honest signal. |
| Performance regression on 32-bit Kodi for tier 2 pass-through | Low | Rust + rustls + zero-copy `tokio::io::copy_bidirectional` is strictly faster than Python `urllib`. |
| Settings migration | Medium | Phase 1 ships a one-shot script that reads existing `settings.xml` and POSTs to `/v1/admin/indexers`. |

---

## 10. Decisions (locked 2026-05-12)

1. **Deployment shape: A — sidecar per Kodi box.** One Rust binary per CoreELEC unit, modelled on `tmdbhelper-warmup-rs.service`. Localhost proxy URL semantics preserved. Shape B (split central + thin proxy) is deferred indefinitely; revisit only if telemetry shows shared peer-cache value across multiple Kodi boxes.
2. **Repo: same repo, dedicated branch.** Orchestrator lives at `nzbdavkodi/orchestrator/` as a Cargo workspace alongside `plugin.video.nzbdav/`. Work happens on branch `feat/rust-orchestrator`, branched from `main`. Keeping it in-repo keeps version coupling tight (the addon and orchestrator ship as one release artefact) and means the `use_orchestrator` flag and the orchestrator binary version are atomically bumped together. CI gets a second pipeline that builds the Rust binary and bundles it into the addon zip.
3. **PTT replacement: `torrent-name-parser` crate first, hand-port the deltas.** Try the crates.io `torrent-name-parser` as a drop-in. Phase 1 includes a parity test that runs every fixture in `tests/test_filter.py` against both Python PTT and Rust `torrent-name-parser`. Any divergence becomes a documented patch list — small enough cases we hand-port (we maintain a thin wrapper crate `orchestrator-release-parser` that delegates to `torrent-name-parser` and overrides specific rules where Python PTT disagreed). This way the vendored Python PTT codebase doesn't have to live forever; the Rust side becomes the source of truth.
4. **Peer validation: unconditional, with a knob.** Runs on every resolve. New settings: `peer_validation_max_candidates: 3` (cohort size), `peer_validation_timeout_seconds: 180` (give up waiting for slow peers), `peer_validation_min_peers: 1` (proceed with playback even if only the primary validates — never block the user). Reasoning: the user said "I have like 4 ways to handle it" — validation is foundational, not optional; making it a feature flag invites a "turn it off when it breaks" failure mode that defeats the architecture.
5. **No baseline telemetry backport to Python.** Skipped. Phase 5 success is judged qualitatively ("did the scripted IMDb Top-50 fault-injection run survive?") rather than against a measured "before" cutover rate. Saves ~1 day, and the harness produces its own quantitative baseline as soon as Phase 5 ships.

---

## 11. Logging contracts at every layer boundary

The developer's stated problem isn't "the code is wrong" — it's "I can't tell *which* layer failed." So the orchestrator treats structured logging as a product feature, not an afterthought.

### 11.1 Rules

- **One event type per layer transition.** No free-form log lines on failure paths. Failures are events with the same schema as successes.
- **Stable JSON shape.** All events emit to stdout as one JSON object per line. Ingested into Loki via the existing Promtail sidecar on Node B (per workspace CLAUDE.md). Field names never change once shipped.
- **Correlation IDs threaded through every boundary.** Every event carries `request_id` (the inbound Kodi → orchestrator request), and where applicable `resolve_id`, `peer_id`, `session_id`. A single play is fully reconstructible by filtering on `resolve_id` in Loki.
- **`outcome` is mandatory.** Every event has `outcome` ∈ `{started, ok, error, timeout, rejected, skipped}`. No event is "informational only" — every event explicitly says what happened.
- **`reason` on every non-`ok` outcome.** Free-form human string, but the *list of possible reasons* is enumerated in the layer's docstring. New reasons are a code change, not an emergent string.
- **`duration_ms` on every span.** Even fast spans get it — comparing tail latencies post-deploy is how you spot regressions before they're outages.

### 11.2 The event catalogue

Each layer emits a small fixed set of events. Names are dotted, lowercase. `{...}` denotes per-event payload fields beyond the common envelope.

**Common envelope (every event):**
```json
{
  "ts": "2026-05-12T09:34:11.123Z",
  "level": "INFO|WARN|ERROR",
  "event": "search.candidate_returned",
  "request_id": "01J...",
  "resolve_id": "01J...|null",
  "peer_id": "01J...|null",
  "session_id": "01J...|null",
  "outcome": "ok|error|timeout|rejected|skipped|started",
  "reason": "...|null",
  "duration_ms": 123
}
```

**Layer 1 — Search (`orchestrator-providers`)**
| Event | Fields | Why |
|---|---|---|
| `search.requested` | `imdb_id`, `kind`, `season?`, `episode?`, `providers` | Did we get the search at all? |
| `search.provider_called` | `provider`, `url_redacted` | Which provider was contacted? |
| `search.provider_response` | `provider`, `http_status`, `candidate_count` | Did the provider answer? |
| `search.candidate_returned` | `total_candidates`, `per_provider` | What was the raw fan-in? |

Reasons enumerated: `provider_timeout`, `provider_http_error`, `provider_xml_invalid`, `provider_disabled`, `no_candidates_returned`.

**Layer 2 — Filter (`orchestrator-filter`)**
| Event | Fields | Why |
|---|---|---|
| `filter.applied` | `before`, `after`, `rules_applied` | How many got cut and by what rules? |
| `filter.candidate_rejected` | `nzb_id`, `reason`, `matched_rule` | **Per-candidate rejection log — single most useful event for "why is my movie not showing".** |

Reasons enumerated: `resolution_mismatch`, `codec_excluded`, `release_group_excluded`, `keyword_required_missing`, `keyword_excluded_present`, `size_below_min`, `size_above_max`, `ptt_parse_failed`.

**Layer 3 — Resolve / submit (`orchestrator-core::resolve`)**
| Event | Fields | Why |
|---|---|---|
| `resolve.started` | `resolve_id`, `selected_nzb_id`, `fallback_count_requested` | Beginning of the pipeline. |
| `submit.attempted` | `peer_id`, `nzb_url_redacted` | One per peer in the cohort. |
| `submit.accepted` | `peer_id`, `nzbdav_job_id` | nzbdav-rs took it. |
| `submit.rejected` | `peer_id`, `http_status`, `body_excerpt` | nzbdav-rs refused. |
| `poll.tick` | `peer_id`, `nzbdav_status`, `percent_complete` | Every poll iteration. Sampled at DEBUG. |
| `poll.terminal` | `peer_id`, `outcome=ok|failed|cancelled`, `final_status` | Job left the queue. |
| `webdav.probe` | `peer_id`, `http_status`, `content_length` | Stream came up. |

Reasons enumerated: `nzbdav_unauthenticated`, `nzbdav_disk_full`, `nzbdav_unparseable_nzb`, `nzbdav_no_video_file`, `poll_timeout`, `nntp_articles_missing`, `webdav_unreachable`.

**Layer 4 — Peer validation (`orchestrator-core::peers`)**
| Event | Fields | Why |
|---|---|---|
| `peer.validation_started` | `resolve_id`, `peer_id`, `gates=[article_id, content_length, byte_sample]` | What we're about to check. |
| `peer.gate_passed` | `peer_id`, `gate`, `evidence` | Per-gate success. `evidence` = e.g. `{jaccard: 0.97}`, `{content_length: 12345678}`, `{sample_hashes: [...]}` |
| `peer.gate_failed` | `peer_id`, `gate`, `expected`, `actual` | Per-gate failure with both values. |
| `peer.admitted` | `peer_id`, `total_peers_in_pool` | Made it into the validated pool. |
| `peer.rejected` | `peer_id`, `failed_gate`, `reason` | Did not make it. |

Reasons enumerated: `article_id_jaccard_below_threshold`, `content_length_mismatch`, `sample_hash_mismatch_at_offset_0`, `sample_hash_mismatch_at_offset_100`, `sample_hash_mismatch_at_offset_4096`, `webdav_fetch_failed_during_validation`.

**Layer 5 — Stream proxy (`orchestrator-proxy`)**
| Event | Fields | Why |
|---|---|---|
| `stream.prepare` | `session_id`, `resolve_id`, `peer_id`, `tier`, `content_length`, `mime` | Tier decision is logged with the inputs that drove it. |
| `stream.tier_selected` | `session_id`, `tier`, `reason` | Why this tier vs others. **Critical for "why was force-remux chosen?"** |
| `stream.range_request` | `session_id`, `range_start`, `range_end`, `current_byte_pos` | Sampled (every Nth) at DEBUG. |
| `stream.upstream_error` | `session_id`, `peer_id`, `http_status`, `byte_offset` | Pre-recovery state. |
| `stream.zero_fill_applied` | `session_id`, `gap_bytes`, `cumulative_zero_fill` | Safety-net recovery fired. |
| `stream.cutover_attempted` | `session_id`, `from_peer_id`, `to_peer_id`, `at_byte_offset` | The integration win. |
| `stream.cutover_succeeded` | `session_id`, `to_peer_id`, `resume_latency_ms` | Stream continued. |
| `stream.cutover_failed` | `session_id`, `reason` | All peers exhausted, or new peer also broke. |
| `stream.session_ended` | `session_id`, `bytes_served`, `cutover_count`, `zero_fill_total_bytes`, `end_reason` | Closing summary. |

Reasons enumerated: `tier_selected_due_to_size_threshold`, `tier_selected_due_to_mp4_already_faststart`, `tier_selected_due_to_dv_p7_fel`, `cutover_no_validated_peer`, `cutover_all_peers_exhausted`, `cutover_peer_byte_misalignment` (defensive — shouldn't happen if validation worked).

**Layer 6 — Kodi addon ↔ orchestrator boundary (Python side)**
The Python addon emits its own log lines tagged `NZB-DAV:` (existing convention) plus a **single mirror event** at each HTTP boundary:

| Event | Fields | Why |
|---|---|---|
| `orchestrator.call` | `endpoint`, `http_status`, `duration_ms` | One line per orchestrator call from Python. |
| `orchestrator.error` | `endpoint`, `error_class`, `error_message` | Bridge errors stay visible even if orchestrator's stdout isn't captured. |

This keeps `kodi.log` self-sufficient as a first stop for "what did the addon think happened?" while the orchestrator's full event stream lives in Loki.

### 11.3 Wire-level proof: every failure is quotable

After this is in place, every developer question becomes a Loki query:

| Question | Query |
|---|---|
| "Why is my movie not showing in the picker?" | `{app="orchestrator"} \|= "filter.candidate_rejected" \|= "<imdb_id>"` |
| "Why did peer 2 not validate?" | `{app="orchestrator"} \|= "peer.gate_failed" \|= "<peer_id>"` |
| "Did cutover fire on the last play?" | `{app="orchestrator"} \|= "stream.cutover_attempted" \| json \| resolve_id="<id>"` |
| "Which tier did the proxy pick and why?" | `{app="orchestrator"} \|= "stream.tier_selected" \| json \| session_id="<id>"` |
| "All failures in the last hour, grouped by reason." | `{app="orchestrator"} \| json \| outcome="error" \| count by (reason)` |

This is the missing-failure-mode problem solved at the source: layers don't fail silently because every transition is named.

### 11.4 Failure-path trace template

When a developer reports "playback broke," the on-call response is a single command:

```bash
just trace-resolve <resolve_id>
```

Which is:
```bash
logcli query "{app=\"orchestrator\"} | json | resolve_id=\"$1\"" --since=2h \
  --output=jsonl | jq -c 'select(.outcome != "ok")'
```

Output: the ordered list of all non-OK events for that play, no noise. Every reason field tells you which layer failed and why. **That's the deliverable** — failure paths are an enumerable list, not a forensic exercise.

---

## 12. Functional test harness (no Kodi)

The existing `tests/extreme/` + `tests/test_functional_fallback_playback.py` already implements the core ideas — search a real Hydra, submit to a real nzbdav-rs, content-length-compare fallback candidates, inject faults, watch playback, correlate events. It just runs all of that inside a Kodi-in-VNC harness, which makes it slow, brittle, and dependent on Xvfb / TMDBHelper dialog timings.

The new harness replicates **every capability** of the existing suite, but drives the orchestrator HTTP API directly and observes through structured logs instead of `Player.GetProperties`. Kodi is *not* in this harness.

### 12.1 Capability inventory — existing → new

| Existing piece | Lives in | What it does today | Replacement |
|---|---|---|---|
| `tests/extreme/fault_proxy.py` (534 LOC) — 5 fault types, scheduled events, JSONL event log | Python proxy container | Wraps nzbdav-rs WebDAV, injects connection_reset / http_500 / slow_upstream / truncated_response / corrupted_bytes at scheduled `at_seconds` offsets | **Port to Rust** as `nzbdav-orchestrator/tests/fault-proxy/`. Same control plane (`POST /control/schedule`), same JSONL events output. Same 5 fault types. |
| `tests/extreme/measurement.py::PlayerPoller` | Python | Polls `Player.GetProperties` every 250ms, writes `timeline.jsonl` | **Replaced by orchestrator log tail.** Subscribe to `stream.range_request` + `stream.session_ended` events; we already know byte position. No Kodi required. |
| `tests/extreme/measurement.py::correlate` | Python | Joins fault events to playback freezes/resumes, computes `resume_seconds` / `max_freeze_seconds` | **Reused, ported to Rust.** Joins fault-proxy events to `stream.cutover_*` events on `session_id` + wall clock. |
| `IMDB_TOP_50_MOVIES` corpus (in `test_functional_fallback_playback.py`) | Python list | 50 titles with IMDB IDs to test against | **Reused as-is.** Move to `nzbdav-orchestrator/tests/fixtures/imdb_top_50.json`. |
| `_submit_and_resolve_live_jobs` | Python | Submits primary + N fallback jobs to nzbdav-rs, content-length compares them | **Replaced by `POST /v1/resolve`** — the orchestrator's peer-validation pipeline IS this, but with the article-ID + byte-sample gates added. |
| `_movie_selections_with_fallbacks` / `_most_duplicated_group_pool` | Python | Picks a primary NZB + finds same-release-group fallback peers | **Replaced by orchestrator's peer-discovery layer.** The "most duplicated release group" heuristic becomes one input to the Jaccard article-ID intersect. |
| `EXTREME_FILTER_SETTINGS` (filter knobs) | Python dict | Sets up filter rules for the extreme run | **Becomes the body of `POST /v1/admin/indexers/settings`** for the test orchestrator instance. |
| `_post_schedule(events)` | Python | Schedules fault events on the fault-proxy | **Unchanged interface** — the new Rust fault-proxy speaks the same HTTP control API. |
| `_read_fallback_switch_count` (greps kodi.log for "Switched pass-through source") | Python | Counts cutovers from Kodi log lines | **Replaced by counting `stream.cutover_succeeded` events** in the orchestrator log stream. |
| `summary.json` / `summary.md` writers | Python | Per-run report | **Reused, ported.** Same JSON shape so reports stay diffable across the migration. |

### 12.2 Harness layout

```
nzbdav-orchestrator/tests/harness/
├── docker-compose.yml          # hydra2 + nzbdav-rs + fault-proxy + orchestrator
├── .env.example                # NNTP_USER, NNTP_PASS, HYDRA_URL, HYDRA_API_KEY, ...
├── fixtures/
│   ├── imdb_top_50.json
│   ├── filter_settings.json    # the EXTREME_FILTER_SETTINGS dict ported
│   └── hydra_caps_snapshot.xml # cached caps for offline-friendly subset
├── crates/
│   ├── fault-proxy/             # Rust port of tests/extreme/fault_proxy.py
│   └── harness-runner/          # the test driver binary
└── scenarios/
    ├── golden_path.rs           # one title, no faults, end-to-end stream
    ├── single_cutover.rs        # one fault at t=60s, assert cutover succeeded
    ├── extreme_fallback.rs      # 5 faults at 90/150/210/270/330s — the existing extreme test
    ├── all_peers_fail.rs        # negative: every peer's WebDAV killed, assert clean failure event
    ├── peer_validation_top50.rs # corpus run: how many of Top-50 yield ≥2 validated peers?
    └── tier_selection_matrix.rs # synthetic files at sizes/types to assert tier picks
```

### 12.3 Credential plumbing

The developer offered to share NNTP server + indexer creds. Those land in **Infisical's `apps` project** (per workspace CLAUDE.md) under a new namespace:

| Secret | Used by |
|---|---|
| `NZBDAVKODI_NNTP_HOST` | nzbdav-rs (downstream of orchestrator) |
| `NZBDAVKODI_NNTP_PORT` | nzbdav-rs |
| `NZBDAVKODI_NNTP_USER` | nzbdav-rs |
| `NZBDAVKODI_NNTP_PASS` | nzbdav-rs |
| `NZBDAVKODI_HYDRA_URL` | orchestrator + harness |
| `NZBDAVKODI_HYDRA_API_KEY` | orchestrator + harness |
| `NZBDAVKODI_PROWLARR_URL` | orchestrator (optional) |
| `NZBDAVKODI_PROWLARR_API_KEY` | orchestrator (optional) |

Local dev: `infisical run --env=prod -- just harness-test`. CI: same, with the Woodpecker secret-injection step. Never put creds in `.env.example` or fixtures — the existing `.env` pattern stays, but it's populated from Infisical, not hand-edited.

### 12.4 What the scenarios actually assert

For each scenario, success criteria are a **set of expected events in order**, not a final boolean. Example for `extreme_fallback.rs`:

```rust
// Drive
let resolve = orchestrator.post("/v1/resolve", &payload).await?;
let prepare = orchestrator.post("/v1/stream/prepare", &resolve.primary).await?;
fault_proxy.post("/control/schedule", &five_resets).await?;
let session = StreamClient::connect(&prepare.stream_url);
session.consume_for(Duration::from_secs(observe_window)).await?;

// Assert via the log stream — every assertion is an event match
log_assert! {
  in resolve.resolve_id:
    sequence [
      "resolve.started",
      at_least_n!(2, "submit.accepted"),
      at_least_n!(2, "peer.admitted"),
      "stream.tier_selected" where tier in ["passthrough", "force_remux_matroska"],
      at_least_n!(5, "stream.cutover_succeeded"),
      "stream.session_ended" where end_reason == "client_closed",
    ];
  forbid_events! [
    "stream.cutover_failed",
    "peer.gate_failed" where reason == "sample_hash_mismatch_at_offset_4096",
  ];
}
```

That's exactly the assertion the existing extreme test makes (`fallback_switch_count >= 5`) but expressed against the layer-boundary event stream instead of grepping `kodi.log`. The reason for the cutover, the tier chosen, the peer that was switched to — all visible by name.

### 12.5 Running modes

| Mode | Cmd | Stack | Time | Purpose |
|---|---|---|---|---|
| `just harness-unit` | cargo test | none | <5s | Pure logic — filter, MP4 parser, DV classifier, peer Jaccard. |
| `just harness-fast` | docker-compose up + cargo test --features fast | hydra2 + nzbdav-rs (mocked NNTP) + fault-proxy + orchestrator | ~60s | All scenarios except corpus. Uses recorded Hydra responses; no live indexer hit. |
| `just harness-live` | infisical run + cargo test --features live | full stack, real NNTP + Hydra | ~5min | All scenarios + 10 random titles from corpus. Used pre-release. |
| `just harness-corpus` | infisical run + cargo test --features corpus -- --nocapture | full stack + IMDB Top-50 | ~30min | Nightly. Asserts ≥2 validated peers per title for ≥40 / 50 titles. |
| `just harness-soak` | infisical run + cargo run --bin soak | full stack | hours | Long-running fault-injection loop, ported from `cinefile_fallback_loop.py`. |

### 12.6 What this kills from the existing extreme suite

After the harness is up:

- **Goes away entirely:** the Kodi-in-VNC container, the `addons_user_confirmed` fixture (clicking through dialogs), the `_dismiss_tmdbhelper_player_choosers` waiting on DialogSelect window id 12000, the `_wait_for_player` 10-minute timeout, all the JSON-RPC plumbing in `tests/extreme/conftest.py`.
- **Shrinks:** `tests/extreme/` reduces to one Kodi-in-VNC smoke test (golden path, one title, assert playback starts) — proof that the Python addon is correctly wired to the orchestrator. Everything else lives in the no-Kodi harness.
- **Stays:** the fault-proxy concept, the IMDB Top-50 corpus, the JSONL events output, the `summary.json` report shape. Those are good ideas; they just need a better host than Kodi-in-Xvfb.

### 12.7 Migration path for the harness (parallel to the Rust migration)

The harness doesn't have to wait for the full orchestrator port. We can build it incrementally alongside the phases:

| Phase | Harness deliverable |
|---|---|
| 0 (skeleton) | `fault-proxy/` Rust port + `harness-runner/` skeleton + `golden_path.rs` against a stub orchestrator (returns canned responses) |
| 1 (search+filter) | `peer_validation_top50.rs` running against real Hydra creds — but with stub resolve/stream layers |
| 2 (resolve) | `single_cutover.rs` end-to-end (cutover stubbed) |
| 3 (peer validation) | `peer_validation_top50.rs` becomes meaningful — assert validated peer counts |
| 4 (proxy) | `tier_selection_matrix.rs` against synthetic byte fixtures |
| 5 (cutover) | `extreme_fallback.rs` + `all_peers_fail.rs` — full parity with current extreme test |

By Phase 5 the new harness is strictly stronger than the existing one (faster, no Kodi, more granular assertions) and the old one can be retired except for the golden-path smoke.

---

## 13. Rough effort estimate

| Phase | Effort |
|---|---|
| 0 — skeleton + deploy | 1 wk |
| 1 — search + filter | 2 wk |
| 2 — resolve + webdav | 1 wk |
| 3 — peer validation | 2 wk |
| 4 — stream proxy | 3 wk |
| 5 — cutover | 1 wk |
| 6 — strip Python | 1 wk |
| **Total** | **~11 weeks** focused, more realistically 4 months around other work |

The big-ticket cost is Phase 4 (the proxy port). Everything else is conventional Rust service work with existing fixtures to port.

§11 (logging contracts) is implemented inline with each phase — every new layer must emit its named events before the phase exits. There is no "add observability later" step; that's how the failure-mode visibility problem stays solved.

§12 (functional test harness) is implemented in parallel with the phases per §12.7. The fault-proxy Rust port + golden-path scenario land in Phase 0 so we have a no-Kodi regression target from day one.
