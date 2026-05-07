# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
from unittest.mock import MagicMock, patch

from resources.lib.player_installer import (
    PLAYER_JSON,
    TMDBHELPER_PLAYER_PATH,
    discover_other_player_targets,
    install_player,
    install_player_other,
)


def test_tmdbhelper_path_defined():
    assert "themoviedb.helper" in TMDBHELPER_PLAYER_PATH


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_writes_file(mock_vfs, mock_notify):
    """Player file gets written to TMDBHelper directory."""
    mock_vfs.exists.return_value = True
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_file = MagicMock()
    mock_vfs.File.return_value = mock_file

    install_player()

    # Installer may read the existing file first (schema-version check) and
    # then write. Assert the write happened and that each File call was
    # against nzbdav.json.
    assert mock_vfs.File.call_count >= 1
    write_calls = [
        c for c in mock_vfs.File.call_args_list if len(c[0]) >= 2 and c[0][1] == "w"
    ]
    assert len(write_calls) == 1
    assert "nzbdav.json" in write_calls[0][0][0]
    mock_file.write.assert_called_once()
    mock_notify.assert_called()


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_creates_directory_when_missing(mock_vfs, mock_notify):
    """Should call mkdirs when target dir doesn't exist."""
    mock_vfs.exists.return_value = False
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_file = MagicMock()
    mock_vfs.File.return_value = mock_file

    install_player()

    mock_vfs.mkdirs.assert_called_once()


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_handles_write_failure(mock_vfs, mock_notify):
    """Should catch write exceptions and notify about failure."""
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_vfs.exists.return_value = True
    mock_vfs.File.side_effect = OSError("Disk full")

    install_player()

    notify_calls = [str(c) for c in mock_notify.call_args_list]
    assert any("Failed" in s or "failed" in s for s in notify_calls)


def test_player_json_uses_script_handoff_instead_of_plugin_media_url():
    assert PLAYER_JSON["is_resolvable"] == "false"
    assert PLAYER_JSON["play_movie"].startswith(
        "executebuiltin://RunScript("
        "special://home/addons/plugin.video.nzbdav/addon.py,tmdb_play,"
    )
    assert "plugin://plugin.video.nzbdav" not in PLAYER_JSON["play_movie"]
    assert "plugin.video.nzbdav" in PLAYER_JSON["play_movie"]
    assert "type=movie" in PLAYER_JSON["play_movie"]
    assert "title={title_url}" in PLAYER_JSON["play_movie"]
    # tmdb_id must be forwarded so resolver can clear TMDBHelper bookmarks
    assert "tmdb_id={tmdb_id}" in PLAYER_JSON["play_movie"]
    assert PLAYER_JSON["play_episode"].startswith(
        "executebuiltin://RunScript("
        "special://home/addons/plugin.video.nzbdav/addon.py,tmdb_play,"
    )
    assert "plugin://plugin.video.nzbdav" not in PLAYER_JSON["play_episode"]
    assert "plugin.video.nzbdav" in PLAYER_JSON["play_movie"]
    assert "plugin.video.nzbdav" in PLAYER_JSON["play_episode"]
    assert "type=episode" in PLAYER_JSON["play_episode"]
    assert "title={showname_url}" in PLAYER_JSON["play_episode"]
    assert "tmdb_id={tmdb_id}" in PLAYER_JSON["play_episode"]
    roundtripped = json.loads(json.dumps(PLAYER_JSON))
    assert roundtripped["name"] == PLAYER_JSON["name"]


@patch("resources.lib.player_installer.xbmcaddon")
@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_enables_tmdbhelper_strm_only_for_script_handoff(
    mock_vfs, mock_notify, mock_addon
):
    """TMDBHelper action players should skip the dummy resolver probe.

    The CoreELEC crash reproduces before NZB-DAV starts when TMDBHelper asks
    Kodi to open plugin://plugin.video.nzbdav/... as media. Script handoff
    avoids that path, but TMDBHelper only executes non-resolvable actions
    directly when only_resolve_strm is true.
    """
    mock_vfs.exists.return_value = True
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_vfs.File.return_value = MagicMock()
    tmdb_addon = MagicMock()
    mock_addon.Addon.return_value = tmdb_addon

    install_player()

    mock_addon.Addon.assert_any_call("plugin.video.themoviedb.helper")
    tmdb_addon.setSetting.assert_called_once_with("only_resolve_strm", "true")


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_refuses_to_write_outside_addon_data(mock_vfs, mock_notify):
    """Defensive check: if special:// resolution maps TMDBHelper's player
    directory outside the Kodi profile's addon_data root, the installer
    must refuse to write and notify the user — no arbitrary-filesystem
    write."""

    # Force translatePath to return a path that doesn't live under
    # addon_data. realpath of both is stable since we picked real-ish
    # temp-ish paths the test host resolves identically.
    def _translate(path):
        if "profile/addon_data/" in path and path.endswith("addon_data/"):
            return "/home/kodi/.kodi/userdata/addon_data/"
        return "/tmp/hostile-elsewhere/plugin.video.themoviedb.helper/players/"

    mock_vfs.translatePath.side_effect = _translate
    mock_vfs.exists.return_value = True
    mock_vfs.File.return_value = MagicMock()

    install_player()

    # No write was attempted.
    write_calls = [
        c for c in mock_vfs.File.call_args_list if len(c[0]) >= 2 and c[0][1] == "w"
    ]
    assert len(write_calls) == 0, "Installer wrote despite escape-prevention check"
    # Notified the user of the failure.
    notify_msgs = [str(c) for c in mock_notify.call_args_list]
    assert any("TMDBHelper" in m for m in notify_msgs)


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_bails_when_mkdirs_fails(mock_vfs, mock_notify):
    """If the addon_data sub-directory doesn't exist and mkdirs() fails
    (filesystem read-only, permissions, etc.), the installer must bail
    out cleanly instead of trying to write into a non-existent dir."""
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_vfs.exists.return_value = False
    mock_vfs.mkdirs.return_value = False  # creation fails
    mock_vfs.File.return_value = MagicMock()

    install_player()

    write_calls = [
        c for c in mock_vfs.File.call_args_list if len(c[0]) >= 2 and c[0][1] == "w"
    ]
    assert len(write_calls) == 0
    notify_msgs = [str(c) for c in mock_notify.call_args_list]
    assert any("TMDBHelper" in m for m in notify_msgs)


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_preserves_existing_file_on_matching_schema(
    mock_vfs, mock_notify
):
    """If a user has a hand-edited nzbdav.json with the current
    schema_version, do NOT overwrite — preserves their customizations."""
    from resources.lib.player_installer import _PLAYER_SCHEMA_VERSION

    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_vfs.exists.return_value = True
    # The existing file reports the same schema_version as shipped code.
    existing_contents = (
        '{"name": "user-customized", "schema_version": '
        + str(_PLAYER_SCHEMA_VERSION)
        + "}"
    )
    mock_read_file = MagicMock()
    mock_read_file.read.return_value = existing_contents
    mock_write_file = MagicMock()

    # pylint: disable=keyword-arg-before-vararg
    # `open(path, mode, *)` is the builtin signature we mirror.
    def _file_factory(path, mode="r", *a, **kw):
        return mock_read_file if mode == "r" else mock_write_file

    mock_vfs.File.side_effect = _file_factory

    install_player()

    mock_write_file.write.assert_not_called()


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_backs_up_and_overwrites_on_schema_mismatch(
    mock_vfs, mock_notify
):
    """Existing stale player JSON must be backed up outside TMDBHelper's scan."""
    from resources.lib.player_installer import _PLAYER_SCHEMA_VERSION

    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_vfs.exists.return_value = True
    existing_contents = (
        '{"name": "stale", "schema_version": ' + str(_PLAYER_SCHEMA_VERSION - 1) + "}"
    )
    mock_read_file = MagicMock()
    mock_read_file.read.return_value = existing_contents
    mock_write_file = MagicMock()

    # pylint: disable=keyword-arg-before-vararg
    # `open(path, mode, *)` is the builtin signature we mirror.
    def _file_factory(path, mode="r", *a, **kw):
        return mock_read_file if mode == "r" else mock_write_file

    mock_vfs.File.side_effect = _file_factory

    install_player()

    # copy() was called before the overwrite fired. The backup filename must
    # not contain ".json" because TMDBHelper scans r".*\.json" with re.match
    # and will otherwise treat nzbdav.json.bak as an active player.
    assert mock_vfs.copy.called
    backup_args = mock_vfs.copy.call_args[0]
    assert backup_args[1].endswith("/nzbdav.bak")
    # And the write did land after the backup.
    mock_write_file.write.assert_called_once()


@patch("resources.lib.player_installer.xbmcaddon")
@patch("resources.lib.player_installer.xbmcvfs")
def test_discover_other_player_targets_lists_existing_non_tmdbhelper_player_dirs(
    mock_vfs, mock_addon
):
    """Other-player discovery should offer existing player folders and skip
    TMDBHelper, since TMDBHelper has its own direct install action."""

    mock_vfs.listdir.return_value = (
        [
            "plugin.video.themoviedb.helper",
            "plugin.video.seren",
            "plugin.video.empty",
            "script.module.not-a-player",
        ],
        [],
    )
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )

    def _exists(path):
        return path.endswith("/plugin.video.seren/players/")

    mock_vfs.exists.side_effect = _exists
    addon_names = {
        "plugin.video.seren": "Seren",
        "plugin.video.empty": "Empty Addon",
        "script.module.not-a-player": "Not A Player",
    }

    def _addon(addon_id=None):
        addon = MagicMock()
        addon.getAddonInfo.side_effect = lambda key: (
            addon_names.get(addon_id, addon_id or "") if key == "name" else ""
        )
        return addon

    mock_addon.Addon.side_effect = _addon

    targets = discover_other_player_targets()

    assert targets == [
        {
            "addon_id": "plugin.video.seren",
            "label": "Seren (plugin.video.seren)",
            "path": "special://profile/addon_data/plugin.video.seren/players/",
        }
    ]


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcgui")
@patch("resources.lib.player_installer.xbmcaddon")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_other_prompts_and_writes_selected_target(
    mock_vfs, mock_addon, mock_gui, mock_notify
):
    """The alternate installer should prompt for discovered player folders
    and write nzbdav.json into the selected addon's players directory."""

    mock_vfs.listdir.return_value = (
        ["plugin.video.seren", "plugin.video.umbrella"],
        [],
    )
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )

    def _exists(path):
        if path.endswith("/plugin.video.seren/players/"):
            return True
        if path.endswith("/plugin.video.umbrella/players/"):
            return True
        # Force installer to skip existing-file preservation and write fresh.
        if path.endswith("/nzbdav.json"):
            return False
        return True

    mock_vfs.exists.side_effect = _exists
    addon_names = {
        "plugin.video.seren": "Seren",
        "plugin.video.umbrella": "Umbrella",
    }

    def _addon(addon_id=None):
        addon = MagicMock()
        addon.getAddonInfo.side_effect = lambda key: (
            addon_names.get(addon_id, addon_id or "") if key == "name" else ""
        )
        return addon

    mock_addon.Addon.side_effect = _addon
    mock_gui.Dialog.return_value.select.return_value = 1
    mock_file = MagicMock()
    mock_vfs.File.return_value = mock_file

    install_player_other()

    mock_gui.Dialog.return_value.select.assert_called_once()
    write_calls = [
        c for c in mock_vfs.File.call_args_list if len(c[0]) >= 2 and c[0][1] == "w"
    ]
    assert len(write_calls) == 1
    assert "/plugin.video.umbrella/players/nzbdav.json" in write_calls[0][0][0]
    mock_file.write.assert_called_once()
    notify_msgs = [str(c) for c in mock_notify.call_args_list]
    assert any("Umbrella" in msg for msg in notify_msgs)


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcgui")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_other_notifies_when_no_other_targets(
    mock_vfs, mock_gui, mock_notify
):
    mock_vfs.listdir.return_value = (["plugin.video.themoviedb.helper"], [])
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_vfs.exists.return_value = False

    install_player_other()

    mock_gui.Dialog.return_value.select.assert_not_called()
    mock_vfs.File.assert_not_called()
    mock_notify.assert_called()
