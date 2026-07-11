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

import re
import time

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


def _play_identity(params, title, season, episode):
    """The release-identity dict ``_release_dupe_key`` consumes, for ``/play``.

    Built from the route params plus the locally RESOLVED title/season/episode
    (``_resolve_play_episode_args`` may have backfilled them from InfoLabels or
    an IMDB lookup, so the raw params can be stale for those three).
    """
    return {
        "type": params.get("type", "movie"),
        "title": title,
        "year": params.get("year", ""),
        "imdb": params.get("imdb", ""),
        "tvdb": params.get("tvdb", ""),
        "tmdb_id": params.get("tmdb_id", ""),
        "season": season,
        "episode": episode,
    }


def _attach_episode_context(
    resolver_params, source_params, title=None, season=None, episode=None
):
    """Attach canonical episode identity to internal resolver parameters.

    The public plugin URL remains unchanged.  Movies and episode requests whose
    season/episode could not be resolved deliberately carry no exact-selection
    context, preserving the legacy largest/title-hint behavior.
    """
    from resources.lib.season_pack import context_from_params

    source_params = source_params if isinstance(source_params, dict) else {}
    resolver_params.pop("_episode_context", None)
    if season is None:
        season = source_params.get("season", "") or source_params.get("ep_season", "")
    if episode is None:
        episode = source_params.get("episode", "") or source_params.get(
            "ep_episode", ""
        )
    context = context_from_params(
        source_params, title=title, season=season, episode=episode
    )
    if (
        context.get("type") == "episode"
        and context.get("season") is not None
        and context.get("episode") is not None
    ):
        resolver_params["_episode_context"] = context


def _season_pack_result(context, settings_getter=None):
    """Return the active backend's matching local-pack picker row, if any."""
    import resources.lib.router as _router
    from resources.lib import season_pack

    if not isinstance(context, dict) or context.get("type") != "episode":
        return None
    backend = (
        "nzbget"
        if _router._nzbget_mode_enabled(settings_getter=settings_getter)
        else "nzbdav"
    )
    record = season_pack.find_for_episode(context, backend)
    if record is None:
        return None
    summary = season_pack.episode_summary(record.get("episodes", []))
    label = _router._fmt(30364, summary)
    return season_pack.picker_result(record, label)


def _prepend_pack(filtered, pack_result):
    """Place one synthetic pack row before all ordinary provider rows."""
    rows = [
        row
        for row in list(filtered or [])
        if not isinstance(row, dict) or not row.get("_season_pack")
    ]
    if pack_result is None:
        return rows
    return [pack_result] + rows


def _provider_rows(results):
    """Return ordinary, selectable provider rows from a mixed picker list."""
    return [
        row
        for row in results or []
        if isinstance(row, dict) and not row.get("_season_pack") and row.get("link")
    ]


def _selection_target(selected, results):
    """Keep pack selections isolated; return ordinary provider pools otherwise.

    A synthetic pack is one exact completed job, never shorthand for the first
    online result. Its empty URL reaches exact validation unchanged; stale or
    transient validation fails that selection without provider submission.
    """
    providers = _provider_rows(results)
    if isinstance(selected, dict) and selected.get("_season_pack"):
        return selected, []
    return selected, providers


def _selection_fallback_loader(selected, results, settings_getter=None):
    """Build an ordinary result loader; exact pack selections have none."""
    if isinstance(selected, dict) and selected.get("_season_pack"):
        return None
    import resources.lib.router as _router

    return _router._fallback_candidate_loader_for_selection(
        selected, results, settings_getter=settings_getter
    )


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


def _handle_play_filter_and_select(
    handle, results, title, year, notify, identity=None, pack_result=None
):
    """Filter, optionally auto-select, tag, and run the picker for ``_handle_play``.

    Resolves the Kodi handle itself (False on abort / no selection, or via the
    auto-select / picker-selection resolvers). ``identity`` carries the release
    id fields the NZBGet DupeKey is built from (#372).
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

    if not filtered and pack_result is None:
        filtered = _filtered_or_prompt(all_parsed, title, notify)
        if not filtered:
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

    filtered = _prepend_pack(filtered, pack_result)

    # Auto-select best match if enabled
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    if _router._get_addon_setting(addon, "auto_select_best", "false").lower() == "true":
        _handle_play_auto_select(handle, filtered[0], filtered, identity)
        return

    # Tag results already downloaded in the active backend (nzbdav / NZBGet)
    providers = _provider_rows(filtered)
    completed_jobs = _router._tag_available(providers) if providers else None

    # Show custom results dialog
    from resources.lib.results_dialog import show_results_dialog

    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )

    if selected:
        _handle_play_resolve_selection(
            handle, selected, filtered, completed_jobs, identity
        )
    else:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())


def _ensure_nzbget_completed_hint(selected, settings_getter=None):
    """Attach the NZBGet completed-history reuse hint to a picker-less pick.

    Auto-select resolves without a picker render, so ``_tag_available_nzbget``
    never tagged the row -- an already-completed release would re-submit, and
    with the wall-clock score base NZBGet would RE-DOWNLOAD it instead of the
    reuse path playing the existing SMB files. Tags the single row in place
    (``_attach_selected_result_metadata`` then copies the hint). No-op when the
    hint is already attached or NZBGet mode is off; best-effort -- a failed
    lookup degrades to the plain submit.
    """
    import resources.lib.router as _router

    if (
        not isinstance(selected, dict)
        or selected.get("_season_pack")
        or selected.get("_nzbget_completed_job")
    ):
        return
    try:
        if not _router._nzbget_mode_enabled(settings_getter):
            return
        _router._tag_available_nzbget([selected], settings_getter=settings_getter)
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: NZBGet completed lookup for auto-select failed: {}".format(error),
            xbmc.LOGDEBUG,
        )


def _handle_play_auto_select(handle, best, filtered, identity=None):
    """Resolve the auto-selected best release through the handle-based resolver."""
    import resources.lib.router as _router
    from resources.lib.resolver import resolve

    target, provider_rows = _selection_target(best, filtered)
    resolver_params = {
        "nzburl": target["link"],
        "title": target["title"],
        "_fallback_candidates": [],
        "_fallback_candidate_loader": _selection_fallback_loader(target, provider_rows),
    }
    _attach_episode_context(resolver_params, identity or {})
    _attach_nzbget_dupe(resolver_params, target, provider_rows, identity)
    _ensure_nzbget_completed_hint(target)
    _router._attach_selected_result_metadata(resolver_params, target)
    if best.get("_season_pack"):
        resolver_params["_season_pack"] = best["_season_pack"]
    resolve(handle, resolver_params)


def _identity_from_params(params):
    """Release-identity subset the DupeKey is built from (#372)."""
    season = params.get("season", "") or params.get("ep_season", "")
    episode = params.get("episode", "") or params.get("ep_episode", "")
    return {
        "type": params.get("type", ""),
        "title": params.get("title", ""),
        "year": params.get("year", ""),
        "imdb": params.get("imdb", ""),
        "tvdb": params.get("tvdb", ""),
        "tmdb_id": params.get("tmdb_id", ""),
        "season": season,
        "episode": episode,
    }


def _normalize_release_name(title):
    """Case/whitespace-normalized release name for same-name matching (#372)."""
    return " ".join(str(title or "").split()).casefold()


_IMDB_DIGITS_RE = re.compile(r"(\d+)")
_TITLE_SLUG_NON_WORD_RE = re.compile(r"[^\w]+")


def _imdb_digits(value):
    """Bare IMDb digits from a possibly ``tt``-prefixed id (docs: ``imdb=123456``)."""
    match = _IMDB_DIGITS_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _key_title_slug(title):
    """Lowercased hyphen slug of a title for the fallback (title-based) DupeKey."""
    return _TITLE_SLUG_NON_WORD_RE.sub("-", str(title or "").strip().lower()).strip("-")


def _int_or_none(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _episode_content_prefix(tvdb, imdb, season, episode):
    """Episode-form content id (``tvdbid=<id>-S<ss>-E<ee>``, docs), or ``""``.

    Split out of ``_content_prefix`` so each key shape stays simple. Empty
    without a reliable numeric season+episode -- there the ``imdb``/``tvdb``
    fields identify the SHOW, so a bare id would span multiple episodes.
    """
    if season is None or episode is None:
        return ""
    suffix = "-S{:02d}-E{:02d}".format(season, episode)
    if tvdb:
        return "tvdbid={}{}".format(tvdb, suffix)
    return "imdb={}{}".format(imdb, suffix) if imdb else ""


def _movie_content_prefix(imdb, tmdb):
    """Movie-form content id (``imdb=<digits>``, else ``themoviedb=``), or ``""``.

    Split out of ``_content_prefix`` so each key shape stays simple.
    """
    if imdb:
        return "imdb={}".format(imdb)
    return "themoviedb={}".format(tmdb) if tmdb else ""


def _content_prefix(identity):
    """Canonical content id for DupeKey namespacing (docs formats), or ``""``.

    Movies -> ``imdb=<digits>`` (else ``themoviedb=<id>``); episodes with a
    numeric season+episode -> ``tvdbid=<id>-S<ss>-E<ee>`` (else
    ``imdb=<digits>-S<ss>-E<ee>``). Returns ``""`` for an episode context without
    a reliable numeric season+episode -- there the ``imdb``/``tvdb`` fields
    identify the SHOW, so a bare id would span multiple episodes; the release
    name (added by ``_release_dupe_key``) keeps distinct episodes apart instead.
    """
    imdb = _imdb_digits(identity.get("imdb"))
    season = _int_or_none(identity.get("season"))
    episode = _int_or_none(identity.get("episode"))
    is_episode = (identity.get("type") or "").lower() == "episode" or (
        season is not None and episode is not None
    )
    if is_episode:
        tvdb = str(identity.get("tvdb") or "").strip()
        return _episode_content_prefix(tvdb, imdb, season, episode)
    tmdb = str(identity.get("tmdb_id") or "").strip()
    return _movie_content_prefix(imdb, tmdb)


def _release_dupe_key(identity, release_title):
    """Build the NZBGet DupeKey grouping a pick with its same-name backups (#372).

    The key is scoped to the SELECTED RELEASE (its normalized release name), not
    just the content: the backups are exact same-name reposts, so keying on the
    release name groups them while keeping a DIFFERENT release of the same
    content (a 4K remux vs a prior 1080p encode, or a different episode of a
    show) under a DIFFERENT key -- so NZBGet never suppresses a later distinct
    pick as a duplicate of an earlier one. A canonical content id (imdb= /
    tvdbid=-S-E / themoviedb=, per nzbget.com/documentation/rss/#duplicates) is
    prefixed for namespacing when available. Returns "" when the release name is
    unusable (then the pick is a plain single submit).
    """
    slug = _key_title_slug(release_title)
    if not slug:
        return ""
    prefix = _content_prefix(identity or {})
    return "{}|{}".format(prefix, slug) if prefix else "nzbdav:{}".format(slug)


def _is_same_name_backup(result, target, selected_link, seen):
    """Whether a picker row is a usable same-name backup for the pick (#372).

    Requires a dict row with a link that is neither the pick's nor already
    collected, and a normalized release name matching the pick's. Split out of
    ``_same_name_backups`` so the collection loop stays simple.
    """
    if not isinstance(result, dict):
        return False
    link = result.get("link")
    if not link or link == selected_link or link in seen:
        return False
    return _normalize_release_name(result.get("title")) == target


def _same_name_backups(selected, filtered, max_backups):
    """The other picker results sharing the pick's release name, deduped/capped."""
    target = _normalize_release_name(selected.get("title"))
    selected_link = selected.get("link")
    backups = []
    seen = set()
    for result in filtered or []:
        if not _is_same_name_backup(result, target, selected_link, seen):
            continue
        seen.add(result["link"])
        # Carry the row's post-date: each same-name backup is a DIFFERENT
        # upload, and a follow-to-backup success is ledger-recorded under the
        # backup's own identity so the picker's repost-guard recognizes it.
        backups.append(
            {
                "link": result["link"],
                "title": result.get("title"),
                "pubdate": result.get("pubdate"),
            }
        )
        if len(backups) >= max_backups:
            break
    return backups


def _dupe_max_backups(getter):
    """Settings gate for the duplicate fleet: the configured backup count, or ``None``.

    ``None`` (plain single submit) when the NZBGet backend is off, fallback
    streams are disabled, or the parsed ``fallback_streams_max`` cap is
    zero/negative; else exactly what the user configured for "Maximum standby
    fallback streams" -- no additional code-level ceiling. ``getter`` reads
    settings on the RunScript/script-play path; ``None`` reads the live Kodi
    addon settings. Split out of ``_nzbget_dupe_submission_for_selection`` so
    the submission builder stays simple.
    """
    import resources.lib.router as _router

    addon = None if getter is not None else xbmcaddon.Addon("plugin.video.nzbdav")

    def _read(key, default):
        if getter is not None:
            return str(getter(key, default) or default)
        return _router._get_addon_setting(addon, key, default)

    nzbget_on = _read("nzbget_enabled", "false").lower() == "true"
    fallback_on = _read("fallback_streams_enabled", "true").lower() != "false"
    if not (nzbget_on and fallback_on):
        return None
    try:
        max_backups = int(_read("fallback_streams_max", "5") or 5)
    except (TypeError, ValueError):
        max_backups = 5
    return max_backups if max_backups > 0 else None


def _nzbget_dupe_submission_for_selection(selected, filtered, identity, getter=None):
    """Build the NZBGet Smart-Duplicates submission for a pick (#372).

    Returns ``{"key", "pick_score", "backups": [{"link","title","score"}]}`` when
    the NZBGet backend is on, fallback streams are enabled, a DupeKey is
    computable, AND there is at least one same-release-name backup on the picker
    (reposts / mirrors) -- else ``None`` (plain single submit). The pick takes
    the top DupeScore and the same-name backups strictly-lower descending scores
    (count-based, so always positive and pick-highest for any fleet size), so
    NZBGet downloads the pick and parks the rest in history as duplicate backups,
    failing over on an unrepairable download. Bounded by ``fallback_streams_max``
    -- the user's own "Maximum standby fallback streams" setting, with no
    additional code-level ceiling. ``getter`` reads settings on the
    RunScript/script-play path (``_get_script_setting``); ``None`` reads the live
    Kodi addon settings. Empty on the nzbdav backend (its own live fallback).
    """
    max_backups = _dupe_max_backups(getter)
    if max_backups is None:
        return None
    key = _release_dupe_key(identity or {}, selected.get("title"))
    if not key:
        return None
    backups = _same_name_backups(selected, filtered, max_backups)
    if not backups:
        return None
    base = _dupe_score_base()
    # Offsets ride BELOW the base (pick == base, backups descending under it):
    # any later fleet's pick then strictly outranks every member of an earlier
    # fleet regardless of their relative sizes -- base+count offsets would let
    # an old 5-backup pick beat a seconds-later loader-only retry.
    scored = [dict(b, score=base - 1 - i) for i, b in enumerate(backups)]
    # Carry the standby cap so the backup worker can bound its loader-widened
    # extras by the cap's REMAINING slots (same-name backups + extras must not
    # exceed "Maximum standby fallback streams"), and the score base so the
    # extras ride below it too.
    return {
        "key": key,
        "pick_score": base,
        "backups": scored,
        "max_backups": max_backups,
        "score_base": base,
    }


# _dupe_score_base counts seconds from here (2026-01-01 UTC) rather than the
# Unix epoch: raw epoch-seconds (~1.75e9) would sit uncomfortably close to
# NZBGet's 32-bit int DupeScore ceiling (2038 overflow), while this offset
# stays in range for decades.
_DUPE_SCORE_EPOCH = 1767225600


def _dupe_score_base():
    """Seconds-since-2026 base under every DupeScore in a submission (#372 r4).

    A fresh submission must OUTRANK any prior same-DupeKey ``SUCCESS`` in
    NZBGet's history: a replay only reaches the submit path when the completed
    files are gone or unverifiable (a reuse-probe HIT plays them directly), and
    with an equal-or-lower score NZBGet would dupe-delete the re-submission
    into a failed playback instead of re-downloading. SECOND granularity so
    even a same-minute retry's PICK (score == base) strictly outranks the
    prior success -- the only guarantee this base needs to hold, independent
    of fleet size (``fallback_streams_max`` has no code-level ceiling; a very
    large fleet only risks a low-stakes BACKUP-tier score tie against another
    fleet submitted in the same wall-clock second, never a pick collision).
    Floored at 0: a box whose clock
    predates 2026 (RTC before NTP sync) degrades to the plain count-only
    ordering instead of emitting hugely negative scores.
    """
    return max(0, int(time.time() - _DUPE_SCORE_EPOCH))


def _loader_only_dupe_submission(selected, identity, getter=None):
    """A Smart-Duplicates submission with NO same-name backups (#372 r4).

    NZBHydra collapses same-release mirrors into a single picker row, so
    ``filtered`` can hold no same-name backup while the fallback loader can
    still surface the collapsed duplicate uploads. The caller only uses this
    when that loader EXISTS; the fleet is then loader-extras-only. Same gates
    as ``_nzbget_dupe_submission_for_selection``; returns ``None`` when they
    fail.
    """
    max_backups = _dupe_max_backups(getter)
    if max_backups is None:
        return None
    key = _release_dupe_key(identity or {}, selected.get("title"))
    if not key:
        return None
    base = _dupe_score_base()
    return {
        "key": key,
        "pick_score": base,
        "backups": [],
        "max_backups": max_backups,
        "score_base": base,
    }


def _attach_nzbget_dupe(resolver_params, selected, filtered, identity):
    """Attach the NZBGet Smart-Duplicates submission, only when there is one.

    Keeps nzbdav-path params clean: ``_nzbget_dupe`` is present only in NZBGet
    mode with a computable DupeKey and at least one same-name backup (#372).
    Reads settings through ``resolver_params["_settings_getter"]`` when present
    (the RunScript/script-play path), else the live Kodi addon settings. Pops any
    inherited ``_nzbget_dupe`` first so a stale value from ``dict(params)`` can't
    survive when this selection yields no submission (bypassing the gate).
    """
    import resources.lib.router as _router

    if isinstance(selected, dict) and selected.get("_season_pack"):
        resolver_params.pop("_nzbget_dupe", None)
        return

    resolver_params.pop("_nzbget_dupe", None)
    getter = resolver_params.get("_settings_getter")
    dupe = _nzbget_dupe_submission_for_selection(selected, filtered, identity, getter)
    # The fallback loader hands the backup worker the same-content /
    # NZBHydra-deferred duplicate uploads (not just the picker's same-name
    # rows) as extra, lowest-priority backups (#372 r2). The worker runs it
    # OFF-THREAD, so build it with the pure-XML _get_script_setting rather than
    # reusing resolver_params' "_fallback_candidate_loader" -- on the
    # handle-based /play path that one carries a None getter and would call
    # xbmcaddon.Addon().getSetting off the main thread (a CoreELEC crash class
    # the snapshot design exists to avoid).
    loader = _router._fallback_candidate_loader_for_selection(
        selected, filtered, settings_getter=_router._get_script_setting
    )
    if dupe is None and loader is not None:
        # NZBHydra collapsed every mirror into this single picker row: no
        # same-name backups exist, but the loader can still surface the
        # collapsed duplicate uploads -- submit a loader-only fleet (#372 r4).
        dupe = _loader_only_dupe_submission(selected, identity, getter)
    if dupe:
        dupe["loader"] = loader
        resolver_params["_nzbget_dupe"] = dupe


def _handle_play_resolve_selection(
    handle, selected, filtered, completed_jobs, identity=None
):
    """Resolve a picker selection through the handle-based resolver."""
    import resources.lib.router as _router
    from resources.lib.resolver import resolve

    target, provider_rows = _selection_target(selected, filtered)
    resolver_params = {
        "nzburl": target["link"],
        "title": target["title"],
        "_fallback_candidates": [],
        "_fallback_candidate_loader": _selection_fallback_loader(target, provider_rows),
    }
    _attach_episode_context(resolver_params, identity or {})
    _attach_nzbget_dupe(resolver_params, target, provider_rows, identity)
    _apply_completed_job_hint(resolver_params, target, completed_jobs)
    _router._attach_selected_result_metadata(resolver_params, target)
    if selected.get("_season_pack"):
        resolver_params["_season_pack"] = selected["_season_pack"]
    resolve(handle, resolver_params)


def _handle_search_filter_and_select(
    handle, params, results, title, year, notify, pack_result=None
):
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

    if not filtered and pack_result is None:
        filtered = _filtered_or_prompt(all_parsed, title, notify)
        if not filtered:
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return

    filtered = _prepend_pack(filtered, pack_result)

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
    providers = _provider_rows(filtered)
    completed_jobs = _router._tag_available(providers) if providers else None

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

    target, provider_rows = _selection_target(best, filtered)
    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = _selection_fallback_loader(
        target, provider_rows
    )
    _attach_nzbget_dupe(
        resolver_params, target, provider_rows, _identity_from_params(params)
    )
    _ensure_nzbget_completed_hint(target)
    _router._attach_selected_result_metadata(resolver_params, target)
    if best.get("_season_pack"):
        resolver_params["_season_pack"] = best["_season_pack"]
    resolve_and_play(target["link"], target["title"], params=resolver_params)


def _handle_search_resolve_selection(params, selected, filtered, completed_jobs):
    """Play a picker selection via the params-based resolver."""
    import resources.lib.router as _router
    from resources.lib.resolver import resolve_and_play

    target, provider_rows = _selection_target(selected, filtered)
    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = _selection_fallback_loader(
        target, provider_rows
    )
    _attach_nzbget_dupe(
        resolver_params, target, provider_rows, _identity_from_params(params)
    )
    _apply_completed_job_hint(resolver_params, target, completed_jobs)
    _router._attach_selected_result_metadata(resolver_params, target)
    if selected.get("_season_pack"):
        resolver_params["_season_pack"] = selected["_season_pack"]
    resolve_and_play(target["link"], target["title"], params=resolver_params)
