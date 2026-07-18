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
from xml.etree import ElementTree

import xbmcvfs


def _parse_local_xml_root(path):
    """Parse Kodi profile XML through the shared XXE-safe XML helper.

    ``safe_fromstring`` rejects entity declarations (XXE / billion-laughs)
    before the parser can act on them; entity-free XML parses normally.
    """
    from resources.lib.xml_safety import safe_fromstring

    with open(path, "rb") as fh:
        xml_bytes = fh.read()
    return safe_fromstring(xml_bytes)


def has_cache_memorysize_zero():
    """Return True iff ``<cache><memorysize>0</memorysize></cache>`` is set.

    Any failure path (missing file, unreadable, malformed XML, unexpected
    structure, non-zero or non-integer value) returns False — callers
    treat False as "the user has not opted in" and gate the passthrough
    mode accordingly.
    """
    # The docstring above promises "any failure path returns False"; that
    # only holds if translatePath itself can't escape this function. In
    # tests / CLI use xbmcvfs is a MagicMock and translatePath should be
    # safe, but in a partly-initialized Kodi environment translatePath
    # has been observed raising RuntimeError. Treat any exception the
    # same as "missing file" → False. See TODO.md §H.3.
    try:
        path = xbmcvfs.translatePath("special://profile/advancedsettings.xml")
    except Exception:  # pylint: disable=broad-except
        return False
    if not os.path.isfile(path):
        return False
    try:
        root = _parse_local_xml_root(path)
    except (ElementTree.ParseError, OSError, ValueError):
        return False
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
