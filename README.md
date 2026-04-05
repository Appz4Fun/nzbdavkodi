# NZB-DAV Kodi Addon

[![CI](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/ci.yml/badge.svg)](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/ci.yml)
[![Pylint](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/pylint.yml/badge.svg)](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/pylint.yml)
[![CodeQL](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/codeql.yml/badge.svg)](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/codeql.yml)
[![Release](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/release.yml/badge.svg)](https://github.com/xbmc4lyfe/nzbdavkodi/actions/workflows/release.yml)
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Kodi](https://img.shields.io/badge/Kodi-21%20Omega-blue.svg)](https://kodi.tv/)
[![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)](https://www.python.org/)

A Kodi 21 (Omega) player/resolver addon that enables Usenet-based streaming through NZBHydra2 and nzbdav. Works as a TMDBHelper player -- search for a movie or TV episode, pick an NZB, and stream it directly through nzbdav's WebDAV server.

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
    F -->|play| G[Kodi Player]
```

No separate SABnzbd needed -- nzbdav handles both downloading and serving.

## Requirements

- **Kodi 21 (Omega)** or later
- **NZBHydra2** -- running and accessible
- **nzbdav** -- running and accessible (provides both SABnzbd-compatible API and WebDAV)
- **TMDBHelper** (or Fen, Seren) -- to trigger searches

## Installation

### Via Kodi Repository (recommended)

Install through the NZB-DAV repository for automatic updates:

1. Download `repository.nzbdav.zip` from the [releases page](../../releases) or [GitHub Pages](https://xbmc4lyfe.github.io/nzbdavkodi/repository.nzbdav/repository.nzbdav.zip)
2. In Kodi: **Settings > Add-ons > Install from zip file** > select `repository.nzbdav.zip`
3. **Settings > Add-ons > Install from repository > NZB-DAV Repository > Video add-ons > NZB-DAV**
4. Future updates are installed automatically

### Manual Install

1. Download `plugin.video.nzbdav.zip` from the [releases page](../../releases)
2. In Kodi: **Settings > Add-ons > Install from zip file**
3. Select `plugin.video.nzbdav.zip`

## Configuration

Open the addon settings (**Add-ons > My add-ons > Video add-ons > NZB-DAV > Configure**):

### Connection Settings

| Setting | Description |
|---------|-------------|
| NZBHydra2 URL | URL to your NZBHydra2 instance (e.g., `http://192.168.1.100:5076`) |
| NZBHydra2 API Key | API key from NZBHydra2's config |
| nzbdav URL | URL to your nzbdav instance (e.g., `http://192.168.1.100:3000`) |
| nzbdav API Key | API key from nzbdav's config |
| WebDAV URL | Leave empty to use nzbdav URL, or set a different URL if WebDAV is on a separate port |
| WebDAV Username | Username for WebDAV authentication |
| WebDAV Password | Password for WebDAV authentication |
| Cache Duration | Search result cache TTL in seconds (default: 300, 0 to disable) |
| Auto-select best match | Skip result list and automatically pick the top result (default: off) |

### Player Installation

Select which addons to install the NZB-DAV player file to:
- TMDBHelper
- Fen
- Seren

Then click **Install Player File** to copy `nzbdav.json` to the selected addons.

### Quality Filters

All filters default to **everything enabled** -- deselect what you don't want.

- **Resolution**: 2160p, 1080p, 720p, 480p
- **HDR**: HDR10, HDR10+, Dolby Vision, HLG, SDR
- **Audio**: Atmos, TrueHD, DTS-HD MA, DTS:X, DD+, DD, AAC
- **Video Codec**: x265/HEVC, x264/AVC, AV1, VP9, MPEG-2
- **Language**: English, Spanish, French, German, Italian, Portuguese, Dutch, Russian, Japanese, Korean, Chinese, Arabic, Hindi

### Keyword Filters

- **Preferred release groups**: Comma-separated list (e.g., `SPARKS,FGT,NTb`) -- boosted to top
- **Excluded release groups**: Comma-separated list -- removed from results
- **Min/Max file size**: In MB (0 = no limit)
- **Exclude/Require keywords**: Comma-separated

### Sort & Display

- **Sort by**: Relevance, Size (largest/smallest), Age (newest/oldest)
- **Max results**: Maximum results to display (default: 25)

#### Relevance Sort Order

When sorted by relevance, results are ranked by these criteria in order of priority:

1. **Resolution**: 4K > 1080p > 720p > 480p
2. **HDR**: Dolby Vision > HDR10+ > HDR10 > HLG > SDR
3. **Preferred release group** (configured in settings)
4. **Audio**: TrueHD+Atmos > Atmos DD+ > TrueHD > DTS:X > DTS-HD MA > DTS > DD+ > DD > AAC
5. **Size**: largest first

### Polling

- **Poll interval**: Seconds between status checks (default: 5)
- **Download timeout**: Max wait time in seconds (default: 3600)

### Search Cache

- **Cache duration**: How long to cache search results in seconds (default: 300, set to 0 to disable)
- Use **Clear Cache** from the addon main menu to manually clear all cached results

### Auto-Select

- **Auto-select best match**: When enabled, the addon automatically picks the top result (after filtering and sorting) and skips the result list entirely. Useful for hands-free playback.

## Usage

1. Open **TMDBHelper** and browse to a movie or TV episode
2. Select **Play with NZB-DAV**
3. Pick an NZB from the custom full-screen results dialog -- each result shows two lines: filename with size/age/indexer/group on line 1, and resolution/HDR/codec/audio/source with color-coded labels on line 2
4. Wait for the download to complete (progress dialog shows status)
5. Playback starts automatically from nzbdav's WebDAV server

With **Auto-select best match** enabled, step 3 is skipped -- the addon automatically picks the top result.

## Development

### Prerequisites

- Python 3.8+
- [just](https://github.com/casey/just) (command runner)

### Commands

```bash
just test          # Run all 132 tests
just test-verbose  # Run tests with full output
just lint          # Check ruff + black formatting
just lint-fix      # Auto-fix lint issues
just release       # Build plugin.video.nzbdav.zip
just ship          # Run tests then build release
just repo          # Build release + generate Kodi repo in dist/
just clean         # Remove build artifacts
just dist-clean    # Remove build artifacts + dist/
```

### Project Structure

```
plugin.video.nzbdav/
  addon.xml              # Kodi addon manifest
  addon.py               # Entry point
  resources/
    settings.xml         # Kodi settings UI
    players/nzbdav.json  # TMDBHelper player template
    lib/
      router.py          # URL routing
      hydra.py           # NZBHydra2 API client
      nzbdav_api.py      # nzbdav API client
      webdav.py          # WebDAV availability checker
      filter.py          # Result filtering with PTT
      results_dialog.py  # Custom full-screen results dialog
      resolver.py        # Download + polling orchestrator
      cache.py           # JSON-based search result cache
      player_installer.py # Player JSON installer
      http_util.py       # Shared HTTP utilities
      playback_monitor.py # Stream failure detection + retry
      ptt/               # Vendored PTT library
    skins/Default/
      1080i/results-dialog.xml  # Dialog skin XML
      media/white.png           # Texture for backgrounds
scripts/
  build_zip.py           # Addon zip builder
  generate_repo.py       # Kodi repo metadata generator
repo/
  repository.nzbdav/     # Repository addon (points to GitHub Pages)
.github/workflows/
  ci.yml                 # Test + lint on push/PR
  release.yml            # Build + deploy on version tags
tests/
  conftest.py            # Kodi module mocks
  test_*.py              # 132 tests
```

### Releasing

1. Bump `version` in `plugin.video.nzbdav/addon.xml`
2. Commit: `git commit -am "release: v0.2.0"`
3. Tag and push: `git tag v0.2.0 && git push origin main v0.2.0`
4. GitHub Actions builds the zip, creates a GitHub Release, and updates the Kodi repo on GitHub Pages
5. Kodi picks up the update automatically via the repository

## Compatibility

- Kodi 21 (Omega) and later
- Python 3.8+
- CoreELEC, LibreELEC, OSMC, Windows, macOS, Linux
- ARM64 (aarch64), x86_64
- No pip packages required -- all dependencies vendored

## License

GPLv3 -- see [LICENSE](LICENSE) for details.
