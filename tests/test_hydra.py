# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from resources.lib.hydra import _calculate_age, parse_results, search_hydra


def _load_fixture(name):
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", name)
    with open(fixture_path, "r") as f:
        return f.read()


def test_parse_results_movie():
    xml_text = _load_fixture("hydra_movie_response.xml")
    results = parse_results(xml_text)
    assert len(results) == 2
    assert (
        results[0]["title"]
        == "The.Matrix.1999.2160p.UHD.BluRay.REMUX.HDR.HEVC.DTS-HD.MA.7.1-GROUP"
    )
    assert results[0]["link"] == "http://hydra:5076/getnzb/abc123?apikey=testkey"
    assert results[0]["size"] == "45000000000"
    assert results[0]["indexer"] == "NZBgeek"
    assert "pubdate" in results[0]


def test_parse_results_tv():
    xml_text = _load_fixture("hydra_tv_response.xml")
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


def test_source_url_hostname_extracts_host():
    from resources.lib.hydra import _source_url_hostname

    assert _source_url_hostname("https://indexer.example.com/path?q=1") == (
        "indexer.example.com"
    )


@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_movie(mock_http, mock_settings):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_http.return_value = _load_fixture("hydra_movie_response.xml")

    results, error = search_hydra("movie", "The Matrix", year="1999", imdb="tt0133093")
    assert error is None
    assert len(results) == 2

    call_url = mock_http.call_args[0][0]
    assert "t=movie" in call_url
    assert "imdbid=tt0133093" in call_url
    assert "apikey=testkey" in call_url


@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_tv(mock_http, mock_settings):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_http.return_value = _load_fixture("hydra_tv_response.xml")

    results, error = search_hydra("episode", "Breaking Bad", season="5", episode="14")
    assert error is None
    assert len(results) == 1

    call_url = mock_http.call_args[0][0]
    assert "t=tvsearch" in call_url
    assert "season=5" in call_url
    assert "ep=14" in call_url


@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_connection_error(mock_http, mock_settings):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_http.side_effect = RuntimeError("Connection refused")

    results, error = search_hydra("movie", "The Matrix")
    assert not results
    assert error == "NZBHydra unavailable: Connection refused"


@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_invalid_xml_reports_bad_response(mock_http, mock_settings):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_http.return_value = "<html>NZBHydra is starting"

    results, error = search_hydra("movie", "The Matrix")
    assert not results
    assert error.startswith("NZBHydra returned an invalid response:")


# --- New tests ---


def test_parse_results_missing_title():
    """Items without a <title> element should return an empty string for title."""
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
        <channel>
            <item>
                <link>http://hydra:5076/getnzb/no_title?apikey=testkey</link>
                <pubDate>Mon, 01 Apr 2026 12:00:00 +0000</pubDate>
                <newznab:attr name="size" value="1000000000"/>
                <newznab:attr name="indexer" value="TestIndexer"/>
            </item>
        </channel>
    </rss>"""
    results = parse_results(xml_text)
    assert len(results) == 1, "Item with no title should still be parsed"
    assert results[0]["title"] == "", "Missing title should be empty string"
    assert results[0]["link"] == "http://hydra:5076/getnzb/no_title?apikey=testkey"


def _enclosure_xml(url, length, extra_attrs=""):
    """Build a minimal Newznab RSS item with an enclosure but no <link>."""
    enc_line = '<enclosure url="{}" length="{}" type="application/x-nzb"/>'.format(
        url, length
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"'
        ' xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        "<channel><item>"
        "<title>Movie.Without.Link.2024.1080p-GRP</title>"
        "<pubDate>Mon, 01 Apr 2026 12:00:00 +0000</pubDate>"
        + enc_line
        + extra_attrs
        + "</item></channel></rss>"
    )


def test_parse_results_missing_link():
    """Items without a <link> element should fall back to enclosure URL."""
    enc_url = "http://hydra:5076/getnzb/enclosure_url?apikey=testkey"
    extra = (
        '<newznab:attr name="size" value="5000000000"/>'
        '<newznab:attr name="indexer" value="TestIndexer"/>'
    )
    xml_text = _enclosure_xml(enc_url, "5000000000", extra)
    results = parse_results(xml_text)
    assert len(results) == 1, "Item with no <link> should still be parsed"
    assert (
        results[0]["link"] == enc_url
    ), "Should fall back to enclosure URL when <link> is absent"


def test_parse_results_html_entities_in_title():
    """HTML entities (e.g. &amp;) in titles should be decoded by the XML parser."""
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
        <channel>
            <item>
                <title>Tom &amp; Jerry 2021 1080p BluRay x264-GRP</title>
                <link>http://hydra:5076/getnzb/entities?apikey=testkey</link>
                <pubDate>Mon, 01 Apr 2026 12:00:00 +0000</pubDate>
                <newznab:attr name="size" value="3000000000"/>
                <newznab:attr name="indexer" value="TestIndexer"/>
            </item>
        </channel>
    </rss>"""
    results = parse_results(xml_text)
    assert len(results) == 1
    assert "&" in results[0]["title"], "XML parser should decode &amp; to &"
    assert (
        "Tom & Jerry" in results[0]["title"]
    ), "Title should contain decoded ampersand"


@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_movie_no_imdb_falls_back_to_title(mock_http, mock_settings):
    """When no IMDb ID is provided, search_hydra should use title query."""
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_http.return_value = _load_fixture("hydra_movie_response.xml")

    results, error = search_hydra("movie", "The Matrix", year="1999")
    assert error is None
    assert len(results) == 2, "Should still return results from fixture"

    call_url = mock_http.call_args[0][0]
    assert "t=movie" in call_url, "Search type should be movie"
    assert (
        "q=The+Matrix" in call_url or "q=The%20Matrix" in call_url
    ), "Without imdbid, should fall back to title query"
    assert "imdbid" not in call_url, "imdbid should not be in URL when not provided"


def test_calculate_age_today():
    """A pubdate from today should return 'today'."""
    now = datetime.now(timezone.utc)
    pubdate_str = now.strftime("%a, %d %b %Y %H:%M:%S +0000")
    result = _calculate_age(pubdate_str)
    assert result == "today", "Same-day pubdate should be 'today'"


def test_calculate_age_one_day():
    """A pubdate from yesterday should return '1 day'."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    pubdate_str = yesterday.strftime("%a, %d %b %Y %H:%M:%S +0000")
    result = _calculate_age(pubdate_str)
    assert result == "1 day", "Yesterday's pubdate should be '1 day'"


def test_calculate_age_thirty_days():
    """A pubdate from 30 days ago should return a day-count string."""
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    pubdate_str = thirty_days_ago.strftime("%a, %d %b %Y %H:%M:%S +0000")
    result = _calculate_age(pubdate_str)
    # 30 days // 30 = 1 month
    assert result == "1 month", "30-day-old pubdate should be '1 month'"


def test_calculate_age_365_days():
    """A pubdate from 365 days ago should return a month-count string."""
    old = datetime.now(timezone.utc) - timedelta(days=365)
    pubdate_str = old.strftime("%a, %d %b %Y %H:%M:%S +0000")
    result = _calculate_age(pubdate_str)
    # 365 days // 30 = 12 months
    assert result == "12 months", "365-day-old pubdate should be '12 months'"


def test_calculate_age_invalid_date():
    """An invalid pubdate string should return an empty string, not raise."""
    result = _calculate_age("not-a-real-date")
    assert result == "", "Invalid pubdate should return empty string"


def test_parse_results_indexer_from_enclosure_fallback():
    """When newznab:attr indexer is absent, indexer should be empty string.

    The enclosure element doesn't carry indexer info; this tests that
    size is correctly extracted from the enclosure fallback path.
    """
    nzb_url = "http://hydra:5076/getnzb/abc?apikey=testkey"
    enc = '<enclosure url="{}" length="7000000000" type="application/x-nzb"/>'.format(
        nzb_url
    )
    xml_text = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"'
        ' xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
        "<channel><item>"
        "<title>Movie.2024.1080p.BluRay.x264-GRP</title>"
        "<link>{}</link>".format(nzb_url)
        + "<pubDate>Mon, 01 Apr 2026 12:00:00 +0000</pubDate>"
        + enc
        + "</item></channel></rss>"
    )
    results = parse_results(xml_text)
    assert len(results) == 1
    assert (
        results[0]["size"] == "7000000000"
    ), "Size should be extracted from enclosure when newznab:attr is absent"
    assert (
        results[0]["indexer"] == ""
    ), "Indexer should be empty when no newznab:attr indexer is present"


@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_returns_error_on_connection_failure(mock_http, mock_settings):
    """search_hydra should return ([], error_string) on connection failure."""
    from urllib.error import URLError

    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_http.side_effect = URLError("Connection refused")

    results, error = search_hydra("movie", "The Matrix")
    assert not results
    assert error == "NZBHydra unavailable: Connection refused"
