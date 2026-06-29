# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Picker label + IMDb-poster helpers split out of ``router``.

``_format_info_line`` and ``_get_tmdb_poster`` are imported by the suite from
``router`` (re-exported there). The IMDb suggestion lookup uses ``router``'s
module-level ``urlopen`` / ``_ORIGINAL_URLOPEN`` swap and ``_IMDB_ID_RE`` /
``_format_size``, reached at call time through ``import resources.lib.router as
_router`` so the conftest URL-opener swap and any ``@patch`` keep applying.
"""

from urllib import request as urllib_request

import xbmc


def _lookup_episode_info(imdb, tmdb_id=""):
    """Look up show title and episode info from IMDB ID via TMDB API.

    Used when TMDBHelper passes only IMDB ID without season/episode
    (e.g., from calendar widgets).
    """
    import resources.lib.router as _router

    # Reject non-IMDB input before hitting the network.
    if not imdb or not _router._IMDB_ID_RE.match(imdb):
        return None
    try:
        import json

        # Use IMDB suggestion API to get the show title
        url = "https://v2.sg.media-imdb.com/suggestion/t/{}.json".format(imdb)
        # nosemgrep
        opener = (
            urllib_request.urlopen
            if _router.urlopen is _router._ORIGINAL_URLOPEN
            else _router.urlopen
        )
        with opener(  # nosec B310 — IMDB suggestion API (trusted)
            url, timeout=5
        ) as resp:
            data = json.loads(resp.read())
            results = data.get("d", [])
            if results:
                title = results[0].get("l", "")
                if title:
                    xbmc.log(
                        "NZB-DAV: Looked up title '{}' for {}".format(title, imdb),
                        xbmc.LOGDEBUG,
                    )
                    return {"title": title}
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Episode lookup failed for {}: {}".format(imdb, e),
            xbmc.LOGDEBUG,
        )
    return None


def _format_info_line(item):
    """Format a single-line label with all parsed PTT elements.

    Example: 1080p | DV HDR10 | x265/HEVC | Atmos DD+ | en |
             31.2 GB | FLUX | NZBgeek | today
    """
    import resources.lib.router as _router

    meta = item.get("_meta", {})
    candidates = [
        meta.get("resolution", ""),
        " ".join(meta.get("hdr", [])),
        meta.get("codec", ""),
        " ".join(meta.get("audio", [])),
        "/".join(meta.get("languages", [])),
        _router._format_size(item.get("size")),
        meta.get("group", ""),
        item.get("indexer", ""),
        item.get("age", ""),
    ]
    parts = [part for part in candidates if part]
    return " | ".join(parts) if parts else "Unknown"


def _fetch_imdb_suggestion_poster(imdb_id):
    """Issue the IMDb suggestion request and return its poster URL (or "").

    Best-effort: any failure is logged at DEBUG and yields "". The TMDBHelper
    panel already has its own artwork so a miss here is not user-visible, but
    silently swallowing made this branch impossible to diagnose when the IMDb
    suggestion API changes shape.
    """
    import json

    import resources.lib.router as _router

    # Use TMDB's find endpoint (no API key needed for basic lookups via v3)
    # Fall back to a free poster service
    url = "https://v2.sg.media-imdb.com/suggestion/t/{}.json".format(imdb_id)
    try:
        # nosemgrep
        opener = (
            urllib_request.urlopen
            if _router.urlopen is _router._ORIGINAL_URLOPEN
            else _router.urlopen
        )
        with opener(  # nosec B310 — IMDB suggestion API (trusted)
            url, timeout=3
        ) as resp:
            data = json.loads(resp.read())
            results = data.get("d", [])
            poster = ""
            if results and results[0].get("i"):
                poster = results[0]["i"].get("imageUrl", "")
            if poster:
                xbmc.log(
                    "NZB-DAV: Got poster for {}: {}".format(imdb_id, poster[:80]),
                    xbmc.LOGDEBUG,
                )
            return poster
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: TMDB poster lookup failed for {}: {}".format(imdb_id, e),
            xbmc.LOGDEBUG,
        )
        return ""


def _get_tmdb_poster(imdb_id):
    """Fetch poster URL from TMDB using an IMDb ID. Returns empty string on failure."""
    import resources.lib.router as _router

    if not imdb_id or not _router._IMDB_ID_RE.match(imdb_id):
        return ""
    try:
        return _fetch_imdb_suggestion_poster(imdb_id)
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: TMDB poster lookup aborted for {}: {}".format(imdb_id, e),
            xbmc.LOGDEBUG,
        )
        return ""
