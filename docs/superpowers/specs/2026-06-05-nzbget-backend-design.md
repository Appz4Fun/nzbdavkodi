# NZBGet streaming backend — design

**Date:** 2026-06-05
**Status:** Approved (brainstorm), pending implementation plan
**Branch context:** authored on `fix/proxy-fallback-resilience`

## Summary

Add an alternative download-and-playback backend to the `plugin.video.nzbdav`
Kodi addon. Today, selecting a release in the picker submits the NZB *URL* to
nzbdav's SABnzbd-compatible `addurl` API, polls nzbdav for job status with a
`DialogProgress` bar, and streams the finished file through the local
WebDAV-backed stream proxy.

This feature adds a **flip**: when enabled, the same picker selection instead
submits the NZB to a configured **NZBGet** instance, waits (with a progress
bar) for NZBGet to finish downloading *and* post-processing, then plays the
resulting file directly from an **SMB share** that maps onto NZBGet's
completed-downloads folder. Kodi plays the `smb://` URL natively — the WebDAV
stream proxy is not involved on this path.

All NZBGet configuration lives in a new **NZBGet** tab in addon settings.

## Decisions (from brainstorm)

1. **The flip is a global setting toggle** (`nzbget_enabled`). When true, every
   play routes through NZBGet. nzbdav/WebDAV/search settings are untouched and
   still used for searching — NZBGet only replaces the download + playback
   backend.
2. **SMB mapping:** the configured SMB root *is* NZBGet's completed dir. We take
   the per-release subfolder NZBGet reports in `DestDir`, append it to the SMB
   root, list it over SMB, and pick the largest video file.
3. **Completion = full post-processing.** Wait until NZBGet reports the job in
   history with a terminal `SUCCESS` status (par2 repair + unrar done), then
   resolve the file. Guarantees a repaired, unpacked, playable file.
4. **Submit method:** the addon fetches the NZB bytes itself and uploads them to
   NZBGet base64-encoded via `append` (works even if NZBGet can't reach the
   indexer).
5. **Integration approach A:** separate NZBGet modules; `resolve_and_play`
   branches at the entry point. No changes to the nzbdav streaming, fallback,
   dead-candidate, or WebDAV machinery.
6. **Progress UI:** full-screen `DialogProgress` with a cancel button, matching
   the current nzbdav resolve flow.
7. **SMB credentials** are embedded directly in the `smb://user:pass@host/...`
   root URL (single text setting), not split into separate fields.
8. **On explicit cancel:** delete the NZBGet job *and* its files
   (`editqueue`/`GroupDelete`). On timeout: leave the job running.

## Architecture

Integration is **Approach A — separate modules, branch at the entry point**:

- New `resources/lib/nzbget_api.py` — JSON-RPC client.
- New `resources/lib/nzbget_resolver.py` — submit → poll → resolve-over-SMB →
  `setResolvedUrl`, plus the pure SMB path-mapping function.
- `resources/lib/resolver.py` — one small branch near the top of
  `resolve_and_play`: if `nzbget_enabled`, delegate to
  `nzbget_resolver.resolve_and_play_nzbget(...)` and return. Everything else is
  unchanged.
- `resources/lib/router.py` — route `test_nzbget` and `test_nzbget_smb` actions.
- `resources/settings.xml` + i18n strings — new NZBGet category.

Rationale: the NZBGet flow (whole-file, SMB, post-processing wait) is
semantically different enough from nzbdav's streaming/fallback model that
forcing both through one code path adds coupling without payoff. Isolation also
means the battle-tested nzbdav path cannot regress — the new code is almost
entirely additive.

## Components

### 1. Settings (new "NZBGet" tab)

New `<category>` in `settings.xml` with new i18n label strings:

- `nzbget_enabled` (bool, default `false`) — the flip.
- `nzbget_url` (text, default `http://localhost:6789`) — NZBGet host.
- `nzbget_username` / `nzbget_password` (text, password `option="hidden"`) —
  NZBGet control credentials (JSON-RPC uses HTTP Basic auth).
- `nzbget_category` (text, optional) — category to tag submissions with.
- `Test NZBGet` action → `RunPlugin(plugin://plugin.video.nzbdav/test_nzbget)`.
- `nzbget_smb_root` (text, e.g. `smb://user:pass@host/completed`) — SMB base
  mapping onto NZBGet's completed dir. Credentials embedded in the URL.
- `Test SMB` action → `RunPlugin(plugin://plugin.video.nzbdav/test_nzbget_smb)`.

### 2. NZBGet API client (`nzbget_api.py`)

Pure-Python JSON-RPC 2.0 client mirroring `nzbdav_api.py`'s conventions:
settings-getter injectable for tests, redacted logging, `(value, error)` tuple
returns. RPC endpoint `<nzbget_url>/jsonrpc`, HTTP Basic auth from settings.

Methods:

- **`append`** — submit. Args: `NZBFilename` (release name + `.nzb`), `Content`
  (base64 of fetched NZB bytes), `Category` (from settings), `Priority`,
  `DupeMode`. Returns **NZBID** (`int > 0`) on success; `0`/negative on failure.
  NZBID is the lifecycle handle.
- **`listgroups`** — poll queue/active. Per-job `Status` (`QUEUED`,
  `DOWNLOADING`, `PP_*`/`POST_PROCESSING`, ...), `DownloadedSizeMB` /
  `FileSizeMB` for progress %, `NZBID`.
- **`history`** — poll completion. Terminal `Status` (`SUCCESS/*`,
  `FAILURE/*`, `WARNING/*`), `DestDir` (server-local final folder), `Name`,
  `NZBID`.
- **`version` / `status`** — connectivity test.
- **`editqueue`** (`GroupDelete` / `HistoryDelete`) — cancel/cleanup.

NZB-byte fetch reuses the existing fetch path
(`nzbdav_api._dump_submitted_nzb` already downloads NZB bodies); the shared
fetch helper is extracted rather than duplicated (AGENTS.md: don't duplicate
HTTP helpers).

**Lifecycle:** `append` → NZBID → poll `listgroups` until the NZBID leaves the
queue → poll `history` until the NZBID shows a terminal status → on `SUCCESS`
take `DestDir`. Failure/warning aborts with a notification.

### 3. Resolver branch & progress (`nzbget_resolver.py`)

`resolve_and_play_nzbget(handle, params, ...)` honors the same
**`setResolvedUrl`-on-failure contract**: exactly one `setResolvedUrl(handle,
True/False, li)` per exit, whole body wrapped so any unexpected raise still
resolves `False` (AGENTS.md invariant).

Flow:

1. Read NZBGet + SMB settings; missing/blank → notify + resolve `False`.
2. Fetch NZB bytes from `params["nzburl"]`, base64-encode, `append`.
   NZBID ≤ 0 → notify + resolve `False`.
3. Open `DialogProgress` with cancel. Poll loop built on
   `xbmc.Monitor.waitForAbort(interval)` — never `time.sleep` (AGENTS.md
   invariant) — so Kodi shuts down cleanly. Reuse backend-agnostic
   clamp/poll-interval helpers.
4. Each tick: `listgroups` for the NZBID → show `Downloading NN%` from
   `DownloadedSizeMB/FileSizeMB`; once gone from `listgroups`, switch to
   `history` → show `Post-processing…` / `Repairing…` from history `Status`.
5. Terminal `SUCCESS` → resolve file over SMB (component 4). Terminal failure,
   user-cancel, or timeout → notify + resolve `False`.

### 4. SMB file resolution

A small **pure, Kodi-VFS-agnostic path-mapping function** (separately unit
tested):

1. Derive the per-release subfolder from NZBGet's `DestDir` (primarily the final
   path component; `MainDir`/`DestDir` config is available via the API to
   compute the relative portion precisely if needed).
2. Join `nzbget_smb_root` + subfolder → `smb://…/<release-folder>/`.
3. `xbmcvfs.listdir(...)` and pick the **largest video file**, reusing the
   video-extension / biggest-file selection logic from `webdav.find_video_file`
   (shared VFS-agnostic part extracted, not duplicated).
4. Build a `ListItem` at `smb://…/file.mkv` and `setResolvedUrl(handle, True,
   li)`. **Kodi plays SMB natively — no stream proxy / WebDAV on this path.**

Edge cases: folder not yet visible over SMB → brief `waitForAbort` retry to
absorb write-visibility lag; no video file → notify + resolve `False`;
multiple video files → largest wins.

### 5. Error handling, cancel, test actions

Every path ends in exactly one `setResolvedUrl` (success `True` / failure
`False`):

| Condition | Action |
|-----------|--------|
| Config missing/blank | notify "NZBGet not configured", resolve `False` |
| NZB fetch fails | notify, resolve `False` |
| `append` rejected (NZBID ≤ 0, auth, dupe) | surface redacted NZBGet message, resolve `False` |
| Job fails in NZBGet (`FAILURE/*`, unusable `WARNING/*`) | notify "Download failed in NZBGet", resolve `False` |
| Timeout (clamped `download_timeout`, same as nzbdav) | notify, resolve `False`, **leave job running** |
| User cancels `DialogProgress` | `editqueue`/`GroupDelete` job **+ files**, resolve `False` |
| SMB unreachable / no video file | notify, resolve `False` |

All NZBGet errors go through existing redacting log/notify helpers
(`http_util`); Basic-auth creds and the SMB password in the URL are redacted in
logs — same discipline as existing WebDAV/apikey redaction.

**Test actions** (router handlers + settings buttons, mirroring `test_nzbdav` /
`test_webdav`):

- `test_nzbget` → `version` + `status`; notify OK / specific failure.
- `test_nzbget_smb` → `xbmcvfs.listdir(nzbget_smb_root)`; notify reachable /
  failure.

## Testing

Follows the repo pytest + Kodi-mock pattern (`tests/conftest.py` pre-mocks
`xbmc*`):

- `tests/test_nzbget_api.py` — `append` payload shape (base64 content, category,
  NZBID parsing), `listgroups`/`history` status parsing, `(value, error)`
  contract, auth header, redacted logging. HTTP layer mocked.
- `tests/test_nzbget_smb_path.py` — pure path-mapping function across
  trailing-slash / category / nested-folder variants. No Kodi.
- `tests/test_nzbget_resolver.py` — orchestration with mocked api +
  `xbmcvfs`/`DialogProgress`/`Monitor`: each failure branch resolves `False`
  exactly once; success resolves `True` with the right `smb://` URL; cancel
  triggers `GroupDelete`; timeout leaves the job; status transitions drive
  progress text; largest-video-file selection.
- `tests/test_router.py` additions — `nzbget_enabled` routes to the NZBGet
  resolver; `test_nzbget` / `test_nzbget_smb` handlers wired.

Constraints: `just lint` + `just test` green; runtime code stays **Python
3.8-pure** (no walrus / `match` / `removeprefix`); no new compiled deps.

## Difficulty estimate

**Moderate — roughly 2–4 days, low-to-medium risk.**

- *Easy:* settings tab, test actions, resolver entry branch, SMB path-mapping
  function (mirror existing patterns).
- *Moderate:* JSON-RPC client + poll/progress loop — needs careful status
  state machine (queue → post-processing → history) and real-NZBGet
  verification.
- *Highest-uncertainty:* SMB path translation + write-visibility timing, and
  NZBGet `DestDir`/category folder-layout edge cases. Field-test on the real
  NZBGet + SMB setup.

Big de-risker: Approach A isolation — none of this touches nzbdav streaming,
fallback, or WebDAV code, so the existing playback path can't regress.

## Out of scope (YAGNI)

- Per-play backend override / picker context menu (global toggle only).
- nzbdav-style partial streaming from NZBGet (NZBGet writes whole files).
- Playing before post-processing completes.
- Path-prefix substitution / name-based folder guessing (SMB root = completed
  dir, trailing subfolder appended).
- Separate SMB user/pass fields (credentials embedded in the URL).
