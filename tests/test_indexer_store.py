# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json

from resources.lib.indexer_store import (
    load_indexers,
    load_provider_caps,
    normalize_indexer,
    save_indexers,
    save_provider_caps,
)


def test_normalize_indexer_never_returns_none_values():
    item = normalize_indexer(
        {
            "id": None,
            "preset_id": None,
            "name": None,
            "api_url": None,
            "api_key": None,
            "enabled": None,
            "caps": None,
        }
    )

    assert item == {
        "id": "",
        "preset_id": "",
        "name": "",
        "api_url": "",
        "api_key": "",
        "enabled": False,
        "caps": {},
    }


def test_normalize_indexer_normalizes_caps_collections():
    item = normalize_indexer(
        {
            "caps": {
                "search_types": None,
                "supported_params": None,
                "categories": None,
            }
        }
    )

    assert item["caps"] == {
        "search_types": [],
        "supported_params": {},
        "categories": [],
    }


def test_load_indexers_missing_file_returns_empty(tmp_path):
    assert load_indexers(str(tmp_path / "missing.json")) == []


def test_load_indexers_corrupt_json_returns_empty(tmp_path):
    path = tmp_path / "indexers.json"
    path.write_text("{bad", encoding="utf-8")

    assert load_indexers(str(path)) == []


def test_save_and_load_indexers_round_trip(tmp_path):
    path = tmp_path / "indexers.json"

    save_indexers(
        [
            {
                "id": "nzbgeek",
                "preset_id": "nzbgeek",
                "name": "NZBGeek",
                "api_url": "https://api.nzbgeek.info",
                "api_key": "secret",
                "enabled": True,
                "caps": {"search_types": ["movie"]},
            }
        ],
        str(path),
    )

    loaded = load_indexers(str(path))
    assert loaded[0]["id"] == "nzbgeek"
    assert loaded[0]["enabled"] is True
    assert loaded[0]["caps"]["search_types"] == ["movie"]


def test_load_provider_caps_missing_or_corrupt_returns_empty(tmp_path):
    missing = tmp_path / "missing.json"
    corrupt = tmp_path / "provider_caps.json"
    corrupt.write_text("{bad", encoding="utf-8")

    assert not load_provider_caps(str(missing))
    assert not load_provider_caps(str(corrupt))


def test_save_and_load_provider_caps_round_trip(tmp_path):
    path = tmp_path / "provider_caps.json"
    caps = {
        "nzbhydra2": {
            "base_url": "http://hydra:5076",
            "checked_at": "2026-05-10T00:00:00Z",
            "caps": {"search_types": ["search", "movie"]},
        }
    }

    save_provider_caps(caps, str(path))

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert load_provider_caps(str(path)) == caps
