# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
from unittest.mock import patch

from resources.lib.tvdb_resolver import (
    _cache_path,
    _get_tmdb_api_key,
    resolve_tvdb_id,
)


def _key_getter(key, default=""):
    return {"tmdb_api_key": "KEY"}.get(key, default)


def _no_key_getter(key, default=""):
    return default


# --- resolve_tvdb_id ---


def test_resolve_from_tmdb_id_hits_external_ids():
    calls = []

    def fake_http_get(url, timeout=15):
        calls.append(url)
        return json.dumps({"id": 1396, "tvdb_id": 81189})

    tvdb = resolve_tvdb_id(
        tmdb_id="1396",
        settings_getter=_key_getter,
        http_get=fake_http_get,
        cache={},
    )

    assert tvdb == "81189"
    assert len(calls) == 1
    assert "/3/tv/1396/external_ids" in calls[0]
    assert "api_key=KEY" in calls[0]


def test_resolve_from_imdb_finds_series_then_external_ids():
    def fake_http_get(url, timeout=15):
        if "/find/" in url:
            assert "external_source=imdb_id" in url
            return json.dumps({"tv_results": [{"id": 1396}], "movie_results": []})
        assert "/3/tv/1396/external_ids" in url
        return json.dumps({"tvdb_id": 81189})

    tvdb = resolve_tvdb_id(
        imdb="tt0903747",
        settings_getter=_key_getter,
        http_get=fake_http_get,
        cache={},
    )

    assert tvdb == "81189"


def test_resolve_prefers_tmdb_id_over_imdb():
    """When both ids are present, the tmdb_id path is used (one call, no find)."""
    calls = []

    def fake_http_get(url, timeout=15):
        calls.append(url)
        return json.dumps({"tvdb_id": 81189})

    tvdb = resolve_tvdb_id(
        tmdb_id="1396",
        imdb="tt0903747",
        settings_getter=_key_getter,
        http_get=fake_http_get,
        cache={},
    )

    assert tvdb == "81189"
    assert all("/find/" not in u for u in calls)


def test_resolve_without_api_key_returns_empty_and_no_network():
    calls = []

    def fake_http_get(url, timeout=15):
        calls.append(url)
        return "{}"

    tvdb = resolve_tvdb_id(
        tmdb_id="1396",
        settings_getter=_no_key_getter,
        http_get=fake_http_get,
        cache={},
    )

    assert tvdb == ""
    assert calls == []


def test_resolve_uses_cache_and_skips_network():
    calls = []

    def fake_http_get(url, timeout=15):
        calls.append(url)
        return "{}"

    cache = {"tmdb:1396": "81189"}
    tvdb = resolve_tvdb_id(
        tmdb_id="1396",
        settings_getter=_key_getter,
        http_get=fake_http_get,
        cache=cache,
    )

    assert tvdb == "81189"
    assert calls == []


def test_resolve_stores_result_in_cache():
    def fake_http_get(url, timeout=15):
        return json.dumps({"tvdb_id": 81189})

    cache = {}
    resolve_tvdb_id(
        tmdb_id="1396",
        settings_getter=_key_getter,
        http_get=fake_http_get,
        cache=cache,
    )

    assert cache.get("tmdb:1396") == "81189"


def test_resolve_network_error_returns_empty():
    def fake_http_get(url, timeout=15):
        raise OSError("boom")

    tvdb = resolve_tvdb_id(
        tmdb_id="1396",
        settings_getter=_key_getter,
        http_get=fake_http_get,
        cache={},
    )

    assert tvdb == ""


def test_resolve_missing_tvdb_in_response_returns_empty_and_not_cached():
    def fake_http_get(url, timeout=15):
        return json.dumps({"id": 1396, "tvdb_id": None})

    cache = {}
    tvdb = resolve_tvdb_id(
        tmdb_id="1396",
        settings_getter=_key_getter,
        http_get=fake_http_get,
        cache=cache,
    )

    assert tvdb == ""
    assert cache == {}  # negatives are not cached


def test_resolve_find_with_no_tv_results_returns_empty():
    def fake_http_get(url, timeout=15):
        return json.dumps({"tv_results": [], "movie_results": [{"id": 99}]})

    tvdb = resolve_tvdb_id(
        imdb="tt0133093",  # a movie imdb -> no tv_results
        settings_getter=_key_getter,
        http_get=fake_http_get,
        cache={},
    )

    assert tvdb == ""


def test_resolve_no_ids_returns_empty():
    tvdb = resolve_tvdb_id(
        settings_getter=_key_getter, http_get=lambda *a, **k: "{}", cache={}
    )
    assert tvdb == ""


def test_resolve_malformed_json_returns_empty():
    def fake_http_get(url, timeout=15):
        return "<html>not json"

    tvdb = resolve_tvdb_id(
        tmdb_id="1396",
        settings_getter=_key_getter,
        http_get=fake_http_get,
        cache={},
    )

    assert tvdb == ""


# --- _get_tmdb_api_key ---


def test_get_tmdb_api_key_prefers_own_setting():
    assert _get_tmdb_api_key(_key_getter) == "KEY"


# --- _cache_path (must be safe in the RunScript/script-play context) ---


def test_cache_path_avoids_xbmcaddon_and_uses_special_protocol():
    """resolve_tvdb_id can run in the RunScript path (script_play -> search),
    where the codebase avoids xbmcaddon.Addon (it can SIGSEGV CoreELEC).
    _cache_path must resolve the profile dir via a special:// path through
    xbmcvfs, never xbmcaddon.Addon (Codex P2)."""
    import os

    with patch("resources.lib.tvdb_resolver.xbmcaddon") as mock_addon, patch(
        "resources.lib.tvdb_resolver.xbmcvfs"
    ) as mock_vfs, patch.object(os, "makedirs"):
        mock_vfs.translatePath.return_value = "/x/addon_data/plugin.video.nzbdav"
        path = _cache_path()

    mock_addon.Addon.assert_not_called()
    mock_vfs.translatePath.assert_called_once_with(
        "special://profile/addon_data/plugin.video.nzbdav"
    )
    assert path.endswith("tvdb_ids.json")


def test_get_tmdb_api_key_falls_back_to_tmdbhelper():
    """When our setting is blank, borrow TMDBHelper's configured key."""
    with patch("resources.lib.tvdb_resolver.xbmcaddon") as mock_xbmcaddon:
        mock_xbmcaddon.Addon.return_value.getSetting.return_value = "HELPERKEY"
        key = _get_tmdb_api_key(_no_key_getter)
    assert key == "HELPERKEY"
    mock_xbmcaddon.Addon.assert_called_once_with("plugin.video.themoviedb.helper")
    # Pin the borrowed setting id so a silent rename can't pass vacuously.
    mock_xbmcaddon.Addon.return_value.getSetting.assert_called_once_with("tmdb_apikey")


def test_get_tmdb_api_key_empty_when_neither_available():
    with patch("resources.lib.tvdb_resolver.xbmcaddon") as mock_xbmcaddon:
        mock_xbmcaddon.Addon.side_effect = RuntimeError("not installed")
        key = _get_tmdb_api_key(_no_key_getter)
    assert key == ""
