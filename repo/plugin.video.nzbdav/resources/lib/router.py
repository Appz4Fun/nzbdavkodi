# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""URL routing for plugin:// calls from Kodi / TMDBHelper."""

import base64
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import chain
from urllib import request as urllib_request
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlsplit, urlunsplit
from urllib.request import urlopen

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

from resources.lib import telemetry
from resources.lib.download_ledger import downloaded_pubdate_epochs
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
from resources.lib.http_util import pubdate_to_epoch
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string
from resources.lib.nzbdav_api import completed_jobs_lookup_done, get_completed_jobs

_ORIGINAL_URLOPEN = urlopen

# IMDB IDs are always `tt` + 7–9 digits. Reject anything else before making
# outbound HTTP calls to IMDB's suggestion API.
_IMDB_ID_RE = re.compile(r"^tt\d{7,9}$")
_SCRIPT_PLAY_STAGE_PATH = "/storage/.kodi/temp/nzbdav-script-play-stage.log"
_SCRIPT_SETTINGS_PATH = (
    "/storage/.kodi/userdata/addon_data/plugin.video.nzbdav/settings.xml"
)
_PROVIDER_SEARCH_SETTING_DEFAULTS = {
    "hydra_url": "",
    "hydra_api_key": "",
    "prowlarr_host": "",
    "prowlarr_api_key": "",
    "prowlarr_indexer_ids": "",
    "max_results": "25",
}


def _addon_instance():
    """Return the addon object, accepting older tests' no-arg Addon mocks."""
    import xbmcaddon as addon_module

    try:
        return addon_module.Addon("plugin.video.nzbdav")
    except TypeError:
        return addon_module.Addon()


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


def _open_loading_dialog(title):
    """Open a NON-modal background progress dialog for the search->picker wait.

    The picker takes several seconds to appear (indexer search + filtering),
    which otherwise looks like a frozen/crashed screen. A background
    ``DialogProgressBG`` (top-right) gives a visible "working" indicator.

    We deliberately do NOT use the modal ``xbmcgui.DialogProgress`` here: on
    CoreELEC/Arctic Fuse it can native-crash Kodi mid-search (the same reason
    ``_handle_play`` avoids it — see
    ``test_handle_play_does_not_open_modal_progress_before_picker``). Any
    failure creating the dialog is swallowed so a missing indicator can never
    break playback. Returns the dialog handle, or ``None``.
    """
    try:
        dialog = xbmcgui.DialogProgressBG()
        dialog.create(_addon_name(), _fmt(30083, title or ""))
        return dialog
    except Exception:  # pylint: disable=broad-except
        return None


def _update_loading_dialog(dialog, percent, message):
    """Update the background loading dialog; no-op when it failed to open."""
    if dialog is None:
        return
    try:
        dialog.update(percent, _addon_name(), message)
    except Exception:  # pylint: disable=broad-except
        pass


def _close_loading_dialog(dialog):
    """Close the background loading dialog; safe to call more than once."""
    if dialog is None:
        return
    try:
        dialog.close()
    except Exception:  # pylint: disable=broad-except
        pass


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


def _self_resolving_route(path):
    """Return the handler for a route that resolves its own Kodi handle.

    These routes call ``setResolvedUrl`` / ``endOfDirectory`` themselves and
    must NOT fall through to ``_safe_resolve_handle``. Returns ``None`` for any
    other path so the caller treats it as an action route.
    """
    return {
        "/play": _handle_play,
        "/search": _handle_search,
        "/direct_play": _handle_direct_play,
        "/menu": lambda handle, _params: _handle_main_menu(handle),
    }.get(path)


def _route_resolve(params):
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


def _route_clear_cache(_params):
    from resources.lib.cache import clear_cache

    clear_cache()
    from resources.lib.http_util import notify

    notify(_addon_name(), _string(30082), 3000)


def _route_configure_preferred_groups(_params):
    from resources.lib.filter import DEFAULT_PREFERRED_GROUPS, configure_groups_dialog

    configure_groups_dialog(
        "filter_release_group",
        _string(30054),
        DEFAULT_PREFERRED_GROUPS,
    )


def _route_configure_excluded_groups(_params):
    from resources.lib.filter import DEFAULT_EXCLUDED_GROUPS, configure_groups_dialog

    configure_groups_dialog(
        "filter_exclude_release_group",
        _string(30055),
        DEFAULT_EXCLUDED_GROUPS,
    )


def _route_install_player(_params):
    from resources.lib.player_installer import install_player

    install_player()


def _route_install_player_other(_params):
    from resources.lib.player_installer import install_player_other

    install_player_other()


def _route_manage_indexers(_params):
    from resources.lib.indexer_manager import open_indexer_manager

    open_indexer_manager()


def _dispatch_action_route(path, params):
    """Run an action route's side-effect (no Kodi-handle resolution).

    Unknown paths fall back to opening the addon settings, matching the
    prior ``else`` branch.
    """
    actions = {
        "/resolve": _route_resolve,
        "/install_player": _route_install_player,
        "/install_player_other": _route_install_player_other,
        "/clear_cache": _route_clear_cache,
        "/settings": lambda _params: _addon_instance().openSettings(),
        "/configure_preferred_groups": _route_configure_preferred_groups,
        "/configure_excluded_groups": _route_configure_excluded_groups,
        "/test_hydra": lambda _params: _test_hydra_connection(),
        "/test_prowlarr": lambda _params: _test_prowlarr_connection(),
        "/test_direct_indexers": lambda _params: _test_direct_indexers_connection(),
        "/manage_indexers": _route_manage_indexers,
        "/test_webdav": lambda _params: _test_webdav_connection(),
        "/test_nzbdav": lambda _params: _test_nzbdav_connection(),
        "/test_nzbget": lambda _params: _test_nzbget_connection(),
        "/test_nzbget_smb": lambda _params: _test_nzbget_smb(),
    }
    action = actions.get(path)
    if action is None:
        _addon_instance().openSettings()
        return
    action(params)


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
        known_first_peer = cached_selection_pool_first_peer(selected, results)
        if (
            known_first_peer is None
            and result_count is not None
            and result_count != 1
            and not selection_pool_may_have_fallback_peer(selected, results)
        ):
            return FALLBACK_CANDIDATES_DISABLED
        if known_first_peer is None:
            known_first_peer = cached_selection_pool_first_peer(selected, results)
        fallback_settings = _resolve_fallback_prefetch_settings(settings_getter)
        if not fallback_candidate_prefetch_enabled(fallback_settings):
            return FALLBACK_CANDIDATES_DISABLED

        extra_uploads = _fetch_fallback_extra_uploads(selected, settings_getter)
        augmented = chain(results or [], extra_uploads or [])
        if known_first_peer is None:
            known_first_peer, disabled = _resolve_known_first_peer(
                selected, results, result_count, extra_uploads, augmented
            )
            if disabled:
                return FALLBACK_CANDIDATES_DISABLED
        attach_fallback_candidates_for_selection(
            selected,
            _selection_pool_with_peer_first(selected, augmented, known_first_peer),
            fallback_settings=fallback_settings,
        )
        return list(selected.get("_fallback_candidates", []) or [])

    return _load_fallback_candidates


def _resolve_fallback_prefetch_settings(settings_getter):
    """Return the fallback prefetch settings for the active getter (or default)."""
    if settings_getter is None:
        return fallback_candidate_prefetch_settings()
    return fallback_candidate_prefetch_settings(settings_getter=settings_getter)


def _fetch_fallback_extra_uploads(selected, settings_getter):
    """Fetch same-title alternate uploads from Hydra's duplicate API (fail-soft).

    The picker UX still shows one row per release for clean UI, but the
    fallback worker needs real same-release/different-upload peers — those are
    exactly what nzbdav-rs needs to swap to without interrupting playback when
    the primary stream's articles fail. Returns ``[]`` when the lookup is
    disabled or raises.
    """
    if not _hydra_duplicate_lookup_enabled(selected, settings_getter=settings_getter):
        return []
    from resources.lib.hydra import fetch_release_duplicate_uploads

    try:
        return fetch_release_duplicate_uploads(
            selected, settings_getter=settings_getter
        )
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: duplicate-uploads lookup raised: {}".format(error),
            xbmc.LOGDEBUG,
        )
        return []


def _has_extra_uploads(extra_uploads):
    """Return whether ``extra_uploads`` is a non-empty sized collection."""
    try:
        len(extra_uploads)
    except TypeError:
        return False
    return bool(extra_uploads)


def _resolve_known_first_peer(
    selected, results, result_count, extra_uploads, augmented
):
    """Resolve the plausible first peer, returning ``(peer, disabled)``.

    ``disabled`` is True when the augmented pool cannot host a fallback peer
    and the loader should return ``FALLBACK_CANDIDATES_DISABLED``.
    """
    if not _has_extra_uploads(extra_uploads):
        try:
            len(results)
        except TypeError:
            return None, False
        if result_count == 1 or not selection_pool_may_have_fallback_peer(
            selected, results
        ):
            return None, True
        return cached_selection_pool_first_peer(selected, results), False
    if (
        isinstance(results, (list, tuple))
        and len(results) == 1
        and results[0] is selected
    ):
        return (extra_uploads[0] if extra_uploads else None), False
    if not selection_pool_may_have_fallback_peer(selected, augmented):
        return None, True
    return cached_selection_pool_first_peer(selected, augmented), False


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
    xbmcgui.Dialog().ok(_addon_name(), message)


def _get_addon_setting(addon, key, default="", runtime_default=None):
    """Read a Kodi setting, returning a default if Kodi's settings layer fails."""
    try:
        value = addon.getSetting(key)
    except RuntimeError as exc:
        xbmc.log(
            "NZB-DAV: setting '{}' unavailable; using default: {}".format(key, exc),
            xbmc.LOGWARNING,
        )
        return default if runtime_default is None else runtime_default
    return value if isinstance(value, str) else default


def _get_script_setting(key, default=""):
    """Read this addon's setting from settings.xml without Kodi settings APIs."""
    try:
        from defusedxml import ElementTree as element_tree
    except ImportError:  # pragma: no cover - Kodi installs may not bundle defusedxml
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


def _snapshot_settings_getter(settings_getter, defaults):
    snapshot = {}
    for key, default in defaults.items():
        try:
            snapshot[key] = settings_getter(key, default)
        except Exception as error:  # pylint: disable=broad-exception-caught
            xbmc.log(
                "NZB-DAV: setting '{}' unavailable during provider snapshot; "
                "using default: {}".format(key, error),
                xbmc.LOGWARNING,
            )
            snapshot[key] = default

    def get_snapshot_setting(key, default=""):
        return snapshot.get(key, default)

    return get_snapshot_setting


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


def _settings_getter_or_addon_default(settings_getter):
    """Return ``settings_getter`` or an addon-backed default that forces hydra on."""
    if settings_getter is not None:
        _script_play_stage("providers using script settings")
        return settings_getter

    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    _script_play_stage("providers addon created")

    def _addon_settings_getter(key, default=""):
        runtime_default = "true" if key == "nzbhydra_enabled" else default
        return _get_addon_setting(addon, key, default, runtime_default=runtime_default)

    return _addon_settings_getter


def _resolve_episode_tvdb_id(search_type, tvdb, tmdb_id, imdb, settings_getter):
    """Resolve a shared TheTVDB id for episode searches (issue #318).

    Many indexers key TV on tvdbid, so imdbid-based tvsearch misses. The
    TMDBHelper player token usually supplies tvdb directly; when it doesn't,
    resolve it once here (cached, fail-soft) from the tmdb/imdb id so every
    provider shares the same id rather than each repeating the lookup. Returns
    the (possibly newly resolved) tvdb id.
    """
    if not (search_type == "episode" and not tvdb and (tmdb_id or imdb)):
        return tvdb
    from resources.lib.tvdb_resolver import resolve_tvdb_id

    resolved_tvdb = resolve_tvdb_id(
        tmdb_id=tmdb_id, imdb=imdb, settings_getter=settings_getter
    )
    if resolved_tvdb:
        tvdb = resolved_tvdb
        _script_play_stage("resolved tvdbid={}".format(tvdb))
    return tvdb


def _search_all_providers(
    search_type,
    title,
    year="",
    imdb="",
    season="",
    episode="",
    settings_getter=None,
    tvdb="",
    tmdb_id="",
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


def _build_provider_jobs(
    nzbhydra_enabled,
    prowlarr_enabled,
    direct_indexers_enabled,
    search_args,
    common_kwargs,
    provider_settings_getter,
):
    """Assemble the (key, label, func, args, kwargs) tuples for enabled providers."""
    provider_jobs = []

    if nzbhydra_enabled:
        from resources.lib.hydra import search_hydra

        kwargs = dict(common_kwargs, settings_getter=provider_settings_getter)
        provider_jobs.append(("hydra", "NZBHydra2", search_hydra, search_args, kwargs))

    if prowlarr_enabled:
        from resources.lib.prowlarr import search_prowlarr

        kwargs = dict(common_kwargs, settings_getter=provider_settings_getter)
        provider_jobs.append(
            ("prowlarr", "Prowlarr", search_prowlarr, search_args, kwargs)
        )

    if direct_indexers_enabled:
        from resources.lib.direct_indexers import (
            _read_max_results,
            get_configured_indexers,
            search_direct_indexers,
        )

        kwargs = dict(
            common_kwargs,
            indexers=get_configured_indexers(),
            max_results=_read_max_results(provider_settings_getter),
        )
        provider_jobs.append(
            (
                "direct indexers",
                "Direct indexer",
                search_direct_indexers,
                search_args,
                kwargs,
            )
        )

    return provider_jobs


def _run_one_provider(provider_key, _provider_label, search_func, args, kwargs):
    """Run a single provider search, emitting stage logs + timing telemetry."""
    provider_started = time.monotonic()
    results = []
    error = None
    provider_failed = False
    _script_play_stage("{} search start".format(provider_key))
    try:
        results, error = search_func(*args, **kwargs)
        _script_play_stage(
            "{} search done count={} error={}".format(
                provider_key, len(results or []), bool(error)
            )
        )
        return results, error
    except Exception:
        provider_failed = True
        raise
    finally:
        telemetry.log_timing(
            "provider_search",
            (time.monotonic() - provider_started) * 1000.0,
            provider=provider_key.replace(" ", "_"),
            count=len(results or []),
            error=provider_failed or bool(error),
        )


def _run_provider_jobs(provider_jobs):
    """Run provider jobs (serially for one, threaded for many).

    Returns a list of ``(provider_label, (results, error))`` outcomes; a job
    that raises is surfaced as an empty-results error outcome.
    """
    if len(provider_jobs) == 1:
        provider_key, provider_label, search_func, args, kwargs = provider_jobs[0]
        try:
            outcome = _run_one_provider(
                provider_key, provider_label, search_func, args, kwargs
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            outcome = ([], "{} search failed: {}".format(provider_label, error))
        return [(provider_label, outcome)]

    with ThreadPoolExecutor(max_workers=len(provider_jobs)) as executor:
        futures = [
            (
                provider_label,
                executor.submit(
                    _run_one_provider,
                    provider_key,
                    provider_label,
                    search_func,
                    args,
                    kwargs,
                ),
            )
            for (
                provider_key,
                provider_label,
                search_func,
                args,
                kwargs,
            ) in provider_jobs
        ]
        provider_outcomes = []
        for provider_label, future in futures:
            try:
                provider_outcomes.append((provider_label, future.result()))
            except Exception as error:  # pylint: disable=broad-exception-caught
                provider_outcomes.append(
                    (
                        provider_label,
                        ([], "{} search failed: {}".format(provider_label, error)),
                    )
                )
    return provider_outcomes


def _dedupe_results_by_link(all_results):
    """Drop linkless results and collapse duplicates that share a ``link``."""
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
    return deduped


# How close an indexer result's advertised size must be to a completed nzbdav
# download's ``bytes`` for them to be treated as the SAME upload. nzbdav history
# is keyed by NAME only, so a name match alone collapses distinct uploads that
# merely share a filename (a different release/resolution, or a repost at a
# different retention). The tolerance is generous enough to absorb the gap
# between an indexer's advertised NZB size and the actually-downloaded bytes
# (yEnc/par2/rar overhead) so a genuine cache hit is never hidden, while still
# separating clearly-different files (e.g. a 1080p vs a 2160p sharing a generic
# filename). True per-upload identity is the article list, but that is not
# available at picker time without fetching every NZB.
_COMPLETED_SIZE_MATCH_TOLERANCE = 0.15


def _result_size_bytes(result):
    """Best-effort parse of an indexer result's advertised size in bytes."""
    value = result.get("size") if isinstance(result, dict) else None
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return 0
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return 0
    return 0


def _completed_job_matches_result(result, completed_job):
    """Return whether a name-matched completed job plausibly IS this result's
    upload, disambiguating same-filename collisions by size.

    Fails OPEN when either size is unknown (keep the prior name-only behavior
    rather than hide a real cache hit). A clearly-different size means a
    different file — do not mark it ``DL`` or reuse its cached stream.
    """
    result_size = _result_size_bytes(result)
    try:
        job_bytes = int(completed_job.get("bytes") or 0)
    except (TypeError, ValueError):
        job_bytes = 0
    if result_size <= 0 or job_bytes <= 0:
        return True
    return (
        abs(result_size - job_bytes)
        <= max(result_size, job_bytes) * _COMPLETED_SIZE_MATCH_TOLERANCE
    )


# A same-name release posted on a *different day* is a different upload, even
# when the size matches (a repost / re-rip). nzbdav history records only the
# download time, never the Usenet post date, so we compare the result's
# pubdate against the post-dates we captured at submit time (download_ledger).
# The tolerance absorbs sub-hour indexer/TZ formatting jitter for the SAME
# post while still separating day-apart reposts cleanly.
_PUBDATE_MATCH_TOLERANCE_SECONDS = 3600


def _result_pubdate_consistent_with_downloads(result):
    """Return whether a name+size-matched result's pubdate is consistent with
    what we actually downloaded under that name.

    Fails OPEN (returns True) when we have no recorded pubdate for the name
    (e.g. downloaded before this feature, or via an external invocation) or
    the result advertises no parseable pubdate -- we'd rather keep the prior
    name+size behavior than hide a real cache hit. Returns False only when we
    DO have recorded pubdates and the result's pubdate matches none of them,
    i.e. it is a same-name repost posted at a different time.
    """
    if not isinstance(result, dict):
        return True
    recorded = downloaded_pubdate_epochs(result.get("title"))
    if not recorded:
        return True
    result_epoch = pubdate_to_epoch(result.get("pubdate"))
    if result_epoch is None:
        return True
    return any(
        abs(result_epoch - epoch) <= _PUBDATE_MATCH_TOLERANCE_SECONDS
        for epoch in recorded
    )


class _LookupDoneJobs(dict):
    """Empty mapping that still reports a finished completed-history lookup.

    ``_completed_lookup_was_done`` keys on the ``_lookup_done`` attribute, so
    returning this from a tagging path tells selection not to re-query history.
    """

    _lookup_done = True


def _nzbget_mode_enabled(settings_getter=None):
    """Return whether the NZBGet backend toggle is on (resolver's reader)."""
    from resources.lib.resolver import _nzbget_enabled

    return _nzbget_enabled(settings_getter)


def _tag_available_nzbget(results, settings_getter=None):
    """Mark results already completed in NZBGet history (NZBGet-mode "DL").

    A release still in NZBGet's history as SUCCESS already has its finished
    files on the SMB share, so the picker shows the same "DL" chip the nzbdav
    cached-stream tag uses, gated by the same name+size(+recorded pubdate)
    identity checks. The matched row is attached as ``_nzbget_completed_job``
    so the NZBGet resolver plays the row's completed files directly instead
    of re-submitting — NZBGet's duplicate check (DupeCheck=yes by default)
    would dupe-delete a re-submission of a SUCCESS item and fail the resolve.
    Deliberately does NOT attach ``_completed_job``: that hint is the nzbdav
    cached-stream reuse contract. Always returns a lookup-done mapping — even
    when the history RPC fails — because the per-selection nzbdav history
    fallback it would otherwise trigger is meaningless on the NZBGet path.
    """
    from resources.lib import nzbget_api

    completed = nzbget_api.completed_history(settings_getter=settings_getter)
    for result in results:
        completed_job = completed.get(result.get("title"))
        if (
            completed_job
            and _completed_job_matches_result(result, completed_job)
            and _result_pubdate_consistent_with_downloads(result)
        ):
            result["_available"] = True
            result["_nzbget_completed_job"] = completed_job
    if completed_jobs_lookup_done(completed):
        return completed
    return _LookupDoneJobs()


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


def _completed_lookup_was_done(completed_jobs):
    """Return whether picker-time completed-history lookup can be reused."""
    return (isinstance(completed_jobs, dict) and bool(completed_jobs)) or (
        completed_jobs_lookup_done(completed_jobs)
    )


def _hydra_duplicate_lookup_enabled(selected, settings_getter=None):
    """Return whether the selected row should use Hydra's duplicate API."""
    if not isinstance(selected, dict):
        return False
    if settings_getter is not None:
        enabled = settings_getter("nzbhydra_enabled", "false")
        if str(enabled).lower() != "true":
            return False
        hydra_url = settings_getter("hydra_url", "")
        return bool(str(hydra_url or "").strip())
    if "indexer" not in selected and "link" not in selected:
        return False
    indexer = str(selected.get("indexer", "") or "").lower()
    if "hydra" in indexer:
        return True
    link = str(selected.get("link", "") or "").lower()
    return "hydra" in link and isinstance(selected.get("_meta"), dict)


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

        # Use IMDB suggestion API to get the show title
        url = "https://v2.sg.media-imdb.com/suggestion/t/{}.json".format(imdb)
        # nosemgrep
        opener = urllib_request.urlopen if urlopen is _ORIGINAL_URLOPEN else urlopen
        with opener(  # nosec B310 — IMDB suggestion API (trusted)
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


def _episode_info_from_listitem(expected_title=""):
    """Read (show, season, episode) from the currently focused Kodi ListItem.

    TMDBHelper Next-Up / widget / home-screen plays frequently invoke the
    player with only the *series* ids and empty season/episode, which
    broadens an episode search to the whole show. The focused widget item
    still exposes the episode numbers via InfoLabels, so they can be
    recovered here as a fallback. Returns ``(show, season, episode)`` strings;
    season/episode are blanked unless numeric, and any failure degrades to
    empty strings.

    When ``expected_title`` is given, prefer the InfoLabel root whose show
    matches it: a stale/bare root that happens to carry S/E for a *different*
    show must not short-circuit the probe (the caller's same-show guard would
    reject it and the correct later root — e.g. ``Container.ListItem.*`` —
    would never be read).
    """
    # Widget/RunScript plays expose the focused item through different
    # InfoLabel roots depending on the skin/window (bare ``ListItem.*`` vs
    # ``Container.ListItem.*`` vs ``VideoPlayer.*``), so probe the same set the
    # handle-based ``_handle_play`` does — reading only bare ``ListItem.*``
    # returns blank for many widget sources and broadens the search.
    label_sources = (
        ("ListItem.TVShowTitle", "ListItem.Season", "ListItem.Episode"),
        (
            "Container.ListItem.TVShowTitle",
            "Container.ListItem.Season",
            "Container.ListItem.Episode",
        ),
        ("VideoPlayer.TVShowTitle", "VideoPlayer.Season", "VideoPlayer.Episode"),
        (
            "Container(50).ListItem.TVShowTitle",
            "Container(50).ListItem.Season",
            "Container(50).ListItem.Episode",
        ),
    )
    want = (expected_title or "").strip().casefold()
    try:
        fallback = ("", "", "")
        for t_label, s_label, e_label in label_sources:
            show = (xbmc.getInfoLabel(t_label) or "").strip()
            season = (xbmc.getInfoLabel(s_label) or "").strip()
            episode = (xbmc.getInfoLabel(e_label) or "").strip()
            # Treat each root as an ATOMIC (show, season, episode) candidate so
            # a stale show title from one root is never paired with numbers from
            # another.
            if not (season.isdigit() and episode.isdigit()):
                continue
            if not want or show.strip().casefold() == want:
                # Matches the search title (or there's no title to match) —
                # this is the focused item; use it.
                return show, season, episode
            # Complete root but a different show: keep probing later roots,
            # remembering the first as a no-title fallback only.
            if fallback == ("", "", ""):
                fallback = (show, season, episode)
        # No root matched the title. With a title set, return blanks rather
        # than inject a different show's numbers; with no title, the first
        # complete root is the only (and best) signal.
        return ("", "", "") if want else fallback
    except Exception:  # pylint: disable=broad-except
        return "", "", ""


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
    import json as _json

    from resources.lib.resolver import (
        _direct_playback_service_config,
        _prepare_direct_playback,
    )

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
    try:
        fallback_urls = _json.loads(fallback_urls_raw)
    except (TypeError, ValueError):
        fallback_urls = []
    if not isinstance(fallback_urls, list):
        fallback_urls = []

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

    fallback_sources = _direct_play_fallback_sources(fallback_urls, _validate_url)

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
    proxy_url = _direct_play_proxy_url(prepared)
    if not proxy_url:
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


def _direct_play_split_auth(url):
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
    userpass = "{}:{}".format(unquote(parsed.username), unquote(parsed.password or ""))
    encoded = base64.b64encode(userpass.encode()).decode()
    host = parsed.hostname or ""
    if parsed.port:
        host = "{}:{}".format(host, parsed.port)
    clean = urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )
    return clean, "Basic " + encoded


def _direct_play_head_length(url, auth_header):
    """HEAD ``url`` and return (content_length, error). error is "" on success."""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request

    try:
        headers = {}
        if auth_header:
            headers["Authorization"] = auth_header
        req = Request(url, method="HEAD", headers=headers)
        # nosemgrep
        opener = urllib_request.urlopen if urlopen is _ORIGINAL_URLOPEN else urlopen
        with opener(req, timeout=10) as resp:  # nosec B310
            headers = getattr(resp, "headers", {}) or {}
            length = int(headers.get("Content-Length", "1") or 1)
            if length <= 0:
                return 0, "missing-length"
            return length, ""
    except HTTPError as exc:
        return 0, "http-{}".format(exc.code)
    except URLError as exc:
        return 0, "url-{}".format(exc.reason)
    except (OSError, ValueError) as exc:
        return 0, str(exc)[:60]


def _direct_play_fallback_sources(fallback_urls, validate_url):
    """Build validated, HEAD-probed fallback source dicts for direct playback.

    Skips non-string/empty entries, non-http(s) URLs, and unstreamable peers
    (HEAD error or non-positive length), logging each skip exactly as before.
    """
    fallback_sources = []
    for idx, url_raw in enumerate(fallback_urls):
        if not isinstance(url_raw, str) or not url_raw:
            continue
        url, auth = _direct_play_split_auth(url_raw)
        try:
            validate_url(url)
        except (ValueError, TypeError):
            xbmc.log(
                "NZB-DAV: /direct_play skipping non-http(s) fallback: {}".format(
                    url_raw[:120]
                ),
                xbmc.LOGWARNING,
            )
            continue
        length, err = _direct_play_head_length(url, auth)
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
    return fallback_sources


def _direct_play_proxy_url(prepared):
    """Extract the proxy URL from a prepare payload, logging failures.

    Returns the URL string, or ``""`` when the payload is missing/empty or has
    no proxy URL (caller resolves the Kodi handle as a failure).
    """
    if not prepared:
        xbmc.log("NZB-DAV: /direct_play prepare returned no payload", xbmc.LOGERROR)
        return ""
    if isinstance(prepared, str):
        return prepared
    proxy_url = prepared.get("playback_url") or prepared.get("proxy_url")
    if not proxy_url:
        xbmc.log(
            "NZB-DAV: /direct_play prepared payload missing proxy URL: keys={}".format(
                list(prepared.keys())
            ),
            xbmc.LOGERROR,
        )
        return ""
    return proxy_url


_PLAY_INFOLABEL_SOURCES = [
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


def _episode_info_from_infolabels(title, season, episode):
    """Backfill (title, season, episode) for a play from focused-item InfoLabels.

    Probes every known InfoLabel root; the first that completes the missing
    season AND episode wins (and is the only source logged). Mirrors the prior
    inline ``_handle_play`` behaviour exactly.
    """
    for src_name, s_label, e_label, t_label in _PLAY_INFOLABEL_SOURCES:
        il_s = xbmc.getInfoLabel(s_label)
        il_e = xbmc.getInfoLabel(e_label)
        il_t = xbmc.getInfoLabel(t_label)
        # "0" is a real season (specials) and episode (pilot/E0) value — only
        # "" / "-1" mean Kodi has no selection. The previous filter dropped
        # specials entirely. TODO.md §H.2-M30.
        if il_s and il_s not in ("", "-1"):
            season = season or il_s
        if il_e and il_e not in ("", "-1"):
            episode = episode or il_e
        if il_t and not title:
            title = il_t
        if season and episode:
            # Only log the winning source; logging every probed source in the
            # success path made a noisy 4-line log entry per play.
            xbmc.log(
                "NZB-DAV: InfoLabel resolved: '{}' S{}E{} (from {})".format(
                    title, season, episode, src_name
                ),
                xbmc.LOGINFO,
            )
            break
    return title, season, episode


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

    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    xbmc.log(
        "NZB-DAV: Search stage: querying providers for '{}'".format(title),
        xbmc.LOGDEBUG,
    )
    results, search_error = _search_all_providers(
        search_type,
        title,
        settings_getter=lambda key, default="": (
            "true"
            if key == "nzbhydra_enabled"
            else _get_addon_setting(addon, key, default)
        ),
        **cache_kwargs,
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
    if all_parsed:
        choice = xbmcgui.Dialog().yesno(
            _addon_name(),
            "All {} results were filtered out. Show unfiltered?".format(
                len(all_parsed)
            ),
        )
        return all_parsed if choice else None
    notify(_addon_name(), _fmt(30087, title), 3000)
    return None


def _apply_completed_job_hint(resolver_params, selected, completed_jobs):
    """Thread the picker's completed-history hint into resolver params.

    Carries the matched ``_completed_job`` when present; otherwise, when the
    picker-time history lookup is known to have run, records
    ``_completed_job_lookup_done`` so the resolver skips a redundant re-query.
    """
    completed_job = selected.get("_completed_job")
    if completed_job:
        resolver_params["_completed_job"] = completed_job
    elif _completed_lookup_was_done(completed_jobs):
        resolver_params["_completed_job_lookup_done"] = True


def _resolve_play_episode_args(params, search_type, title, season, episode, imdb):
    """Backfill episode (title, season, episode) for ``_handle_play``.

    First probes the focused Kodi InfoLabels for a missing season/episode, then
    looks the show title up from IMDB when only an IMDB id is present. Mirrors
    the prior inline behaviour exactly; no-op for non-episode searches.
    """
    # Fallback: try every possible Kodi InfoLabel source for episode info
    if search_type == "episode" and (not season or not episode):
        title, season, episode = _episode_info_from_infolabels(title, season, episode)

    # If we still have IMDB but no title, look up from IMDB
    if search_type == "episode" and imdb and not title:
        looked_up = _lookup_episode_info(imdb, params.get("tmdb_id", ""))
        if looked_up:
            title = looked_up.get("title", title)
    return title, season, episode


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
    search_type = params.get("type", "movie")
    title = params.get("title", "")
    year = params.get("year", "")
    imdb = params.get("imdb", "")
    tvdb = params.get("tvdb", "")
    tmdb_id = params.get("tmdb_id", "")
    season = params.get("season", "") or params.get("ep_season", "")
    episode = params.get("episode", "") or params.get("ep_episode", "")

    title, season, episode = _resolve_play_episode_args(
        params, search_type, title, season, episode, imdb
    )

    cache_kwargs = dict(
        year=year, imdb=imdb, season=season, episode=episode, tvdb=tvdb, tmdb_id=tmdb_id
    )
    results, search_error = _search_with_cache(search_type, title, cache_kwargs)
    if search_error:
        _show_error_dialog(search_error)
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

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
        filtered = _filtered_or_prompt(all_parsed, title, notify)
        if not filtered:
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

    # Auto-select best match if enabled
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    if _get_addon_setting(addon, "auto_select_best", "false").lower() == "true":
        _handle_play_auto_select(handle, filtered[0], filtered)
        return

    # Tag results already downloaded in the active backend (nzbdav / NZBGet)
    completed_jobs = _tag_available(filtered)

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
    from resources.lib.resolver import resolve

    resolver_params = {
        "nzburl": best["link"],
        "title": best["title"],
        "_fallback_candidates": [],
        "_fallback_candidate_loader": _fallback_candidate_loader_for_selection(
            best, filtered
        ),
    }
    _attach_selected_result_metadata(resolver_params, best)
    resolve(handle, resolver_params)


def _handle_play_resolve_selection(handle, selected, filtered, completed_jobs):
    """Resolve a picker selection through the handle-based resolver."""
    from resources.lib.resolver import resolve

    resolver_params = {
        "nzburl": selected["link"],
        "title": selected["title"],
        "_fallback_candidates": [],
        "_fallback_candidate_loader": _fallback_candidate_loader_for_selection(
            selected, filtered
        ),
    }
    _apply_completed_job_hint(resolver_params, selected, completed_jobs)
    _attach_selected_result_metadata(resolver_params, selected)
    resolve(handle, resolver_params)


def _lookup_search_episode_args(params, search_type, title, season, episode, imdb):
    """Backfill (title, season, episode) from an IMDB lookup for ``_handle_search``.

    When an episode search has an IMDB id but no title, look up the show and
    fill any missing title/season/episode. No-op otherwise. Mirrors the prior
    inline behaviour exactly.
    """
    if search_type == "episode" and imdb and not title:
        looked_up = _lookup_episode_info(imdb, params.get("tmdb_id", ""))
        if looked_up:
            title = looked_up.get("title", title)
            season = season or looked_up.get("season", "")
            episode = episode or looked_up.get("episode", "")
    return title, season, episode


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
    from resources.lib.filter import filter_results
    from resources.lib.http_util import notify

    params = _clean_params(params)
    search_type = params.get("type", "movie")
    title = params.get("title", "")
    year = params.get("year", "")
    imdb = params.get("imdb", "")
    tvdb = params.get("tvdb", "")
    tmdb_id = params.get("tmdb_id", "")
    season = params.get("season", "") or params.get("ep_season", "")
    episode = params.get("episode", "") or params.get("ep_episode", "")

    title, season, episode = _lookup_search_episode_args(
        params, search_type, title, season, episode, imdb
    )

    cache_kwargs = dict(
        year=year, imdb=imdb, season=season, episode=episode, tvdb=tvdb, tmdb_id=tmdb_id
    )
    results, search_error = _search_with_cache(search_type, title, cache_kwargs)
    if search_error:
        _show_error_dialog(search_error)
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    if not results:
        xbmc.log(
            "NZB-DAV: Search stage: no results found for '{}'".format(title),
            xbmc.LOGINFO,
        )
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
        filtered = _filtered_or_prompt(all_parsed, title, notify)
        if not filtered:
            xbmcplugin.endOfDirectory(handle, succeeded=False)
            return

    # Auto-select best match if enabled
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    if (
        _get_addon_setting(addon, "auto_select_best", "false").lower() == "true"
        and filtered
    ):
        _handle_search_auto_select(params, filtered[0], filtered)
        # Same hang class as C1 (router.py): /search is a directory
        # route, so Kodi blocks until endOfDirectory fires. Without
        # this, the auto-select branch returned silently and Kodi
        # waited forever for a directory listing that never came.
        # Mark the directory as not-succeeded since playback already
        # ran via resolve_and_play.
        xbmcplugin.endOfDirectory(handle, succeeded=False)
        return

    # Tag results already downloaded in the active backend (nzbdav / NZBGet)
    completed_jobs = _tag_available(filtered)

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
    from resources.lib.resolver import resolve_and_play

    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = (
        _fallback_candidate_loader_for_selection(best, filtered)
    )
    _attach_selected_result_metadata(resolver_params, best)
    resolve_and_play(best["link"], best["title"], params=resolver_params)


def _handle_search_resolve_selection(params, selected, filtered, completed_jobs):
    """Play a picker selection via the params-based resolver."""
    from resources.lib.resolver import resolve_and_play

    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = (
        _fallback_candidate_loader_for_selection(selected, filtered)
    )
    _apply_completed_job_hint(resolver_params, selected, completed_jobs)
    _attach_selected_result_metadata(resolver_params, selected)
    resolve_and_play(selected["link"], selected["title"], params=resolver_params)


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
    li_show, li_season, li_episode = _episode_info_from_listitem(title)
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
    if same_show:
        if not title:
            title = li_show
        season = season or li_season
        episode = episode or li_episode
        if season:
            params["season"] = season
        if episode:
            params["episode"] = episode
    return title, season, episode


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
    search_type = params.get("type", "movie")
    title = params.get("title", "")
    year = params.get("year", "")
    imdb = params.get("imdb", "")
    tvdb = params.get("tvdb", "")
    tmdb_id = params.get("tmdb_id", "")
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

    # TMDBHelper Next-Up / widget / home-screen plays often invoke the player
    # with only the series ids and empty season/episode, so an episode search
    # broadens to the whole show. Recover the numbers from the focused
    # ListItem, but trust them only when that item is the same show we're
    # about to search (the focus may have moved by the time the player fires).
    if search_type == "episode" and not (season and episode):
        title, season, episode = _script_play_recover_episode_info(
            params, title, season, episode
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
        params, search_type, title, year, search_kwargs, notify
    )
    if prepared is None:
        return
    filtered, total_count, completed_jobs = prepared

    from resources.lib.results_dialog import show_results_dialog

    _script_play_stage("picker open")
    selected = show_results_dialog(
        filtered, title=title, year=year, total_count=total_count
    )
    if not selected:
        _script_play_stage("picker cancelled")
        return
    _script_play_stage("picker selected")
    _script_play_resolve_selected(params, selected, filtered, completed_jobs)


def _script_play_search_filter_tag(
    params, search_type, title, year, search_kwargs, notify
):
    """Search, filter, optionally auto-play, and tag for the RunScript flow.

    Runs the whole non-modal-loading-dialog phase. Returns ``None`` when the
    caller should stop (provider error, no results, unfiltered-prompt declined,
    or an auto-selected release was already played), otherwise the
    ``(filtered, total_count, completed_jobs)`` payload for the picker.
    """
    from resources.lib.filter import filter_results

    # The indexer search + filtering below can take several seconds; with no
    # on-screen indicator the player looks frozen/crashed. Show a NON-modal
    # background progress dialog (see _open_loading_dialog — the modal
    # DialogProgress native-crashes Kodi mid-search on CoreELEC/Arctic Fuse).
    # The finally guarantees it is closed before the picker opens and on every
    # early return / exception below.
    loading = _open_loading_dialog(title)
    try:
        results, search_error = _search_all_providers(
            search_type, title, settings_getter=_get_script_setting, **search_kwargs
        )
        _script_play_stage("provider search done count={}".format(len(results or [])))
        if search_error:
            xbmc.log(
                "NZB-DAV: Search stage: provider error - {}".format(search_error),
                xbmc.LOGWARNING,
            )
            _show_error_dialog(search_error)
            return None

        if not results:
            xbmc.log(
                "NZB-DAV: Search stage: no results found for '{}'".format(title),
                xbmc.LOGINFO,
            )
            notify(_addon_name(), _fmt(30087, title), 3000)
            return None

        total_count = len(results)
        _update_loading_dialog(loading, 60, _string(30088))
        _script_play_stage("filter start count={} for '{}'".format(len(results), title))
        filtered, all_parsed = filter_results(
            results, settings_getter=_get_script_setting
        )
        _script_play_stage(
            "filter done filtered={} parsed={}".format(
                len(filtered or []), len(all_parsed or [])
            )
        )

        if not filtered:
            filtered = _script_play_filtered_or_prompt(
                loading, all_parsed, title, notify
            )
            if not filtered:
                return None

        if (
            _get_script_setting("auto_select_best", "false").lower() == "true"
            and filtered
        ):
            # resolve_and_play blocks on the download; drop the indicator first.
            _close_loading_dialog(loading)
            _script_play_auto_select(params, filtered[0], filtered)
            return None

        completed_jobs = _script_play_tag_available(filtered)
    finally:
        _close_loading_dialog(loading)

    return filtered, total_count, completed_jobs


def _script_play_filtered_or_prompt(loading, all_parsed, title, notify):
    """RunScript variant of the unfiltered-results prompt.

    Closes the loading dialog before the modal yes/no so the two don't stack.
    Returns ``all_parsed`` on yes, or ``None`` (caller stops) on no / when
    nothing parsed.
    """
    if all_parsed:
        # Close before the modal yes/no so the two don't stack.
        _close_loading_dialog(loading)
        choice = xbmcgui.Dialog().yesno(
            _addon_name(),
            "All {} results were filtered out. Show unfiltered?".format(
                len(all_parsed)
            ),
        )
        return all_parsed if choice else None
    notify(_addon_name(), _fmt(30087, title), 3000)
    return None


def _script_play_tag_available(filtered):
    """Tag already-downloaded results for the RunScript flow (fail-soft)."""
    try:
        completed_jobs = _tag_available(filtered, settings_getter=_get_script_setting)
        _script_play_stage("tag available done")
        return completed_jobs
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Script completed-history tagging failed: {}".format(error),
            xbmc.LOGDEBUG,
        )
        _script_play_stage("tag available failed")
        return None


def _script_play_auto_select(params, best, filtered):
    """Build resolver params for the auto-selected best release and play it."""
    from resources.lib.resolver import resolve_and_play

    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = (
        _fallback_candidate_loader_for_selection(
            best, filtered, settings_getter=_get_script_setting
        )
    )
    completed_job = None
    if not _nzbget_mode_enabled(_get_script_setting):
        # In NZBGet mode the nzbdav completed-history hint is dead weight
        # (resolve_and_play delegates to NZBGet before reading it) — skip the
        # lookup instead of stalling on a stale nzbdav config.
        completed_job = _script_completed_job_for_selection(best)
    if completed_job:
        resolver_params["_completed_job"] = completed_job
    else:
        resolver_params["_completed_job_lookup_done"] = True
    resolver_params["_settings_getter"] = _get_script_setting
    _attach_selected_result_metadata(resolver_params, best)
    _script_play_stage("resolve start '{}'".format(best.get("title", "")))
    resolve_and_play(best["link"], best["title"], params=resolver_params)
    _script_play_stage("resolve returned")


def _script_play_resolve_selected(params, selected, filtered, completed_jobs):
    """Build resolver params for the picker selection and play it."""
    from resources.lib.resolver import resolve_and_play

    resolver_params = dict(params)
    resolver_params["_fallback_candidates"] = []
    resolver_params["_fallback_candidate_loader"] = (
        _fallback_candidate_loader_for_selection(
            selected, filtered, settings_getter=_get_script_setting
        )
    )
    resolver_params["_settings_getter"] = _get_script_setting
    completed_job = selected.get("_completed_job")
    if not completed_job and not _completed_lookup_was_done(completed_jobs):
        completed_job = _script_completed_job_for_selection(selected)
    if completed_job:
        resolver_params["_completed_job"] = completed_job
    elif _completed_lookup_was_done(completed_jobs):
        resolver_params["_completed_job_lookup_done"] = True
    _attach_selected_result_metadata(resolver_params, selected)
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

        # Use TMDB's find endpoint (no API key needed for basic lookups via v3)
        # Fall back to a free poster service
        url = "https://v2.sg.media-imdb.com/suggestion/t/{}.json".format(imdb_id)
        try:
            # nosemgrep
            opener = urllib_request.urlopen if urlopen is _ORIGINAL_URLOPEN else urlopen
            with opener(  # nosec B310 — IMDB suggestion API (trusted)
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


def _test_nzbget_connection():
    """Test NZBGet JSON-RPC reachability + auth via the version method."""
    from resources.lib.http_util import notify
    from resources.lib.nzbget_api import test_connection

    ok, _error = test_connection()
    if ok:
        notify(_addon_name(), _string(30224), 3000)
    else:
        notify(_addon_name(), _string(30225), 5000)


def _test_nzbget_smb():
    """Test the SMB completed-folder root is listable via xbmcvfs."""
    import xbmcvfs

    from resources.lib.http_util import notify

    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    smb_root = addon.getSetting("nzbget_smb_root").strip()
    # xbmcvfs.listdir() does NOT raise for an unreachable/typo'd/wrong-
    # credentials SMB path — it returns ([], []) and only logs at the C++
    # VFS layer — so a non-raising listdir is a false "reachable". Gate on
    # xbmcvfs.exists(), which returns False for those paths (the same
    # positive-signal check player_installer.py uses).
    reachable = False
    if smb_root:
        try:
            reachable = bool(xbmcvfs.exists(smb_root))
        except Exception:  # pylint: disable=broad-except
            reachable = False
    if reachable:
        notify(_addon_name(), _string(30226), 3000)
    else:
        notify(_addon_name(), _string(30227), 5000)


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
