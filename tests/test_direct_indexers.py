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
