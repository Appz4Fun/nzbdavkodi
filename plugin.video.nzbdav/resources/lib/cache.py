# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Simple JSON-based search result cache."""

import json
import os
import time

import xbmc
import xbmcaddon
import xbmcvfs


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
    cache_ttl = int(addon.getSetting("cache_ttl") or "300")
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
    cache_ttl = int(addon.getSetting("cache_ttl") or "300")
    if cache_ttl <= 0:
        return

    key = _cache_key(search_type, title, **kwargs)
    path = os.path.join(_get_cache_dir(), key + ".json")

    try:
        data = {"timestamp": time.time(), "results": results}
        with open(path, "w") as f:
            json.dump(data, f)
        xbmc.log(
            "NZB-DAV: Cached {} results for '{}'".format(len(results), title),
            xbmc.LOGDEBUG,
        )
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
