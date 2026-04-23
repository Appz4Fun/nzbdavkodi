# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
from unittest.mock import MagicMock, patch

from resources.lib.player_installer import (
    PLAYER_JSON,
    TMDBHELPER_PLAYER_PATH,
    install_player,
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


def test_player_json_contains_correct_plugin_url_patterns():
    assert "plugin.video.nzbdav" in PLAYER_JSON["play_movie"]
    assert "type=movie" in PLAYER_JSON["play_movie"]
    assert "{title}" in PLAYER_JSON["play_movie"]
    # tmdb_id must be forwarded so resolver can clear TMDBHelper bookmarks
    assert "tmdb_id={tmdb_id}" in PLAYER_JSON["play_movie"]
    assert "plugin.video.nzbdav" in PLAYER_JSON["play_episode"]
    assert "type=episode" in PLAYER_JSON["play_episode"]
    assert "{showname}" in PLAYER_JSON["play_episode"]
    assert "tmdb_id={tmdb_id}" in PLAYER_JSON["play_episode"]
    roundtripped = json.loads(json.dumps(PLAYER_JSON))
    assert roundtripped["name"] == PLAYER_JSON["name"]
