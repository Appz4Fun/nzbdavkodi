# Contributing

NZB-DAV is open source under GPL-3.0-or-later. Contributions are welcome. This
page is a quick orientation; the authoritative contributor contract lives in
[`AGENTS.md`](https://github.com/Appz4Fun/nzbdavkodi/blob/main/AGENTS.md) in the
repository.

## Ground rules

- **Runtime code stays pure Python and 3.8-compatible.** No walrus operators,
  no `match`, no `str.removeprefix`, and no compiled or C-extension
  dependencies — CoreELEC/ARM64 installs must remain pure Python.
- **Preserve the Kodi invariants.** Every resolve path calls `setResolvedUrl`;
  polling loops use `xbmc.Monitor.waitForAbort`; the stream proxy preserves HTTP
  range behavior; ffmpeg stays optional.
- **Don't edit the vendored PTT library** unless you're fixing a compatibility
  issue.
- **Run `just lint` and `just test` before every commit or push.**

## Development setup

You need [uv](https://docs.astral.sh/uv/) (runs the pinned test and lint
toolchain) and [just](https://github.com/casey/just) (the command runner). The
add-on runtime targets Python 3.8, but the tooling runs on a pinned modern
Python via uv.

## Common commands

```bash
just test          # Run the default unit test suite
just test-verbose  # Same, with full tracebacks
just lint          # ruff + black + pylint + vermin (3.8 target)
just lint-fix      # Auto-fix lint and formatting
just release       # Build plugin.video.nzbdav.zip
just ship          # Test, then build the release zip
just docs          # Build the documentation site into ./site
just docs-serve    # Serve the docs locally with live reload
```

## How distribution works

- **Releases** are built by the `Release` workflow when a `v*` tag is pushed. It
  runs the tests, verifies the version in `addon.xml` matches the tag, builds
  the zip, and creates a GitHub Release.
- **Distribution** happens in the external
  [Appz4Fun Kodi repository](https://github.com/Appz4Fun/Appz4Fun-Kodi-Repo),
  which rebuilds from each project's GitHub Releases. The `Release` workflow
  notifies it so updates appear quickly.
- **This site** is built and deployed to GitHub Pages by the `Docs` workflow on
  every change to `docs-site/`, `mkdocs.yml`, or the README.

## Testing notes

The test suite mocks Kodi's `xbmc*` modules in `tests/conftest.py` before the
add-on modules import them. Individual tests usually patch module-bound Kodi
imports. Add focused tests near the behavior you change — especially around the
resolve, poll, proxy, and fallback paths.

## Where to start reading

- Architecture and module map: [How it works](how-it-works/architecture.md).
- Active backlog: [`TODO.md`](https://github.com/Appz4Fun/nzbdavkodi/blob/main/TODO.md).
- Contributor internals for the proxy:
  [`docs/proxy-architecture.md`](https://github.com/Appz4Fun/nzbdavkodi/blob/main/docs/proxy-architecture.md)
  (historical detail; the [Stream proxy](how-it-works/stream-proxy.md) page here
  reflects the current, verified behavior).
