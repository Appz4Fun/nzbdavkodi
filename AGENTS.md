# AGENTS.md

Orientation for agents (Claude, Copilot, Codex, etc.) working in this repo. User-facing install / config / usage docs start in [README.md](README.md), [docs/quickstart.md](docs/quickstart.md), and [docs/troubleshooting.md](docs/troubleshooting.md). Contributor architecture docs live in [docs/architecture.md](docs/architecture.md) and [docs/proxy-architecture.md](docs/proxy-architecture.md). Active backlog lives in [TODO.md](TODO.md).

## Pre-Push

Before any `git commit` or `git push`, run the same tooling the GitHub workflow runs so failures are caught locally instead of in CI:

```bash
just lint    # ruff + black format check
just test    # default non-integration pytest suite
```

Both must pass cleanly. If `just lint` reports formatting issues, run `just lint-fix` and re-run `just lint` before committing.

## Pre-Release

Before cutting a new versioned release:

1. Update `README.md` with any user-visible changes.
2. Update `CHANGELOG.md` (repo-level) with the full version notes.
3. Update `repo/plugin.video.nzbdav/changelog.txt` (the addon's Kodi-visible changelog) with only a short, sweet release summary under 80 characters.
4. **ONLY** bump the addon version in `repo/plugin.video.nzbdav/addon.xml` to the new semver. Do NOT bump repository version—this allows users with the Kodi repo already installed to see the addon version upgrade without re-adding the repository.
5. Run `just repo` so `repo/zips/` reflects the new addon release for raw GitHub hosting.
6. Commit and push to `main`.
7. Tag with the new semver and push the tag: `git tag vX.Y.Z && git push origin vX.Y.Z` (the Release workflow takes over from there).

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
just test          # Run the default non-integration pytest suite
just lint          # ruff + black check
just lint-fix      # Auto-fix lint issues
just release       # Build plugin.video.nzbdav.zip
just ship          # test + release
just repo          # Build release + generate Kodi repo in repo/zips/
just clean         # Remove __pycache__, .pytest_cache, zip
just dist-clean    # clean + remove repo/zips/
```

## Code Layout

- `repo/plugin.video.nzbdav/` -- The Kodi addon (installed via zip)
- `repo/plugin.video.nzbdav/resources/lib/` -- All Python modules
- `repo/plugin.video.nzbdav/resources/lib/ptt/` -- Vendored PTT library (DO NOT EDIT unless fixing compatibility)
- `scripts/` -- Build and repo generation scripts (`build_zip.py`, `generate_repo.py`)
- `repo/repository.nzbdav/` -- Kodi repository addon descriptor (points to raw GitHub)
- `repo/zips/` -- Generated Kodi repository metadata and zips
- `.github/workflows/` -- CI (test+lint on push/PR), Release (build+deploy on `v*` tags)
- `tests/` -- pytest tests with Kodi module mocks in conftest.py

## CI/CD

- **CI** runs on every push to main and PRs: tests across Python 3.10/3.12, ruff, black
- **Release** triggers on `v*` tags: runs tests, verifies addon.xml version matches tag, builds zip, creates GitHub Release
- **Kodi repo metadata** served from raw GitHub at `https://raw.githubusercontent.com/xbmc4lyfe/nzbdavkodi/main/repo/zips/`
- To release: bump version in `repo/plugin.video.nzbdav/addon.xml`, run `just repo`, commit, `git tag v0.X.0 && git push origin main v0.X.0`

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
- **Testing Kodi code**: conftest.py mocks all xbmc* modules globally. Add `repo/plugin.video.nzbdav` and `repo/plugin.video.nzbdav/resources/lib` to sys.path.
- **Live CoreELEC/Kodi debugging**: Agents may restart or kill/restart Kodi on `root@coreelec.local` when Kodi is crashed, hung, wedged in a core dump, or a deployment/debugging change needs a fresh Kodi process. Preserve useful crash/log evidence first when practical, then restart without waiting for separate approval.

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
