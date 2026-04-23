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


def test_service_main_loop_absorbs_tick_exceptions():
    """A crash inside ``player.tick()`` used to bubble up to main()'s
    loop and kill the service, silently breaking every future stream.
    The hardened main() wraps tick() so a single exception just logs
    and the loop keeps spinning."""
    from unittest.mock import MagicMock

    import service

    # monitor.abortRequested returns True on the 3rd call → 2 full loop
    # iterations, each calling player.tick() once.
    mock_monitor = MagicMock()
    mock_monitor.abortRequested.side_effect = [False, False, True]
    mock_monitor.waitForAbort.return_value = False

    failing_player = MagicMock()
    failing_player.tick.side_effect = RuntimeError("boom")

    mock_proxy = MagicMock()
    mock_proxy.port = 12345

    with patch("service.xbmc.Monitor", return_value=mock_monitor), patch(
        "service.StreamProxy", return_value=mock_proxy
    ), patch("service.NzbdavPlayer", return_value=failing_player), patch(
        "service.xbmc.log"
    ) as mock_log, patch(
        "service._HOME_WINDOW"
    ):
        service.main()

    # Both tick() calls raised; service still reached the "stopped" log.
    assert failing_player.tick.call_count == 2
    log_lines = [c.args[0] for c in mock_log.call_args_list]
    assert any("Service stopped" in line for line in log_lines)
    # First failure gets its own ERROR line; second logs the streak counter.
    assert any("Unhandled exception in player.tick()" in line for line in log_lines)
    assert any("still failing" in line and "streak=2" in line for line in log_lines)


def test_service_main_loop_resets_failure_streak_on_good_tick():
    """One transient tick failure shouldn't remain in the 'streak' log
    forever. After a successful tick, the streak counter resets so the
    NEXT failure logs a full trace again."""
    from unittest.mock import MagicMock

    import service

    mock_monitor = MagicMock()
    mock_monitor.abortRequested.side_effect = [False, False, False, True]
    mock_monitor.waitForAbort.return_value = False

    tick_results = iter([RuntimeError("first"), None, RuntimeError("second")])

    def _tick():
        r = next(tick_results)
        if isinstance(r, Exception):
            raise r

    intermittent_player = MagicMock()
    intermittent_player.tick.side_effect = _tick

    mock_proxy = MagicMock()
    mock_proxy.port = 12345

    with patch("service.xbmc.Monitor", return_value=mock_monitor), patch(
        "service.StreamProxy", return_value=mock_proxy
    ), patch("service.NzbdavPlayer", return_value=intermittent_player), patch(
        "service.xbmc.log"
    ) as mock_log, patch(
        "service._HOME_WINDOW"
    ):
        service.main()

    log_lines = [c.args[0] for c in mock_log.call_args_list]
    # Two first-failure ERROR lines — one per streak (since the good
    # tick between them resets the counter).
    first_failure_lines = [
        line for line in log_lines if "Unhandled exception in player.tick()" in line
    ]
    assert len(first_failure_lines) == 2


def test_service_detects_dead_proxy_thread_and_restarts():
    """If the proxy's serve_forever thread dies (unhandled exception in
    socket accept loop, rare memory-pressure paths), every subsequent
    /prepare call from the plugin hangs on ECONNREFUSED with no log
    hint. main() polls proxy.is_alive() each tick and rebuilds the
    proxy when the thread drops."""
    from unittest.mock import MagicMock

    import service

    mock_monitor = MagicMock()
    mock_monitor.abortRequested.side_effect = [False, False, False, True]
    mock_monitor.waitForAbort.return_value = False

    # First proxy simulates a dead thread on iteration #2.
    dead_proxy = MagicMock()
    dead_proxy.is_alive.side_effect = [True, False, False]
    dead_proxy.port = 11111

    live_proxy = MagicMock()
    live_proxy.is_alive.return_value = True
    live_proxy.port = 22222

    proxies = iter([dead_proxy, live_proxy])

    mock_player = MagicMock()
    mock_home = MagicMock()

    with patch("service.xbmc.Monitor", return_value=mock_monitor), patch(
        "service.StreamProxy", side_effect=lambda: next(proxies)
    ), patch("service.NzbdavPlayer", return_value=mock_player), patch(
        "service._HOME_WINDOW", mock_home
    ), patch(
        "service.xbmc.log"
    ) as mock_log:
        service.main()

    log_lines = [c.args[0] for c in mock_log.call_args_list]
    assert any("Stream proxy thread is dead" in line for line in log_lines)
    assert any("Stream proxy restarted on port 22222" in line for line in log_lines)
    # The replacement proxy's port landed on the IPC property so plugin
    # calls know where to find the new listener.
    port_set_calls = [
        c
        for c in mock_home.setProperty.call_args_list
        if c.args[0] == "nzbdav.proxy_port"
    ]
    assert any(c.args[1] == "22222" for c in port_set_calls)
    # Player's proxy reference was updated so stop-callbacks go to the
    # live proxy.
    assert mock_player._proxy is live_proxy


def test_service_logs_when_proxy_restart_fails():
    """If the replacement proxy can't start either (port still stuck,
    OS refusing bind), log the failure but keep the service loop alive
    so the user can fix the underlying issue without reinstalling."""
    from unittest.mock import MagicMock

    import service

    mock_monitor = MagicMock()
    mock_monitor.abortRequested.side_effect = [False, False, True]
    mock_monitor.waitForAbort.return_value = False

    dead_proxy = MagicMock()
    dead_proxy.is_alive.return_value = False
    dead_proxy.port = 11111

    replacement_proxy = MagicMock()
    replacement_proxy.start.side_effect = OSError("Address already in use")
    replacement_proxy.port = 0

    proxies = iter([dead_proxy, replacement_proxy])
    mock_home = MagicMock()

    with patch("service.xbmc.Monitor", return_value=mock_monitor), patch(
        "service.StreamProxy", side_effect=lambda: next(proxies)
    ), patch("service.NzbdavPlayer", return_value=MagicMock()), patch(
        "service._HOME_WINDOW", mock_home
    ), patch(
        "service.xbmc.log"
    ) as mock_log:
        service.main()

    log_lines = [c.args[0] for c in mock_log.call_args_list]
    assert any("Stream proxy restart failed" in line for line in log_lines)
    # The stale port property was cleared when restart failed.
    assert mock_home.clearProperty.called


def test_stream_proxy_is_alive_returns_false_before_start():
    """A freshly-constructed StreamProxy without start() called must
    report is_alive()==False so the service's health check doesn't
    mistake the initial idle state for a crash."""
    from resources.lib.stream_proxy import StreamProxy

    proxy = StreamProxy()
    assert proxy.is_alive() is False


def test_stream_proxy_is_alive_tracks_thread_liveness():
    """After start(), is_alive() reflects the underlying thread state.
    Join the thread and the helper should flip to False."""
    import time as _time

    from resources.lib.stream_proxy import StreamProxy

    proxy = StreamProxy()
    proxy.start()
    try:
        # Give the thread a cycle to actually start serving.
        _time.sleep(0.05)
        assert proxy.is_alive() is True
    finally:
        proxy.stop()
    # After stop(), the thread joined and is no longer alive.
    assert proxy.is_alive() is False
