# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the combined multi-provider search flow in router.py."""

import threading
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


@patch("resources.lib.tvdb_resolver.resolve_tvdb_id")
@patch("resources.lib.prowlarr.search_prowlarr", return_value=([PROWLARR_RESULT], None))
@patch("xbmcaddon.Addon")
def test_episode_passes_explicit_tvdb_to_providers(
    mock_addon, mock_prowlarr, mock_resolve
):
    """A TVDB id from the player token reaches each provider unchanged and
    does not trigger the (network) resolver (issue #318)."""
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="false", prowlarr_enabled="true"
    )

    _search_all_providers(
        "episode",
        "Silo",
        imdb="tt14688458",
        tvdb="305288",
        season="2",
        episode="5",
    )

    mock_resolve.assert_not_called()
    assert mock_prowlarr.call_args.kwargs["tvdb"] == "305288"


@patch("resources.lib.tvdb_resolver.resolve_tvdb_id", return_value="305288")
@patch("resources.lib.prowlarr.search_prowlarr", return_value=([PROWLARR_RESULT], None))
@patch("xbmcaddon.Addon")
def test_episode_resolves_tvdb_when_absent(mock_addon, mock_prowlarr, mock_resolve):
    """When no tvdb is supplied, resolve it once from tmdb_id/imdb and pass
    the result to every provider."""
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="false", prowlarr_enabled="true"
    )

    _search_all_providers(
        "episode",
        "Silo",
        imdb="tt14688458",
        tmdb_id="125988",
        season="2",
        episode="5",
    )

    assert mock_resolve.called
    assert mock_prowlarr.call_args.kwargs["tvdb"] == "305288"


@patch("resources.lib.tvdb_resolver.resolve_tvdb_id")
@patch("resources.lib.prowlarr.search_prowlarr", return_value=([PROWLARR_RESULT], None))
@patch("xbmcaddon.Addon")
def test_movie_search_does_not_resolve_tvdb(mock_addon, mock_prowlarr, mock_resolve):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="false", prowlarr_enabled="true"
    )

    _search_all_providers("movie", "The Matrix", imdb="tt0133093")

    mock_resolve.assert_not_called()
    assert mock_prowlarr.call_args.kwargs["tvdb"] == ""


@patch("resources.lib.tvdb_resolver.resolve_tvdb_id", return_value="")
@patch("resources.lib.prowlarr.search_prowlarr", return_value=([PROWLARR_RESULT], None))
@patch("xbmcaddon.Addon")
def test_episode_unresolved_tvdb_falls_through_empty(
    mock_addon, mock_prowlarr, mock_resolve
):
    """If resolution fails, providers get tvdb='' and keep working (imdbid)."""
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="false", prowlarr_enabled="true"
    )

    _search_all_providers(
        "episode", "Silo", imdb="tt14688458", tmdb_id="125988", season="2", episode="5"
    )

    assert mock_prowlarr.call_args.kwargs["tvdb"] == ""


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


@patch("resources.lib.hydra.search_hydra")
@patch("resources.lib.prowlarr.search_prowlarr")
@patch("xbmcaddon.Addon")
def test_both_top_level_providers_run_concurrently(
    mock_addon, mock_prowlarr, mock_hydra
):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="true", prowlarr_enabled="true"
    )
    both_providers_started = threading.Barrier(2, timeout=2)
    prowlarr_crossed_barrier = []

    def hydra_search(*_args, **_kwargs):
        both_providers_started.wait()
        return [HYDRA_RESULT], None

    def prowlarr_search(*_args, **_kwargs):
        both_providers_started.wait()
        prowlarr_crossed_barrier.append(True)
        return [PROWLARR_RESULT], None

    mock_hydra.side_effect = hydra_search
    mock_prowlarr.side_effect = prowlarr_search

    results, error = _search_all_providers("movie", "The Matrix")

    assert error is None
    assert len(results) == 2
    assert prowlarr_crossed_barrier == [True]


@patch("resources.lib.hydra.search_hydra")
@patch("resources.lib.prowlarr.search_prowlarr")
@patch("xbmcaddon.Addon")
def test_concurrent_provider_search_uses_snapshot_settings_getter(
    mock_addon, mock_prowlarr, mock_hydra
):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="true", prowlarr_enabled="true"
    )
    main_thread = threading.current_thread()
    setting_read_threads = []

    def setting(key, default=""):
        setting_read_threads.append((key, threading.current_thread()))
        return {
            "nzbhydra_enabled": "true",
            "prowlarr_enabled": "true",
            "direct_indexers_enabled": "false",
            "hydra_url": "http://hydra:5076",
            "hydra_api_key": "hydra-key",
            "prowlarr_host": "http://prowlarr:9696",
            "prowlarr_api_key": "prowlarr-key",
            "prowlarr_indexer_ids": "1,2",
            "max_results": "25",
        }.get(key, default)

    def hydra_search(*_args, **kwargs):
        assert kwargs["settings_getter"]("hydra_url") == "http://hydra:5076"
        return [HYDRA_RESULT], None

    def prowlarr_search(*_args, **kwargs):
        assert kwargs["settings_getter"]("prowlarr_host") == "http://prowlarr:9696"
        return [PROWLARR_RESULT], None

    mock_hydra.side_effect = hydra_search
    mock_prowlarr.side_effect = prowlarr_search

    results, error = _search_all_providers(
        "movie", "The Matrix", settings_getter=setting
    )

    assert error is None
    assert len(results) == 2
    assert setting_read_threads
    assert all(thread is main_thread for _key, thread in setting_read_threads)


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


@patch("resources.lib.hydra.search_hydra", side_effect=RuntimeError("boom"))
@patch("xbmcaddon.Addon")
def test_single_provider_exception_returns_provider_error(mock_addon, mock_hydra):
    mock_addon.return_value = _mock_addon(
        nzbhydra_enabled="true", prowlarr_enabled="false"
    )

    results, error = _search_all_providers("movie", "The Matrix")

    assert not results
    assert error == "NZBHydra2 search failed: boom"


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


@patch("resources.lib.hydra.search_hydra", return_value=([HYDRA_RESULT], None))
@patch("resources.lib.prowlarr.search_prowlarr", side_effect=RuntimeError("boom"))
@patch("xbmcaddon.Addon")
def test_provider_exception_preserves_other_provider_results(
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
