# AGENTS.md

Orientation for agents (Claude, Copilot, Codex, etc.) working in this repo. User-facing install / config / usage docs live in [README.md](README.md); outstanding work and architecture deep-dives live in [TODO.md](TODO.md).

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
just test          # Run all 670 tests (~2s)
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

- **CI** runs on every push to main and PRs: tests across Python 3.10/3.12, ruff, black
- **Release** triggers on `v*` tags: runs tests, verifies addon.xml version matches tag, builds zip, creates GitHub Release, deploys Kodi repo to GitHub Pages
- **Kodi repo** served at `https://xbmc4lyfe.github.io/nzbdavkodi/`
- To release: bump version in `addon.xml`, commit, `git tag v0.X.0 && git push origin main v0.X.0`

## Key Patterns

- **Module-level Kodi imports + conftest pre-mock**: `import xbmc` / `import xbmcgui` / etc. happen at module top. Tests work because `tests/conftest.py` installs MagicMocks into `sys.modules["xbmc"]` (etc.) BEFORE any `resources.lib.*` is imported, so the module-level `import xbmc` binds to the mock. Individual tests then patch specific attributes via `@patch("resources.lib.<mod>.xbmc")`. A few spots use lazy imports inside functions — usually because the function is only reachable at Kodi runtime and the import is slow — but that is the exception, not the rule.
- **Shared utilities**: `http_util.py` has `http_get()` and `notify()` -- don't duplicate HTTP or notification logic
- **PTT vendored**: The ptt/ directory is a vendored copy of parse-torrent-title with `regex` replaced by `re` and `arrow` replaced by `datetime`. No pip packages required.
- **Settings via Kodi API**: All config is in `resources/settings.xml` and read via `xbmcaddon.Addon().getSetting()`

## Gotchas

- **Python 3.8 minimum**: No walrus operators, match statements, or str.removeprefix. Target platform is CoreELEC on ARM64.
- **Test tooling is Python 3.10+**: `pytest>=9.0.3` is required to clear `GHSA-6w46-j5rx-g56g`, so local `just test` and CI no longer run under Python 3.8.
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
