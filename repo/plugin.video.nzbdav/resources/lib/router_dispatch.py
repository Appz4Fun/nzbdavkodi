# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""argv parsing + route-table dispatch helpers split out of ``router``.

``route()`` (the entry point the suite patches around) stays in ``router`` and
calls these. The self-resolving and action route tables reference handlers and
connection-tests that live in (or are patched via) ``router`` —
``_handle_play``, ``_test_hydra_connection``, ``_addon_instance``, … — so they
are reached at call time through ``import resources.lib.router as _router``,
preserving every ``@patch("resources.lib.router.<name>")`` and avoiding a
top-level import cycle.
"""

import xbmc


def _parse_route_argv(argv):
    """Parse Kodi's argv into ``(base_url, handle, query_string)`` or ``None``.

    argv length and the handle's numericness are both contractually provided by
    Kodi, but a misconfigured shortcut / external launcher could violate that
    and the unhandled IndexError / ValueError used to escape ``route()`` with no
    setResolvedUrl, hanging Kodi. Surface both as a logged early-return (``None``)
    instead. Closes TODO.md §H.3.
    """
    if len(argv) < 2:
        xbmc.log(
            "NZB-DAV: route() called with argv shorter than 2: {!r}".format(argv),
            xbmc.LOGERROR,
        )
        return None
    base_url = argv[0]
    try:
        handle = int(argv[1])
    except (TypeError, ValueError):
        xbmc.log(
            "NZB-DAV: route() got non-numeric handle argv[1]={!r}; "
            "skipping this invocation".format(argv[1]),
            xbmc.LOGERROR,
        )
        return None
    query_string = argv[2] if len(argv) > 2 else ""
    return base_url, handle, query_string


def _redact_route_params(params):
    """Mask url/api/key-bearing param values before they reach the debug log."""
    redacted = {}
    for key, value in params.items():
        lowered = key.lower()
        sensitive = "url" in lowered or "api" in lowered or "key" in lowered
        redacted[key] = "***" if sensitive else value
    return redacted


def _self_resolving_route(path):
    """Return the handler for a route that resolves its own Kodi handle.

    These routes call ``setResolvedUrl`` / ``endOfDirectory`` themselves and
    must NOT fall through to ``_safe_resolve_handle``. Returns ``None`` for any
    other path so the caller treats it as an action route.
    """
    import resources.lib.router as _router

    return {
        "/play": _router._handle_play,
        "/search": _router._handle_search,
        "/direct_play": _router._handle_direct_play,
        "/menu": lambda handle, _params: _router._handle_main_menu(handle),
    }.get(path)


def _route_resolve(params):
    import resources.lib.router as _router
    from resources.lib.resolver import resolve_and_play

    # Normalize TMDBHelper "_" placeholders to empty strings so the
    # resolver sees `""`, not the literal `"_"`.
    clean = _router._clean_params(params)
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
    import resources.lib.router as _router
    from resources.lib.cache import clear_cache

    clear_cache()
    from resources.lib.http_util import notify

    notify(_router._addon_name(), _router._string(30082), 3000)


def _route_configure_preferred_groups(_params):
    import resources.lib.router as _router
    from resources.lib.filter import DEFAULT_PREFERRED_GROUPS, configure_groups_dialog

    configure_groups_dialog(
        "filter_release_group",
        _router._string(30054),
        DEFAULT_PREFERRED_GROUPS,
    )


def _route_configure_excluded_groups(_params):
    import resources.lib.router as _router
    from resources.lib.filter import DEFAULT_EXCLUDED_GROUPS, configure_groups_dialog

    configure_groups_dialog(
        "filter_exclude_release_group",
        _router._string(30055),
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
    import resources.lib.router as _router

    actions = {
        "/resolve": _route_resolve,
        "/install_player": _route_install_player,
        "/install_player_other": _route_install_player_other,
        "/clear_cache": _route_clear_cache,
        "/settings": lambda _params: _router._addon_instance().openSettings(),
        "/configure_preferred_groups": _route_configure_preferred_groups,
        "/configure_excluded_groups": _route_configure_excluded_groups,
        "/test_hydra": lambda _params: _router._test_hydra_connection(),
        "/test_prowlarr": lambda _params: _router._test_prowlarr_connection(),
        "/test_direct_indexers": lambda _params: (
            _router._test_direct_indexers_connection()
        ),
        "/manage_indexers": _route_manage_indexers,
        "/test_webdav": lambda _params: _router._test_webdav_connection(),
        "/test_nzbdav": lambda _params: _router._test_nzbdav_connection(),
        "/test_nzbget": lambda _params: _router._test_nzbget_connection(),
        "/test_nzbget_smb": lambda _params: _router._test_nzbget_smb(),
    }
    action = actions.get(path)
    if action is None:
        _router._addon_instance().openSettings()
        return
    action(params)
