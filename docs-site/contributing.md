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
add-on runtime targets Python 3.8, but the tooling runs on Python 3.14 via uv
with the exact pins in `requirements-dev.txt` (pytest, pytest-cov, pylint,
ruff, black, vermin). The docs build uses its own pins in
`requirements-docs.txt` on Python 3.12.

Run `just make-dev` once. It fetches the Python 3.14 and 3.8 interpreters
through uv and installs ffmpeg (Homebrew on macOS; apt-get, dnf, or pacman on
Linux), which the integration tests need.

## Common commands

```bash
just test              # Default unit test suite (skips integration/functional/extreme)
just test-verbose      # Same, with long tracebacks
just test-cov          # Same, plus a Cobertura coverage.xml
just test-integration  # Real-ffmpeg integration tests (tests-extensive/)
just lint              # ruff + black --check + pylint + vermin (3.8 target)
just lint-fix          # ruff --fix + black (re-run just lint afterwards)
just compat-3-8        # compileall the add-on on Python 3.8
just ci                # lint + test + compat-3-8, same as GitHub CI
just release           # Build plugin.video.nzbdav-<version>.zip
just ship              # Test, then build the release zip
just docs              # Build the documentation site into ./site (strict)
just docs-serve        # Serve the docs locally with live reload
```

`just functional-test` and `just extreme-tests` (alias of
`just extreme-functional-test`) run against live services and need a local
`.env` file. `just setup-extreme-functional-test` creates one. They aren't part
of CI.

## How distribution works

- **CI** (`ci.yml`) runs on every push to `main` and on pull requests against
  `main`: `just lint` and `just test` on Python 3.14, plus a `compat-3-8` job
  that byte-compiles the add-on on Python 3.8.
- **Releases** are built by the `Release` workflow when a `v*` tag is pushed. It
  runs the tests, verifies the version in `addon.xml` matches the tag, builds
  the zip, and creates a GitHub Release. Tags with a hyphen (for example
  `v2.0.0-beta.2`) are marked **pre-release**.
- **Distribution** happens in the external
  [Appz4Fun Kodi repository](https://github.com/Appz4Fun/Appz4Fun-Kodi-Repo),
  which rebuilds from each project's GitHub Releases: pre-releases go to the
  Beta channel only, other releases to Stable and Beta. The `Release` workflow
  notifies it so updates appear quickly; otherwise it rebuilds on its daily
  schedule.
- **This site** is built and deployed to GitHub Pages by the `Docs` workflow on
  every push to `main` that changes `docs-site/`, `mkdocs.yml`,
  `requirements-docs.txt`, the README, or the workflow itself. Pull requests
  that touch those paths get a strict build-only check.

## Testing notes

The test suite mocks Kodi's `xbmc*` modules in `tests/conftest.py` (via
`tests/kodi_mocks.py`) before the add-on modules import them. Individual tests
usually patch module-bound Kodi imports. Add focused tests near the behavior
you change — especially around the resolve, poll, proxy, and fallback paths.

## Where to start reading

- Architecture and module map: [How it works](how-it-works/architecture.md).
- Active backlog: [`TODO.md`](https://github.com/Appz4Fun/nzbdavkodi/blob/main/TODO.md).
- Contributor internals for the proxy:
  [`docs/proxy-architecture.md`](https://github.com/Appz4Fun/nzbdavkodi/blob/main/docs/proxy-architecture.md)
  (historical detail; the [Stream proxy](how-it-works/stream-proxy.md) page here
  reflects the current, verified behavior).
- Code-level map of the fallback system:
  [`FALLBACK_INFO.md`](https://github.com/Appz4Fun/nzbdavkodi/blob/main/FALLBACK_INFO.md)
  (module and function names; the user-level view is
  [Fallback cutover](how-it-works/fallback-cutover.md)).
- Release steps: the **Release Checklist** in `AGENTS.md`. Bump only
  `repo/plugin.video.nzbdav/addon.xml`, then push a `vX.Y.Z` tag.
