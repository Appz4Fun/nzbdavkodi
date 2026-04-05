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
    """Minimal stand-in for xbmc.Player that supports normal subclassing."""

    def __init__(self):
        pass

    def isPlaying(self):
        return False

    def getTime(self):
        return 0.0

    def play(self, item="", listitem=None, windowed=False, startpos=-1):
        pass


sys.modules["xbmc"].Player = _FakePlayer

# Add plugin.video.nzbdav to the path so imports work
sys.path.insert(0, "plugin.video.nzbdav")
# Add resources/lib so PTT's internal imports resolve
sys.path.insert(0, "plugin.video.nzbdav/resources/lib")
