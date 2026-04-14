# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""URL routing for plugin:// calls from Kodi / TMDBHelper."""

from urllib.parse import parse_qs, urlparse

import xbmc

from resources.lib.http_util import format_size as _format_size
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string


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


def _safe_resolve_handle(handle):
    """Resolve a plugin handle as a non-playable action.

    Action routes (install_player, clear_cache, settings, configure_*,
    test_hydra, test_nzbdav, resolve) are reached from ``_handle_main_menu``
    items created with ``isFolder=False``. Kodi blocks the UI until the
    plugin calls ``setResolvedUrl`` for that handle; a bare ``return`` from
    the route leaves Kodi waiting indefinitely.

    Calling ``setResolvedUrl(handle, False, ListItem())`` unblocks Kodi
    without initiating playback. When the route was invoked via ``RunPlugin``
    (``handle == -1``) there is no handle to resolve, so the call is skipped.
    """
    if handle < 0:
        return
    import xbmcgui
    import xbmcplugin

    xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())


def route(argv):
    """Main entry point called from addon.py with sys.argv."""
    base_url = argv[0]
    handle = int(argv[1])
    query_string = argv[2] if len(argv) > 2 else ""

    path = parse_route(base_url)
    params = parse_params(query_string)

    safe_params = {
        k: (
            "***"
            if "url" in k.lower() or "api" in k.lower() or "key" in k.lower()
            else v
        )
        for k, v in params.items()
    }
    xbmc.log(
        "NZB-DAV: Routing path='{}' params={}".format(path, safe_params), xbmc.LOGDEBUG
    )

    # /play, /search, and the main menu call setResolvedUrl / endOfDirectory
    # themselves and return early. Everything else is an "action route" that
    # runs a side-effect and then falls through to _safe_resolve_handle so
    # Kodi receives a resolution signal.
    try:
        if path == "/play":
            _handle_play(handle, params)
            return
        if path == "/search":
            _handle_search(handle, params)
            return
        if path == "/resolve":
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

            notify(_addon_name(), _string(30082), 3000)
        elif path == "/settings":
            import xbmcaddon

            xbmcaddon.Addon().openSettings()
        elif path == "/configure_preferred_groups":
            from resources.lib.filter import (
                DEFAULT_PREFERRED_GROUPS,
                configure_groups_dialog,
            )

            configure_groups_dialog(
                "filter_release_group",
                _string(30054),
                DEFAULT_PREFERRED_GROUPS,
            )
        elif path == "/configure_excluded_groups":
            from resources.lib.filter import (
                DEFAULT_EXCLUDED_GROUPS,
                configure_groups_dialog,
            )

            configure_groups_dialog(
                "filter_exclude_release_group",
                _string(30055),
                DEFAULT_EXCLUDED_GROUPS,
            )
        elif path == "/test_hydra":
            _test_hydra_connection()
        elif path == "/test_nzbdav":
            _test_nzbdav_connection()
        else:
            _handle_main_menu(handle)
            return
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Unhandled error in route for path='{}': {}".format(path, e),
            xbmc.LOGERROR,
        )
        _safe_resolve_handle(handle)
        raise

    _safe_resolve_handle(handle)


def _clean_params(params):
    """Convert TMDBHelper '_' placeholders to empty strings.

    TMDBHelper fills empty template fields with a literal underscore when
    calling external players; see PlayerConfig docs:
    https://github.com/jurialmunkey/plugin.video.themoviedb.helper/wiki/PlayerConfig
    """
    return {k: ("" if v == "_" else v) for k, v in params.items()}


def _show_error_dialog(message):
    """Show a modal Kodi error dialog."""
    import xbmcgui

    xbmcgui.Dialog().ok(_addon_name(), message)


def _tag_available(results):
    """Tag results that are already downloaded in nzbdav with _available flag."""
    from resources.lib.nzbdav_api import get_completed_names

    completed = get_completed_names()
    if not completed:
        return
    for result in results:
        if result.get("title") in completed:
            result["_available"] = True


def _lookup_episode_info(imdb, tmdb_id=""):
    """Look up show title and episode info from IMDB ID via TMDB API.

    Used when TMDBHelper passes only IMDB ID without season/episode
    (e.g., from calendar widgets).
    """
    try:
        import json
        from urllib.request import urlopen

        # Use IMDB suggestion API to get the show title
        url = "https://v2.sg.media-imdb.com/suggestion/t/{}.json".format(imdb)
        with urlopen(url, timeout=5) as resp:  # nosec B310
            data = json.loads(resp.read())
            results = data.get("d", [])
            if results:
                title = results[0].get("l", "")
                if title:
                    xbmc.log(
                        "NZB-DAV: Looked up title '{}' for {}".format(title, imdb),
                        xbmc.LOGDEBUG,
                    )
                    return {"title": title}
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Episode lookup failed for {}: {}".format(imdb, e),
            xbmc.LOGDEBUG,
        )
    return None


def _handle_play(handle, params):
    """Called via plugin:// URL from TMDBHelper.

    Searches NZBHydra, shows results dialog, then resolves the selected
    NZB through Kodi's setResolvedUrl pipeline (no dummy.mp4 needed).
    """
    import xbmcgui
    import xbmcplugin

    from resources.lib.cache import get_cached, set_cached
    from resources.lib.http_util import notify
    from resources.lib.hydra import search_hydra

    params = _clean_params(params)
    search_type = params.get("type", "movie")
    title = params.get("title", "")
    year = params.get("year", "")
    imdb = params.get("imdb", "")
    season = params.get("season", "") or params.get("ep_season", "")
    episode = params.get("episode", "") or params.get("ep_episode", "")

    # Fallback: try every possible Kodi InfoLabel source for episode info
    if search_type == "episode" and (not season or not episode):
        # Try all known InfoLabel paths
        label_sources = [
            ("ListItem", "ListItem.Season", "ListItem.Episode", "ListItem.TVShowTitle"),
            (
                "Container.ListItem",
                "Container.ListItem.Season",
                "Container.ListItem.Episode",
                "Container.ListItem.TVShowTitle",
            ),
            (
                "VideoPlayer",
                "VideoPlayer.Season",
                "VideoPlayer.Episode",
                "VideoPlayer.TVShowTitle",
            ),
            (
                "Container(50).ListItem",
                "Container(50).ListItem.Season",
                "Container(50).ListItem.Episode",
                "Container(50).ListItem.TVShowTitle",
            ),
        ]
        for src_name, s_label, e_label, t_label in label_sources:
            il_s = xbmc.getInfoLabel(s_label)
            il_e = xbmc.getInfoLabel(e_label)
            il_t = xbmc.getInfoLabel(t_label)
            xbmc.log(
                "NZB-DAV: InfoLabel [{}]: S='{}' E='{}' T='{}'".format(
                    src_name, il_s, il_e, il_t
                ),
                xbmc.LOGDEBUG,
            )
            if il_s and il_s not in ("", "-1", "0"):
                season = season or il_s
            if il_e and il_e not in ("", "-1", "0"):
                episode = episode or il_e
            if il_t and not title:
                title = il_t
            if season and episode:
                xbmc.log(
                    "NZB-DAV: InfoLabel resolved: '{}' S{}E{} (from {})".format(
                        title, season, episode, src_name
                    ),
                    xbmc.LOGINFO,
                )
                break

    # If we still have IMDB but no title, look up from IMDB
    if search_type == "episode" and imdb and not title:
        looked_up = _lookup_episode_info(imdb, params.get("tmdb_id", ""))
        if looked_up:
            title = looked_up.get("title", title)

    # Show progress bar while searching
    progress = xbmcgui.DialogProgress()
    progress.create(_addon_name(), _fmt(30083, title))
    progress.update(10)
    xbmc.log(
        "NZB-DAV: Search stage: checking cache for '{}' ({})".format(
            title, search_type
        ),
        xbmc.LOGDEBUG,
    )

    cache_kwargs = dict(year=year, imdb=imdb, season=season, episode=episode)
    results = get_cached(search_type, title, **cache_kwargs)

    if results is None:
        progress.update(30, _string(30084))
        xbmc.log(
            "NZB-DAV: Search stage: querying NZBHydra for '{}'".format(title),
            xbmc.LOGDEBUG,
        )
        results, search_error = search_hydra(
            search_type, title, year=year, imdb=imdb, season=season, episode=episode
        )
        if search_error:
            xbmc.log(
                "NZB-DAV: Search stage: NZBHydra error — {}".format(search_error),
                xbmc.LOGWARNING,
            )
            progress.close()
            _show_error_dialog(search_error)
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return
        if results:
            progress.update(70, _fmt(30085, len(results)))
            xbmc.log(
                "NZB-DAV: Search stage: caching {} results for '{}'".format(
                    len(results), title
                ),
                xbmc.LOGDEBUG,
            )
            set_cached(search_type, title, results, **cache_kwargs)
    else:
        progress.update(70, _fmt(30086, len(results)))
        xbmc.log(
            "NZB-DAV: Search stage: loaded {} results from cache for '{}'".format(
                len(results), title
            ),
            xbmc.LOGDEBUG,
        )

    if progress.iscanceled():
        xbmc.log("NZB-DAV: Search stage: cancelled by user", xbmc.LOGDEBUG)
        progress.close()
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    if not results:
        xbmc.log(
            "NZB-DAV: Search stage: no results found for '{}'".format(title),
            xbmc.LOGINFO,
        )
        progress.close()
        notify(_addon_name(), _fmt(30087, title), 3000)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    progress.update(90, _string(30088))
    xbmc.log(
        "NZB-DAV: Search stage: filtering {} results for '{}'".format(
            len(results), title
        ),
        xbmc.LOGDEBUG,
    )

    from resources.lib.filter import filter_results

    total_count = len(results)
    filtered, all_parsed = filter_results(results)

    progress.close()

    if not filtered:
        if all_parsed:
            choice = xbmcgui.Dialog().yesno(
                _addon_name(),
                "All {} results were filtered out. Show unfiltered?".format(
                    len(all_parsed)
                ),
            )
            if choice:
                filtered = all_parsed
            else:
                xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
                return
        else:
            notify(_addon_name(), _fmt(30087, title), 3000)
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

    # Auto-select best match if enabled
    import xbmcaddon

    addon = xbmcaddon.Addon()
    if addon.getSetting("auto_select_best").lower() == "true":
        best = filtered[0]
        from resources.lib.resolver import resolve

        resolve(handle, {"nzburl": best["link"], "title": best["title"]})
        return

    # Tag results already downloaded in nzbdav
    _tag_available(filtered)

    # Show custom results dialog
    from resources.lib.results_dialog import show_results_dialog

    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )

    if selected:
        from resources.lib.resolver import resolve

        resolve(handle, {"nzburl": selected["link"], "title": selected["title"]})
    else:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())


def _handle_search(handle, params):
    """Display search results using the custom full-screen dialog."""
    import xbmcaddon
    import xbmcplugin

    from resources.lib.cache import get_cached, set_cached
    from resources.lib.filter import filter_results
    from resources.lib.hydra import search_hydra

    params = _clean_params(params)
    search_type = params.get("type", "movie")
    title = params.get("title", "")
    year = params.get("year", "")
    imdb = params.get("imdb", "")
    season = params.get("season", "") or params.get("ep_season", "")
    episode = params.get("episode", "") or params.get("ep_episode", "")

    # If we have IMDB but no title/season/episode, look up from TMDB
    if search_type == "episode" and imdb and not title:
        looked_up = _lookup_episode_info(imdb, params.get("tmdb_id", ""))
        if looked_up:
            title = looked_up.get("title", title)
            season = season or looked_up.get("season", "")
            episode = episode or looked_up.get("episode", "")

    cache_kwargs = dict(year=year, imdb=imdb, season=season, episode=episode)
    xbmc.log(
        "NZB-DAV: Search stage: checking cache for '{}' ({})".format(
            title, search_type
        ),
        xbmc.LOGDEBUG,
    )
    results = get_cached(search_type, title, **cache_kwargs)
    if results is None:
        xbmc.log(
            "NZB-DAV: Search stage: querying NZBHydra for '{}'".format(title),
            xbmc.LOGDEBUG,
        )
        results, search_error = search_hydra(
            search_type, title, year=year, imdb=imdb, season=season, episode=episode
        )
        if search_error:
            xbmc.log(
                "NZB-DAV: Search stage: NZBHydra error — {}".format(search_error),
                xbmc.LOGWARNING,
            )
            _show_error_dialog(search_error)
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return
        if results:
            xbmc.log(
                "NZB-DAV: Search stage: caching {} results for '{}'".format(
                    len(results), title
                ),
                xbmc.LOGDEBUG,
            )
            set_cached(search_type, title, results, **cache_kwargs)
    else:
        xbmc.log(
            "NZB-DAV: Search stage: loaded {} results from cache for '{}'".format(
                len(results), title
            ),
            xbmc.LOGDEBUG,
        )

    if not results:
        xbmc.log(
            "NZB-DAV: Search stage: no results found for '{}'".format(title),
            xbmc.LOGINFO,
        )
        from resources.lib.http_util import notify

        notify(_addon_name(), _fmt(30087, title), 3000)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    total_count = len(results)
    xbmc.log(
        "NZB-DAV: Search stage: filtering {} results for '{}'".format(
            len(results), title
        ),
        xbmc.LOGDEBUG,
    )
    filtered, all_parsed = filter_results(results)

    if not filtered:
        if all_parsed:
            import xbmcgui as _gui

            choice = _gui.Dialog().yesno(
                _addon_name(),
                "All {} results were filtered out. Show unfiltered?".format(
                    len(all_parsed)
                ),
            )
            if choice:
                filtered = all_parsed
            else:
                xbmcplugin.endOfDirectory(handle, succeeded=False)
                return
        else:
            from resources.lib.http_util import notify

            notify(_addon_name(), _fmt(30087, title), 3000)
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return

    # Auto-select best match if enabled
    addon = xbmcaddon.Addon()
    if addon.getSetting("auto_select_best").lower() == "true" and filtered:
        best = filtered[0]
        from resources.lib.resolver import resolve_and_play

        resolve_and_play(best["link"], best["title"])
        return

    # Tag results already downloaded in nzbdav
    _tag_available(filtered)

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
    if size_str:
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
            with urlopen(url, timeout=3) as resp:  # nosec B310
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


def _test_hydra_connection():
    """Test NZBHydra2 connection by hitting the caps endpoint."""
    import xbmcaddon

    from resources.lib.http_util import http_get, notify

    addon = xbmcaddon.Addon()
    url = addon.getSetting("hydra_url").rstrip("/")
    api_key = addon.getSetting("hydra_api_key")

    if not url:
        notify(_addon_name(), "NZBHydra URL not configured", 3000)
        return

    test_url = "{}/api?apikey={}&t=caps&o=xml".format(url, api_key)
    try:
        response = http_get(test_url)
        if "<caps>" in response or "<server" in response:
            notify(_addon_name(), "NZBHydra connection OK", 3000)
        else:
            notify(_addon_name(), "NZBHydra: unexpected response", 5000)
    except Exception as e:
        notify(
            _addon_name(),
            "NZBHydra: {}".format(str(e)[:60]),
            5000,
        )


def _test_nzbdav_connection():
    """Test nzbdav connection by hitting the version endpoint."""
    import xbmcaddon

    from resources.lib.http_util import http_get, notify

    addon = xbmcaddon.Addon()
    url = addon.getSetting("nzbdav_url").rstrip("/")
    api_key = addon.getSetting("nzbdav_api_key")

    if not url:
        notify(_addon_name(), "nzbdav URL not configured", 3000)
        return

    test_url = "{}/api?mode=version&apikey={}&output=json".format(url, api_key)
    try:
        response = http_get(test_url)
        if "version" in response:
            notify(_addon_name(), "nzbdav connection OK", 3000)
        else:
            notify(_addon_name(), "nzbdav: unexpected response", 5000)
    except Exception as e:
        notify(
            _addon_name(),
            "nzbdav: {}".format(str(e)[:60]),
            5000,
        )


def _handle_main_menu(handle):
    """Show main menu with settings and install player options."""
    import xbmcgui
    import xbmcplugin

    li = xbmcgui.ListItem(label=_string(30011))
    url = "plugin://plugin.video.nzbdav/install_player"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label=_string(30091))
    url = "plugin://plugin.video.nzbdav/clear_cache"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label=_string(30092))
    url = "plugin://plugin.video.nzbdav/settings"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(handle)
