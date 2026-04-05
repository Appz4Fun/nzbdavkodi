# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
from unittest.mock import MagicMock, patch

# xbmcgui is imported lazily inside install_player(), so we patch
# the global mock from conftest (sys.modules["xbmcgui"])
import xbmcgui
from resources.lib.player_installer import (
    PLAYER_JSON,
    PLAYER_TARGETS,
    install_player,
)


def test_player_targets_defined():
    assert "TMDBHelper" in PLAYER_TARGETS
    assert "Fen" in PLAYER_TARGETS
    assert "Seren" in PLAYER_TARGETS


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_writes_to_selected(mock_vfs, mock_notify):
    """Dialog selects TMDBHelper, player file gets written."""
    dialog = MagicMock()
    dialog.multiselect.return_value = [0]  # First item (TMDBHelper)
    xbmcgui.Dialog.return_value = dialog

    mock_vfs.exists.return_value = True
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_file = MagicMock()
    mock_vfs.File.return_value = mock_file

    install_player()

    mock_vfs.File.assert_called()
    mock_notify.assert_called()


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_cancelled(mock_vfs, mock_notify):
    """User cancels dialog, nothing happens."""
    dialog = MagicMock()
    dialog.multiselect.return_value = None
    xbmcgui.Dialog.return_value = dialog

    install_player()

    mock_vfs.File.assert_not_called()
    mock_notify.assert_not_called()


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_multiple_targets(mock_vfs, mock_notify):
    """Dialog selects TMDBHelper and Seren, both get written."""
    dialog = MagicMock()
    dialog.multiselect.return_value = [0, 2]  # TMDBHelper and Seren
    xbmcgui.Dialog.return_value = dialog

    mock_vfs.exists.return_value = True
    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_file = MagicMock()
    mock_vfs.File.return_value = mock_file

    install_player()

    assert mock_vfs.File.call_count == 2


@patch("resources.lib.player_installer._notify")
@patch("resources.lib.player_installer.xbmcvfs")
def test_install_player_creates_directory_when_missing(mock_vfs, mock_notify):
    """Should call mkdirs when target dir doesn't exist."""
    dialog = MagicMock()
    dialog.multiselect.return_value = [0]
    xbmcgui.Dialog.return_value = dialog

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
    dialog = MagicMock()
    dialog.multiselect.return_value = [0]
    xbmcgui.Dialog.return_value = dialog

    mock_vfs.translatePath.side_effect = lambda p: p.replace(
        "special://profile", "/home/kodi/.kodi/userdata"
    )
    mock_vfs.exists.return_value = True
    mock_vfs.File.side_effect = OSError("Disk full")

    install_player()

    notify_calls = [str(c) for c in mock_notify.call_args_list]
    assert any("Failed" in s or "failed" in s for s in notify_calls)


def test_player_json_contains_correct_plugin_url_patterns():
    assert "plugin.video.nzbdav" in PLAYER_JSON["play_movie"]
    assert "type=movie" in PLAYER_JSON["play_movie"]
    assert "{title}" in PLAYER_JSON["play_movie"]
    assert "plugin.video.nzbdav" in PLAYER_JSON["play_episode"]
    assert "type=episode" in PLAYER_JSON["play_episode"]
    assert "{showname}" in PLAYER_JSON["play_episode"]
    roundtripped = json.loads(json.dumps(PLAYER_JSON))
    assert roundtripped["name"] == PLAYER_JSON["name"]
