# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Kodi ListItem identity for resolved movie and episode playback."""

import json
from urllib.parse import unquote, urlencode

from resources.lib import resolver as _resolver


def _clean_id(params, *keys):
    for key in keys:
        value = str((params or {}).get(key, "") or "").strip()
        if value and value != "_":
            return value
    return ""


def _episode_identity(params):
    """Return stable show/episode identity carried by a TMDBHelper action."""
    params = params or {}
    return {
        "show_tmdb_id": _clean_id(params, "show_tmdb_id", "tmdb_id"),
        "episode_tmdb_id": _clean_id(params, "episode_tmdb_id"),
        "show_imdb_id": _clean_id(params, "imdb"),
        "showtitle": unquote(str(params.get("title", "") or "")),
        "season": params.get("season", params.get("ep_season", "")),
        "episode": params.get("episode", params.get("ep_episode", "")),
    }


def _tmdb_helper_metadata(params):
    """Return TMDBHelper's rich metadata using the TV-show id for episodes."""
    params = params or {}
    media_type = str(params.get("type", "movie") or "movie").lower()
    identity = _episode_identity(params)
    tmdb_id = (
        identity["show_tmdb_id"]
        if media_type == "episode"
        else _clean_id(params, "tmdb_id")
    )
    if not tmdb_id:
        return {}

    query = {
        "info": "details",
        "tmdb_type": "tv" if media_type == "episode" else "movie",
        "tmdb_id": tmdb_id,
    }
    if media_type == "episode":
        query["season"] = identity["season"]
        query["episode"] = identity["episode"]

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
        response = json.loads(_resolver.xbmc.executeJSONRPC(json.dumps(request)))
        files = response.get("result", {}).get("files", [])
        if files:
            return files[0]
    except (AttributeError, RuntimeError, TypeError, ValueError) as error:
        _resolver.xbmc.log(
            "NZB-DAV: TMDBHelper metadata lookup failed: {}".format(error),
            _resolver.xbmc.LOGWARNING,
        )
    return {}


def _fallback_info(params):
    params = params or {}
    media_type = str(params.get("type", "") or "").lower()
    if media_type == "episode":
        identity = _episode_identity(params)
        return {
            "title": identity["showtitle"],
            "tvshowtitle": identity["showtitle"],
            "year": params.get("year", ""),
            "season": identity["season"],
            "episode": identity["episode"],
            "mediatype": "episode",
        }
    return {
        "title": unquote(str(params.get("title", "") or "")),
        "year": params.get("year", ""),
        "mediatype": "movie",
    }


def _stable_unique_ids(params, metadata):
    params = params or {}
    unique_ids = dict((metadata or {}).get("uniqueid") or {})
    media_type = str(params.get("type", "") or "").lower()
    if media_type == "episode":
        identity = _episode_identity(params)
        if identity["show_tmdb_id"]:
            unique_ids["tvshow.tmdb"] = identity["show_tmdb_id"]
        if identity["episode_tmdb_id"]:
            unique_ids["tmdb"] = identity["episode_tmdb_id"]
        if identity["show_imdb_id"]:
            unique_ids["tvshow.imdb"] = identity["show_imdb_id"]
    else:
        tmdb_id = _clean_id(params, "tmdb_id")
        imdb_id = _clean_id(params, "imdb")
        if tmdb_id:
            unique_ids["tmdb"] = tmdb_id
        if imdb_id:
            unique_ids["imdb"] = imdb_id
    return unique_ids


def _apply_playback_identity(li, params):
    """Attach rich metadata plus stable fallback identity to a playback item."""
    params = params or {}
    metadata = _tmdb_helper_metadata(params)
    info_keys = (
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
    info = {
        key: metadata[key] for key in info_keys if metadata.get(key) not in (None, "")
    }
    for key, value in _fallback_info(params).items():
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

    unique_ids = _stable_unique_ids(params, metadata)
    if unique_ids:
        default_id = "tmdb" if unique_ids.get("tmdb") else ""
        li.setUniqueIDs(unique_ids, default_id)
