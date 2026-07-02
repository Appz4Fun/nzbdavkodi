# Fallback streams

Usenet retention is imperfect: a release can lose articles at any time, and a
source that starts fine can fail partway through. **Fallback streams** protect
your playback against this. If the source you're watching goes bad mid-stream,
NZB-DAV switches to a verified alternate release of the same content — after a
length check and sampled SHA-256 fingerprints confirm the bytes match — and
continues from the same point, with no stop, no rewind, and
no visible interruption.

Fallback streams are on by default.

## How it behaves

- Backups aren't free: once playback has run for a short delay (default 120
  seconds), NZB-DAV submits standby backups in the background, so even healthy
  playback uses some backend and Usenet capacity.
- Backups are only prepared after playback has been running successfully for a
  short while, so they don't compete with the fragile stream startup.
- Before NZB-DAV ever switches, it verifies the alternate matches what you were
  watching — an exact length match plus sampled SHA-256 fingerprints. If it
  can't verify that, it won't switch.
- When a switch does happen, you see a brief "Switched to fallback stream"
  notification. Playback itself doesn't pause or jump.

## Settings

On the **Advanced** tab:

| Setting | Default | What it does |
|---------|---------|--------------|
| **Enable fallback streams** | On | Master switch for the whole feature. |
| **Maximum standby fallback streams** | 5 | How many verified backups NZB-DAV keeps ready per title. The hard ceiling is 5. |
| **Seconds into playback before submitting fallback backups** | 120 | How long playback must run before backups are submitted. `0` submits immediately at playback start. |

!!! info "Why the delay before submitting backups"
    Submitting backups opens extra connections to your backend. Doing that
    during the first seconds of playback — when the stream's own cache is still
    filling — can starve the live stream and stall video on some devices. NZB-DAV
    therefore waits until playback is well established (default 120 seconds)
    before preparing backups.

## How a backup is chosen

NZB-DAV only considers alternate releases that are genuinely the **same
content** — same title, year, season/episode, edition, and so on. It ranks
candidates into similarity tiers:

- **Tier 0** — same resolution, codec, and release group, with a size within 3%.
- **Tier 1** — same resolution and codec.
- **Tier 2** — same resolution, different codec.
- **Tier 3** — same content, otherwise different.

Candidates come from the same search-result pool you picked from, and can be
augmented by same-release re-uploads reported by NZBHydra2. Reposts of the same
release made within the same hour are collapsed to the single best-tier copy so
your backups are genuinely diverse.

## How the switch is verified

Switching sources mid-stream is only safe if the bytes line up exactly, so
NZB-DAV runs a two-stage proof before any cutover:

1. **Length check** — the alternate's total size must exactly equal the current
   source's size.
2. **Fingerprint sweep** — NZB-DAV compares SHA-256 hashes of matching byte
   ranges sampled across both files (20 samples for files under 1 GiB, up to 100
   for larger files). Every sampled range must match.

Verified backups are checked in the background ahead of time, so when a switch
is needed it can happen instantly. If a candidate fails verification, it's
discarded and never used.

## When the source can't be saved

If the active source fails and no verified backup can resume from the exact
byte position, NZB-DAV closes the stream cleanly rather than skipping bytes or
showing corruption. That's the safe outcome — you get a clear failure instead of
a broken picture.

For the full cutover sequence — the byte offset preservation, the demotion of
the dead source, and the tri-state match logic — see
[How it works → Fallback cutover](../how-it-works/fallback-cutover.md).
