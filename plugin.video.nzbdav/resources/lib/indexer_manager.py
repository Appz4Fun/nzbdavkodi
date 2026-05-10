# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Direct Newznab indexer manager actions."""

from urllib.parse import urlsplit

import xbmcaddon
import xbmcgui

from resources.lib.direct_indexers import get_legacy_configured_indexers
from resources.lib.http_util import redact_text
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
# Unique identity sentinel: distinguishes "user accepted the displayed
# default unchanged" from "user typed the literal placeholder string".
# Compared via ``is`` so a user whose actual api_key happens to equal
# ``"<keep current>"`` is not silently overwritten with the prior value.
_KEEP_CURRENT_SENTINEL = object()
_VERSION_CONFLICT = "Indexer was modified elsewhere; reload and retry"
_DISABLED_SUFFIX = " (disabled)"
_LAST_INDEXER_HEADING = "Last indexer"
_LAST_INDEXER_PROMPT = (
    "This is your last enabled indexer. Search will fail until you "
    "add another. Continue?"
)

_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _validate_indexer_url(url):
    """Return ``(True, "")`` if ``url`` is acceptable, else ``(False, reason)``.

    Reject scheme not in {http, https}, missing hostname, control chars
    (``ord(c) < 0x20``), or any whitespace anywhere in the string. UI-
    agnostic: callers decide how to surface the rejection reason.
    """
    text = "" if url is None else str(url)
    if not text:
        return False, "URL is empty"
    for char in text:
        if char.isspace():
            return False, "URL must not contain whitespace"
        if ord(char) < 0x20:
            return False, "URL contains control characters"
    try:
        parts = urlsplit(text)
    except ValueError as error:
        return False, "URL parse error: {}".format(error)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        return False, "URL scheme must be http or https (got {!r})".format(scheme)
    if not parts.hostname:
        return False, "URL is missing a hostname"
    return True, ""


def _preset_id(preset):
    return str(preset.get("id") or "").strip()


def _indexer_id(indexer):
    return str(indexer.get("id") or "").strip()


def _indexer_label(indexer):
    name = str(indexer.get("name") or "").strip()
    base = name or _indexer_id(indexer)
    # Visual disabled-state marker so the user can spot disabled
    # indexers in the manage list at a glance instead of having to
    # drill into each entry. ``enabled`` defaults to True for legacy
    # entries that pre-date the flag.
    if "enabled" in indexer and not bool(indexer.get("enabled")):
        return "{}{}".format(base, _DISABLED_SUFFIX)
    return base


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


def _normalized_legacy_entry(indexer):
    indexer_id = str(indexer.get("id") or "").strip()
    label = str(indexer.get("label") or indexer_id).strip()
    return normalize_indexer(
        {
            "id": indexer_id,
            "preset_id": indexer_id,
            "name": label,
            "api_url": indexer.get("api_url"),
            "api_key": indexer.get("api_key"),
            "enabled": True,
            "caps": indexer.get("caps", {}),
        }
    )


def _replace_indexer(indexers, indexer):
    return [
        existing
        for existing in indexers
        if _indexer_id(existing) != _indexer_id(indexer)
    ] + [indexer]


def _sorted_for_display(indexers):
    """Return enabled indexers first, then alphabetically by display label."""
    return sorted(
        indexers,
        key=lambda indexer: (
            not bool(indexer.get("enabled")),
            _indexer_label(indexer).lower(),
            _indexer_id(indexer),
        ),
    )


def _entry_version(entry):
    """Derive a content-fingerprint version for an indexer entry.

    `indexer_store.normalize_indexer` strips unknown fields, so an
    integer counter persisted on the dict wouldn't survive a save
    roundtrip. We instead derive a stable-but-mutation-sensitive
    "version" from the user-editable fields. Two concurrent edits
    against the same starting state will see the *original* fingerprint
    on first load; the second writer will see a *different* fingerprint
    on its pre-save re-load, triggering the conflict path.
    """
    if not isinstance(entry, dict):
        return 0
    snapshot = (
        str(entry.get("id") or ""),
        str(entry.get("name") or ""),
        str(entry.get("api_url") or ""),
        str(entry.get("api_key") or ""),
        bool(entry.get("enabled")),
    )
    # hash() keeps the value as a small int; offset by 1 so an empty
    # entry still reports a positive "default" version.
    return (hash(snapshot) & 0x7FFFFFFF) + 1


def _confirm_replace(indexer_id, name):
    """Prompt the user before silently overwriting an existing entry."""
    return xbmcgui.Dialog().yesno(
        "Replace existing?",
        "An indexer named {!r} (id={!r}) is already configured. Replace it?".format(
            name, indexer_id
        ),
    )


def _find_indexer(indexers, indexer_id):
    for position, indexer in enumerate(indexers):
        if _indexer_id(indexer) == indexer_id:
            return position, indexer
    return -1, None


def load_managed_indexers():
    """Load JSON indexers and migrate complete legacy static settings once."""
    indexers = load_indexers()
    existing_ids = {_indexer_id(indexer) for indexer in indexers}
    existing_urls = {
        str(indexer.get("api_url") or "").rstrip("/") for indexer in indexers
    }
    migrated = []
    for legacy in get_legacy_configured_indexers():
        indexer_id = str(legacy.get("id") or "").strip()
        api_url = str(legacy.get("api_url") or "").rstrip("/")
        if not indexer_id or indexer_id in existing_ids:
            continue
        if api_url and api_url in existing_urls:
            continue
        migrated.append(_normalized_legacy_entry(legacy))

    if migrated:
        indexers = indexers + migrated
        save_indexers(indexers)
    return indexers


def add_preset_indexer(preset, api_key):
    """Fetch caps and persist an enabled preset-backed Newznab indexer."""
    caps, error = fetch_caps(preset.get("api_url"), api_key)
    if error:
        return None, error

    indexer = _normalized_preset_entry(preset, api_key, caps)
    indexers = load_indexers()
    if _find_indexer(indexers, _indexer_id(indexer))[1] is not None:
        if not _confirm_replace(_indexer_id(indexer), _indexer_label(indexer)):
            return None, None
    save_indexers(_replace_indexer(indexers, indexer))
    return indexer, None


def add_custom_indexer(name, api_url, api_key, error_callback=None):
    """Fetch caps and persist an enabled custom Newznab indexer.

    ``error_callback`` (optional): invoked with the rejection reason if
    URL validation fails. Lets UI callers surface the reason without
    making this helper itself UI-bound.
    """
    valid, reason = _validate_indexer_url(api_url)
    if not valid:
        if error_callback is not None:
            error_callback(reason)
        return None, reason

    caps, error = fetch_caps(api_url, api_key)
    if error:
        return None, redact_text(error)

    indexer = _normalized_custom_entry(name, api_url, api_key, caps)
    indexers = load_indexers()
    if _find_indexer(indexers, _indexer_id(indexer))[1] is not None:
        if not _confirm_replace(_indexer_id(indexer), _indexer_label(indexer)):
            return None, None
    save_indexers(_replace_indexer(indexers, indexer))
    return indexer, None


def refresh_hydra_provider_caps():
    """Refresh NZBHydra2 caps from the configured Kodi settings."""
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
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
        # Redact apikey-style tokens before bubbling the error up: callers
        # render this directly to a Kodi dialog/notification.
        return caps, redact_text(error)

    updated = dict(indexer)
    updated["caps"] = caps
    indexers[position] = updated
    save_indexers(indexers)
    return caps, None


def update_indexer(
    indexer_id, name=None, api_url=None, api_key=None, error_callback=None
):
    """Update editable indexer fields, refreshing caps when connection changes.

    ``error_callback`` (optional): invoked with the rejection reason if
    URL validation fails. Lets UI callers surface the reason without
    making this helper itself UI-bound.
    """
    indexers = load_indexers()
    position, indexer = _find_indexer(indexers, indexer_id)
    if indexer is None:
        return None, _NOT_FOUND

    original_version = _entry_version(indexer)
    updated = dict(indexer)
    if name is not None:
        updated["name"] = name
    if api_url is not None:
        updated["api_url"] = api_url
    if api_key is not None:
        updated["api_key"] = api_key

    # Validate the *effective* URL on the updated entry. Edits that don't
    # touch api_url still get re-validated so a previously-stored bad URL
    # cannot escape new policy on first edit.
    valid, reason = _validate_indexer_url(updated.get("api_url"))
    if not valid:
        if error_callback is not None:
            error_callback(reason)
        return None, reason

    current_indexers = load_indexers()
    current_position, current_indexer = _find_indexer(current_indexers, indexer_id)
    if current_indexer is None:
        return None, _NOT_FOUND
    if _entry_version(current_indexer) != original_version:
        return None, _VERSION_CONFLICT

    connection_changed = updated.get("api_url") != indexer.get(
        "api_url"
    ) or updated.get("api_key") != indexer.get("api_key")
    if connection_changed:
        caps, error = fetch_caps(updated.get("api_url"), updated.get("api_key"))
        if error:
            return None, redact_text(error)
        updated["caps"] = caps

    current_indexers[current_position] = normalize_indexer(updated)
    save_indexers(current_indexers)
    return current_indexers[current_position], None


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
    name = str(dialog.input("Indexer name", "") or "").strip()
    if not name:
        return False
    api_url = str(dialog.input("API URL", "") or "").strip()
    if not api_url:
        return False
    api_key = str(
        dialog.input("API key", "", option=xbmcgui.ALPHANUM_HIDE_INPUT) or ""
    ).strip()
    if not api_key:
        return False

    indexer, error = add_custom_indexer(name, api_url, api_key)
    if error:
        _ok(dialog, error)
        return False
    else:
        _notify(dialog, "Added {}".format(_indexer_label(indexer)))
        return True


def _refresh_hydra_flow(dialog):
    _caps, error = refresh_hydra_provider_caps()
    # ``refresh_hydra_caps`` can surface the request URL (with apikey=...)
    # via urllib's HTTPError; redact before rendering to a Kodi dialog.
    _show_result(
        dialog, "NZBHydra2 caps refreshed", redact_text(error) if error else None
    )


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
        return _KEEP_CURRENT_SENTINEL
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
    if api_key is _KEEP_CURRENT_SENTINEL:
        api_key = current_key

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
        # ``retest_indexer`` already redacts via http_util, but apply
        # again defensively in case the error path changes upstream.
        _show_result(
            dialog,
            "{} caps OK".format(_indexer_label(indexer)),
            redact_text(error) if error else None,
        )
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
        # If this is the last enabled indexer, warn the user before
        # delete: search will fail with no enabled indexers configured.
        enabled_count = sum(
            1 for entry in load_indexers() if bool(entry.get("enabled"))
        )
        if enabled_count == 1 and bool(indexer.get("enabled")):
            if not dialog.yesno(_LAST_INDEXER_HEADING, _LAST_INDEXER_PROMPT):
                return
        _deleted, error = delete_indexer(indexer_id)
        _show_result(dialog, "Deleted {}".format(_indexer_label(indexer)), error)


def open_indexer_manager():
    """Open a simple Kodi dialog for managing configured direct indexers."""
    dialog = xbmcgui.Dialog()
    indexers = _sorted_for_display(load_managed_indexers())
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
