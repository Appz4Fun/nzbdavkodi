# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""RunScript-player (``_handle_script_play``) helpers split out of ``router``.

``_handle_script_play`` stays in ``router`` (the suite imports/patches it from
there) and drives the helpers here. Every router-resident or router-patched
name (``_search_all_providers``, ``_get_script_setting``, ``_tag_available``,
``_script_completed_job_for_selection``, ``_string``, the loading-dialog
helpers, …) is reached at call time through ``import resources.lib.router as
_router`` so the suite's ``@patch("resources.lib.router.<name>")`` decorators
keep resolving and no top-level import cycle is introduced.
"""

import xbmc


def _script_play_recover_episode_info(params, title, season, episode):
    """Backfill (title, season, episode) for a RunScript episode play.

    Recovers the missing season/episode from the focused Kodi ListItem, but
    trusts them only when that item is the same show being searched (focus may
    have moved by the time the player fires). On a same-show match, the
    recovered numbers are also threaded back into ``params`` so the downstream
    ``resolver_params = dict(params)`` carries them into
    ``_clear_kodi_playback_state`` — otherwise the actual SxxExx TMDBHelper
    bookmark that triggered the widget play is left behind and the next replay
    can still hit the stale plugin-URL resume failure.
    """
    import resources.lib.router as _router

    li_show, li_season, li_episode = _router._episode_info_from_listitem(title)
    xbmc.log(
        "NZB-DAV: Episode args missing season/episode; ListItem fallback "
        "show={!r} season={!r} episode={!r} (search title {!r})".format(
            li_show, li_season, li_episode, title
        ),
        xbmc.LOGINFO,
    )
    same_show = bool(li_show) and (
        not title or li_show.strip().lower() == title.strip().lower()
    )
    if not same_show:
        return title, season, episode
    if not title:
        title = li_show
    season = season or li_season
    episode = episode or li_episode
    _write_back_episode_params(params, season, episode)
    return title, season, episode


def _write_back_episode_params(params, season, episode):
    """Thread recovered season/episode back into ``params`` (skip blanks)."""
    if season:
        params["season"] = season
    if episode:
        params["episode"] = episode


def _script_play_resolve_episode_args(
    params, search_type, title, season, episode, imdb
):
    """Backfill (title, season, episode) for a RunScript episode play.

    First looks the show up from IMDB when only an IMDB id is present, then
    recovers a missing season/episode from the focused Kodi ListItem (trusted
    only when the focused item is the same show). Mirrors the prior inline
    behaviour exactly; no-op for non-episode searches.
    """
    import resources.lib.router as _router

    title, season, episode = _router._lookup_search_episode_args(
        params, search_type, title, season, episode, imdb
    )

    # TMDBHelper Next-Up / widget / home-screen plays often invoke the player
    # with only the series ids and empty season/episode, so an episode search
    # broadens to the whole show. Recover the numbers from the focused
    # ListItem, but trust them only when that item is the same show we're
    # about to search (the focus may have moved by the time the player fires).
    if search_type == "episode" and not (season and episode):
        title, season, episode = _script_play_recover_episode_info(
            params, title, season, episode
        )
    return title, season, episode


def _script_play_log_route(params, search_type, title, imdb):
    """Emit the RunScript route entry log + stage line (verbatim split)."""
    import resources.lib.router as _router

    xbmc.log(
        "NZB-DAV: Script play route: type={!r} title={!r} imdb={!r} "
        "tmdb_id={!r}".format(search_type, title, imdb, params.get("tmdb_id", "")),
        xbmc.LOGINFO,
    )
    _router._script_play_stage(
        "route type={!r} title={!r} imdb={!r} tmdb_id={!r}".format(
            search_type, title, imdb, params.get("tmdb_id", "")
        )
    )


def _script_play_picker_and_resolve(
    params, filtered, all_rows, title, year, total_count, completed_jobs
):
    """Open the RunScript picker and resolve the selection (no-op on cancel)."""
    import resources.lib.router as _router
    from resources.lib.results_dialog import show_results_dialog

    _router._script_play_stage("picker open")
    selected = show_results_dialog(
        filtered,
        title=title,
        year=year,
        total_count=total_count,
        all_results=all_rows,
    )
    if not selected:
        _router._script_play_stage("picker cancelled")
        return
    _router._script_play_stage("picker selected")
    _script_play_resolve_selected(params, selected, all_rows, completed_jobs)


def _script_play_search_results(
    search_type, title, search_kwargs, notify, allow_local_pack=False
):
    """Run the RunScript provider search, returning results or ``None`` to stop.

    Shows the provider-error dialog (or the no-results notify) and returns
    ``None`` so the caller stops, exactly as the prior inline block did.
    """
    import resources.lib.router as _router
    from resources.lib.search_planner import SearchQuery

    query = SearchQuery(search_type=search_type, title=title, **search_kwargs)
    results, search_error = _router._search_all_providers(
        query, settings_getter=_router._get_script_setting
    )
    _router._script_play_stage(
        "provider search done count={}".format(len(results or []))
    )
    if search_error:
        xbmc.log(
            "NZB-DAV: Search stage: provider error - {}".format(search_error),
            xbmc.LOGWARNING,
        )
        if not allow_local_pack:
            _router._show_error_dialog(search_error)
            return None
        return []

    if not results:
        xbmc.log(
            "NZB-DAV: Search stage: no results found for '{}'".format(title),
            xbmc.LOGINFO,
        )
        if not allow_local_pack:
            notify(_router._addon_name(), _router._fmt(30087, title), 3000)
            return None
        return []
    return results


def _script_play_search_filter_tag(
    params, search_type, title, year, search_kwargs, notify, pack_result=None
):
    """Search, filter, optionally auto-play, and tag for the RunScript flow.

    Runs the whole non-modal-loading-dialog phase. Returns ``None`` when the
    caller should stop (provider error, no results, or an auto-selected release
    was already played), otherwise the
    ``(filtered, all_rows, total_count, completed_jobs)`` payload for the picker.
    """
    import resources.lib.router as _router

    # The indexer search + filtering below can take several seconds; with no
    # on-screen indicator the player looks frozen/crashed. Show a NON-modal
    # background progress dialog (see _open_loading_dialog — the modal
    # DialogProgress native-crashes Kodi mid-search on CoreELEC/Arctic Fuse).
    # The finally guarantees it is closed before the picker opens and on every
    # early return / exception below.
    loading = _router._open_loading_dialog(title)
    try:
        results = _script_play_search_results(
            search_type,
            title,
            search_kwargs,
            notify,
            allow_local_pack=pack_result is not None,
        )
        if results is None:
            return None
        return _script_play_filter_autoselect_tag(
            loading, params, results, title, notify, pack_result=pack_result
        )
    finally:
        _router._close_loading_dialog(loading)


def _script_play_filter_autoselect_tag(
    loading, params, results, title, notify, pack_result=None
):
    """Filter, optionally auto-play, and tag for the RunScript flow.

    Returns ``None`` when the caller should stop (nothing to show at all, or an
    auto-selected release was already played), otherwise the
    ``(filtered, all_rows, total_count, completed_jobs)`` payload. Zero-survivor
    filtering no longer prompts: the picker opens in show-all mode instead.
    """
    import resources.lib.router as _router
    from resources.lib.filter import filter_results

    total_count = len(results)
    _router._update_loading_dialog(loading, 60, _router._string(30088))
    _router._script_play_stage(
        "filter start count={} for '{}'".format(len(results), title)
    )
    filtered, all_parsed = filter_results(
        results, settings_getter=_router._get_script_setting
    )
    _router._script_play_stage(
        "filter done filtered={} parsed={}".format(
            len(filtered or []), len(all_parsed or [])
        )
    )

    filtered = _script_play_available_rows(
        filtered, all_parsed, title, notify, pack_result
    )
    if filtered is None:
        return None
    provider_row_count = len(filtered)
    filtered = _router._prepend_pack(filtered, pack_result)
    all_rows = _router._prepend_pack(all_parsed, pack_result)
    total_count += len(filtered) - provider_row_count

    if (
        _router._get_script_setting("auto_select_best", "false").lower() == "true"
        and filtered
    ):
        # resolve_and_play blocks on the download; drop the indicator first.
        _router._close_loading_dialog(loading)
        _script_play_auto_select(params, filtered[0], all_rows)
        return None

    # Tag the all-rows superset so DL tags / completed-reuse stay valid for
    # rows revealed by the picker's show-all toggle.
    completed_jobs = _script_play_completed_jobs(_router, all_rows)
    return filtered, all_rows, total_count, completed_jobs


def _script_play_available_rows(filtered, all_parsed, title, notify, pack_result):
    """Return the filtered provider rows, or ``None`` when nothing exists at all.

    Zero-survivor filtering no longer prompts: with parsed-but-filtered
    rows the picker opens in show-all mode instead. ``None`` (stop, with
    the "no results" notify) only when nothing parsed AND no local pack
    row exists.
    """
    if filtered or all_parsed or pack_result is not None:
        return filtered
    import resources.lib.router as _router

    notify(_router._addon_name(), _router._fmt(30087, title), 3000)
    return None


def _script_play_completed_jobs(router_module, filtered):
    """Tag ordinary provider rows while leaving a pack-only picker untouched."""
    providers = router_module._provider_rows(filtered)
    return _script_play_tag_available(providers) if providers else None


def _script_play_tag_available(filtered):
    """Tag already-downloaded results for the RunScript flow (fail-soft)."""
    import resources.lib.router as _router

    try:
        completed_jobs = _router._tag_available(
            filtered, settings_getter=_router._get_script_setting
        )
        _router._script_play_stage("tag available done")
        return completed_jobs
    except Exception as error:  # pylint: disable=broad-except
        from resources.lib.http_util import redact_text

        xbmc.log(
            "NZB-DAV: Script completed-history tagging failed: {}".format(
                redact_text(str(error))
            ),
            xbmc.LOGDEBUG,
        )
        _router._script_play_stage("tag available failed")
        return None


def _script_play_auto_select(params, best, filtered):
    """Build resolver params for the auto-selected best release and play it."""
    import resources.lib.router as _router
    from resources.lib.resolver import resolve_and_play

    target, provider_rows = _router._selection_target(best, filtered)
    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = _router._selection_fallback_loader(
        target, provider_rows, settings_getter=_router._get_script_setting
    )
    completed_job = None
    if not best.get("_season_pack") and not _router._nzbget_mode_enabled(
        _router._get_script_setting
    ):
        # In NZBGet mode the nzbdav completed-history hint is dead weight
        # (resolve_and_play delegates to NZBGet before reading it) — skip the
        # lookup instead of stalling on a stale nzbdav config.
        completed_job = _router._script_completed_job_for_selection(target)
    if completed_job:
        resolver_params["_completed_job"] = completed_job
    else:
        resolver_params["_completed_job_lookup_done"] = True
    resolver_params["_settings_getter"] = _router._get_script_setting
    _router._attach_nzbget_dupe(
        resolver_params,
        target,
        provider_rows,
        _router._identity_from_params(params),
    )
    _router._ensure_nzbget_completed_hint(target, _router._get_script_setting)
    _router._attach_selected_result_metadata(resolver_params, target)
    if best.get("_season_pack"):
        resolver_params["_season_pack"] = best["_season_pack"]
    _router._script_play_stage("resolve start '{}'".format(target.get("title", "")))
    resolve_and_play(target["link"], target["title"], params=resolver_params)
    _router._script_play_stage("resolve returned")


def _script_play_resolve_selected(params, selected, filtered, completed_jobs):
    """Build resolver params for the picker selection and play it."""
    import resources.lib.router as _router
    from resources.lib.resolver import resolve_and_play

    target, provider_rows = _router._selection_target(selected, filtered)
    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = _router._selection_fallback_loader(
        target, provider_rows, settings_getter=_router._get_script_setting
    )
    resolver_params["_settings_getter"] = _router._get_script_setting
    completed_job = target.get("_completed_job")
    if (
        not selected.get("_season_pack")
        and not completed_job
        and not _router._completed_lookup_was_done(completed_jobs)
    ):
        completed_job = _router._script_completed_job_for_selection(target)
    if completed_job:
        resolver_params["_completed_job"] = completed_job
    elif _router._completed_lookup_was_done(completed_jobs):
        resolver_params["_completed_job_lookup_done"] = True
    _router._attach_nzbget_dupe(
        resolver_params,
        target,
        provider_rows,
        _router._identity_from_params(params),
    )
    _router._attach_selected_result_metadata(resolver_params, target)
    if selected.get("_season_pack"):
        resolver_params["_season_pack"] = selected["_season_pack"]
    _router._script_play_stage("resolve start '{}'".format(target.get("title", "")))
    resolve_and_play(target["link"], target["title"], params=resolver_params)
    _router._script_play_stage("resolve returned")
