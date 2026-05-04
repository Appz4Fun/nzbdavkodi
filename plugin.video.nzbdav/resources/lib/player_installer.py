# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Player JSON installer for TMDBHelper and compatible player folders."""

import json
import os

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from resources.lib.http_util import notify as _notify
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string

ADDON_DATA_ROOT = "special://profile/addon_data/"
NZBDAV_ADDON_ID = "plugin.video.nzbdav"
PLAYER_FILENAME = "nzbdav.json"
TMDBHELPER_ADDON_ID = "plugin.video.themoviedb.helper"
TMDBHELPER_LABEL = "TMDBHelper"


def _player_path_for(addon_id):
    return ADDON_DATA_ROOT + addon_id + "/players/"


TMDBHELPER_PLAYER_PATH = _player_path_for(TMDBHELPER_ADDON_ID)

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


def _addon_label(addon_id):
    try:
        name = xbmcaddon.Addon(addon_id).getAddonInfo("name")
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Could not read addon name for {}: {}".format(addon_id, e),
            xbmc.LOGDEBUG,
        )
        name = ""

    if isinstance(name, str) and name and name != addon_id:
        return "{} ({})".format(name, addon_id)
    return addon_id


def discover_other_player_targets():
    """Return non-TMDBHelper addon_data player folders that already exist."""
    try:
        addon_dirs, _files = xbmcvfs.listdir(ADDON_DATA_ROOT)
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Failed to list addon_data for player targets: {}".format(e),
            xbmc.LOGWARNING,
        )
        return []

    targets = []
    for addon_id in sorted(addon_dirs):
        if addon_id in (TMDBHELPER_ADDON_ID, NZBDAV_ADDON_ID):
            continue

        player_path = _player_path_for(addon_id)
        real_path = xbmcvfs.translatePath(player_path)
        if not xbmcvfs.exists(real_path):
            continue

        targets.append(
            {
                "addon_id": addon_id,
                "label": _addon_label(addon_id),
                "path": player_path,
            }
        )

    return targets


def _install_player_to_path(target_name, target_path):
    """Install player JSON to the requested Kodi player directory."""
    player_content = json.dumps(PLAYER_JSON, indent=4)

    xbmc.log(
        "NZB-DAV: Installing player to {} at {}".format(target_name, target_path),
        xbmc.LOGINFO,
    )
    try:
        real_path = xbmcvfs.translatePath(target_path)

        # Defensive check: the resolved real_path must sit under the Kodi
        # profile's addon_data directory. If special:// resolution is ever
        # hijacked (symlink, environment override, Kodi mis-config) we'd
        # otherwise happily write nzbdav.json anywhere on disk.
        profile_root = xbmcvfs.translatePath(ADDON_DATA_ROOT)
        # Use os.path.commonpath so a sibling like
        # `/.../addon_data_evil/...` doesn't pass the prefix check just
        # because its name happens to start with `addon_data`. Closes
        # TODO.md §H.3.
        real_resolved = os.path.realpath(real_path)
        profile_resolved = os.path.realpath(profile_root)
        try:
            common = os.path.commonpath([real_resolved, profile_resolved])
        except ValueError:
            # Different drive on Windows — definitely not inside profile_root.
            common = ""
        if common != profile_resolved:
            xbmc.log(
                "NZB-DAV: Refusing to install player outside addon_data "
                "(resolved {} from {})".format(real_path, target_path),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _fmt(30095, target_name))
            return

        if not xbmcvfs.exists(real_path):
            if not xbmcvfs.mkdirs(real_path):
                xbmc.log(
                    "NZB-DAV: Failed to create player directory {}".format(real_path),
                    xbmc.LOGERROR,
                )
                _notify(_addon_name(), _fmt(30095, target_name))
                return

        file_path = os.path.join(real_path, PLAYER_FILENAME)

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
                    _notify(_addon_name(), _fmt(30094, target_name))
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
            # xbmcvfs.File.write returns False on disk-full / permission
            # failure rather than raising; without this check the install
            # path used to log "successfully" and toast a success
            # notification on a partial write. TODO.md §H.2-L23.
            wrote = f.write(player_content)
            if wrote is False:
                raise OSError(
                    "xbmcvfs.File.write returned False "
                    "(disk-full or permission failure)"
                )
            xbmc.log("NZB-DAV: Player installed successfully", xbmc.LOGINFO)
            _notify(_addon_name(), _fmt(30094, target_name))
        finally:
            f.close()
    except Exception as e:
        xbmc.log("NZB-DAV: Failed to install player: {}".format(e), xbmc.LOGERROR)
        _notify(_addon_name(), _fmt(30095, target_name))


def install_player():
    """Install player JSON to TMDBHelper."""
    _install_player_to_path(TMDBHELPER_LABEL, TMDBHELPER_PLAYER_PATH)


def install_player_other():
    """Prompt for another compatible player directory and install there."""
    targets = discover_other_player_targets()
    if not targets:
        _notify(_addon_name(), _string(30162), 5000)
        return

    labels = [target["label"] for target in targets]
    selected = xbmcgui.Dialog().select(_string(30161), labels)
    if selected < 0:
        return

    target = targets[selected]
    _install_player_to_path(target["label"], target["path"])
