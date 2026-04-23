# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the NZB-DAV background service."""

import time
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


def test_on_playback_stopped_tears_down_proxy_session():
    """onPlayBackStopped must call proxy.clear_sessions() so ffmpeg
    remux processes don't linger after the user presses stop."""
    from unittest.mock import MagicMock

    proxy = MagicMock()
    player = NzbdavPlayer(proxy=proxy)
    player._state = PlaybackState.MONITORING

    player.onPlayBackStopped()

    proxy.clear_sessions.assert_called_once()


def test_on_playback_ended_tears_down_proxy_session():
    """Natural end-of-playback must also release the proxy session."""
    from unittest.mock import MagicMock

    proxy = MagicMock()
    player = NzbdavPlayer(proxy=proxy)
    player._state = PlaybackState.MONITORING

    player.onPlayBackEnded()

    proxy.clear_sessions.assert_called_once()


def test_playback_stop_hook_survives_proxy_errors():
    """A misbehaving proxy must not crash the Kodi player callback —
    xbmc.Player callbacks run on Kodi's thread and an uncaught exception
    can destabilize the whole addon service."""
    from unittest.mock import MagicMock

    proxy = MagicMock()
    proxy.clear_sessions.side_effect = RuntimeError("boom")
    player = NzbdavPlayer(proxy=proxy)
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
    player._av_started = True

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


@patch("service.xbmcgui")
@patch("service.xbmc")
@patch("service._HOME_WINDOW")
def test_tick_shows_dialog_when_playback_never_started(
    mock_window, mock_xbmc, mock_gui
):
    """tick() shows error dialog when AV never starts within timeout.

    Threshold is 30 s now (raised from 5 s) to give the fmp4 HLS
    path enough headroom for HlsProducer spawn + ffmpeg
    analyzeduration + Kodi demuxer init."""
    mock_window.getProperty.return_value = ""

    player = NzbdavPlayer()
    player._state = PlaybackState.MONITORING
    player._av_started = False
    player._play_time = time.time() - 60  # well past the 30 s threshold
    player._title = "Shutter.Island.mkv"
    player.isPlaying = lambda: False

    player.tick()

    assert player._state == PlaybackState.IDLE
    mock_gui.Dialog.return_value.ok.assert_called_once()
    mock_xbmc.PlayList.return_value.clear.assert_called_once()


@patch("service._HOME_WINDOW")
def test_tick_waits_before_declaring_failure(mock_window):
    """tick() does not show error within the 30-second grace period.

    With the threshold raised from 5 s to 30 s for fmp4 HLS startup
    headroom, the legitimate proxy-side init path (ffmpeg analyze +
    init.mp4 + decoder bring-up) consistently takes 4-8 s on a slow
    box. A play_time 10 s in the past must not trigger the
    never-started dialog."""
    mock_window.getProperty.return_value = ""

    player = NzbdavPlayer()
    player._state = PlaybackState.MONITORING
    player._av_started = False
    player._play_time = time.time() - 10  # well under the 30 s threshold

    player.tick()

    assert player._state == PlaybackState.MONITORING
