# Prerequisites

Before you install NZB-DAV, make sure the surrounding pieces are in place. NZB-DAV
is the glue between Kodi and your existing Usenet stack — it doesn't replace any
of these components.

## Required

| Component | What you need | Notes |
|-----------|---------------|-------|
| **Kodi 21 (Omega)** or later | A working Kodi install | Runs on CoreELEC, LibreELEC, OSMC, Windows, macOS, and Linux. |
| **nzbdav** or **InfiniDysk** | A running, reachable [nzbdav](https://github.com/nzbdav-dev/nzbdav) instance, or its maintained fork [InfiniDysk](https://github.com/infinidysk/infinidysk) (recommended) | Accepts the NZB and serves the file over WebDAV for streaming. No separate download client is needed. (Beta builds can use NZBGet instead — see below.) |
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
| **TMDB API key** | Lets NZB-DAV turn TMDBHelper's TMDB ids into the IMDb (movie) or TVDB (TV) ids that indexers search by, for more accurate results. It's a TMDB key — NZB-DAV doesn't need a TVDB key. Without it, searches use whatever ids TMDBHelper supplies, or the title. See [Configure connections](configuration.md#improve-search-accuracy-optional). |

## Optional: NZBGet instead of nzbdav

!!! info "Beta feature"
    Added in 2.0.0-beta.1 — available on the [Beta channel](beta-channel.md).

NZB-DAV can use **NZBGet** as the download and playback backend instead of
nzbdav. In this mode NZB-DAV submits to NZBGet, waits for it to finish
downloading and post-processing, and plays the finished file from NZBGet's
completed-downloads folder. Kodi must be able to read that folder, either as an
SMB share (`smb://…`) or as a local or mounted path (for example an NFS mount).
You still need a search provider. nzbdav's WebDAV streaming and live stream
fallback don't apply in NZBGet mode. NZBGet's own duplicate handling covers
failover instead. See [NZBGet backend](../features/nzbget-backend.md).

## About dependencies

The add-on's runtime is **pure Python and 3.8-compatible**, with every library
vendored. There's nothing to `pip install`, and no compiled extensions — so it
runs cleanly on ARM64 CoreELEC boxes as well as x86-64 desktops.

## Next step

Once these are ready, [install the add-on](installation.md).
