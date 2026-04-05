# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

from resources.lib.playback_monitor import PlaybackMonitor


@patch("xbmcaddon.Addon")
def test_monitor_no_retry_when_disabled(mock_addon_cls):
    addon = MagicMock()
    addon.getSetting.return_value = "false"
    mock_addon_cls.return_value = addon

    monitor = PlaybackMonitor("http://test/movie.mkv", title="test")
    result = monitor.start_monitoring()
    assert result is True  # Returns immediately when disabled


@patch("xbmcaddon.Addon")
@patch("xbmc.Monitor")
def test_monitor_detects_playback_error(mock_monitor_cls, mock_addon_cls):
    addon = MagicMock()
    addon.getSetting.return_value = "true"
    mock_addon_cls.return_value = addon

    mock_monitor = MagicMock()
    mock_monitor.waitForAbort.return_value = False
    mock_monitor_cls.return_value = mock_monitor

    pm = PlaybackMonitor("http://test/movie.mkv", title="test", max_retries=0)

    # Simulate: playback starts then errors
    pm._playback_started = True
    pm._playback_error = True

    result = pm.start_monitoring()
    assert result is False  # Failed with max_retries=0


def test_playback_monitor_callbacks():
    pm = PlaybackMonitor("http://test/movie.mkv", title="test")

    pm.onAVStarted()
    assert pm._playback_started is True
    assert pm._retry_count == 0

    pm.onPlayBackStopped()
    assert pm._playback_ended is True

    pm2 = PlaybackMonitor("http://test/movie.mkv")
    pm2.onPlayBackError()
    assert pm2._playback_error is True


def test_playback_monitor_save_position():
    pm = PlaybackMonitor("http://test/movie.mkv")
    # When not playing, position should stay at 0
    pm._save_position()
    assert pm._last_position == 0.0
