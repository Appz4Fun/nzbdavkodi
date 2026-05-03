# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

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

    assert get_configured_indexers() == []


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
        }
    ]


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

    assert results == []
    assert error.startswith("Direct indexer returned an invalid response:")
