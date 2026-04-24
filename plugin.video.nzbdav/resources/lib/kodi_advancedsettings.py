# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Probe Kodi's user ``advancedsettings.xml`` for settings the addon depends on.

Kodi reads ``advancedsettings.xml`` from the user's profile directory at
startup; this module does not attempt to override or write to it. It only
reads, so the addon can detect whether the user has applied the
``<cache><memorysize>0</memorysize></cache>`` change that the
``force_remux_mode=passthrough`` path requires on 32-bit CoreELEC builds.

See TODO.md §D.2.3 (advancedsettings bypass) and §D.5.1 (Phase 1 plan).
"""

import os
from xml.etree import ElementTree as ET

import xbmcvfs


def has_cache_memorysize_zero():
    """Return True iff ``<cache><memorysize>0</memorysize></cache>`` is set.

    Any failure path (missing file, unreadable, malformed XML, unexpected
    structure, non-zero or non-integer value) returns False — callers
    treat False as "the user has not opted in" and gate the passthrough
    mode accordingly.
    """
    path = xbmcvfs.translatePath("special://profile/advancedsettings.xml")
    if not os.path.isfile(path):
        return False
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return False
    root = tree.getroot()
    cache = root.find("cache")
    if cache is None:
        return False
    memorysize = cache.find("memorysize")
    if memorysize is None or memorysize.text is None:
        return False
    try:
        return int(memorysize.text.strip()) == 0
    except ValueError:
        return False
