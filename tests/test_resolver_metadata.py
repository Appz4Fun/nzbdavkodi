# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from resources.lib.resolver import (
    _apply_playback_identity,
    _tmdb_helper_metadata,
)

HOD_S02E01 = {
    "type": "episode",
    "title": "House of the Dragon",
    "year": "2022",
    "season": "2",
    "episode": "1",
    "imdb": "tt11198330",
    "tmdb_id": "94997",
    "show_tmdb_id": "94997",
    "episode_tmdb_id": "10396624",
}


@patch("resources.lib.resolver.xbmc")
def test_hod_episode_metadata_lookup_uses_show_tmdb_id(mock_xbmc):
    mock_xbmc.executeJSONRPC.return_value = json.dumps({"result": {"files": []}})

    assert _tmdb_helper_metadata(HOD_S02E01) == {}

    request = json.loads(mock_xbmc.executeJSONRPC.call_args[0][0])
    directory = request["params"]["directory"]
    query = parse_qs(urlsplit(directory).query)
    assert query["tmdb_type"] == ["tv"]
    assert query["tmdb_id"] == ["94997"]
    assert query["season"] == ["2"]
    assert query["episode"] == ["1"]
    assert "10396624" not in directory


@patch("resources.lib.resolver_metadata._tmdb_helper_metadata", return_value={})
def test_hod_episode_fallback_preserves_complete_listitem_identity(_metadata):
    listitem = MagicMock()

    _apply_playback_identity(listitem, HOD_S02E01)

    listitem.setInfo.assert_called_once_with(
        "video",
        {
            "title": "House of the Dragon",
            "tvshowtitle": "House of the Dragon",
            "year": "2022",
            "season": "2",
            "episode": "1",
            "mediatype": "episode",
        },
    )
    listitem.setUniqueIDs.assert_called_once_with(
        {
            "tvshow.tmdb": "94997",
            "tmdb": "10396624",
            "tvshow.imdb": "tt11198330",
        },
        "tmdb",
    )


@patch("resources.lib.resolver_metadata._tmdb_helper_metadata")
def test_hod_episode_identity_overrides_incomplete_rich_metadata(metadata):
    metadata.return_value = {
        "title": "A Son for a Son",
        "showtitle": "House of the Dragon",
        "season": 2,
        "episode": 1,
        "type": "episode",
        "uniqueid": {"tmdb": "10396624"},
    }
    listitem = MagicMock()

    _apply_playback_identity(listitem, HOD_S02E01)

    info = listitem.setInfo.call_args[0][1]
    assert info["title"] == "A Son for a Son"
    assert info["tvshowtitle"] == "House of the Dragon"
    assert info["season"] == 2
    assert info["episode"] == 1
    assert info["mediatype"] == "episode"
    unique_ids = listitem.setUniqueIDs.call_args[0][0]
    assert unique_ids["tvshow.tmdb"] == "94997"
    assert unique_ids["tmdb"] == "10396624"
