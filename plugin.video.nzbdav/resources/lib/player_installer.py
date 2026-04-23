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

# Bump this when PLAYER_JSON's shape changes in a way that requires the
# installer to overwrite an older generation. We ignore the user's manual
# edits only when the stored schema_version differs from ours.
_PLAYER_SCHEMA_VERSION = 1

PLAYER_JSON = {
    "name": "NZB-DAV",
    "plugin": "plugin.video.nzbdav",
    "priority": 100,
    "is_resolvable": "true",
    "schema_version": _PLAYER_SCHEMA_VERSION,
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

        # Defensive check: the resolved real_path must sit under the Kodi
        # profile's addon_data directory. If special:// resolution is ever
        # hijacked (symlink, environment override, Kodi mis-config) we'd
        # otherwise happily write nzbdav.json anywhere on disk.
        profile_root = xbmcvfs.translatePath("special://profile/addon_data/")
        if not os.path.realpath(real_path).startswith(os.path.realpath(profile_root)):
            xbmc.log(
                "NZB-DAV: Refusing to install player outside addon_data "
                "(resolved {} from {})".format(real_path, TMDBHELPER_PLAYER_PATH),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _fmt(30095, "TMDBHelper"))
            return

        if not xbmcvfs.exists(real_path):
            if not xbmcvfs.mkdirs(real_path):
                xbmc.log(
                    "NZB-DAV: Failed to create TMDBHelper player directory {}".format(
                        real_path
                    ),
                    xbmc.LOGERROR,
                )
                _notify(_addon_name(), _fmt(30095, "TMDBHelper"))
                return

        file_path = os.path.join(real_path, "nzbdav.json")

        # If an existing nzbdav.json is present with the SAME schema_version,
        # skip the overwrite so a user who edited the file (e.g. customized
        # priority, added extra fields) doesn't lose those edits on every
        # addon upgrade. Different schema_version → overwrite with a backup.
        if xbmcvfs.exists(file_path):
            try:
                existing_f = xbmcvfs.File(file_path, "r")
                try:
                    existing_text = existing_f.read()
                finally:
                    existing_f.close()
                existing = json.loads(existing_text)
                if existing.get("schema_version") == _PLAYER_SCHEMA_VERSION:
                    xbmc.log(
                        "NZB-DAV: Player already installed at schema v{}; "
                        "preserving existing file".format(_PLAYER_SCHEMA_VERSION),
                        xbmc.LOGINFO,
                    )
                    _notify(_addon_name(), _fmt(30094, "TMDBHelper"))
                    return
                # Schema change — back up the old file before overwriting.
                backup_path = file_path + ".bak"
                try:
                    xbmcvfs.copy(file_path, backup_path)
                except Exception as e:  # pylint: disable=broad-except
                    xbmc.log(
                        "NZB-DAV: Could not back up {} to {}: {}".format(
                            file_path, backup_path, e
                        ),
                        xbmc.LOGWARNING,
                    )
            except (OSError, ValueError, TypeError):
                # Unreadable or malformed existing file (including the
                # MagicMock-returns-MagicMock case in tests) — just
                # overwrite.
                pass

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
