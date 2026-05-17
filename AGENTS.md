# AGENTS.md

Orientation for agents (Claude, Copilot, Codex, etc.) working in this repo. User-facing install, config, and usage docs live in [README.md](README.md). Outstanding work and architecture deep-dives live in [TODO.md](TODO.md).

## TL;DR

- Runtime addon code must stay Python 3.8 compatible and pure Python.
- Preserve `setResolvedUrl`, `waitForAbort`, and HTTP Range behavior.
- Follow existing Kodi mock, settings, HTTP helper, and player install patterns.
- Run `just lint` and `just test` before commit or push.
- For releases, bump only `repo/plugin.video.nzbdav/addon.xml` and regenerate repo artifacts with `just repo`.

## Agent Contract

Follow these rules before making code, release, or deployment changes:

- Run `just lint` and `just test` before any `git commit` or `git push`.
- If `just lint` reports formatting issues, run `just lint-fix`, then re-run `just lint`.
- Keep runtime addon code Python 3.8 compatible. No walrus operators, `match`, or `str.removeprefix`.
- Do not add compiled dependencies or C extensions. CoreELEC/ARM64 installs must stay pure Python.
- Do not edit vendored PTT under `repo/plugin.video.nzbdav/resources/lib/ptt/` unless fixing compatibility.
- Do not duplicate shared HTTP or notification helpers; use `http_util.py`.
- Do not bump the repository addon version for normal addon releases. Only bump `repo/plugin.video.nzbdav/addon.xml`.
- Do not hand-edit `repo/zips/`; regenerate it with `just repo`.
- Do not commit real API keys, WebDAV credentials, Kodi logs, copied crash logs, or local device artifacts.

## Critical Invariants

These must stay true or Kodi playback, shutdown, or updates can break:

- Every resolver path must call `xbmcplugin.setResolvedUrl(...)`: success with `True`, failure with `False`.
- Kodi polling loops must use `xbmc.Monitor.waitForAbort()` instead of `time.sleep()` so Kodi can shut down cleanly.
- Settings must be defined in `resources/settings.xml` and read through `xbmcaddon.Addon().getSetting(...)`.
- The stream proxy must preserve HTTP Range behavior; seeking depends on it.
- MP4 rewrite and ffmpeg remux paths must keep ffmpeg optional and degrade gracefully when it is missing.
- Non-MP4 pass-through is the default unless settings explicitly choose a force-remux path.
- Test imports depend on `tests/conftest.py` pre-mocking `xbmc*` modules before `resources.lib.*` imports.

## Fast Commands

```bash
just test          # Run all tests
just lint          # ruff + black check
just lint-fix      # Auto-fix lint/format issues, then re-run just lint
just release       # Build plugin.video.nzbdav.zip
just ship          # test + release
just repo          # Build release + generate Kodi repo in repo/zips/
just clean         # Remove __pycache__, .pytest_cache, zip
just dist-clean    # clean + remove repo/zips/
```

## Repository Map

- `repo/plugin.video.nzbdav/` -- Kodi addon installed via zip
- `repo/plugin.video.nzbdav/resources/lib/` -- addon runtime Python modules
- `repo/plugin.video.nzbdav/resources/lib/ptt/` -- vendored PTT library
- `repo/plugin.video.nzbdav/resources/settings.xml` -- Kodi settings schema
- `repo/repository.nzbdav/` -- Kodi repository addon descriptor
- `repo/zips/` -- generated Kodi repository metadata and zips
- `scripts/` -- build and repo generation scripts
- `tests/` -- pytest suite with Kodi module mocks in `conftest.py`
- `.github/workflows/` -- CI and release workflows

## Where To Start

- Entry routing: `repo/plugin.video.nzbdav/resources/lib/router.py`
- NZBHydra2 search: `repo/plugin.video.nzbdav/resources/lib/hydra.py`
- Prowlarr search: `repo/plugin.video.nzbdav/resources/lib/prowlarr.py`
- Filtering and result ranking: `repo/plugin.video.nzbdav/resources/lib/filter.py`
- Submit, poll, and resolve: `repo/plugin.video.nzbdav/resources/lib/resolver.py`
- WebDAV checks: `repo/plugin.video.nzbdav/resources/lib/webdav.py`
- Local playback proxy: `repo/plugin.video.nzbdav/resources/lib/stream_proxy.py`
- TMDBHelper player install: `repo/plugin.video.nzbdav/resources/lib/player_installer.py`
- Kodi test mocks: `tests/conftest.py`

## Architecture Snapshot

NZB-DAV Kodi addon (`plugin.video.nzbdav`) is a player/resolver for Kodi 21. It searches NZBHydra2 or Prowlarr, submits selected NZBs to nzbdav, polls until the stream is ready on nzbdav's WebDAV server, then plays the result through Kodi. It also registers as a TMDBHelper player.

External services:

- **NZBHydra2 / Prowlarr**: Newznab-compatible NZB search APIs
- **nzbdav**: SABnzbd-compatible API for NZB submission plus WebDAV streaming
- **This addon**: TMDBHelper -> search -> filter -> submit -> poll -> proxy -> Kodi playback

Flow:

```text
TMDBHelper plugin:// URL
-> router.py
-> hydra.py / prowlarr.py
-> filter.py with PTT parsing
-> user selects result
-> resolver.py submits to nzbdav and polls
-> webdav.py checks availability
-> stream_proxy.py serves or remuxes stream
-> xbmcplugin.setResolvedUrl(...) starts playback
```

The background service (`service.py`) runs `StreamProxy`. MP4 sources may be rewritten or remuxed to avoid Kodi/CoreELEC cache and moov-atom issues. MKV and other formats are proxied directly with Range request support unless the user enables force-remux settings.

## Key Patterns

- Module-level Kodi imports are normal. Tests work because `tests/conftest.py` installs MagicMocks into `sys.modules["xbmc"]`, `sys.modules["xbmcgui"]`, etc. before addon modules import them.
- Individual tests usually patch module-bound Kodi imports, for example `@patch("resources.lib.<mod>.xbmc")`.
- Lazy Kodi imports inside functions are exceptions, usually for Kodi-runtime-only paths or slow imports.
- `http_util.py` owns shared `http_get()` and `notify()` helpers.
- PTT is vendored with `regex` replaced by `re` and `arrow` replaced by `datetime`.
- Some PTT regex patterns can trigger `FutureWarning` on newer Python. Escape `[` inside character classes when fixing them.
- Test tooling is Python 3.10+ because `pytest>=9.0.3` is required for security, even though addon runtime code must remain Python 3.8 compatible.

## When In Doubt

- Prefer existing local patterns over new abstractions.
- Add focused tests near the behavior being changed.
- Preserve Kodi shutdown behavior, playback failure paths, and proxy seeking.
- Read `TODO.md` before changing stream proxy, fallback, or release architecture.

## Change Recipes

### Adding Settings

1. Add the setting to `repo/plugin.video.nzbdav/resources/settings.xml`.
2. Read it via `xbmcaddon.Addon().getSetting("setting_id")`.
3. Add tests that mock the setting value.
4. Run `just test` and `just lint`.

### Adding Player Targets

Add to `PLAYER_TARGETS` in `repo/plugin.video.nzbdav/resources/lib/player_installer.py`:

```python
"AddonName": {
    "setting_id": "install_addonname",
    "path": "special://profile/addon_data/plugin.video.addonname/players/",
}
```

Then add the corresponding boolean setting in `resources/settings.xml`.

### Playback / Resolver Changes

- Preserve `setResolvedUrl` on every success, cancellation, timeout, and failure path.
- Use `xbmc.Monitor.waitForAbort()` for polling loops.
- Check fallback behavior when changing submit, poll, WebDAV discovery, or proxy handoff logic.
- Keep settings reads safe for Kodi's threading constraints; avoid unsafe service-thread Kodi setting reads.
- Add focused tests around success, failure, cancellation, and timeout paths.

### Stream Proxy Changes

- Preserve HTTP Range support and status handling.
- Preserve pass-through as the default for MKV and other non-MP4 containers.
- Keep ffmpeg optional. If ffmpeg is absent or fails to start, playback should fall back gracefully where possible.
- Be careful with MP4 faststart/moov rewrite behavior, large-file offsets, subtitle conversion, and seeking.
- When touching fallback streams, preserve strict validation before switching sources.

### Search / Filter Changes

- Keep NZBHydra2 and Prowlarr behavior aligned where practical.
- Preserve PTT parsing compatibility and avoid adding non-stdlib dependencies.
- Add tests for ranking, filtering, and edge-case titles.

## Live CoreELEC / Kodi Debugging

Agents may SSH to `root@coreelec.local` and restart Kodi when Kodi is crashed, hung, wedged in a core dump, or a deployment/debugging change needs a fresh Kodi process. Preserve useful log/crash evidence first when practical, then restart without waiting for separate approval.

Useful commands:

```bash
ssh root@coreelec.local 'tail -200 /storage/.kodi/temp/kodi.log'
ssh root@coreelec.local 'ls -lh /storage/.kodi/temp/kodi_crashlog* 2>/dev/null || true'
scp root@coreelec.local:/storage/.kodi/temp/kodi.log ./kodi.log
ssh root@coreelec.local 'systemctl restart kodi'
```

Prefer evidence first, restart second. If Kodi is actively wedged and logs are already captured or inaccessible, restart directly.

## CI/CD

- CI runs on every push to `main` and PRs: tests across Python 3.10/3.12, ruff, and black.
- Release workflow triggers on `v*` tags: runs tests, verifies `addon.xml` version matches the tag, builds the zip, and creates a GitHub Release.
- Kodi repo metadata is served from raw GitHub at `https://raw.githubusercontent.com/xbmc4lyfe/nzbdavkodi/main/repo/zips/`.

## Release Checklist

Before cutting a new versioned release:

1. Update `README.md` with user-visible changes.
2. Update repo-level `CHANGELOG.md` with full version notes.
3. Update `repo/plugin.video.nzbdav/changelog.txt` with only a short Kodi-visible summary under 80 characters.
4. Bump only the addon version in `repo/plugin.video.nzbdav/addon.xml`.
5. Do not bump the repository addon version for normal addon releases.
6. Run `just repo` so `repo/zips/` reflects the new addon release for raw GitHub hosting.
7. Run `just lint` and `just test`.
8. Commit and push to `main`.
9. Tag with the new semver and push the tag: `git tag vX.Y.Z && git push origin main vX.Y.Z`.

The Release workflow takes over after the tag is pushed.
