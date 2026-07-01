# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import os
from unittest.mock import patch

from resources.lib.prowlarr import (
    _parse_results_checked,
    parse_results,
    search_prowlarr,
)


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


def _qp(url):
    """Decode a Prowlarr request URL into {param: last-value} (percent- and
    plus-decoded), so tests can assert on the real query/type values."""
    from urllib.parse import parse_qs, urlsplit

    return {k: v[-1] for k, v in parse_qs(urlsplit(url).query).items()}


# --- _build_prowlarr_query: Prowlarr's {token:value} query syntax (#313) ---
# Prowlarr's native /api/v1/search binds only Query/Type/IndexerIds and parses
# ids/season/episode out of the query TEXT as {tvdbid:..} tokens — it ignores
# imdbid=/tvdbid=/season=/ep= params entirely (verified against Prowlarr src).


def test_build_prowlarr_query_episode_prefers_tvdbid_token():
    from resources.lib.prowlarr import _build_prowlarr_query

    q = _build_prowlarr_query(
        "episode",
        "Breaking Bad",
        imdb="tt0903747",
        tvdb="81189",
        season="5",
        episode="14",
    )
    assert q == "Breaking Bad {tvdbid:81189}{season:5}{episode:14}"


def test_build_prowlarr_query_episode_imdb_token_when_no_tvdb():
    from resources.lib.prowlarr import _build_prowlarr_query

    q = _build_prowlarr_query(
        "episode", "Breaking Bad", imdb="tt0903747", season="5", episode="14"
    )
    # imdb id is reduced to its newznab digits (no "tt") inside the token.
    assert q == "Breaking Bad {imdbid:0903747}{season:5}{episode:14}"


def test_build_prowlarr_query_episode_no_id_keeps_season_episode():
    from resources.lib.prowlarr import _build_prowlarr_query

    q = _build_prowlarr_query("episode", "Breaking Bad", season="5", episode="14")
    assert q == "Breaking Bad {season:5}{episode:14}"


def test_build_prowlarr_query_movie_imdb_token():
    from resources.lib.prowlarr import _build_prowlarr_query

    q = _build_prowlarr_query("movie", "The Matrix", imdb="tt0133093")
    assert q == "The Matrix {imdbid:0133093}"


def test_build_prowlarr_query_movie_title_only():
    from resources.lib.prowlarr import _build_prowlarr_query

    assert _build_prowlarr_query("movie", "The Matrix") == "The Matrix"


def test_build_prowlarr_query_sanitizes_decorated_tvdb():
    from resources.lib.prowlarr import _build_prowlarr_query

    q = _build_prowlarr_query(
        "episode", "Show", tvdb="tvdb-81189", season="1", episode="2"
    )
    assert q == "Show {tvdbid:81189}{season:1}{episode:2}"


def test_build_prowlarr_query_strips_ampersand_from_title():
    """A '&' in the title must not reach Prowlarr's keyword query — indexers
    AND query terms against release names that spell it 'and' or omit it, so
    an '&' token matches nothing and the search returns nothing (#294)."""
    from resources.lib.prowlarr import _build_prowlarr_query

    assert (
        _build_prowlarr_query("movie", "Your Friends & Neighbors")
        == "Your Friends Neighbors"
    )


def test_build_prowlarr_query_strips_ampersand_but_keeps_tokens():
    """Title cleaning must leave the {token:value} ids intact."""
    from resources.lib.prowlarr import _build_prowlarr_query

    q = _build_prowlarr_query(
        "episode", "Will & Grace", tvdb="305288", season="1", episode="2"
    )
    assert q == "Will Grace {tvdbid:305288}{season:1}{episode:2}"


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
    assert not results


def test_parse_results_non_rss_root_returns_empty():
    xml_text = '<?xml version="1.0"?><response><error code="100"/></response>'
    results = parse_results(xml_text)
    assert not results


def test_parse_json_results_redacts_apikey_in_error_payload():
    """A Prowlarr JSON error body can echo the indexer apikey; it must be
    redacted before it lands in the returned error string (and the log).
    """
    from resources.lib.prowlarr import _parse_json_results

    body = '{"error": "bad request to http://idx?apikey=SECRET123"}'
    results, error = _parse_json_results(body)
    assert isinstance(results, list)
    assert not results
    assert "SECRET123" not in error
    assert "REDACTED" in error


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
    qp = _qp(call_url)
    assert qp["type"] == "movie"
    assert qp["query"] == "The Matrix {imdbid:0133093}"
    assert "apikey=testkey" in call_url
    assert "indexerIds=1" in call_url
    assert "indexerIds=2" in call_url
    assert mock_http.call_args.kwargs["timeout"] == 300


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
    qp = _qp(call_url)
    assert qp["type"] == "tvsearch"
    assert qp["query"] == "Breaking Bad {season:5}{episode:14}"
    assert "indexerIds=3" in call_url


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_tv_prefers_tvdbid(mock_http, mock_settings):
    """When a TVDB id is available, episode searches must key on tvdbid
    (not imdbid) — many indexers index TV by TheTVDB id (issue #318)."""
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["3"])
    mock_http.return_value = _load_fixture("prowlarr_tv_response.xml")

    results, error = search_prowlarr(
        "episode",
        "Breaking Bad",
        imdb="tt0903747",
        tvdb="81189",
        season="5",
        episode="14",
    )
    assert error is None

    qp = _qp(mock_http.call_args[0][0])
    assert qp["type"] == "tvsearch"
    # The id is embedded as a {tvdbid:..} token in the query (Prowlarr's only
    # id mechanism); tvdbid is preferred over imdbid, and the title is kept.
    assert qp["query"] == "Breaking Bad {tvdbid:81189}{season:5}{episode:14}"
    assert "imdbid" not in qp["query"]


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_tv_falls_back_to_imdbid_without_tvdb(mock_http, mock_settings):
    """Absent a TVDB id, the existing imdbid behavior is preserved."""
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["3"])
    mock_http.return_value = _load_fixture("prowlarr_tv_response.xml")

    results, error = search_prowlarr(
        "episode", "Breaking Bad", imdb="tt0903747", season="5", episode="14"
    )
    assert error is None

    qp = _qp(mock_http.call_args[0][0])
    assert qp["query"] == "Breaking Bad {imdbid:0903747}{season:5}{episode:14}"
    assert "tvdbid" not in qp["query"]


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_title_query_when_no_imdb(mock_http, mock_settings):
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["1"])
    mock_http.return_value = _load_fixture("prowlarr_movie_response.xml")

    results, error = search_prowlarr("movie", "The Matrix")
    assert error is None

    qp = _qp(mock_http.call_args[0][0])
    assert qp["query"] == "The Matrix"
    assert "imdbid" not in qp["query"]


@patch("xbmcaddon.Addon")
@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_invalid_max_results_uses_default(
    mock_http, mock_settings, mock_addon
):
    addon = mock_addon.return_value
    addon.getSetting.return_value = "many"
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["1"])
    mock_http.return_value = _load_fixture("prowlarr_movie_response.xml")

    results, error = search_prowlarr("movie", "The Matrix")

    assert error is None
    assert len(results) == 2
    call_url = mock_http.call_args[0][0]
    assert "limit=25" in call_url


@patch("xbmcaddon.Addon")
@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_getter_error_for_max_results_uses_default(
    mock_http, mock_settings, mock_addon
):
    addon = mock_addon.return_value
    addon.getSetting.return_value = "999"
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["1"])
    mock_http.return_value = _load_fixture("prowlarr_movie_response.xml")

    def failing_getter(key, default=""):
        if key == "max_results":
            raise RuntimeError("settings unavailable")
        return default

    results, error = search_prowlarr(
        "movie", "The Matrix", settings_getter=failing_getter
    )

    assert error is None
    assert len(results) == 2
    call_url = mock_http.call_args[0][0]
    assert "limit=25" in call_url
    assert "limit=999" not in call_url


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
    assert not results
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
    # Primary carried the imdbid token; the fallback drops it, leaving the title.
    assert (
        _qp(mock_http.call_args_list[0][0][0])["query"] == "The Matrix {imdbid:0133093}"
    )
    fallback_query = _qp(mock_http.call_args_list[1][0][0])["query"]
    assert fallback_query == "The Matrix"
    assert "imdbid" not in fallback_query
    assert [call.kwargs["timeout"] for call in mock_http.call_args_list] == [300, 300]


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_tvdb_fallback_to_title(mock_http, mock_settings):
    """When a tvdbid episode search returns nothing, retry by title and
    drop the tvdbid — mirrors the imdbid->title fallback (issue #318)."""
    empty_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
        <channel><newznab:response offset="0" total="0"/></channel>
    </rss>"""
    mock_settings.return_value = ("http://prowlarr:9696", "testkey", ["3"])
    mock_http.side_effect = [
        empty_xml,
        _load_fixture("prowlarr_tv_response.xml"),
    ]

    results, error = search_prowlarr(
        "episode", "Breaking Bad", tvdb="81189", season="5", episode="14"
    )
    assert error is None
    assert len(results) == 1
    assert mock_http.call_count == 2
    # Primary used the tvdbid token; the fallback drops the id but keeps the
    # title plus season/episode tokens.
    assert (
        _qp(mock_http.call_args_list[0][0][0])["query"]
        == "Breaking Bad {tvdbid:81189}{season:5}{episode:14}"
    )
    fallback_query = _qp(mock_http.call_args_list[1][0][0])["query"]
    assert fallback_query == "Breaking Bad {season:5}{episode:14}"
    assert "tvdbid" not in fallback_query
    assert "imdbid" not in fallback_query


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


# --- native JSON response parsing (issue #313) ---
#
# Prowlarr's /api/v1/search endpoint answers with a JSON array, not Newznab
# XML. The old XML-only parser crashed every search with
# "syntax error: line 1, column 0". These tests pin the JSON path.


def test_parse_results_json_movie():
    """A native Prowlarr JSON array parses into normalized result dicts."""
    json_text = _load_fixture("prowlarr_movie_response.json")
    results = parse_results(json_text)
    # 3 entries in the fixture, but the torrent release is dropped.
    assert len(results) == 2

    first = results[0]
    assert (
        first["title"]
        == "The.Matrix.1999.2160p.UHD.BluRay.REMUX.HDR.HEVC.DTS-HD.MA.7.1-GROUP"
    )
    assert first["link"].startswith("http://192.168.1.12:9696/1/download")
    assert first["size"] == "45000000000"
    assert first["indexer"] == "NZBgeek"
    assert first["age"] == "1 day"

    # publishDate (ISO-8601) is normalized to RFC-2822 so the stable-identity
    # and "Age" sort consumers (pubdate_to_epoch / filter._pubdate_sort_key)
    # can parse it — they reject ISO-8601 outright (issue #313 review).
    from datetime import datetime, timezone

    from resources.lib.http_util import pubdate_to_epoch

    assert first["pubdate"] != "2026-06-25T11:00:00Z"  # normalized, not raw ISO
    expected_epoch = int(
        datetime(2026, 6, 25, 11, 0, 0, tzinfo=timezone.utc).timestamp()
    )
    assert pubdate_to_epoch(first["pubdate"]) == expected_epoch

    # 400-day-old release -> 400 // 30 == 13 months.
    assert results[1]["size"] == "8200000000"
    assert results[1]["age"] == "13 months"
    # Distinct, correctly-ordered post identities -> "Age" sort works.
    assert pubdate_to_epoch(results[1]["pubdate"]) < pubdate_to_epoch(first["pubdate"])


def test_parse_results_json_filters_torrent_releases():
    """Torrent releases are unusable for an NZB pipeline and must be dropped."""
    json_text = _load_fixture("prowlarr_movie_response.json")
    results = parse_results(json_text)
    titles = [r["title"] for r in results]
    assert "The.Matrix.1999.1080p.BluRay.x264-TORRENT" not in titles
    assert all("magnet:" not in r["link"] for r in results)


def test_parse_results_json_protocol_filter_case_insensitive():
    """Prowlarr may serialize the protocol enum as 'Torrent' (capitalized)."""
    json_text = (
        '[{"title": "X", "downloadUrl": "http://x/1", "size": 100, '
        '"protocol": "Torrent"}, '
        '{"title": "Y", "downloadUrl": "http://x/2", "size": 200, '
        '"protocol": "Usenet"}]'
    )
    results = parse_results(json_text)
    assert len(results) == 1
    assert results[0]["title"] == "Y"


def test_parse_results_json_keeps_only_usenet_protocol():
    """nzbdav is usenet-only: keep strictly ``protocol == 'usenet'`` — drop
    torrents AND any release with a missing/unknown protocol."""
    json_text = (
        '[{"title": "U", "downloadUrl": "http://x/1", "protocol": "usenet"}, '
        '{"title": "T", "downloadUrl": "http://x/2", "protocol": "torrent"}, '
        '{"title": "M", "downloadUrl": "http://x/3"}]'  # no protocol -> dropped
    )
    results = parse_results(json_text)
    assert [r["title"] for r in results] == ["U"]


def test_parse_results_json_empty_array():
    """An empty JSON array is a valid 'no results' answer, not an error."""
    results, error = _parse_results_checked("[]")
    assert not results
    assert error is None


def test_parse_results_json_error_object_reports_message():
    """Prowlarr error bodies are JSON objects; surface their message."""
    results, error = _parse_results_checked('{"error": "Invalid API Key"}')
    assert not results
    assert error == "Prowlarr returned an invalid response: Invalid API Key"


def test_parse_results_json_non_array_object_without_message():
    results, error = _parse_results_checked('{"unexpected": true}')
    assert not results
    assert error == "Prowlarr returned an invalid response: expected a JSON array"


def test_parse_results_json_malformed_reports_bad_response():
    results, error = _parse_results_checked('[{"title": "truncated"')
    assert not results
    assert error.startswith("Prowlarr returned an invalid response:")


def test_parse_results_json_missing_fields_degrade_to_empty_strings():
    """Sparse releases must not raise; missing fields become empty strings."""
    results = parse_results('[{"title": "Only A Title", "protocol": "usenet"}]')
    assert len(results) == 1
    assert results[0] == {
        "title": "Only A Title",
        "link": "",
        "size": "",
        "indexer": "",
        "pubdate": "",
        "age": "",
    }


@patch("resources.lib.prowlarr._get_settings")
@patch("resources.lib.prowlarr._http_get")
def test_search_prowlarr_parses_json_response(mock_http, mock_settings):
    """End-to-end regression for issue #313: a JSON body yields results,
    not 'Prowlarr returned an invalid response'."""
    mock_settings.return_value = ("http://192.168.1.12:9696", "testkey", ["1"])
    mock_http.return_value = _load_fixture("prowlarr_movie_response.json")

    results, error = search_prowlarr("movie", "The Avengers", imdb="tt0848228")
    assert error is None
    assert len(results) == 2

    call_url = mock_http.call_args[0][0]
    assert "/api/v1/search" in call_url
    qp = _qp(call_url)
    assert qp["type"] == "movie"
    assert qp["query"] == "The Avengers {imdbid:0848228}"


def test_parse_results_json_size_as_digit_string():
    """Some indexers serialize size as a numeric string, not an int."""
    results = parse_results(
        '[{"title": "Z", "downloadUrl": "http://x/3", "size": "500", '
        '"protocol": "usenet"}]'
    )
    assert len(results) == 1
    assert results[0]["size"] == "500"


def test_parse_results_json_size_non_digit_string_is_dropped():
    """A non-numeric size string must degrade to empty, not crash/leak."""
    results = parse_results(
        '[{"title": "Z", "downloadUrl": "http://x/3", "size": "unknown", '
        '"protocol": "usenet"}]'
    )
    assert len(results) == 1
    assert results[0]["size"] == ""


def test_parse_results_json_error_object_message_key_fallback():
    """Prowlarr error bodies may use 'message' instead of 'error'."""
    results, error = _parse_results_checked('{"message": "Indexer offline"}')
    assert not results
    assert error == "Prowlarr returned an invalid response: Indexer offline"


def test_parse_results_json_skips_non_dict_array_entries():
    """A defensive array with stray scalars must skip them, not raise."""
    results = parse_results(
        '[null, 42, "junk", {"title": "X", "downloadUrl": "http://x/1", '
        '"protocol": "Usenet"}]'
    )
    assert len(results) == 1
    assert results[0]["title"] == "X"
