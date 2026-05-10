# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Direct Newznab indexer manager actions."""

import xbmcaddon
import xbmcgui

from resources.lib.hydra import refresh_hydra_caps
from resources.lib.i18n import addon_name, string
from resources.lib.indexer_presets import list_newznab_presets, slugify_preset_id
from resources.lib.indexer_store import load_indexers, normalize_indexer, save_indexers
from resources.lib.newznab_caps import fetch_caps

_ADD_NEWZNAB = "Add Newznab Indexer"
_CUSTOM_NEWZNAB = "Custom Newznab"
_TEST = "Test"
_EDIT = "Edit"
_DELETE = "Delete"
_NOT_FOUND = "Indexer not found"
_KEEP_CURRENT = "<keep current>"


def _preset_id(preset):
    return str(preset.get("id") or "").strip()


def _indexer_id(indexer):
    return str(indexer.get("id") or "").strip()


def _indexer_label(indexer):
    name = str(indexer.get("name") or "").strip()
    return name or _indexer_id(indexer)


def _normalized_preset_entry(preset, api_key, caps):
    indexer_id = _preset_id(preset)
    return normalize_indexer(
        {
            "id": indexer_id,
            "preset_id": indexer_id,
            "name": preset.get("name"),
            "api_url": preset.get("api_url"),
            "api_key": api_key,
            "enabled": True,
            "caps": caps,
        }
    )


def _normalized_custom_entry(name, api_url, api_key, caps):
    return normalize_indexer(
        {
            "id": slugify_preset_id(name),
            "preset_id": "custom",
            "name": name,
            "api_url": api_url,
            "api_key": api_key,
            "enabled": True,
            "caps": caps,
        }
    )


def _replace_indexer(indexers, indexer):
    return [
        existing
        for existing in indexers
        if _indexer_id(existing) != _indexer_id(indexer)
    ] + [indexer]


def _find_indexer(indexers, indexer_id):
    for position, indexer in enumerate(indexers):
        if _indexer_id(indexer) == indexer_id:
            return position, indexer
    return -1, None


def add_preset_indexer(preset, api_key):
    """Fetch caps and persist an enabled preset-backed Newznab indexer."""
    caps, error = fetch_caps(preset.get("api_url"), api_key)
    if error:
        return None, error

    indexer = _normalized_preset_entry(preset, api_key, caps)
    save_indexers(_replace_indexer(load_indexers(), indexer))
    return indexer, None


def add_custom_indexer(name, api_url, api_key):
    """Fetch caps and persist an enabled custom Newznab indexer."""
    caps, error = fetch_caps(api_url, api_key)
    if error:
        return None, error

    indexer = _normalized_custom_entry(name, api_url, api_key, caps)
    save_indexers(_replace_indexer(load_indexers(), indexer))
    return indexer, None


def refresh_hydra_provider_caps():
    """Refresh NZBHydra2 caps from the configured Kodi settings."""
    addon = xbmcaddon.Addon()
    base_url = addon.getSetting("hydra_url").rstrip("/")
    api_key = addon.getSetting("hydra_api_key")
    return refresh_hydra_caps(base_url, api_key)


def set_indexer_enabled(indexer_id, enabled):
    """Set an indexer's enabled flag and persist the indexer list."""
    indexers = load_indexers()
    position, indexer = _find_indexer(indexers, indexer_id)
    if indexer is None:
        return None, _NOT_FOUND

    updated = dict(indexer)
    updated["enabled"] = bool(enabled)
    indexers[position] = updated
    save_indexers(indexers)
    return updated, None


def enable_indexer(indexer_id):
    """Enable an indexer and persist the change."""
    return set_indexer_enabled(indexer_id, True)


def disable_indexer(indexer_id):
    """Disable an indexer and persist the change."""
    return set_indexer_enabled(indexer_id, False)


def toggle_indexer_enabled(indexer_id):
    """Toggle an indexer's enabled flag and persist the change."""
    indexers = load_indexers()
    position, indexer = _find_indexer(indexers, indexer_id)
    if indexer is None:
        return None, _NOT_FOUND

    updated = dict(indexer)
    updated["enabled"] = not bool(indexer.get("enabled"))
    indexers[position] = updated
    save_indexers(indexers)
    return updated, None


def delete_indexer(indexer_id):
    """Delete an indexer and persist the remaining list."""
    indexers = load_indexers()
    position, indexer = _find_indexer(indexers, indexer_id)
    if indexer is None:
        return None, _NOT_FOUND

    del indexers[position]
    save_indexers(indexers)
    return indexer, None


remove_indexer = delete_indexer


def retest_indexer(indexer_id):
    """Fetch an indexer's caps again, saving updated caps on success."""
    indexers = load_indexers()
    position, indexer = _find_indexer(indexers, indexer_id)
    if indexer is None:
        return {}, _NOT_FOUND

    caps, error = fetch_caps(indexer.get("api_url"), indexer.get("api_key"))
    if error:
        return caps, error

    updated = dict(indexer)
    updated["caps"] = caps
    indexers[position] = updated
    save_indexers(indexers)
    return caps, None


def update_indexer(indexer_id, name=None, api_url=None, api_key=None):
    """Update editable indexer fields, refreshing caps when connection changes."""
    indexers = load_indexers()
    position, indexer = _find_indexer(indexers, indexer_id)
    if indexer is None:
        return None, _NOT_FOUND

    updated = dict(indexer)
    if name is not None:
        updated["name"] = name
    if api_url is not None:
        updated["api_url"] = api_url
    if api_key is not None:
        updated["api_key"] = api_key

    connection_changed = updated.get("api_url") != indexer.get(
        "api_url"
    ) or updated.get("api_key") != indexer.get("api_key")
    if connection_changed:
        caps, error = fetch_caps(updated.get("api_url"), updated.get("api_key"))
        if error:
            return None, error
        updated["caps"] = caps

    indexers[position] = normalize_indexer(updated)
    save_indexers(indexers)
    return indexers[position], None


def _notify(dialog, message, time=3000):
    dialog.notification(addon_name(), message, time=time)


def _ok(dialog, message):
    dialog.ok(addon_name(), message)


def _show_result(dialog, success_message, error):
    if error:
        _ok(dialog, error)
    else:
        _notify(dialog, success_message)


def _add_preset_flow(dialog):
    presets = list_newznab_presets()
    options = [_CUSTOM_NEWZNAB] + [preset.get("name", "") for preset in presets]

    choice = dialog.select(_ADD_NEWZNAB, options)
    if choice < 0:
        return
    if choice == 0:
        _add_custom_flow(dialog)
        return

    preset = presets[choice - 1]
    api_key = dialog.input(
        "{} API key".format(preset.get("name", "")),
        "",
        option=xbmcgui.ALPHANUM_HIDE_INPUT,
    )
    if not api_key:
        return

    indexer, error = add_preset_indexer(preset, api_key)
    if error:
        _ok(dialog, error)
    else:
        _notify(dialog, "Added {}".format(_indexer_label(indexer)))


def _add_custom_flow(dialog):
    name = dialog.input("Indexer name", "")
    if not name:
        return
    api_url = dialog.input("API URL", "")
    if not api_url:
        return
    api_key = dialog.input("API key", "", option=xbmcgui.ALPHANUM_HIDE_INPUT)
    if not api_key:
        return

    indexer, error = add_custom_indexer(name, api_url, api_key)
    if error:
        _ok(dialog, error)
    else:
        _notify(dialog, "Added {}".format(_indexer_label(indexer)))


def _refresh_hydra_flow(dialog):
    _caps, error = refresh_hydra_provider_caps()
    _show_result(dialog, "NZBHydra2 caps refreshed", error)


def _indexer_actions(indexer):
    toggle_label = "Disable" if indexer.get("enabled") else "Enable"
    return [_TEST, _EDIT, toggle_label, _DELETE]


def _input_or_cancel(dialog, heading, current, hidden=False):
    kwargs = {"option": xbmcgui.ALPHANUM_HIDE_INPUT} if hidden else {}
    default_value = _KEEP_CURRENT if hidden else current
    value = dialog.input(heading, default_value, **kwargs)
    if value == "":
        return None
    if value == _KEEP_CURRENT:
        return current
    return value


def _edit_indexer_flow(dialog, indexer):
    current_name = str(indexer.get("name") or "")
    current_url = str(indexer.get("api_url") or "")
    current_key = str(indexer.get("api_key") or "")
    name = _input_or_cancel(dialog, "Display name", current_name)
    if name is None:
        return
    api_url = _input_or_cancel(dialog, "API URL", current_url)
    if api_url is None:
        return
    api_key = _input_or_cancel(dialog, "API key", current_key, hidden=True)
    if api_key is None:
        return

    updated, error = update_indexer(
        _indexer_id(indexer),
        name=name,
        api_url=api_url,
        api_key=api_key,
    )
    if error:
        _ok(dialog, error)
    else:
        _notify(dialog, "Updated {}".format(_indexer_label(updated)))


def _existing_indexer_flow(dialog, indexer):
    choice = dialog.select(_indexer_label(indexer), _indexer_actions(indexer))
    if choice < 0:
        return

    indexer_id = _indexer_id(indexer)
    if choice == 0:
        _caps, error = retest_indexer(indexer_id)
        _show_result(dialog, "{} caps OK".format(_indexer_label(indexer)), error)
    elif choice == 1:
        _edit_indexer_flow(dialog, indexer)
    elif choice == 2:
        updated, error = toggle_indexer_enabled(indexer_id)
        if error:
            _ok(dialog, error)
        else:
            state = "enabled" if updated.get("enabled") else "disabled"
            _notify(dialog, "{} {}".format(_indexer_label(updated), state))
    elif choice == 3 and dialog.yesno(
        addon_name(), "Delete {}?".format(_indexer_label(indexer))
    ):
        _deleted, error = delete_indexer(indexer_id)
        _show_result(dialog, "Deleted {}".format(_indexer_label(indexer)), error)


def open_indexer_manager():
    """Open a simple Kodi dialog for managing configured direct indexers."""
    dialog = xbmcgui.Dialog()
    indexers = load_indexers()
    options = [_ADD_NEWZNAB, string(30196)]
    options.extend(_indexer_label(indexer) for indexer in indexers)

    choice = dialog.select(string(30195), options)
    if choice < 0:
        return
    if choice == 0:
        _add_preset_flow(dialog)
    elif choice == 1:
        _refresh_hydra_flow(dialog)
    elif choice - 2 < len(indexers):
        _existing_indexer_flow(dialog, indexers[choice - 2])
