# Beta channel and beta features

NZB-DAV ships through two channels of the
[Appz4Fun Kodi repository](installation.md#choose-a-channel). **Stable** gets
only full releases. **Beta** gets every release, including pre-releases. This
page covers what the Beta channel gives you, how to join it or leave it, and how
to report problems.

## Where each channel is today

| Channel | Version | Released |
|---------|---------|----------|
| Stable | 1.2.3 | 2026-05-08 |
| Beta | 2.0.0-beta.2 | 2026-07-18 |

The full release list is on the
[GitHub releases page](https://github.com/Appz4Fun/nzbdavkodi/releases). Any
release marked **Pre-release** there goes only to the Beta channel. The release
workflow sets that flag automatically for any version tag with a hyphen, such
as `v2.0.0-beta.2`.

!!! warning "Beta means beta"
    The 2.0.0 line changes a lot at once: a new download backend, a rewritten
    settings screen, and reworked fallback and recovery. It's used daily, but
    expect rough edges. If something breaks, please
    [report it](#reporting-beta-problems).

## What's in the beta

These features are in 2.0.0-beta.1 or 2.0.0-beta.2 and are **not** in Stable
1.2.3. The full notes are in the
[changelog](https://github.com/Appz4Fun/nzbdavkodi/blob/main/CHANGELOG.md).

### New features

| Feature | What it does | Details |
|---------|--------------|---------|
| **NZBGet backend** | Use NZBGet instead of nzbdav. NZB-DAV submits the NZB, shows download and post-processing progress, then plays the finished file from your completed-downloads folder. | [NZBGet backend](../features/nzbget-backend.md) |
| **Smart Duplicates failover** (NZBGet) | Other same-name results are queued as backups. If your pick can't be repaired, NZBGet switches to a backup and playback follows it. | [Smart Duplicates](../features/nzbget-backend.md#smart-duplicates-failover) |
| **Exact season-pack episode reuse** (beta.2) | A finished season pack plays the episode you asked for, not the largest file. Later episodes from the same pack play from it without downloading again. | [NZBGet backend](../features/nzbget-backend.md) |
| **Indexer manager** | Add, edit, and remove direct Newznab indexers from a preset list of known indexers, with searches that respect each indexer's capabilities. | [Search and indexers](../features/search-and-indexers.md) |
| **TVDB-aware TV search** | With an optional TMDB API key, NZB-DAV looks up the show's TVDB id and searches indexers by id instead of by title. | [Search and indexers](../features/search-and-indexers.md) |
| **Read-ahead buffer** | While a stream plays, and while it's paused, NZB-DAV reads ahead of the playhead so a pause builds real buffer. Default 256 MB. | [Settings reference](../reference/settings.md) |
| **Stall wait and starvation notices** | A slow backend gets a patience window (default 120 s) instead of a dropped stream, and a notification tells you what's happening instead of a silent black screen. | [Playback](../features/playback-and-remux.md) |
| **Queue-clear prompt** | When you start a new download, NZB-DAV can clear nzbdav's queue: **Ask** (default), **Always clear**, or **Never**. It never cancels the job for the title you're starting. | [Settings reference](../reference/settings.md) |
| **Help text for every setting** | The settings screen was rebuilt on Kodi's newer settings format, so every setting and category shows a help line. | [Settings reference](../reference/settings.md) |

### Improvements

- **Fallback streams** match alternate releases in tiers and search more
  widely for same-content peers. For files of 1 GiB or more, the byte-fingerprint
  check samples 100 points instead of 20. See [Fallback streams](../features/fallback-streams.md).
- **SMB playback is checked before it starts** (beta.2). A file that lists over
  SMB but can't be read yet is retried until it can. If it never becomes
  readable, you get a "restart Kodi" hint instead of a failed player.
- **The results dialog** scrolls long labels on the focused row, has
  zebra-striped rows, and keeps remote focus inside the list.
- **Large MKVs start faster.** The proxy pre-reads the end of the file, where
  Matroska keeps its seek index, before playback starts.
- **Prowlarr** results are read from Prowlarr's native search API.
- **Security:** every XML parser that reads network data now goes through one
  hardened parser.

## Coming in the next beta

These changes are merged on `main` after 2.0.0-beta.2 and will ship in the next
beta. The pages they link to already describe them.

| Change | Details |
|--------|---------|
| **More media filters.** Many more resolution, HDR, audio, video-codec, and language options. Each group gets its own **Other / Unknown** switch (on by default), so releases the parser can't classify don't disappear. | [Quality filtering](../features/quality-filtering.md) |
| **New relevance ranking.** Results rank by resolution, then HDR, then Hybrid REMUX / REMUX, then three editable preferred-group tiers (seeded from the TRaSH remux tiers). | [Quality filtering](../features/quality-filtering.md) |
| **Show hidden results in the picker.** Press ++c++ (the context-menu key) to switch between filtered and all results. Hidden rows show why they were filtered. On Linux devices such as CoreELEC, you can also hold OK for five seconds. If nothing passes your filters, the picker opens on the full list instead of an empty one. | [Play your first title](first-playback.md) |
| **Movie IMDb lookup.** With a TMDB API key set, movie searches also get the IMDb id from TMDB. The search cache now defaults to 60 seconds. | [Search and indexers](../features/search-and-indexers.md) |
| **NZBGet completed folder relabelled.** **SMB Completed Folder** becomes **Completed Folder (SMB or Local Path)**. Local and mounted paths already play on the current beta; the next beta also lets season-pack reuse work from them. | [NZBGet backend](../features/nzbget-backend.md) |
| **Blu-ray `.m2ts` files** are recognized as playable video. | — |

## Joining the beta

**New install:** follow [Install the add-on](installation.md) and choose the
**Beta** repository zip (`repository.appz4fun.beta-<version>.zip`).

**Already on Stable:** Kodi only updates a third-party add-on from the
repository it was installed from, so adding the Beta repository isn't enough on
its own.

1. Install the Beta repository zip (**Settings → Add-ons → Install from zip
   file**).
2. Open **Settings → Add-ons → My add-ons → Video add-ons → NZB-DAV →
   Versions**, and pick the newest version listed under **Appz4Fun Repository
   (Beta)**.
3. Optionally uninstall the Stable repository add-on (**Appz4Fun
   Repository**). It no longer affects NZB-DAV.

Your settings stay in place. The 2.0.0 settings screen adds an **NZBGet**
category (and, from the next beta, a **Languages** category). Every new setting
starts at its default.

## Switching channels

To go back to Stable, use the same **Versions** list: open NZB-DAV's add-on
info, choose **Versions**, and pick the version listed under **Appz4Fun
Repository**. Kodi never downgrades an add-on on its own, because the Stable
version (1.2.3) is lower than the beta (2.0.0-beta.x).

After you switch either way, check that **Auto-update** is still on in the
add-on info page.

!!! warning "When 2.0.0 final is released"
    Kodi reads a version like `2.0.0-beta.2` as the base version `2.0.0` plus
    an extra suffix, and it ranks a version with a suffix **above** the same
    version without one. So Kodi treats `2.0.0-beta.2` as newer than a final
    `2.0.0`, and won't offer that update on its own. If a final release has the
    same base number as the beta you're running, install it from
    **Versions**. Any later version, such as `2.0.1`, updates normally.

## Reporting beta problems

1. Turn on **Settings → System → Logging → Enable debug logging**, reproduce
   the problem, and save `kodi.log`. On CoreELEC and LibreELEC it's at
   `/storage/.kodi/temp/kodi.log`.
2. Remove API keys, passwords, and server addresses from anything you share.
   NZB-DAV redacts credentials in its own log lines, but other add-ons may not.
3. Open an issue on
   [GitHub](https://github.com/Appz4Fun/nzbdavkodi/issues). Include the
   NZB-DAV version (shown on the add-on info page), your backend (nzbdav or
   NZBGet), your platform, and the relevant log lines.

See [Troubleshooting](../operations/troubleshooting.md) for common problems
and fixes.
