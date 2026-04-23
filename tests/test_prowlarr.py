# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import os
from unittest.mock import patch

from resources.lib.prowlarr import parse_results, search_prowlarr


def _load_fixture(name):
    """
    Load and return the text contents of a fixture file located in the
    module's "fixtures" directory.

    Parameters:
        name (str): Filename of the fixture within the "fixtures" directory
            (relative to this file).

    Returns:
        str: The fixture file's contents as a string.
    """
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", name)
    with open(fixture_path, "r") as f:
        return f.read()


# --- parse_results tests ---


def test_parse_results_movie():
    """
    Verifies that parse_results correctly parses a Prowlarr movie RSS fixture
    into expected result entries.

    Asserts the function returns two results and that the first result
    contains the expected title, a link containing "prowlarr", a size of
    "45000000000", an indexer of "NZBgeek", and a present `pubdate` field.
    """
    xml_text = _load_fixture("prowlarr_movie_response.xml")
    results = parse_results(xml_text)
    assert len(results) == 2
    assert (
        results[0]["title"]
        == "The.Matrix.1999.2160p.UHD.BluRay.REMUX.HDR.HEVC.DTS-HD.MA.7.1-GROUP"
    )
    assert "prowlarr" in results[0]["link"]
    assert results[0]["size"] == "45000000000"
    assert results[0]["indexer"] == "NZBgeek"
    assert "pubdate" in results[0]


def test_parse_results_tv():
    """
    Verify parse_results extracts expected fields from a Prowlarr TV RSS response.

    Asserts that the parser returns exactly one result and that the result's
    `title` and `size` match the expected values from the `prowlarr_tv_response.xml`
    fixture.
    """
    xml_text = _load_fixture("prowlarr_tv_response.xml")
    results = parse_results(xml_text)
    assert len(results) == 1
    assert (
        results[0]["title"]
        == "Breaking.Bad.S05E14.Ozymandias.1080p.BluRay.x265.DTS-HD.MA.5.1-NTb"
    )
    assert results[0]["size"] == "4200000000"


def test_parse_results_empty():
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
        <channel><newznab:response offset="0" total="0"/></channel>
    </rss>"""
    results = parse_results(xml_text)
    assert not results


def test_parse_results_invalid_xml_returns_empty():
    results = parse_results("<html>not xml")
    assert results == []


def test_parse_results_non_rss_root_returns_empty():
    xml_text = '<?xml version="1.0"?><response><error code="100"/></response>'
    results = parse_results(xml_text)
    assert results == []


# --- search_prowlarr URL-building tests ---


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_movie(mock_http, mock_settings):
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["1", "2"])
    mock_http.return_value = _load_fixture("prowlarr_movie_response.xml")

    results, error = search_prowlarr(
        "movie", "The Matrix", year="1999", imdb="tt0133093"
    )
    assert error is None
    assert len(results) == 2

    call_url = mock_http.call_args[0][0]
    assert "/api/v1/search" in call_url
    assert "t=movie" in call_url
    assert "imdbid=tt0133093" in call_url
    assert "apikey=testkey" in call_url
    assert "indexerIds=1" in call_url
    assert "indexerIds=2" in call_url


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_tv(mock_http, mock_settings):
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["3"])
    mock_http.return_value = _load_fixture("prowlarr_tv_response.xml")

    results, error = search_prowlarr(
        "episode", "Breaking Bad", season="5", episode="14"
    )
    assert error is None
    assert len(results) == 1

    call_url = mock_http.call_args[0][0]
    assert "t=tvsearch" in call_url
    assert "season=5" in call_url
    assert "ep=14" in call_url
    assert "indexerIds=3" in call_url


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_title_query_when_no_imdb(mock_http, mock_settings):
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["1"])
    mock_http.return_value = _load_fixture("prowlarr_movie_response.xml")

    results, error = search_prowlarr("movie", "The Matrix")
    assert error is None

    call_url = mock_http.call_args[0][0]
    assert "q=The+Matrix" in call_url or "q=The%20Matrix" in call_url
    assert "imdbid" not in call_url


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_connection_error(mock_http, mock_settings):
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["1"])
    mock_http.side_effect = Exception("Connection refused")

    results, error = search_prowlarr("movie", "The Matrix")
    assert not results
    assert error == "Prowlarr unavailable: Connection refused"


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_invalid_xml_reports_bad_response(mock_http, mock_settings):
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["1"])
    mock_http.return_value = "<html>Prowlarr is starting"

    results, error = search_prowlarr("movie", "The Matrix")
    assert not results
    assert error.startswith("Prowlarr returned an invalid response:")


def test_search_prowlarr_no_indexer_ids_returns_empty_without_error():
    """When no indexer IDs are configured, return ([], None) — not an error."""
    with patch("resources.lib.prowlarr._get_settings") as mock_settings:
        mock_settings.return_value = ("http://prowlarr:9696", "testkey", [])
        results, error = search_prowlarr("movie", "The Matrix")
    assert results == []
    assert error is None


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_imdb_fallback_to_title(mock_http, mock_settings):
    """When IMDB search returns no results, retry with title query."""
    empty_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
        <channel><newznab:response offset="0" total="0"/></channel>
    </rss>"""
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["1"])
    mock_http.side_effect = [
        empty_xml,
        _load_fixture("prowlarr_movie_response.xml"),
    ]

    results, error = search_prowlarr("movie", "The Matrix", imdb="tt0133093")
    assert error is None
    assert len(results) == 2
    assert mock_http.call_count == 2
    fallback_url = mock_http.call_args_list[1][0][0]
    assert "q=The+Matrix" in fallback_url or "q=The%20Matrix" in fallback_url
    assert "imdbid" not in fallback_url


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_url_error_returns_error(mock_http, mock_settings):
    from urllib.error import URLError

    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["1"])
    mock_http.side_effect = URLError("Connection refused")

    results, error = search_prowlarr("movie", "The Matrix")
    assert not results
    assert error == "Prowlarr unavailable: Connection refused"


# --- parse_results fallback-path coverage (source text / source url hostname) ---


def test_parse_results_falls_back_to_source_text_when_attr_missing():
    """Prowlarr sometimes omits the Newznab indexer attr and puts the
    indexer name in a ``<source>text</source>`` element. parse_results
    must pick that up."""
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel>
<item>
<title>Some.Release.2024.mkv</title>
<link>http://prowlarr/dl/1</link>
<pubDate>Mon, 01 Apr 2024 12:00:00 +0000</pubDate>
<source>IndexerFromText</source>
<newznab:attr name="size" value="4000000000" />
</item>
</channel>
</rss>"""
    results = parse_results(xml_text)
    assert len(results) == 1
    assert results[0]["indexer"] == "IndexerFromText"


def test_parse_results_falls_back_to_source_url_hostname():
    """No attr, no source text — just a ``<source url="..."/>`` element.
    parse_results must extract the hostname as the indexer label."""
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel>
<item>
<title>Some.Release.2024.mkv</title>
<link>http://prowlarr/dl/2</link>
<source url="https://hosted.example.org/api/rss" />
<newznab:attr name="size" value="4000000000" />
</item>
</channel>
</rss>"""
    results = parse_results(xml_text)
    assert len(results) == 1
    assert results[0]["indexer"] == "hosted.example.org"


def test_parse_results_enclosure_length_fills_in_when_attr_size_missing():
    """When ``<newznab:attr name="size">`` is missing, the <enclosure>
    ``length`` attribute provides the size — matches SABnzbd-compatible
    fallback behavior."""
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel>
<item>
<title>Movie.2024.mkv</title>
<link>http://prowlarr/dl/3</link>
<enclosure url="http://prowlarr/dl/3" length="987654321" type="application/x-nzb" />
</item>
</channel>
</rss>"""
    results = parse_results(xml_text)
    assert len(results) == 1
    assert results[0]["size"] == "987654321"
