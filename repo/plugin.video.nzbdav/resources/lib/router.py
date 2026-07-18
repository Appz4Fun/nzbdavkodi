# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# This module is the addon's public router surface: cohesive helper groups live
# in sibling ``router_*`` modules and are re-imported here so the test suite's
# ``from resources.lib.router import <name>`` imports and
# ``@patch("resources.lib.router.<name>")`` decorators keep resolving. The moved
# helpers reach back via a function-local ``import resources.lib.router as
# _router`` (call-time resolution), so the re-exports are deliberately unused
# within this file and the back-edges are provably function-local.
# pylint: disable=cyclic-import,unused-import

"""URL routing for plugin:// calls from Kodi / TMDBHelper."""

import os
import re
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import xbmc
import xbmcaddon  # noqa: F401  # re-exported; router_conn uses _router.xbmcaddon
import xbmcgui
import xbmcplugin

# telemetry / downloaded_pubdate_epochs / the fallback_streams symbols / format_size
# are kept here as router attributes even where this module no longer references
# them directly: the suite patches several via ``resources.lib.router.<name>`` and
# the moved sibling helpers reach them through that namespace at call time.
from resources.lib import telemetry  # noqa: F401
from resources.lib.download_ledger import downloaded_pubdate_epochs  # noqa: F401
from resources.lib.fallback_streams import (  # noqa: F401
    FALLBACK_CANDIDATES_DISABLED,
    attach_fallback_candidates_for_selection,
    cached_selection_pool_first_peer,
    fallback_candidate_prefetch_enabled,
    fallback_candidate_prefetch_settings,
    selected_manifest_may_have_fallback_peer,
    selection_pool_may_have_fallback_peer,
)
from resources.lib.http_util import format_size as _format_size  # noqa: F401
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string
from resources.lib.nzbdav_api import get_completed_jobs

# Cohesive helper groups split into sibling modules to keep this router below
# Codacy's 500-NLOC file gate. Re-exported here so the test suite's
# ``from resources.lib.router import <name>`` imports and
# ``@patch("resources.lib.router.<name>")`` decorators keep resolving; the
# moved helpers reach back into this module via a function-local
# ``import resources.lib.router as _router`` (call-time resolution preserves
# the patches and avoids a top-level import cycle).
from resources.lib.router_conn import (  # noqa: F401
    _hydra_search_response_ok,
    _json_object,
    _nzbdav_queue_response_ok,
    _prowlarr_indexers_response_ok,
    _test_connection,
    _test_direct_indexers_connection,
    _test_hydra_connection,
    _test_nzbdav_connection,
    _test_nzbget_connection,
    _test_nzbget_smb,
    _test_prowlarr_connection,
    _test_webdav_connection,
    _xml_root_name,
)
from resources.lib.router_directplay import (  # noqa: F401
    _direct_play_fallback_sources,
    _direct_play_head_length,
    _direct_play_parse_fallback_urls,
    _direct_play_prepare_and_serve,
    _direct_play_proxy_url,
    _direct_play_split_auth,
    _head_content_length,
)
from resources.lib.router_dispatch import (  # noqa: F401
    _dispatch_action_route,
    _parse_route_argv,
    _redact_route_params,
    _route_clear_cache,
    _route_configure_excluded_groups,
    _route_configure_preferred_groups,
    _route_install_player,
    _route_install_player_other,
    _route_manage_indexers,
    _route_resolve,
    _self_resolving_route,
)
from resources.lib.router_episodeinfo import (  # noqa: F401
    _episode_info_from_infolabels,
    _episode_info_from_listitem,
    _infolabel_backfill_from_source,
    _listitem_episode_candidate,
    _numeric_infolabel,
    _scan_listitem_episode_sources,
)
from resources.lib.router_fallback import (  # noqa: F401
    _augmented_pool_and_first_peer,
    _compute_fallback_candidates,
    _fetch_fallback_extra_uploads,
    _has_extra_uploads,
    _pool_has_no_fallback_peer,
    _resolve_fallback_prefetch_settings,
    _resolve_first_peer_no_extras,
    _resolve_known_first_peer,
    _selection_pool_with_peer_first,
)
from resources.lib.router_play import (  # noqa: F401
    _apply_completed_job_hint,
    _attach_episode_context,
    _attach_nzbget_dupe,
    _ensure_nzbget_completed_hint,
    _extract_search_params,
    _filtered_or_prompt,
    _handle_play_auto_select,
    _handle_play_filter_and_select,
    _handle_play_resolve_selection,
    _handle_search_auto_select,
    _handle_search_filter_and_select,
    _handle_search_resolve_selection,
    _handle_search_tag_and_picker,
    _identity_from_params,
    _lookup_search_episode_args,
    _play_identity,
    _prepend_pack,
    _provider_rows,
    _query_and_cache_providers,
    _resolve_play_episode_args,
    _search_with_cache,
    _season_pack_result,
    _selection_fallback_loader,
    _selection_target,
)
from resources.lib.router_poster import (  # noqa: F401
    _fetch_imdb_suggestion_poster,
    _format_info_line,
    _get_tmdb_poster,
    _lookup_episode_info,
)
from resources.lib.router_scriptplay import (  # noqa: F401
    _script_play_auto_select,
    _script_play_filter_autoselect_tag,
    _script_play_filtered_or_prompt,
    _script_play_log_route,
    _script_play_picker_and_resolve,
    _script_play_recover_episode_info,
    _script_play_resolve_episode_args,
    _script_play_resolve_selected,
    _script_play_search_filter_tag,
    _script_play_search_results,
    _script_play_tag_available,
    _write_back_episode_params,
)
from resources.lib.router_search import (  # noqa: F401
    _PROVIDER_SEARCH_SETTING_DEFAULTS,
    _PUBDATE_MATCH_TOLERANCE_SECONDS,
    _build_provider_jobs,
    _completed_job_matches_result,
    _completed_lookup_was_done,
    _dedupe_results_by_link,
    _hydra_duplicate_lookup_enabled,
    _LookupDoneJobs,
    _nzbget_mode_enabled,
    _provider_error_message,
    _result_pubdate_consistent_with_downloads,
    _result_size_bytes,
    _run_provider_jobs,
    _tag_available_nzbget,
)
from resources.lib.router_settings import (  # noqa: F401
    _addon_instance,
    _close_loading_dialog,
    _get_addon_setting,
    _open_loading_dialog,
    _resolve_episode_tvdb_id,
    _script_settings_paths,
    _script_stage_paths,
    _settings_getter_or_addon_default,
    _show_error_dialog,
    _snapshot_settings_getter,
    _translate_path,
    _update_loading_dialog,
)

_ORIGINAL_URLOPEN = urlopen

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
    parsed_argv = _parse_route_argv(argv)
    if parsed_argv is None:
        return
    base_url, handle, query_string = parsed_argv

    path = parse_route(base_url)
    params = parse_params(query_string)

    safe_params = _redact_route_params(params)
    xbmc.log(
        "NZB-DAV: Routing path='{}' params={}".format(path, safe_params), xbmc.LOGDEBUG
    )

    # /play, /search, /direct_play, and the main menu call setResolvedUrl /
    # endOfDirectory themselves and return early. Everything else is an
    # "action route" that runs a side-effect and then falls through to
    # _safe_resolve_handle so Kodi receives a resolution signal.
    try:
        self_resolving = _self_resolving_route(path)
        if self_resolving is not None:
            self_resolving(handle, params)
            return
        _dispatch_action_route(path, params)
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
        result_count = len(results)
    except TypeError:
        result_count = None
    if result_count == 1 and not _hydra_duplicate_lookup_enabled(
        selected, settings_getter=settings_getter
    ):
        return None

    def _load_fallback_candidates():
        # Multi-result distinct-peer scans can walk the full picker pool. Keep
        # them inside the loader so resolver can start the primary submit first.
        return _compute_fallback_candidates(
            selected, results, result_count, settings_getter
        )

    return _load_fallback_candidates


def _attach_selected_result_metadata(resolver_params, selected):
    """Thread metadata from the chosen result into the resolver params.

    Beyond the indexer label, this carries the release's ``pubdate`` and
    ``size`` so that, on a fresh submit, the resolver can record the
    download's Usenet post-date (see ``download_ledger``). That lets a later
    picker render tell THIS download apart from a same-name repost posted on
    a different day. Absent fields are left unset so the resolver fails open.
    """
    if not isinstance(selected, dict):
        return
    indexer = str(selected.get("indexer", "") or "").strip()
    if indexer:
        resolver_params["_selected_indexer"] = indexer
    pubdate = selected.get("pubdate")
    if pubdate:
        resolver_params["_download_pubdate"] = pubdate
    size = selected.get("size")
    if size:
        resolver_params["_download_size"] = size
    # NZBGet-mode reuse hint set at picker-tag time (_tag_available_nzbget):
    # lets the NZBGet resolver play the already-completed files instead of
    # re-submitting into NZBGet's duplicate check.
    nzbget_job = selected.get("_nzbget_completed_job")
    if nzbget_job:
        resolver_params["_nzbget_completed_job"] = nzbget_job


def _get_script_setting(key, default=""):
    """Read this addon's setting from settings.xml without Kodi settings APIs.

    Mirrors the ``xbmcaddon`` binding's semantics: the user-saved value
    when present, else the SCHEMA default from resources/settings.xml —
    Kodi's profile XML omits settings left at their displayed defaults,
    so returning ``default`` for those would silently flip filter
    toggles and blank out the default nzbdav URL on the disk-read
    paths. ``default`` only applies when the key has no non-empty
    schema default either.
    """
    from resources.lib.xml_safety import ParseError, safe_fromstring

    for settings_path in _script_settings_paths():
        try:
            with open(settings_path, "rb") as fh:
                xml_bytes = fh.read()
            root = safe_fromstring(xml_bytes)
        except (OSError, ParseError, ValueError):
            continue

        for setting in root.findall(".//setting"):
            if setting.get("id") != key:
                continue
            value = setting.text
            return value if isinstance(value, str) else default
    try:
        from resources.lib.fallback_streams import _schema_setting_default

        schema_default = _schema_setting_default(key)
    except Exception:  # pylint: disable=broad-except
        schema_default = ""
    return schema_default if schema_default else default


def _script_completed_job_for_selection(selected):
    """Look up completed-history metadata for a RunScript picker selection.

    Gated by size AND pubdate the same way ``_tag_available`` is: nzbdav history
    is keyed by NAME, so a name-only match would reuse the wrong cached stream
    for a distinct same-filename upload. Only return the completed job when its
    size matches the selection (fail-open on unknown size) and the selection's
    pubdate is consistent with what we downloaded under that name (fail-open
    when unknown), so a same-name same-size repost from a different day isn't
    reused.
    """
    title = selected.get("title", "") if isinstance(selected, dict) else ""
    if not title:
        return None
    try:
        from resources.lib.nzbdav_api import find_completed_by_name

        job = find_completed_by_name(title, settings_getter=_get_script_setting)
        if (
            job
            and _completed_job_matches_result(selected, job)
            and _result_pubdate_consistent_with_downloads(selected)
        ):
            return job
        return None
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Script completed lookup failed for '{}': {}".format(title, error),
            xbmc.LOGDEBUG,
        )
        return None


def _search_all_providers(query, settings_getter=None):
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
    search_type = query.search_type
    title = query.title
    year = query.year
    imdb = query.imdb
    season = query.season
    episode = query.episode
    tvdb = query.tvdb
    tmdb_id = query.tmdb_id
    settings_getter = _settings_getter_or_addon_default(settings_getter)

    # Provider defaults mirror settings.xml. Runtime setting read failures still
    # use the explicit defaults passed through _get_addon_setting above.
    nzbhydra_enabled = settings_getter("nzbhydra_enabled", "false").lower() == "true"
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

    tvdb = _resolve_episode_tvdb_id(search_type, tvdb, tmdb_id, imdb, settings_getter)

    provider_settings_getter = _snapshot_settings_getter(
        settings_getter, _PROVIDER_SEARCH_SETTING_DEFAULTS
    )
    search_args = (search_type, title)
    common_kwargs = {
        "year": year,
        "imdb": imdb,
        "season": season,
        "episode": episode,
        "tvdb": tvdb,
    }
    provider_jobs = _build_provider_jobs(
        nzbhydra_enabled,
        prowlarr_enabled,
        direct_indexers_enabled,
        search_args,
        common_kwargs,
        provider_settings_getter,
    )

    provider_outcomes = _run_provider_jobs(provider_jobs)
    return _collect_provider_outcomes(provider_outcomes)


def _collect_provider_outcomes(provider_outcomes):
    """Merge provider outcomes into deduplicated results plus a first error.

    Logs each provider failure, dedupes surviving results by ``link``, and
    returns ``(deduped, error_message)`` where ``error_message`` is the first
    collected provider error only when every provider produced no surviving
    result; otherwise ``None``.
    """
    all_results = []
    errors = []
    for provider_label, outcome in provider_outcomes:
        provider_results, provider_error = outcome
        if provider_error:
            xbmc.log(
                "NZB-DAV: {} search error: {}".format(provider_label, provider_error),
                xbmc.LOGWARNING,
            )
            errors.append(provider_error)
        else:
            all_results.extend(provider_results)

    deduped = _dedupe_results_by_link(all_results)

    if not deduped and errors:
        return [], errors[0]

    return deduped, None


def _tag_available(results, settings_getter=None):
    """
    Mark result entries that already exist in the active download backend by
    setting the `_available` flag.

    Parameters:
        results (list[dict]): Iterable of result dictionaries; entries whose
            `"title"` matches a completed name in nzbdav AND whose size matches
            that completed download (see ``_completed_job_matches_result``) are
            modified in-place with `result["_available"] = True`. The size gate
            stops distinct uploads that merely share a filename from being
            collapsed onto one cached stream.

    In NZBGet mode the completed-name source is NZBGet's own SUCCESS history
    instead of nzbdav's (see ``_tag_available_nzbget``); nzbdav is not queried.
    """
    if not results:
        return {}
    if _nzbget_mode_enabled(settings_getter):
        return _tag_available_nzbget(results, settings_getter=settings_getter)
    completed = get_completed_jobs(settings_getter=settings_getter)
    if not completed:
        return completed
    for result in results:
        completed_job = completed.get(result.get("title"))
        if (
            completed_job
            and _completed_job_matches_result(result, completed_job)
            and _result_pubdate_consistent_with_downloads(result)
        ):
            result["_available"] = True
            result["_completed_job"] = completed_job
    return completed


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
    # Reject non-http(s) URLs before any HEAD: urlopen will happily
    # dereference file:// (reading arbitrary local files) and ftp://,
    # and a junk scheme can throw deep inside urllib. _validate_url
    # is shared with stream_proxy so the policy stays consistent.
    from resources.lib.stream_proxy import _validate_url

    primary_url_raw = params.get("primary_url", "")
    fallback_urls_raw = params.get("fallback_urls", "[]")
    if not primary_url_raw:
        xbmc.log("NZB-DAV: /direct_play missing primary_url", xbmc.LOGERROR)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return
    primary_url, primary_auth = _direct_play_split_auth(primary_url_raw)
    fallback_urls = _direct_play_parse_fallback_urls(fallback_urls_raw)

    try:
        _validate_url(primary_url)
    except (ValueError, TypeError):
        xbmc.log(
            "NZB-DAV: /direct_play rejecting non-http(s) primary",
            xbmc.LOGERROR,
        )
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    _primary_len, primary_err = _direct_play_head_length(primary_url, primary_auth)
    if primary_err:
        xbmc.log(
            "NZB-DAV: /direct_play primary HEAD failed: {}".format(primary_err),
            xbmc.LOGERROR,
        )
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    _direct_play_prepare_and_serve(
        handle, primary_url, primary_auth, fallback_urls, _validate_url
    )


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
    from resources.lib.http_util import notify

    params = _clean_params(params)
    search_type, title, year, imdb, tvdb, tmdb_id, season, episode = (
        _extract_search_params(params)
    )

    title, season, episode = _resolve_play_episode_args(
        params, search_type, title, season, episode, imdb
    )

    identity = _play_identity(params, title, season, episode)
    pack_result = _season_pack_result(identity)

    cache_kwargs = dict(
        year=year, imdb=imdb, season=season, episode=episode, tvdb=tvdb, tmdb_id=tmdb_id
    )
    results, search_error = _search_with_cache(search_type, title, cache_kwargs)
    if search_error and pack_result is None:
        _show_error_dialog(search_error)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    if not results and pack_result is None:
        xbmc.log(
            "NZB-DAV: Search stage: no results found for '{}'".format(title),
            xbmc.LOGINFO,
        )
        notify(_addon_name(), _fmt(30087, title), 3000)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    _handle_play_filter_and_select(
        handle,
        results or [],
        title,
        year,
        notify,
        identity,
        pack_result=pack_result,
    )


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
    from resources.lib.http_util import notify

    params = _clean_params(params)
    search_type, title, year, imdb, tvdb, tmdb_id, season, episode = (
        _extract_search_params(params)
    )

    title, season, episode = _lookup_search_episode_args(
        params, search_type, title, season, episode, imdb
    )
    _attach_episode_context(params, params, title=title, season=season, episode=episode)
    pack_result = _season_pack_result(params.get("_episode_context"))

    cache_kwargs = dict(
        year=year, imdb=imdb, season=season, episode=episode, tvdb=tvdb, tmdb_id=tmdb_id
    )
    results, search_error = _search_with_cache(search_type, title, cache_kwargs)
    if search_error and pack_result is None:
        _show_error_dialog(search_error)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    if not results and pack_result is None:
        xbmc.log(
            "NZB-DAV: Search stage: no results found for '{}'".format(title),
            xbmc.LOGINFO,
        )
        notify(_addon_name(), _fmt(30087, title), 3000)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    _handle_search_filter_and_select(
        handle,
        params,
        results or [],
        title,
        year,
        notify,
        pack_result=pack_result,
    )


def _handle_script_play(params):
    """
    Run the TMDBHelper player flow from a RunScript action.

    This path intentionally avoids plugin handle APIs. On CoreELEC/Kodi 21,
    asking Kodi to open plugin://plugin.video.nzbdav/... as a playable URL can
    crash before this addon's router is invoked. RunScript enters Python
    directly, shows the NZB picker, then starts playback via resolve_and_play().
    """
    from resources.lib.http_util import notify

    params = _clean_params(params)
    search_type, title, year, imdb, tvdb, tmdb_id, season, episode = (
        _extract_search_params(params)
    )

    _script_play_log_route(params, search_type, title, imdb)

    title, season, episode = _script_play_resolve_episode_args(
        params, search_type, title, season, episode, imdb
    )
    _attach_episode_context(params, params, title=title, season=season, episode=episode)
    pack_result = _season_pack_result(
        params.get("_episode_context"), settings_getter=_get_script_setting
    )

    _script_play_stage(
        "skipping cache for '{}' ({})".format(
            title,
            search_type,
        )
    )
    _script_play_stage(
        "provider search start for '{}'".format(title),
    )
    search_kwargs = dict(
        year=year, imdb=imdb, season=season, episode=episode, tvdb=tvdb, tmdb_id=tmdb_id
    )
    prepared = _script_play_search_filter_tag(
        params,
        search_type,
        title,
        year,
        search_kwargs,
        notify,
        pack_result=pack_result,
    )
    if prepared is None:
        return
    filtered, total_count, completed_jobs = prepared
    _script_play_picker_and_resolve(
        params, filtered, title, year, total_count, completed_jobs
    )


def _handle_main_menu(handle):
    """Show main menu with settings and install player options."""
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
