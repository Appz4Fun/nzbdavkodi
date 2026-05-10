# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZBHydra2-derived Newznab preset catalog."""

import re

DIRECT_FALLBACK_HOSTS = ("dognzb", "nzbplanet", "nzbgeek", "6box")
DOGNZB_TVSEARCH_FALLBACK_HOSTS = ("dognzb",)
RATE_LIMITED_CAPS_HOSTS = ("nzb.su", "nzb.life")

_PRESETS = (
    ("abnzb", "abNZB", "https://abnzb.com/"),
    ("althub", "altHUB", "https://api.althub.co.za"),
    ("animetosho_newznab", "Animetosho (Newznab)", "https://feed.animetosho.org"),
    ("digital_carnage", "Digital Carnage", "https://digitalcarnage.info"),
    ("dognzb", "DogNZB", "https://api.dognzb.cr"),
    ("drunken_slug", "Drunken Slug", "https://api.drunkenslug.com"),
    ("fastnzb", "FastNZB", "https://fastnzb.com"),
    ("lulunzb", "LuluNZB", "https://lulunzb.com"),
    ("miatrix", "miatrix", "https://www.miatrix.com"),
    ("nzb_finder", "NZB Finder", "https://nzbfinder.ws"),
    ("nzbcat", "NZBCat", "https://nzb.cat"),
    ("nzb_life", "nzb.life", "https://api.nzb.life"),
    ("nzbgeek", "NZBGeek", "https://api.nzbgeek.info"),
    ("nzbndx", "NzbNdx", "https://www.nzbndx.com"),
    ("nzbnoob", "NzBNooB", "https://www.nzbnoob.com"),
    ("nzbnation", "NzbNation", "http://www.nzbnation.com/"),
    ("nzbplanet", "nzbplanet", "https://nzbplanet.net"),
    ("omgwtfnzbs", "omgwtfnzbs", "https://api.omgwtfnzbs.org"),
    ("scenenzbs", "SceneNZBs", "https://scenenzbs.com"),
    ("spotweb", "spotweb.com", "https://spotweb.me"),
    ("tabula_rasa", "Tabula-Rasa", "https://www.tabula-rasa.pw/api/v1/"),
    ("torbox_newznab", "Torbox (Newznab)", "https://search-api.torbox.app/newznab"),
)


def slugify_preset_id(name):
    value = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").lower()
    return re.sub(r"_+", "_", value)


def _preset(indexer_id, name, api_url):
    return {"id": indexer_id, "name": name, "api_url": api_url}


def list_newznab_presets():
    return [
        _preset(indexer_id, name, api_url)
        for indexer_id, name, api_url in sorted(
            _PRESETS,
            key=lambda item: item[1].lower(),
        )
    ]


def get_preset(indexer_id):
    for preset in list_newznab_presets():
        if preset["id"] == indexer_id:
            return preset
    return None


def host_contains(host, needles):
    lowered = str(host or "").lower()
    return any(needle in lowered for needle in needles)
