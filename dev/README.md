# Dev stack — nzbdav + smoke harness

Minimal setup for validating the Kodi addon's backend logic end-to-end. No Kodi, no NZBHydra2, no indexer account. You supply an NZB file; the harness drives the addon's real Python against real nzbdav and gives you a VLC URL.

## What runs

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| nzbdav | `nzbdav/nzbdav:latest` | 3000 | SABnzbd-compat API + WebDAV |

Search via NZBHydra2 is intentionally dropped — for *stream* validation we only need the submit → download → serve path. Add it back later if you also want to exercise the search half of the addon.

## 1. Bring up

```bash
cd dev/
cp .env.example .env      # if you haven't already; edit as needed
docker compose up -d
```

First-run setup (browser, one-time):

1. Open **http://localhost:3000** → create admin user.
2. **Settings → Usenet → Add Provider** — use the creds in `.env`:
   - Host: `aunews.frugalusenet.com` · Port: `563` · SSL: on
   - User: `sprooty` · Password: (from `.env`)
   - Max connections: `4` · Type: Pool Connections
   - Hit **Test** before saving.
3. **Settings → SABnzbd** → copy the **API key**.
4. **Settings → WebDAV** → set a **username** and **password**.

## 2. Get an NZB

Drop `.nzb` files into `dev/nzbs/`. The harness will spin up a short-lived HTTP server on a random port to serve them to the nzbdav container (compose wires `host.docker.internal` so nzbdav can reach your host). Any source works:

- NZBgeek / NZBPlanet / DrunkenSlug (needs an account)
- Public test NZBs (e.g. Matroska sample downloads repackaged as NZB)
- A real download you've posted yourself
- The **Ronaldinho S01E03** sample committed in this repo under `dev/nzbs/` — a known-good 1.18 GB MKV. Runs against the frugalusenet creds in `.env` without needing your own indexer account.

## 3. Run the smoke test

Quick run against the committed sample (replace the creds with yours):

```bash
export NZBDAV_API_KEY=<settings → sabnzbd → api key>
export WEBDAV_USER=<settings → webdav → username>
export WEBDAV_PASS=<settings → webdav → password>

python3 dev/smoke.py dev/nzbs/Ronaldinho.The.One.and.Only.S01E03.*.nzb
```

Or by URL (no local HTTP server needed):

```bash
python3 dev/smoke.py --nzb-url https://example.com/foo.nzb
```

On success, the script prints a `=== STREAM READY ===` block and also writes the URL to `dev/last-stream.url` and `dev/last-stream.m3u` (both gitignored — they contain Basic-auth credentials).

### Playing in VLC

The URLs contain Basic auth credentials, so copy/paste across chat/terminals can mangle them (newlines in the middle of a long URL break things). Two safe options:

- Double-click `dev/last-stream.m3u` — opens in VLC, no copy/paste.
- In VLC: `Media → Open Network Stream…` (Ctrl+N), paste the URL from `dev/last-stream.url`.

What it does, step by step:

1. Mocks only the Kodi runtime modules (`xbmc`, `xbmcgui`, `xbmcplugin`, `xbmcaddon`, `xbmcvfs`) — exactly as `tests/conftest.py` does
2. Stubs `xbmcaddon.Addon().getSetting(...)` with your CLI flags / env vars
3. Serves your NZB on a local HTTP port (if you passed a file)
4. Calls the addon's real `submit_nzb()` → real `addurl` request to nzbdav
5. Polls the addon's real `get_job_history()` / `get_job_status()` until the job is Completed or Failed
6. Resolves the stream with the addon's real `find_video_file()` (WebDAV PROPFIND) + `get_webdav_stream_url_for_path()`
7. Prints the WebDAV URL and a ready-to-run VLC command

Everything inside the addon's `nzbdav_api.py`, `webdav.py`, and resolver helpers runs for real against the live container — no mocks on the network path.

## 4. Play in VLC

When the script prints `=== STREAM READY ===`, copy the `vlc ...` command. Two forms are shown:

- URL with credentials embedded (easy copy/paste)
- VLC's `--http-user` / `--http-password` form (safer, no creds in URL)

## What this covers vs skips

**Real code exercised**
- `resources/lib/nzbdav_api.py` — submit, history, queue, timeout handling
- `resources/lib/webdav.py` — PROPFIND recursion, auth headers, URL building
- `resources/lib/resolver.py::_storage_to_webdav_path` — path translation
- All the HTTP plumbing in `http_util.py`

**Skipped**
- `hydra.py`, `filter.py` (PTT parsing), `results_dialog.py` — search half, intentionally out of scope here
- `stream_proxy.py` — Kodi's 32-bit MP4 workaround; VLC doesn't need it. Covered separately by `just test-integration`
- Kodi UI (dialogs, ListItem mime types, setResolvedUrl) — not validatable without Kodi

## Tear-down

```bash
docker compose down         # stop
rm -rf data/                # wipe nzbdav config/state
```
