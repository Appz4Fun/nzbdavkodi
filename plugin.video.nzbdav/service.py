# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZB-DAV background service — hosts stream proxy and monitors playback."""

import os
import sys
from enum import Enum

# Add resources/lib/ to sys.path (same as addon.py)
addon_dir = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(addon_dir, "resources", "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402
from resources.lib.http_util import notify as _notify  # noqa: E402
from resources.lib.kodi_advancedsettings import (  # noqa: E402
    has_cache_memorysize_zero,
)
from resources.lib.stream_proxy import StreamProxy  # noqa: E402

# Window property keys for IPC between plugin and service
_PROP_STREAM_URL = "nzbdav.stream_url"
_PROP_STREAM_TITLE = "nzbdav.stream_title"
_PROP_ACTIVE = "nzbdav.active"
_PROP_PROXY_PORT = "nzbdav.proxy_port"

_HOME_WINDOW = xbmcgui.Window(10000)
_PLAYER_RUNTIME_ERRORS = (
    AttributeError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


class PlaybackState(Enum):
    """State machine for NzbdavPlayer.

    Transitions::

        IDLE -> MONITORING   (new stream signalled via window properties)
        MONITORING -> ERROR  (onPlayBackError fires)
        ERROR -> MONITORING  (retry succeeds — onAVStarted resets)
        ERROR -> IDLE        (max retries exceeded, or retries disabled)
        MONITORING -> IDLE   (onPlayBackStopped or onPlayBackEnded)
    """

    IDLE = "idle"  # No active stream; waiting for next play
    MONITORING = "monitoring"  # Stream is playing; watching for errors
    ERROR = "error"  # Error detected; retry in progress


class NzbdavPlayer(xbmc.Player):
    """Persistent playback monitor running inside the background service.

    Registered once when the service starts and kept alive for the entire Kodi
    session.  The plugin (resolver.py) signals a new stream by writing window
    properties; ``tick()`` is called every second from the service loop to check
    those properties and handle retries.
    """

    def __init__(self, proxy=None):
        super().__init__()
        self._state = PlaybackState.IDLE
        self._stream_url = ""
        self._title = ""
        self._last_position = 0.0
        self._retry_count = 0
        self._av_started = False
        self._play_time = 0.0
        self._monitor = xbmc.Monitor()
        self._proxy = proxy

    def _cleanup_proxy_session(self):
        """Kill any active proxy ffmpeg processes.

        Called from onPlayBackStopped / onPlayBackEnded so a clean stop
        immediately tears down the remux chain instead of leaving ffmpeg
        running until the next prepare_stream call discovers it.
        """
        if self._proxy is None:
            return
        try:
            self._proxy.clear_sessions()
        except _PLAYER_RUNTIME_ERRORS:
            pass

    @staticmethod
    def _read_settings():
        """Read retry settings from addon config."""
        addon = xbmcaddon.Addon()
        enabled = addon.getSetting("stream_auto_retry").lower() == "true"
        max_retries = 3
        retry_delay = 5
        try:
            max_retries = int(addon.getSetting("stream_max_retries"))
        except (ValueError, TypeError):
            pass
        try:
            retry_delay = int(addon.getSetting("stream_retry_delay"))
        except (ValueError, TypeError):
            pass
        return enabled, max_retries, retry_delay

    def _check_active(self):
        """Check if the plugin signaled a new stream via window properties."""
        import time

        active = _HOME_WINDOW.getProperty(_PROP_ACTIVE)
        if active == "true":
            self._stream_url = _HOME_WINDOW.getProperty(_PROP_STREAM_URL)
            self._title = _HOME_WINDOW.getProperty(_PROP_STREAM_TITLE)
            self._state = PlaybackState.MONITORING
            self._retry_count = 0
            self._last_position = 0.0
            self._av_started = False
            self._play_time = time.time()
            # Clear the signal so we don't re-trigger
            _HOME_WINDOW.clearProperty(_PROP_ACTIVE)
            xbmc.log(
                "NZB-DAV: Service monitoring stream '{}'".format(self._title),
                xbmc.LOGINFO,
            )

    def onAVStarted(self):
        """Reset retry state when playback begins successfully."""
        if self._state in (PlaybackState.MONITORING, PlaybackState.ERROR):
            self._retry_count = 0
            self._av_started = True
            self._state = PlaybackState.MONITORING
            xbmc.log(
                "NZB-DAV: Playback started for '{}'".format(self._title),
                xbmc.LOGINFO,
            )

    def _clear_stream_properties(self):
        """Erase the IPC window properties for this stream.

        Without this, ``nzbdav.stream_url`` and ``nzbdav.stream_title``
        linger across sessions. A second play whose plugin call fails
        before writing fresh properties would cause the service to pick
        up the previous session's URL/title if ``nzbdav.active="true"``
        is ever re-set by a stale/racing writer.
        """
        for prop in (_PROP_STREAM_URL, _PROP_STREAM_TITLE):
            try:
                _HOME_WINDOW.clearProperty(prop)
            except _PLAYER_RUNTIME_ERRORS:
                pass

    def onPlayBackStopped(self):
        """Mark stream inactive when user stops playback."""
        if self._state != PlaybackState.IDLE:
            xbmc.log(
                "NZB-DAV: Playback stopped for '{}'".format(self._title),
                xbmc.LOGINFO,
            )
            self._state = PlaybackState.IDLE
            self._cleanup_proxy_session()
            self._clear_stream_properties()

    def onPlayBackEnded(self):
        """Mark stream inactive when playback finishes naturally."""
        if self._state != PlaybackState.IDLE:
            xbmc.log(
                "NZB-DAV: Playback completed for '{}'".format(self._title),
                xbmc.LOGINFO,
            )
            self._state = PlaybackState.IDLE
            self._cleanup_proxy_session()
            self._clear_stream_properties()

    def onPlayBackError(self):
        """Transition to ERROR state. Dialogs are shown from tick().

        Kodi player callbacks run on internal threads — showing a modal dialog
        here could deadlock or freeze the UI. So we only set the state flag
        and let tick() handle user notification on the service loop thread.
        """
        if self._state == PlaybackState.MONITORING:
            self._state = PlaybackState.ERROR
            xbmc.log(
                "NZB-DAV: Playback error for '{}' (retry {})".format(
                    self._title, self._retry_count
                ),
                xbmc.LOGERROR,
            )

    def onPlayBackSeek(self, time, seek_offset):
        """Capture the new seek target immediately for retry resume.

        A seek can fail before the 1 Hz service tick gets a chance to refresh
        ``_last_position`` via ``getTime()``. Without this callback the retry
        path falls back to the older saved position and appears to "jump
        backwards" after a failed seek.
        """
        if self._state not in (PlaybackState.MONITORING, PlaybackState.ERROR):
            return
        try:
            self._last_position = max(0.0, float(time))
        except (TypeError, ValueError):
            self._save_position()
            return
        xbmc.log(
            "NZB-DAV: Playback seek for '{}' -> {:.0f}s (offset={:.0f}s)".format(
                self._title,
                self._last_position,
                float(seek_offset),
            ),
            xbmc.LOGINFO,
        )

    def _save_position(self):
        """Save current playback position for resume on retry."""
        try:
            if self.isPlaying():
                self._last_position = self.getTime()
        except _PLAYER_RUNTIME_ERRORS:
            pass

    def _retry_playback(self, max_retries, retry_delay):
        """Attempt to resume playback from last known position."""
        self._retry_count += 1
        self._state = PlaybackState.MONITORING

        xbmc.log(
            "NZB-DAV: Retrying '{}' from {:.0f}s ({}/{})".format(
                self._title,
                self._last_position,
                self._retry_count,
                max_retries,
            ),
            xbmc.LOGINFO,
        )
        _notify(
            "NZB-DAV",
            "Reconnecting ({}/{})...".format(self._retry_count, max_retries),
            5000,
        )

        if self._monitor.waitForAbort(retry_delay):
            return False

        li = xbmcgui.ListItem(path=self._stream_url)
        li.setProperty("StartOffset", str(self._last_position))
        self.play(self._stream_url, li)

        # Wait for playback to start or fail (10s timeout)
        for _ in range(20):
            if self._state in (PlaybackState.IDLE, PlaybackState.ERROR):
                break
            try:
                if self.isPlaying():
                    return True
            except _PLAYER_RUNTIME_ERRORS:
                pass
            if self._monitor.waitForAbort(0.5):
                return False

        return self._state != PlaybackState.ERROR

    def tick(self):
        """Called each service loop iteration. Handle retries if needed."""
        import time

        self._check_active()

        if self._state == PlaybackState.IDLE:
            return

        # Detect playback that never started (stream error, auth failure, etc.)
        # The timeout has to comfortably exceed the worst-case
        # proxy-side startup latency, which on the fmp4 HLS path is
        # roughly: HlsProducer spawn (~0.1 s) + prepare() early-exit
        # poll (~0.5 s) + ffmpeg analyzeduration (2 s, bumped from 0
        # to fix DTS/TrueHD AV sync) + first init.mp4 + first segment
        # write (~0.5 s) + Kodi HLS demuxer + decoder init (~1-3 s).
        # That can land at 4-6 s on the test box even when everything
        # is healthy. The previous 5 s threshold was tripping before
        # Kodi could fire onAVStarted on the new fmp4 path. 30 s
        # gives all the legitimate paths headroom while still
        # catching genuinely dead streams within a reasonable window.
        if self._state == PlaybackState.MONITORING and not self._av_started:
            elapsed = time.time() - self._play_time
            if elapsed > 30 and not self.isPlaying():
                xbmc.log(
                    "NZB-DAV: Playback never started for '{}' after {:.0f}s".format(
                        self._title, elapsed
                    ),
                    xbmc.LOGERROR,
                )
                from resources.lib.i18n import addon_name as _addon_name
                from resources.lib.i18n import string as _s

                xbmcgui.Dialog().ok(
                    _addon_name(),
                    _s(30121),
                )
                xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
                self._state = PlaybackState.IDLE
                return

        self._save_position()

        if self._state != PlaybackState.ERROR:
            return

        enabled, max_retries, retry_delay = self._read_settings()
        if not enabled:
            from resources.lib.i18n import addon_name as _addon_name
            from resources.lib.i18n import string as _s

            xbmcgui.Dialog().ok(_addon_name(), _s(30115))
            self._state = PlaybackState.IDLE
            return

        if self._retry_count >= max_retries:
            xbmc.log(
                "NZB-DAV: Max retries ({}) reached for '{}'".format(
                    max_retries, self._title
                ),
                xbmc.LOGERROR,
            )
            from resources.lib.i18n import addon_name as _addon_name
            from resources.lib.i18n import fmt as _f

            xbmcgui.Dialog().ok(_addon_name(), _f(30116, max_retries))
            self._state = PlaybackState.IDLE
            return

        if not self._retry_playback(max_retries, retry_delay):
            self._state = PlaybackState.IDLE


def check_cache_warning(state):
    """Surface a one-shot notification when the user selected
    ``force_remux_mode=passthrough`` but has not applied the
    ``<cache><memorysize>0</memorysize></cache>`` advancedsettings.xml
    change that the passthrough path requires on 32-bit Kodi.

    Called on every service tick. ``state`` is a dict the caller owns
    that retains the last-seen ``force_remux_mode`` between ticks; when
    the user changes the mode, ``cache_warning_shown`` is reset so a
    subsequent matroska→passthrough toggle re-fires the warning.

    No-op when mode != passthrough, when the warning has already been
    shown for the current mode, or when cache=0 is present.
    """
    addon = xbmcaddon.Addon()
    mode = addon.getSetting("force_remux_mode")

    if mode != state.get("last_mode"):
        state["last_mode"] = mode
        try:
            addon.setSetting("cache_warning_shown", "false")
        except _PLAYER_RUNTIME_ERRORS:
            pass

    if mode != "2":
        return
    if addon.getSetting("cache_warning_shown").lower() == "true":
        return
    if has_cache_memorysize_zero():
        return

    _notify(
        "NZB-DAV",
        "Passthrough mode: advancedsettings.xml cache=0 missing — "
        "falling back to matroska",
        10000,
    )
    try:
        addon.setSetting("cache_warning_shown", "true")
    except _PLAYER_RUNTIME_ERRORS:
        pass


def main():
    """Service entry point — runs for the lifetime of Kodi."""
    monitor = xbmc.Monitor()

    # Start the stream proxy in this long-lived service process.
    # Plugin scripts are short-lived — their daemon threads get killed
    # when Kodi's CPythonInvoker destroys the interpreter after the script
    # exits, so the proxy must live here instead.
    proxy = StreamProxy()
    try:
        proxy.start()
    except Exception as e:  # pylint: disable=broad-except
        # Socket bind failure (port in use), permission error, or any other
        # startup exception. Without this guard the service dies silently
        # and every plugin-side /prepare call hangs on "connection refused"
        # with no hint in the log. Surface it clearly, clear the port
        # property so plugin callers fall back quickly, and keep the service
        # alive so the user can fix the config and trigger a re-run.
        xbmc.log(
            "NZB-DAV: Service failed to start stream proxy: {}".format(e),
            xbmc.LOGERROR,
        )
        _HOME_WINDOW.clearProperty(_PROP_PROXY_PORT)
        # Idle-loop until Kodi shuts down so the service process stays alive;
        # otherwise Kodi keeps restarting us every few seconds and spams the
        # log with the same start failure.
        while not monitor.abortRequested():
            if monitor.waitForAbort(5):
                break
        return
    _HOME_WINDOW.setProperty(_PROP_PROXY_PORT, str(proxy.port))

    # Pass the proxy to the player so stop/end callbacks can tear down
    # active remux ffmpeg processes immediately instead of leaving them
    # running until the next prepare_stream call.
    player = NzbdavPlayer(proxy=proxy)
    xbmc.log(
        "NZB-DAV: Service started (proxy on port {})".format(proxy.port),
        xbmc.LOGINFO,
    )

    # Track consecutive tick failures so we can escalate a chronic bug
    # from "log once per tick" (flooding the log with the same trace) to
    # a one-shot "service is unhealthy, please file an issue" warning.
    consecutive_tick_failures = 0

    # State dict for check_cache_warning: retains the last-seen
    # force_remux_mode so a user toggle resets the "already notified"
    # flag and lets the warning re-fire.
    cache_warn_state = {
        "last_mode": xbmcaddon.Addon().getSetting("force_remux_mode"),
    }

    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break

        # Proxy health check: the HTTP server runs in a daemon thread.
        # If the serve_forever loop ever exits (unhandled exception,
        # socket error, rare memory-pressure path), every subsequent
        # /prepare call from the plugin side hangs on "connection
        # refused" with no log hint and no recovery. Detect the dead
        # thread and rebuild the proxy so streams keep working.
        if not proxy.is_alive():
            xbmc.log(
                "NZB-DAV: Stream proxy thread is dead; restarting "
                "(reason=proxy_thread_died)",
                xbmc.LOGERROR,
            )
            try:
                proxy.stop()
            except Exception as e:  # pylint: disable=broad-except
                # Logged at LOGWARNING (not LOGERROR) because we're about
                # to spawn a fresh proxy anyway — the stop failure is
                # diagnostic-only, not user-actionable. Closes §H.3.
                xbmc.log(
                    "NZB-DAV: proxy.stop() raised during restart "
                    "(continuing): {!r}".format(e),
                    xbmc.LOGWARNING,
                )
            proxy = StreamProxy()
            try:
                proxy.start()
            except Exception as e:  # pylint: disable=broad-except
                xbmc.log(
                    "NZB-DAV: Stream proxy restart failed: {} "
                    "(reason=proxy_restart_failed)".format(e),
                    xbmc.LOGERROR,
                )
                _HOME_WINDOW.clearProperty(_PROP_PROXY_PORT)
            else:
                _HOME_WINDOW.setProperty(_PROP_PROXY_PORT, str(proxy.port))
                # The player holds a reference to the old proxy for
                # cleanup calls from onPlayBackStopped; point it at the
                # new one so the next stop() fires on the live proxy.
                player._proxy = proxy  # pylint: disable=protected-access
                xbmc.log(
                    "NZB-DAV: Stream proxy restarted on port {}".format(proxy.port),
                    xbmc.LOGINFO,
                )

        try:
            check_cache_warning(cache_warn_state)
        except Exception as e:  # pylint: disable=broad-except
            # Never let a settings-read glitch take down the service loop.
            xbmc.log(
                "NZB-DAV: cache warning check failed: {}".format(e),
                xbmc.LOGERROR,
            )

        try:
            player.tick()
            consecutive_tick_failures = 0
        except Exception as e:  # pylint: disable=broad-except
            # A crash inside tick() used to kill the whole service,
            # silently breaking all future streams until Kodi restart.
            # Absorb it so the loop keeps running. Rate-limit the full
            # trace to the first failure of a streak; subsequent
            # failures log a single line with the streak counter so a
            # chronic bug is visible without flooding the log.
            consecutive_tick_failures += 1
            if consecutive_tick_failures == 1:
                xbmc.log(
                    "NZB-DAV: Unhandled exception in player.tick(): {} "
                    "(reason=tick_exception)".format(e),
                    xbmc.LOGERROR,
                )
            else:
                xbmc.log(
                    "NZB-DAV: player.tick() still failing "
                    "(streak={}, latest={})".format(consecutive_tick_failures, e),
                    xbmc.LOGERROR,
                )

    proxy.stop()
    _HOME_WINDOW.clearProperty(_PROP_PROXY_PORT)
    xbmc.log("NZB-DAV: Service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    main()
