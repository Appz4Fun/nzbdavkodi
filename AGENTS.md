# NZB-DAV Kodi Addon

[![CI](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/ci.yml/badge.svg)](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/ci.yml)
[![Pylint](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/pylint.yml/badge.svg)](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/pylint.yml)
[![CodeQL](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/codeql.yml/badge.svg)](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/codeql.yml)
[![Release](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/release.yml/badge.svg)](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/release.yml)
[![CodeRabbit Pull Request Reviews](https://img.shields.io/coderabbit/prs/github/xbmc4lyfe/nzbdavkodi?utm_source=oss&utm_medium=github&utm_campaign=xbmc4lyfe%2Fnzbdavkodi&labelColor=171717&color=FF570A&link=https%3A%2F%2Fcoderabbit.ai&label=CodeRabbit+Reviews)](https://coderabbit.ai)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Kodi](https://img.shields.io/badge/Kodi-21%20Omega-blue.svg)](https://kodi.tv/)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)

A Kodi 21 (Omega) player/resolver addon that enables Usenet-based streaming through NZBHydra2 and nzbdav. Works as a TMDBHelper player -- search for a movie or TV episode, pick an NZB, and stream it directly through nzbdav's WebDAV server.

> **Current pre-release: `v1.0.0-pre-alpha`** (tagged on the `spike/hls-fmp4` branch, not yet merged to main). Big-file force-remux is on by default, an experimental self-healing fragmented-MP4 HLS path gives full random seek across multi-hundred-gigabyte sources, the submit pipeline no longer freezes on slow nzbdav, and a real-ffmpeg integration test catches container regressions at PR time. See [CHANGELOG.md](CHANGELOG.md#100-pre-alpha--2026-04-15) and [PROXY.md](PROXY.md) for the full picture.

## How It Works

```mermaid
flowchart LR
    A[TMDBHelper] -->|movie / episode| B[NZB-DAV Addon]
    B -->|Newznab search| C[NZBHydra2]
    C -->|NZB results| B
    B -->|user picks result| D{Filter & Select}
    D -->|submit NZB| E[nzbdav]
    E -->|poll status| B
    E -->|stream ready| F[WebDAV Server]
    F -->|range requests| G[Stream Proxy]
    G -->|HTTP 206 / zero-fill on bad articles| H[Kodi Player]
```

No separate SABnzbd needed -- nzbdav handles both downloading and serving.

## Stream Proxy

Every playback request is routed through a local HTTP proxy (`stream_proxy.py`) running on a random port in the background service. Kodi never talks to the WebDAV server directly, which sidesteps a PROPFIND parent-directory scan that caused `Open - Unhandled exception` errors on several Kodi builds.

The proxy picks one of four paths based on the container and file size:

1. **MP4 (already faststart)** -- redirected straight to the WebDAV URL; Kodi seeks and plays natively.
2. **MP4 (moov at tail)** -- parsed in pure Python via HTTP range requests, `stco` / `co64` chunk offsets rewritten, and served as a virtual faststart MP4 with `Accept-Ranges: bytes`. If parsing fails, falls back to an ffmpeg tempfile remux.
3. **MKV and other containers (under the force-remux threshold)** -- served as a byte pass-through with ranged upstream fetches. Kodi gets native seeking from the source file's real Cues, and the proxy layers zero-fill recovery on top: when an upstream read fails mid-stream, it probes forward to the next readable offset, writes zero bytes across the gap, and keeps streaming. No more black screen when a single Usenet article is unrecoverable.
4. **Force remux (files above the threshold, default 20 GB)** -- huge non-MP4 files are streamed through ffmpeg to hide their true size from 32-bit Kodi's `CFileCache` overflow. Two output shapes:
   - **Piped Matroska (default, DV-safe)** -- `ffmpeg -c copy -f matroska pipe:1`, unsized. Known-good on Dolby Vision HEVC + TrueHD/Atmos 100 GB REMUXes. Seek is approximate (each seek respawns ffmpeg with `-ss`).
   - **Fragmented MP4 HLS (experimental, opt-in)** -- `force_remux_mode` in Advanced settings. Produces an HLS VOD playlist with per-segment `hvc1`-tagged fMP4 + a canonical `init.mp4` that survives seek respawns. Gives full random seek across multi-hundred-gigabyte sources. **Self-healing**: if ffmpeg fails to start or doesn't produce a valid init segment within 30 s, the proxy automatically rewrites the session to the matroska branch *before* Kodi sees a broken URL. Dolby Vision profile 7 sources are detected and routed straight to matroska (fmp4 HLS cannot carry dual-layer HEVC).

If ffmpeg isn't installed, the proxy degrades gracefully to pass-through or direct redirect.

> **Architecture deep-dive:** [PROXY.md](PROXY.md) documents the full session lifecycle, how the proxy interacts with `resolver.py` / `service.py` / `router.py` / `mp4_parser.py`, the HLS producer internals, and where to look when debugging playback failures.

## Requirements

| Component | Description |
|-----------|-------------|
| **Kodi 21 (Omega)** | Or later |
| **NZBHydra2** | Running and accessible |
| **nzbdav** | Running and accessible (provides SABnzbd-compatible API + WebDAV) |
| **TMDBHelper** | To trigger searches |

## Installation

### Via Kodi Repository (recommended)

Install through the NZB-DAV repository for automatic updates:

1. In Kodi: **Settings > File Manager > Add source** > enter `https://xbmc4lyfe.github.io/nzbdavkodi/` > name it `nzbdav`
2. **Settings > Add-ons > Install from zip file** > `nzbdav` > `repository.nzbdav` > `repository.nzbdav-1.0.0.zip`
3. **Settings > Add-ons > Install from repository > NZB-DAV Repository > Video add-ons > NZB-DAV**
4. Future updates are installed automatically

### Manual Install

1. Download the addon zip from the [releases page](../../releases)
2. In Kodi: **Settings > Add-ons > Install from zip file** > select `plugin.video.nzbdav.zip`

---

## TMDBHelper Setup

NZB-DAV works as a player for TMDBHelper, which provides the movie/TV browsing interface. If you don't have TMDBHelper installed yet:

### 1. Install TMDBHelper

TMDBHelper is available from the official Kodi repository:

1. **Settings > Add-ons > Install from repository > Kodi Add-on repository > Video add-ons > TheMovieDb Helper**
2. Click **Install** and wait for the notification

If it's not in the official repo for your Kodi version, install from the [TMDBHelper GitHub releases](https://github.com/jurialmunkey/plugin.video.themoviedb.helper/releases):

1. Download the latest `plugin.video.themoviedb.helper` zip
2. **Settings > Add-ons > Install from zip file** > select the downloaded zip

### 2. Configure TMDBHelper

1. Open TMDBHelper settings: **Add-ons > My add-ons > Video add-ons > TheMovieDb Helper > Configure**
2. Under **API Keys**, enter a [TMDB API key](https://www.themoviedb.org/settings/api) (free account required)
3. Under **Players**, set **Default player** to **NZB-DAV** for both Movies and TV Shows

### 3. Install the NZB-DAV Player File

The player file tells TMDBHelper how to call NZB-DAV:

1. Open NZB-DAV settings: **Add-ons > My add-ons > Video add-ons > NZB-DAV > Configure**
2. Click **Install Player File** (installs directly to TMDBHelper)
3. Restart Kodi (or go to TMDBHelper settings > **Players** > **Update players**)

### 4. Set NZB-DAV as the Default Player

1. Open TMDBHelper settings > **Players**
2. Set **Default player (Movies)** to **NZB-DAV**
3. Set **Default player (TV Shows)** to **NZB-DAV**

With this configured, selecting any movie or episode in TMDBHelper will automatically search and stream via NZB-DAV without a player selection prompt.

> **Tip:** If you want to keep multiple players available (e.g., NZB-DAV + a Debrid service), leave the default player as **Choose** and you'll get a player selection dialog each time.

---

## Configuration

Open the addon settings (**Add-ons > My add-ons > Video add-ons > NZB-DAV > Configure**):

![NZB-DAV Settings](docs/images/settings.png)

### Connection Settings

| Setting | Where to find it |
|---------|-----------------|
| NZBHydra2 URL | URL to your NZBHydra2 instance (e.g., `http://192.168.1.100:5076`) |
| NZBHydra2 API Key | NZBHydra2 web UI > `http://<hydra>:5076/config/main` > **Security** section > **API key** |
| nzbdav URL | URL to your nzbdav instance (e.g., `http://192.168.1.100:3333`) |
| nzbdav API Key | nzbdav web UI > `http://<nzbdav>/settings` > **Usenet** tab > **API Key** |
| WebDAV Username | nzbdav web UI > `http://<nzbdav>/settings` > **WebDAV** tab > **Username** |
| WebDAV Password | nzbdav web UI > `http://<nzbdav>/settings` > **WebDAV** tab > **Password** |

> **Tip for entering long API keys:** Use a Kodi remote app with keyboard support (e.g., Sybu on iPhone). Navigate to the nzbdav/NZBHydra2 settings page on your computer, copy the key, then paste from your clipboard into the Kodi input field via the remote app's on-screen keyboard.

### Player Installation

Click **Install Player File** to install the `nzbdav.json` player to TMDBHelper. This registers NZB-DAV as a playback source in TMDBHelper's player selection menu. The player is installed directly to TMDBHelper's players directory.

### Quality Filters

All filters default to **everything enabled** -- deselect what you don't want.

| Filter | Options |
|--------|---------|
| Resolution | 2160p, 1080p, 720p, 480p |
| HDR | HDR10, HDR10+, Dolby Vision, HLG, SDR |
| Audio | Atmos, TrueHD, DTS-HD MA, DTS:X, DD+, DD, AAC |
| Video Codec | x265/HEVC, x264/AVC, AV1, VP9, MPEG-2 |
| Language | EN, ES, FR, DE, IT, PT, NL, RU, JA, KO, ZH, AR, HI |

### Keyword Filters

| Setting | Description |
|---------|-------------|
| Preferred release groups | Comma-separated (e.g., `SPARKS,FGT,NTb`) -- boosted to top |
| Excluded release groups | Comma-separated -- removed from results |
| Min file size | In MB (0 = no limit) |
| Max file size | In MB (0 = no limit) |
| Exclude keywords | Comma-separated |
| Require keywords | Comma-separated |

### Sort & Display

| Setting | Options | Default |
|---------|---------|---------|
| Sort by | Relevance, Size (largest/smallest), Age (newest/oldest) | Relevance |
| Max results | 1--100 | 25 |

### Relevance Sort Order

When sorted by relevance, results are ranked by priority:

| Priority | Criteria | Ranking |
|----------|----------|---------|
| 1 | Resolution | 4K > 1080p > 720p > 480p |
| 2 | HDR | Dolby Vision > HDR10+ > HDR10 > HLG > SDR |
| 3 | Preferred group | Configured groups boosted |
| 4 | Audio | TrueHD+Atmos > Atmos DD+ > TrueHD > DTS:X > DTS-HD MA > DTS > DD+ > DD > AAC |
| 5 | Size | Largest first |

### Polling

| Setting | Description | Default |
|---------|-------------|---------|
| Poll interval | Seconds between status checks | 5 |
| Download timeout | Max wait time in seconds | 3600 |

### Search Cache

| Setting | Description | Default |
|---------|-------------|---------|
| Cache duration | Seconds to cache search results (0 to disable) | 300 |
| Clear Cache | Available from addon main menu | -- |

### Auto-Select

| Setting | Description | Default |
|---------|-------------|---------|
| Auto-select best match | Automatically pick the top result and skip the selection dialog | Off |

---

## Usage

1. Open **TMDBHelper** and browse to a movie or TV episode
2. Select **Play with NZB-DAV**
3. Pick an NZB from the full-screen results dialog
4. Wait for the download to complete (progress dialog shows status)
5. Playback starts automatically from nzbdav's WebDAV server

### Results Dialog

The results dialog shows all matching NZBs with color-coded quality labels, sorted by relevance. Each row displays the release name, resolution, codec, audio format, release type, file size, age, indexer, and release group.

![NZB Results Dialog](docs/images/results-dialog.png)

The status bar at the bottom shows how many sources passed your filters. Use **Enter** to download and play, **C** for the context menu, or **Esc** to go back.

With **Auto-select best match** enabled, the dialog is skipped and the top result plays automatically.

---

## Development

### Prerequisites

- Python 3.10+ for local test tooling
- Kodi addon runtime remains Python 3.8+
- [just](https://github.com/casey/just) (command runner)

### Commands

```bash
just test              # Run all 496 unit tests (integration tests excluded)
just test-verbose      # Run unit tests with full output
just test-integration  # Run integration tests against a real ffmpeg binary
just lint              # Check ruff + black formatting
just lint-fix          # Auto-fix lint issues
just release           # Build plugin.video.nzbdav.zip
just ship              # Run tests then build release
just repo              # Build release + generate Kodi repo in dist/
just repo-zip          # Build repo + copy repository zip to cwd
just clean             # Remove build artifacts
just dist-clean        # Remove build artifacts + dist/
```

### Project Structure

```
plugin.video.nzbdav/
  addon.xml              # Kodi addon manifest
  addon.py               # Entry point
  service.py             # Background service (stream proxy + playback monitor)
  resources/
    settings.xml         # Kodi settings UI
    lib/
      router.py          # URL routing
      hydra.py           # NZBHydra2 API client
      nzbdav_api.py      # nzbdav API client
      webdav.py          # WebDAV availability checker
      filter.py          # Result filtering with PTT
      results_dialog.py  # Custom full-screen results dialog
      resolver.py        # Download + polling orchestrator
      stream_proxy.py    # Local HTTP proxy -- MP4->MKV remux via ffmpeg
      cache.py           # JSON-based search result cache
      player_installer.py # TMDBHelper player JSON installer
      http_util.py       # Shared HTTP utilities
      i18n.py            # Localization helper
      playback_monitor.py # Stream failure detection + retry
      ptt/               # Vendored PTT library (parse-torrent-title)
    language/             # Kodi localization files
    skins/Default/
      1080i/results-dialog.xml  # Dialog skin XML
      media/white.png           # Texture for backgrounds
scripts/
  build_zip.py           # Addon zip builder
  generate_repo.py       # Kodi repo metadata generator
repo/
  repository.nzbdav/     # Repository addon (points to GitHub Pages)
.github/workflows/
  ci.yml                 # Test + lint on push/PR (Python 3.10/3.12)
  release.yml            # Build + deploy on version tags
  pylint.yml             # Pylint analysis (Python 3.8 to validate runtime compat)
  codeql.yml             # CodeQL analysis
  bandit.yml             # Bandit security scan
tests/
  conftest.py                       # Kodi module mocks
  test_*.py                         # 496 unit tests
  test_integration_hls_ffmpeg.py    # 2 integration tests (real ffmpeg, opt-in)
PROXY.md                            # Stream proxy architecture deep-dive
```

### Releasing

1. Bump `version` in `plugin.video.nzbdav/addon.xml`
2. Commit: `git commit -am "release: v0.X.0"`
3. Tag and push: `git tag v0.X.0 && git push origin main v0.X.0`
4. GitHub Actions builds the zip, creates a GitHub Release, and updates the Kodi repo on GitHub Pages
5. Kodi picks up the update automatically via the repository

---

## Compatibility

| Platform | Supported |
|----------|-----------|
| Kodi | 21 (Omega) and later |
| Python | 3.8+ |
| OS | CoreELEC, LibreELEC, OSMC, Windows, macOS, Linux |
| Architecture | ARM64 (aarch64), x86_64 |
| Dependencies | None -- all vendored, no pip required |

## License

GPLv3 -- see [LICENSE](LICENSE) for details.
