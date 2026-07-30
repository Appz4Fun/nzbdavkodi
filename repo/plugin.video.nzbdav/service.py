# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZB-DAV background service — hosts stream proxy and monitors playback."""

import faulthandler
import os
import sys
import threading
from enum import Enum

# Same faulthandler hook as addon.py, but the service runs continuously so
# this catches crashes during the long-lived stream-proxy thread too.
try:
    _fh = open("/tmp/nzbdav-faulthandler-service.log", "a", buffering=1)  # nosec B108
    faulthandler.enable(file=_fh, all_threads=True)
except OSError:
    pass

# Add resources/lib/ to sys.path (same as addon.py)
addon_dir = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(addon_dir, "resources", "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402
from resources.lib import resume_store  # noqa: E402
from resources.lib.http_util import notify as _notify  # noqa: E402
from resources.lib.stream_proxy import StreamProxy  # noqa: E402

# Proxy-lifecycle helpers live in a sibling module to keep this entry module
# under the file-size budget. The thin wrappers below preserve the original
# signatures and bind the proxy class / Home window from THIS module's
# namespace, so ``@patch("service.StreamProxy")`` and
# ``@patch("service._HOME_WINDOW")`` reach the implementations and there is no
# import cycle back into ``service``. (E402: this import follows sys.path setup.)
from service_proxy import _restart_dead_proxy as _restart_dead_proxy_impl  # noqa: E402
from service_proxy import _shutdown_proxy as _shutdown_proxy_impl  # noqa: E402
from service_proxy import _start_proxy as _start_proxy_impl  # noqa: E402

# Window property keys for IPC between plugin and service
_PROP_STREAM_URL = "nzbdav.stream_url"
_PROP_RESUME_KEY = "nzbdav.resume_key"
_PROP_RESUME_OFFSET = "nzbdav.resume_offset"
_PROP_STREAM_TITLE = "nzbdav.stream_title"
_PROP_ACTIVE = "nzbdav.active"
# Persistent live-playback liveness flag (distinct from the consume-once
# ``_PROP_ACTIVE`` handoff signal): set while the service monitors a stream and
# cleared only on stop/end, so the plugin-process fallback submit worker can
# observe cross-process whether playback is still live during its standby wait.
_PROP_PLAYING = "nzbdav.playing"
_PROP_PROXY_PORT = "nzbdav.proxy_port"
_PROP_PROXY_TOKEN = "nzbdav.proxy_token"  # nosec B105 — settings key, not a secret

_HOME_WINDOW = xbmcgui.Window(10000)
_PLAYER_RUNTIME_ERRORS = (
    AttributeError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


def _coerce_resume_offset(value):
    """Return a non-negative resume offset from window-property IPC."""
    try:
        return max(0.0, float(value or 0.0))
    except (TypeError, ValueError):
        return 0.0


# Bounds for retry settings. Without clamps, a typo of "99999" would
# wedge the player monitor for ~28 hours between attempts (delay) or
# never give up retrying a permanently-broken stream (max_retries).
# The accepted ranges below cover every realistic recovery scenario:
#   * max_retries: 0 disables retries entirely; 10 already takes >1 minute
#     of cumulative backoff with the default 5 s delay.
#   * retry_delay: 1 s is the smallest poll the service tick can honor;
#     300 s (5 min) gives flaky-network recovery plenty of headroom.
_STREAM_MAX_RETRIES_MIN = 0
_STREAM_MAX_RETRIES_MAX = 10
_STREAM_RETRY_DELAY_MIN = 1
_STREAM_RETRY_DELAY_MAX = 300


def _clamp_int_setting(setting_id, value, lo, hi):
    """Clamp an integer setting and log when user input was out of range.

    Mirrors the helper in ``stream_proxy.py`` / ``resolver.py`` — kept
    as a private duplicate (rather than imported) so ``service.py``
    stays importable in the early service-startup phase before
    ``resources.lib`` is fully wired into ``sys.path`` for some
    embedded Kodi builds.
    """
    clamped = value
    if value < lo:
        clamped = lo
    elif value > hi:
        clamped = hi
    if clamped != value:
        xbmc.log(
            "NZB-DAV: Setting {}={} out of range [{}..{}]; clamping to {}".format(
                setting_id, value, lo, hi, clamped
            ),
            xbmc.LOGWARNING,
        )
    return clamped


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
        # ``_state_lock`` serializes read-then-write transitions on the
        # state fields below. Kodi player callbacks (onPlayBackStopped /
        # Ended / Error / AVStarted / Seek) execute on internal Kodi
        # threads while ``tick()`` runs on the service main thread; the
        # GIL makes individual attribute writes atomic but does NOT
        # protect "if state == X: state = Y" sequences. Without the lock
        # a callback could flip state between tick's check and tick's
        # write, e.g. user-stop racing into a retry attempt.
        # ``RLock`` so tick → _retry_playback → onAVStarted on the same
        # thread doesn't self-deadlock. Closes TODO.md §H.2-H16.
        self._state_lock = threading.RLock()
        self._state = PlaybackState.IDLE
        self._stream_url = ""
        self._resume_key = ""
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
        """Read retry settings from addon config (clamped to safe bounds).

        Both ``stream_max_retries`` and ``stream_retry_delay`` are
        unconstrained ``type="number"`` fields in settings.xml; without
        the clamps applied below an unclamped 99999 retry_delay would
        freeze the monitor loop for ~28 h between retries. The clamp
        helper logs the original value at LOGWARNING so users debugging
        "why didn't my stream retry?" see the cause in kodi.log.
        """
        addon = xbmcaddon.Addon("plugin.video.nzbdav")
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
        max_retries = _clamp_int_setting(
            "stream_max_retries",
            max_retries,
            _STREAM_MAX_RETRIES_MIN,
            _STREAM_MAX_RETRIES_MAX,
        )
        retry_delay = _clamp_int_setting(
            "stream_retry_delay",
            retry_delay,
            _STREAM_RETRY_DELAY_MIN,
            _STREAM_RETRY_DELAY_MAX,
        )
        return enabled, max_retries, retry_delay

    @staticmethod
    def _clear_props(props):
        """Best-effort clear of the given window properties."""
        for prop in props:
            try:
                _HOME_WINDOW.clearProperty(prop)
            except _PLAYER_RUNTIME_ERRORS:
                pass

    def _check_active(self):
        """Check if the plugin signaled a new stream via window properties."""
        import time

        active = _HOME_WINDOW.getProperty(_PROP_ACTIVE)
        if active != "true":
            return
        with self._state_lock:
            self._stream_url = _HOME_WINDOW.getProperty(_PROP_STREAM_URL)
            self._resume_key = _HOME_WINDOW.getProperty(_PROP_RESUME_KEY)
            self._last_position = _coerce_resume_offset(
                _HOME_WINDOW.getProperty(_PROP_RESUME_OFFSET)
            )
            self._title = _HOME_WINDOW.getProperty(_PROP_STREAM_TITLE)
            self._state = PlaybackState.MONITORING
            self._retry_count = 0
            self._av_started = False
            # ``time.monotonic()`` rather than ``time.time()`` so an NTP
            # step that lands during the 30 s startup grace can't trip a
            # false "playback never started" notification. Compared
            # against ``time.monotonic()`` in tick() below.
            # TODO.md §H.2-M35.
            self._play_time = time.monotonic()
            title = self._title
        # Clear all three properties — not just ACTIVE — so a future
        # second resolver call that fails before re-writing them
        # (network blip, _play_via_proxy crash) doesn't leak the prior
        # session's URL/title into the next monitor cycle. The values we
        # need are now snapshotted onto self.* fields. TODO.md §H.2-L29.
        self._clear_props(
            (
                _PROP_ACTIVE,
                _PROP_STREAM_URL,
                _PROP_RESUME_KEY,
                _PROP_RESUME_OFFSET,
                _PROP_STREAM_TITLE,
            )
        )
        # Raise the persistent liveness flag now that we are monitoring this
        # stream. Unlike ``_PROP_ACTIVE`` (consumed above), this stays set until
        # onPlayBackStopped/Ended clears it, giving the plugin-process fallback
        # submit worker a reliable cross-process "still playing" signal.
        try:
            _HOME_WINDOW.setProperty(_PROP_PLAYING, "true")
        except _PLAYER_RUNTIME_ERRORS:
            pass
        xbmc.log(
            "NZB-DAV: Service monitoring stream '{}'".format(title),
            xbmc.LOGINFO,
        )

    def onAVStarted(self):
        """Reset retry state when playback begins successfully."""
        with self._state_lock:
            if self._state not in (PlaybackState.MONITORING, PlaybackState.ERROR):
                return
            self._retry_count = 0
            self._av_started = True
            self._state = PlaybackState.MONITORING
            title = self._title
        xbmc.log(
            "NZB-DAV: Playback started for '{}'".format(title),
            xbmc.LOGINFO,
        )

    def _clear_stream_properties(self):
        """Erase the IPC window properties for this stream.

        Without this, ``nzbdav.stream_url`` and ``nzbdav.stream_title``
        linger across sessions. A second play whose plugin call fails
        before writing fresh properties would cause the service to pick
        up the previous session's URL/title if ``nzbdav.active="true"``
        is ever re-set by a stale/racing writer.

        ``nzbdav.playing`` (the persistent liveness flag) is cleared here too,
        on stop/end, so that "playback inactive" is observable cross-process:
        the in-process ``state["stop"]`` event the fallback submit worker waits
        on lives in the resolver/plugin process and can't be set from this
        service process. Clearing this shared window property lets that worker
        abort its prewarm wait when the user stops/ends playback during the
        standby-submit window. ``nzbdav.active`` is cleared as a belt-and-braces
        measure (it is normally already consumed by ``_check_active``).
        """
        self._clear_props(
            (
                _PROP_PLAYING,
                _PROP_ACTIVE,
                _PROP_STREAM_URL,
                _PROP_RESUME_KEY,
                _PROP_RESUME_OFFSET,
                _PROP_STREAM_TITLE,
            )
        )

    def _enter_idle_and_clear(self):
        """Transition to IDLE and clear the IPC properties (incl. liveness).

        Used by tick()'s terminal failure paths (never-started, retries
        disabled, max retries reached, retry relaunch failed). Like the
        onPlayBackStopped/Ended callbacks, this clears ``nzbdav.playing`` so the
        plugin-process fallback submit worker observes the session as dead and
        aborts its standby wait. ERROR is NOT terminal -- a retry that recovers
        keeps the flag set so backups still arrive into the recovered playback;
        only these end-of-the-line transitions clear it.
        """
        with self._state_lock:
            self._state = PlaybackState.IDLE
        self._clear_stream_properties()

    def _save_stable_resume(self, resume_key, position, av_started):
        """Persist the last position under the original source stream identity."""
        if not resume_key or not av_started or position <= 0.0:
            return
        duration = None
        try:
            duration = self.getTotalTime()
        except _PLAYER_RUNTIME_ERRORS:
            pass
        try:
            resume_store.save_resume(resume_key, position, duration=duration)
        except _PLAYER_RUNTIME_ERRORS:
            pass

    @staticmethod
    def _clear_stable_resume(resume_key):
        if not resume_key:
            return
        try:
            resume_store.clear_resume(resume_key)
        except _PLAYER_RUNTIME_ERRORS:
            pass

    def onPlayBackStopped(self):
        """Mark stream inactive when user stops playback."""
        with self._state_lock:
            if self._state == PlaybackState.IDLE:
                return
            self._state = PlaybackState.IDLE
            title = self._title
            resume_key = self._resume_key
            position = self._last_position
            av_started = self._av_started
        # Cleanup outside the lock — `_cleanup_proxy_session` calls
        # `proxy.clear_sessions()` which kills ffmpeg processes; that
        # must not run while a Kodi callback thread holds the lock or
        # the service tick will block waiting for ffmpeg to exit.
        xbmc.log(
            "NZB-DAV: Playback stopped for '{}'".format(title),
            xbmc.LOGINFO,
        )
        self._save_stable_resume(resume_key, position, av_started)
        self._cleanup_proxy_session()
        self._clear_stream_properties()

    def onPlayBackEnded(self):
        """Mark stream inactive when playback finishes naturally."""
        with self._state_lock:
            if self._state == PlaybackState.IDLE:
                return
            self._state = PlaybackState.IDLE
            title = self._title
            resume_key = self._resume_key
        xbmc.log(
            "NZB-DAV: Playback completed for '{}'".format(title),
            xbmc.LOGINFO,
        )
        self._clear_stable_resume(resume_key)
        self._cleanup_proxy_session()
        self._clear_stream_properties()

    def onPlayBackError(self):
        """Transition to ERROR state. Dialogs are shown from tick().

        Kodi player callbacks run on internal threads — showing a modal dialog
        here could deadlock or freeze the UI. So we only set the state flag
        and let tick() handle user notification on the service loop thread.
        """
        with self._state_lock:
            if self._state != PlaybackState.MONITORING:
                return
            self._state = PlaybackState.ERROR
            title = self._title
            retry_count = self._retry_count
        xbmc.log(
            "NZB-DAV: Playback error for '{}' (retry {})".format(title, retry_count),
            xbmc.LOGERROR,
        )

    def onPlayBackSeek(self, time, seek_offset):
        """Capture the new seek target immediately for retry resume.

        A seek can fail before the 1 Hz service tick gets a chance to refresh
        ``_last_position`` via ``getTime()``. Without this callback the retry
        path falls back to the older saved position and appears to "jump
        backwards" after a failed seek.

        Kodi passes ``time`` and ``seek_offset`` in **milliseconds**, while
        every other position in this class (``getTime()``, ``_last_position``,
        ``_save_position``, the ``StartOffset`` handed to a retry) is in
        seconds. Converting here keeps a single unit throughout; without it a
        seek stored a value 1000x too large and the retry then tried to resume
        far past the end of the file, which failed instantly and looped.
        """
        with self._state_lock:
            if self._state not in (PlaybackState.MONITORING, PlaybackState.ERROR):
                return
            try:
                self._last_position = max(0.0, float(time) / 1000.0)
            except (TypeError, ValueError):
                pos_failed = True
            else:
                pos_failed = False
            title = self._title
            position = self._last_position
        if pos_failed:
            self._save_position()
            return
        xbmc.log(
            "NZB-DAV: Playback seek for '{}' -> {:.0f}s (offset={:.0f}s)".format(
                title,
                position,
                float(seek_offset) / 1000.0,
            ),
            xbmc.LOGINFO,
        )

    def _save_position(self):
        """Save current playback position for resume on retry."""
        try:
            if self.isPlaying():
                position = self.getTime()
            else:
                return
        except _PLAYER_RUNTIME_ERRORS:
            return
        with self._state_lock:
            self._last_position = position

    def _retry_playback(self, max_retries, retry_delay):
        """Attempt to resume playback from last known position."""
        with self._state_lock:
            self._retry_count += 1
            self._state = PlaybackState.MONITORING
            title = self._title
            stream_url = self._stream_url
            position = self._last_position
            retry_count = self._retry_count

        xbmc.log(
            "NZB-DAV: Retrying '{}' from {:.0f}s ({}/{})".format(
                title,
                position,
                retry_count,
                max_retries,
            ),
            xbmc.LOGINFO,
        )
        _notify(
            "NZB-DAV",
            "Reconnecting ({}/{})...".format(retry_count, max_retries),
            5000,
        )

        if self._monitor.waitForAbort(retry_delay):
            return False

        li = xbmcgui.ListItem(path=stream_url)
        li.setProperty("StartOffset", str(position))
        self.play(stream_url, li)

        return self._await_playback_start()

    def _await_playback_start(self):
        """Poll until the relaunched stream starts, fails, or abort (10s).

        Returns True if playback is confirmed live, False on abort. When the
        poll window elapses without a terminal transition the final state
        decides: anything other than ERROR is treated as a successful retry.
        """
        # Wait for playback to start or fail (10s timeout)
        for _ in range(20):
            with self._state_lock:
                state = self._state
            if state in (PlaybackState.IDLE, PlaybackState.ERROR):
                break
            try:
                if self.isPlaying():
                    return True
            except _PLAYER_RUNTIME_ERRORS:
                pass
            if self._monitor.waitForAbort(0.5):
                return False

        with self._state_lock:
            return self._state != PlaybackState.ERROR

    def tick(self):
        """Called each service loop iteration. Handle retries if needed.

        State reads are all done under ``_state_lock`` so a Kodi
        callback (player thread) can't flip state mid-tick. The lock
        is released around long-running calls (``_save_position``,
        ``_retry_playback``) — those re-acquire internally as needed.
        Closes TODO.md §H.2-H16.
        """
        self._check_active()

        with self._state_lock:
            state = self._state
        if state == PlaybackState.IDLE:
            return

        if self._handle_startup_grace():
            return

        self._save_position()

        with self._state_lock:
            state = self._state
            retry_count = self._retry_count
            title = self._title
        if state != PlaybackState.ERROR:
            return

        self._handle_error_retry(retry_count, title)

    def _handle_startup_grace(self):
        """Catch playback that never started; return True if handled terminally.

        Detects playback that never started (stream error, auth failure, etc).
        Returns True only when the never-started timeout fired and the session
        was torn down (so tick() stops processing this iteration); else False.

        The 30 s timeout exceeds worst-case fmp4 HLS startup latency (producer
        spawn + ffmpeg analyzeduration + demuxer/decoder init can hit 4-6 s on
        the test box); the old 5 s threshold tripped before onAVStarted fired.
        Notification uses the fire-and-forget ``_notify`` toast rather than a
        modal dialog (which would wedge this ~1 Hz tick thread); the error is
        still logged at LOGERROR. Closes TODO.md §H.2-H8.
        """
        import time

        with self._state_lock:
            in_startup_grace = (
                self._state == PlaybackState.MONITORING and not self._av_started
            )
            play_time = self._play_time
            title = self._title

        if not in_startup_grace:
            return False
        elapsed = time.monotonic() - play_time
        if elapsed <= 30 or self.isPlaying():
            return False

        xbmc.log(
            "NZB-DAV: Playback never started for '{}' after {:.0f}s".format(
                title, elapsed
            ),
            xbmc.LOGERROR,
        )
        from resources.lib.i18n import addon_name as _addon_name
        from resources.lib.i18n import string as _s

        _notify(_addon_name(), _s(30121), 8000)
        xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
        self._cleanup_proxy_session()
        self._enter_idle_and_clear()
        return True

    def _handle_error_retry(self, retry_count, title):
        """Drive the ERROR-state retry decision (retry, give up, or relaunch)."""
        enabled, max_retries, retry_delay = self._read_settings()
        if not enabled:
            from resources.lib.i18n import addon_name as _addon_name
            from resources.lib.i18n import string as _s

            _notify(_addon_name(), _s(30115), 8000)
            self._enter_idle_and_clear()
            return

        if retry_count >= max_retries:
            xbmc.log(
                "NZB-DAV: Max retries ({}) reached for '{}'".format(max_retries, title),
                xbmc.LOGERROR,
            )
            from resources.lib.i18n import addon_name as _addon_name
            from resources.lib.i18n import fmt as _f

            _notify(_addon_name(), _f(30116, max_retries), 8000)
            self._enter_idle_and_clear()
            return

        if not self._retry_playback(max_retries, retry_delay):
            self._enter_idle_and_clear()


def check_cache_warning(state):
    """Compatibility no-op for the retired cache warning.

    Large non-MP4 pass-through is now the default even when Kodi cache=0 is
    absent, so the old warning has no state to mutate and no notification to
    show. ``state`` is accepted only to keep existing imports and service-loop
    wiring harmless.
    """
    return None


def _clear_stale_ipc_properties():
    """Drop nzbdav.* IPC window properties left over from a prior service.

    Window 10000 (Home) properties survive across the Kodi restart that
    kills the service, so a stale ``nzbdav.active="true"`` would cause this
    service's first tick to immediately enter MONITORING with the prior
    session's stream metadata. Clearing them on entry starts from a clean
    slate. TODO.md §H.2-M34.
    """
    for stale_prop in (
        _PROP_ACTIVE,
        _PROP_PLAYING,
        _PROP_STREAM_URL,
        _PROP_RESUME_KEY,
        _PROP_RESUME_OFFSET,
        _PROP_STREAM_TITLE,
        _PROP_PROXY_TOKEN,
    ):
        try:
            _HOME_WINDOW.clearProperty(stale_prop)
        except Exception:  # noqa: BLE001 — best-effort, never block startup
            pass


def _run_tick(player, consecutive_failures):
    """Run one player.tick(), absorbing crashes; return the failure streak.

    A crash inside tick() used to kill the whole service, silently breaking
    all future streams until Kodi restart. Absorb it so the loop keeps
    running. The full trace is rate-limited to the first failure of a streak;
    subsequent failures log a single line with the streak counter so a
    chronic bug is visible without flooding the log.
    """
    try:
        player.tick()
        return 0
    except Exception as e:  # pylint: disable=broad-except
        consecutive_failures += 1
        if consecutive_failures == 1:
            xbmc.log(
                "NZB-DAV: Unhandled exception in player.tick(): {} "
                "(reason=tick_exception)".format(e),
                xbmc.LOGERROR,
            )
        else:
            xbmc.log(
                "NZB-DAV: player.tick() still failing "
                "(streak={}, latest={})".format(consecutive_failures, e),
                xbmc.LOGERROR,
            )
        return consecutive_failures


# Thin wrappers preserving the original proxy-helper signatures. They bind the
# Home window and ``StreamProxy`` class from this module's namespace at call
# time so test patches on ``service._HOME_WINDOW`` / ``service.StreamProxy``
# flow into ``service_proxy``'s injected implementations.
def _start_proxy(monitor):
    """Start the stream proxy; return it, or None if startup failed."""
    return _start_proxy_impl(_HOME_WINDOW, StreamProxy, monitor)


def _restart_dead_proxy(proxy, player):
    """Rebuild the proxy when its daemon thread has died; return the proxy."""
    return _restart_dead_proxy_impl(_HOME_WINDOW, StreamProxy, proxy, player)


def _shutdown_proxy(proxy):
    """Stop the proxy and clear its port/token, guarding the stop()."""
    _shutdown_proxy_impl(_HOME_WINDOW, proxy)


def main():
    """Service entry point — runs for the lifetime of Kodi."""
    monitor = xbmc.Monitor()

    _clear_stale_ipc_properties()

    proxy = _start_proxy(monitor)
    if proxy is None:
        return

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

    # Retained for the no-op check_cache_warning compatibility hook.
    cache_warn_state = {
        "last_mode": None,
    }

    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break

        proxy = _restart_dead_proxy(proxy, player)

        try:
            check_cache_warning(cache_warn_state)
        except Exception as e:  # pylint: disable=broad-except
            # Never let a settings-read glitch take down the service loop.
            xbmc.log(
                "NZB-DAV: cache warning check failed: {}".format(e),
                xbmc.LOGERROR,
            )

        consecutive_tick_failures = _run_tick(player, consecutive_tick_failures)

    _shutdown_proxy(proxy)
    xbmc.log("NZB-DAV: Service stopped", xbmc.LOGINFO)


if __name__ == "__main__":
    main()
