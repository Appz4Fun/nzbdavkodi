# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

from resources.lib.cache import _cache_key, clear_cache, get_cached, set_cached


def test_cache_key_basic():
    key = _cache_key("movie", "The Matrix", year="1999")
    assert "movie" in key
    assert "Matrix" in key or "matrix" in key


def test_cache_key_special_chars():
    key = _cache_key("movie", "Movie: The Sequel!")
    assert all(c.isalnum() or c in "-_" for c in key)


def test_cache_key_length_limited():
    long_title = "A" * 500
    key = _cache_key("movie", long_title)
    assert len(key) <= 200


@patch("resources.lib.cache._get_cache_dir")
@patch("resources.lib.cache.xbmcaddon")
def test_set_and_get_cached(mock_addon_mod, mock_cache_dir):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_cache_dir.return_value = tmpdir
        addon = MagicMock()
        addon.getSetting.return_value = "300"
        mock_addon_mod.Addon.return_value = addon

        results = [{"title": "Test", "link": "http://test"}]
        set_cached("movie", "Test", results)
        cached = get_cached("movie", "Test")
        assert cached is not None
        assert len(cached) == 1
        assert cached[0]["title"] == "Test"


@patch("resources.lib.cache._get_cache_dir")
@patch("resources.lib.cache.xbmcaddon")
def test_cache_expired(mock_addon_mod, mock_cache_dir):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_cache_dir.return_value = tmpdir
        addon = MagicMock()
        addon.getSetting.return_value = "1"  # 1 second TTL
        mock_addon_mod.Addon.return_value = addon

        results = [{"title": "Test"}]
        set_cached("movie", "Test", results)
        time.sleep(1.1)
        cached = get_cached("movie", "Test")
        assert cached is None


@patch("resources.lib.cache._get_cache_dir")
@patch("resources.lib.cache.xbmcaddon")
def test_cache_disabled_when_ttl_zero(mock_addon_mod, mock_cache_dir):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_cache_dir.return_value = tmpdir
        addon = MagicMock()
        addon.getSetting.return_value = "0"
        mock_addon_mod.Addon.return_value = addon

        set_cached("movie", "Test", [{"title": "Test"}])
        cached = get_cached("movie", "Test")
        assert cached is None


@patch("resources.lib.cache._get_cache_dir")
def test_clear_cache(mock_cache_dir):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_cache_dir.return_value = tmpdir
        # Create some cache files
        for i in range(3):
            with open(os.path.join(tmpdir, "test_{}.json".format(i)), "w") as f:
                json.dump({"timestamp": time.time(), "results": []}, f)

        assert len(os.listdir(tmpdir)) == 3
        clear_cache()
        assert len(os.listdir(tmpdir)) == 0


@patch("resources.lib.cache._get_cache_dir")
@patch("resources.lib.cache.xbmcaddon")
def test_cache_miss_returns_none(mock_addon_mod, mock_cache_dir):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_cache_dir.return_value = tmpdir
        addon = MagicMock()
        addon.getSetting.return_value = "300"
        mock_addon_mod.Addon.return_value = addon

        cached = get_cached("movie", "Nonexistent")
        assert cached is None
