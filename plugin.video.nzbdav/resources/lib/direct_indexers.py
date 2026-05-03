# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Direct Newznab-compatible indexer provider."""

import xbmc
import xbmcaddon


PRESET_INDEXERS = (
    ("nzblife", "NZB.life / NZB.su", "https://api.nzb.su/api"),
    ("nzbgeek", "NZBGeek", "https://api.nzbgeek.info/api"),
    ("nzbfinder", "NZBFinder", "https://nzbfinder.ws/api"),
    ("drunkenslug", "DrunkenSlug", "https://drunkenslug.com/api"),
    ("nzbplanet", "NZBPlanet", "https://api.nzbplanet.net/api"),
    ("dognzb", "DOGnzb", "https://api.dognzb.cr/api"),
)

_CUSTOM_SLOT_IDS = ("custom1", "custom2", "custom3")


def _setting_enabled(addon, setting_id):
    return addon.getSetting(setting_id).lower() == "true"


def _setting_text(addon, setting_id):
    value = addon.getSetting(setting_id)
    return value.strip() if isinstance(value, str) else ""


def _configured_preset(addon, indexer_id, label, default_url):
    if not _setting_enabled(addon, "direct_indexer_{}_enabled".format(indexer_id)):
        return None
    url = _setting_text(addon, "direct_indexer_{}_url".format(indexer_id)) or default_url
    api_key = _setting_text(addon, "direct_indexer_{}_api_key".format(indexer_id))
    if not url or not api_key:
        xbmc.log(
            "NZB-DAV: Direct indexer {} enabled without URL/API key; skipping".format(
                label
            ),
            xbmc.LOGDEBUG,
        )
        return None
    return {"id": indexer_id, "label": label, "api_url": url, "api_key": api_key}


def _configured_custom(addon, slot_id):
    if not _setting_enabled(addon, "direct_indexer_{}_enabled".format(slot_id)):
        return None
    name = _setting_text(addon, "direct_indexer_{}_name".format(slot_id))
    url = _setting_text(addon, "direct_indexer_{}_url".format(slot_id))
    api_key = _setting_text(addon, "direct_indexer_{}_api_key".format(slot_id))
    if not name or not url or not api_key:
        xbmc.log(
            "NZB-DAV: Direct indexer {} missing name, URL, or API key; skipping".format(
                slot_id
            ),
            xbmc.LOGDEBUG,
        )
        return None
    return {"id": slot_id, "label": name, "api_url": url, "api_key": api_key}


def get_configured_indexers():
    """Return enabled direct indexers with complete URL and API key config."""
    addon = xbmcaddon.Addon()
    if not _setting_enabled(addon, "direct_indexers_enabled"):
        return []

    configured = []
    for indexer_id, label, default_url in PRESET_INDEXERS:
        item = _configured_preset(addon, indexer_id, label, default_url)
        if item:
            configured.append(item)

    for slot_id in _CUSTOM_SLOT_IDS:
        item = _configured_custom(addon, slot_id)
        if item:
            configured.append(item)

    return configured
