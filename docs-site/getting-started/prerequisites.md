# Prerequisites

Before you install NZB-DAV, make sure the surrounding pieces are in place. NZB-DAV
is the glue between Kodi and your existing Usenet stack — it doesn't replace any
of these components.

## Required

| Component | What you need | Notes |
|-----------|---------------|-------|
| **Kodi 21 (Omega)** or later | A working Kodi install | Runs on CoreELEC, LibreELEC, OSMC, Windows, macOS, and Linux. |
| **nzbdav** | A running, reachable [nzbdav](https://github.com/nzbdav-dev/nzbdav) instance | Provides both the SABnzbd-compatible submission API and the WebDAV server that streams the file. You don't need a separate SABnzbd. |
| **A Usenet provider** | Configured inside nzbdav | nzbdav connects to your news server; NZB-DAV never talks to Usenet directly. |
| **At least one search provider** | **NZBHydra2**, **Prowlarr**, *or* **direct Newznab indexers** | You can enable more than one; results are merged. See below. |
| **TMDBHelper** | `plugin.video.themoviedb.helper` installed in Kodi | This is how you browse titles and trigger playback. |

## Choose your search provider

NZB-DAV supports three provider types. Enable any combination:

- **NZBHydra2** — a Newznab aggregator that fronts many indexers. Best if you
  already run Hydra.
- **Prowlarr** — an alternative aggregator. NZB-DAV queries Prowlarr's native
  search API and keeps only Usenet (not torrent) results.
- **Direct Newznab indexers** — connect straight to individual indexers
  (NZBGeek, NZBFinder, NZBPlanet, DrunkenSlug, DOGnzb, NZB.su/NZB.life, and
  more, plus custom entries). Use this when you don't run Hydra or Prowlarr.

You need only one of these to start. For details on how each behaves, see
[Search and indexers](../features/search-and-indexers.md).

## Recommended

| Component | Why |
|-----------|-----|
| **ffmpeg** on the Kodi device | Enables the optional remux and HLS compatibility tiers for large or awkward files. NZB-DAV works without it — the proxy simply falls back to direct pass-through. |

## Optional: NZBGet instead of nzbdav

NZB-DAV can use **NZBGet** as the download and playback backend instead of
nzbdav. In this mode NZB-DAV submits to NZBGet, waits for post-processing, and
plays the finished file from an SMB share. The nzbdav streaming and fallback
features don't apply in NZBGet mode. See [NZBGet backend](../features/nzbget-backend.md).

## About dependencies

The add-on's runtime is **pure Python and 3.8-compatible**, with every library
vendored. There's nothing to `pip install`, and no compiled extensions — so it
runs cleanly on ARM64 CoreELEC boxes as well as x86-64 desktops.

## Next step

Once these are ready, [install the add-on](installation.md).
