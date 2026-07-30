# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the TMDb Helper rich-metadata handoff."""

import json
from unittest.mock import MagicMock, patch

from resources.lib import tmdbhelper_metadata

EPISODE_PARAMS = {
    "type": "episode",
    "title": "House%20of%20the%20Dragon",
    "show_tmdb_id": "94997",
    "episode_tmdb_id": "5234722",
    "imdb": "tt11198330",
    "tvdb": "371572",
    "season": "2",
    "episode": "2",
}

TMDB_HELPER_RESPONSE = {
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "files": [
            {
                "title": "Rhaenyra the Cruel",
                "showtitle": "House of the Dragon",
                "plot": "The realm reels.",
                "year": 2024,
                "art": {"fanart": "http://x/fanart.jpg"},
                "thumbnail": "http://x/thumb.jpg",
                "cast": [{"name": "Emma D'Arcy", "role": "Rhaenyra"}],
                "uniqueid": {"tvdb": "10396629"},
                "season": 2,
                "episode": 2,
            }
        ]
    },
}


def test_fetch_metadata_returns_empty_without_identity():
    assert tmdbhelper_metadata.fetch_metadata({}) == {}
    assert tmdbhelper_metadata.fetch_metadata({"type": "movie"}) == {}


@patch("resources.lib.tmdbhelper_metadata.xbmc")
def test_fetch_metadata_queries_the_show_id_for_episodes(mock_xbmc):
    """The lookup must use the show's tmdb id, not the episode's."""
    mock_xbmc.executeJSONRPC.return_value = json.dumps(TMDB_HELPER_RESPONSE)

    metadata = tmdbhelper_metadata.fetch_metadata(EPISODE_PARAMS)

    request = json.loads(mock_xbmc.executeJSONRPC.call_args.args[0])
    directory = request["params"]["directory"]
    assert "tmdb_id=94997" in directory
    assert "tmdb_type=tv" in directory
    assert "season=2" in directory
    assert "episode=2" in directory
    assert metadata["title"] == "Rhaenyra the Cruel"


@patch("resources.lib.tmdbhelper_metadata.xbmc")
def test_fetch_metadata_swallows_lookup_failures(mock_xbmc):
    """A malformed or failed JSON-RPC response must not raise."""
    mock_xbmc.executeJSONRPC.return_value = "not json"

    assert tmdbhelper_metadata.fetch_metadata(EPISODE_PARAMS) == {}


@patch("resources.lib.tmdbhelper_metadata.fetch_metadata")
def test_apply_metadata_sets_info_art_and_cast(mock_fetch):
    mock_fetch.return_value = TMDB_HELPER_RESPONSE["result"]["files"][0]
    li = MagicMock()

    tmdbhelper_metadata.apply_metadata(li, EPISODE_PARAMS)

    info = li.setInfo.call_args.args[1]
    assert info["title"] == "Rhaenyra the Cruel"
    assert info["tvshowtitle"] == "House of the Dragon"
    assert info["mediatype"] == "episode"
    assert info["season"] == 2
    assert info["episode"] == 2

    art = li.setArt.call_args.args[0]
    assert art["fanart"] == "http://x/fanart.jpg"
    assert art["thumb"] == "http://x/thumb.jpg"

    li.setCast.assert_called_once_with(
        TMDB_HELPER_RESPONSE["result"]["files"][0]["cast"]
    )


@patch("resources.lib.tmdbhelper_metadata.fetch_metadata")
def test_apply_metadata_sets_show_level_unique_ids_for_episodes(mock_fetch):
    """Episode identity must be the show's ids, with the episode id separate."""
    mock_fetch.return_value = {}
    li = MagicMock()

    tmdbhelper_metadata.apply_metadata(li, EPISODE_PARAMS)

    unique_ids, default_key = li.setUniqueIDs.call_args.args
    assert unique_ids["tvshow.tmdb"] == "94997"
    assert unique_ids["tmdb"] == "5234722"
    assert unique_ids["tvshow.imdb"] == "tt11198330"


@patch("resources.lib.tmdbhelper_metadata.fetch_metadata")
def test_apply_metadata_falls_back_to_title_from_params_when_lookup_empty(mock_fetch):
    """If TMDb Helper has nothing, at least show the title we already know."""
    mock_fetch.return_value = {}
    li = MagicMock()

    tmdbhelper_metadata.apply_metadata(li, EPISODE_PARAMS)

    info = li.setInfo.call_args.args[1]
    assert info["title"] == "House of the Dragon"
    assert info["tvshowtitle"] == "House of the Dragon"
    assert info["season"] == "2"
    assert info["episode"] == "2"


def test_apply_metadata_never_raises_into_playback():
    """A ListItem that rejects setInfo must not break the play call."""
    li = MagicMock()
    li.setInfo.side_effect = RuntimeError("boom")

    tmdbhelper_metadata.apply_metadata(li, EPISODE_PARAMS)  # must not raise


def test_publish_writes_json_and_clears_when_empty():
    window = MagicMock()

    tmdbhelper_metadata.publish_params(EPISODE_PARAMS, window=window)
    key, payload = window.setProperty.call_args.args
    assert key == "nzbdav.metadata_params"
    assert json.loads(payload) == EPISODE_PARAMS

    tmdbhelper_metadata.publish_params({}, window=window)
    window.clearProperty.assert_called_once_with("nzbdav.metadata_params")


def test_publish_drops_internal_non_serializable_state():
    """Real resolve-stage params carry internal state on the same dict —
    e.g. ``_fallback_candidate_loader`` is a callable — that must not break
    publishing. Regression: this previously raised inside publish_params'
    own try/except, so the property was silently never written and no
    metadata ever appeared, with only a WARNING in the log to show for it.
    """
    window = MagicMock()
    params_with_callable = dict(EPISODE_PARAMS, _fallback_candidate_loader=lambda: None)

    tmdbhelper_metadata.publish_params(params_with_callable, window=window)

    key, payload = window.setProperty.call_args.args
    assert key == "nzbdav.metadata_params"
    written = json.loads(payload)
    assert "_fallback_candidate_loader" not in written
    assert written["show_tmdb_id"] == "94997"


@patch("resources.lib.tmdbhelper_metadata.apply_metadata")
def test_apply_from_published_params_reads_back_the_property(mock_apply):
    window = MagicMock()
    window.getProperty.return_value = json.dumps(EPISODE_PARAMS)
    li = MagicMock()

    tmdbhelper_metadata.apply_from_published_params(li, window=window)

    mock_apply.assert_called_once_with(li, EPISODE_PARAMS)


@patch("resources.lib.tmdbhelper_metadata.apply_metadata")
def test_apply_from_published_params_is_a_noop_when_nothing_published(mock_apply):
    """Empty property (nothing published for this play) must not call apply."""
    window = MagicMock()
    window.getProperty.return_value = ""
    li = MagicMock()

    tmdbhelper_metadata.apply_from_published_params(li, window=window)

    mock_apply.assert_not_called()


@patch("resources.lib.tmdbhelper_metadata.apply_metadata")
def test_apply_from_published_params_tolerates_malformed_json(mock_apply):
    """A mocked/garbage property value (e.g. in unrelated unit tests that
    stub xbmcgui.Window entirely) must not raise or apply anything."""
    window = MagicMock()
    window.getProperty.return_value = MagicMock()  # not a JSON string
    li = MagicMock()

    tmdbhelper_metadata.apply_from_published_params(li, window=window)

    mock_apply.assert_not_called()
