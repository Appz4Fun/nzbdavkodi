# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Attach TMDb Helper's rich metadata to the playing ListItem.

Playback here hands Kodi a bare proxy stream URL with no library identity, so
without this the video info dialog, OSD, and Home widgets show only the raw
filename: no title, plot, artwork, cast, season, or episode number.

The ListItem is built deep inside the finish-playback functions, past where
the caller (which has the TMDB params in scope) hands off, so the params are
round-tripped through a window property the same way playback identity is
published to TMDb Helper's scrobbler (see ``tmdbhelper_scrobble``).
"""

import json
from urllib.parse import unquote, urlencode

import xbmc
import xbmcgui

_WINDOW_ID = 10000
_PROPERTY = "nzbdav.metadata_params"

_INFO_KEYS = (
    "title",
    "year",
    "plot",
    "director",
    "writer",
    "genre",
    "rating",
    "userrating",
    "premiered",
    "originaltitle",
    "tagline",
    "studio",
    "duration",
    "showtitle",
    "season",
    "episode",
)


def _clean(value):
    """Return ``value`` as a stripped string ("" when absent)."""
    return str(value or "").strip()


def publish_params(params, window=None):
    """Stash ``params`` for the ListItem builder to read back (best-effort).

    Clears the property when there is nothing useful, so a stale item's
    params can never be attached to the next play's ListItem.
    """
    try:
        window = window or xbmcgui.Window(_WINDOW_ID)
        if not params:
            window.clearProperty(_PROPERTY)
            return
        window.setProperty(_PROPERTY, json.dumps(params))
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Could not publish metadata params: {}".format(error),
            xbmc.LOGWARNING,
        )


def fetch_metadata(params):
    """Return TMDb Helper's native rich item data for ``params`` ({} on miss)."""
    params = params or {}
    media_type = _clean(params.get("type") or "movie").lower()
    tmdb_id = _clean(
        params.get("show_tmdb_id", params.get("tmdb_id", ""))
        if media_type == "episode"
        else params.get("tmdb_id", "")
    )
    if not tmdb_id:
        return {}

    query = {
        "info": "details",
        "tmdb_type": "tv" if media_type == "episode" else "movie",
        "tmdb_id": tmdb_id,
    }
    if media_type == "episode":
        query["season"] = params.get("season", params.get("ep_season", ""))
        query["episode"] = params.get("episode", params.get("ep_episode", ""))

    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "Files.GetDirectory",
        "params": {
            "directory": (
                "plugin://plugin.video.themoviedb.helper/?" + urlencode(query)
            ),
            "media": "video",
            "properties": [
                "title",
                "year",
                "plot",
                "cast",
                "director",
                "writer",
                "art",
                "thumbnail",
                "fanart",
                "imdbnumber",
                "uniqueid",
                "genre",
                "rating",
                "userrating",
                "premiered",
                "originaltitle",
                "tagline",
                "studio",
                "duration",
                "showtitle",
                "season",
                "episode",
            ],
        },
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
        files = response.get("result", {}).get("files", [])
        if files:
            xbmc.log(
                "NZB-DAV: Attached TMDb Helper metadata for tmdb_id={}".format(tmdb_id),
                xbmc.LOGINFO,
            )
            return files[0]
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        xbmc.log(
            "NZB-DAV: TMDb Helper metadata lookup failed: {}".format(error),
            xbmc.LOGWARNING,
        )
    return {}


def apply_metadata(li, params):
    """Attach TMDb Helper metadata (info/art/cast/ids) to ``li``.

    Never raises: a metadata lookup must not be able to break playback.
    """
    try:
        params = params or {}
        media_type = _clean(params.get("type")).lower()
        is_episode = media_type == "episode"
        show_title = unquote(_clean(params.get("title")))
        season = params.get("season", params.get("ep_season", ""))
        episode = params.get("episode", params.get("ep_episode", ""))
        metadata = fetch_metadata(params)

        info = {
            key: metadata[key]
            for key in _INFO_KEYS
            if metadata.get(key) not in (None, "")
        }
        if info.pop("showtitle", None) and not info.get("tvshowtitle"):
            info["tvshowtitle"] = metadata["showtitle"]
        fallback_info = {
            "title": show_title,
            "year": params.get("year", ""),
            "mediatype": "episode" if is_episode else "movie",
        }
        if is_episode:
            fallback_info.update(
                {
                    "tvshowtitle": show_title,
                    "season": season,
                    "episode": episode,
                }
            )
        for key, value in fallback_info.items():
            if value not in (None, ""):
                info.setdefault(key, value)
        li.setInfo("video", info)

        art = dict(metadata.get("art") or {})
        if metadata.get("thumbnail") and not art.get("thumb"):
            art["thumb"] = metadata["thumbnail"]
        if metadata.get("fanart") and not art.get("fanart"):
            art["fanart"] = metadata["fanart"]
        if art:
            li.setArt(art)
        if metadata.get("cast"):
            li.setCast(metadata["cast"])

        unique_ids = dict(metadata.get("uniqueid") or {})
        if is_episode:
            show_tmdb_id = _clean(params.get("show_tmdb_id"))
            episode_tmdb_id = _clean(params.get("episode_tmdb_id"))
            show_imdb_id = _clean(params.get("imdb"))
            if show_tmdb_id:
                unique_ids["tvshow.tmdb"] = show_tmdb_id
            if episode_tmdb_id and episode_tmdb_id != show_tmdb_id:
                unique_ids["tmdb"] = episode_tmdb_id
            if show_imdb_id:
                unique_ids["tvshow.imdb"] = show_imdb_id
        else:
            tmdb_id = _clean(params.get("tmdb_id"))
            imdb_id = _clean(params.get("imdb"))
            if tmdb_id:
                unique_ids["tmdb"] = tmdb_id
            if imdb_id:
                unique_ids["imdb"] = imdb_id
        if unique_ids:
            li.setUniqueIDs(unique_ids, "tmdb" if unique_ids.get("tmdb") else "")
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Could not apply TMDb Helper metadata: {}".format(error),
            xbmc.LOGWARNING,
        )


def apply_from_published_params(li, window=None):
    """Read the params published by ``publish_params`` and apply to ``li``.

    Best-effort: a missing, empty, or malformed property means nothing was
    published for this play, which is a normal case (e.g. no TMDB identity
    available), not an error.
    """
    try:
        window = window or xbmcgui.Window(_WINDOW_ID)
        raw = window.getProperty(_PROPERTY)
        if not raw:
            return
        params = json.loads(raw)
    except (TypeError, ValueError, AttributeError):
        return
    apply_metadata(li, params)
