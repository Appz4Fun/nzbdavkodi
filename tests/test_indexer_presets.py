# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from resources.lib.indexer_presets import (
    DIRECT_FALLBACK_HOSTS,
    DOGNZB_TVSEARCH_FALLBACK_HOSTS,
    get_preset,
    list_newznab_presets,
    slugify_preset_id,
)


def test_newznab_presets_include_hydra_defaults():
    presets = list_newznab_presets()
    names = [preset["name"] for preset in presets]

    assert "NZBGeek" in names
    assert "DogNZB" in names
    assert "Drunken Slug" in names
    assert "NZB Finder" in names
    assert "nzbplanet" in names
    assert "Torbox (Newznab)" in names


def test_newznab_presets_exclude_hydra_special_non_newznab_providers():
    names = [preset["name"] for preset in list_newznab_presets()]

    assert "Binsearch" not in names
    assert "NZBIndex" not in names
    assert "NZBKing.com" not in names
    assert "WtfNzb" not in names


def test_preset_ids_are_stable_and_sorted():
    presets = list_newznab_presets()
    ids = [preset["id"] for preset in presets]
    names = [preset["name"].lower() for preset in presets]

    assert len(ids) == len(set(ids))
    assert names == sorted(names)
    assert get_preset("nzbgeek")["api_url"] == "https://api.nzbgeek.info"
    assert get_preset("missing") is None


def test_slugify_preset_id_uses_ascii_lowercase():
    assert slugify_preset_id("Drunken Slug") == "drunken_slug"
    assert slugify_preset_id("Torbox (Newznab)") == "torbox_newznab"


def test_hydra_host_fallback_hints_are_available():
    assert "nzbgeek" in DIRECT_FALLBACK_HOSTS
    assert "dognzb" in DIRECT_FALLBACK_HOSTS
    assert "nzbplanet" in DIRECT_FALLBACK_HOSTS
    assert "dognzb" in DOGNZB_TVSEARCH_FALLBACK_HOSTS
