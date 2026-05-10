# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""URL routing for plugin:// calls from Kodi / TMDBHelper."""

import os
import re
from urllib.parse import parse_qs, urlencode, urlparse

import xbmc

from resources.lib.fallback_streams import (
    FALLBACK_CANDIDATES_DISABLED,
    attach_fallback_candidates_for_selection,
    cached_selection_pool_first_peer,
    fallback_candidate_prefetch_enabled,
    fallback_candidate_prefetch_settings,
    selected_manifest_may_have_fallback_peer,
    selection_pool_may_have_fallback_peer,
)
from resources.lib.http_util import format_size as _format_size
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string
from resources.lib.nzbdav_api import completed_jobs_lookup_done, get_completed_jobs

# IMDB IDs are always `tt` + 7–9 digits. Reject anything else before making
# outbound HTTP calls to IMDB's suggestion API.
_IMDB_ID_RE = re.compile(r"^tt\d{7,9}$")
_SCRIPT_PLAY_STAGE_PATH = "/storage/.kodi/temp/nzbdav-script-play-stage.log"
_SCRIPT_SETTINGS_PATH = (
    "/storage/.kodi/userdata/addon_data/plugin.video.nzbdav/settings.xml"
)


def _script_play_stage(message):
    xbmc.log("NZB-DAV: Script play stage: {}".format(message), xbmc.LOGINFO)
    for stage_path in _script_stage_paths():
        try:
            parent = os.path.dirname(stage_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(stage_path, "a", encoding="utf-8") as stage_file:
                stage_file.write(message + "\n")
                stage_file.flush()
                os.fsync(stage_file.fileno())
            return
        except OSError:
            continue


def _translate_path(path):
    """Translate Kodi special:// paths, returning empty string on failure."""
    try:
        import xbmcvfs

        translated = xbmcvfs.translatePath(path)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""
    return translated if isinstance(translated, str) else ""


def _script_stage_paths():
    paths = []
    translated_temp = _translate_path("special://temp/")
    if translated_temp:
        paths.append(os.path.join(translated_temp, "nzbdav-script-play-stage.log"))
    paths.append(_SCRIPT_PLAY_STAGE_PATH)
    return paths


def _script_settings_paths():
    paths = []
    translated = _translate_path(
        "special://profile/addon_data/plugin.video.nzbdav/settings.xml"
    )
    if translated:
        paths.append(translated)
    paths.append(_SCRIPT_SETTINGS_PATH)
    return paths


def parse_route(url):
    """Extract the path from a plugin:// URL."""
    parsed = urlparse(url)
    path = parsed.path
    if not path:
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
    # keep_blank_values=True so a deliberately-empty parameter (e.g.
    # `&imdb=`) survives instead of vanishing — older callers used the
    # presence of a key as a signal regardless of value. TODO.md §H.3
    # Medium: parse_qs silently drops duplicate params. We still take
    # only `v[0]` (Kodi's plugin URLs don't repeat keys), but at least
    # the drop is visible if a future handler iterates `parsed.items()`.
    parsed = parse_qs(query_string, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def _safe_resolve_handle(handle):
    """Resolve a plugin handle as a non-playable action.

    Action routes (install_player, install_player_other, clear_cache, settings,
    configure_*, test_hydra, test_nzbdav, resolve) are reached from
    ``_handle_main_menu`` items created with ``isFolder=False``. Kodi blocks
    the UI until the plugin calls ``setResolvedUrl`` for that handle; a bare
    ``return`` from the route leaves Kodi waiting indefinitely.

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
    """
    Route a plugin invocation to the appropriate handler based on the URL.

    Routes the incoming plugin call (provided as the Kodi `sys.argv` list) to
    handlers such as play, search, resolve, settings, install, cache clearing,
    provider tests, and the main menu. Action routes with side effects will be
    followed by a safe resolution call so Kodi does not hang.

    Parameters:
        argv (list): The Kodi argv list for the plugin invocation. Expected
            elements:
            - argv[0]: base plugin URL (e.g., "plugin://...") used to derive
              the route path
            - argv[1]: numeric handle for Kodi plugin operations (int)
            - argv[2] (optional): query string containing route parameters
    """
    # argv length and the handle's numericness are both contractually
    # provided by Kodi, but a misconfigured shortcut / external launcher
    # could violate that and the unhandled IndexError / ValueError used
    # to escape `route()` with no setResolvedUrl, hanging Kodi. Surface
    # both as a logged early-return instead. Closes TODO.md §H.3.
    if len(argv) < 2:
        xbmc.log(
            "NZB-DAV: route() called with argv shorter than 2: {!r}".format(argv),
            xbmc.LOGERROR,
        )
        return
    base_url = argv[0]
    try:
        handle = int(argv[1])
    except (TypeError, ValueError):
        xbmc.log(
            "NZB-DAV: route() got non-numeric handle argv[1]={!r}; "
            "skipping this invocation".format(argv[1]),
            xbmc.LOGERROR,
        )
        return
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

            # Normalize TMDBHelper "_" placeholders to empty strings so the
            # resolver sees `""`, not the literal `"_"`.
            clean = _clean_params(params)
            # Pass `clean` so resolve_and_play can clear the matching
            # TMDBHelper bookmark row (keyed by tmdb_id+title) when
            # playback starts. Without it, replays resume from a stale
            # offset. TODO.md §H.3.
            resolve_and_play(
                clean.get("nzburl", ""),
                clean.get("title", ""),
                params=clean,
            )
        elif path == "/direct_play":
            # Test/diagnostic entry: play an explicit primary stream URL
            # via the addon's stream_proxy (so failover validates each
            # fallback with the 100×4 KiB SHA256 sweep before swapping
            # the upstream — Kodi keeps reading from the same proxy
            # URL, so the user sees no interruption).
            #
            # Query params:
            #   primary_url     — full URL with embedded auth user:pass
            #   fallback_urls   — JSON array of URLs (with embedded auth)
            _handle_direct_play(handle, params)
            return
        elif path == "/install_player":
            from resources.lib.player_installer import install_player

            install_player()
        elif path == "/install_player_other":
            from resources.lib.player_installer import install_player_other

            install_player_other()
        elif path == "/clear_cache":
            from resources.lib.cache import clear_cache

            clear_cache()
            from resources.lib.http_util import notify

            notify(_addon_name(), _string(30082), 3000)
        elif path == "/settings":
            import xbmcaddon

            xbmcaddon.Addon("plugin.video.nzbdav").openSettings()
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
        elif path == "/test_prowlarr":
            _test_prowlarr_connection()
        elif path == "/test_direct_indexers":
            _test_direct_indexers_connection()
        elif path == "/manage_indexers":
            from resources.lib.indexer_manager import open_indexer_manager

            open_indexer_manager()
        elif path == "/test_webdav":
            _test_webdav_connection()
        elif path == "/test_nzbdav":
            _test_nzbdav_connection()
        elif path == "/menu":
            _handle_main_menu(handle)
            return
        else:
            import xbmcaddon

            xbmcaddon.Addon("plugin.video.nzbdav").openSettings()
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


def _fallback_candidate_loader_for_selection(selected, results, settings_getter=None):
    """Build a deferred fallback lookup for the selected release."""
    if not selected_manifest_may_have_fallback_peer(selected):
        return None
    if results is None:
        return None
    try:
        if len(results) == 1 and not selection_pool_may_have_fallback_peer(
            selected, results
        ):
            return None
    except TypeError:
        pass

    # Multi-result distinct-peer scans can walk the full picker pool. Keep them
    # inside the loader so resolver can start the primary submit first.
    first_peer = cached_selection_pool_first_peer(selected, results)

    def _load_fallback_candidates():
        # Augment the picker's deduped pool with same-title alternate
        # uploads from Hydra's internal API (showSingleResult... = false).
        # The picker UX still shows one row per release for clean UI, but
        # the fallback worker needs real same-release/different-upload
        # peers — those are exactly what nzbdav-rs needs to swap to
        # without interrupting playback when the primary stream's
        # articles fail.
        from resources.lib.hydra import fetch_release_duplicate_uploads

        try:
            extra_uploads = fetch_release_duplicate_uploads(
                selected, settings_getter=settings_getter
            )
        except Exception as error:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: duplicate-uploads lookup raised: {}".format(error),
                xbmc.LOGDEBUG,
            )
            extra_uploads = []
        augmented = list(results or []) + list(extra_uploads or [])

        if not selection_pool_may_have_fallback_peer(selected, augmented):
            return FALLBACK_CANDIDATES_DISABLED
        if settings_getter is None:
            fallback_settings = fallback_candidate_prefetch_settings()
        else:
            fallback_settings = fallback_candidate_prefetch_settings(
                settings_getter=settings_getter
            )
        if not fallback_candidate_prefetch_enabled(fallback_settings):
            return FALLBACK_CANDIDATES_DISABLED
        known_first_peer = cached_selection_pool_first_peer(selected, augmented)
        attach_fallback_candidates_for_selection(
            selected,
            _selection_pool_with_peer_first(
                selected, augmented, known_first_peer or first_peer
            ),
            fallback_settings=fallback_settings,
        )
        return list(selected.get("_fallback_candidates", []) or [])

    return _load_fallback_candidates


def _attach_selected_indexer(resolver_params, selected):
    if not isinstance(selected, dict):
        return
    indexer = str(selected.get("indexer", "") or "").strip()
    if indexer:
        resolver_params["_selected_indexer"] = indexer


def _selection_pool_with_peer_first(selected, results, first_peer):
    """Return a selection pool that tries the known plausible peer first."""
    if isinstance(selected, dict):
        yield selected
    if isinstance(first_peer, dict) and first_peer is not selected:
        yield first_peer
    for result in results or []:
        if result is selected or result is first_peer:
            continue
        yield result


def _show_error_dialog(message):
    """
    Display a modal error dialog in Kodi with the add-on name as the dialog title.

    Parameters:
        message (str): The error message to display.
    """
    import xbmcgui

    xbmcgui.Dialog().ok(_addon_name(), message)


def _get_addon_setting(addon, key, default=""):
    """Read a Kodi setting, returning a default if Kodi's settings layer fails."""
    try:
        value = addon.getSetting(key)
    except RuntimeError as exc:
        xbmc.log(
            "NZB-DAV: setting '{}' unavailable; using default: {}".format(key, exc),
            xbmc.LOGWARNING,
        )
        return default
    return value if isinstance(value, str) else default


def _get_script_setting(key, default=""):
    """Read this addon's setting from settings.xml without Kodi settings APIs."""
    from xml.etree import ElementTree as element_tree

    for settings_path in _script_settings_paths():
        try:
            root = element_tree.parse(settings_path).getroot()
        except (OSError, element_tree.ParseError):
            continue

        for setting in root.findall(".//setting"):
            if setting.get("id") != key:
                continue
            value = setting.text
            return value if isinstance(value, str) else default
    return default


def _script_completed_job_for_selection(selected):
    """Look up completed-history metadata for a RunScript picker selection."""
    title = selected.get("title", "") if isinstance(selected, dict) else ""
    if not title:
        return None
    try:
        from resources.lib.nzbdav_api import find_completed_by_name

        return find_completed_by_name(title, settings_getter=_get_script_setting)
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Script completed lookup failed for '{}': {}".format(title, error),
            xbmc.LOGDEBUG,
        )
        return None


def _search_all_providers(
    search_type,
    title,
    year="",
    imdb="",
    season="",
    episode="",
    settings_getter=None,
):
    """
    Search enabled indexer providers and return combined, deduplicated results.

    Searches configured providers (NZBHydra2, Prowlarr, and/or direct
    Newznab indexers), merges their results, and removes duplicate entries by
    `link`. If no providers are
    enabled, returns an explicit error message. If every enabled provider
    failed and produced no results, returns the first collected error.

    Returns:
        tuple: (results, error_message)
            results (list): Deduplicated list of result dictionaries returned
                by providers.
            error_message (str or None): Error text when every enabled
                provider failed or when no providers are enabled; otherwise
                `None`.
    """
    _script_play_stage("providers entry")
    if settings_getter is None:
        import xbmcaddon

        addon = xbmcaddon.Addon("plugin.video.nzbdav")
        _script_play_stage("providers addon created")

        def settings_getter(key, default=""):
            return _get_addon_setting(addon, key, default)

    else:
        _script_play_stage("providers using script settings")

    # NZBHydra2 defaults ON (settings.xml default="true"), Prowlarr defaults
    # OFF (default="false"). The two getSetting checks below are the
    # default-preserving forms of those defaults — empty/unset reads to True
    # for nzbhydra and False for prowlarr.
    nzbhydra_raw = settings_getter("nzbhydra_enabled", "true")
    nzbhydra_enabled = nzbhydra_raw.lower() != "false"
    prowlarr_enabled = settings_getter("prowlarr_enabled", "false").lower() == "true"
    direct_indexers_enabled = (
        settings_getter("direct_indexers_enabled", "false").lower() == "true"
    )
    _script_play_stage(
        "providers settings nzbhydra={} prowlarr={} direct={}".format(
            nzbhydra_enabled, prowlarr_enabled, direct_indexers_enabled
        )
    )

    if not nzbhydra_enabled and not prowlarr_enabled and not direct_indexers_enabled:
        return (
            [],
            "No search providers enabled. Enable NZBHydra2, Prowlarr, "
            "or direct indexers in settings.",
        )

    all_results = []
    errors = []

    if nzbhydra_enabled:
        from resources.lib.hydra import search_hydra

        _script_play_stage("hydra search start")
        hydra_results, hydra_error = search_hydra(
            search_type,
            title,
            year=year,
            imdb=imdb,
            season=season,
            episode=episode,
            settings_getter=settings_getter,
        )
        _script_play_stage(
            "hydra search done count={} error={}".format(
                len(hydra_results or []), bool(hydra_error)
            )
        )
        if hydra_error:
            xbmc.log(
                "NZB-DAV: NZBHydra2 search error: {}".format(hydra_error),
                xbmc.LOGWARNING,
            )
            errors.append(hydra_error)
        else:
            all_results.extend(hydra_results)

    if prowlarr_enabled:
        from resources.lib.prowlarr import search_prowlarr

        _script_play_stage("prowlarr search start")
        prowlarr_results, prowlarr_error = search_prowlarr(
            search_type, title, year=year, imdb=imdb, season=season, episode=episode
        )
        _script_play_stage(
            "prowlarr search done count={} error={}".format(
                len(prowlarr_results or []), bool(prowlarr_error)
            )
        )
        if prowlarr_error:
            xbmc.log(
                "NZB-DAV: Prowlarr search error: {}".format(prowlarr_error),
                xbmc.LOGWARNING,
            )
            errors.append(prowlarr_error)
        else:
            all_results.extend(prowlarr_results)

    if direct_indexers_enabled:
        from resources.lib.direct_indexers import search_direct_indexers

        _script_play_stage("direct indexers search start")
        direct_results, direct_error = search_direct_indexers(
            search_type, title, year=year, imdb=imdb, season=season, episode=episode
        )
        _script_play_stage(
            "direct indexers search done count={} error={}".format(
                len(direct_results or []), bool(direct_error)
            )
        )
        if direct_error:
            xbmc.log(
                "NZB-DAV: Direct indexer search error: {}".format(direct_error),
                xbmc.LOGWARNING,
            )
            errors.append(direct_error)
        else:
            all_results.extend(direct_results)

    seen_links = set()
    deduped = []
    for result in all_results:
        key = result.get("link", "")
        if not key:
            # No link → no way to play this result. Dropping is better
            # than presenting a dead entry in the selection dialog.
            continue
        if key in seen_links:
            continue
        seen_links.add(key)
        deduped.append(result)

    if not deduped and errors:
        return [], errors[0]

    return deduped, None


def _tag_available(results):
    """
    Mark result entries that already exist in nzbdav by setting the `_available` flag.

    Parameters:
        results (list[dict]): Iterable of result dictionaries; entries whose
            `"title"` matches a completed name in nzbdav will be modified
            in-place with `result["_available"] = True`.
    """
    if not results:
        return {}
    completed = get_completed_jobs()
    if not completed:
        return completed
    for result in results:
        completed_job = completed.get(result.get("title"))
        if completed_job:
            result["_available"] = True
            result["_completed_job"] = completed_job
    return completed


def _completed_lookup_was_done(completed_jobs):
    """Return whether picker-time completed-history lookup can be reused."""
    return (isinstance(completed_jobs, dict) and bool(completed_jobs)) or (
        completed_jobs_lookup_done(completed_jobs)
    )


def _lookup_episode_info(imdb, tmdb_id=""):
    """Look up show title and episode info from IMDB ID via TMDB API.

    Used when TMDBHelper passes only IMDB ID without season/episode
    (e.g., from calendar widgets).
    """
    # Reject non-IMDB input before hitting the network.
    if not imdb or not _IMDB_ID_RE.match(imdb):
        return None
    try:
        import json
        from urllib.request import urlopen

        # Use IMDB suggestion API to get the show title
        url = "https://v2.sg.media-imdb.com/suggestion/t/{}.json".format(imdb)
        # nosemgrep
        with urlopen(  # nosec B310 — IMDB suggestion API (trusted)
            url, timeout=5
        ) as resp:
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


def _handle_direct_play(handle, params):
    """Resolve a primary stream URL through stream_proxy and hand
    Kodi the proxy URL via setResolvedUrl.

    Returns a single proxy URL to Kodi — when an article fails on the
    primary upstream, stream_proxy validates the fallback (HEAD +
    100×4 KiB SHA256 sweep) and continues serving Kodi the same
    response stream from the new upstream's matching offset, with no
    Player.Stop / no rewind to t=0 / no visible blip.

    Triggered via ``Player.Open({"file": "plugin://plugin.video.nzbdav/direct_play?..."})``
    so the handle is real and setResolvedUrl actually starts playback.
    """
    import base64
    import json as _json

    from urllib.error import HTTPError, URLError
    from urllib.parse import urlsplit, urlunsplit
    from urllib.request import Request, urlopen

    import xbmcgui
    import xbmcplugin

    from resources.lib.resolver import (
        _direct_playback_service_config,
        _prepare_direct_playback,
    )

    def _split_auth(url):
        """Return (clean_url, auth_header) — Python urllib's name
        resolver mis-parses ``user:pass@host`` and raises gaierror,
        so we have to peel off the inline auth and pass it via header."""
        try:
            parsed = urlsplit(url)
        except (ValueError, TypeError):
            return url, ""
        # Empty username (``://:pass@host`` or ``://@host``) is not a
        # legitimate auth credential; emitting ``Basic OnBhc3M=`` would
        # send a malformed header that some upstreams accept and some
        # reject. Treat it as "no auth" and let the caller forward the
        # URL verbatim.
        if parsed.username in (None, ""):
            return url, ""
        userpass = "{}:{}".format(parsed.username, parsed.password or "")
        encoded = base64.b64encode(userpass.encode()).decode()
        host = parsed.hostname or ""
        if parsed.port:
            host = "{}:{}".format(host, parsed.port)
        clean = urlunsplit(
            (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
        )
        return clean, "Basic " + encoded

    primary_url_raw = params.get("primary_url", "")
    fallback_urls_raw = params.get("fallback_urls", "[]")
    if not primary_url_raw:
        xbmc.log("NZB-DAV: /direct_play missing primary_url", xbmc.LOGERROR)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return
    primary_url, primary_auth = _split_auth(primary_url_raw)
    try:
        fallback_urls = _json.loads(fallback_urls_raw)
    except (TypeError, ValueError):
        fallback_urls = []
    if not isinstance(fallback_urls, list):
        fallback_urls = []

    # Reject non-http(s) URLs before any HEAD: urlopen will happily
    # dereference file:// (reading arbitrary local files) and ftp://,
    # and a junk scheme can throw deep inside urllib. _validate_url
    # is shared with stream_proxy so the policy stays consistent.
    from resources.lib.stream_proxy import _validate_url

    def _head_length(url, auth_header):
        try:
            headers = {}
            if auth_header:
                headers["Authorization"] = auth_header
            req = Request(url, method="HEAD", headers=headers)
            # nosemgrep
            with urlopen(req, timeout=10) as resp:  # nosec B310
                length = int(resp.headers.get("Content-Length", "0") or 0)
                if length <= 0:
                    return 0, "missing-length"
                return length, ""
        except HTTPError as exc:
            return 0, "http-{}".format(exc.code)
        except URLError as exc:
            return 0, "url-{}".format(exc.reason)
        except (OSError, ValueError) as exc:
            return 0, str(exc)[:60]

    try:
        _validate_url(primary_url)
    except (ValueError, TypeError):
        xbmc.log(
            "NZB-DAV: /direct_play rejecting non-http(s) primary",
            xbmc.LOGERROR,
        )
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    primary_len, primary_err = _head_length(primary_url, primary_auth)
    if primary_err:
        xbmc.log(
            "NZB-DAV: /direct_play primary HEAD failed: {}".format(primary_err),
            xbmc.LOGERROR,
        )
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    fallback_sources = []
    for idx, url_raw in enumerate(fallback_urls):
        if not isinstance(url_raw, str) or not url_raw:
            continue
        url, auth = _split_auth(url_raw)
        try:
            _validate_url(url)
        except (ValueError, TypeError):
            xbmc.log(
                "NZB-DAV: /direct_play skipping non-http(s) fallback: {}".format(
                    url_raw[:120]
                ),
                xbmc.LOGWARNING,
            )
            continue
        length, err = _head_length(url, auth)
        if err or length <= 0:
            xbmc.log(
                "NZB-DAV: /direct_play skipping unstreamable fallback "
                "({}): {}".format(err, url[:120]),
                xbmc.LOGWARNING,
            )
            continue
        stream_headers = {"Authorization": auth} if auth else {}
        fallback_sources.append(
            {
                "title": "direct-play-fallback-{}".format(idx),
                "nzb_url": "",
                "job_name": "direct-play-fallback-{}".format(idx),
                "nzo_id": "direct-play-fallback-{}".format(idx),
                "stream_url": url,
                "stream_headers": stream_headers,
                "content_length": length,
            }
        )

    xbmc.log(
        "NZB-DAV: /direct_play primary={} fallbacks={}".format(
            primary_url[:120], len(fallback_sources)
        ),
        xbmc.LOGINFO,
    )

    primary_headers = {"Authorization": primary_auth} if primary_auth else {}
    service_port, prepare_token = _direct_playback_service_config()
    prepared = _prepare_direct_playback(
        primary_url,
        primary_headers,
        fallback_sources=fallback_sources,
        service_port=service_port,
        prepare_token=prepare_token,
    )
    if not prepared:
        xbmc.log("NZB-DAV: /direct_play prepare returned no payload", xbmc.LOGERROR)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return
    proxy_url = prepared.get("playback_url") or prepared.get("proxy_url")
    if not proxy_url:
        xbmc.log(
            "NZB-DAV: /direct_play prepared payload missing proxy URL: keys={}".format(
                list(prepared.keys())
            ),
            xbmc.LOGERROR,
        )
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return
    xbmc.log(
        "NZB-DAV: /direct_play handing Kodi proxy URL: {}".format(proxy_url[:160]),
        xbmc.LOGINFO,
    )
    listitem = xbmcgui.ListItem(path=proxy_url)
    listitem.setMimeType("video/x-matroska")
    listitem.setContentLookup(False)
    xbmcplugin.setResolvedUrl(handle, True, listitem)


def _handle_play(handle, params):
    """
    Handle a play request from TMDBHelper by searching configured providers
    for matching NZB releases and resolving the chosen item for playback.

    Performs provider search (with caching), shows progress and results
    dialogs, applies filtering and optional auto-selection, and ultimately
    resolves the selected NZB via Kodi's resolver pipeline or marks the
    request as not resolved when cancelled or no selection is made.

    Parameters:
        handle (int): Kodi plugin handle used to report a resolved URL or to
            end the request.
        params (dict): Query parameters from the plugin URL (e.g., "type",
            "title", "year", "imdb", "season", "episode"); TMDBHelper may
            provide "_" placeholders which are normalized.
    """
    import xbmcgui
    import xbmcplugin

    from resources.lib.cache import get_cached, set_cached
    from resources.lib.http_util import notify

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
            # "0" is a real season (specials) and episode (pilot/E0)
            # value — only "" / "-1" mean Kodi has no selection. The
            # previous filter dropped specials entirely. TODO.md §H.2-M30.
            if il_s and il_s not in ("", "-1"):
                season = season or il_s
            if il_e and il_e not in ("", "-1"):
                episode = episode or il_e
            if il_t and not title:
                title = il_t
            if season and episode:
                # Only log the winning source; logging every probed source
                # in the success path made a noisy 4-line log entry per play.
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

    xbmc.log(
        "NZB-DAV: Search stage: checking cache for '{}' ({})".format(
            title, search_type
        ),
        xbmc.LOGDEBUG,
    )

    cache_kwargs = dict(year=year, imdb=imdb, season=season, episode=episode)
    results = get_cached(search_type, title, **cache_kwargs)

    if results is None:
        xbmc.log(
            "NZB-DAV: Search stage: querying providers for '{}'".format(title),
            xbmc.LOGDEBUG,
        )
        results, search_error = _search_all_providers(
            search_type, title, year=year, imdb=imdb, season=season, episode=episode
        )
        if search_error:
            xbmc.log(
                "NZB-DAV: Search stage: provider error — {}".format(search_error),
                xbmc.LOGWARNING,
            )
            _show_error_dialog(search_error)
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
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
        notify(_addon_name(), _fmt(30087, title), 3000)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    xbmc.log(
        "NZB-DAV: Search stage: filtering {} results for '{}'".format(
            len(results), title
        ),
        xbmc.LOGDEBUG,
    )

    from resources.lib.filter import filter_results

    total_count = len(results)
    filtered, all_parsed = filter_results(results)

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

    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    if _get_addon_setting(addon, "auto_select_best", "false").lower() == "true":
        best = filtered[0]
        from resources.lib.resolver import resolve

        resolver_params = {
            "nzburl": best["link"],
            "title": best["title"],
            "_fallback_candidates": [],
            "_fallback_candidate_loader": _fallback_candidate_loader_for_selection(
                best, filtered
            ),
        }
        _attach_selected_indexer(resolver_params, best)
        resolve(
            handle,
            resolver_params,
        )
        return

    # Tag results already downloaded in nzbdav
    completed_jobs = _tag_available(filtered)

    # Show custom results dialog
    from resources.lib.results_dialog import show_results_dialog

    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )

    if selected:
        from resources.lib.resolver import resolve

        resolver_params = {
            "nzburl": selected["link"],
            "title": selected["title"],
            "_fallback_candidates": [],
            "_fallback_candidate_loader": _fallback_candidate_loader_for_selection(
                selected, filtered
            ),
        }
        completed_job = selected.get("_completed_job")
        if completed_job:
            resolver_params["_completed_job"] = completed_job
        elif _completed_lookup_was_done(completed_jobs):
            resolver_params["_completed_job_lookup_done"] = True
        _attach_selected_indexer(resolver_params, selected)
        resolve(
            handle,
            resolver_params,
        )
    else:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())


def _handle_search(handle, params):
    """
    Perform a provider search for the given query, display results in the
    full-screen results dialog, and handle selection or auto-resolve.

    Performs a cached search across enabled providers, applies filtering,
    optionally prompts to show unfiltered results, tags already-downloaded
    items, and either auto-resolves the best match or presents a results
    dialog for user selection. Ensures the plugin directory is ended to avoid
    Kodi hanging.

    Parameters:
        handle (int): Kodi plugin handle provided by the caller (sys.argv[1]).
        params (dict): Route query parameters (e.g., keys: "type", "title",
            "year", "imdb", "season", "episode", "tmdb_id").
    """
    import xbmcaddon
    import xbmcplugin

    from resources.lib.cache import get_cached, set_cached
    from resources.lib.filter import filter_results

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
            "NZB-DAV: Search stage: querying providers for '{}'".format(title),
            xbmc.LOGDEBUG,
        )
        results, search_error = _search_all_providers(
            search_type, title, year=year, imdb=imdb, season=season, episode=episode
        )
        if search_error:
            xbmc.log(
                "NZB-DAV: Search stage: provider error — {}".format(search_error),
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
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    if (
        _get_addon_setting(addon, "auto_select_best", "false").lower() == "true"
        and filtered
    ):
        best = filtered[0]
        from resources.lib.resolver import resolve_and_play

        resolver_params = dict(params)
        resolver_params["_fallback_candidates"] = []
        fallback_loader = _fallback_candidate_loader_for_selection(best, filtered)
        resolver_params["_fallback_candidate_loader"] = fallback_loader
        _attach_selected_indexer(resolver_params, best)
        resolve_and_play(best["link"], best["title"], params=resolver_params)
        # Same hang class as C1 (router.py): /search is a directory
        # route, so Kodi blocks until endOfDirectory fires. Without
        # this, the auto-select branch returned silently and Kodi
        # waited forever for a directory listing that never came.
        # Mark the directory as not-succeeded since playback already
        # ran via resolve_and_play.
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    # Tag results already downloaded in nzbdav
    completed_jobs = _tag_available(filtered)

    # Show custom results dialog
    from resources.lib.results_dialog import show_results_dialog

    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )

    if selected:
        from resources.lib.resolver import resolve_and_play

        resolver_params = dict(params)
        resolver_params["_fallback_candidates"] = []
        resolver_params["_fallback_candidate_loader"] = (
            _fallback_candidate_loader_for_selection(selected, filtered)
        )
        completed_job = selected.get("_completed_job")
        if completed_job:
            resolver_params["_completed_job"] = completed_job
        elif _completed_lookup_was_done(completed_jobs):
            resolver_params["_completed_job_lookup_done"] = True
        _attach_selected_indexer(resolver_params, selected)
        resolve_and_play(selected["link"], selected["title"], params=resolver_params)

    # Must end the directory or Kodi hangs
    xbmcplugin.endOfDirectory(handle, succeeded=False)


def _handle_script_play(params):
    """
    Run the TMDBHelper player flow from a RunScript action.

    This path intentionally avoids plugin handle APIs. On CoreELEC/Kodi 21,
    asking Kodi to open plugin://plugin.video.nzbdav/... as a playable URL can
    crash before this addon's router is invoked. RunScript enters Python
    directly, shows the NZB picker, then starts playback via resolve_and_play().
    """
    from resources.lib.filter import filter_results
    from resources.lib.http_util import notify

    params = _clean_params(params)
    search_type = params.get("type", "movie")
    title = params.get("title", "")
    year = params.get("year", "")
    imdb = params.get("imdb", "")
    season = params.get("season", "") or params.get("ep_season", "")
    episode = params.get("episode", "") or params.get("ep_episode", "")

    xbmc.log(
        "NZB-DAV: Script play route: type={!r} title={!r} imdb={!r} "
        "tmdb_id={!r}".format(search_type, title, imdb, params.get("tmdb_id", "")),
        xbmc.LOGINFO,
    )
    _script_play_stage(
        "route type={!r} title={!r} imdb={!r} tmdb_id={!r}".format(
            search_type, title, imdb, params.get("tmdb_id", "")
        )
    )

    if search_type == "episode" and imdb and not title:
        looked_up = _lookup_episode_info(imdb, params.get("tmdb_id", ""))
        if looked_up:
            title = looked_up.get("title", title)
            season = season or looked_up.get("season", "")
            episode = episode or looked_up.get("episode", "")

    _script_play_stage(
        "skipping cache for '{}' ({})".format(
            title,
            search_type,
        )
    )
    _script_play_stage(
        "provider search start for '{}'".format(title),
    )
    results, search_error = _search_all_providers(
        search_type,
        title,
        year=year,
        imdb=imdb,
        season=season,
        episode=episode,
        settings_getter=_get_script_setting,
    )
    _script_play_stage("provider search done count={}".format(len(results or [])))
    if search_error:
        xbmc.log(
            "NZB-DAV: Search stage: provider error - {}".format(search_error),
            xbmc.LOGWARNING,
        )
        _show_error_dialog(search_error)
        return

    if not results:
        xbmc.log(
            "NZB-DAV: Search stage: no results found for '{}'".format(title),
            xbmc.LOGINFO,
        )
        notify(_addon_name(), _fmt(30087, title), 3000)
        return

    total_count = len(results)
    _script_play_stage("filter start count={} for '{}'".format(len(results), title))
    filtered, all_parsed = filter_results(results, settings_getter=_get_script_setting)
    _script_play_stage(
        "filter done filtered={} parsed={}".format(
            len(filtered or []), len(all_parsed or [])
        )
    )

    if not filtered:
        if all_parsed:
            import xbmcgui

            choice = xbmcgui.Dialog().yesno(
                _addon_name(),
                "All {} results were filtered out. Show unfiltered?".format(
                    len(all_parsed)
                ),
            )
            if choice:
                filtered = all_parsed
            else:
                return
        else:
            notify(_addon_name(), _fmt(30087, title), 3000)
            return

    if _get_script_setting("auto_select_best", "false").lower() == "true" and filtered:
        best = filtered[0]
        from resources.lib.resolver import resolve_and_play

        resolver_params = dict(params)
        resolver_params["_fallback_candidates"] = []
        fallback_loader = _fallback_candidate_loader_for_selection(
            best, filtered, settings_getter=_get_script_setting
        )
        resolver_params["_fallback_candidate_loader"] = fallback_loader
        completed_job = _script_completed_job_for_selection(best)
        if completed_job:
            resolver_params["_completed_job"] = completed_job
        else:
            resolver_params["_completed_job_lookup_done"] = True
        resolver_params["_settings_getter"] = _get_script_setting
        _attach_selected_indexer(resolver_params, best)
        _script_play_stage("resolve start '{}'".format(best.get("title", "")))
        resolve_and_play(best["link"], best["title"], params=resolver_params)
        _script_play_stage("resolve returned")
        return

    _script_play_stage("tag available skipped")

    from resources.lib.results_dialog import show_results_dialog

    _script_play_stage("picker open")
    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )
    if not selected:
        _script_play_stage("picker cancelled")
        return
    _script_play_stage("picker selected")

    from resources.lib.resolver import resolve_and_play

    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    fallback_loader = _fallback_candidate_loader_for_selection(
        selected, filtered, settings_getter=_get_script_setting
    )
    resolver_params["_fallback_candidate_loader"] = fallback_loader
    resolver_params["_completed_job_lookup_done"] = True
    resolver_params["_settings_getter"] = _get_script_setting
    completed_job = selected.get(
        "_completed_job"
    ) or _script_completed_job_for_selection(selected)
    if completed_job:
        resolver_params.pop("_completed_job_lookup_done", None)
        resolver_params["_completed_job"] = completed_job
    _attach_selected_indexer(resolver_params, selected)
    _script_play_stage("resolve start '{}'".format(selected.get("title", "")))
    resolve_and_play(selected["link"], selected["title"], params=resolver_params)
    _script_play_stage("resolve returned")


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
    if not imdb_id or not _IMDB_ID_RE.match(imdb_id):
        return ""
    try:
        import json
        from urllib.request import urlopen

        # Use TMDB's find endpoint (no API key needed for basic lookups via v3)
        # Fall back to a free poster service
        url = "https://v2.sg.media-imdb.com/suggestion/t/{}.json".format(imdb_id)
        try:
            # nosemgrep
            with urlopen(  # nosec B310 — IMDB suggestion API (trusted)
                url, timeout=3
            ) as resp:
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
        except Exception as e:  # pylint: disable=broad-except
            # Poster lookup is best-effort — the TMDBHelper panel already
            # has its own artwork so a miss here is not user-visible. But
            # silently swallowing with no log made this branch impossible
            # to diagnose when the IMDb suggestion API changes shape.
            xbmc.log(
                "NZB-DAV: TMDB poster lookup failed for {}: {}".format(imdb_id, e),
                xbmc.LOGDEBUG,
            )

        return ""
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: TMDB poster lookup aborted for {}: {}".format(imdb_id, e),
            xbmc.LOGDEBUG,
        )
        return ""


def _test_connection(label, url, test_url, ok_condition):
    """Test a service connection and notify the user of the result.

    If url is empty, notifies "<label> URL not configured". Otherwise
    issues a GET to test_url, notifies "<label> connection OK" when
    ok_condition(response) is True, "<label>: unexpected response" when
    False, and "<label>: <error>" (truncated to 60 chars) on exception.
    """
    from resources.lib.http_util import http_get, notify, redact_url

    if not url:
        notify(_addon_name(), "{} URL not configured".format(label), 3000)
        return
    try:
        response = http_get(test_url)
        if ok_condition(response):
            notify(_addon_name(), "{} connection OK".format(label), 3000)
        else:
            notify(_addon_name(), "{}: unexpected response".format(label), 5000)
    except Exception as e:
        # urllib exceptions often embed the full URL (with apikey!) in
        # str(e). The verbatim-URL substitution catches the most common
        # case; ``redact_text`` handles the residue (apikey embedded in
        # an error phrase, percent-encoded variants, etc.) — TODO.md §H.2-M31.
        from resources.lib.http_util import redact_text

        err_msg = str(e).replace(test_url, redact_url(test_url))
        err_msg = redact_text(err_msg)
        notify(_addon_name(), "{}: {}".format(label, err_msg[:60]), 5000)


def _json_object(response):
    """Parse a JSON object response, returning an empty dict on bad shape."""
    import json

    try:
        data = json.loads(response)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _xml_root_name(response):
    """Return the unqualified root XML tag name, lowercased."""
    import xml.etree.ElementTree as ET  # nosec B405 - trusted service response

    try:
        root = ET.fromstring(response)  # nosec B314 - trusted service response
    except (TypeError, ET.ParseError):
        return ""
    return root.tag.rsplit("}", 1)[-1].lower()


def _hydra_search_response_ok(response):
    """True when NZBHydra/Newznab returned an authenticated search RSS payload."""
    return _xml_root_name(response) == "rss"


def _nzbdav_queue_response_ok(response):
    """True when nzbdav returned an authenticated queue payload."""
    data = _json_object(response)
    return isinstance(data.get("queue"), dict)


def _prowlarr_indexers_response_ok(response):
    """True when Prowlarr returned the authenticated indexer list."""
    import json

    try:
        data = json.loads(response)
    except (TypeError, ValueError):
        return False
    return isinstance(data, list)


def _test_hydra_connection():
    """Test NZBHydra2 connection and API-key auth with a lightweight search."""
    import xbmcaddon

    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    url = addon.getSetting("hydra_url").rstrip("/")
    api_key = addon.getSetting("hydra_api_key")
    params = {
        "apikey": api_key,
        "t": "search",
        "q": "__nzbdav_connection_test__",
        "o": "xml",
        "limit": "1",
    }
    test_url = "{}/api?{}".format(url, urlencode(params))
    _test_connection("NZBHydra", url, test_url, _hydra_search_response_ok)


def _test_prowlarr_connection():
    """Test Prowlarr connection by hitting the indexer endpoint."""
    import xbmcaddon

    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    host = addon.getSetting("prowlarr_host").rstrip("/")
    api_key = addon.getSetting("prowlarr_api_key")

    test_url = "{}/api/v1/indexer?apikey={}".format(host, api_key)
    _test_connection("Prowlarr", host, test_url, _prowlarr_indexers_response_ok)


def _test_webdav_connection():
    """Test WebDAV reachability and credentials with the shared probe."""
    from resources.lib.http_util import notify
    from resources.lib.webdav import probe_webdav_reachable

    reachable, error = probe_webdav_reachable(max_retries=0)
    if reachable:
        notify(_addon_name(), _string(30189), 3000)
    elif error == "auth_failed":
        notify(_addon_name(), _string(30190), 5000)
    elif error == "server_error":
        notify(_addon_name(), _string(30191), 5000)
    else:
        notify(_addon_name(), _string(30192), 5000)


def _test_direct_indexers_connection():
    """Test configured direct Newznab indexer caps endpoints."""
    from resources.lib.direct_indexers import test_configured_indexers
    from resources.lib.http_util import notify

    ok_count, total_count, errors = test_configured_indexers()
    if total_count == 0:
        notify(_addon_name(), _string(30176), 3000)
    elif ok_count == total_count:
        notify(_addon_name(), _fmt(30177, ok_count, total_count), 3000)
    else:
        notify(_addon_name(), _fmt(30178, errors[0] if errors else "unknown"), 5000)


def _test_nzbdav_connection():
    """Test nzbdav connection and API-key auth by reading the queue."""
    import xbmcaddon

    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    url = addon.getSetting("nzbdav_url").rstrip("/")
    api_key = addon.getSetting("nzbdav_api_key")
    params = {
        "mode": "queue",
        "start": "0",
        "limit": "0",
        "apikey": api_key,
        "output": "json",
    }
    test_url = "{}/api?{}".format(url, urlencode(params))
    _test_connection("nzbdav", url, test_url, _nzbdav_queue_response_ok)


def _handle_main_menu(handle):
    """Show main menu with settings and install player options."""
    import xbmcgui
    import xbmcplugin

    li = xbmcgui.ListItem(label=_string(30011))
    url = "plugin://plugin.video.nzbdav/install_player"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label=_string(30160))
    url = "plugin://plugin.video.nzbdav/install_player_other"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label=_string(30091))
    url = "plugin://plugin.video.nzbdav/clear_cache"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    li = xbmcgui.ListItem(label=_string(30092))
    url = "plugin://plugin.video.nzbdav/settings"
    xbmcplugin.addDirectoryItem(handle=handle, url=url, listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(handle)
