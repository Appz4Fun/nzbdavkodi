# Contributing

The full contributor guide (dev setup, `just` commands, CI and release flow) is on
the docs site: [Contributing](https://appz4fun.github.io/nzbdavkodi/contributing/).
The authoritative rules for code changes are in [AGENTS.md](AGENTS.md).

## Before you start

- Open an issue first for substantial changes so the scope is clear.
- Keep changes focused. Separate refactors from behavior changes.
- Do not include generated ZIP artifacts in normal pull requests.

## Local workflow

1. Create a topic branch from `main`.
2. Run `just make-dev` once to bootstrap the pinned toolchain (needs
   [uv](https://docs.astral.sh/uv/) and [just](https://github.com/casey/just)).
3. Run `just lint` and `just test` (or `just ci`, which also runs the Python 3.8
   compile check that CI runs). If `just lint` reports formatting issues, run
   `just lint-fix` and re-run `just lint`.
4. If you touch Kodi UI or playback flows, test in Kodi 21 as well.
5. When user-facing behavior changes, update `README.md`, `CHANGELOG.md`, and
   the matching pages under `docs-site/`. Only touch
   `repo/plugin.video.nzbdav/changelog.txt` and the version in
   `repo/plugin.video.nzbdav/addon.xml` when cutting a release.

## Pull requests

- Fill out the pull request template.
- Describe the user-visible impact and any migration or configuration changes.
- Link the related issue when applicable.
- Keep secrets, API keys, and personal server URLs out of commits, screenshots, and logs.
