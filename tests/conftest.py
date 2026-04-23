# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Mock Kodi modules for testing outside of Kodi."""

import sys
from unittest.mock import MagicMock

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
