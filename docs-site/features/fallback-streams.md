# Fallback streams

Usenet retention is imperfect. A release can lose articles at any time, and a
source that starts fine can fail partway through. **Fallback streams** protect
your playback against this. If the source you're watching goes bad mid-stream,
NZB-DAV switches to a verified alternate upload of the same file. An exact
length check and SHA-256 fingerprints of sampled byte ranges must match first.
Playback then continues from the same byte position, without stopping or
rewinding.

Fallback streams are on by default. They apply to the nzbdav backend. The
[NZBGet backend](nzbget-backend.md) uses the same settings to drive NZBGet's
Smart Duplicates failover instead.

## How it behaves

- Backups aren't free. Once playback has run for a short delay (120 seconds
  by default), NZB-DAV submits standby backups to nzbdav in the background.
  Even healthy playback therefore uses some backend and Usenet capacity.
- NZB-DAV prepares backups only after playback has actually started, so they
  don't compete with the fragile stream startup.
- NZB-DAV switches only after it verifies that the alternate matches what you
  were watching: the length must match exactly, and sampled SHA-256
  fingerprints must match too. If it can't verify a backup, it won't switch.
- Mid-stream switching happens on the byte pass-through path, which is the
  default for MKV and other non-MP4 files. Streams served through the MP4
  faststart rewrite or an ffmpeg remux tier aren't switched mid-stream. See
  [Playback, remux, and seeking](playback-and-remux.md).
- When a switch happens, a brief notification tells you which backup it
  switched to ("fall back to candidate #N successful"). Playback doesn't pause
  or jump.

## Settings

These are in the **Fallback Streams** group on the **Advanced** tab:

| Setting | Default | What it does |
|---------|---------|--------------|
| **Enable fallback streams** | On | Turns the whole feature on or off. |
| **Maximum standby fallback streams** | 5 | How many backups NZB-DAV keeps ready per title. On the nzbdav backend, values are clamped to 0–5. `0` turns off backup discovery. |
| **Seconds into playback before submitting fallback backups** | 120 | How long playback must run before backups are submitted. `0` submits them as soon as playback starts. |

!!! info "Why the delay before submitting backups"
    Submitting backups opens extra connections to your backend. In the first
    seconds of playback, the stream's own cache is still filling. Extra
    connections then can starve the live stream and stall video on some
    devices. NZB-DAV therefore waits until playback is well established
    (120 seconds by default) before it prepares backups.

!!! info "Beta feature"
    The **Seconds into playback before submitting fallback backups** setting
    was added in 2.0.0-beta.1 and is available on the
    [Beta channel](../getting-started/beta-channel.md).

## How a backup is chosen

Candidates come from the search results you picked from. On NZBHydra2, NZB-DAV
can also find same-release uploads that Hydra collapsed into one row. Before
NZB-DAV reads any NZB, it prefetches only candidates whose indexer size is
within 25% of your pick.

A candidate is admitted only when it's a plausible copy of the **same file**:

- It's **the same content**: the same title, year, season and episodes,
  part number, edition, and PROPER/REPACK status.
- It's from the **same release group at the same resolution**. Both must be
  parsed and equal. If either is unknown, the candidate is rejected.
- Its codec, container, quality, HDR format, audio, and channels are
  compatible.
- It's a different upload. Its NZB link and article set must differ from your
  pick's.
- The video payload in its NZB is within about 10% of your pick's size.

The admitted candidates are then ranked:

1. **Exact same video filename** first.
2. Then by **similarity tier**. Every candidate already has the same release
   group and resolution, and a known codec mismatch is rejected, so the tiers
   are:
    - **Tier 0:** same codec, with a size within 3%.
    - **Tier 1:** same codec, larger size difference.
    - **Tier 2:** codec not recognized on one side.
3. Then by **smallest size difference**.

Uploads posted within an hour of each other are treated as the same upload
and collapse to the single best-ranked copy. Candidates posted within an hour
of your pick are dropped, because they're the same upload as the one you're
watching.

!!! info "Beta feature"
    Tiered ranking, post-date collapsing, and same-release discovery through
    Hydra were added in 2.0.0-beta.1 and are available on the
    [Beta channel](../getting-started/beta-channel.md).

## How the switch is verified

Switching sources mid-stream only works if the bytes line up exactly.
NZB-DAV runs a two-stage, sampled check before any switch:

1. **Length check:** the alternate's total size must exactly equal the current
   source's size.
2. **Fingerprint sweep:** NZB-DAV compares SHA-256 hashes of matching 4 KiB
   ranges sampled across both files: 20 samples for files under 1 GiB and 100
   for larger files. Every sampled range must match.

These checks sample the file rather than compare every byte, so they give
strong evidence that the files match, not proof. In practice a different
encode or a corrupted upload almost always fails the length check or one of
the samples.

Backups are checked in the background as soon as they're ready, so a switch
can happen instantly when it's needed. A candidate that fails the check is
discarded and never used.

## When the source can't be saved

A switch is triggered by upstream read errors, recoverable short reads, or a
stretch where the source stops delivering new data. If a backup is attached
but none has been verified yet, the current source first gets another pass
through its retry ladder. If no verified backup can resume from the exact
byte position and the retries are used up, NZB-DAV closes the stream cleanly.
It doesn't skip bytes or show corruption. You get a clear failure instead of a
broken picture.

For the full switch sequence, see
[How it works → Fallback cutover](../how-it-works/fallback-cutover.md). It
covers how the byte offset is preserved, how the dead source is demoted, and
the tri-state match logic.
