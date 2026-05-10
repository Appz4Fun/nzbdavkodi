# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import time
from unittest.mock import MagicMock, patch


def _addon_with_settings(values):
    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: values.get(key, "")
    return addon


@patch("resources.lib.direct_indexers.xbmcaddon")
def test_get_configured_indexers_returns_empty_when_disabled(mock_xbmcaddon):
    from resources.lib.direct_indexers import get_configured_indexers

    mock_xbmcaddon.Addon.return_value = _addon_with_settings(
        {"direct_indexers_enabled": "false"}
    )

    assert not get_configured_indexers()


@patch("resources.lib.direct_indexers.xbmcaddon")
def test_get_configured_indexers_reads_enabled_preset(mock_xbmcaddon):
    from resources.lib.direct_indexers import get_configured_indexers

    mock_xbmcaddon.Addon.return_value = _addon_with_settings(
        {
            "direct_indexers_enabled": "true",
            "direct_indexer_nzbgeek_enabled": "true",
            "direct_indexer_nzbgeek_url": "https://api.nzbgeek.info/api",
            "direct_indexer_nzbgeek_api_key": "geek-key",
        }
    )

    assert get_configured_indexers() == [
        {
            "id": "nzbgeek",
            "label": "NZBGeek",
            "api_url": "https://api.nzbgeek.info/api",
            "api_key": "geek-key",
            "caps": {},
        }
    ]


@patch("resources.lib.direct_indexers.xbmcaddon")
def test_get_configured_indexers_reads_enabled_custom_slot(mock_xbmcaddon):
    from resources.lib.direct_indexers import get_configured_indexers

    mock_xbmcaddon.Addon.return_value = _addon_with_settings(
        {
            "direct_indexers_enabled": "true",
            "direct_indexer_custom1_enabled": "true",
            "direct_indexer_custom1_name": "My Indexer",
            "direct_indexer_custom1_url": "https://indexer.example",
            "direct_indexer_custom1_api_key": "custom-key",
        }
    )

    assert get_configured_indexers() == [
        {
            "id": "custom1",
            "label": "My Indexer",
            "api_url": "https://indexer.example",
            "api_key": "custom-key",
            "caps": {},
        }
    ]


@patch("resources.lib.direct_indexers.load_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
def test_get_configured_indexers_merges_json_store_and_legacy_static_settings(
    mock_xbmcaddon, mock_load_indexers
):
    from resources.lib.direct_indexers import get_configured_indexers

    mock_xbmcaddon.Addon.return_value = _addon_with_settings(
        {
            "direct_indexers_enabled": "true",
            "direct_indexer_nzbgeek_enabled": "true",
            "direct_indexer_nzbgeek_url": "https://api.nzbgeek.info/api",
            "direct_indexer_nzbgeek_api_key": "static-key",
            "direct_indexer_custom1_enabled": "true",
            "direct_indexer_custom1_name": "Static Custom",
            "direct_indexer_custom1_url": "https://static.example/newznab",
            "direct_indexer_custom1_api_key": "custom-key",
        }
    )
    mock_load_indexers.return_value = [
        {
            "id": "disabled",
            "name": "Disabled",
            "api_url": "https://disabled.example/api",
            "api_key": "disabled-key",
            "enabled": False,
            "caps": {"search_types": ["search"]},
        },
        {
            "id": "missing-url",
            "name": "Missing URL",
            "api_url": "",
            "api_key": "missing-url-key",
            "enabled": True,
            "caps": {"search_types": ["search"]},
        },
        {
            "id": "missing-key",
            "name": "Missing Key",
            "api_url": "https://missing-key.example/api",
            "api_key": "",
            "enabled": True,
            "caps": {"search_types": ["search"]},
        },
        {
            "id": "json-geek",
            "name": "JSON Geek",
            "api_url": "https://api.nzbgeek.info/api",
            "api_key": "json-key",
            "enabled": True,
            "caps": {"search_types": ["search"], "supported_params": {"search": ["q"]}},
        },
        {
            "id": "unnamed",
            "name": "",
            "api_url": "https://unnamed.example/api",
            "api_key": "unnamed-key",
            "enabled": True,
            "caps": {},
        },
    ]

    assert get_configured_indexers() == [
        {
            "id": "json-geek",
            "label": "JSON Geek",
            "api_url": "https://api.nzbgeek.info/api",
            "api_key": "json-key",
            "caps": {"search_types": ["search"], "supported_params": {"search": ["q"]}},
        },
        {
            "id": "unnamed",
            "label": "unnamed",
            "api_url": "https://unnamed.example/api",
            "api_key": "unnamed-key",
            "caps": {},
        },
        {
            "id": "nzbgeek",
            "label": "NZBGeek",
            "api_url": "https://api.nzbgeek.info/api",
            "api_key": "static-key",
            "caps": {},
        },
        {
            "id": "custom1",
            "label": "Static Custom",
            "api_url": "https://static.example/newznab",
            "api_key": "custom-key",
            "caps": {},
        },
    ]


@patch("resources.lib.direct_indexers.load_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
def test_get_configured_indexers_disabled_json_row_blocks_legacy_static_fallback(
    mock_xbmcaddon, mock_load_indexers
):
    from resources.lib.direct_indexers import get_configured_indexers

    mock_xbmcaddon.Addon.return_value = _addon_with_settings(
        {
            "direct_indexers_enabled": "true",
            "direct_indexer_nzbgeek_enabled": "true",
            "direct_indexer_nzbgeek_url": "https://api.nzbgeek.info/api",
            "direct_indexer_nzbgeek_api_key": "static-key",
        }
    )
    mock_load_indexers.return_value = [
        {
            "id": "nzbgeek",
            "name": "NZBGeek",
            "api_url": "https://api.nzbgeek.info/api",
            "api_key": "json-key",
            "enabled": False,
            "caps": {},
        }
    ]

    assert not get_configured_indexers()


def test_build_search_url_appends_api_when_missing():
    from resources.lib.direct_indexers import build_search_url

    url = build_search_url(
        "https://indexer.example",
        {"apikey": "secret", "t": "movie", "o": "xml"},
    )

    assert url.startswith("https://indexer.example/api?")
    assert "apikey=secret" in url
    assert "t=movie" in url


def test_build_search_url_preserves_existing_api_endpoint():
    from resources.lib.direct_indexers import build_search_url

    url = build_search_url(
        "https://api.nzbgeek.info/api",
        {"apikey": "secret", "t": "tvsearch", "o": "xml"},
    )

    assert url.startswith("https://api.nzbgeek.info/api?")


def test_build_search_url_preserves_nonstandard_newznab_endpoint_paths():
    from resources.lib.direct_indexers import build_search_url

    tabula = build_search_url(
        "https://tabula-rasa.pw/api/v1/",
        {"apikey": "secret", "t": "movie", "o": "xml"},
    )
    torbox = build_search_url(
        "https://torbox.app/newznab",
        {"apikey": "secret", "t": "search", "o": "xml"},
    )

    assert tabula.startswith("https://tabula-rasa.pw/api/v1?")
    assert torbox.startswith("https://torbox.app/newznab?")


def test_parse_results_uses_configured_label_when_xml_omits_indexer():
    from resources.lib.direct_indexers import parse_results

    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel>
<item>
<title>The.Matrix.1999.1080p.BluRay.x264-GRP</title>
<link>https://indexer.example/api?t=get&amp;id=abc&amp;apikey=secret</link>
<pubDate>Mon, 01 Apr 2026 12:00:00 +0000</pubDate>
<newznab:attr name="size" value="1234567890" />
</item>
</channel>
</rss>"""

    results, error = parse_results(xml_text, "My Indexer")

    assert error is None
    assert results[0]["title"] == "The.Matrix.1999.1080p.BluRay.x264-GRP"
    assert results[0]["indexer"] == "My Indexer"
    assert results[0]["size"] == "1234567890"


def test_parse_results_reports_invalid_xml():
    from resources.lib.direct_indexers import parse_results

    results, error = parse_results("<html>bad", "My Indexer")

    assert not results
    assert error.startswith("Direct indexer returned an invalid response:")


EMPTY_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel><newznab:response offset="0" total="0"/></channel>
</rss>"""

ONE_RESULT_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">
<channel>
<item>
<title>The.Matrix.1999.2160p.UHD.BluRay.x265-GRP</title>
<link>https://indexer.example/api?t=get&amp;id=abc&amp;apikey=secret</link>
<pubDate>Mon, 01 Apr 2026 12:00:00 +0000</pubDate>
<newznab:attr name="size" value="45000000000" />
</item>
</channel>
</rss>"""


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._http_get")
def test_search_direct_indexers_movie_uses_imdb_when_present(
    mock_http, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "nzbgeek",
            "label": "NZBGeek",
            "api_url": "https://api.nzbgeek.info/api",
            "api_key": "geek-key",
            "caps": {},
        }
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})
    mock_http.return_value = ONE_RESULT_RSS

    results, error = search_direct_indexers(
        "movie", "The Matrix", year="1999", imdb="tt0133093"
    )

    assert error is None
    assert len(results) == 1
    call_url = mock_http.call_args[0][0]
    assert "t=movie" in call_url
    assert "imdbid=0133093" in call_url
    assert "q=The+Matrix" not in call_url
    assert "apikey=geek-key" in call_url


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._http_get")
def test_search_direct_indexers_episode_uses_tvsearch_params(
    mock_http, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "nzbfinder",
            "label": "NZBFinder",
            "api_url": "https://nzbfinder.ws/api",
            "api_key": "finder-key",
            "caps": {},
        }
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})
    mock_http.return_value = ONE_RESULT_RSS

    results, error = search_direct_indexers(
        "episode", "Breaking Bad", season="5", episode="14"
    )

    assert error is None
    assert len(results) == 1
    call_url = mock_http.call_args[0][0]
    assert "t=tvsearch" in call_url
    assert "q=Breaking+Bad" in call_url or "q=Breaking%20Bad" in call_url
    assert "season=5" in call_url
    assert "ep=14" in call_url


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._http_get")
def test_search_direct_indexers_imdb_empty_retries_with_title(
    mock_http, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "nzbgeek",
            "label": "NZBGeek",
            "api_url": "https://api.nzbgeek.info/api",
            "api_key": "geek-key",
            "caps": {},
        }
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})
    mock_http.side_effect = [EMPTY_RSS, ONE_RESULT_RSS]

    results, error = search_direct_indexers("movie", "The Matrix", imdb="tt0133093")

    assert error is None
    assert len(results) == 1
    assert mock_http.call_count == 2
    fallback_url = mock_http.call_args_list[1][0][0]
    assert "q=The+Matrix" in fallback_url or "q=The%20Matrix" in fallback_url
    assert "imdbid" not in fallback_url


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._http_get")
def test_search_direct_indexers_uses_planner_for_host_fallback(
    mock_http, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "nzbgeek",
            "label": "NZBGeek",
            "api_url": "https://api.nzbgeek.info/api",
            "api_key": "geek-key",
            "caps": {
                "search_types": ["movie", "search"],
                "supported_params": {"movie": ["q"], "search": ["q"]},
            },
        }
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})
    mock_http.return_value = ONE_RESULT_RSS

    results, error = search_direct_indexers("movie", "The Matrix", imdb="tt0133093")

    assert error is None
    assert len(results) == 1
    call_url = mock_http.call_args[0][0]
    assert "t=search" in call_url
    assert "q=The+Matrix" in call_url or "q=The%20Matrix" in call_url
    assert "imdbid" not in call_url


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._http_get")
def test_search_direct_indexers_passes_supported_movie_year_to_planner(
    mock_http, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "custom",
            "label": "Custom",
            "api_url": "https://custom.example/api",
            "api_key": "custom-key",
            "caps": {
                "search_types": ["movie"],
                "supported_params": {"movie": ["q", "year"]},
            },
        }
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})
    mock_http.return_value = ONE_RESULT_RSS

    results, error = search_direct_indexers("movie", "The Odyssey", year="2026")

    assert error is None
    assert len(results) == 1
    call_url = mock_http.call_args[0][0]
    assert "t=movie" in call_url
    assert "q=The+Odyssey" in call_url or "q=The%20Odyssey" in call_url
    assert "year=2026" in call_url


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._http_get")
def test_search_direct_indexers_allows_large_result_limit_up_to_ten_thousand(
    mock_http, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "custom",
            "label": "Custom",
            "api_url": "https://custom.example/api",
            "api_key": "custom-key",
            "caps": {},
        }
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "2500"})
    mock_http.return_value = ONE_RESULT_RSS

    results, error = search_direct_indexers("movie", "Terminator 2")

    assert error is None
    assert len(results) == 1
    call_url = mock_http.call_args[0][0]
    assert "limit=2500" in call_url


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._http_get")
def test_search_direct_indexers_skips_when_caps_have_no_supported_query(
    mock_http, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "limited",
            "label": "Limited",
            "api_url": "https://limited.example/api",
            "api_key": "limited-key",
            "caps": {
                "search_types": ["movie"],
                "supported_params": {"movie": ["imdbid"]},
            },
        }
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})

    results, error = search_direct_indexers("movie", "The Matrix")

    assert not results
    assert error is None
    mock_http.assert_not_called()


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._http_get")
def test_search_direct_indexers_partial_failure_keeps_successful_results(
    mock_http, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "bad",
            "label": "Bad",
            "api_url": "https://bad.example/api",
            "api_key": "bad",
        },
        {
            "id": "good",
            "label": "Good",
            "api_url": "https://good.example/api",
            "api_key": "good",
        },
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})
    mock_http.side_effect = [RuntimeError("down"), ONE_RESULT_RSS]

    results, error = search_direct_indexers("movie", "The Matrix")

    assert error is None
    assert len(results) == 1
    assert results[0]["indexer"] == "Good"


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._search_one_indexer")
def test_search_direct_indexers_fans_out_concurrently(
    mock_search_one, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "one",
            "label": "One",
            "api_url": "https://one.example/api",
            "api_key": "one",
        },
        {
            "id": "two",
            "label": "Two",
            "api_url": "https://two.example/api",
            "api_key": "two",
        },
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})

    def slow_search(indexer, *_args, **_kwargs):
        time.sleep(0.2)
        return ([{"title": indexer["label"], "link": indexer["id"]}], None)

    mock_search_one.side_effect = slow_search

    started = time.monotonic()
    results, error = search_direct_indexers("movie", "The Matrix")
    elapsed = time.monotonic() - started

    assert error is None
    assert len(results) == 2
    assert elapsed < 0.32


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._search_one_indexer")
def test_search_direct_indexers_marks_incomplete_futures_timed_out(
    mock_search_one, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "slow",
            "label": "Slow",
            "api_url": "https://slow.example/api",
            "api_key": "slow",
        }
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})

    def slow_search(*_args, **_kwargs):
        time.sleep(0.2)
        return ([{"title": "late", "link": "late"}], None)

    mock_search_one.side_effect = slow_search

    with patch(
        "resources.lib.direct_indexers._DIRECT_FANOUT_TIMEOUT", 0.05, create=True
    ):
        started = time.monotonic()
        results, error = search_direct_indexers("movie", "The Matrix")
        elapsed = time.monotonic() - started

    assert not results
    assert "Direct indexer Slow unavailable:" in error
    assert "timed out" in error
    assert elapsed < 0.15


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers.xbmcaddon")
@patch("resources.lib.direct_indexers._http_get")
def test_search_direct_indexers_all_failures_return_error(
    mock_http, mock_xbmcaddon, mock_configured
):
    from resources.lib.direct_indexers import search_direct_indexers

    mock_configured.return_value = [
        {
            "id": "bad",
            "label": "Bad",
            "api_url": "https://bad.example/api",
            "api_key": "bad",
        },
    ]
    mock_xbmcaddon.Addon.return_value = _addon_with_settings({"max_results": "25"})
    mock_http.side_effect = RuntimeError("down")

    results, error = search_direct_indexers("movie", "The Matrix")

    assert not results
    assert error == "Direct indexer Bad unavailable: down"


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers._http_get")
def test_test_configured_indexers_marks_incomplete_futures_timed_out(
    mock_http, mock_configured
):
    from resources.lib.direct_indexers import test_configured_indexers

    mock_configured.return_value = [
        {
            "id": "slow",
            "label": "Slow",
            "api_url": "https://slow.example/api",
            "api_key": "slow",
        }
    ]

    def slow_caps(*_args, **_kwargs):
        time.sleep(0.2)
        return "<caps></caps>"

    mock_http.side_effect = slow_caps

    with patch(
        "resources.lib.direct_indexers._DIRECT_FANOUT_TIMEOUT", 0.05, create=True
    ):
        started = time.monotonic()
        ok_count, total_count, errors = test_configured_indexers()
        elapsed = time.monotonic() - started

    assert ok_count == 0
    assert total_count == 1
    assert len(errors) == 1
    assert "Direct indexer Slow unavailable:" in errors[0]
    assert "timed out" in errors[0]
    assert elapsed < 0.15


@patch("resources.lib.direct_indexers.get_configured_indexers")
@patch("resources.lib.direct_indexers._http_get")
def test_test_configured_indexers_counts_caps_success(mock_http, mock_configured):
    from resources.lib.direct_indexers import test_configured_indexers

    mock_configured.return_value = [
        {
            "id": "one",
            "label": "One",
            "api_url": "https://one.example/api",
            "api_key": "one",
        },
        {
            "id": "two",
            "label": "Two",
            "api_url": "https://two.example/api",
            "api_key": "two",
        },
    ]
    mock_http.side_effect = ["<caps></caps>", RuntimeError("down")]

    ok_count, total_count, errors = test_configured_indexers()

    assert ok_count == 1
    assert total_count == 2
    assert errors == ["Direct indexer Two unavailable: down"]
