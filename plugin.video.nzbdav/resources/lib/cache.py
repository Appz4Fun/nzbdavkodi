# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Simple JSON-based search result cache."""

import json
import os
import time

import xbmc
import xbmcaddon
import xbmcvfs

MAX_CACHE_SIZE_BYTES = 52428800  # 50MB


def _get_cache_dir():
    addon = xbmcaddon.Addon()
    profile = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
    cache_dir = os.path.join(profile, "cache")
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
    return cache_dir


def _cache_key(search_type, title, year="", imdb="", season="", episode=""):
    """Generate a filesystem-safe cache key."""
    parts = [search_type, title, year, imdb, season, episode]
    key = "_".join(str(p) for p in parts if p)
    # Sanitize for filesystem
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
    return safe[:200]  # Limit length


def get_cached(search_type, title, **kwargs):
    """Get cached results if fresh enough. Returns list or None."""
    addon = xbmcaddon.Addon()
    # Clamp to [0, 86400] so a typo (e.g. "99999999") can't leave a cache
    # entry valid forever. 24 h is the upper sensible bound for search-
    # result freshness on a home-scale indexer.
    try:
        cache_ttl = max(0, min(int(addon.getSetting("cache_ttl") or "300"), 86400))
    except (TypeError, ValueError):
        cache_ttl = 300
    if cache_ttl <= 0:
        return None

    key = _cache_key(search_type, title, **kwargs)
    path = os.path.join(_get_cache_dir(), key + ".json")

    if not os.path.exists(path):
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
        if time.time() - data.get("timestamp", 0) > cache_ttl:
            return None
        xbmc.log("NZB-DAV: Cache hit for '{}'".format(title), xbmc.LOGDEBUG)
        return data.get("results", [])
    except (json.JSONDecodeError, OSError):
        return None


def set_cached(search_type, title, results, **kwargs):
    """Cache search results."""
    addon = xbmcaddon.Addon()
    # Clamp to [0, 86400] so a typo (e.g. "99999999") can't leave a cache
    # entry valid forever. 24 h is the upper sensible bound for search-
    # result freshness on a home-scale indexer.
    try:
        cache_ttl = max(0, min(int(addon.getSetting("cache_ttl") or "300"), 86400))
    except (TypeError, ValueError):
        cache_ttl = 300
    if cache_ttl <= 0:
        return

    key = _cache_key(search_type, title, **kwargs)
    path = os.path.join(_get_cache_dir(), key + ".json")

    try:
        data = {"timestamp": time.time(), "results": results}
        # Atomic write: dump to a sibling temp file then os.replace onto the
        # final path. A concurrent get_cached() will see either the old file
        # or the new file, never a half-written JSON blob that would
        # JSONDecodeError.
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
        xbmc.log(
            "NZB-DAV: Cached {} results for '{}'".format(len(results), title),
            xbmc.LOGDEBUG,
        )
    except OSError:
        # Clean up the temp file if the replace didn't happen.
        try:
            os.remove(tmp_path)
        except (OSError, NameError):
            pass
    _evict_oldest()


def _evict_oldest():
    """Delete oldest cache files until total size is under MAX_CACHE_SIZE_BYTES."""
    cache_dir = _get_cache_dir()
    try:
        files = [
            os.path.join(cache_dir, f)
            for f in os.listdir(cache_dir)
            if f.endswith(".json")
        ]
        total = sum(os.path.getsize(p) for p in files if os.path.exists(p))
        if total <= MAX_CACHE_SIZE_BYTES:
            return
        # Sort by mtime ascending (oldest first)
        files.sort(key=os.path.getmtime)
        for path in files:
            if total <= MAX_CACHE_SIZE_BYTES:
                break
            try:
                size = os.path.getsize(path)
                os.remove(path)
                total -= size
                xbmc.log(
                    "NZB-DAV: Cache evicted '{}'".format(os.path.basename(path)),
                    xbmc.LOGDEBUG,
                )
            except OSError:
                pass
    except OSError:
        pass


def clear_cache():
    """Delete all cached results."""
    cache_dir = _get_cache_dir()
    for f in os.listdir(cache_dir):
        if f.endswith(".json"):
            try:
                os.remove(os.path.join(cache_dir, f))
            except OSError:
                pass
