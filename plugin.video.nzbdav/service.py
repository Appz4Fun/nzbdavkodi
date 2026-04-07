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
from resources.lib.stream_proxy import StreamProxy  # noqa: E402

# Window property keys for IPC between plugin and service
_PROP_STREAM_URL = "nzbdav.stream_url"
_PROP_STREAM_TITLE = "nzbdav.stream_title"
_PROP_ACTIVE = "nzbdav.active"
_PROP_PROXY_PORT = "nzbdav.proxy_port"

_HOME_WINDOW = xbmcgui.Window(10000)


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

    This is the **primary** player monitor used in production.  It differs from
    :class:`resources.lib.playback_monitor.PlaybackMonitor` in two ways:

    * It is **long-lived** — one instance covers many play sessions rather than
      being created per-play.
    * It uses Kodi window properties for inter-process communication so that
      the short-lived plugin script can hand off monitoring to this service.

    ``PlaybackMonitor`` (in ``playback_monitor.py``) is a self-contained
    per-play alternative used when calling ``resolve_and_play()`` directly
    (i.e. without the service running).
    """

    def __init__(self):
        super().__init__()
        self._state = PlaybackState.IDLE
        self._stream_url = ""
        self._title = ""
        self._last_position = 0.0
        self._retry_count = 0
        self._monitor = xbmc.Monitor()

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
        active = _HOME_WINDOW.getProperty(_PROP_ACTIVE)
        if active == "true":
            self._stream_url = _HOME_WINDOW.getProperty(_PROP_STREAM_URL)
            self._title = _HOME_WINDOW.getProperty(_PROP_STREAM_TITLE)
            self._state = PlaybackState.MONITORING
            self._retry_count = 0
            self._last_position = 0.0
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
            self._state = PlaybackState.MONITORING
            xbmc.log(
                "NZB-DAV: Playback started for '{}'".format(self._title),
                xbmc.LOGINFO,
            )

    def onPlayBackStopped(self):
        """Mark stream inactive when user stops playback."""
        if self._state != PlaybackState.IDLE:
            xbmc.log(
                "NZB-DAV: Playback stopped for '{}'".format(self._title),
                xbmc.LOGINFO,
            )
            self._state = PlaybackState.IDLE

    def onPlayBackEnded(self):
        """Mark stream inactive when playback finishes naturally."""
        if self._state != PlaybackState.IDLE:
            xbmc.log(
                "NZB-DAV: Playback completed for '{}'".format(self._title),
                xbmc.LOGINFO,
            )
            self._state = PlaybackState.IDLE

    def onPlayBackError(self):
        """Transition to ERROR state for retry logic; notify if retries exhausted."""
        if self._state == PlaybackState.MONITORING:
            self._state = PlaybackState.ERROR
            xbmc.log(
                "NZB-DAV: Playback error for '{}' (retry {})".format(
                    self._title, self._retry_count
                ),
                xbmc.LOGERROR,
            )
            enabled, max_retries, _ = self._read_settings()
            if not enabled or self._retry_count >= max_retries:
                from resources.lib.i18n import string as _s

                _notify("NZB-DAV", _s(30115), 8000)

    def _save_position(self):
        """Save current playback position for resume on retry."""
        try:
            if self.isPlaying():
                self._last_position = self.getTime()
        except Exception:
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
            if self._state == PlaybackState.IDLE or self._state == PlaybackState.ERROR:
                break
            try:
                if self.isPlaying():
                    return True
            except Exception:
                pass
            if self._monitor.waitForAbort(0.5):
                return False

        return self._state != PlaybackState.ERROR

    def tick(self):
        """Called each service loop iteration. Handle retries if needed."""
        self._check_active()

        if self._state == PlaybackState.IDLE:
            return

        self._save_position()

        if self._state != PlaybackState.ERROR:
            return

        enabled, max_retries, retry_delay = self._read_settings()
        if not enabled:
            self._state = PlaybackState.IDLE
            return

        if self._retry_count >= max_retries:
            xbmc.log(
                "NZB-DAV: Max retries ({}) reached for '{}'".format(
                    max_retries, self._title
                ),
                xbmc.LOGERROR,
            )
            from resources.lib.i18n import fmt as _f

            _notify("NZB-DAV", _f(30116, max_retries), 8000)
            self._state = PlaybackState.IDLE
            return

        if not self._retry_playback(max_retries, retry_delay):
            self._state = PlaybackState.IDLE


def main():
    """Service entry point — runs for the lifetime of Kodi."""
    monitor = xbmc.Monitor()
    player = NzbdavPlayer()

    # Start the stream proxy in this long-lived service process.
    # Plugin scripts are short-lived — their daemon threads get killed
    # when Kodi's CPythonInvoker destroys the interpreter after the script
    # exits, so the proxy must live here instead.
    proxy = StreamProxy()
    proxy.start()
    _HOME_WINDOW.setProperty(_PROP_PROXY_PORT, str(proxy.port))
    xbmc.log(
        "NZB-DAV: Service started (proxy on port {})".format(proxy.port),
        xbmc.LOGINFO,
    )

    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break
        player.tick()

    proxy.stop()
    _HOME_WINDOW.clearProperty(_PROP_PROXY_PORT)
    xbmc.log("NZB-DAV: Service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    main()
