# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the NZB-DAV background service."""

from unittest.mock import patch

from service import NzbdavPlayer, PlaybackState


@patch("service._HOME_WINDOW")
def test_check_active_reads_window_properties(mock_window):
    """Service picks up stream info from window properties."""
    mock_window.getProperty.side_effect = lambda key: {
        "nzbdav.active": "true",
        "nzbdav.stream_url": "http://127.0.0.1:57800/stream",
        "nzbdav.stream_title": "movie.mkv",
    }.get(key, "")

    player = NzbdavPlayer()
    player._check_active()

    assert player._state == PlaybackState.MONITORING
    assert player._stream_url == "http://127.0.0.1:57800/stream"
    assert player._title == "movie.mkv"
    mock_window.clearProperty.assert_called_once_with("nzbdav.active")


@patch("service._HOME_WINDOW")
def test_check_active_ignores_when_not_signaled(mock_window):
    """Service stays inactive when no signal is set."""
    mock_window.getProperty.return_value = ""

    player = NzbdavPlayer()
    player._check_active()

    assert player._state == PlaybackState.IDLE


def test_on_av_started_resets_retry_count():
    """onAVStarted resets retry count when monitoring."""
    player = NzbdavPlayer()
    player._state = PlaybackState.MONITORING
    player._retry_count = 2

    player.onAVStarted()

    assert player._retry_count == 0
    assert player._state == PlaybackState.MONITORING


def test_on_playback_stopped_deactivates():
    """onPlayBackStopped transitions to IDLE."""
    player = NzbdavPlayer()
    player._state = PlaybackState.MONITORING

    player.onPlayBackStopped()

    assert player._state == PlaybackState.IDLE


def test_on_playback_error_sets_flag():
    """onPlayBackError transitions to ERROR when monitoring."""
    player = NzbdavPlayer()
    player._state = PlaybackState.MONITORING

    player.onPlayBackError()

    assert player._state == PlaybackState.ERROR


def test_on_playback_error_ignored_when_inactive():
    """onPlayBackError does nothing when not monitoring."""
    player = NzbdavPlayer()
    player._state = PlaybackState.IDLE

    player.onPlayBackError()

    assert player._state == PlaybackState.IDLE


@patch("service._HOME_WINDOW")
def test_tick_deactivates_when_retries_exhausted(mock_window):
    """tick() deactivates after max retries are reached."""
    mock_window.getProperty.return_value = ""

    player = NzbdavPlayer()
    player._state = PlaybackState.ERROR
    player._retry_count = 5
    player._title = "test"

    with patch.object(player, "_read_settings", return_value=(True, 3, 1)):
        player.tick()

    assert player._state == PlaybackState.IDLE


@patch("service._HOME_WINDOW")
def test_tick_does_nothing_when_no_error(mock_window):
    """tick() does nothing when playback is fine."""
    mock_window.getProperty.return_value = ""

    player = NzbdavPlayer()
    player._state = PlaybackState.MONITORING

    player.tick()

    assert player._state == PlaybackState.MONITORING


@patch("service._HOME_WINDOW")
def test_tick_skips_retry_when_disabled(mock_window):
    """tick() deactivates without retrying when auto-retry is off."""
    mock_window.getProperty.return_value = ""

    player = NzbdavPlayer()
    player._state = PlaybackState.ERROR
    player._title = "test"

    with patch.object(player, "_read_settings", return_value=(False, 3, 5)):
        player.tick()

    assert player._state == PlaybackState.IDLE
