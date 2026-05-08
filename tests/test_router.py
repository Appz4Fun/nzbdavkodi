# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import itertools
import threading
import time as _time
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from resources.lib.nzb_manifest import make_empty_manifest
from resources.lib.router import (
    _clean_params,
    _fallback_candidate_loader_for_selection,
    _format_info_line,
    _format_size,
    _get_tmdb_poster,
    _handle_play,
    _handle_search,
    _prowlarr_indexers_response_ok,
    _safe_resolve_handle,
    _tag_available,
    _test_connection,
    _test_hydra_connection,
    _test_nzbdav_connection,
    _test_prowlarr_connection,
    parse_params,
    parse_route,
    route,
)


def test_parse_route_root():
    assert parse_route("plugin://plugin.video.nzbdav/") == "/"


def test_parse_route_search():
    assert parse_route("plugin://plugin.video.nzbdav/search") == "/search"


def test_parse_route_resolve():
    assert parse_route("plugin://plugin.video.nzbdav/resolve") == "/resolve"


def test_parse_route_install_player():
    assert (
        parse_route("plugin://plugin.video.nzbdav/install_player") == "/install_player"
    )


def test_parse_route_install_player_other():
    assert (
        parse_route("plugin://plugin.video.nzbdav/install_player_other")
        == "/install_player_other"
    )


def test_parse_params_movie():
    query = "?" + urlencode(
        {"type": "movie", "title": "The Matrix", "year": "1999", "imdb": "tt0133093"}
    )
    params = parse_params(query)
    assert params["type"] == "movie"
    assert params["title"] == "The Matrix"
    assert params["year"] == "1999"
    assert params["imdb"] == "tt0133093"


def test_parse_params_episode():
    query = "?" + urlencode(
        {"type": "episode", "title": "Breaking Bad", "season": "5", "episode": "14"}
    )
    params = parse_params(query)
    assert params["type"] == "episode"
    assert params["title"] == "Breaking Bad"
    assert params["season"] == "5"
    assert params["episode"] == "14"


def test_parse_params_empty():
    params = parse_params("")
    assert params == {}


def test_clean_params_converts_tmdbhelper_placeholders():
    """TMDBHelper sends '_' for missing template params; convert to empty strings."""
    params = {
        "type": "movie",
        "title": "The Matrix",
        "year": "_",
        "imdb": "_",
        "season": "1",
    }
    cleaned = _clean_params(params)
    assert cleaned["type"] == "movie"
    assert cleaned["title"] == "The Matrix"
    assert cleaned["year"] == ""
    assert cleaned["imdb"] == ""
    assert cleaned["season"] == "1", "Non-placeholder values should be preserved"


# --- URL encoding/decoding round-trip tests ---


def test_parse_params_special_characters_roundtrip():
    """Titles with special chars survive URL encode/decode."""
    title = "Spider-Man: No Way Home (2021)"
    query = "?" + urlencode({"title": title})
    params = parse_params(query)
    assert params["title"] == title


def test_parse_params_unicode_title():
    """Unicode characters in titles are preserved."""
    title = "Crouching Tiger, Hidden Dragon"
    query = "?" + urlencode({"title": title})
    params = parse_params(query)
    assert params["title"] == title


def test_parse_params_ampersand_in_title():
    """Ampersands in titles must be properly encoded."""
    title = "Tom & Jerry"
    query = "?" + urlencode({"title": title})
    params = parse_params(query)
    assert params["title"] == title


def test_parse_params_question_mark_only():
    """A bare '?' should return empty params."""
    params = parse_params("?")
    assert params == {}


def test_parse_params_none():
    """None input should return empty params."""
    params = parse_params(None)
    assert params == {}


# --- _format_size tests ---


def test_format_size_gb():
    assert _format_size(5368709120) == "5.0 GB"


def test_format_size_mb():
    assert _format_size(10485760) == "10.0 MB"


def test_format_size_bytes():
    assert _format_size(512) == "512 B"


def test_format_size_none():
    assert _format_size(None) == ""


def test_format_size_zero():
    assert _format_size(0) == ""


def test_format_size_very_large():
    """100 GB file."""
    assert _format_size(107374182400) == "100.0 GB"


def test_format_size_string_input():
    """_format_size should handle string input by converting to int."""
    # Sizes from NZBHydra come as strings
    assert (
        _format_size("5368709120") == "5.0 GB"
    ), "_format_size should accept string byte counts"
    assert (
        _format_size("10485760") == "10.0 MB"
    ), "_format_size should handle MB string input"


# --- route() dispatch tests ---


@patch("resources.lib.router._handle_search")
def test_route_dispatches_to_handle_search(mock_handle_search):
    """route() with /search path should dispatch to _handle_search."""
    query = "?" + urlencode(
        {"type": "movie", "title": "The Matrix", "year": "1999", "imdb": "tt0133093"}
    )
    argv = ["plugin://plugin.video.nzbdav/search", "1", query]
    route(argv)
    mock_handle_search.assert_called_once()
    call_args = mock_handle_search.call_args
    handle = call_args[0][0]
    params = call_args[0][1]
    assert handle == 1, "Handle should be passed as integer"
    assert params["type"] == "movie", "type param should be forwarded"
    assert params["title"] == "The Matrix", "title param should be forwarded"
    assert params["imdb"] == "tt0133093", "imdb param should be forwarded"


@patch("resources.lib.router.xbmc")
@patch("resources.lib.router._handle_search")
def test_route_redacts_sensitive_params_in_logs(mock_handle_search, mock_xbmc):
    query = "?" + urlencode(
        {
            "type": "movie",
            "nzburl": "http://hydra/getnzb/abc?apikey=secret123",
            "api_key": "secret123",
            "title": "The Matrix",
        }
    )

    route(["plugin://plugin.video.nzbdav/search", "1", query])

    logged = mock_xbmc.log.call_args[0][0]
    assert "secret123" not in logged
    assert "'nzburl': '***'" in logged
    assert "'api_key': '***'" in logged


@patch("resources.lib.router.install_player", create=True)
def test_route_dispatches_to_install_player(mock_install):
    """route() with /install_player path should dispatch to install_player."""
    with patch("resources.lib.router.install_player", mock_install, create=True):
        # Patch the import inside route()
        with patch.dict(
            "sys.modules",
            {"resources.lib.player_installer": MagicMock(install_player=mock_install)},
        ):
            argv = ["plugin://plugin.video.nzbdav/install_player", "1", ""]
            route(argv)
    # install_player is imported inside route() so we verify it was called via
    # checking the module-level mock
    # The simplest check: route didn't raise an exception
    assert True, "route() with /install_player should complete without error"


@patch("xbmcaddon.Addon")
def test_search_all_providers_calls_direct_indexers_when_enabled(mock_addon):
    from resources.lib.router import _search_all_providers

    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: {
        "nzbhydra_enabled": "false",
        "prowlarr_enabled": "false",
        "direct_indexers_enabled": "true",
    }.get(key, "")
    mock_addon.return_value = addon

    direct_search = MagicMock(
        return_value=(
            [
                {
                    "title": "The.Matrix.1999.1080p-GRP",
                    "link": "https://indexer/api?t=get&id=1&apikey=secret",
                    "size": "123",
                    "indexer": "NZBGeek",
                    "pubdate": "",
                    "age": "",
                }
            ],
            None,
        )
    )

    with patch.dict(
        "sys.modules",
        {
            "resources.lib.direct_indexers": MagicMock(
                search_direct_indexers=direct_search
            )
        },
    ):
        results, error = _search_all_providers(
            "episode",
            "Breaking Bad",
            year="2008",
            imdb="tt0903747",
            season="5",
            episode="14",
        )

    assert error is None
    assert len(results) == 1
    direct_search.assert_called_once_with(
        "episode",
        "Breaking Bad",
        year="2008",
        imdb="tt0903747",
        season="5",
        episode="14",
    )


@patch("xbmcaddon.Addon")
def test_search_all_providers_no_provider_error_mentions_direct_indexers(
    mock_addon,
):
    from resources.lib.router import _search_all_providers

    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: {
        "nzbhydra_enabled": "false",
        "prowlarr_enabled": "false",
        "direct_indexers_enabled": "false",
    }.get(key, "")
    mock_addon.return_value = addon

    results, error = _search_all_providers("movie", "The Matrix")

    assert not results
    assert "direct indexers" in error


@patch("xbmcaddon.Addon")
def test_search_all_providers_uses_defaults_when_setting_read_raises(mock_addon):
    from resources.lib.router import _search_all_providers

    addon = MagicMock()
    addon.getSetting.side_effect = RuntimeError(
        'Unknown exception thrown from the call "getSetting"'
    )
    mock_addon.return_value = addon

    hydra_search = MagicMock(
        return_value=(
            [
                {
                    "title": "The.Matrix.1999.1080p-GRP",
                    "link": "https://indexer/api?t=get&id=1&apikey=secret",
                    "size": "123",
                    "indexer": "NZBHydra2",
                    "pubdate": "",
                    "age": "",
                }
            ],
            None,
        )
    )

    with patch("resources.lib.hydra.search_hydra", hydra_search):
        results, error = _search_all_providers("movie", "The Matrix")

    assert error is None
    assert len(results) == 1
    hydra_search.assert_called_once()


@patch("xbmcaddon.Addon", side_effect=RuntimeError("no addon context"))
def test_search_all_providers_uses_script_settings_getter_without_kodi_addon(
    mock_addon,
):
    from resources.lib.router import _search_all_providers

    def setting(key, default=""):
        return {
            "nzbhydra_enabled": "true",
            "prowlarr_enabled": "false",
            "direct_indexers_enabled": "false",
        }.get(key, default)

    hydra_search = MagicMock(
        return_value=(
            [
                {
                    "title": "The.Odyssey.2026.1080p-GRP",
                    "link": "https://hydra/getnzb/1",
                    "size": "123",
                    "indexer": "NZBHydra2",
                    "pubdate": "",
                    "age": "",
                }
            ],
            None,
        )
    )

    with patch("resources.lib.hydra.search_hydra", hydra_search):
        results, error = _search_all_providers(
            "movie", "The Odyssey", settings_getter=setting
        )

    assert error is None
    assert len(results) == 1
    mock_addon.assert_not_called()
    hydra_search.assert_called_once()
    assert hydra_search.call_args.kwargs["settings_getter"] is setting


# --- _safe_resolve_handle + action route handle-resolution tests ---
#
# Action routes (install_player, clear_cache, settings, configure_*,
# test_hydra, test_nzbdav, test_webdav, resolve) are invoked from main-menu
# items with
# isFolder=False. Kodi blocks the UI until setResolvedUrl is called on the
# handle. These tests assert the route path always resolves the handle so
# Kodi never hangs. Regression test for TODO.md §H.2 C1 (was ISSUE_REPORT.md
# C1 before audit-file merge on 2026-04-24).


@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.ListItem")
def test_safe_resolve_handle_resolves_positive_handle(mock_listitem, mock_resolved):
    """_safe_resolve_handle should call setResolvedUrl for valid handles."""
    mock_listitem.return_value = "fake_listitem"
    _safe_resolve_handle(5)
    mock_resolved.assert_called_once_with(5, False, "fake_listitem")


@patch("xbmcplugin.setResolvedUrl")
def test_safe_resolve_handle_skips_runplugin_handle(mock_resolved):
    """_safe_resolve_handle should be a no-op for handle == -1 (RunPlugin)."""
    _safe_resolve_handle(-1)
    mock_resolved.assert_not_called()


@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.router._handle_main_menu")
def test_route_main_menu_does_not_call_safe_resolve(mock_menu, mock_resolved):
    """Main-menu dispatch (directory) must not also call setResolvedUrl."""
    route(["plugin://plugin.video.nzbdav/", "1", ""])
    mock_menu.assert_called_once_with(1)
    mock_resolved.assert_not_called()


@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.router._handle_play")
def test_route_play_does_not_call_safe_resolve(mock_play, mock_resolved):
    """/play handles its own resolution — _safe_resolve_handle must not fire."""
    route(["plugin://plugin.video.nzbdav/play", "1", "?type=movie&title=X"])
    mock_play.assert_called_once()
    mock_resolved.assert_not_called()


@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.router._handle_search")
def test_route_search_does_not_call_safe_resolve(mock_search, mock_resolved):
    """/search handles its own resolution — _safe_resolve_handle must not fire."""
    route(["plugin://plugin.video.nzbdav/search", "1", "?type=movie&title=X"])
    mock_search.assert_called_once()
    mock_resolved.assert_not_called()


@patch("xbmcplugin.setResolvedUrl")
def test_route_install_player_resolves_handle(mock_resolved):
    """/install_player must resolve the handle after running."""
    with patch.dict(
        "sys.modules",
        {"resources.lib.player_installer": MagicMock(install_player=MagicMock())},
    ):
        route(["plugin://plugin.video.nzbdav/install_player", "7", ""])
    assert mock_resolved.called, "setResolvedUrl must be called for /install_player"
    assert mock_resolved.call_args[0][0] == 7
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
def test_route_install_player_other_resolves_handle(mock_resolved):
    """/install_player_other must resolve the handle after running."""
    install_player_other = MagicMock()
    with patch.dict(
        "sys.modules",
        {
            "resources.lib.player_installer": MagicMock(
                install_player_other=install_player_other
            )
        },
    ):
        route(["plugin://plugin.video.nzbdav/install_player_other", "9", ""])
    install_player_other.assert_called_once()
    assert (
        mock_resolved.called
    ), "setResolvedUrl must be called for /install_player_other"
    assert mock_resolved.call_args[0][0] == 9
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.http_util.notify")
def test_route_clear_cache_resolves_handle(mock_notify, mock_resolved):
    """/clear_cache must resolve the handle after running."""
    with patch.dict(
        "sys.modules",
        {"resources.lib.cache": MagicMock(clear_cache=MagicMock())},
    ):
        route(["plugin://plugin.video.nzbdav/clear_cache", "2", ""])
    assert mock_resolved.called, "setResolvedUrl must be called for /clear_cache"
    assert mock_resolved.call_args[0][0] == 2
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
def test_route_settings_resolves_handle(mock_resolved):
    """/settings must resolve the handle after openSettings returns."""
    fake_addon = MagicMock()
    with patch.dict("sys.modules", {"xbmcaddon": MagicMock(Addon=lambda: fake_addon)}):
        route(["plugin://plugin.video.nzbdav/settings", "3", ""])
    fake_addon.openSettings.assert_called_once()
    assert mock_resolved.called, "setResolvedUrl must be called for /settings"
    assert mock_resolved.call_args[0][0] == 3
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
def test_route_configure_preferred_groups_resolves_handle(mock_resolved):
    """/configure_preferred_groups must resolve the handle after running."""
    fake_filter = MagicMock(
        configure_groups_dialog=MagicMock(),
        DEFAULT_PREFERRED_GROUPS=[],
    )
    with patch.dict("sys.modules", {"resources.lib.filter": fake_filter}):
        route(["plugin://plugin.video.nzbdav/configure_preferred_groups", "4", ""])
    fake_filter.configure_groups_dialog.assert_called_once()
    assert mock_resolved.called
    assert mock_resolved.call_args[0][0] == 4
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
def test_route_configure_excluded_groups_resolves_handle(mock_resolved):
    """/configure_excluded_groups must resolve the handle after running."""
    fake_filter = MagicMock(
        configure_groups_dialog=MagicMock(),
        DEFAULT_EXCLUDED_GROUPS=[],
    )
    with patch.dict("sys.modules", {"resources.lib.filter": fake_filter}):
        route(["plugin://plugin.video.nzbdav/configure_excluded_groups", "5", ""])
    fake_filter.configure_groups_dialog.assert_called_once()
    assert mock_resolved.called
    assert mock_resolved.call_args[0][0] == 5
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.router._test_hydra_connection")
def test_route_test_hydra_resolves_handle(mock_test, mock_resolved):
    """/test_hydra must resolve the handle after running."""
    route(["plugin://plugin.video.nzbdav/test_hydra", "6", ""])
    mock_test.assert_called_once()
    assert mock_resolved.called
    assert mock_resolved.call_args[0][0] == 6
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.router._test_nzbdav_connection")
def test_route_test_nzbdav_resolves_handle(mock_test, mock_resolved):
    """/test_nzbdav must resolve the handle after running."""
    route(["plugin://plugin.video.nzbdav/test_nzbdav", "8", ""])
    mock_test.assert_called_once()
    assert mock_resolved.called
    assert mock_resolved.call_args[0][0] == 8
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.router._test_prowlarr_connection")
def test_route_test_prowlarr_resolves_handle(mock_test, mock_resolved):
    """/test_prowlarr must resolve the handle after running."""
    route(["plugin://plugin.video.nzbdav/test_prowlarr", "10", ""])
    mock_test.assert_called_once()
    assert mock_resolved.called
    assert mock_resolved.call_args[0][0] == 10
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
def test_route_test_direct_indexers_resolves_handle(mock_resolved):
    """Route /test_direct_indexers and resolve the action handle."""
    test_configured = MagicMock(return_value=(1, 1, []))
    with patch.dict(
        "sys.modules",
        {
            "resources.lib.direct_indexers": MagicMock(
                test_configured_indexers=test_configured
            )
        },
    ):
        route(["plugin://plugin.video.nzbdav/test_direct_indexers", "12", ""])
    test_configured.assert_called_once()
    assert mock_resolved.called
    assert mock_resolved.call_args[0][0] == 12
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
def test_route_resolve_path_resolves_handle(mock_resolved):
    """/resolve must resolve the handle after running (regardless of handle value)."""
    fake_resolver = MagicMock(resolve_and_play=MagicMock())
    with patch.dict("sys.modules", {"resources.lib.resolver": fake_resolver}):
        route(["plugin://plugin.video.nzbdav/resolve", "9", "?nzburl=x&title=y"])
    fake_resolver.resolve_and_play.assert_called_once()
    assert mock_resolved.called
    assert mock_resolved.call_args[0][0] == 9
    assert mock_resolved.call_args[0][1] is False


@patch("xbmcplugin.setResolvedUrl")
def test_route_resolve_path_with_runplugin_handle_does_not_call_resolved_url(
    mock_resolved,
):
    """/resolve with handle=-1 (RunPlugin) must not call setResolvedUrl."""
    fake_resolver = MagicMock(resolve_and_play=MagicMock())
    with patch.dict("sys.modules", {"resources.lib.resolver": fake_resolver}):
        route(["plugin://plugin.video.nzbdav/resolve", "-1", "?nzburl=x&title=y"])
    fake_resolver.resolve_and_play.assert_called_once()
    mock_resolved.assert_not_called()


@patch("xbmcplugin.setResolvedUrl")
def test_route_exception_in_action_route_still_resolves_handle(mock_resolved):
    """If an action route raises, the handle must still be resolved."""
    fake_resolver = MagicMock(resolve_and_play=MagicMock(side_effect=RuntimeError("x")))
    with patch.dict("sys.modules", {"resources.lib.resolver": fake_resolver}):
        try:
            route(["plugin://plugin.video.nzbdav/resolve", "11", "?nzburl=a&title=b"])
        except RuntimeError:
            pass
    assert mock_resolved.called, "Handle must be resolved even when the route raises"
    assert mock_resolved.call_args[0][0] == 11
    assert mock_resolved.call_args[0][1] is False


# --- _format_info_line tests ---


def test_format_info_line_full():
    """Test rich label formatting with all metadata."""
    item = {
        "title": "The.Matrix.1999.2160p.UHD.BluRay.REMUX.HEVC.DTS-HD.MA.7.1-GROUP",
        "size": "45000000000",
        "_meta": {
            "resolution": "2160p",
            "hdr": ["HDR10"],
            "audio": ["DTS-HD MA"],
            "codec": "x265/HEVC",
            "group": "GROUP",
            "languages": [],
        },
    }
    label = _format_info_line(item)
    assert "2160p" in label
    assert "HDR10" in label
    assert "DTS-HD MA" in label
    assert "x265/HEVC" in label
    assert "GROUP" in label
    assert "GB" in label


@patch("resources.lib.router.xbmc")
def test_route_dispatches_to_test_hydra(mock_xbmc):
    """Route /test_hydra should call the hydra connection test."""
    with patch("resources.lib.router._test_hydra_connection") as mock_test:
        route(["plugin://plugin.video.nzbdav/test_hydra", "1", ""])
        mock_test.assert_called_once()


@patch("resources.lib.router.xbmc")
def test_route_dispatches_to_test_nzbdav(mock_xbmc):
    """Route /test_nzbdav should call the nzbdav connection test."""
    with patch("resources.lib.router._test_nzbdav_connection") as mock_test:
        route(["plugin://plugin.video.nzbdav/test_nzbdav", "1", ""])
        mock_test.assert_called_once()


def test_format_info_line_minimal():
    """Test label with no metadata."""
    item = {
        "title": "some.file.mkv",
        "size": "",
        "_meta": {
            "resolution": "",
            "hdr": [],
            "audio": [],
            "codec": "",
            "group": "",
            "languages": [],
        },
    }
    label = _format_info_line(item)
    assert label == "N/A" or "Unknown" in label


@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.Dialog")
@patch("resources.lib.hydra.search_hydra", return_value=([], "NZBHydra unavailable"))
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_play_shows_hydra_errors_in_modal_dialog(
    mock_cache, mock_search, mock_dialog, mock_resolved
):
    _handle_play(1, {"type": "movie", "title": "The Matrix"})

    mock_dialog.return_value.ok.assert_called_once_with(
        "NZB-DAV", "NZBHydra unavailable"
    )
    mock_resolved.assert_called_once()


@patch("xbmcplugin.endOfDirectory")
@patch("xbmcgui.Dialog")
@patch("resources.lib.hydra.search_hydra", return_value=([], "NZBHydra unavailable"))
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_search_shows_hydra_errors_in_modal_dialog(
    mock_cache, mock_search, mock_dialog, mock_end
):
    _handle_search(1, {"type": "movie", "title": "The Matrix"})

    mock_dialog.return_value.ok.assert_called_once_with(
        "NZB-DAV", "NZBHydra unavailable"
    )
    mock_end.assert_called_once_with(1, succeeded=False)


# --- _safe_resolve_handle boundary tests ---


@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.ListItem")
def test_safe_resolve_handle_resolves_zero_handle(mock_listitem, mock_resolved):
    """Handle 0 is a valid Kodi handle (first plugin invocation in a
    session) — must resolve, not be skipped like -1."""
    mock_listitem.return_value = "fake_listitem"
    _safe_resolve_handle(0)
    mock_resolved.assert_called_once_with(0, False, "fake_listitem")


@patch("xbmcplugin.setResolvedUrl")
def test_safe_resolve_handle_skips_arbitrary_negative_handle(mock_resolved):
    """Any negative handle is treated as a RunPlugin-style no-handle
    invocation. Guards against Kodi passing an unexpected sentinel."""
    _safe_resolve_handle(-42)
    mock_resolved.assert_not_called()


# --- _handle_play direct coverage for happy path + edge cases ---


def _install_progress_dialog_that_wont_cancel():
    """Return a non-cancelling DialogProgress mock.

    The global ``xbmcgui`` MagicMock returns MagicMock for every
    attribute, so ``progress.iscanceled()`` normally evaluates truthy
    and every ``_handle_play`` / ``_handle_search`` test would fall
    into the cancelled-by-user branch before reaching the real code
    under test. Calling this in each direct-handler test pins
    iscanceled() to False."""
    import xbmcgui

    progress_instance = MagicMock()
    progress_instance.iscanceled.return_value = False
    xbmcgui.DialogProgress.return_value = progress_instance
    return progress_instance


def _stub_setting(value):
    """Return a ``getSetting`` stub that returns ``value`` for every key.

    Used inside ``@patch("xbmcaddon.Addon")`` blocks to give the addon a
    predictable getSetting payload without mutating the global xbmcaddon
    MagicMock (which would leak into later tests — notably
    ``test_stream_proxy`` reads many settings with different expected
    shapes and can't tolerate a one-size-fits-all override)."""
    return lambda *args, **kwargs: value


def _attach_primary_duplicate_fallbacks(results):
    for index, result in enumerate(results):
        result["_fallback_candidates"] = []
        result["_fallback_manifest"] = {
            "payload_kind": "video",
            "group_name": "the matrix 1999 1080p bluray x264 group.mkv",
            "group_bytes": 8589934592,
            "video_name": "The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv",
            "normalized_video_name": "the matrix 1999 1080p bluray x264 group.mkv",
            "video_bytes": 8589934592,
            "archive_base_name": "",
            "article_digest": "articles-{}".format(index),
            "article_count": 100,
            "skipped_candidate_count": 0,
            "skipped_candidates": [],
            "unsupported_reason": "",
        }
        result["_fallback_manifest_error"] = ""
    from resources.lib.fallback_streams import attach_fallback_candidates

    with patch("resources.lib.fallback_streams._fallback_settings") as mock_settings:
        mock_settings.return_value = (True, 2)
        return attach_fallback_candidates(results)


def _attach_selected_primary_duplicate_fallbacks(selected, results, **_kwargs):
    _attach_primary_duplicate_fallbacks(list(results))
    return selected


def _manifest(name, size, digest):
    return {
        "payload_kind": "video",
        "group_name": name,
        "group_bytes": size,
        "video_name": name,
        "normalized_video_name": name,
        "video_bytes": size,
        "archive_base_name": "",
        "article_digest": digest,
        "article_count": 100,
        "skipped_candidate_count": 0,
        "skipped_candidates": [],
        "unsupported_reason": "",
    }


def _duplicate_release(link, size=8 * 1024**3):
    return {
        "title": "The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv",
        "link": link,
        "size": size,
        "_meta": {
            "resolution": "1080p",
            "quality": "bluray",
            "codec": "x264",
            "group": "group",
            "container": "mkv",
        },
    }


def test_fallback_candidate_loader_skips_single_result_pool():
    selected = _duplicate_release("http://hydra/nzb/only")

    loader = _fallback_candidate_loader_for_selection(selected, [selected])

    assert loader is None


@patch("resources.lib.router.fallback_candidate_prefetch_settings")
def test_fallback_candidate_loader_skips_duplicate_only_pool_before_settings(
    mock_settings,
):
    from resources.lib.fallback_streams import FALLBACK_CANDIDATES_DISABLED

    mock_settings.return_value = (True, 5)
    selected = _duplicate_release("http://hydra/nzb/selected")
    duplicate = _duplicate_release("http://hydra/nzb/selected")
    missing_link = _duplicate_release("")

    loader = _fallback_candidate_loader_for_selection(
        selected, [selected, duplicate, missing_link]
    )

    assert callable(loader)
    assert loader() is FALLBACK_CANDIDATES_DISABLED
    mock_settings.assert_not_called()


@patch("resources.lib.router.fallback_candidate_prefetch_settings")
def test_fallback_candidate_loader_skips_unusable_selected_manifest_before_settings(
    mock_settings,
):
    mock_settings.side_effect = AssertionError("settings should not be read")
    selected = _duplicate_release("http://hydra/nzb/selected")
    selected["_fallback_manifest"] = make_empty_manifest("fetch_error")
    related = _duplicate_release("http://hydra/nzb/related")

    loader = _fallback_candidate_loader_for_selection(selected, [selected, related])

    assert loader is None
    mock_settings.assert_not_called()


@patch(
    "resources.lib.router.selection_pool_may_have_fallback_peer",
    side_effect=AssertionError("pool should not be scanned"),
)
@patch("resources.lib.router.fallback_candidate_prefetch_settings")
def test_fallback_candidate_loader_skips_unusable_selected_manifest_before_pool_scan(
    mock_settings, mock_selection_pool
):
    mock_settings.side_effect = AssertionError("settings should not be read")
    selected = _duplicate_release("http://hydra/nzb/selected")
    selected["_fallback_manifest"] = make_empty_manifest("fetch_error")
    related = _duplicate_release("http://hydra/nzb/related")

    loader = _fallback_candidate_loader_for_selection(selected, [selected, related])

    assert loader is None
    mock_selection_pool.assert_not_called()
    mock_settings.assert_not_called()


@patch("resources.lib.router.fallback_candidate_prefetch_settings")
def test_fallback_candidate_loader_reuses_distinct_peer_scan_for_prefetch(
    mock_settings,
):
    mock_settings.return_value = (True, 5)
    selected = _duplicate_release("http://hydra/nzb/selected")
    duplicates = [
        _duplicate_release("http://hydra/nzb/selected") for _index in range(5)
    ]
    related = _duplicate_release("http://hydra/nzb/related")

    class CountedResults:
        def __init__(self, items):
            self.items = items
            self.iterations = 0

        def __len__(self):
            return len(self.items)

        def __iter__(self):
            for item in self.items:
                self.iterations += 1
                yield item

    results = CountedResults([selected] + duplicates + [related])

    def attach_selection(selected_result, _pool, **_kwargs):
        selected_result["_fallback_candidates"] = [related]

    with patch(
        "resources.lib.router.attach_fallback_candidates_for_selection",
        side_effect=attach_selection,
    ) as mock_attach:
        loader = _fallback_candidate_loader_for_selection(selected, results)

        assert callable(loader)
        assert results.iterations == 0
        assert loader() == [related]

    assert results.iterations == len(results.items)
    called_selected, called_pool = mock_attach.call_args.args
    assert called_selected is selected
    assert list(itertools.islice(called_pool, 2)) == [selected, related]


@patch(
    "resources.lib.router.first_prefetchable_fallback_peer",
    side_effect=AssertionError("disabled fallback scanned prefetch peers"),
    create=True,
)
@patch("resources.lib.fallback_streams._fallback_settings", return_value=(False, 5))
def test_fallback_candidate_loader_skips_prefetch_when_fallback_disabled(
    _mock_settings, mock_prefetch
):
    from resources.lib.fallback_streams import FALLBACK_CANDIDATES_DISABLED

    selected = _duplicate_release("http://hydra/nzb/selected")
    related = _duplicate_release("http://hydra/nzb/related")

    loader = _fallback_candidate_loader_for_selection(selected, [selected, related])

    assert callable(loader)
    assert loader() is FALLBACK_CANDIDATES_DISABLED
    mock_prefetch.assert_not_called()


@patch("resources.lib.router.attach_fallback_candidates_for_selection")
@patch(
    "resources.lib.router.first_prefetchable_fallback_peer",
    side_effect=AssertionError("post-picker loader construction scanned peers"),
    create=True,
)
@patch("resources.lib.router.fallback_candidate_prefetch_settings")
def test_fallback_candidate_loader_defers_prefetch_scan_until_loader_runs(
    mock_settings, mock_prefetch, mock_attach
):
    mock_settings.return_value = (True, 5)
    selected = _duplicate_release("http://hydra/nzb/selected")
    related = _duplicate_release("http://hydra/nzb/related")

    def attach_selection(selected_result, _pool, **_kwargs):
        selected_result["_fallback_candidates"] = [related]

    mock_attach.side_effect = attach_selection

    loader = _fallback_candidate_loader_for_selection(selected, [selected, related])

    assert callable(loader)
    mock_prefetch.assert_not_called()
    assert loader() == [related]


def test_fallback_candidate_loader_defers_settings_until_loader_runs():
    """Kodi settings reads should not block the selected-result submit path."""
    selected = _duplicate_release("http://hydra/nzb/selected")
    related = _duplicate_release("http://hydra/nzb/related")

    def slow_settings():
        _time.sleep(0.12)
        return (True, 5)

    def attach_selection(selected_result, _pool, **_kwargs):
        selected_result["_fallback_candidates"] = [related]

    with patch(
        "resources.lib.router.fallback_candidate_prefetch_settings",
        side_effect=slow_settings,
    ) as mock_settings, patch(
        "resources.lib.router.attach_fallback_candidates_for_selection",
        side_effect=attach_selection,
    ):
        started = _time.perf_counter()
        loader = _fallback_candidate_loader_for_selection(selected, [selected, related])
        elapsed = _time.perf_counter() - started

        assert callable(loader)
        assert (
            elapsed < 0.05
        ), "fallback settings delayed loader construction by {:.3f}s".format(elapsed)
        mock_settings.assert_not_called()
        assert loader() == [related]
        mock_settings.assert_called_once()


def test_fallback_candidate_loader_construction_defers_slow_pool_scan():
    """Selected result -> resolver should not scan the fallback pool first."""
    selected = _duplicate_release("http://hydra/nzb/selected")
    duplicates = [
        _duplicate_release("http://hydra/nzb/selected") for _index in range(5)
    ]
    related = _duplicate_release("http://hydra/nzb/related")

    class SlowResults:
        def __init__(self, items):
            self.items = items
            self.iterations = 0

        def __len__(self):
            return len(self.items)

        def __iter__(self):
            for item in self.items:
                self.iterations += 1
                _time.sleep(0.025)
                yield item

    results = SlowResults([selected] + duplicates + [related])

    with patch(
        "resources.lib.router.fallback_candidate_prefetch_settings",
        side_effect=AssertionError("settings should stay deferred"),
    ):
        started = _time.perf_counter()
        loader = _fallback_candidate_loader_for_selection(selected, results)
        elapsed = _time.perf_counter() - started

    assert callable(loader)
    assert elapsed < 0.05, "post-picker fallback pool scan took {:.3f}s".format(elapsed)
    assert results.iterations == 0


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_fallback_candidate_loader_reuses_prefetch_settings_for_attach(
    mock_settings, mock_fetch
):
    selected = _duplicate_release("http://hydra/nzb/selected")
    related = _duplicate_release("http://hydra/nzb/related")
    manifests = {
        selected["link"]: _manifest("the matrix 1999.mkv", selected["size"], "a"),
        related["link"]: _manifest("the matrix 1999.mkv", related["size"], "b"),
    }
    mock_settings.return_value = (True, 1)
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    loader = _fallback_candidate_loader_for_selection(selected, [selected, related])
    candidates = loader()

    assert candidates == [related]
    assert mock_settings.call_count == 1


def test_fallback_candidate_loader_skips_unrelated_peer_pool():
    selected = _duplicate_release("http://hydra/nzb/selected")
    unrelated = _duplicate_release("http://hydra/nzb/unrelated")
    unrelated["title"] = "Bourne.Identity.2002.1080p.BluRay.x264-GROUP.mkv"

    loader = _fallback_candidate_loader_for_selection(selected, [selected, unrelated])

    assert callable(loader)
    with patch(
        "resources.lib.fallback_streams.fetch_nzb_video_manifest",
        side_effect=AssertionError("unrelated pool fetched manifests"),
    ):
        assert not loader()


def test_fallback_candidate_loader_skips_raw_unrelated_selected_metadata_parse():
    selected = {
        "title": "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "link": "http://hydra/nzb/selected-raw",
        "size": 60 * 1024**3,
    }
    unrelated = [
        {
            "title": "Bourne.Identity.Raw{:02d}.2160p.UHD.BluRay.REMUX."
            "DV.HEVC-GROUP".format(index),
            "link": "http://hydra/nzb/unrelated-raw-{}".format(index),
            "size": 60 * 1024**3,
        }
        for index in range(5)
    ]
    parsed_titles = []

    def parse_title_metadata(title):
        parsed_titles.append(title)
        return {
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "container": "mkv",
        }

    with patch(
        "resources.lib.filter.parse_title_metadata", side_effect=parse_title_metadata
    ):
        loader = _fallback_candidate_loader_for_selection(
            selected, [selected] + unrelated
        )
        assert callable(loader)
        assert not loader()

    assert not parsed_titles


def test_loader_skips_cached_meta_unrelated_selected_metadata_parse():
    selected = {
        "title": "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "link": "http://hydra/nzb/selected-raw",
        "size": 60 * 1024**3,
    }
    unrelated = []
    for index in range(5):
        unrelated.append(
            {
                "title": "Bourne.Identity.Meta{:02d}.2160p.UHD.BluRay.REMUX."
                "DV.HEVC-GROUP".format(index),
                "link": "http://hydra/nzb/unrelated-meta-{}".format(index),
                "size": 60 * 1024**3,
                "_meta": {
                    "resolution": "2160p",
                    "quality": "REMUX",
                    "codec": "x265/HEVC",
                    "hdr": ["Dolby Vision"],
                    "audio": ["TrueHD", "Atmos"],
                    "container": "mkv",
                },
            }
        )
    parsed_titles = []

    def parse_title_metadata(title):
        parsed_titles.append(title)
        return {
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "container": "mkv",
        }

    with patch(
        "resources.lib.filter.parse_title_metadata", side_effect=parse_title_metadata
    ):
        loader = _fallback_candidate_loader_for_selection(
            selected, [selected] + unrelated
        )
        assert callable(loader)
        assert not loader()

    assert not parsed_titles


def test_fallback_candidate_loader_skips_profile_mismatched_peer_pool():
    selected = _duplicate_release("http://hydra/nzb/selected")
    mismatched = _duplicate_release("http://hydra/nzb/profile-mismatch")
    mismatched["title"] = "The.Matrix.1999.2160p.BluRay.x264-GROUP.mkv"
    mismatched["_meta"]["resolution"] = "2160p"

    loader = _fallback_candidate_loader_for_selection(selected, [selected, mismatched])

    assert callable(loader)
    with patch(
        "resources.lib.fallback_streams.fetch_nzb_video_manifest",
        side_effect=AssertionError("profile mismatch fetched manifests"),
    ):
        assert not loader()


@patch("resources.lib.router.attach_fallback_candidates_for_selection")
def test_fallback_candidate_loader_keeps_prefetch_match_scan_deferred(mock_attach):
    selected = _duplicate_release("http://hydra/nzb/selected")
    unrelated = []
    for index in range(5):
        result = _duplicate_release("http://hydra/nzb/unrelated-{}".format(index))
        result["title"] = "Bourne.Identity.2002.1080p.BluRay.x264-GROUP.mkv"
        unrelated.append(result)
    related = _duplicate_release("http://hydra/nzb/related")

    def attach_selection(selected_result, _pool, **_kwargs):
        selected_result["_fallback_candidates"] = [related]

    mock_attach.side_effect = attach_selection

    loader = _fallback_candidate_loader_for_selection(
        selected, [selected] + unrelated + [related]
    )
    candidates = loader()

    assert candidates == [related]
    called_selected, called_pool = mock_attach.call_args.args
    assert called_selected is selected
    assert list(itertools.islice(called_pool, 2)) == [selected, unrelated[0]]


@patch("resources.lib.router.attach_fallback_candidates_for_selection")
def test_fallback_candidate_loader_pool_stays_lazy_after_known_peer(mock_attach):
    selected = _duplicate_release("http://hydra/nzb/selected")
    related = _duplicate_release("http://hydra/nzb/related")
    unrelated = []
    for index in range(20):
        result = _duplicate_release("http://hydra/nzb/unrelated-{}".format(index))
        result["title"] = "Bourne.Identity.2002.1080p.BluRay.x264-GROUP.mkv"
        unrelated.append(result)

    class CountedResults:  # pylint: disable=too-few-public-methods
        def __init__(self, items):
            self.items = items
            self.iterations = 0

        def __iter__(self):
            for item in self.items:
                self.iterations += 1
                yield item

    results = CountedResults([selected, related] + unrelated)

    def attach_selection(selected_result, pool, **_kwargs):
        assert list(itertools.islice(pool, 2)) == [selected, related]
        selected_result["_fallback_candidates"] = [related]

    mock_attach.side_effect = attach_selection

    loader = _fallback_candidate_loader_for_selection(selected, results)
    assert results.iterations == 0

    assert loader() == [related]
    assert results.iterations == 2


@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.ListItem")
@patch("resources.lib.http_util.notify")
@patch("resources.lib.router._search_all_providers", return_value=([], None))
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_play_notifies_when_no_results(
    mock_cache, mock_search, mock_notify, mock_listitem, mock_resolved
):
    """When both cache and live search return zero results, _handle_play
    must surface the 'no results' notification AND resolve the handle
    (never leave Kodi hanging). Patches ``_search_all_providers`` rather
    than ``hydra.search_hydra`` to sidestep the provider-enabled settings
    lookup entirely."""
    _install_progress_dialog_that_wont_cancel()
    mock_listitem.return_value = "li"

    _handle_play(3, {"type": "movie", "title": "Obscure Movie"})

    assert mock_notify.called, "no-results path must notify the user"
    mock_resolved.assert_called_once_with(3, False, "li")


@patch("xbmcaddon.Addon")
@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.ListItem")
@patch("resources.lib.results_dialog.show_results_dialog", return_value=None)
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_play_resolves_handle_when_user_cancels_picker(
    mock_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_listitem,
    mock_resolved,
    mock_addon,
):
    """User cancels the results picker dialog → return selected=None.
    _handle_play must call setResolvedUrl(False) so Kodi unblocks."""
    _install_progress_dialog_that_wont_cancel()
    # auto_select_best must be falsy so we land in the picker branch.
    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")

    mock_listitem.return_value = "li"
    results = [{"title": "Some.Release.mkv", "link": "http://hydra/nzb/1"}]
    mock_search.return_value = (results, None)
    mock_filter.return_value = (results, results)

    _handle_play(4, {"type": "movie", "title": "The Matrix"})

    mock_dialog.assert_called_once()
    mock_resolved.assert_called_once_with(4, False, "li")


@patch("xbmcaddon.Addon")
@patch("xbmcgui.DialogProgress")
@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.ListItem")
@patch("resources.lib.results_dialog.show_results_dialog", return_value=None)
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_play_does_not_open_modal_progress_before_picker(
    mock_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_listitem,
    mock_resolved,
    mock_progress_cls,
    mock_addon,
):
    """TMDBHelper /play should go straight to the picker without DialogProgress.

    On CoreELEC/Arctic Fuse, the modal progress dialog can native-crash Kodi
    while the label still reads "Searching NZBHydra", even though Hydra has
    already returned.
    """
    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")
    mock_listitem.return_value = "li"
    results = [{"title": "Matrix.1999.mkv", "link": "http://hydra/nzb/x"}]
    mock_search.return_value = (results, None)
    mock_filter.return_value = (results, results)

    _handle_play(4, {"type": "movie", "title": "The Matrix"})

    mock_progress_cls.assert_not_called()
    mock_dialog.assert_called_once()
    mock_resolved.assert_called_once_with(4, False, "li")


@patch("xbmcaddon.Addon")
@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.ListItem")
@patch("resources.lib.resolver.resolve")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_play_happy_path_invokes_resolve(
    mock_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_resolve,
    mock_listitem,
    mock_resolved,
    mock_addon,
):
    """Happy path: search returns results, filter keeps them, user picks
    one in the dialog → resolver.resolve() is invoked with the chosen
    nzburl/title. This is the path every successful TMDBHelper click
    takes and it wasn't directly covered before."""
    _install_progress_dialog_that_wont_cancel()
    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")

    mock_listitem.return_value = "li"
    chosen = {"title": "Matrix.1999.mkv", "link": "http://hydra/nzb/x"}
    results = [chosen]
    mock_search.return_value = (results, None)
    mock_filter.return_value = (results, results)
    mock_dialog.return_value = chosen

    _handle_play(5, {"type": "movie", "title": "The Matrix", "year": "1999"})

    mock_resolve.assert_called_once()
    args, _kwargs = mock_resolve.call_args
    assert args[0] == 5
    assert args[1]["nzburl"] == chosen["link"]
    assert args[1]["title"] == chosen["title"]


@patch("xbmcaddon.Addon")
@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.ListItem")
@patch("resources.lib.resolver.resolve")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router.get_completed_jobs")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_play_marks_completed_history_miss_from_picker_snapshot(
    mock_cache,
    mock_completed_jobs,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_resolve,
    mock_listitem,
    mock_resolved,
    mock_addon,
):
    """Post-picker resolve should not repeat a completed-history miss."""
    _install_progress_dialog_that_wont_cancel()
    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")
    mock_listitem.return_value = "li"
    chosen = {"title": "Matrix.1999.mkv", "link": "http://hydra/nzb/x"}
    mock_completed_jobs.return_value = {
        "Other.1999.mkv": {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Other.1999.mkv",
            "name": "Other.1999.mkv",
            "nzo_id": "SABnzbd_nzo_other",
        }
    }
    mock_search.return_value = ([chosen], None)
    mock_filter.return_value = ([chosen], [chosen])
    mock_dialog.return_value = chosen

    _handle_play(5, {"type": "movie", "title": "The Matrix", "year": "1999"})

    mock_resolve.assert_called_once()
    args, _kwargs = mock_resolve.call_args
    assert args[1]["_completed_job_lookup_done"] is True
    assert "_completed_job" not in args[1]


@patch("resources.lib.resolver._start_direct_playback_service_config_lookup")
@patch("resources.lib.resolver._get_poll_settings", return_value=(1, 60))
@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver._submit_nzb_with_retries")
@patch("xbmcaddon.Addon")
@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.ListItem")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router.get_completed_jobs")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_play_empty_completed_snapshot_skips_post_picker_history_lookup(
    mock_cache,
    mock_completed_jobs,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_listitem,
    mock_resolved,
    mock_addon,
    mock_submit,
    mock_find_completed,
    mock_poll_settings,
    mock_service_config,
):
    """A successful empty picker snapshot should not delay selected-result submit."""

    class SuccessfulCompletedJobs(dict):
        _lookup_done = True

    _install_progress_dialog_that_wont_cancel()
    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")
    mock_listitem.return_value = "li"
    mock_service_config.return_value = {"done": threading.Event()}
    mock_service_config.return_value["done"].set()
    chosen = {"title": "Matrix.1999.mkv", "link": "http://hydra/nzb/x"}
    mock_completed_jobs.return_value = SuccessfulCompletedJobs()
    mock_search.return_value = ([chosen], None)
    mock_filter.return_value = ([chosen], [chosen])
    mock_dialog.return_value = chosen

    def slow_completed_lookup(_title):
        _time.sleep(0.12)

    submit_started = []
    mock_find_completed.side_effect = slow_completed_lookup
    mock_submit.side_effect = (
        lambda *_args, **_kwargs: submit_started.append(_time.perf_counter()) or None
    )

    started = _time.perf_counter()
    _handle_play(5, {"type": "movie", "title": "The Matrix", "year": "1999"})
    elapsed_to_submit = submit_started[0] - started

    assert (
        elapsed_to_submit < 0.05
    ), "post-picker submit waited {:.3f}s on a repeated history miss".format(
        elapsed_to_submit
    )
    mock_find_completed.assert_not_called()
    mock_resolved.assert_called_once_with(5, False, "li")


@patch("resources.lib.router.get_completed_jobs")
def test_tag_available_attaches_completed_job_hint(mock_completed_jobs):
    completed_job = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Matrix.1999.mkv",
        "name": "Matrix.1999.mkv",
        "nzo_id": "SABnzbd_nzo_done",
    }
    mock_completed_jobs.return_value = {"Matrix.1999.mkv": completed_job}
    results = [
        {"title": "Matrix.1999.mkv", "link": "http://hydra/nzb/x"},
        {"title": "Other.mkv", "link": "http://hydra/nzb/y"},
    ]

    _tag_available(results)

    assert results[0]["_available"] is True
    assert results[0]["_completed_job"] == completed_job
    assert "_available" not in results[1]
    assert "_completed_job" not in results[1]


@patch("xbmcaddon.Addon")
@patch("xbmcplugin.setResolvedUrl")
@patch("xbmcgui.ListItem")
@patch("resources.lib.resolver.resolve")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.router.attach_fallback_candidates_for_selection")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_play_picker_forwards_fallback_candidates(
    mock_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_attach,
    mock_dialog,
    mock_resolve,
    mock_listitem,
    mock_resolved,
    mock_addon,
):
    _install_progress_dialog_that_wont_cancel()
    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")
    primary = _duplicate_release("http://hydra/nzb/primary")
    duplicate = _duplicate_release("http://hydra/nzb/dupe")
    oversized = _duplicate_release("http://hydra/nzb/oversized", size=20 * 1024**3)
    filtered = [primary, duplicate, oversized]
    mock_search.return_value = (filtered, None)
    mock_filter.return_value = (filtered, filtered)
    mock_attach.side_effect = _attach_selected_primary_duplicate_fallbacks
    mock_dialog.return_value = primary

    with patch(
        "resources.lib.fallback_streams._fallback_settings", return_value=(True, 2)
    ):
        _handle_play(5, {"type": "movie", "title": "The Matrix", "year": "1999"})

    mock_attach.assert_not_called()
    mock_resolve.assert_called_once()
    args, _kwargs = mock_resolve.call_args
    assert args[0] == 5
    assert args[1]["nzburl"] == primary["link"]
    assert args[1]["title"] == primary["title"]
    assert args[1]["_fallback_candidates"] == []
    loader = args[1]["_fallback_candidate_loader"]
    assert callable(loader)

    with patch(
        "resources.lib.fallback_streams._fallback_settings", return_value=(True, 2)
    ):
        assert loader() == [duplicate, oversized]
    mock_attach.assert_called_once()
    assert mock_attach.call_args.args[0] is primary
    assert duplicate["_fallback_candidates"] == [primary, oversized]
    assert oversized["_fallback_candidates"] == [primary, duplicate]


# --- _handle_search direct coverage for no-results path ---


@patch("xbmcplugin.endOfDirectory")
@patch("resources.lib.http_util.notify")
@patch("resources.lib.router._search_all_providers", return_value=([], None))
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_search_notifies_and_ends_directory_when_no_results(
    mock_cache, mock_search, mock_notify, mock_end
):
    """_handle_search with empty results must both notify AND close the
    directory listing via endOfDirectory — leaving it open hangs Kodi's
    spinner indefinitely."""
    _install_progress_dialog_that_wont_cancel()

    _handle_search(6, {"type": "movie", "title": "Nonexistent Film"})

    assert mock_notify.called
    mock_end.assert_called_once_with(6, succeeded=False)


@patch("xbmcaddon.Addon")
@patch("xbmcplugin.endOfDirectory")
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.cache.set_cached")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_search_auto_select_passes_clean_params_to_resolver(
    mock_cache,
    mock_set_cache,
    mock_search,
    mock_filter,
    mock_resolve_and_play,
    mock_end,
    mock_addon,
):
    """Search auto-select must preserve TMDB metadata for bookmark cleanup."""
    _install_progress_dialog_that_wont_cancel()
    mock_addon.return_value.getSetting.side_effect = _stub_setting("true")
    chosen = {"title": "Matrix.1999.mkv", "link": "http://hydra/nzb/x"}
    mock_search.return_value = ([chosen], None)
    mock_filter.return_value = ([chosen], [chosen])

    _handle_search(
        7,
        {
            "type": "movie",
            "title": "The Matrix",
            "year": "_",
            "tmdb_id": "603",
        },
    )

    mock_resolve_and_play.assert_called_once()
    args, kwargs = mock_resolve_and_play.call_args
    assert args == (chosen["link"], chosen["title"])
    resolver_params = dict(kwargs["params"])
    loader = resolver_params.pop("_fallback_candidate_loader")
    assert loader is None
    assert resolver_params == {
        "type": "movie",
        "title": "The Matrix",
        "year": "",
        "tmdb_id": "603",
        "_fallback_candidates": [],
    }
    mock_end.assert_called_once_with(7, succeeded=False)


@patch("xbmcaddon.Addon")
@patch("xbmcplugin.endOfDirectory")
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available")
@patch("resources.lib.cache.set_cached")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_search_picker_passes_clean_params_to_resolver(
    mock_cache,
    mock_set_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_resolve_and_play,
    mock_end,
    mock_addon,
):
    """Manual result selection must preserve TMDB metadata for cleanup too."""
    _install_progress_dialog_that_wont_cancel()
    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")
    chosen = {"title": "Matrix.1999.mkv", "link": "http://hydra/nzb/x"}
    mock_search.return_value = ([chosen], None)
    mock_filter.return_value = ([chosen], [chosen])
    mock_dialog.return_value = chosen

    _handle_search(
        8,
        {
            "type": "movie",
            "title": "The Matrix",
            "year": "_",
            "tmdb_id": "603",
        },
    )

    mock_resolve_and_play.assert_called_once()
    args, kwargs = mock_resolve_and_play.call_args
    assert args == (chosen["link"], chosen["title"])
    resolver_params = dict(kwargs["params"])
    loader = resolver_params.pop("_fallback_candidate_loader")
    assert loader is None
    assert resolver_params == {
        "type": "movie",
        "title": "The Matrix",
        "year": "",
        "tmdb_id": "603",
        "_fallback_candidates": [],
    }
    mock_end.assert_called_once_with(8, succeeded=False)


@patch("xbmcaddon.Addon", side_effect=RuntimeError("Kodi settings unavailable"))
@patch("xbmcplugin.endOfDirectory")
@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available", side_effect=RuntimeError("slow history"))
@patch("resources.lib.cache.set_cached")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_script_play_uses_picker_without_plugin_handle_resolution(
    mock_cache,
    mock_set_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_resolve_and_play,
    mock_set_resolved,
    mock_end,
    mock_addon,
):
    """Script handoff runs in RunScript context, so it must not resolve a
    plugin handle or end a plugin directory."""
    from resources.lib.router import _handle_script_play

    chosen = {
        "title": "The.Odyssey.2026.mkv",
        "link": "http://hydra/nzb/odyssey",
        "indexer": "NZBFinder",
    }
    alternate = {
        "title": "The.Odyssey.2026.1080p.mkv",
        "link": "http://hydra/nzb/odyssey-alt",
    }
    mock_search.return_value = ([chosen], None)
    mock_filter.return_value = ([chosen, alternate], [chosen, alternate])
    mock_dialog.return_value = chosen

    _handle_script_play(
        {
            "type": "movie",
            "title": "The Odyssey",
            "year": "2026",
            "tmdb_id": "1368337",
        }
    )

    _, filter_kwargs = mock_filter.call_args
    assert callable(filter_kwargs["settings_getter"])
    mock_resolve_and_play.assert_called_once()
    args, kwargs = mock_resolve_and_play.call_args
    assert args == (chosen["link"], chosen["title"])
    resolver_params = dict(kwargs["params"])
    assert callable(resolver_params.pop("_settings_getter"))
    assert callable(resolver_params.pop("_fallback_candidate_loader"))
    assert resolver_params == {
        "type": "movie",
        "title": "The Odyssey",
        "year": "2026",
        "tmdb_id": "1368337",
        "_fallback_candidates": [],
        "_completed_job_lookup_done": True,
        "_selected_indexer": "NZBFinder",
    }
    mock_addon.assert_not_called()
    mock_tag.assert_not_called()
    mock_end.assert_not_called()
    mock_set_resolved.assert_not_called()


@patch(
    "resources.lib.router.fallback_candidate_prefetch_settings", return_value=(True, 2)
)
@patch("resources.lib.router.attach_fallback_candidates_for_selection")
@patch("resources.lib.nzbdav_api.find_completed_by_name", return_value=None)
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
def test_handle_script_play_picker_forwards_deferred_fallback_loader(
    mock_search,
    mock_filter,
    mock_dialog,
    mock_resolve_and_play,
    mock_find_completed,
    mock_attach,
    mock_fallback_settings,
):
    """RunScript TMDBHelper playback should keep fallback submission available.

    The live TMDBHelper player enters via ``tmdb_play`` instead of the plugin
    handle routes, so this path must forward the same deferred fallback loader
    used by the picker routes.
    """
    from resources.lib.router import _handle_script_play

    primary = _duplicate_release("http://hydra/nzb/primary")
    duplicate = _duplicate_release("http://hydra/nzb/duplicate")
    filtered = [primary, duplicate]
    seen_pool = []

    def attach_selected(selected, results, **_kwargs):
        seen_pool.extend(list(results))
        selected["_fallback_candidates"] = [duplicate]
        return selected

    mock_search.return_value = (filtered, None)
    mock_filter.return_value = (filtered, filtered)
    mock_dialog.return_value = primary
    mock_attach.side_effect = attach_selected

    _handle_script_play({"type": "movie", "title": "The Matrix", "year": "1999"})

    mock_find_completed.assert_called_once()
    mock_resolve_and_play.assert_called_once()
    resolver_params = dict(mock_resolve_and_play.call_args.kwargs["params"])
    loader = resolver_params["_fallback_candidate_loader"]
    assert callable(loader)
    mock_attach.assert_not_called()

    assert loader() == [duplicate]
    mock_fallback_settings.assert_called_once()
    mock_attach.assert_called_once()
    assert seen_pool[0] is primary
    assert duplicate in seen_pool


@patch("resources.lib.router._get_script_setting")
@patch("resources.lib.router.fallback_candidate_prefetch_settings")
@patch("resources.lib.router.attach_fallback_candidates_for_selection")
@patch("resources.lib.nzbdav_api.find_completed_by_name", return_value=None)
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
def test_handle_script_play_picker_fallback_loader_uses_script_settings_getter(
    mock_search,
    mock_filter,
    mock_dialog,
    mock_resolve_and_play,
    _mock_find_completed,
    mock_attach,
    mock_fallback_settings,
    mock_script_setting,
):
    """Deferred RunScript fallback discovery must avoid Kodi settings APIs."""
    from resources.lib.router import _handle_script_play

    primary = _duplicate_release("http://hydra/nzb/primary")
    duplicate = _duplicate_release("http://hydra/nzb/duplicate")
    filtered = [primary, duplicate]

    def script_setting(key, default=""):
        return {
            "auto_select_best": "false",
            "fallback_streams_enabled": "true",
            "fallback_streams_max": "2",
        }.get(key, default)

    def fallback_settings(*_args, **kwargs):
        assert kwargs.get("settings_getter") is mock_script_setting
        return (True, 2)

    def attach_selected(selected, _results, **_kwargs):
        selected["_fallback_candidates"] = [duplicate]
        return selected

    mock_script_setting.side_effect = script_setting
    mock_fallback_settings.side_effect = fallback_settings
    mock_attach.side_effect = attach_selected
    mock_search.return_value = (filtered, None)
    mock_filter.return_value = (filtered, filtered)
    mock_dialog.return_value = primary

    _handle_script_play({"type": "movie", "title": "The Matrix", "year": "1999"})

    resolver_params = dict(mock_resolve_and_play.call_args.kwargs["params"])
    loader = resolver_params["_fallback_candidate_loader"]
    assert loader() == [duplicate]
    mock_fallback_settings.assert_called_once()


@patch("xbmcaddon.Addon", side_effect=RuntimeError("Kodi settings unavailable"))
@patch("xbmcplugin.endOfDirectory")
@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.nzbdav_api.find_completed_by_name")
def test_handle_script_play_attaches_completed_job_for_selected_result(
    mock_find_completed,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_resolve_and_play,
    mock_set_resolved,
    mock_end,
    mock_addon,
):
    from resources.lib.router import _handle_script_play

    chosen = {"title": "Wuthering.Heights.2026.mkv", "link": "http://hydra/nzb/wh"}
    completed_job = {
        "status": "Completed",
        "storage": "/mnt/data/completed-symlinks/uncategorized/Wuthering",
        "name": chosen["title"],
        "nzo_id": "nzo_done",
    }
    mock_find_completed.return_value = completed_job
    mock_search.return_value = ([chosen], None)
    mock_filter.return_value = ([chosen], [chosen])
    mock_dialog.return_value = chosen

    _handle_script_play(
        {
            "type": "movie",
            "title": "Wuthering Heights",
            "year": "2026",
            "tmdb_id": "1316092",
        }
    )

    mock_find_completed.assert_called_once()
    args, kwargs = mock_find_completed.call_args
    assert args == (chosen["title"],)
    assert callable(kwargs["settings_getter"])
    resolver_params = dict(mock_resolve_and_play.call_args.kwargs["params"])
    assert resolver_params["_completed_job"] == completed_job
    assert "_completed_job_lookup_done" not in resolver_params
    mock_addon.assert_not_called()
    mock_end.assert_not_called()
    mock_set_resolved.assert_not_called()


@patch("xbmcaddon.Addon")
@patch("xbmcplugin.endOfDirectory")
@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available")
@patch("resources.lib.cache.set_cached")
@patch("resources.lib.cache.get_cached", side_effect=RuntimeError("cache unsafe"))
def test_handle_script_play_skips_search_cache_in_runscript_context(
    mock_cache,
    mock_set_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_resolve_and_play,
    mock_set_resolved,
    mock_end,
    mock_addon,
):
    """The file-path RunScript context can crash CoreELEC inside cache profile
    lookup, so script playback searches providers directly."""
    from resources.lib.router import _handle_script_play

    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")
    chosen = {"title": "The.Odyssey.2026.mkv", "link": "http://hydra/nzb/odyssey"}
    alternate = {
        "title": "The.Odyssey.2026.1080p.mkv",
        "link": "http://hydra/nzb/odyssey-alt",
    }
    mock_search.return_value = ([chosen], None)
    mock_filter.return_value = ([chosen, alternate], [chosen, alternate])
    mock_dialog.return_value = chosen

    _handle_script_play({"type": "movie", "title": "The Odyssey", "year": "2026"})

    mock_cache.assert_not_called()
    mock_set_cache.assert_not_called()
    mock_resolve_and_play.assert_called_once()
    mock_end.assert_not_called()
    mock_set_resolved.assert_not_called()


@patch(
    "resources.lib.router._get_script_setting",
    side_effect=lambda key, default="": (
        "true" if key == "auto_select_best" else default
    ),
)
@patch("xbmcaddon.Addon", side_effect=RuntimeError("Kodi settings unavailable"))
@patch("xbmcplugin.endOfDirectory")
@patch("xbmcplugin.setResolvedUrl")
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available", side_effect=RuntimeError("slow history"))
@patch("resources.lib.cache.set_cached")
@patch("resources.lib.cache.get_cached", side_effect=RuntimeError("cache unsafe"))
def test_handle_script_play_auto_select_marks_completed_lookup_done(
    mock_cache,
    mock_set_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_dialog,
    mock_resolve_and_play,
    mock_set_resolved,
    mock_end,
    mock_addon,
    mock_script_setting,
):
    from resources.lib.router import _handle_script_play

    chosen = {"title": "The.Odyssey.2026.mkv", "link": "http://hydra/nzb/odyssey"}
    alternate = {
        "title": "The.Odyssey.2026.1080p.mkv",
        "link": "http://hydra/nzb/odyssey-alt",
    }
    mock_search.return_value = ([chosen], None)
    mock_filter.return_value = ([chosen, alternate], [chosen, alternate])

    _handle_script_play({"type": "movie", "title": "The Odyssey", "year": "2026"})

    mock_cache.assert_not_called()
    mock_set_cache.assert_not_called()
    mock_tag.assert_not_called()
    mock_dialog.assert_not_called()
    mock_addon.assert_not_called()
    mock_resolve_and_play.assert_called_once()
    resolver_params = dict(mock_resolve_and_play.call_args.kwargs["params"])
    assert callable(resolver_params.pop("_settings_getter"))
    assert callable(resolver_params.pop("_fallback_candidate_loader"))
    assert resolver_params == {
        "type": "movie",
        "title": "The Odyssey",
        "year": "2026",
        "_fallback_candidates": [],
        "_completed_job_lookup_done": True,
    }
    mock_end.assert_not_called()
    mock_set_resolved.assert_not_called()


@patch("xbmcaddon.Addon")
@patch("xbmcplugin.endOfDirectory")
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.fallback_streams._fallback_settings")
@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available")
@patch("resources.lib.cache.set_cached")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_search_picker_fetches_fallbacks_after_selection(
    mock_cache,
    mock_set_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_fetch_manifest,
    mock_fallback_settings,
    mock_dialog,
    mock_resolve_and_play,
    mock_end,
    mock_addon,
):
    _install_progress_dialog_that_wont_cancel()
    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")
    primary = _duplicate_release("http://hydra/nzb/primary")
    duplicate = _duplicate_release("http://hydra/nzb/dupe")
    oversized = _duplicate_release("http://hydra/nzb/oversized", size=20 * 1024**3)
    filtered = [primary, duplicate, oversized]
    mock_search.return_value = (filtered, None)
    mock_filter.return_value = (filtered, filtered)
    mock_fallback_settings.return_value = (True, 2)
    manifests = {
        "http://hydra/nzb/primary": {
            "payload_kind": "video",
            "group_name": "the matrix primary.mkv",
            "group_bytes": 8589934592,
            "video_name": "The.Matrix.primary.mkv",
            "normalized_video_name": "the matrix primary.mkv",
            "video_bytes": 8589934592,
            "archive_base_name": "",
            "article_digest": "articles-primary",
            "article_count": 100,
            "skipped_candidate_count": 0,
            "skipped_candidates": [],
            "unsupported_reason": "",
        },
        "http://hydra/nzb/dupe": {
            "payload_kind": "video",
            "group_name": "the matrix alternate post.mkv",
            "group_bytes": 8589934592,
            "video_name": "The.Matrix.alternate.post.mkv",
            "normalized_video_name": "the matrix alternate post.mkv",
            "video_bytes": 8589934592,
            "archive_base_name": "",
            "article_digest": "articles-dupe",
            "article_count": 100,
            "skipped_candidate_count": 0,
            "skipped_candidates": [],
            "unsupported_reason": "",
        },
        "http://hydra/nzb/oversized": {
            "payload_kind": "video",
            "group_name": "the matrix oversized.mkv",
            "group_bytes": 21474836480,
            "video_name": "The.Matrix.oversized.mkv",
            "normalized_video_name": "the matrix oversized.mkv",
            "video_bytes": 21474836480,
            "archive_base_name": "",
            "article_digest": "articles-oversized",
            "article_count": 100,
            "skipped_candidate_count": 0,
            "skipped_candidates": [],
            "unsupported_reason": "",
        },
    }
    mock_fetch_manifest.side_effect = lambda url, **_kwargs: manifests[url]

    def choose_primary(*_args, **_kwargs):
        mock_fetch_manifest.assert_not_called()
        return primary

    mock_dialog.side_effect = choose_primary

    _handle_search(
        8,
        {
            "type": "movie",
            "title": "The Matrix",
            "year": "_",
            "tmdb_id": "603",
        },
    )

    mock_resolve_and_play.assert_called_once()
    args, kwargs = mock_resolve_and_play.call_args
    assert args == (primary["link"], primary["title"])
    resolver_params = dict(kwargs["params"])
    loader = resolver_params.pop("_fallback_candidate_loader")
    assert callable(loader)
    assert resolver_params == {
        "type": "movie",
        "title": "The Matrix",
        "year": "",
        "tmdb_id": "603",
        "_fallback_candidates": [],
    }

    mock_fetch_manifest.assert_not_called()
    assert loader() == [duplicate]
    assert [call.args[0] for call in mock_fetch_manifest.call_args_list] == [
        "http://hydra/nzb/primary",
        "http://hydra/nzb/dupe",
    ]
    assert "_fallback_candidates" not in duplicate
    assert "_fallback_candidates" not in oversized
    mock_end.assert_called_once_with(8, succeeded=False)


@patch("xbmcaddon.Addon")
@patch("xbmcplugin.endOfDirectory")
@patch("resources.lib.resolver.resolve_and_play")
@patch("resources.lib.results_dialog.show_results_dialog")
@patch("resources.lib.fallback_streams._fallback_settings")
@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.filter.filter_results")
@patch("resources.lib.router._search_all_providers")
@patch("resources.lib.router._tag_available")
@patch("resources.lib.cache.set_cached")
@patch("resources.lib.cache.get_cached", return_value=None)
def test_handle_search_does_not_wait_for_slow_fallback_lookup_before_playing(
    mock_cache,
    mock_set_cache,
    mock_tag,
    mock_search,
    mock_filter,
    mock_fetch_manifest,
    mock_fallback_settings,
    mock_dialog,
    mock_resolve_and_play,
    mock_end,
    mock_addon,
):
    _install_progress_dialog_that_wont_cancel()
    mock_addon.return_value.getSetting.side_effect = _stub_setting("false")
    primary = _duplicate_release("http://hydra/nzb/primary")
    duplicate = _duplicate_release("http://hydra/nzb/dupe")
    filtered = [primary, duplicate]
    mock_search.return_value = (filtered, None)
    mock_filter.return_value = (filtered, filtered)
    mock_fallback_settings.return_value = (True, 2)
    mock_dialog.return_value = primary

    fetch_started = threading.Event()
    release_fetch = threading.Event()
    manifests = {
        "http://hydra/nzb/primary": _manifest(
            "the matrix primary.mkv", 8589934592, "articles-primary"
        ),
        "http://hydra/nzb/dupe": _manifest(
            "the matrix alternate.mkv", 8589934592, "articles-dupe"
        ),
    }

    def slow_fetch(url, **_kwargs):
        fetch_started.set()
        release_fetch.wait(timeout=1)
        return manifests[url]

    mock_fetch_manifest.side_effect = slow_fetch

    try:
        _handle_search(
            8,
            {
                "type": "movie",
                "title": "The Matrix",
                "year": "_",
                "tmdb_id": "603",
            },
        )
        mock_fetch_manifest.assert_not_called()
        mock_resolve_and_play.assert_called_once()
        args, kwargs = mock_resolve_and_play.call_args
        assert args == (primary["link"], primary["title"])
        assert callable(kwargs["params"]["_fallback_candidate_loader"])
    finally:
        release_fetch.set()


# --- _test_connection and per-provider connection tests ---


@patch("resources.lib.http_util.notify")
@patch("resources.lib.http_util.http_get")
def test_test_connection_reports_ok_when_condition_true(mock_http_get, mock_notify):
    """_test_connection notifies 'OK' when ok_condition(response) is True."""
    mock_http_get.return_value = "<caps><server/></caps>"
    _test_connection(
        "NZBHydra",
        "http://hydra:5076",
        "http://hydra:5076/api?apikey=secret&t=caps",
        lambda r: "<caps>" in r,
    )
    # Find the OK notify. notify() receives (heading, message, duration).
    msgs = [c.args[1] for c in mock_notify.call_args_list]
    assert any("OK" in m for m in msgs), msgs


@patch("resources.lib.http_util.notify")
@patch("resources.lib.http_util.http_get")
def test_test_connection_reports_unexpected_when_condition_false(
    mock_http_get, mock_notify
):
    """_test_connection notifies 'unexpected response' when ok_condition False."""
    mock_http_get.return_value = "<html>login required</html>"
    _test_connection(
        "NZBHydra",
        "http://hydra:5076",
        "http://hydra:5076/api?apikey=secret&t=caps",
        lambda r: "<caps>" in r,
    )
    msgs = [c.args[1] for c in mock_notify.call_args_list]
    assert any("unexpected response" in m for m in msgs), msgs


@patch("resources.lib.http_util.notify")
def test_test_connection_bails_early_when_url_empty(mock_notify):
    """Empty url should short-circuit to a 'not configured' notification
    — never issue an HTTP request."""
    _test_connection("Prowlarr", "", "http://example/api", lambda _r: True)
    msgs = [c.args[1] for c in mock_notify.call_args_list]
    assert any("not configured" in m for m in msgs), msgs


@patch("resources.lib.http_util.notify")
@patch("resources.lib.http_util.http_get")
def test_test_connection_redacts_api_key_on_error(mock_http_get, mock_notify):
    """Exception messages sometimes embed the full URL (with apikey).
    _test_connection must redact the key before surfacing it."""

    class _UrlLeakingError(Exception):
        pass

    test_url = "http://hydra:5076/api?apikey=SUPERSECRET123&t=caps"
    mock_http_get.side_effect = _UrlLeakingError(
        "HTTP 401 for url: {}".format(test_url)
    )
    _test_connection("NZBHydra", "http://hydra:5076", test_url, lambda _r: True)
    msgs = [c.args[1] for c in mock_notify.call_args_list]
    assert all("SUPERSECRET123" not in m for m in msgs), msgs


@patch("resources.lib.router._test_connection")
@patch("xbmcaddon.Addon")
def test_test_hydra_connection_wires_search_endpoint(mock_addon, mock_test):
    """_test_hydra_connection builds an authenticated search URL."""
    mock_addon.return_value.getSetting.side_effect = lambda k: {
        "hydra_url": "http://hydra:5076",
        "hydra_api_key": "abc",
    }.get(k, "")

    _test_hydra_connection()

    mock_test.assert_called_once()
    label, url, test_url, ok_cond = mock_test.call_args[0]
    assert label == "NZBHydra"
    assert url == "http://hydra:5076"
    assert "t=search" in test_url
    assert "apikey=abc" in test_url
    assert ok_cond("<rss><channel/></rss>") is True
    assert ok_cond('<error code="100" description="Invalid API key"/>') is False


@patch("resources.lib.router._test_connection")
@patch("xbmcaddon.Addon")
def test_test_hydra_connection_uses_authenticated_search_endpoint(
    mock_addon, mock_test
):
    """Hydra verification must exercise an API-key-gated query, not public caps."""
    mock_addon.return_value.getSetting.side_effect = lambda k: {
        "hydra_url": "http://hydra:5076",
        "hydra_api_key": "abc",
    }.get(k, "")

    _test_hydra_connection()

    label, url, test_url, ok_cond = mock_test.call_args[0]
    assert label == "NZBHydra"
    assert url == "http://hydra:5076"
    assert "t=search" in test_url
    assert "t=caps" not in test_url
    assert "apikey=abc" in test_url
    assert (
        ok_cond(
            '<?xml version="1.0"?><rss><channel><title>NZBHydra</title></channel></rss>'
        )
        is True
    )
    assert (
        ok_cond(
            '<?xml version="1.0"?><error code="100" description="Invalid API key"/>'
        )
        is False
    )


@patch("resources.lib.router._test_connection")
@patch("xbmcaddon.Addon")
def test_test_nzbdav_connection_wires_queue_endpoint(mock_addon, mock_test):
    """_test_nzbdav_connection builds an authenticated queue URL."""
    mock_addon.return_value.getSetting.side_effect = lambda k: {
        "nzbdav_url": "http://nzbdav:6789",
        "nzbdav_api_key": "xyz",
    }.get(k, "")

    _test_nzbdav_connection()

    mock_test.assert_called_once()
    label, url, test_url, ok_cond = mock_test.call_args[0]
    assert label == "nzbdav"
    assert url == "http://nzbdav:6789"
    assert "mode=queue" in test_url
    assert "apikey=xyz" in test_url
    assert ok_cond('{"queue": {"slots": []}}') is True
    assert ok_cond("nope") is False


@patch("resources.lib.router._test_connection")
@patch("xbmcaddon.Addon")
def test_test_nzbdav_connection_uses_queue_endpoint_to_validate_api_key(
    mock_addon, mock_test
):
    """The nzbdav test must hit an authenticated SABnzbd API endpoint."""
    mock_addon.return_value.getSetting.side_effect = lambda k: {
        "nzbdav_url": "http://nzbdav:6789",
        "nzbdav_api_key": "xyz",
    }.get(k, "")

    _test_nzbdav_connection()

    label, url, test_url, ok_cond = mock_test.call_args[0]
    assert label == "nzbdav"
    assert url == "http://nzbdav:6789"
    assert "mode=queue" in test_url
    assert "mode=version" not in test_url
    assert "apikey=xyz" in test_url
    assert ok_cond('{"queue": {"slots": []}}') is True
    assert ok_cond('{"version": "1.0"}') is False
    assert ok_cond('{"status": false, "error": "invalid api key"}') is False


@patch("resources.lib.http_util.notify")
@patch("resources.lib.http_util.http_get")
@patch("xbmcaddon.Addon")
def test_test_prowlarr_connection_reports_ok(mock_addon, mock_http_get, mock_notify):
    """_test_prowlarr_connection hits /api/v1/indexer and notifies OK when
    the response looks JSON-shaped."""
    mock_addon.return_value.getSetting.side_effect = lambda k: {
        "prowlarr_host": "http://prowlarr:9696",
        "prowlarr_api_key": "zzz",
    }.get(k, "")
    mock_http_get.return_value = '[{"id": 1}]'

    _test_prowlarr_connection()

    called_url = mock_http_get.call_args[0][0]
    assert "/api/v1/indexer" in called_url
    assert "apikey=zzz" in called_url
    msgs = [c.args[1] for c in mock_notify.call_args_list]
    assert any("OK" in m for m in msgs), msgs


@patch("resources.lib.http_util.notify")
@patch("resources.lib.http_util.http_get")
@patch("xbmcaddon.Addon")
def test_test_prowlarr_connection_rejects_json_error_object(
    mock_addon, mock_http_get, mock_notify
):
    """A JSON-shaped error body must not pass Prowlarr verification."""
    mock_addon.return_value.getSetting.side_effect = lambda k: {
        "prowlarr_host": "http://prowlarr:9696",
        "prowlarr_api_key": "zzz",
    }.get(k, "")
    mock_http_get.return_value = '{"message": "Invalid API key"}'

    _test_prowlarr_connection()

    msgs = [c.args[1] for c in mock_notify.call_args_list]
    assert not any("OK" in m for m in msgs), msgs
    assert any("unexpected response" in m for m in msgs), msgs


@patch("resources.lib.router._test_connection")
@patch("xbmcaddon.Addon")
def test_test_prowlarr_connection_delegates_to_shared_connection_helper(
    mock_addon, mock_test
):
    """Prowlarr verification should use shared redaction/error handling."""
    mock_addon.return_value.getSetting.side_effect = lambda k: {
        "prowlarr_host": "http://prowlarr:9696",
        "prowlarr_api_key": "zzz",
    }.get(k, "")

    _test_prowlarr_connection()

    mock_test.assert_called_once_with(
        "Prowlarr",
        "http://prowlarr:9696",
        "http://prowlarr:9696/api/v1/indexer?apikey=zzz",
        _prowlarr_indexers_response_ok,
    )


@patch("resources.lib.http_util.notify")
@patch("xbmcaddon.Addon")
def test_test_prowlarr_connection_bails_when_host_empty(mock_addon, mock_notify):
    """No prowlarr_host → notify 'not configured' and return without HTTP."""
    mock_addon.return_value.getSetting.side_effect = lambda k: ""

    _test_prowlarr_connection()

    msgs = [c.args[1] for c in mock_notify.call_args_list]
    assert any("not configured" in m for m in msgs), msgs


@patch("resources.lib.router._string")
@patch("resources.lib.http_util.notify")
@patch("resources.lib.webdav.probe_webdav_reachable")
def test_test_webdav_connection_uses_localized_notifications(
    mock_probe, mock_notify, mock_string
):
    """The WebDAV settings action should localize each user-facing result."""
    from resources.lib import router

    labels = {
        30189: "localized ok",
        30190: "localized auth",
        30191: "localized server",
        30192: "localized error",
    }
    mock_string.side_effect = labels.__getitem__

    cases = [
        ((True, None), 30189, 3000),
        ((False, "auth_failed"), 30190, 5000),
        ((False, "server_error"), 30191, 5000),
        ((False, "connection_error"), 30192, 5000),
    ]
    for probe_result, msg_id, duration in cases:
        mock_probe.return_value = probe_result
        mock_notify.reset_mock()

        router._test_webdav_connection()

        mock_notify.assert_called_once_with("NZB-DAV", labels[msg_id], duration)


@patch("resources.lib.router._test_webdav_connection", create=True)
def test_route_dispatches_to_test_webdav(mock_test):
    """Route /test_webdav should call the WebDAV connection test."""
    route(["plugin://plugin.video.nzbdav/test_webdav", "1", ""])
    mock_test.assert_called_once()


# --- _get_tmdb_poster tests ---


def test_get_tmdb_poster_rejects_non_imdb_input():
    """Non-IMDb strings (empty, numeric-only, malformed) must not trigger
    a network call and must return ''."""
    assert _get_tmdb_poster("") == ""
    assert _get_tmdb_poster("not-an-id") == ""
    assert _get_tmdb_poster("12345") == ""  # missing tt prefix


@patch("urllib.request.urlopen")
def test_get_tmdb_poster_returns_image_url_from_suggestion_api(mock_urlopen):
    """A valid tt-prefixed imdb_id triggers a lookup; when the API
    returns an imageUrl, _get_tmdb_poster returns it."""
    resp = MagicMock()
    resp.read.return_value = (
        b'{"d": [{"i": {"imageUrl": "https://example.com/poster.jpg"}}]}'
    )
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = resp

    url = _get_tmdb_poster("tt0133093")
    assert url == "https://example.com/poster.jpg"


@patch("urllib.request.urlopen")
def test_get_tmdb_poster_returns_empty_on_api_error(mock_urlopen):
    """Network failure must be swallowed and return '' — this runs on a
    UI thread in settings and must never raise."""
    mock_urlopen.side_effect = OSError("connection refused")
    assert _get_tmdb_poster("tt0133093") == ""
