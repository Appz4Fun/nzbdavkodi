# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import resources.lib.cache as cache_module
from resources.lib.cache import (
    _cache_key,
    _evict_oldest,
    clear_cache,
    get_cached,
    set_cached,
)


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


@patch("resources.lib.cache._get_cache_dir")
@patch("resources.lib.cache.xbmcaddon")
def test_cache_read_handles_corrupted_json(mock_addon_mod, mock_cache_dir):
    """Reading a corrupted cache file should return None, not raise JSONDecodeError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_cache_dir.return_value = tmpdir
        addon = MagicMock()
        addon.getSetting.return_value = "300"
        mock_addon_mod.Addon.return_value = addon

        cache_path = os.path.join(tmpdir, "movie_Test.json")
        with open(cache_path, "w") as f:
            f.write("{not valid json]")

        cached = get_cached("movie", "Test")
        assert cached is None


@patch("resources.lib.cache._get_cache_dir")
def test_cache_evicts_oldest_when_over_limit(mock_cache_dir):
    """Cache should evict oldest files when total size exceeds limit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_cache_dir.return_value = tmpdir

        # Create three cache files with distinct mtimes
        file_paths = []
        for i in range(3):
            path = os.path.join(tmpdir, "test_{}.json".format(i))
            with open(path, "w") as f:
                # Write enough data so 3 files exceed a small limit
                json.dump({"timestamp": time.time(), "results": ["x" * 200]}, f)
            file_paths.append(path)
            # Stagger mtimes so eviction order is deterministic
            os.utime(path, (time.time() - (3 - i), time.time() - (3 - i)))

        # Confirm all three files exist
        assert len([f for f in os.listdir(tmpdir) if f.endswith(".json")]) == 3

        # Measure the size of one file so we can set the limit to just above it,
        # meaning 2 files would push over the limit and the oldest gets evicted.
        single_size = os.path.getsize(file_paths[0])
        limit = single_size + 1  # allow exactly 1 file before triggering eviction

        with patch.object(cache_module, "MAX_CACHE_SIZE_BYTES", limit):
            _evict_oldest()

        remaining = [f for f in os.listdir(tmpdir) if f.endswith(".json")]
        # At least the two oldest files should have been removed, leaving only newest
        assert len(remaining) < 3
        # The newest file (test_2.json, highest mtime) should still be present
        assert "test_2.json" in remaining
