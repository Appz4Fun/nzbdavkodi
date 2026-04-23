# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the combined multi-provider search flow in router.py."""

from unittest.mock import MagicMock, patch

from resources.lib.router import _search_all_providers


def _make_result(title, link, indexer="TestIndexer"):
    """
    Create a standardized provider search result dictionary.

    Parameters:
        title (str): Item title shown to the user.
        link (str): Unique download or detail URL for the item.
        indexer (str): Name of the provider/indexer; defaults to "TestIndexer".

    Returns:
        dict: A provider search item with keys:
            - "title": given title
            - "link": given link
            - "size": file size in bytes (fixed "1000000000")
            - "indexer": provider name
            - "pubdate": publication date (fixed "Mon, 01 Apr 2026 12:00:00 +0000")
            - "age": human-readable age (fixed "today")
    """
    return {
        "title": title,
        "link": link,
        "size": "1000000000",
        "indexer": indexer,
        "pubdate": "Mon, 01 Apr 2026 12:00:00 +0000",
        "age": "today",
    }


HYDRA_RESULT = _make_result(
    "Movie.2024.1080p.BluRay.x264-GRP",
    "http://hydra:5076/getnzb/abc?apikey=key",
    "NZBgeek",
)
PROWLARR_RESULT = _make_result(
    "Movie.2024.2160p.UHD.BluRay.HEVC-GRP",
    "http://prowlarr:9696/1/api?t=get&id=xyz&apikey=key",
    "DrunkenSlug",
)
DUPLICATE_RESULT = _make_result(
    "Movie.2024.1080p.BluRay.x264-GRP",
    "http://hydra:5076/getnzb/abc?apikey=key",  # same link as HYDRA_RESULT
    "AnotherIndexer",
)


def _mock_addon(nzbhydra_enabled="true", prowlarr_enabled="false"):
    """
    Create a MagicMock addon whose `getSetting` returns configured
    enabled/disabled values for NZBHydra and Prowlarr.

    Parameters:
        nzbhydra_enabled (str): Value returned for the "nzbhydra_enabled"
            setting (expected "true" or "false").
        prowlarr_enabled (str): Value returned for the "prowlarr_enabled"
            setting (expected "true" or "false").

    Returns:
        MagicMock: A mock addon with `getSetting(key)` returning the
            corresponding configured value for "nzbhydra_enabled" and
            "prowlarr_enabled", and an empty string for any other keys.
    """
    addon = MagicMock()
    addon.getSetting.side_effect = lambda k: {
        "nzbhydra_enabled": nzbhydra_enabled,
        "prowlarr_enabled": prowlarr_enabled,
    }.get(k, "")
    return addon


# --- Both providers enabled ---


@patch("resources.lib.hydra.search_hydra", return_value=([HYDRA_RESULT], None))
@patch("resources.lib.prowlarr.search_prowlarr", return_value=([PROWLARR_RESULT], None))
@patch("xbmcaddon.Addon")
def test_both_providers_returns_combined_results(mock_addon, mock_prowlarr, mock_hydra):
    """
    Verifies that when both NZBHydra and Prowlarr are enabled, the combined
    search returns results from both providers.

    Asserts that no error is returned, exactly two results are produced, and
    that one result contains the HYDRA link and the other contains the
    PROWLARR link.
    """
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="true", prowlarr_enabled="true"
    )

    results, error = _search_all_providers("movie", "The Matrix")

    assert error is None
    assert len(results) == 2
    links = [r["link"] for r in results]
    assert HYDRA_RESULT["link"] in links
    assert PROWLARR_RESULT["link"] in links


@patch("resources.lib.hydra.search_hydra", return_value=([HYDRA_RESULT], None))
@patch(
    "resources.lib.prowlarr.search_prowlarr",
    return_value=([DUPLICATE_RESULT], None),
)
@patch("xbmcaddon.Addon")
def test_both_providers_deduplicates_by_link(mock_addon, mock_prowlarr, mock_hydra):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="true", prowlarr_enabled="true"
    )

    results, error = _search_all_providers("movie", "The Matrix")

    assert error is None
    assert len(results) == 1, "Duplicate link must be dropped"
    assert results[0]["link"] == HYDRA_RESULT["link"]


# --- Only one provider enabled ---


@patch("resources.lib.hydra.search_hydra", return_value=([HYDRA_RESULT], None))
@patch("xbmcaddon.Addon")
def test_only_nzbhydra_enabled(mock_addon, mock_hydra):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="true", prowlarr_enabled="false"
    )

    results, error = _search_all_providers("movie", "The Matrix")

    assert error is None
    assert len(results) == 1
    assert results[0]["link"] == HYDRA_RESULT["link"]


@patch("resources.lib.prowlarr.search_prowlarr", return_value=([PROWLARR_RESULT], None))
@patch("xbmcaddon.Addon")
def test_only_prowlarr_enabled(mock_addon, mock_prowlarr):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="false", prowlarr_enabled="true"
    )

    results, error = _search_all_providers("movie", "The Matrix")

    assert error is None
    assert len(results) == 1
    assert results[0]["link"] == PROWLARR_RESULT["link"]


# --- Neither provider enabled ---


@patch("xbmcaddon.Addon")
def test_neither_provider_enabled_returns_error(mock_addon):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="false", prowlarr_enabled="false"
    )

    results, error = _search_all_providers("movie", "The Matrix")

    assert not results
    assert error is not None
    assert "No search providers enabled" in error


# --- Partial failure scenarios ---


@patch("resources.lib.hydra.search_hydra", return_value=([], "NZBHydra unavailable"))
@patch("resources.lib.prowlarr.search_prowlarr", return_value=([PROWLARR_RESULT], None))
@patch("xbmcaddon.Addon")
def test_hydra_fails_prowlarr_succeeds_returns_prowlarr_results(
    mock_addon, mock_prowlarr, mock_hydra
):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="true", prowlarr_enabled="true"
    )

    results, error = _search_all_providers("movie", "The Matrix")

    assert error is None, "Should not error when at least one provider succeeded"
    assert len(results) == 1
    assert results[0]["link"] == PROWLARR_RESULT["link"]


@patch("resources.lib.hydra.search_hydra", return_value=([HYDRA_RESULT], None))
@patch(
    "resources.lib.prowlarr.search_prowlarr", return_value=([], "Prowlarr unavailable")
)
@patch("xbmcaddon.Addon")
def test_prowlarr_fails_hydra_succeeds_returns_hydra_results(
    mock_addon, mock_prowlarr, mock_hydra
):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="true", prowlarr_enabled="true"
    )

    results, error = _search_all_providers("movie", "The Matrix")

    assert error is None, "Should not error when at least one provider succeeded"
    assert len(results) == 1
    assert results[0]["link"] == HYDRA_RESULT["link"]


@patch("resources.lib.hydra.search_hydra", return_value=([], "NZBHydra unavailable"))
@patch(
    "resources.lib.prowlarr.search_prowlarr", return_value=([], "Prowlarr unavailable")
)
@patch("xbmcaddon.Addon")
def test_all_providers_fail_returns_first_error(mock_addon, mock_prowlarr, mock_hydra):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="true", prowlarr_enabled="true"
    )

    results, error = _search_all_providers("movie", "The Matrix")

    assert not results
    assert error == "NZBHydra unavailable"
