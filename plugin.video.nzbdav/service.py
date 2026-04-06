# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZB-DAV background service — monitors playback and retries on failure."""

import os
import sys

# Add resources/lib/ to sys.path (same as addon.py)
addon_dir = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(addon_dir, "resources", "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402
from resources.lib.http_util import notify as _notify  # noqa: E402

# Window property keys for IPC between plugin and service
_PROP_STREAM_URL = "nzbdav.stream_url"
_PROP_STREAM_TITLE = "nzbdav.stream_title"
_PROP_ACTIVE = "nzbdav.active"

_HOME_WINDOW = xbmcgui.Window(10000)


class NzbdavPlayer(xbmc.Player):
    """Persistent player that monitors NZB-DAV streams for failures."""

    def __init__(self):
        super().__init__()
        self._active = False
        self._stream_url = ""
        self._title = ""
        self._last_position = 0.0
        self._retry_count = 0
        self._playback_error = False
        self._playback_ended = False
        self._monitor = xbmc.Monitor()

    def _read_settings(self):
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
            self._active = True
            self._retry_count = 0
            self._playback_error = False
            self._playback_ended = False
            self._last_position = 0.0
            # Clear the signal so we don't re-trigger
            _HOME_WINDOW.clearProperty(_PROP_ACTIVE)
            xbmc.log(
                "NZB-DAV: Service monitoring stream '{}'".format(self._title),
                xbmc.LOGINFO,
            )

    def onAVStarted(self):
        if self._active:
            self._retry_count = 0
            self._playback_error = False
            xbmc.log(
                "NZB-DAV: Playback started for '{}'".format(self._title),
                xbmc.LOGINFO,
            )

    def onPlayBackStopped(self):
        if self._active:
            xbmc.log(
                "NZB-DAV: Playback stopped for '{}'".format(self._title),
                xbmc.LOGINFO,
            )
            self._active = False
            self._playback_ended = True

    def onPlayBackEnded(self):
        if self._active:
            xbmc.log(
                "NZB-DAV: Playback completed for '{}'".format(self._title),
                xbmc.LOGINFO,
            )
            self._active = False
            self._playback_ended = True

    def onPlayBackError(self):
        if self._active:
            self._playback_error = True
            xbmc.log(
                "NZB-DAV: Playback error for '{}' (retry {})".format(
                    self._title, self._retry_count
                ),
                xbmc.LOGERROR,
            )

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
        self._playback_error = False

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
            "Stream interrupted. Reconnecting... ({}/{})".format(
                self._retry_count, max_retries
            ),
        )

        if self._monitor.waitForAbort(retry_delay):
            return False

        li = xbmcgui.ListItem(path=self._stream_url)
        li.setProperty("StartOffset", str(self._last_position))
        self.play(self._stream_url, li)

        # Wait for playback to start or fail (10s timeout)
        for _ in range(20):
            if self._playback_ended or self._playback_error:
                break
            try:
                if self.isPlaying():
                    return True
            except Exception:
                pass
            if self._monitor.waitForAbort(0.5):
                return False

        return not self._playback_error

    def tick(self):
        """Called each service loop iteration. Handle retries if needed."""
        self._check_active()

        if not self._active:
            return

        self._save_position()

        if not self._playback_error:
            return

        enabled, max_retries, retry_delay = self._read_settings()
        if not enabled:
            self._active = False
            return

        if self._retry_count >= max_retries:
            xbmc.log(
                "NZB-DAV: Max retries ({}) reached for '{}'".format(
                    max_retries, self._title
                ),
                xbmc.LOGERROR,
            )
            _notify(
                "NZB-DAV",
                "Stream failed after {} retries".format(max_retries),
            )
            self._active = False
            return

        if not self._retry_playback(max_retries, retry_delay):
            self._active = False


def main():
    """Service entry point — runs for the lifetime of Kodi."""
    monitor = xbmc.Monitor()
    player = NzbdavPlayer()

    xbmc.log("NZB-DAV: Service started", xbmc.LOGINFO)

    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break
        player.tick()

    xbmc.log("NZB-DAV: Service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    main()
