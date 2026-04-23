# CoreELEC Smoke Validation (clean-article release)

**Status:** gated on PR-1 merge.

**Goal:** confirm the merged PR-1 code plays a clean-article release identically on the real CoreELEC box. Catches regressions the unit-test suite cannot (actual HW decoder, Kodi UI path, WebDAV roundtrip).

---

## Entry criteria

- PR-1 merged into the branch that produces the addon zip.
- CoreELEC box available and idle (no family member watching).
- `kodi.log` is clean / rotated before starting so failures are easy to find.

---

## Setup

- Box: `root@coreelec.local`, 32-bit Kodi on Amlogic AM6B.
- Build: `just release` → `plugin.video.nzbdav.zip`.
- Pick a release with **low article-dead rate** — reference a recent Trakt top-10 popular movie submitted to nzbdav within the last 48 h. Avoid anything older than 30 days (higher article decay).

---

## Steps

1. `just release` on the merged branch.
2. `scp dist/plugin.video.nzbdav.zip root@coreelec.local:/storage/`.
3. Install via Kodi Add-ons → Install from zip file.
4. Restart the addon via the main menu if needed. **Ask permission before `systemctl restart kodi`** — per `memory/feedback_no_kodi_restart_without_permission.md`.
5. Trigger playback via TMDBHelper → "Play with NZB-DAV" on the chosen title.
6. Watch continuously for at least 2 hours. Seek to `00:10`, `00:30`, `01:00`, `01:30`. Each seek should resume within 5 s.
7. On the box, tail `kodi.log` for the final terminal summary line (grep for `NZB-DAV: Stream summary`).

---

## What to verify in `kodi.log`

- One terminal summary line at playback stop containing bytes-served, bytes zero-filled, recovery count, reason.
- Zero occurrences of `strict_contract_mode` rejection messages (default is `warn`, should be silent on conformant nzbdav).
- Zero `PROTOCOL_MISMATCH` / `UPSTREAM_ERROR` reason codes.
- No new error-level lines vs. a pre-merge baseline of the same release.

---

## Acceptance

- Full 2-hour playback end-to-end with no manual intervention.
- All four seek resumes successful within 5 s.
- Audio sync within ±50 ms at every seek.
- Terminal summary shows 0 bytes zero-filled on the clean-article release.

---

## If it fails

Attach the full `kodi.log` to a new issue. Do NOT flip any flags (`strict_contract_mode`, `density_breaker_enabled`) to try to mask the failure — PR-1 is supposed to be a no-op at default flags. A regression at defaults means PR-1 actually regressed something.
