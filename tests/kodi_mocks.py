# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Kodi module mocks shared by tests/ and tests-extensive/ suites.

Call ``install_kodi_mocks()`` at the very top of a conftest.py — before any
addon module is imported — to inject MagicMock stubs for every xbmc* module
that Kodi provides at runtime, seed realistic defaults, and add the addon
sources to ``sys.path``.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock


def install_kodi_mocks() -> None:
    """Install Kodi module mocks and add addon modules to sys.path.

    Must be called before any ``resources.lib.*`` import.  Calling it
    multiple times is safe (re-assigns the same structure).
    """

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
    _install_addon_defaults()

    # Seed `xbmc.Monitor().waitForAbort()` so it returns ``False``.
    #
    # Without this, the default MagicMock return is itself a MagicMock,
    # which is truthy when evaluated as a bool. Production code uses
    # ``if xbmc.Monitor().waitForAbort(0.25): return None`` to detect
    # Kodi shutdown — a truthy MagicMock causes every loop to think Kodi
    # is shutting down and bail out on iteration 1, breaking unrelated
    # HLS/poll/probe tests. Tests that need to simulate an abort can
    # override the leaf return per-test.
    _install_monitor_defaults()

    # xbmc.Player must be a real class so that subclassing works correctly
    # (MagicMock subclasses swallow attribute assignments in __init__)
    sys.modules["xbmc"].Player = _FakePlayer

    REPO_ROOT = Path(__file__).resolve().parents[1]

    # Add repo/plugin.video.nzbdav to the path so imports work
    sys.path.insert(0, str(REPO_ROOT / "repo" / "plugin.video.nzbdav"))
    # Add resources/lib so PTT's internal imports resolve
    sys.path.insert(
        0,
        str(REPO_ROOT / "repo" / "plugin.video.nzbdav" / "resources" / "lib"),
    )


def _install_addon_defaults() -> None:
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


def _install_monitor_defaults() -> None:
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
