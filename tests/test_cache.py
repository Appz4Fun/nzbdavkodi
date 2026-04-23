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

        # Create three cache files with identical content and distinct mtimes
        file_paths = []
        payload = {"timestamp": 0, "results": ["x" * 200]}
        for i in range(3):
            path = os.path.join(tmpdir, "test_{}.json".format(i))
            with open(path, "w") as f:
                json.dump(payload, f)
            file_paths.append(path)
            # Stagger mtimes so eviction order is deterministic
            os.utime(path, (1000 + i, 1000 + i))

        # Confirm all three files exist
        assert len([f for f in os.listdir(tmpdir) if f.endswith(".json")]) == 3

        # Set the limit to fit exactly one file so the two oldest get evicted.
        single_size = os.path.getsize(file_paths[0])
        limit = single_size + 1

        with patch.object(cache_module, "MAX_CACHE_SIZE_BYTES", limit):
            _evict_oldest()

        remaining = [f for f in os.listdir(tmpdir) if f.endswith(".json")]
        # At least the two oldest files should have been removed, leaving only newest
        assert len(remaining) < 3
        # The newest file (test_2.json, highest mtime) should still be present
        assert "test_2.json" in remaining


@patch("resources.lib.cache._get_cache_dir")
@patch("resources.lib.cache.xbmcaddon")
def test_get_cached_falls_back_to_300s_when_ttl_setting_unparseable(
    mock_addon_mod, mock_cache_dir
):
    """When cache_ttl is a non-numeric string (user typo, corrupt
    settings file), get_cached must fall back to the 300 s default
    rather than raising ValueError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_cache_dir.return_value = tmpdir
        addon = MagicMock()
        # ``int("")`` raises ValueError in the production path. The
        # test covers the except-branch fallback.
        addon.getSetting.return_value = "absolute nonsense"
        mock_addon_mod.Addon.return_value = addon

        # Write a fresh cache entry by hand so we can observe whether
        # the fallback TTL (300 s) treats it as live.
        fresh = {
            "timestamp": time.time() - 60,  # 1 min old
            "results": [{"title": "Fresh"}],
        }
        os.makedirs(tmpdir, exist_ok=True)
        key_path = os.path.join(tmpdir, _cache_key("movie", "Fresh") + ".json")
        with open(key_path, "w") as f:
            json.dump(fresh, f)

        cached = get_cached("movie", "Fresh")
        assert (
            cached is not None
        ), "Fallback TTL of 300 s must still accept 1-min-old entry"
        assert cached[0]["title"] == "Fresh"


@patch("resources.lib.cache._get_cache_dir")
def test_clear_cache_swallows_per_file_oserror(mock_cache_dir):
    """clear_cache() must not raise if one of the files can't be
    deleted (locked by Kodi, gone mid-loop, permissions). Other files
    should still be processed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_cache_dir.return_value = tmpdir
        # Seed two cache files.
        for name in ("a.json", "b.json"):
            with open(os.path.join(tmpdir, name), "w") as f:
                f.write("{}")

        # Force the first os.remove call to raise; the second must
        # still land so b.json is deleted even though a.json "failed".
        real_remove = os.remove
        calls = {"n": 0}

        def _sometimes_fail(path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("locked")
            real_remove(path)

        with patch.object(cache_module.os, "remove", side_effect=_sometimes_fail):
            clear_cache()  # must not raise

        remaining = sorted(os.listdir(tmpdir))
        # Exactly one of the two files survives the partial failure.
        assert len(remaining) == 1
