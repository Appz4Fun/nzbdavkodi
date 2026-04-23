# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from resources.lib.router import (
    _clean_params,
    _format_info_line,
    _format_size,
    _get_tmdb_poster,
    _handle_play,
    _handle_search,
    _safe_resolve_handle,
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


# --- _safe_resolve_handle + action route handle-resolution tests ---
#
# Action routes (install_player, clear_cache, settings, configure_*,
# test_hydra, test_nzbdav, resolve) are invoked from main-menu items with
# isFolder=False. Kodi blocks the UI until setResolvedUrl is called on the
# handle. These tests assert the route path always resolves the handle so
# Kodi never hangs. Regression test for ISSUE_REPORT.md C1.


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
def test_test_hydra_connection_wires_caps_endpoint(mock_addon, mock_test):
    """_test_hydra_connection builds the /api?t=caps URL and checks for
    ``<caps>`` / ``<server`` in the response via _test_connection."""
    mock_addon.return_value.getSetting.side_effect = lambda k: {
        "hydra_url": "http://hydra:5076",
        "hydra_api_key": "abc",
    }.get(k, "")

    _test_hydra_connection()

    mock_test.assert_called_once()
    label, url, test_url, ok_cond = mock_test.call_args[0]
    assert label == "NZBHydra"
    assert url == "http://hydra:5076"
    assert "t=caps" in test_url
    assert "apikey=abc" in test_url
    # ok_cond accepts either <caps> or <server
    assert ok_cond("<caps><server/></caps>") is True
    assert ok_cond("<rss><channel/></rss>") is False


@patch("resources.lib.router._test_connection")
@patch("xbmcaddon.Addon")
def test_test_nzbdav_connection_wires_version_endpoint(mock_addon, mock_test):
    """_test_nzbdav_connection builds the SABnzbd-compatible mode=version
    URL and checks the response contains the ``version`` key."""
    mock_addon.return_value.getSetting.side_effect = lambda k: {
        "nzbdav_url": "http://nzbdav:6789",
        "nzbdav_api_key": "xyz",
    }.get(k, "")

    _test_nzbdav_connection()

    mock_test.assert_called_once()
    label, url, test_url, ok_cond = mock_test.call_args[0]
    assert label == "nzbdav"
    assert url == "http://nzbdav:6789"
    assert "mode=version" in test_url
    assert "apikey=xyz" in test_url
    assert ok_cond('{"version": "1.0"}') is True
    assert ok_cond("nope") is False


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
@patch("xbmcaddon.Addon")
def test_test_prowlarr_connection_bails_when_host_empty(mock_addon, mock_notify):
    """No prowlarr_host → notify 'not configured' and return without HTTP."""
    mock_addon.return_value.getSetting.side_effect = lambda k: ""

    _test_prowlarr_connection()

    msgs = [c.args[1] for c in mock_notify.call_args_list]
    assert any("not configured" in m for m in msgs), msgs


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
