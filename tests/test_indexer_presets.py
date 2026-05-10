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


# ---------------------------------------------------------------------------
# Dev-S5 Fix #1 / Fix #2 — preset URL hygiene
# ---------------------------------------------------------------------------


def test_all_preset_urls_use_https_scheme():
    """Every preset must use https — http leaks the apikey on caps fetch."""
    presets = list_newznab_presets()
    bad = [p for p in presets if not p["api_url"].lower().startswith("https://")]
    assert (
        bad == []
    ), "Non-https preset URLs leak apikey over the wire on caps fetch: " "{}".format(
        [(p["id"], p["api_url"]) for p in bad]
    )


def test_nzbnation_preset_uses_https():
    """Regression: nzbnation previously shipped http://, leaking apikey."""
    preset = get_preset("nzbnation")
    if preset is not None:
        assert preset["api_url"].lower().startswith("https://")


def test_tabula_rasa_preset_does_not_carry_api_path():
    """Regression: trailing /api/v1/ path produced /api/v1/api?t=caps and 404'd.

    fetch_caps appends the Newznab /api?t=caps suffix itself, so preset
    URLs must be a bare host (no extra /api[/vN] tail).
    """
    preset = get_preset("tabula_rasa")
    if preset is not None:
        url = preset["api_url"].rstrip("/").lower()
        # Bare host: no `/api` (any version) suffix.
        assert not url.endswith("/api"), url
        assert "/api/v" not in url, url
