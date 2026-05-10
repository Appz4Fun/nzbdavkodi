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
    save_indexers.assert_called_once_with([second])


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
    save_indexers.assert_called_with([])


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
