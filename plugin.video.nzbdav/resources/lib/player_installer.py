# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Player JSON installer for TMDBHelper."""

import json
import os

import xbmc
import xbmcvfs

from resources.lib.http_util import notify as _notify
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt

TMDBHELPER_PLAYER_PATH = (
    "special://profile/addon_data/plugin.video.themoviedb.helper/players/"
)

PLAYER_JSON = {
    "name": "NZB-DAV",
    "plugin": "plugin.video.nzbdav",
    "priority": 100,
    "is_resolvable": "true",
    "play_movie": (
        "plugin://plugin.video.nzbdav/play?type=movie"
        "&title={title}&year={year}&imdb={imdb}&tmdb_id={tmdb_id}"
    ),
    "play_episode": (
        "plugin://plugin.video.nzbdav/play?type=episode"
        "&title={showname}&year={showyear}&season={season}&episode={episode}"
        "&imdb={imdb}&tmdb_id={tmdb_id}"
        "&ep_season={ep_showseason}&ep_episode={ep_showepisode}"
    ),
}


def install_player():
    """Install player JSON to TMDBHelper."""
    player_content = json.dumps(PLAYER_JSON, indent=4)

    xbmc.log(
        "NZB-DAV: Installing player to TMDBHelper at {}".format(TMDBHELPER_PLAYER_PATH),
        xbmc.LOGINFO,
    )
    try:
        real_path = xbmcvfs.translatePath(TMDBHELPER_PLAYER_PATH)
        if not xbmcvfs.exists(real_path):
            xbmcvfs.mkdirs(real_path)

        file_path = os.path.join(real_path, "nzbdav.json")
        f = xbmcvfs.File(file_path, "w")
        try:
            f.write(player_content)
            xbmc.log("NZB-DAV: Player installed successfully", xbmc.LOGINFO)
            _notify(_addon_name(), _fmt(30094, "TMDBHelper"))
        finally:
            f.close()
    except Exception as e:
        xbmc.log("NZB-DAV: Failed to install player: {}".format(e), xbmc.LOGERROR)
        _notify(_addon_name(), _fmt(30095, "TMDBHelper"))
