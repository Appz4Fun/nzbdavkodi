# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch


def test_parse_script_args_decodes_tmdbhelper_run_script_arguments():
    from resources.lib.script_player import parse_script_args

    params = parse_script_args(
        [
            "type=movie",
            "title=The%20Odyssey",
            "year=2026",
            "imdb=tt33764258",
            "tmdb_id=1368337",
        ]
    )

    assert params == {
        "type": "movie",
        "title": "The Odyssey",
        "year": "2026",
        "imdb": "tt33764258",
        "tmdb_id": "1368337",
    }


def test_parse_script_args_preserves_commas_in_titles():
    from resources.lib.script_player import parse_script_args

    params = parse_script_args(
        [
            "type=movie",
            "title=Crouching Tiger",
            " Hidden Dragon",
            "year=2000",
        ]
    )

    assert params["title"] == "Crouching Tiger, Hidden Dragon"
    assert params["year"] == "2000"


def test_parse_script_args_unwraps_quoted_titles_split_on_commas():
    from resources.lib.script_player import parse_script_args

    params = parse_script_args(
        [
            "type=movie",
            'title="Crouching Tiger',
            ' Hidden Dragon"',
            "year=2000",
        ]
    )

    assert params["title"] == "Crouching Tiger, Hidden Dragon"
    assert params["year"] == "2000"


def test_parse_script_args_unwraps_quoted_titles():
    from resources.lib.script_player import parse_script_args

    params = parse_script_args(
        [
            "type=movie",
            'title="Wuthering Heights"',
            "year=2026",
        ]
    )

    assert params["title"] == "Wuthering Heights"
    assert params["year"] == "2026"


def test_parse_script_args_preserves_split_title_after_empty_key():
    from resources.lib.script_player import parse_script_args

    params = parse_script_args(
        [
            "type=movie",
            "title=",
            "Wuthering Heights",
            "year=2026",
        ]
    )

    assert params["title"] == "Wuthering Heights"
    assert params["year"] == "2026"


@patch("resources.lib.router._handle_script_play")
def test_run_tmdb_play_routes_to_script_play_handler(mock_handle_script_play):
    from resources.lib.script_player import run_tmdb_play

    run_tmdb_play(
        [
            "type=movie",
            "title=The%20Odyssey",
            "year=2026",
            "tmdb_id=1368337",
        ]
    )

    mock_handle_script_play.assert_called_once_with(
        {
            "type": "movie",
            "title": "The Odyssey",
            "year": "2026",
            "tmdb_id": "1368337",
        }
    )


@patch("resources.lib.router._handle_script_play")
def test_run_tmdb_play_routes_clean_split_quoted_title(mock_handle_script_play):
    from resources.lib.script_player import run_tmdb_play

    run_tmdb_play(
        [
            "type=movie",
            'title="Crouching Tiger',
            ' Hidden Dragon"',
            "year=2000",
        ]
    )

    mock_handle_script_play.assert_called_once_with(
        {
            "type": "movie",
            "title": "Crouching Tiger, Hidden Dragon",
            "year": "2000",
        }
    )


def test_run_tmdb_play_provides_addon_context_for_file_path_runs():
    from resources.lib.script_player import run_tmdb_play

    def addon_factory(*args, **kwargs):
        if args or kwargs:
            addon = MagicMock()
            addon.getAddonInfo.return_value = args[0]
            return addon
        raise RuntimeError("No valid addon id could be obtained")

    seen = []

    def handler(_params):
        import xbmcaddon

        seen.append(xbmcaddon.Addon().getAddonInfo("id"))

    with patch("xbmcaddon.Addon", side_effect=addon_factory):
        with patch("resources.lib.router._handle_script_play", side_effect=handler):
            run_tmdb_play(["type=movie", "title=The%20Odyssey"])

    assert seen == ["plugin.video.nzbdav"]
