# CLAUDE.md

## Project Overview

NZB-DAV Kodi addon (`plugin.video.nzbdav`) -- a player/resolver for Kodi 21 that searches NZBHydra2 for NZBs, submits them to nzbdav, polls until the stream is ready on nzbdav's WebDAV server, and plays it back. Registers as a TMDBHelper player.

## Architecture

Two external services, one addon:
- **NZBHydra2**: Newznab API for NZB search (XML responses)
- **nzbdav**: SABnzbd-compatible API for NZB submission + WebDAV for streaming
- **This addon**: Bridges TMDBHelper -> NZBHydra2 -> nzbdav -> Kodi player

Flow: TMDBHelper calls plugin:// URL -> router.py dispatches -> hydra.py searches -> filter.py filters with PTT -> user picks result -> resolver.py submits to nzbdav + polls -> webdav.py checks availability -> stream_proxy.py remuxes MP4 to MKV via ffmpeg (with subtitle conversion and seeking) -> setResolvedUrl() plays stream.

The background service (`service.py`) runs a `StreamProxy` HTTP server that remuxes MP4 files on the fly to MKV using ffmpeg. This bypasses a 32-bit Kodi CFileCache bug with large MP4 moov atoms. MKV and other formats are proxied directly with range request support.

## Commands

```bash
just test          # Run all 235 tests (~2s)
just lint          # ruff + black check
just lint-fix      # Auto-fix lint issues
just release       # Build plugin.video.nzbdav.zip
just ship          # test + release
just repo          # Build release + generate Kodi repo in dist/
just clean         # Remove __pycache__, .pytest_cache, zip
just dist-clean    # clean + remove dist/
```

## Code Layout

- `plugin.video.nzbdav/` -- The Kodi addon (installed via zip)
- `plugin.video.nzbdav/resources/lib/` -- All Python modules
- `plugin.video.nzbdav/resources/lib/ptt/` -- Vendored PTT library (DO NOT EDIT unless fixing compatibility)
- `scripts/` -- Build and repo generation scripts (`build_zip.py`, `generate_repo.py`)
- `repo/repository.nzbdav/` -- Kodi repository addon descriptor (points to GitHub Pages)
- `.github/workflows/` -- CI (test+lint on push/PR), Release (build+deploy on `v*` tags)
- `tests/` -- pytest tests with Kodi module mocks in conftest.py

## CI/CD

- **CI** runs on every push to main and PRs: tests across Python 3.8/3.10/3.12, ruff, black
- **Release** triggers on `v*` tags: runs tests, verifies addon.xml version matches tag, builds zip, creates GitHub Release, deploys Kodi repo to GitHub Pages
- **Kodi repo** served at `https://xbmc4lyfe.github.io/nzbdavkodi/`
- To release: bump version in `addon.xml`, commit, `git tag v0.X.0 && git push origin main v0.X.0`

## Key Patterns

- **Lazy imports**: Kodi modules (xbmc, xbmcgui, etc.) are imported inside functions, not at module level, so tests can mock them via conftest.py
- **Shared utilities**: `http_util.py` has `http_get()` and `notify()` -- don't duplicate HTTP or notification logic
- **PTT vendored**: The ptt/ directory is a vendored copy of parse-torrent-title with `regex` replaced by `re` and `arrow` replaced by `datetime`. No pip packages required.
- **Settings via Kodi API**: All config is in `resources/settings.xml` and read via `xbmcaddon.Addon().getSetting()`

## Gotchas

- **Python 3.8 minimum**: No walrus operators, match statements, or str.removeprefix. Target platform is CoreELEC on ARM64.
- **No C extensions**: Everything must be pure Python (no compiled .so files). That's why we replaced `regex` with `re`.
- **PTT regex patterns**: Some PTT patterns use features that produce FutureWarning with newer Python. Escape `[` inside character classes.
- **setResolvedUrl**: MUST be called on ALL paths (success with True, failure with False) or Kodi hangs waiting for resolution.
- **xbmc.Monitor.waitForAbort()**: Use instead of time.sleep() in loops so Kodi can shut down cleanly.
- **Testing Kodi code**: conftest.py mocks all xbmc* modules globally. Add `plugin.video.nzbdav` and `plugin.video.nzbdav/resources/lib` to sys.path.

## Adding New Features

1. Add settings to `resources/settings.xml`
2. Read them via `xbmcaddon.Addon().getSetting("setting_id")`
3. Add tests that mock the setting values
4. Run `just test` and `just lint`

## Adding New Player Targets

Add to `PLAYER_TARGETS` dict in `player_installer.py`:
```python
"AddonName": {
    "setting_id": "install_addonname",
    "path": "special://profile/addon_data/plugin.video.addonname/players/",
}
```
Then add the corresponding boolean setting in `settings.xml`.
