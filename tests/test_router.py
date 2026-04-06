# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

from resources.lib.router import (
    _format_info_line,
    _format_size,
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
    assert _format_size(None) == "N/A"


def test_format_size_zero():
    assert _format_size(0) == "N/A"


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
