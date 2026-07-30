# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Publish playback identity so TMDb Helper can scrobble to Trakt.

When playback starts from TMDb Helper, its scrobbler needs to know *which*
TMDB item is playing. It learns that from a Home-window property rather than
from the ListItem, because this add-on hands Kodi a proxy stream URL that
carries no library identity.

Two details make or break this handoff, and both fail silently:

* TMDb Helper reads its window properties through a helper that prefixes every
  key with ``TMDbHelper.``. Writing the bare ``PlayerInfoString`` key stores a
  property nothing ever reads.
* Trakt requires ``tmdb`` ids as JSON **integers**. Posting them as strings is
  rejected with HTTP 422, and TMDb Helper's request layer deliberately does not
  log 4xx bodies, so the scrobble simply never lands.
"""

import json

import xbmc
import xbmcgui

_WINDOW_ID = 10000
_PROPERTY = "TMDbHelper.PlayerInfoString"


def _clean(value):
    """Return ``value`` as a stripped string ("" when absent)."""
    return str(value or "").strip()


def _as_id(value):
    """Return a numeric id as ``int``, else the cleaned string, else ""."""
    value = _clean(value)
    if not value:
        return ""
    # Trakt rejects string tmdb ids (HTTP 422), so send real integers. Ids that
    # are not purely numeric are passed through untouched rather than dropped.
    return int(value) if value.isdigit() else value


def _as_number(value):
    """Return a season/episode number as ``int`` (0 when unusable)."""
    try:
        return int(_clean(value) or 0)
    except ValueError:
        return 0


def build_player_info(params):
    """Return TMDb Helper's player-info mapping for ``params``.

    Returns ``{}`` when the item cannot be identified, in which case callers
    should clear the property instead of writing a partial identity.
    """
    params = params or {}
    if _clean(params.get("type")).lower() == "episode":
        show_id = _as_id(params.get("show_tmdb_id") or params.get("tmdb_id"))
        if not show_id:
            return {}
        season = _as_number(params.get("season") or params.get("ep_season"))
        episode = _as_number(params.get("episode") or params.get("ep_episode"))
        if not season or not episode:
            return {}
        info = {
            "tmdb_type": "episode",
            "tmdb_id": show_id,
            "imdb_id": _clean(params.get("imdb")),
            "tvdb_id": _clean(params.get("tvdb")),
            "season": season,
            "episode": episode,
        }
    else:
        movie_id = _as_id(params.get("tmdb_id"))
        if not movie_id:
            return {}
        info = {
            "tmdb_type": "movie",
            "tmdb_id": movie_id,
            "imdb_id": _clean(params.get("imdb")),
        }
    return {key: value for key, value in info.items() if value}


def publish_player_info(params, window=None):
    """Write the player-info property for ``params``; clear it when unknown.

    Returns the published mapping (``{}`` when cleared). Never raises: a
    metadata handoff must not be able to break playback.
    """
    try:
        window = window or xbmcgui.Window(_WINDOW_ID)
        info = build_player_info(params)
        if not info:
            # Clear rather than leave a previous item's identity in place,
            # which would scrobble the wrong episode.
            window.clearProperty(_PROPERTY)
            return {}
        window.setProperty(_PROPERTY, json.dumps(info))
        xbmc.log(
            "NZB-DAV: Published TMDb Helper playback identity ({} {})".format(
                info.get("tmdb_type"), info.get("tmdb_id")
            ),
            xbmc.LOGINFO,
        )
        return info
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Could not publish TMDb Helper playback identity: {}".format(
                error
            ),
            xbmc.LOGWARNING,
        )
        return {}
