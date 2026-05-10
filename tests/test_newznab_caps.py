# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import patch

from resources.lib.newznab_caps import build_caps_url, fetch_caps, parse_caps

CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server appversion="1.0" />
  <searching>
    <search available="yes" supportedParams="q" />
    <tv-search available="yes" supportedParams="q,imdbid,tvdbid,season,ep" />
    <movie-search available="yes" supportedParams="q,imdbid" />
    <audio-search available="no" supportedParams="q" />
  </searching>
  <categories>
    <category id="2000" name="Movies">
      <subcat id="2040" name="HD" />
    </category>
    <category id="5000" name="TV" />
  </categories>
</caps>
"""


def test_build_caps_url_appends_api_and_redacts_nothing():
    url = build_caps_url("https://api.nzbgeek.info", "secret")

    assert url.startswith("https://api.nzbgeek.info/api?")
    assert "t=caps" in url
    assert "apikey=secret" in url
    assert "o=xml" in url


def test_parse_caps_reads_search_types_params_and_categories():
    caps = parse_caps(CAPS_XML)

    assert caps["search_types"] == ["search", "tvsearch", "movie"]
    assert caps["supported_params"]["search"] == ["q"]
    assert caps["supported_params"]["tvsearch"] == [
        "q",
        "imdbid",
        "tvdbid",
        "season",
        "ep",
    ]
    assert caps["supported_params"]["movie"] == ["q", "imdbid"]
    assert {"id": 2000, "name": "Movies"} in caps["categories"]
    assert {"id": 2040, "name": "HD"} in caps["categories"]


def test_parse_caps_invalid_xml_returns_empty_caps_and_error():
    caps = parse_caps("<html>bad")

    assert caps == {"search_types": [], "supported_params": {}, "categories": []}


@patch("resources.lib.newznab_caps._http_get")
def test_fetch_caps_uses_caps_url(mock_http):
    mock_http.return_value = CAPS_XML

    caps, error = fetch_caps("https://api.nzbgeek.info", "secret")

    assert error is None
    assert "movie" in caps["search_types"]
    assert "t=caps" in mock_http.call_args[0][0]
