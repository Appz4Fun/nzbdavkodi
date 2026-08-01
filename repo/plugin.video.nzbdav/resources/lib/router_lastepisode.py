# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Session-scoped "last picked episode" memory for the RunScript player.

TMDBHelper's Next-Up/widget re-invocation of the addon often arrives with
blank season/episode (only the series ids). The existing fallback recovers
those numbers from whatever Kodi ListItem currently has UI focus, which can
be stale (e.g. focus resets to the top of the season folder after an episode
finishes, reading back episode 1 instead of the next one). This module lets
that fallback prefer "the episode after whatever was last picked for this
show" instead, using the same ``xbmcgui.Window(10000)`` IPC pattern already
used by ``resolver_resume.py`` / ``stream_proxy_serviceprops.py`` for
cross-invocation state within a single Kodi session.
"""

import xbmcgui

_HOME_WINDOW_ID = 10000
_PROP_SHOW_ID = "nzbdav.last_ep_show_id"
_PROP_SEASON = "nzbdav.last_ep_season"
_PROP_EPISODE = "nzbdav.last_ep_episode"

# Preference order for building a per-show identity key. Season/episode are
# deliberately excluded -- this identifies the *show*, not the episode.
_ID_FIELDS = ("tmdb_id", "tvdb", "imdb")


def _show_identity_key(params):
    """Stable per-show key from ``params``, or "" if the show is unknown."""
    params = params if isinstance(params, dict) else {}
    for field in _ID_FIELDS:
        value = str(params.get(field) or "").strip()
        if value:
            return "{}:{}".format(field, value)
    title = str(params.get("title") or "").strip().casefold()
    return "title:{}".format(title) if title else ""


def remember_last_episode(params, season, episode):
    """Record ``season``/``episode`` as the last pick for this show.

    No-op when the show identity or season/episode can't be determined
    (harmless for movies, which never carry a numeric season/episode).
    """
    show_id = _show_identity_key(params)
    season = str(season or "")
    episode = str(episode or "")
    if not (show_id and season.isdigit() and episode.isdigit()):
        return
    home = xbmcgui.Window(_HOME_WINDOW_ID)
    home.setProperty(_PROP_SHOW_ID, show_id)
    home.setProperty(_PROP_SEASON, season)
    home.setProperty(_PROP_EPISODE, episode)


def recall_next_episode(params):
    """Return ``(season, episode)`` one past the last pick for this show.

    Returns ``("", "")`` when nothing is remembered, the remembered show
    doesn't match ``params``, or the stored numbers aren't usable.
    """
    show_id = _show_identity_key(params)
    if not show_id:
        return "", ""
    home = xbmcgui.Window(_HOME_WINDOW_ID)
    if home.getProperty(_PROP_SHOW_ID) != show_id:
        return "", ""
    season = home.getProperty(_PROP_SEASON)
    episode = home.getProperty(_PROP_EPISODE)
    if not (season.isdigit() and episode.isdigit()):
        return "", ""
    return season, str(int(episode) + 1)
