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
MAX_CACHE_ENTRY_COUNT = 1000


def _get_cache_dir():
    addon = xbmcaddon.Addon()
    profile = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
    cache_dir = os.path.join(profile, "cache")
    # `exist_ok=True` rather than the exists-then-makedirs pattern, which
    # races a concurrent first-call: two callers can both observe "not
    # exists" and the second hits FileExistsError. TODO.md §H.2-L24.
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _cache_key(search_type, title, year="", imdb="", season="", episode=""):
    """Generate a filesystem-safe, collision-resistant cache key.

    Previous implementation collapsed non-alphanumeric characters to ``_``
    and truncated at 200 chars. That meant "Spider-Man: No Way Home" and
    "Spider_Man_ No Way Home" collapsed to the same filename, and any two
    distinct titles sharing a 200-char prefix aliased to the same cache
    file. Stale results would be served across searches.

    Switch to SHA-256 of the joined parts: deterministic, 64-char hex
    filename, no collisions in practice. Prefix the ``search_type`` so
    a glance at the cache dir still shows which bucket a file belongs
    to; the readable ``_make_legible_slug`` tail is cosmetic.
    """
    import hashlib

    parts = [search_type, title, year, imdb, season, episode]
    joined = "\x1f".join(
        str(p) for p in parts
    )  # unit-separator — can't appear in inputs
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()
    legible = "".join(c if c.isalnum() or c in "-_" else "_" for c in title)[:40]
    return "{}_{}_{}".format(search_type, legible or "untitled", digest)


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
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        timestamp = data.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        if time.time() - timestamp > cache_ttl:
            try:
                os.remove(path)
            except OSError:
                pass
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        xbmc.log("NZB-DAV: Cache hit for '{}'".format(title), xbmc.LOGDEBUG)
        return data.get("results", [])
    except json.JSONDecodeError:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    except OSError:
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
        with open(tmp_path, "w", encoding="utf-8") as f:
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


def _cache_file_size(path):
    """Return allocated bytes for cache eviction, falling back to logical size."""
    return _cache_size_from_stat(os.stat(path))


def _cache_size_from_stat(stat):
    """Return allocated bytes from a stat result, falling back to logical size."""
    blocks = getattr(stat, "st_blocks", None)
    if isinstance(blocks, int) and blocks > 0:
        return blocks * 512
    return stat.st_size


def _evict_oldest():
    """Delete oldest cache files until size and entry-count limits are met."""
    cache_dir = _get_cache_dir()
    try:
        total = 0
        entries = []
        for f in os.listdir(cache_dir):
            if not f.endswith(".json"):
                continue
            path = os.path.join(cache_dir, f)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            size = _cache_size_from_stat(stat)
            total += size
            entries.append((getattr(stat, "st_mtime", 0), path, size))

        if total <= MAX_CACHE_SIZE_BYTES and len(entries) <= MAX_CACHE_ENTRY_COUNT:
            return
        # Sort by mtime ascending (oldest first)
        entries.sort(key=lambda entry: entry[0])
        current_count = len(entries)
        while entries and (
            total > MAX_CACHE_SIZE_BYTES or current_count > MAX_CACHE_ENTRY_COUNT
        ):
            _mtime, path, size = entries.pop(0)
            try:
                os.remove(path)
            except OSError:
                if not os.path.exists(path):
                    total -= size
                    current_count -= 1
                continue
            total -= size
            current_count -= 1
            xbmc.log(
                "NZB-DAV: Cache evicted '{}'".format(os.path.basename(path)),
                xbmc.LOGDEBUG,
            )
    except OSError:
        pass


def clear_cache():
    """Delete all cached results.

    Tolerate a missing cache directory — `clear_cache` is exposed via
    the addon's settings menu and a user can hit it on a fresh install
    where the directory was never created. The previous unguarded
    ``os.listdir`` raised FileNotFoundError that bubbled up to the
    settings handler. TODO.md §H.2-M42.
    """
    cache_dir = _get_cache_dir()
    try:
        entries = os.listdir(cache_dir)
    except FileNotFoundError:
        return
    except OSError:
        return
    for f in entries:
        if f.endswith(".json"):
            try:
                os.remove(os.path.join(cache_dir, f))
            except OSError:
                pass
