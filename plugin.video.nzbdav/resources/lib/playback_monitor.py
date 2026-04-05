"""Playback monitor for detecting and recovering from stream failures."""

import xbmc
import xbmcgui

from resources.lib.http_util import notify as _notify


class PlaybackMonitor(xbmc.Player):
    """Monitors playback and optionally retries on failure.

    Usage:
        monitor = PlaybackMonitor(stream_url, max_retries=3)
        monitor.start_monitoring()
        # Returns when playback completes, fails permanently, or user stops
    """

    def __init__(self, stream_url, title="", max_retries=3, retry_delay=5):
        super().__init__()
        self._stream_url = stream_url
        self._title = title
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._retry_count = 0
        self._playback_started = False
        self._playback_ended = False
        self._playback_error = False
        self._last_position = 0.0
        self._monitor = xbmc.Monitor()

    def onAVStarted(self):
        """Called when audio/video actually starts playing."""
        self._playback_started = True
        self._retry_count = 0  # Reset retries on successful start
        xbmc.log("NZB-DAV: Playback started for '{}'".format(self._title), xbmc.LOGINFO)

    def onPlayBackStopped(self):
        """Called when user stops playback."""
        self._playback_ended = True
        xbmc.log(
            "NZB-DAV: Playback stopped by user for '{}'".format(self._title),
            xbmc.LOGINFO,
        )

    def onPlayBackEnded(self):
        """Called when playback completes normally."""
        self._playback_ended = True
        xbmc.log(
            "NZB-DAV: Playback completed for '{}'".format(self._title), xbmc.LOGINFO
        )

    def onPlayBackError(self):
        """Called when playback fails."""
        self._playback_error = True
        xbmc.log(
            "NZB-DAV: Playback error for '{}' (retry {}/{})".format(
                self._title, self._retry_count, self._max_retries
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

    def _retry_playback(self):
        """Attempt to resume playback from last known position."""
        self._retry_count += 1
        self._playback_error = False
        self._playback_started = False

        xbmc.log(
            "NZB-DAV: Retrying playback for '{}' from {:.0f}s (attempt {}/{})".format(
                self._title,
                self._last_position,
                self._retry_count,
                self._max_retries,
            ),
            xbmc.LOGINFO,
        )
        _notify(
            "NZB-DAV",
            "Stream interrupted. Reconnecting... ({}/{})".format(
                self._retry_count, self._max_retries
            ),
        )

        # Wait before retrying
        if self._monitor.waitForAbort(self._retry_delay):
            return False  # Kodi shutting down

        # Replay from last position
        li = xbmcgui.ListItem(path=self._stream_url)
        li.setProperty("StartOffset", str(self._last_position))
        self.play(self._stream_url, li)

        # Wait for playback to start or fail
        for _ in range(20):  # 10 second timeout
            if self._playback_started or self._playback_error or self._playback_ended:
                break
            if self._monitor.waitForAbort(0.5):
                return False

        return self._playback_started

    def start_monitoring(self):
        """Monitor playback with auto-retry on failure.

        Returns:
            True if playback completed normally, False if failed permanently.
        """
        import xbmcaddon

        addon = xbmcaddon.Addon()
        auto_retry = addon.getSetting("stream_auto_retry").lower() == "true"

        if not auto_retry:
            return True  # No monitoring needed, let Kodi handle it

        xbmc.log(
            "NZB-DAV: Monitoring playback for '{}'".format(self._title), xbmc.LOGINFO
        )

        # Wait for initial playback to start
        for _ in range(60):  # 30 second timeout for initial start
            if self._playback_started or self._playback_error or self._playback_ended:
                break
            if self._monitor.waitForAbort(0.5):
                return False

        # Monitor loop
        while not self._playback_ended:
            if self._monitor.waitForAbort(1):
                return False  # Kodi shutting down

            # Save position periodically
            self._save_position()

            # Handle errors
            if self._playback_error:
                if self._retry_count >= self._max_retries:
                    xbmc.log(
                        "NZB-DAV: Max retries ({}) reached for '{}'".format(
                            self._max_retries, self._title
                        ),
                        xbmc.LOGERROR,
                    )
                    _notify(
                        "NZB-DAV",
                        "Stream failed after {} retries".format(self._max_retries),
                    )
                    return False

                if not self._retry_playback():
                    return False

        return True
