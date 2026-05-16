# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""JSON storage for dynamic indexers and provider caps."""

import json
import os

import xbmc

INDEXERS_FILENAME = "indexers.json"
PROVIDER_CAPS_FILENAME = "provider_caps.json"
ADDON_PROFILE = "special://profile/addon_data/plugin.video.nzbdav/"
STORE_VERSION = 1

_READ_ERRORS = (OSError, TypeError, ValueError)


def _profile_path():
    try:
        import xbmcvfs

        return xbmcvfs.translatePath(ADDON_PROFILE)
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        return ""


def default_indexers_path():
    return os.path.join(_profile_path(), INDEXERS_FILENAME)


def default_provider_caps_path():
    return os.path.join(_profile_path(), PROVIDER_CAPS_FILENAME)


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _list(value):
    return value if isinstance(value, list) else []


def _bool_setting(value, default=False):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off", ""):
            return False
    return default if value is None else bool(value)


def normalize_caps(caps):
    if not isinstance(caps, dict):
        return {}

    normalized = dict(caps)
    if "search_types" in normalized:
        normalized["search_types"] = _list(normalized.get("search_types"))
    if "supported_params" in normalized:
        normalized["supported_params"] = (
            normalized["supported_params"]
            if isinstance(normalized.get("supported_params"), dict)
            else {}
        )
    if "categories" in normalized:
        normalized["categories"] = _list(normalized.get("categories"))
    return normalized


def normalize_indexer(item):
    item = item if isinstance(item, dict) else {}
    deleted = bool(item.get("deleted"))
    if deleted:
        enabled = False
    elif "enabled" in item:
        enabled = _bool_setting(item.get("enabled"))
    else:
        enabled = True
    normalized = {
        "id": _text(item.get("id")),
        "preset_id": _text(item.get("preset_id")),
        "name": _text(item.get("name")),
        "api_url": _text(item.get("api_url")),
        "api_key": _text(item.get("api_key")),
        "enabled": enabled,
        "caps": normalize_caps(item.get("caps")),
    }
    if item.get("deleted"):
        normalized["deleted"] = True
    return normalized


def _read_json(path, empty_value, warning):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except _READ_ERRORS:
        xbmc.log(warning, xbmc.LOGWARNING)
        return empty_value


def _ensure_parent_dir(path):
    directory = os.path.dirname(path)
    if directory and not os.path.exists(directory):
        os.makedirs(directory)


def load_indexers(path=None):
    path = path or default_indexers_path()
    data = _read_json(path, {}, "NZB-DAV: Failed to read indexers JSON")
    indexers = data.get("indexers", []) if isinstance(data, dict) else []
    if not isinstance(indexers, list):
        return []
    return [normalize_indexer(item) for item in indexers]


def save_indexers(indexers, path=None):
    path = path or default_indexers_path()
    _ensure_parent_dir(path)
    payload = {
        "version": STORE_VERSION,
        "indexers": [normalize_indexer(item) for item in indexers],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)


def _normalize_provider_caps(data):
    providers = data if isinstance(data, dict) else {}
    normalized = {}
    for key, value in providers.items():
        provider_id = _text(key)
        if not provider_id or not isinstance(value, dict):
            continue
        normalized[provider_id] = {
            "base_url": _text(value.get("base_url")),
            "checked_at": _text(value.get("checked_at")),
            "caps": normalize_caps(value.get("caps")),
        }
    return normalized


def load_provider_caps(path=None):
    path = path or default_provider_caps_path()
    data = _read_json(path, {}, "NZB-DAV: Failed to read provider caps JSON")
    if not isinstance(data, dict):
        return {}
    return _normalize_provider_caps(data.get("providers", {}))


def save_provider_caps(providers, path=None):
    path = path or default_provider_caps_path()
    _ensure_parent_dir(path)
    payload = {
        "version": STORE_VERSION,
        "providers": _normalize_provider_caps(providers),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
