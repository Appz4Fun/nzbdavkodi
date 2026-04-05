"""URL routing for plugin:// calls from Kodi / TMDBHelper."""

from urllib.parse import parse_qs, urlparse

import xbmc


def parse_route(url):
    """Extract the path from a plugin:// URL."""
    parsed = urlparse(url)
    path = parsed.path
    if not path or path == "":
        path = "/"
    return path


def parse_params(query_string):
    """Parse query string into a flat dict (first value only)."""
    if not query_string:
        return {}
    if query_string.startswith("?"):
        query_string = query_string[1:]
    if not query_string:
        return {}
    parsed = parse_qs(query_string)
    return {k: v[0] for k, v in parsed.items()}


def route(argv):
    """Main entry point called from addon.py with sys.argv."""
    base_url = argv[0]
    handle = int(argv[1])
    query_string = argv[2] if len(argv) > 2 else ""

    path = parse_route(base_url)
    params = parse_params(query_string)

    xbmc.log("NZB-DAV: Routing path='{}' params={}".format(path, params), xbmc.LOGDEBUG)

    if path == "/play":
        _handle_play(params)
    elif path == "/search":
        _handle_search(handle, params)
    elif path == "/resolve":
        from resources.lib.resolver import resolve_and_play

        resolve_and_play(
            params.get("nzburl", ""),
            params.get("title", ""),
        )
    elif path == "/install_player":
        from resources.lib.player_installer import install_player

        install_player()
    elif path == "/clear_cache":
        from resources.lib.cache import clear_cache

        clear_cache()
        from resources.lib.http_util import notify

        notify("NZB-DAV", "Search cache cleared", 3000)
    elif path == "/settings":
        import xbmcaddon

        xbmcaddon.Addon().openSettings()
    else:
        _handle_main_menu(handle)


def _handle_play(params):
    """Called via executebuiltin://RunPlugin from TMDBHelper.

    Shows progress bar while searching, then redirects to full-screen
    /search directory listing via ActivateWindow.
    """
    import xbmcgui

    from resources.lib.cache import get_cached, set_cached
    from resources.lib.http_util import notify
    from resources.lib.hydra import search_hydra

    search_type = params.get("type", "movie")
    title = params.get("title", "")
    year = params.get("year", "")
    imdb = params.get("imdb", "")
    season = params.get("season", "")
    episode = params.get("episode", "")

    # Show progress bar while searching
    progress = xbmcgui.DialogProgress()
    progress.create("NZB-DAV", "Searching NZBHydra for {}...".format(title))
    progress.update(10)

    cache_kwargs = dict(year=year, imdb=imdb, season=season, episode=episode)
    results = get_cached(search_type, title, **cache_kwargs)

    if results is None:
        progress.update(30, "Querying NZBHydra2...")
        results = search_hydra(
            search_type, title, year=year, imdb=imdb, season=season, episode=episode
        )
        if results:
            progress.update(70, "Caching {} results...".format(len(results)))
            set_cached(search_type, title, results, **cache_kwargs)
    else:
        progress.update(70, "Loaded {} results from cache".format(len(results)))

    if progress.iscanceled():
        progress.close()
        return

    if not results:
        progress.close()
        notify("NZB-DAV", "No results found for {}".format(title), 3000)
        return

    progress.update(90, "Filtering results...")

    from resources.lib.filter import filter_results

    total_count = len(results)
    filtered = filter_results(results)

    progress.close()

    if not filtered:
        notify("NZB-DAV", "No results after filtering for {}".format(title), 3000)
        return

    # Auto-select best match if enabled
    import xbmcaddon

    addon = xbmcaddon.Addon()
    if addon.getSetting("auto_select_best").lower() == "true":
        best = filtered[0]
        from resources.lib.resolver import resolve_and_play

        resolve_and_play(best["link"], best["title"])
        return

    # Show custom results dialog directly (no ActivateWindow needed)
    from resources.lib.results_dialog import show_results_dialog

    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )

    if selected:
        from resources.lib.resolver import resolve_and_play

        resolve_and_play(selected["link"], selected["title"])


def _handle_search(handle, params):
    """Display search results using the custom full-screen dialog."""
    import xbmcaddon
    import xbmcplugin

    from resources.lib.cache import get_cached, set_cached
    from resources.lib.filter import filter_results
    from resources.lib.hydra import search_hydra

    search_type = params.get("type", "movie")
    title = params.get("title", "")
    year = params.get("year", "")
    imdb = params.get("imdb", "")
    season = params.get("season", "")
    episode = params.get("episode", "")

    cache_kwargs = dict(year=year, imdb=imdb, season=season, episode=episode)
    results = get_cached(search_type, title, **cache_kwargs)
    if results is None:
        results = search_hydra(
            search_type, title, year=year, imdb=imdb, season=season, episode=episode
        )
        if results:
            set_cached(search_type, title, results, **cache_kwargs)

    if not results:
        from resources.lib.http_util import notify

        notify("NZB-DAV", "No results found for {}".format(title), 3000)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    total_count = len(results)
    filtered = filter_results(results)

    # Auto-select best match if enabled
    addon = xbmcaddon.Addon()
    if addon.getSetting("auto_select_best").lower() == "true" and filtered:
        best = filtered[0]
        from resources.lib.resolver import resolve_and_play

        resolve_and_play(best["link"], best["title"])
        return

    # Show custom results dialog
    from resources.lib.results_dialog import show_results_dialog

    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )

    if selected:
        from resources.lib.resolver import resolve_and_play

        resolve_and_play(selected["link"], selected["title"])

    # Must end the directory or Kodi hangs
    xbmcplugin.endOfDirectory(handle, succeeded=False)


def _format_info_line(item):
    """Format a single-line label with all parsed PTT elements.

    Example: 1080p | DV HDR10 | x265/HEVC | Atmos DD+ | en |
             31.2 GB | FLUX | NZBgeek | today
    """
    meta = item.get("_meta", {})
    parts = []

    res = meta.get("resolution", "")
    if res:
        parts.append(res)

    hdr = meta.get("hdr", [])
    if hdr:
        parts.append(" ".join(hdr))

    codec = meta.get("codec", "")
    if codec:
        parts.append(codec)

    audio = meta.get("audio", [])
    if audio:
        parts.append(" ".join(audio))

    langs = meta.get("languages", [])
    if langs:
        parts.append("/".join(langs))

    size_str = _format_size(item.get("size"))
    if size_str and size_str != "N/A":
        parts.append(size_str)

    group = meta.get("group", "")
    if group:
        parts.append(group)

    indexer = item.get("indexer", "")
    if indexer:
        parts.append(indexer)

    age = item.get("age", "")
    if age:
        parts.append(age)

    return " | ".join(parts) if parts else "Unknown"


def _get_tmdb_poster(imdb_id):
    """Fetch poster URL from TMDB using an IMDb ID. Returns empty string on failure."""
    try:
        import json
        from urllib.request import urlopen

        # Use TMDB's find endpoint (no API key needed for basic lookups via v3)
        # Fall back to a free poster service
        url = "https://v2.sg.media-imdb.com/suggestion/t/{}.json".format(imdb_id)
        try:
            with urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read())
                results = data.get("d", [])
                if results and results[0].get("i"):
                    poster = results[0]["i"].get("imageUrl", "")
                    if poster:
                        xbmc.log(
                            "NZB-DAV: Got poster for {}: {}".format(
                                imdb_id, poster[:80]
                            ),
                            xbmc.LOGDEBUG,
                        )
                        return poster
        except Exception:
            pass

        return ""
    except Exception:
        return ""


def _handle_main_menu(handle):
    """Show main menu with settings and install player options."""
    import xbmcgui
    import xbmcplugin

    li = xbmcgui.ListItem(label="Install Player File")
    url = "plugin://plugin.video.nzbdav/install_player"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Clear Cache")
    url = "plugin://plugin.video.nzbdav/clear_cache"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label="Settings")
    url = "plugin://plugin.video.nzbdav/settings"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(handle)


def _format_size(size_bytes):
    """Format byte size to human readable."""
    if not size_bytes:
        return "N/A"
    size_bytes = int(size_bytes)
    if size_bytes >= 1073741824:
        return "{:.1f} GB".format(size_bytes / 1073741824)
    if size_bytes >= 1048576:
        return "{:.1f} MB".format(size_bytes / 1048576)
    return "{} B".format(size_bytes)
