# Contributing

## Before you start

- Open an issue first for substantial changes so the scope is clear.
- Keep changes focused. Separate refactors from behavior changes.
- Do not include generated ZIP artifacts in normal pull requests.

## Local workflow

1. Create a topic branch from `main`.
2. Run `just lint` and `just test`.
3. If you touch Kodi UI or playback flows, test in Kodi 21 as well.
4. Update `README.md`, `SECURITY.md`, or `plugin.video.nzbdav/changelog.txt` when user-facing behavior changes.

## Pull requests

- Fill out the pull request template.
- Describe the user-visible impact and any migration or configuration changes.
- Link the related issue when applicable.
- Keep secrets, API keys, and personal server URLs out of commits, screenshots, and logs.
