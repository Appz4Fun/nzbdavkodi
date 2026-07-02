# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Search/select/resolve helpers behind ``_handle_play`` / ``_handle_search``.

``_handle_play`` and ``_handle_search`` stay in ``router`` (the suite imports /
patches them from there); they drive the helpers here. Router-resident or
router-patched names (``_search_all_providers``, ``_tag_available``,
``_fallback_candidate_loader_for_selection``, ``_attach_selected_result_metadata``,
``_lookup_episode_info``, the i18n helpers, …) are reached at call time through
``import resources.lib.router as _router`` so the suite's ``@patch`` decorators
keep resolving and no top-level import cycle is introduced. ``xbmc*`` are global
modules (mocked once in conftest) so they are imported normally.
"""

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin


def _search_with_cache(search_type, title, cache_kwargs):
    """Return ``(results, search_error)`` from cache or a provider query.

    Reads the per-query cache; on a miss, queries all enabled providers (with
    the addon-backed ``nzbhydra_enabled``-forcing settings getter) and caches
    any results. Logging matches the prior inline ``_handle_play`` /
    ``_handle_search`` stages. ``search_error`` is non-empty only on a provider
    failure; a clean empty result returns ``([], None)``.
    """
    from resources.lib.cache import get_cached, set_cached

    xbmc.log(
        "NZB-DAV: Search stage: checking cache for '{}' ({})".format(
            title, search_type
        ),
        xbmc.LOGDEBUG,
    )
    results = get_cached(search_type, title, **cache_kwargs)
    if results is not None:
        xbmc.log(
            "NZB-DAV: Search stage: loaded {} results from cache for '{}'".format(
                len(results), title
            ),
            xbmc.LOGDEBUG,
        )
        return results, None

    return _query_and_cache_providers(search_type, title, cache_kwargs, set_cached)


def _query_and_cache_providers(search_type, title, cache_kwargs, set_cached):
    """Query all enabled providers on a cache miss and cache any results.

    Returns ``(results, search_error)``; caches results only on a clean
    (non-error) non-empty query. Extracted verbatim from ``_search_with_cache``.
    """
    import resources.lib.router as _router
    from resources.lib.search_planner import SearchQuery

    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    xbmc.log(
        "NZB-DAV: Search stage: querying providers for '{}'".format(title),
        xbmc.LOGDEBUG,
    )
    query = SearchQuery(search_type=search_type, title=title, **cache_kwargs)
    results, search_error = _router._search_all_providers(
        query,
        settings_getter=lambda key, default="": (
            "true"
            if key == "nzbhydra_enabled"
            else _router._get_addon_setting(addon, key, default)
        ),
    )
    if search_error:
        xbmc.log(
            "NZB-DAV: Search stage: provider error — {}".format(search_error),
            xbmc.LOGWARNING,
        )
        return results, search_error
    if results:
        xbmc.log(
            "NZB-DAV: Search stage: caching {} results for '{}'".format(
                len(results), title
            ),
            xbmc.LOGDEBUG,
        )
        set_cached(search_type, title, results, **cache_kwargs)
    return results, None


def _filtered_or_prompt(all_parsed, title, notify):
    """Resolve the list to display when filtering removed every result.

    With parsed-but-filtered results, prompts to show them unfiltered and
    returns ``all_parsed`` on yes / ``None`` on no. With nothing parsed,
    notifies "no results" and returns ``None``. The caller treats ``None`` as
    "abort and resolve the handle as a failure".
    """
    import resources.lib.router as _router

    if all_parsed:
        choice = xbmcgui.Dialog().yesno(
            _router._addon_name(),
            "All {} results were filtered out. Show unfiltered?".format(
                len(all_parsed)
            ),
        )
        return all_parsed if choice else None
    notify(_router._addon_name(), _router._fmt(30087, title), 3000)
    return None


def _apply_completed_job_hint(resolver_params, selected, completed_jobs):
    """Thread the picker's completed-history hint into resolver params.

    Carries the matched ``_completed_job`` when present; otherwise, when the
    picker-time history lookup is known to have run, records
    ``_completed_job_lookup_done`` so the resolver skips a redundant re-query.
    """
    import resources.lib.router as _router

    completed_job = selected.get("_completed_job")
    if completed_job:
        resolver_params["_completed_job"] = completed_job
    elif _router._completed_lookup_was_done(completed_jobs):
        resolver_params["_completed_job_lookup_done"] = True


def _extract_search_params(params):
    """Pull the common (search_type, title, year, imdb, tvdb, tmdb_id, season,
    episode) tuple from cleaned route params.

    ``season``/``episode`` fall back to the TMDBHelper ``ep_*`` aliases.
    """
    season = params.get("season", "") or params.get("ep_season", "")
    episode = params.get("episode", "") or params.get("ep_episode", "")
    return (
        params.get("type", "movie"),
        params.get("title", ""),
        params.get("year", ""),
        params.get("imdb", ""),
        params.get("tvdb", ""),
        params.get("tmdb_id", ""),
        season,
        episode,
    )


def _resolve_play_episode_args(params, search_type, title, season, episode, imdb):
    """Backfill episode (title, season, episode) for ``_handle_play``.

    First probes the focused Kodi InfoLabels for a missing season/episode, then
    looks the show title up from IMDB when only an IMDB id is present. Mirrors
    the prior inline behaviour exactly; no-op for non-episode searches.
    """
    import resources.lib.router as _router

    # Fallback: try every possible Kodi InfoLabel source for episode info
    if search_type == "episode" and (not season or not episode):
        title, season, episode = _router._episode_info_from_infolabels(
            title, season, episode
        )

    # If we still have IMDB but no title, look up from IMDB
    if search_type == "episode" and imdb and not title:
        looked_up = _router._lookup_episode_info(imdb, params.get("tmdb_id", ""))
        if looked_up:
            title = looked_up.get("title", title)
    return title, season, episode


def _lookup_search_episode_args(params, search_type, title, season, episode, imdb):
    """Backfill (title, season, episode) from an IMDB lookup for ``_handle_search``.

    When an episode search has an IMDB id but no title, look up the show and
    fill any missing title/season/episode. No-op otherwise. Mirrors the prior
    inline behaviour exactly.
    """
    import resources.lib.router as _router

    if search_type == "episode" and imdb and not title:
        looked_up = _router._lookup_episode_info(imdb, params.get("tmdb_id", ""))
        if looked_up:
            title = looked_up.get("title", title)
            season = season or looked_up.get("season", "")
            episode = episode or looked_up.get("episode", "")
    return title, season, episode


def _handle_play_filter_and_select(handle, results, title, year, notify):
    """Filter, optionally auto-select, tag, and run the picker for ``_handle_play``.

    Resolves the Kodi handle itself (False on abort / no selection, or via the
    auto-select / picker-selection resolvers). Extracted verbatim from the tail
    of ``_handle_play``.
    """
    import resources.lib.router as _router

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
        filtered = _filtered_or_prompt(all_parsed, title, notify)
        if not filtered:
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

    # Auto-select best match if enabled
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    if _router._get_addon_setting(addon, "auto_select_best", "false").lower() == "true":
        _handle_play_auto_select(handle, filtered[0], filtered)
        return

    # Tag results already downloaded in the active backend (nzbdav / NZBGet)
    completed_jobs = _router._tag_available(filtered)

    # Show custom results dialog
    from resources.lib.results_dialog import show_results_dialog

    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )

    if selected:
        _handle_play_resolve_selection(handle, selected, filtered, completed_jobs)
    else:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())


def _handle_play_auto_select(handle, best, filtered):
    """Resolve the auto-selected best release through the handle-based resolver."""
    import resources.lib.router as _router
    from resources.lib.resolver import resolve

    resolver_params = {
        "nzburl": best["link"],
        "title": best["title"],
        "_fallback_candidates": [],
        "_fallback_candidate_loader": _router._fallback_candidate_loader_for_selection(
            best, filtered
        ),
    }
    _router._attach_selected_result_metadata(resolver_params, best)
    resolve(handle, resolver_params)


def _handle_play_resolve_selection(handle, selected, filtered, completed_jobs):
    """Resolve a picker selection through the handle-based resolver."""
    import resources.lib.router as _router
    from resources.lib.resolver import resolve

    resolver_params = {
        "nzburl": selected["link"],
        "title": selected["title"],
        "_fallback_candidates": [],
        "_fallback_candidate_loader": _router._fallback_candidate_loader_for_selection(
            selected, filtered
        ),
    }
    _apply_completed_job_hint(resolver_params, selected, completed_jobs)
    _router._attach_selected_result_metadata(resolver_params, selected)
    resolve(handle, resolver_params)


def _handle_search_filter_and_select(handle, params, results, title, year, notify):
    """Filter, optionally auto-select, tag, and run the picker for ``_handle_search``.

    Always ends the Kodi directory (succeeded=False) so the route never hangs.
    Extracted verbatim from the tail of ``_handle_search``.
    """
    import resources.lib.router as _router
    from resources.lib.filter import filter_results

    total_count = len(results)
    xbmc.log(
        "NZB-DAV: Search stage: filtering {} results for '{}'".format(
            len(results), title
        ),
        xbmc.LOGDEBUG,
    )
    filtered, all_parsed = filter_results(results)

    if not filtered:
        filtered = _filtered_or_prompt(all_parsed, title, notify)
        if not filtered:
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return

    # Auto-select best match if enabled
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    if (
        _router._get_addon_setting(addon, "auto_select_best", "false").lower() == "true"
        and filtered
    ):
        _handle_search_auto_select(params, filtered[0], filtered)
        # Same hang class as C1 (router.py): /search is a directory route, so
        # Kodi blocks until endOfDirectory fires. Mark the directory as
        # not-succeeded since playback already ran via resolve_and_play.
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    _handle_search_tag_and_picker(handle, params, filtered, title, year, total_count)


def _handle_search_tag_and_picker(handle, params, filtered, title, year, total_count):
    """Tag, run the picker, resolve a selection, and end the directory."""
    import resources.lib.router as _router

    # Tag results already downloaded in the active backend (nzbdav / NZBGet)
    completed_jobs = _router._tag_available(filtered)

    # Show custom results dialog
    from resources.lib.results_dialog import show_results_dialog

    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )

    if selected:
        _handle_search_resolve_selection(params, selected, filtered, completed_jobs)

    # Must end the directory or Kodi hangs
    xbmcplugin.endOfDirectory(handle, succeeded=False)


def _handle_search_auto_select(params, best, filtered):
    """Play the auto-selected best release via the params-based resolver."""
    import resources.lib.router as _router
    from resources.lib.resolver import resolve_and_play

    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = (
        _router._fallback_candidate_loader_for_selection(best, filtered)
    )
    _router._attach_selected_result_metadata(resolver_params, best)
    resolve_and_play(best["link"], best["title"], params=resolver_params)


def _handle_search_resolve_selection(params, selected, filtered, completed_jobs):
    """Play a picker selection via the params-based resolver."""
    import resources.lib.router as _router
    from resources.lib.resolver import resolve_and_play

    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = (
        _router._fallback_candidate_loader_for_selection(selected, filtered)
    )
    _apply_completed_job_hint(resolver_params, selected, completed_jobs)
    _router._attach_selected_result_metadata(resolver_params, selected)
    resolve_and_play(selected["link"], selected["title"], params=resolver_params)
