# Configure connections

Open the add-on settings at **Settings → Add-ons → My add-ons → Video add-ons →
NZB-DAV → Configure**. This page walks through the **Connection** tab, which
is the minimum you need before your first search. It also covers the
**Indexers** and **NZBGet** tabs if you use them. Every setting is listed in
the [settings reference](../reference/settings.md).

<!--
Screenshot placeholder — Capture the NZB-DAV Connection settings tab.
IMPORTANT: use sanitized/dummy values (example hostnames, no real IPs or
usernames) — do not commit a screenshot of a live configuration.
To add: save it as docs-site/images/settings.png, then replace this comment
with:  ![NZB-DAV settings](../images/settings.png)
-->

## Connect to nzbdav

Under **nzbdav**, enter the address and API key of your nzbdav server.

| Setting | What to enter |
|---------|---------------|
| **nzbdav URL** | The base URL of your nzbdav instance, for example `http://192.168.1.100:3000`. Default: `http://localhost:3000`. |
| **API Key** | From the nzbdav web UI: **Settings → Usenet tab → API Key**. |

Select **Test nzbdav Connection**. It reads nzbdav's queue with your key, so
it confirms both the URL and the API key.

## Connect to WebDAV

nzbdav serves finished files over WebDAV. NZB-DAV streams from there.

| Setting | What to enter |
|---------|---------------|
| **WebDAV URL (leave empty to use nzbdav URL)** | Leave this **empty** if WebDAV is served from the same address as the nzbdav URL — NZB-DAV then reuses the nzbdav URL. Enter a value only if your setup exposes WebDAV on a separate address. |
| **Username** | From the nzbdav web UI: **Settings → WebDAV tab → Username**. |
| **Password** | From the nzbdav web UI: **Settings → WebDAV tab → Password**. |

Select **Test WebDAV Connection** to confirm access. It reports separately
whether the server was unreachable, rejected your credentials, or returned a
server error.

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
    | **Prowlarr Indexer IDs (comma-separated)** | Comma-separated indexer IDs to query. **Required** — NZB-DAV skips the Prowlarr search if this is left empty. |

    Select **Test Prowlarr Connection**. It checks the URL and API key by
    listing Prowlarr's indexers. It doesn't check the indexer IDs you entered.
    Prowlarr only contributes **Usenet** results. Torrent results are
    dropped.

=== "Direct Newznab indexers"

    Use this if you don't run Hydra or Prowlarr. Go to the **Indexers** tab:

    1. Turn on **Enable direct Newznab indexers**.
    2. Under **Popular Indexers**, turn on the ones you use (NZB.life / NZB.su,
       NZBGeek, NZBFinder, DrunkenSlug, NZBPlanet, DOGnzb) and enter each
       **API Key**. For any other Newznab indexer, fill in one of the three
       **Custom Newznab Indexers** slots (name, API URL, API key). On beta
       builds you can also use **Manage Indexers**.
    3. Select **Test Direct Indexers**. It queries each enabled indexer's
       capabilities endpoint. It shows **Direct indexers OK: n/n** when all of
       them respond, or the first error when one fails.

    See [Search and indexers](../features/search-and-indexers.md#direct-newznab-indexers)
    for the full indexer manager.

## Improve search accuracy (optional)

!!! info "Beta feature"
    Added in 2.0.0-beta.1 and available on the
    [Beta channel](beta-channel.md).

The last group on the **Connection** tab, **TV search accuracy**, has one
setting: **TMDB API key (optional, movies and TV)**. Enter a key from TMDB
here, not from TVDB. With a key, NZB-DAV converts the ids TMDBHelper sends:

- **Movies:** When TMDBHelper sends only a TMDB id, NZB-DAV looks up the IMDb
  id and uses it to query indexers.
- **TV:** When TMDBHelper doesn't send a TVDB id, NZB-DAV looks up the show's
  TVDB id so indexers can search by id. Id searches return more accurate
  episode results than a title-only search.

Ids that TMDBHelper already supplies are used directly. If you don't enter a
key or a lookup fails, NZB-DAV falls back to the supplied ids or the title.

!!! tip "Entering long API keys with a remote"
    Typing API keys on a TV remote is painful. Use a Kodi remote app with
    keyboard and clipboard support (for example, Kore or Sybu): copy the key on
    your computer, then paste it into the Kodi field from the app.

## Using NZBGet instead of nzbdav

If you use NZBGet as your backend, fill in the **NZBGet** tab as well. Turn on
**Use NZBGet instead of nzbdav for playback**, enter the NZBGet URL,
credentials, and category, and set **Completed Folder (SMB or Local Path)**.
Then run **Test NZBGet Connection** and **Test Completed Folder**. See
[NZBGet backend](../features/nzbget-backend.md) for details. This is a beta
feature. See [Prerequisites](prerequisites.md#optional-nzbget-instead-of-nzbdav).

## Next step

[Set up TMDBHelper](tmdbhelper.md) so you can launch playback from any movie or
episode.
