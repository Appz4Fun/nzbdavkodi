# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Placeholder entry point for the direct indexer manager."""

from resources.lib.i18n import addon_name, string


def open_indexer_manager():
    """Open the direct indexer manager placeholder."""
    import xbmcgui

    xbmcgui.Dialog().notification(addon_name(), string(30195), time=3000)
