# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Mock Kodi modules for testing outside of Kodi."""

import contextlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Mock all xbmc* modules that Kodi provides at runtime
for module_name in ["xbmc", "xbmcgui", "xbmcplugin", "xbmcaddon", "xbmcvfs"]:
    sys.modules[module_name] = MagicMock()


# Install realistic defaults on the xbmcaddon MagicMock. Without these,
# ``xbmcaddon.Addon().getSetting("anything")`` returns a MagicMock —
# which makes ``getSetting("enabled").lower() == "true"`` produce
# MagicMock comparisons (always False), hiding bugs in production
# paths that depend on real string-valued settings.
#
# Keeping ``Addon`` itself as a MagicMock preserves every test pattern
# that relies on ``.return_value`` / ``.return_value.getSetting.return_value``
# (used in test_nzbdav_api, test_stream_proxy, test_router, test_i18n).
# We only seed the leaf methods with str defaults so unstubbed code
# sees real-looking values.
def _install_addon_defaults():
    xbmcaddon_mod = sys.modules["xbmcaddon"]
    # Addon() always returns the same MagicMock (stable identity), and
    # per-leaf defaults apply until a specific test overrides them.
    addon_instance = xbmcaddon_mod.Addon.return_value
    addon_instance.getSetting.return_value = ""
    addon_instance.getLocalizedString.return_value = ""

    _fake_info = {
        "id": "plugin.video.nzbdav",
        "name": "NZB-DAV",
        "version": "0.0.0",
        "path": "",
        "profile": "",
    }

    def _fake_addon_info(key, *_args, **_kwargs):
        return _fake_info.get(key, "")

    addon_instance.getAddonInfo.side_effect = _fake_addon_info


_install_addon_defaults()


def _install_monitor_defaults():
    """Seed `xbmc.Monitor().waitForAbort()` so it returns ``False``.

    Without this, the default MagicMock return is itself a MagicMock,
    which is truthy when evaluated as a bool. Production code uses
    ``if xbmc.Monitor().waitForAbort(0.25): return None`` to detect
    Kodi shutdown — a truthy MagicMock causes every loop to think Kodi
    is shutting down and bail out on iteration 1, breaking unrelated
    HLS/poll/probe tests. Tests that need to simulate an abort can
    override the leaf return per-test.
    """
    xbmc_mod = sys.modules["xbmc"]
    # Side-effect (not just return_value) so the mock actually waits for
    # the requested duration. Production code reads `waitForAbort(0.05)`
    # as "sleep up to 50 ms", and tests like the HlsProducer prepare
    # argv-rejection window depend on that timing window for ffmpeg
    # crashes to be detected at the right poll cycle. Without the real
    # sleep, the loop iterates microseconds-fast and the argv window
    # closes before the test's mocked ffmpeg has a chance to "die" at
    # the expected iteration.
    import time as _real_time

    def _waitForAbort(timeout=0.0):
        if timeout and timeout > 0:
            _real_time.sleep(timeout)
        return False

    xbmc_mod.Monitor.return_value.waitForAbort.side_effect = _waitForAbort
    xbmc_mod.Monitor.return_value.abortRequested.return_value = False


_install_monitor_defaults()

# xbmc.Player must be a real class so that subclassing works correctly
# (MagicMock subclasses swallow attribute assignments in __init__)


class _FakePlayer:
    """Minimal stand-in for xbmc.Player with a mutable isPlaying state.

    ``isPlaying()`` previously hard-coded ``False``, which meant any
    test exercising a playback-transition path (wait-for-player,
    post-play cleanup, ``_clear_kodi_playback_state``) couldn't move
    the fake past the "not yet playing" state. The setter
    ``_set_is_playing(True/False)`` lets individual tests simulate
    transitions without monkeypatching the class attribute."""

    def __init__(self):
        self._is_playing = False
        self._time = 0.0

    def isPlaying(self):
        return self._is_playing

    def isPlayingVideo(self):
        # resolver.py:239 calls isPlayingVideo() during the "wait for
        # Kodi to actually start the video" handshake. _FakePlayer
        # previously only exposed isPlaying(), so any test that hit
        # _FakePlayer directly (instead of patching xbmc.Player) would
        # AttributeError. Mirror isPlaying for parity. TODO.md §H.3.
        return self._is_playing

    def getTime(self):
        return self._time

    def play(self, item="", listitem=None, windowed=False, startpos=-1):
        # Kept as a no-op by default. Changing play() to auto-transition
        # into the playing state would break any existing test that
        # asserts isPlaying()==False after construction — the original
        # behavior we don't want to silently regress. Tests that need
        # the transition call ``_set_is_playing(True)`` explicitly.
        pass

    def _set_is_playing(self, value):
        self._is_playing = bool(value)

    def _set_time(self, value):
        self._time = float(value)


sys.modules["xbmc"].Player = _FakePlayer

REPO_ROOT = Path(__file__).resolve().parents[1]

# Add repo/plugin.video.nzbdav to the path so imports work
sys.path.insert(0, str(REPO_ROOT / "repo" / "plugin.video.nzbdav"))
# Add resources/lib so PTT's internal imports resolve
sys.path.insert(
    0,
    str(REPO_ROOT / "repo" / "plugin.video.nzbdav" / "resources" / "lib"),
)


@pytest.fixture(autouse=True)
def _reap_readahead_threads():
    """Stop any read-ahead prefetch daemon a test left running.

    ``prepare_stream()`` spawns a per-session ``nzbdav-readahead`` daemon
    thread (default on via ``readahead_buffer_mb``). In production
    ``_cleanup_session`` stops it and ``waitForAbort`` really blocks; in the
    test harness neither happens, so without this reaper one such daemon
    leaks per ``prepare_stream`` test and they accumulate across the suite,
    adding timer/GIL load that flakes timing-sensitive tests (e.g. the
    byte-0 prefetch 0.08s deadline). After each test, signal abort (so the
    loop's ``waitForAbort`` returns True and it exits) and join briefly,
    then restore the monitor defaults. Cheap no-op when none were spawned.
    """
    yield
    import threading

    leftover = [
        t
        for t in threading.enumerate()
        if t.name == "nzbdav-readahead" and t.is_alive()
    ]
    if not leftover:
        return
    monitor = sys.modules["xbmc"].Monitor.return_value
    saved_side = monitor.waitForAbort.side_effect
    saved_ret = monitor.waitForAbort.return_value
    monitor.waitForAbort.side_effect = lambda timeout=0.0: True
    try:
        for thread in leftover:
            thread.join(timeout=2)
    finally:
        monitor.waitForAbort.side_effect = saved_side
        monitor.waitForAbort.return_value = saved_ret


@pytest.fixture(autouse=True)
def _suppress_readahead_daemon(request):
    """Skip spawning the read-ahead prefetch daemon in tests that don't target it.

    ``prepare_stream`` spawns a per-session ``nzbdav-readahead`` daemon (read-ahead
    is on by default). Because ``Monitor.waitForAbort`` REALLY sleeps in this
    harness, that daemon backs off on real 0.25 s / 1.0 s sleeps: the autouse reaper
    above then pays ~1.5 s per ``prepare_stream`` test joining it (≈30 s across the
    suite), and any daemon that outlives the reaper's join races sibling tests'
    patched ``urlopen`` — the root of the nondeterministic
    ``test_prevalidated_fallback_reuses_current_probe`` full-suite flake. No general
    test asserts the daemon spawned (``_serve_proxy`` never spawns it; only
    ``prepare_stream`` does), so no-op the spawn by default.

    Exemptions keep the REAL daemon running:
      * ``real_readahead`` — unit tests that exercise the spawn directly.
      * ``functional`` / ``integration`` / ``extreme`` — the live/dev-box suites
        (``just functional-test`` etc.) exist to catch real prefetch/cutover races
        against actual playback; users get read-ahead by default, so suppressing it
        there would hide exactly what those suites validate. They are excluded from
        the default ``just test`` run, so this does not affect the fast unit suite's
        speed-up or de-flaking.
    """
    keep_real_daemon = ("real_readahead", "functional", "integration", "extreme")
    if any(request.node.get_closest_marker(marker) for marker in keep_real_daemon):
        yield
        return
    from resources.lib import stream_proxy

    with patch.object(
        stream_proxy.StreamProxy,
        "_start_readahead_prefetch",
        lambda self, ctx: None,
    ):
        yield


@pytest.fixture
def resolver_mocks():
    """Patch the dependencies that nearly every resolver test needs.

    Before this fixture, ``tests/test_resolver_errors.py`` stacked
    6-13 ``@patch`` decorators per test function (plus the fiddly
    argument-order that comes with decorator patching). Every test
    also re-built the same ``DialogProgress`` / ``Monitor`` / time
    scaffolding. This fixture consolidates the common set and
    exposes the mocks as a namespace so tests can customize return
    values without re-threading decorator arguments.

    Defaults mirror the v0.6.20 lesson (pin ``time.time()`` to 0.0
    so elapsed stays well under the download timeout) and the
    1s-poll / 60s-timeout values used by almost every test.
    """
    with contextlib.ExitStack() as stack:
        xbmc_mock = stack.enter_context(patch("resources.lib.resolver.xbmc"))
        gui_mock = stack.enter_context(patch("resources.lib.resolver.xbmcgui"))
        plugin_mock = stack.enter_context(patch("resources.lib.resolver.xbmcplugin"))
        submit_mock = stack.enter_context(patch("resources.lib.resolver.submit_nzb"))
        poll_mock = stack.enter_context(
            patch("resources.lib.resolver._get_poll_settings")
        )
        status_mock = stack.enter_context(
            patch("resources.lib.resolver.get_job_status")
        )
        history_mock = stack.enter_context(
            patch("resources.lib.resolver.get_job_history")
        )
        time_mock = stack.enter_context(patch("resources.lib.resolver.time"))
        probe_mock = stack.enter_context(
            patch("resources.lib.resolver.probe_webdav_reachable")
        )

        dialog = MagicMock()
        dialog.iscanceled.return_value = False
        gui_mock.DialogProgress.return_value = dialog

        monitor = MagicMock()
        monitor.waitForAbort.return_value = False
        xbmc_mock.Monitor.return_value = monitor

        poll_mock.return_value = (1, 60)
        time_mock.time.return_value = 0.0
        # resolver._poll_until_ready uses time.monotonic for elapsed-time
        # tracking. Default to 0.0 so tests that don't override it stay
        # below the timeout; tests that need a stale clock override
        # `resolver_mocks.time.monotonic.side_effect`.
        time_mock.monotonic.return_value = 0.0

        yield SimpleNamespace(
            xbmc=xbmc_mock,
            gui=gui_mock,
            plugin=plugin_mock,
            submit=submit_mock,
            poll=poll_mock,
            status=status_mock,
            history=history_mock,
            time=time_mock,
            probe=probe_mock,
            dialog=dialog,
            monitor=monitor,
        )
