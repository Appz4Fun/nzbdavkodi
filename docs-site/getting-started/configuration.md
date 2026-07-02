# Configure connections

Open the add-on settings at **Settings → Add-ons → My add-ons → Video add-ons →
NZB-DAV → Configure**. This page walks through the **Connection** tab — the
minimum you need before your first search.

![NZB-DAV settings](../images/settings.png)

## Connect to nzbdav

Under **nzbdav**, enter the address and API key of your nzbdav server.

| Setting | What to enter |
|---------|---------------|
| **nzbdav URL** | The base URL of your nzbdav instance, for example `http://192.168.1.100:3000`. |
| **API Key** | From the nzbdav web UI: **Settings → Usenet tab → API Key**. |

Select **Test nzbdav Connection** to confirm the URL and key work.

## Connect to WebDAV

nzbdav serves finished files over WebDAV. NZB-DAV streams from there.

| Setting | What to enter |
|---------|---------------|
| **WebDAV URL** | Leave this **empty** if WebDAV is served from the same address as the nzbdav URL — NZB-DAV then reuses the nzbdav URL. Enter a value only if your setup exposes WebDAV on a separate address. |
| **Username** | From the nzbdav web UI: **Settings → WebDAV tab → Username**. |
| **Password** | From the nzbdav web UI: **Settings → WebDAV tab → Password**. |

Select **Test WebDAV Connection** to confirm access.

!!! warning "Leave WebDAV URL empty unless you need it"
    The WebDAV URL field defaults to `http://localhost:8080`. If your WebDAV
    lives at the same address as nzbdav, clear this field so NZB-DAV reuses the
    nzbdav URL. Leaving an unreachable `localhost:8080` in place is a common
    cause of WebDAV connection errors.

## Enable a search provider

You need at least one provider. Turn on whichever you use and fill in its
details. You can enable more than one — results are merged and de-duplicated.

=== "NZBHydra2"

    | Setting | What to enter |
    |---------|---------------|
    | **Enable NZBHydra2** | Turn on. |
    | **NZBHydra2 URL** | e.g. `http://192.168.1.100:5076`. |
    | **API Key** | NZBHydra2 web UI → **Config → Main → Security → API key**. |

    Select **Test NZBHydra Connection** to verify.

=== "Prowlarr"

    | Setting | What to enter |
    |---------|---------------|
    | **Enable Prowlarr** | Turn on. |
    | **Prowlarr URL** | e.g. `http://192.168.1.100:9696`. |
    | **Prowlarr API Key** | Prowlarr web UI → **Settings → General → Security → API Key**. |
    | **Prowlarr Indexer IDs** | Comma-separated indexer IDs to query. **Required** — Prowlarr returns no results if this is left empty. |

    Select **Test Prowlarr Connection** — it checks the URL, key, and indexer
    reachability. Prowlarr only contributes **Usenet** indexers; torrent
    indexers are ignored.

=== "Direct Newznab indexers"

    Use this if you don't run Hydra or Prowlarr. Go to the **Indexers** tab:

    1. Turn on **Enable direct Newznab indexers**.
    2. Enable the popular indexers you use (NZBGeek, NZBFinder, NZBPlanet,
       DrunkenSlug, DOGnzb, NZB.su/NZB.life) and enter each API key, or add your
       own with **Manage Indexers**.
    3. Select **Test Direct Indexers** to verify.

    See [Search and indexers](../features/search-and-indexers.md#direct-newznab-indexers)
    for the full indexer manager.

## Improve TV search accuracy (optional)

Under **TV search accuracy**, you can add a **TMDB API key**. When present,
NZB-DAV resolves a show's TVDB id and uses it to query indexers by id — which
returns more accurate episode results than a title-only search. This is
optional; title search still works without it.

!!! tip "Entering long API keys with a remote"
    Typing API keys on a TV remote is painful. Use a Kodi remote app with
    keyboard and clipboard support (for example, Kore or Sybu): copy the key on
    your computer, then paste it into the Kodi field from the app.

## Next step

[Set up TMDBHelper](tmdbhelper.md) so you can launch playback from any movie or
episode.
