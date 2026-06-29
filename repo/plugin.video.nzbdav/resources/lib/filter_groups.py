# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Release-group master list and the configure-groups multiselect dialog.

Split out of :mod:`resources.lib.filter` to keep that module under the
file-size guard. ``configure_groups_dialog`` is invoked from router.py via
``resources.lib.filter.configure_groups_dialog``; ``filter`` re-exports both
names so that import keeps resolving. This module must not import ``filter``
(no cycle).
"""

import xbmcaddon
import xbmcgui

# ---------------------------------------------------------------------------
# Known release groups — master list for multiselect dialogs
# ---------------------------------------------------------------------------

ALL_RELEASE_GROUPS = [
    "4KDVS",
    "Amen",
    "AOC",
    "APEX",
    "B0MBARDiERS",
    "Ben The Men",
    "BHDstudio",
    "BiTOR",
    "BYNDR",
    "c0kE",
    "CiNEPHiLES",
    "CM",
    "CMRG",
    "DDR",
    "DEFLATE",
    "DirtyHippie",
    "DiscoD",
    "DON",
    "DreamHD",
    "DVSUX",
    "EDITH",
    "ENDSTATiON",
    "ETHEL",
    "EVO",
    "FETiSH",
    "FGT",
    "FLUX",
    "FraMeSToR",
    "FrameStor",
    "FW",
    "GalaxyRG",
    "GLHF",
    "Gungnir",
    "hallowed",
    "HDS",
    "HDT",
    "HHWEB",
    "HiDt",
    "HONE",
    "HSaber",
    "IAMABLE",
    "j3rico",
    "KC",
    "Kira",
    "Kitsune",
    "KOGi",
    "KTR",
    "LEGi0N",
    "MainFrame",
    "MgB",
    "MIXED",
    "mkv",
    "mp4",
    "MZABI",
    "NAHOM",
    "Narcos",
    "NBQ",
    "NHTFS",
    "NOGRP",
    "NTb",
    "NUXWIO",
    "P2P",
    "playWEB",
    "PSA",
    "R3MiX",
    "Ralphy",
    "RARBG",
    "SDH",
    "Sensei",
    "SESKAPiLE",
    "SEV",
    "SiC",
    "SMURF",
    "SPHD",
    "SPx",
    "STRiKES",
    "SuccessfulCrab",
    "SUPPLY",
    "SURCODE",
    "SWTYBLZ",
    "TERMiNAL",
    "TEPES",
    "TheBiscuitMan",
    "ToonsHub",
    "TrollUHD",
    "TW",
    "VSEX",
    "W4NK3R",
    "WADU",
    "WiKi",
    "WRB",
    "XEBEC",
    "XXX",
    "ZAX",
]


def _csv_setting(addon, key):
    """Read a comma-separated setting into a stripped list."""
    val = addon.getSetting(key).strip()
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


def configure_groups_dialog(setting_id, title, default_set):
    """Show a multiselect dialog for release group configuration.

    Args:
        setting_id: Kodi setting ID to read/write (comma-separated string).
        title: Dialog title string.
        default_set: Set of group names to preselect when setting is empty.
    """
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    current = _csv_setting(addon, setting_id)

    if current:
        selected = set(current)
    else:
        selected = set(default_set)

    preselect = [i for i, g in enumerate(ALL_RELEASE_GROUPS) if g in selected]

    dialog = xbmcgui.Dialog()
    result = dialog.multiselect(title, ALL_RELEASE_GROUPS, preselect=preselect)

    if result is None:
        return

    chosen = [ALL_RELEASE_GROUPS[i] for i in result]
    addon.setSetting(setting_id, ",".join(chosen))
