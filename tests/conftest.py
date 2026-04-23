# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Mock Kodi modules for testing outside of Kodi."""

import contextlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Mock all xbmc* modules that Kodi provides at runtime
for module_name in ["xbmc", "xbmcgui", "xbmcplugin", "xbmcaddon", "xbmcvfs"]:
    sys.modules[module_name] = MagicMock()

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

# Add plugin.video.nzbdav to the path so imports work
sys.path.insert(0, "plugin.video.nzbdav")
# Add resources/lib so PTT's internal imports resolve
sys.path.insert(0, "plugin.video.nzbdav/resources/lib")


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
