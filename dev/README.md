# Dev stack — nzbdav-rs + smoke harness

Self-contained dev stack for validating the Kodi addon against the **nzbdav-rs** backend (Rust rewrite, local checkout). No Kodi runtime needed; drop an NZB, run one Python command, play the resulting URL in VLC.

## What runs

| Service | Source | Port | Purpose |
|---------|--------|------|---------|
| nzbdav-rs | built from `../../../../apps/nzbdav-rs` | 8080 | SABnzbd-compat API + WebDAV |

Upstream `nzbdav/nzbdav:latest` is deliberately swapped out on this branch so the addon's backend-integration surface exercises the Rust port.

Differences vs upstream worth remembering:

- **Port**: container listens on `8080`; host-published on `NZBDAV_PORT` (default `8180`; upstream nzbdav was `3000`)
- **Env-driven setup**: `NZBDAV_API_KEY`, `NZBDAV_WEBDAV_USER`, `NZBDAV_WEBDAV_PASS` apply at container start — no web-UI click-through for those
- **`storage` shape in SABnzbd history**: returned as a `/content/...` path directly (vs upstream's `/mnt/nzbdav/completed-symlinks/...`). The addon's `_storage_to_webdav_path()` handles both.
- **Usenet provider**: must still be configured in the web UI Servers tab on first run (not env-driven)

## 1. Bring up

```bash
cd dev/
cp .env.example .env         # first time
docker compose up -d --build
```

First build takes ~3–8 minutes (Rust release build). Subsequent builds are cached.

On boot:

1. Open **http://localhost:8180/ui**
2. **Servers tab → Add server** — use the creds in `.env`:
   - Host: `aunews.frugalusenet.com` · Port: `563` · SSL: on
   - User: `sprooty` · Password: (from `.env`)
   - Max connections: `4`

API key + WebDAV creds are already set via env — no UI step for those. Defaults (change in `.env` if you want):

| Setting | Default |
|---------|---------|
| API key | `smokekey-dev-only` |
| WebDAV user | `admin` |
| WebDAV pass | `devpass` |

## 2. Run the smoke test

The committed sample NZB (`dev/nzbs/Ronaldinho.*.nzb`) is known-good against the frugalusenet provider.

```bash
# Source .env so NZBDAV_API_KEY / WEBDAV_USER / WEBDAV_PASS are in env:
set -a; source dev/.env; set +a

python3 dev/smoke.py dev/nzbs/Ronaldinho.The.One.and.Only.S01E03.*.nzb
```

Or by URL:

```bash
python3 dev/smoke.py --nzb-url https://example.com/foo.nzb
```

What the harness does:

1. Mocks only the Kodi runtime modules (`xbmc*`) — same as `tests/conftest.py`
2. Stubs `xbmcaddon.Addon().getSetting(...)` with CLI flags / env vars
3. Spins up a short-lived HTTP server serving your NZB file (nzbdav-rs fetches via `host.docker.internal`; compose wires the host-gateway)
4. Calls the addon's real `submit_nzb()` → real `addurl` request to nzbdav-rs
5. Polls `get_job_history()` / `get_job_status()` — uses the addon's real code
6. Resolves via the addon's real `find_video_file()` + `_storage_to_webdav_path()` + `get_webdav_stream_url_for_path()`
7. Writes URL to `dev/last-stream.url` + `dev/last-stream.m3u` (both gitignored — contain Basic-auth creds)
8. Prints a ready-to-run VLC command

## 3. Play in VLC

Two copy-safe options (URLs are long; chat/terminal wrap breaks them):

- Double-click `dev/last-stream.m3u` — opens in VLC, no paste.
- VLC → `Media → Open Network Stream` (Ctrl+N) → paste the URL from `dev/last-stream.url`.

## What this covers vs skips

**Real code exercised** (against real nzbdav-rs)
- `resources/lib/nzbdav_api.py` — submit, history, queue, timeout handling
- `resources/lib/webdav.py` — PROPFIND recursion, auth headers, URL building
- `resources/lib/resolver.py::_storage_to_webdav_path` — path translation (both upstream + nzbdav-rs shapes)
- All the HTTP plumbing in `http_util.py`

**Skipped**
- `hydra.py`, `filter.py`, `results_dialog.py` — search half (out of scope for stream validation)
- `stream_proxy.py` — Kodi 32-bit MP4 workaround; VLC doesn't need it. Covered separately by `just test-integration`
- Kodi UI surfaces (dialogs, ListItem mime types, `setResolvedUrl`)

## Iterating on nzbdav-rs

```bash
cd ../../../../apps/nzbdav-rs
# make changes, then:
cd -
docker compose up -d --build     # dev/ is cwd
```

The compose build context points at the nzbdav-rs checkout, so each `--build` picks up your local changes. First build is slow; incremental rebuilds reuse the cargo layer cache.

## Tear-down

```bash
docker compose down
# Container writes /data as root; use a helper container to wipe:
docker run --rm -v "$(pwd)/data:/d" alpine:3.21 sh -c 'rm -rf /d/*'
```
