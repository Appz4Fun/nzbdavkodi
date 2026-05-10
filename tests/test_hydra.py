# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import resources.lib.hydra as hydra
from resources.lib.hydra import (
    _calculate_age,
    parse_results,
    refresh_hydra_caps,
    search_hydra,
)


def _load_fixture(name):
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", name)
    with open(fixture_path, "r") as f:
        return f.read()


def _query_params(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


def test_search_hydra_reuses_module_level_addon_for_kodi_settings(monkeypatch):
    fake_addon = MagicMock()
    fake_addon.getSetting.side_effect = lambda key: {
        "hydra_api_key": "testkey",
        "max_results": "33",
    }.get(key, "")
    monkeypatch.setattr(hydra, "addon", fake_addon, raising=False)
    monkeypatch.setattr(hydra, "url", "http://hydra:5076", raising=False)

    with patch(
        "resources.lib.hydra.xbmcaddon.Addon",
        side_effect=RuntimeError("should reuse module-level addon"),
    ) as mock_addon_ctor, patch("resources.lib.hydra._http_get") as mock_http, patch(
        "resources.lib.hydra.load_provider_caps"
    ) as mock_load_provider_caps:
        mock_load_provider_caps.return_value = {
            "nzbhydra2": {
                "base_url": "http://hydra:5076",
                "checked_at": "2026-05-10T00:00:00Z",
                "caps": {"search_types": ["movie"], "supported_params": {}},
            }
        }
        mock_http.return_value = _load_fixture("hydra_movie_response.xml")

        results, error = hydra.search_hydra(
            "movie", "The Matrix", year="1999", imdb="tt0133093"
        )

    assert error is None
    assert len(results) == 2
    mock_addon_ctor.assert_not_called()
    assert "limit=33" in mock_http.call_args[0][0]
    fake_addon.getSetting.assert_any_call("hydra_api_key")
    fake_addon.getSetting.assert_any_call("max_results")


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
    params = _query_params(call_url)
    assert params["t"] == "movie"
    assert params["imdbid"] == "0133093"
    assert params["apikey"] == "testkey"
    assert mock_http.call_args.kwargs["timeout"] == 300


@patch("xbmcaddon.Addon", side_effect=RuntimeError("no addon context"))
@patch("resources.lib.hydra._http_get")
def test_search_hydra_uses_script_settings_getter_without_kodi_addon(
    mock_http, mock_addon
):
    mock_http.return_value = _load_fixture("hydra_movie_response.xml")

    def setting(key, default=""):
        return {
            "hydra_url": "http://hydra:5076",
            "hydra_api_key": "testkey",
            "max_results": "12",
        }.get(key, default)

    results, error = search_hydra(
        "movie",
        "The Odyssey",
        year="2026",
        imdb="tt33764258",
        settings_getter=setting,
    )

    assert error is None
    assert len(results) == 2
    mock_addon.assert_not_called()
    call_url = mock_http.call_args[0][0]
    assert "limit=12" in call_url
    assert "apikey=testkey" in call_url


@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_allows_large_result_limit_up_to_ten_thousand(
    mock_http, mock_settings
):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_http.return_value = _load_fixture("hydra_movie_response.xml")

    def setting(key, default=""):
        return {
            "hydra_url": "http://hydra:5076",
            "hydra_api_key": "testkey",
            "max_results": "2500",
        }.get(key, default)

    results, error = search_hydra(
        "movie",
        "Terminator 2: Judgment Day",
        year="1991",
        imdb="tt0103064",
        settings_getter=setting,
    )

    assert error is None
    assert len(results) == 2
    call_url = mock_http.call_args[0][0]
    assert "limit=2500" in call_url


@patch("resources.lib.hydra.load_provider_caps")
@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_uses_cached_provider_caps(
    mock_http, mock_settings, mock_load_provider_caps
):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_load_provider_caps.return_value = {
        "nzbhydra2": {
            "base_url": "http://hydra:5076",
            "checked_at": "2026-05-10T00:00:00Z",
            "caps": {
                "search_types": ["search"],
                "supported_params": {"search": ["q"]},
            },
        }
    }
    mock_http.return_value = _load_fixture("hydra_movie_response.xml")

    results, error = search_hydra("movie", "The Matrix", imdb="tt0133093")

    assert error is None
    assert len(results) == 2
    params = _query_params(mock_http.call_args[0][0])
    assert params["t"] == "search"
    assert params["q"] == "The Matrix"
    assert "imdbid" not in params


@patch("resources.lib.hydra.load_provider_caps")
@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_movie_title_includes_year_when_caps_support_it(
    mock_http, mock_settings, mock_load_provider_caps
):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_load_provider_caps.return_value = {
        "nzbhydra2": {
            "base_url": "http://hydra:5076",
            "checked_at": "2026-05-10T00:00:00Z",
            "caps": {
                "search_types": ["movie"],
                "supported_params": {"movie": ["q", "year"]},
            },
        }
    }
    mock_http.return_value = _load_fixture("hydra_movie_response.xml")

    results, error = search_hydra("movie", "The Odyssey", year="2026")

    assert error is None
    assert len(results) == 2
    params = _query_params(mock_http.call_args[0][0])
    assert params["t"] == "movie"
    assert params["q"] == "The Odyssey"
    assert params["year"] == "2026"


@patch("resources.lib.hydra.save_provider_caps")
@patch("resources.lib.hydra.fetch_caps")
@patch("resources.lib.hydra.load_provider_caps")
@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_refreshes_provider_caps_when_cache_missing(
    mock_http,
    mock_settings,
    mock_load_provider_caps,
    mock_fetch_caps,
    mock_save_provider_caps,
):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_load_provider_caps.return_value = {}
    mock_fetch_caps.return_value = (
        {
            "search_types": ["search"],
            "supported_params": {"search": ["q"]},
        },
        None,
    )
    mock_http.return_value = _load_fixture("hydra_movie_response.xml")

    results, error = search_hydra("movie", "The Matrix", imdb="tt0133093")

    assert error is None
    assert len(results) == 2
    mock_fetch_caps.assert_called_once_with("http://hydra:5076", "testkey")
    mock_save_provider_caps.assert_called_once()
    params = _query_params(mock_http.call_args[0][0])
    assert params["t"] == "search"
    assert params["q"] == "The Matrix"
    assert "imdbid" not in params


@patch("resources.lib.hydra.load_provider_caps")
@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_provider_caps_base_url_mismatch_uses_conservative_defaults(
    mock_http, mock_settings, mock_load_provider_caps
):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_load_provider_caps.return_value = {
        "nzbhydra2": {
            "base_url": "http://other-hydra:5076",
            "checked_at": "2026-05-10T00:00:00Z",
            "caps": {
                "search_types": ["search"],
                "supported_params": {"search": ["q"]},
            },
        }
    }
    mock_http.return_value = _load_fixture("hydra_tv_response.xml")

    results, error = search_hydra(
        "episode",
        "Breaking Bad",
        imdb="tt0903747",
        season="5",
        episode="14",
    )

    assert error is None
    assert len(results) == 1
    params = _query_params(mock_http.call_args[0][0])
    assert params["t"] == "tvsearch"
    assert params["imdbid"] == "0903747"
    assert params["season"] == "5"
    assert params["ep"] == "14"


@patch("resources.lib.hydra.load_provider_caps")
@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_provider_caps_mismatch_movie_fallback_keeps_movie_search_type(
    mock_http, mock_settings, mock_load_provider_caps
):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_load_provider_caps.return_value = {
        "nzbhydra2": {
            "base_url": "http://other-hydra:5076",
            "checked_at": "2026-05-10T00:00:00Z",
            "caps": {
                "search_types": ["search"],
                "supported_params": {"search": ["q"]},
            },
        }
    }
    mock_http.side_effect = [
        """<?xml version="1.0" encoding="UTF-8"?><rss><channel /></rss>""",
        _load_fixture("hydra_movie_response.xml"),
    ]

    results, error = search_hydra("movie", "The Matrix", imdb="tt0133093")

    assert error is None
    assert len(results) == 2
    primary = _query_params(mock_http.call_args_list[0][0][0])
    fallback = _query_params(mock_http.call_args_list[1][0][0])
    assert primary["t"] == "movie"
    assert primary["imdbid"] == "0133093"
    assert fallback["t"] == "movie"
    assert fallback["q"] == "The Matrix"
    assert "imdbid" not in fallback


@patch("resources.lib.hydra.load_provider_caps")
@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_skips_when_planner_has_no_supported_query(
    mock_http, mock_settings, mock_load_provider_caps
):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_load_provider_caps.return_value = {
        "nzbhydra2": {
            "base_url": "http://hydra:5076",
            "checked_at": "2026-05-10T00:00:00Z",
            "caps": {
                "search_types": ["movie"],
                "supported_params": {"movie": ["imdbid"]},
            },
        }
    }

    results, error = search_hydra("movie", "The Matrix")

    assert results == []
    assert error is None
    mock_http.assert_not_called()


@patch("resources.lib.hydra.load_provider_caps")
@patch("resources.lib.hydra._get_settings")
@patch("resources.lib.hydra._http_get")
def test_search_hydra_retries_planner_fallback_when_primary_has_no_results(
    mock_http, mock_settings, mock_load_provider_caps
):
    mock_settings.return_value = ("http://hydra:5076", "testkey")
    mock_load_provider_caps.return_value = {
        "nzbhydra2": {
            "base_url": "http://hydra:5076",
            "checked_at": "2026-05-10T00:00:00Z",
            "caps": {
                "search_types": ["search", "movie"],
                "supported_params": {"search": ["q"], "movie": ["imdbid"]},
            },
        }
    }
    mock_http.side_effect = [
        """<?xml version="1.0" encoding="UTF-8"?><rss><channel /></rss>""",
        _load_fixture("hydra_movie_response.xml"),
    ]

    results, error = search_hydra("movie", "The Matrix", imdb="tt0133093")

    assert error is None
    assert len(results) == 2
    primary = _query_params(mock_http.call_args_list[0][0][0])
    fallback = _query_params(mock_http.call_args_list[1][0][0])
    assert primary["t"] == "movie"
    assert primary["imdbid"] == "0133093"
    assert fallback["t"] == "search"
    assert fallback["q"] == "The Matrix"


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


@patch("resources.lib.hydra.save_provider_caps")
@patch("resources.lib.hydra.load_provider_caps")
@patch("resources.lib.hydra.fetch_caps")
def test_refresh_hydra_caps_fetches_and_saves_provider_caps(
    mock_fetch_caps, mock_load_provider_caps, mock_save_provider_caps
):
    caps = {
        "search_types": ["search", "movie"],
        "supported_params": {"search": ["q"], "movie": ["imdbid"]},
    }
    mock_fetch_caps.return_value = (caps, None)
    mock_load_provider_caps.return_value = {
        "direct": {
            "base_url": "https://api.example.test",
            "checked_at": "2026-05-09T00:00:00Z",
            "caps": {"search_types": ["search"]},
        }
    }

    result, error = refresh_hydra_caps("http://hydra:5076", "testkey")

    assert result == caps
    assert error is None
    mock_fetch_caps.assert_called_once_with("http://hydra:5076", "testkey")
    saved = mock_save_provider_caps.call_args[0][0]
    assert saved["direct"] == mock_load_provider_caps.return_value["direct"]
    assert saved["nzbhydra2"]["base_url"] == "http://hydra:5076"
    assert saved["nzbhydra2"]["caps"] == caps
    assert saved["nzbhydra2"]["checked_at"].endswith("Z")


@patch("resources.lib.hydra.save_provider_caps")
@patch("resources.lib.hydra.load_provider_caps")
@patch("resources.lib.hydra.fetch_caps")
def test_refresh_hydra_caps_fetch_error_does_not_save(
    mock_fetch_caps, mock_load_provider_caps, mock_save_provider_caps
):
    caps = {"search_types": [], "supported_params": {}, "categories": []}
    mock_fetch_caps.return_value = (caps, "Connection refused")

    result, error = refresh_hydra_caps("http://hydra:5076", "testkey")

    assert result == caps
    assert error == "Connection refused"
    mock_load_provider_caps.assert_not_called()
    mock_save_provider_caps.assert_not_called()


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


# --- _source_url_hostname + _resolve_indexer coverage ---


def test_source_url_hostname_returns_empty_on_empty_input():
    """Empty or None source URL → empty string, no parse attempt."""
    from resources.lib.hydra import _source_url_hostname

    assert _source_url_hostname("") == ""
    assert _source_url_hostname(None) == ""


def test_source_url_hostname_returns_input_when_not_url_shaped():
    """Hydra sometimes puts a plain indexer name in <source url="...">
    (no slash). Preserve it as the hostname fallback."""
    from resources.lib.hydra import _source_url_hostname

    assert _source_url_hostname("nzbfinder") == "nzbfinder"


def test_source_url_hostname_extracts_host_from_url():
    """A real URL yields its hostname, shorn of scheme / path."""
    from resources.lib.hydra import _source_url_hostname

    assert _source_url_hostname("https://nzb.example.com/path") == "nzb.example.com"


def test_resolve_indexer_prefers_newznab_attr_over_source_element():
    """When a Newznab <attr name="indexer"> was parsed, _resolve_indexer
    returns it directly — no <source> fallback is consulted."""
    import xml.etree.ElementTree as ET

    from resources.lib.hydra import _resolve_indexer

    item = ET.fromstring(
        '<item><source>OtherSource</source><source url="https://ignored/" /></item>'
    )
    assert _resolve_indexer(item, "PreferredIndexer") == "PreferredIndexer"


def test_resolve_indexer_falls_back_to_source_text():
    """No Newznab attr → use the <source>text</source> body as the
    display name."""
    import xml.etree.ElementTree as ET

    from resources.lib.hydra import _resolve_indexer

    item = ET.fromstring("<item><source>FromSourceText</source></item>")
    assert _resolve_indexer(item, "") == "FromSourceText"


def test_resolve_indexer_falls_back_to_source_url_hostname():
    """No Newznab attr, no source text → derive hostname from
    <source url="..."/>."""
    import xml.etree.ElementTree as ET

    from resources.lib.hydra import _resolve_indexer

    item = ET.fromstring('<item><source url="https://nzb.example.com/path" /></item>')
    assert _resolve_indexer(item, "") == "nzb.example.com"


def test_resolve_indexer_returns_empty_when_no_source_at_all():
    """No attr, no <source> element at all → empty string."""
    import xml.etree.ElementTree as ET

    from resources.lib.hydra import _resolve_indexer

    item = ET.fromstring("<item><title>No source info</title></item>")
    assert _resolve_indexer(item, "") == ""
