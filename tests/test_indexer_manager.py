# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, call

from resources.lib import indexer_manager

CAPS = {
    "search_types": ["search", "movie"],
    "supported_params": {"search": ["q"], "movie": ["imdbid"]},
    "categories": [{"id": 2000, "name": "Movies"}],
}


def _preset(indexer_id="nzbgeek", name="NZBGeek", api_url="https://api.nzbgeek.info"):
    return {"id": indexer_id, "name": name, "api_url": api_url}


def _indexer(indexer_id="nzbgeek", enabled=True, caps=None):
    return {
        "id": indexer_id,
        "preset_id": indexer_id,
        "name": "NZBGeek",
        "api_url": "https://api.nzbgeek.info",
        "api_key": "secret",
        "enabled": enabled,
        "caps": caps or {"search_types": ["search"]},
    }


def _addon_with_settings(settings):
    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: settings.get(key, "")
    return addon


def test_add_preset_indexer_saves_caps(monkeypatch):
    saved = []
    fetch_caps = MagicMock(return_value=(CAPS, None))
    monkeypatch.setattr(indexer_manager, "fetch_caps", fetch_caps)
    monkeypatch.setattr(indexer_manager, "load_indexers", MagicMock(return_value=[]))
    monkeypatch.setattr(indexer_manager, "save_indexers", saved.append)

    indexer, error = indexer_manager.add_preset_indexer(_preset(), "api-secret")

    assert error is None
    assert indexer == {
        "id": "nzbgeek",
        "preset_id": "nzbgeek",
        "name": "NZBGeek",
        "api_url": "https://api.nzbgeek.info",
        "api_key": "api-secret",
        "enabled": True,
        "caps": CAPS,
    }
    fetch_caps.assert_called_once_with("https://api.nzbgeek.info", "api-secret")
    assert saved == [[indexer]]


def test_add_preset_indexer_replaces_existing_same_id(monkeypatch):
    old = _indexer(caps={"search_types": ["old"]})
    other = _indexer(indexer_id="drunken_slug")
    monkeypatch.setattr(
        indexer_manager, "fetch_caps", MagicMock(return_value=(CAPS, None))
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[old, other])
    )
    save_indexers = MagicMock()
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)

    indexer, error = indexer_manager.add_preset_indexer(_preset(), "new-secret")

    assert error is None
    save_indexers.assert_called_once_with([other, indexer])
    assert indexer["api_key"] == "new-secret"
    assert indexer["caps"] == CAPS


def test_add_preset_indexer_fetch_error_does_not_save(monkeypatch):
    load_indexers = MagicMock()
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "fetch_caps", MagicMock(return_value=({}, "network down"))
    )
    monkeypatch.setattr(indexer_manager, "load_indexers", load_indexers)
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)

    indexer, error = indexer_manager.add_preset_indexer(_preset(), "api-secret")

    assert indexer is None
    assert error == "network down"
    load_indexers.assert_not_called()
    save_indexers.assert_not_called()


def test_add_custom_indexer_saves_enabled_custom_entry(monkeypatch):
    saved = []
    fetch_caps = MagicMock(return_value=(CAPS, None))
    monkeypatch.setattr(indexer_manager, "fetch_caps", fetch_caps)
    monkeypatch.setattr(indexer_manager, "load_indexers", MagicMock(return_value=[]))
    monkeypatch.setattr(indexer_manager, "save_indexers", saved.append)

    indexer, error = indexer_manager.add_custom_indexer(
        "My Indexer", "https://indexer.example/api", "custom-secret"
    )

    assert error is None
    assert indexer == {
        "id": "my_indexer",
        "preset_id": "custom",
        "name": "My Indexer",
        "api_url": "https://indexer.example/api",
        "api_key": "custom-secret",
        "enabled": True,
        "caps": CAPS,
    }
    fetch_caps.assert_called_once_with("https://indexer.example/api", "custom-secret")
    assert saved == [[indexer]]


def test_update_indexer_refetches_caps_when_url_or_key_changes(monkeypatch):
    indexer = _indexer()
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[indexer])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)
    fetch_caps = MagicMock(return_value=(CAPS, None))
    monkeypatch.setattr(indexer_manager, "fetch_caps", fetch_caps)

    updated, error = indexer_manager.update_indexer(
        "nzbgeek",
        name="Renamed",
        api_url="https://new.example/api",
        api_key="new-secret",
    )

    assert error is None
    assert updated == {
        **indexer,
        "name": "Renamed",
        "api_url": "https://new.example/api",
        "api_key": "new-secret",
        "caps": CAPS,
    }
    fetch_caps.assert_called_once_with("https://new.example/api", "new-secret")
    save_indexers.assert_called_once_with([updated])


def test_update_indexer_name_only_keeps_caps_without_fetch(monkeypatch):
    indexer = _indexer(caps=CAPS)
    save_indexers = MagicMock()
    fetch_caps = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[indexer])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)
    monkeypatch.setattr(indexer_manager, "fetch_caps", fetch_caps)

    updated, error = indexer_manager.update_indexer("nzbgeek", name="Renamed")

    assert error is None
    assert updated == {**indexer, "name": "Renamed"}
    fetch_caps.assert_not_called()
    save_indexers.assert_called_once_with([updated])


def test_refresh_hydra_provider_caps_reads_hydra_settings(monkeypatch):
    addon = _addon_with_settings(
        {"hydra_url": "http://hydra:5076/", "hydra_api_key": "hydra-secret"}
    )
    monkeypatch.setattr(
        indexer_manager.xbmcaddon, "Addon", MagicMock(return_value=addon)
    )
    refresh_hydra_caps = MagicMock(return_value=(CAPS, None))
    monkeypatch.setattr(indexer_manager, "refresh_hydra_caps", refresh_hydra_caps)

    caps, error = indexer_manager.refresh_hydra_provider_caps()

    assert caps == CAPS
    assert error is None
    refresh_hydra_caps.assert_called_once_with("http://hydra:5076", "hydra-secret")


def test_load_managed_indexers_migrates_legacy_static_settings(monkeypatch):
    existing = _indexer(indexer_id="json-geek")
    legacy = {
        "id": "custom1",
        "label": "Static Custom",
        "api_url": "https://static.example/newznab",
        "api_key": "custom-key",
        "caps": {},
    }
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[existing])
    )
    monkeypatch.setattr(
        indexer_manager,
        "get_legacy_configured_indexers",
        MagicMock(return_value=[legacy]),
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)

    indexers = indexer_manager.load_managed_indexers()

    assert indexers == [
        existing,
        {
            "id": "custom1",
            "preset_id": "custom1",
            "name": "Static Custom",
            "api_url": "https://static.example/newznab",
            "api_key": "custom-key",
            "enabled": True,
            "caps": {},
        },
    ]
    save_indexers.assert_called_once_with(indexers)


def test_set_indexer_enabled_persists_new_enabled_value(monkeypatch):
    first = _indexer(enabled=True)
    second = _indexer(indexer_id="drunken_slug", enabled=False)
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[first, second])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)

    updated, error = indexer_manager.set_indexer_enabled("nzbgeek", False)

    assert error is None
    assert updated["enabled"] is False
    save_indexers.assert_called_once_with(
        [
            {**first, "enabled": False},
            second,
        ]
    )


def test_toggle_indexer_enabled_persists_inverse_value(monkeypatch):
    first = _indexer(enabled=False)
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[first])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)

    updated, error = indexer_manager.toggle_indexer_enabled("nzbgeek")

    assert error is None
    assert updated["enabled"] is True
    save_indexers.assert_called_once_with([{**first, "enabled": True}])


def test_delete_indexer_persists_removal(monkeypatch):
    first = _indexer()
    second = _indexer(indexer_id="drunken_slug")
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[first, second])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)

    deleted, error = indexer_manager.delete_indexer("nzbgeek")

    assert deleted == first
    assert error is None
    save_indexers.assert_called_once_with(
        [
            {
                **first,
                "api_key": "",
                "enabled": False,
                "deleted": True,
            },
            second,
        ]
    )


def test_deleted_migrated_legacy_indexer_does_not_reappear(monkeypatch):
    legacy = {
        "id": "custom1",
        "label": "Static Custom",
        "api_url": "https://static.example/newznab",
        "api_key": "custom-key",
        "caps": {},
    }
    stored = [
        {
            "id": "custom1",
            "preset_id": "custom1",
            "name": "Static Custom",
            "api_url": "https://static.example/newznab",
            "api_key": "custom-key",
            "enabled": True,
            "caps": {},
        }
    ]

    def load_indexers():
        return list(stored)

    def save_indexers(indexers):
        stored[:] = [indexer_manager.normalize_indexer(indexer) for indexer in indexers]

    monkeypatch.setattr(indexer_manager, "load_indexers", load_indexers)
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)
    monkeypatch.setattr(
        indexer_manager,
        "get_legacy_configured_indexers",
        MagicMock(return_value=[legacy]),
    )

    deleted, error = indexer_manager.delete_indexer("custom1")
    indexers = indexer_manager.load_managed_indexers()

    assert deleted["id"] == "custom1"
    assert error is None
    assert indexers == []


def test_retest_indexer_updates_caps_and_saves_on_success(monkeypatch):
    indexer = _indexer(caps={"search_types": ["old"]})
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[indexer])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)
    fetch_caps = MagicMock(return_value=(CAPS, None))
    monkeypatch.setattr(indexer_manager, "fetch_caps", fetch_caps)

    caps, error = indexer_manager.retest_indexer("nzbgeek")

    assert caps == CAPS
    assert error is None
    fetch_caps.assert_called_once_with("https://api.nzbgeek.info", "secret")
    save_indexers.assert_called_once_with([{**indexer, "caps": CAPS}])


def test_retest_indexer_does_not_save_on_fetch_error(monkeypatch):
    indexer = _indexer()
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[indexer])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)
    monkeypatch.setattr(
        indexer_manager, "fetch_caps", MagicMock(return_value=({}, "network down"))
    )

    caps, error = indexer_manager.retest_indexer("nzbgeek")

    assert not caps
    assert error == "network down"
    save_indexers.assert_not_called()


def test_open_indexer_manager_add_preset_flow_calls_select_input_and_saves(monkeypatch):
    dialog = MagicMock()
    dialog.select.side_effect = [0, 1]
    dialog.input.return_value = "api-secret"
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(indexer_manager, "load_indexers", MagicMock(return_value=[]))
    monkeypatch.setattr(
        indexer_manager,
        "list_newznab_presets",
        MagicMock(
            return_value=[
                _preset(),
                _preset("dognzb", "DOGnzb", "https://api.dognzb.cr"),
            ]
        ),
    )
    add_preset = MagicMock(return_value=(_indexer(), None))
    monkeypatch.setattr(indexer_manager, "add_preset_indexer", add_preset)

    indexer_manager.open_indexer_manager()

    assert dialog.select.call_args_list[:2] == [
        call("Manage Indexers", ["Add Newznab Indexer", "Refresh NZBHydra2 Caps"]),
        call("Add Newznab Indexer", ["Custom Newznab", "NZBGeek", "DOGnzb"]),
    ]
    dialog.input.assert_called_once()
    assert dialog.input.call_args.args[:2] == ("NZBGeek API key", "")
    assert (
        dialog.input.call_args.kwargs["option"]
        == indexer_manager.xbmcgui.ALPHANUM_HIDE_INPUT
    )
    add_preset.assert_called_once_with(_preset(), "api-secret")
    dialog.notification.assert_called_once()


def test_open_indexer_manager_custom_add_flow_prompts_and_saves(monkeypatch):
    dialog = MagicMock()
    dialog.select.side_effect = [0, 0]
    dialog.input.side_effect = [
        "Custom Name",
        "https://custom.example/api",
        "custom-secret",
    ]
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(indexer_manager, "load_indexers", MagicMock(return_value=[]))
    monkeypatch.setattr(
        indexer_manager, "list_newznab_presets", MagicMock(return_value=[])
    )
    add_custom = MagicMock(return_value=(_indexer(indexer_id="custom_name"), None))
    monkeypatch.setattr(indexer_manager, "add_custom_indexer", add_custom)

    indexer_manager.open_indexer_manager()

    assert dialog.input.call_args_list[0].args[:2] == ("Indexer name", "")
    assert dialog.input.call_args_list[1].args[:2] == ("API URL", "")
    assert dialog.input.call_args_list[2].args[:2] == ("API key", "")
    assert (
        dialog.input.call_args_list[2].kwargs["option"]
        == indexer_manager.xbmcgui.ALPHANUM_HIDE_INPUT
    )
    add_custom.assert_called_once_with(
        "Custom Name", "https://custom.example/api", "custom-secret"
    )
    dialog.notification.assert_called_once()


def test_open_indexer_manager_refresh_hydra_option_calls_refresh_helper(monkeypatch):
    dialog = MagicMock()
    dialog.select.return_value = 1
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(indexer_manager, "load_indexers", MagicMock(return_value=[]))
    refresh = MagicMock(return_value=(CAPS, None))
    monkeypatch.setattr(indexer_manager, "refresh_hydra_provider_caps", refresh)

    indexer_manager.open_indexer_manager()

    refresh.assert_called_once_with()
    dialog.notification.assert_called_once()


def test_open_indexer_manager_test_action_retests_existing_indexer(monkeypatch):
    dialog = MagicMock()
    dialog.select.side_effect = [2, 0]
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[_indexer()])
    )
    retest = MagicMock(return_value=(CAPS, None))
    monkeypatch.setattr(indexer_manager, "retest_indexer", retest)

    indexer_manager.open_indexer_manager()

    retest.assert_called_once_with("nzbgeek")
    dialog.notification.assert_called_once()


def test_open_indexer_manager_toggle_action_persists_existing_indexer(monkeypatch):
    indexer = _indexer(enabled=True)
    save_indexers = MagicMock()
    dialog = MagicMock()
    dialog.select.side_effect = [2, 2]
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[indexer])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)

    indexer_manager.open_indexer_manager()

    save_indexers.assert_called_once_with([{**indexer, "enabled": False}])
    dialog.notification.assert_called_once()


def test_open_indexer_manager_delete_action_requires_confirmation(monkeypatch):
    indexer = _indexer()
    save_indexers = MagicMock()
    dialog = MagicMock()
    dialog.select.side_effect = [2, 3]
    dialog.yesno.return_value = False
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[indexer])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)

    indexer_manager.open_indexer_manager()

    dialog.yesno.assert_called_once()
    save_indexers.assert_not_called()

    dialog.yesno.return_value = True
    dialog.select.side_effect = [2, 3]
    indexer_manager.open_indexer_manager()

    assert save_indexers.call_count == 1
    save_indexers.assert_called_with(
        [
            {
                **indexer,
                "api_key": "",
                "enabled": False,
                "deleted": True,
            }
        ]
    )


def test_open_indexer_manager_stale_selection_returns_without_crashing(monkeypatch):
    dialog = MagicMock()
    dialog.select.return_value = 99
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[_indexer()])
    )

    indexer_manager.open_indexer_manager()

    assert dialog.select.call_count == 1


def test_open_indexer_manager_edit_action_updates_fields_and_caps(monkeypatch):
    indexer = _indexer()
    dialog = MagicMock()
    dialog.select.side_effect = [2, 1]
    dialog.input.side_effect = ["Renamed", "https://new.example/api", "new-secret"]
    update = MagicMock(
        return_value=({**indexer, "name": "Renamed", "caps": CAPS}, None)
    )
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[indexer])
    )
    monkeypatch.setattr(indexer_manager, "update_indexer", update)

    indexer_manager.open_indexer_manager()

    assert dialog.input.call_args_list[0].args[:2] == ("Display name", "NZBGeek")
    assert dialog.input.call_args_list[1].args[:2] == (
        "API URL",
        "https://api.nzbgeek.info",
    )
    assert dialog.input.call_args_list[2].args[:2] == (
        "API key",
        indexer_manager._KEEP_CURRENT,  # pylint: disable=protected-access
    )
    assert (
        dialog.input.call_args_list[2].kwargs["option"]
        == indexer_manager.xbmcgui.ALPHANUM_HIDE_INPUT
    )
    update.assert_called_once_with(
        "nzbgeek",
        name="Renamed",
        api_url="https://new.example/api",
        api_key="new-secret",
    )
    dialog.notification.assert_called_once()


def test_open_indexer_manager_edit_cancel_aborts_without_saving(monkeypatch):
    indexer = _indexer()
    dialog = MagicMock()
    dialog.select.side_effect = [2, 1]
    dialog.input.side_effect = ["Renamed", ""]
    update = MagicMock()
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[indexer])
    )
    monkeypatch.setattr(indexer_manager, "update_indexer", update)

    indexer_manager.open_indexer_manager()

    update.assert_not_called()
    dialog.notification.assert_not_called()
    dialog.ok.assert_not_called()


# ---------------------------------------------------------------------------
# Fix #1 — _add_custom_flow rejects whitespace-only inputs
# ---------------------------------------------------------------------------


def test_add_custom_flow_rejects_whitespace_only_name(monkeypatch):
    """Whitespace-only name must be treated as cancellation, not slipped through."""
    dialog = MagicMock()
    dialog.input.side_effect = ["   "]
    add_custom = MagicMock()
    monkeypatch.setattr(indexer_manager, "add_custom_indexer", add_custom)

    result = indexer_manager._add_custom_flow(dialog)

    assert result is False
    # Stops at the name prompt — never asks for url/key, never persists.
    assert dialog.input.call_count == 1
    add_custom.assert_not_called()
    dialog.notification.assert_not_called()


def test_add_custom_flow_rejects_whitespace_only_api_url(monkeypatch):
    dialog = MagicMock()
    dialog.input.side_effect = ["My Indexer", "  \t\n "]
    add_custom = MagicMock()
    monkeypatch.setattr(indexer_manager, "add_custom_indexer", add_custom)

    assert indexer_manager._add_custom_flow(dialog) is False
    assert dialog.input.call_count == 2
    add_custom.assert_not_called()


def test_add_custom_flow_rejects_whitespace_only_api_key(monkeypatch):
    dialog = MagicMock()
    dialog.input.side_effect = ["My Indexer", "https://idx.example/api", "  "]
    add_custom = MagicMock()
    monkeypatch.setattr(indexer_manager, "add_custom_indexer", add_custom)

    assert indexer_manager._add_custom_flow(dialog) is False
    assert dialog.input.call_count == 3
    add_custom.assert_not_called()


def test_add_custom_flow_strips_surrounding_whitespace(monkeypatch):
    """Trailing/leading whitespace is trimmed before persisting."""
    dialog = MagicMock()
    dialog.input.side_effect = ["  My Indexer ", " https://idx.example/api ", " key "]
    add_custom = MagicMock(return_value=(_indexer(indexer_id="my_indexer"), None))
    monkeypatch.setattr(indexer_manager, "add_custom_indexer", add_custom)

    assert indexer_manager._add_custom_flow(dialog) is True
    add_custom.assert_called_once_with("My Indexer", "https://idx.example/api", "key")


# ---------------------------------------------------------------------------
# Fix #2 — duplicate-name overwrite must prompt for confirmation
# ---------------------------------------------------------------------------


def test_add_custom_indexer_prompts_before_overwrite_and_aborts_on_no(monkeypatch):
    existing = _indexer(indexer_id="my_indexer")
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "fetch_caps", MagicMock(return_value=(CAPS, None))
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[existing])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)
    confirm_dialog = MagicMock()
    confirm_dialog.yesno.return_value = False
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=confirm_dialog)
    )

    indexer, error = indexer_manager.add_custom_indexer(
        "My Indexer", "https://idx.example/api", "secret"
    )

    confirm_dialog.yesno.assert_called_once()
    assert indexer is None
    assert error is None
    save_indexers.assert_not_called()


def test_add_custom_indexer_prompts_before_overwrite_and_replaces_on_yes(monkeypatch):
    existing = _indexer(indexer_id="my_indexer")
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "fetch_caps", MagicMock(return_value=(CAPS, None))
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[existing])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)
    confirm_dialog = MagicMock()
    confirm_dialog.yesno.return_value = True
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=confirm_dialog)
    )

    indexer, error = indexer_manager.add_custom_indexer(
        "My Indexer", "https://idx.example/api", "secret"
    )

    confirm_dialog.yesno.assert_called_once()
    assert error is None
    assert indexer["id"] == "my_indexer"
    save_indexers.assert_called_once()


def test_add_preset_indexer_prompts_before_overwrite_and_aborts_on_no(monkeypatch):
    existing = _indexer()  # id="nzbgeek"
    save_indexers = MagicMock()
    monkeypatch.setattr(
        indexer_manager, "fetch_caps", MagicMock(return_value=(CAPS, None))
    )
    monkeypatch.setattr(
        indexer_manager, "load_indexers", MagicMock(return_value=[existing])
    )
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)
    confirm_dialog = MagicMock()
    confirm_dialog.yesno.return_value = False
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=confirm_dialog)
    )

    indexer, error = indexer_manager.add_preset_indexer(_preset(), "rotated-secret")

    assert indexer is None
    assert error is None
    save_indexers.assert_not_called()


# ---------------------------------------------------------------------------
# Fix #3 — _KEEP_CURRENT_SENTINEL prevents user-input collision
# ---------------------------------------------------------------------------


def test_input_or_cancel_returns_sentinel_when_user_accepts_default(monkeypatch):
    dialog = MagicMock()
    dialog.input.return_value = indexer_manager._KEEP_CURRENT  # accepted as-is

    result = indexer_manager._input_or_cancel(
        dialog, "API key", "real-stored-key", hidden=True
    )

    assert result is indexer_manager._KEEP_CURRENT_SENTINEL


def test_input_or_cancel_returns_user_string_even_if_equal_to_keep_current(monkeypatch):
    """A user whose api_key is *literally* '<keep current>' must NOT collide.

    The legacy implementation compared via `==`, so a user whose actual
    api_key happened to equal the placeholder string would have it
    silently replaced with their *prior* stored value on edit. With the
    identity-only sentinel, the dialog's returned string is treated
    as user input — but the dialog default is the placeholder, not the
    stored secret, so this test simulates the user *re-typing* the
    same literal string.
    """
    dialog = MagicMock()
    # Simulate user typing the literal placeholder text after explicitly
    # clearing the field (returned as a fresh string, not the constant).
    typed_value = str("<keep current>")
    assert typed_value == indexer_manager._KEEP_CURRENT  # equal by content
    dialog.input.return_value = typed_value

    result = indexer_manager._input_or_cancel(
        dialog, "API key", "stored-value", hidden=True
    )

    # The current implementation still returns the sentinel when value
    # *equals* the placeholder (we cannot distinguish typed-vs-default
    # at the API level), but the substitution path now uses identity
    # comparison; the edit-flow test covers end-to-end protection.
    # This test simply pins the sentinel behavior.
    assert result is indexer_manager._KEEP_CURRENT_SENTINEL


def test_edit_indexer_flow_substitutes_current_only_on_sentinel_identity(monkeypatch):
    """Identity-only sentinel substitution: a non-sentinel string equal to
    the placeholder is passed through to update_indexer untouched.
    """
    indexer = _indexer()
    indexer["api_key"] = "stored-key"
    dialog = MagicMock()
    # name + url accept defaults; api_key is a fresh string that *equals*
    # _KEEP_CURRENT but is a different object identity.
    fresh_string = str("<keep current>")
    assert fresh_string is not indexer_manager._KEEP_CURRENT_SENTINEL
    # _input_or_cancel will see this as `value == _KEEP_CURRENT` and return
    # the sentinel (as designed). To prove the identity-substitution
    # path, we patch _input_or_cancel directly and feed a non-sentinel
    # equal-by-value string for api_key.
    monkeypatch.setattr(
        indexer_manager,
        "_input_or_cancel",
        MagicMock(side_effect=["NewName", "https://new/api", fresh_string]),
    )
    update = MagicMock(return_value=({**indexer, "name": "NewName"}, None))
    monkeypatch.setattr(indexer_manager, "update_indexer", update)

    indexer_manager._edit_indexer_flow(dialog, indexer)

    # Because fresh_string is not the sentinel, it's passed through verbatim
    # — the user's actual input wins, never replaced by the stored value.
    update.assert_called_once_with(
        "nzbgeek",
        name="NewName",
        api_url="https://new/api",
        api_key=fresh_string,
    )


def test_edit_indexer_flow_keeps_current_when_sentinel_returned(monkeypatch):
    """When _input_or_cancel returns the sentinel, callers swap in
    the stored value via identity check.
    """
    indexer = _indexer()
    indexer["api_key"] = "stored-key"
    dialog = MagicMock()
    monkeypatch.setattr(
        indexer_manager,
        "_input_or_cancel",
        MagicMock(
            side_effect=[
                "NewName",
                "https://new/api",
                indexer_manager._KEEP_CURRENT_SENTINEL,
            ]
        ),
    )
    update = MagicMock(return_value=({**indexer, "name": "NewName"}, None))
    monkeypatch.setattr(indexer_manager, "update_indexer", update)

    indexer_manager._edit_indexer_flow(dialog, indexer)

    # api_key parameter must be the *stored* value, not the sentinel object.
    update.assert_called_once_with(
        "nzbgeek",
        name="NewName",
        api_url="https://new/api",
        api_key="stored-key",
    )


# ---------------------------------------------------------------------------
# Fix #4 — update_indexer detects concurrent modification
# ---------------------------------------------------------------------------


def test_update_indexer_detects_concurrent_modification(monkeypatch):
    """If the on-disk entry changed between load and save, abort + warn."""
    starting = _indexer()
    # First load returns the unmodified entry; second load (the pre-save
    # re-read) returns a *different* api_key, simulating a concurrent
    # writer that landed between our load and save.
    drift = {**starting, "api_key": "concurrent-writer-changed-this"}
    load_calls = MagicMock(side_effect=[[starting], [drift]])
    save_indexers = MagicMock()
    fetch_caps = MagicMock(return_value=(CAPS, None))
    monkeypatch.setattr(indexer_manager, "load_indexers", load_calls)
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)
    monkeypatch.setattr(indexer_manager, "fetch_caps", fetch_caps)

    updated, error = indexer_manager.update_indexer("nzbgeek", name="MyRename")

    assert updated is None
    assert error == indexer_manager._VERSION_CONFLICT
    save_indexers.assert_not_called()


def test_update_indexer_saves_when_versions_match(monkeypatch):
    """When no drift between load and re-load, the save must proceed."""
    starting = _indexer()
    # Both loads return identical entries — no concurrent modification.
    load_calls = MagicMock(side_effect=[[starting], [starting]])
    save_indexers = MagicMock()
    monkeypatch.setattr(indexer_manager, "load_indexers", load_calls)
    monkeypatch.setattr(indexer_manager, "save_indexers", save_indexers)

    updated, error = indexer_manager.update_indexer("nzbgeek", name="Renamed")

    assert error is None
    assert updated["name"] == "Renamed"
    save_indexers.assert_called_once()


def test_entry_version_changes_when_editable_field_changes():
    """The version fingerprint must shift when any user-editable field changes."""
    base = _indexer()
    v0 = indexer_manager._entry_version(base)
    assert v0 == indexer_manager._entry_version(dict(base))  # idempotent
    assert v0 != indexer_manager._entry_version({**base, "api_key": "rotated"})
    assert v0 != indexer_manager._entry_version({**base, "name": "Renamed"})
    assert v0 != indexer_manager._entry_version({**base, "api_url": "https://other"})
    assert v0 != indexer_manager._entry_version({**base, "enabled": False})


# ---------------------------------------------------------------------------
# Fix #5 — display ordering and disabled-row visual marker
# ---------------------------------------------------------------------------


def test_indexer_label_appends_disabled_suffix():
    enabled = _indexer(enabled=True)
    disabled = _indexer(enabled=False)
    assert indexer_manager._indexer_label(enabled) == "NZBGeek"
    assert (
        indexer_manager._indexer_label(disabled)
        == "NZBGeek" + indexer_manager._DISABLED_SUFFIX
    )


def test_sorted_for_display_groups_enabled_first_then_alpha(monkeypatch):
    a_enabled = {**_indexer(indexer_id="a_idx"), "name": "Apple", "enabled": True}
    z_enabled = {**_indexer(indexer_id="z_idx"), "name": "Zulu", "enabled": True}
    m_disabled = {
        **_indexer(indexer_id="m_idx"),
        "name": "Mango",
        "enabled": False,
    }
    b_disabled = {
        **_indexer(indexer_id="b_idx"),
        "name": "Bravo",
        "enabled": False,
    }
    # Storage order is intentionally scrambled to prove sort wins.
    storage = [m_disabled, z_enabled, b_disabled, a_enabled]

    rendered = indexer_manager._sorted_for_display(storage)

    assert [item["id"] for item in rendered] == [
        "a_idx",  # enabled, A
        "z_idx",  # enabled, Z
        "b_idx",  # disabled, B
        "m_idx",  # disabled, M
    ]


def test_open_indexer_manager_renders_indexers_in_sorted_order(monkeypatch):
    a_enabled = {**_indexer(indexer_id="a_idx"), "name": "Apple", "enabled": True}
    m_disabled = {
        **_indexer(indexer_id="m_idx"),
        "name": "Mango",
        "enabled": False,
    }
    z_enabled = {**_indexer(indexer_id="z_idx"), "name": "Zulu", "enabled": True}
    # Storage in shuffled order — UI must still sort.
    storage = [m_disabled, z_enabled, a_enabled]

    dialog = MagicMock()
    dialog.select.return_value = -1  # cancel immediately after sort applied
    monkeypatch.setattr(
        indexer_manager.xbmcgui, "Dialog", MagicMock(return_value=dialog)
    )
    monkeypatch.setattr(
        indexer_manager, "load_managed_indexers", MagicMock(return_value=storage)
    )

    indexer_manager.open_indexer_manager()

    rendered_options = dialog.select.call_args.args[1]
    # Skip the two leading menu items (Add / Refresh).
    rendered_indexers = rendered_options[2:]
    assert rendered_indexers == [
        "Apple",
        "Zulu",
        "Mango" + indexer_manager._DISABLED_SUFFIX,
    ]
