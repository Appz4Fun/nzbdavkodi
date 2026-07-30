# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the TMDb Helper playback-identity handoff."""

import json
from unittest.mock import MagicMock

from resources.lib import tmdbhelper_scrobble

EPISODE_PARAMS = {
    "type": "episode",
    "show_tmdb_id": "94997",
    "episode_tmdb_id": "5234722",
    "imdb": "tt11198330",
    "tvdb": "371572",
    "season": "2",
    "episode": "2",
}

MOVIE_PARAMS = {"type": "movie", "tmdb_id": "603", "imdb": "tt0133093"}


def test_episode_tmdb_id_is_an_integer():
    """Trakt rejects string tmdb ids with HTTP 422, so send real integers."""
    info = tmdbhelper_scrobble.build_player_info(EPISODE_PARAMS)

    assert info["tmdb_id"] == 94997
    assert isinstance(info["tmdb_id"], int)
    assert info["tmdb_type"] == "episode"
    assert info["season"] == 2
    assert info["episode"] == 2


def test_episode_identity_uses_show_not_episode_id():
    """The scrobbler expects the show id plus season/episode numbers."""
    info = tmdbhelper_scrobble.build_player_info(EPISODE_PARAMS)

    assert info["tmdb_id"] == 94997
    assert info["imdb_id"] == "tt11198330"
    assert info["tvdb_id"] == "371572"


def test_episode_falls_back_to_alternate_param_names():
    """Season/episode arrive under ep_* names on some playback routes."""
    info = tmdbhelper_scrobble.build_player_info(
        {"type": "episode", "tmdb_id": "94997", "ep_season": "3", "ep_episode": "7"}
    )

    assert (info["season"], info["episode"]) == (3, 7)


def test_movie_tmdb_id_is_an_integer():
    info = tmdbhelper_scrobble.build_player_info(MOVIE_PARAMS)

    assert info == {
        "tmdb_type": "movie",
        "tmdb_id": 603,
        "imdb_id": "tt0133093",
    }


def test_unidentifiable_items_yield_nothing():
    """No tmdb id, or an episode without numbers, must not be published."""
    assert tmdbhelper_scrobble.build_player_info({}) == {}
    assert tmdbhelper_scrobble.build_player_info({"type": "movie"}) == {}
    assert (
        tmdbhelper_scrobble.build_player_info(
            {"type": "episode", "show_tmdb_id": "94997", "season": "2"}
        )
        == {}
    )


def test_publish_writes_the_prefixed_property():
    """TMDb Helper only reads keys prefixed with ``TMDbHelper.``."""
    window = MagicMock()

    published = tmdbhelper_scrobble.publish_player_info(EPISODE_PARAMS, window=window)

    key, payload = window.setProperty.call_args.args
    assert key == "TMDbHelper.PlayerInfoString"
    assert json.loads(payload) == published
    # Serialized, not repr()'d — the consumer parses this as JSON.
    assert '"tmdb_id": 94997' in payload


def test_publish_clears_property_when_unidentifiable():
    """A stale identity would scrobble the previously played item."""
    window = MagicMock()

    assert tmdbhelper_scrobble.publish_player_info({}, window=window) == {}

    window.clearProperty.assert_called_once_with("TMDbHelper.PlayerInfoString")
    window.setProperty.assert_not_called()


def test_publish_never_raises_into_playback():
    """A metadata handoff must not be able to break playback."""
    window = MagicMock()
    window.setProperty.side_effect = RuntimeError("boom")

    assert tmdbhelper_scrobble.publish_player_info(EPISODE_PARAMS, window=window) == {}
