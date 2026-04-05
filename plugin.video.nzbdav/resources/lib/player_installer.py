# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Player JSON installer for TMDBHelper and compatible addons."""

import json
import os

import xbmc
import xbmcaddon
import xbmcvfs

from resources.lib.http_util import notify as _notify

PLAYER_TARGETS = {
    "TMDBHelper": {
        "setting_id": "install_tmdbhelper",
        "path": "special://profile/addon_data/plugin.video.themoviedb.helper/players/",
    },
    "Fen": {
        "setting_id": "install_fen",
        "path": "special://profile/addon_data/plugin.video.fen/players/",
    },
    "Seren": {
        "setting_id": "install_seren",
        "path": "special://profile/addon_data/plugin.video.seren/players/",
    },
}

PLAYER_JSON = {
    "name": "NZB-DAV",
    "plugin": "plugin.video.nzbdav",
    "priority": 100,
    "is_resolvable": "true",
    "play_movie": "executebuiltin://RunPlugin(plugin://plugin.video.nzbdav/play?type=movie&title={title}&year={year}&imdb={imdb})",
    "play_episode": "executebuiltin://RunPlugin(plugin://plugin.video.nzbdav/play?type=episode&title={showname}&year={showyear}&season={season}&episode={episode}&imdb={imdb})",
}


def get_install_targets():
    addon = xbmcaddon.Addon()
    targets = []
    for name, config in PLAYER_TARGETS.items():
        val = addon.getSetting(config["setting_id"])
        xbmc.log(
            "NZB-DAV: Setting '{}' = '{}' (type={})".format(
                config["setting_id"], val, type(val).__name__
            ),
            xbmc.LOGDEBUG,
        )
        if val.lower() == "true":
            targets.append((name, config["path"]))
    xbmc.log(
        "NZB-DAV: Install targets selected: {}".format([t[0] for t in targets]),
        xbmc.LOGINFO,
    )
    return targets


def install_player():
    """Install player JSON via a multi-select dialog."""
    import xbmcgui

    names = list(PLAYER_TARGETS.keys())
    dialog = xbmcgui.Dialog()
    selected = dialog.multiselect("Install NZB-DAV Player To", names)

    if selected is None or len(selected) == 0:
        return

    targets = [(names[i], PLAYER_TARGETS[names[i]]["path"]) for i in selected]

    if not targets:
        return

    player_content = json.dumps(PLAYER_JSON, indent=4)
    succeeded = []
    failed = []

    for name, path in targets:
        xbmc.log(
            "NZB-DAV: Installing player to {} at {}".format(name, path), xbmc.LOGINFO
        )
        try:
            real_path = xbmcvfs.translatePath(path)
            if not xbmcvfs.exists(real_path):
                xbmcvfs.mkdirs(real_path)

            file_path = os.path.join(real_path, "nzbdav.json")
            f = xbmcvfs.File(file_path, "w")
            try:
                f.write(player_content)
                succeeded.append(name)
                xbmc.log(
                    "NZB-DAV: Player installed successfully to {}".format(name),
                    xbmc.LOGINFO,
                )
            finally:
                f.close()
        except Exception as e:
            failed.append(name)
            xbmc.log(
                "NZB-DAV: Failed to install player to {}: {}".format(name, e),
                xbmc.LOGERROR,
            )

    if succeeded:
        _notify("NZB-DAV", "Player installed to: {}".format(", ".join(succeeded)))
    if failed:
        _notify("NZB-DAV", "Failed to install to: {}".format(", ".join(failed)))
