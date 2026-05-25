# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

import pytest
from resources.lib import setup_wizard


def _addon_with_settings(values=None):
    values = dict(values or {})
    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: values.get(key, "")
    addon.getAddonInfo.side_effect = lambda key: "" if key == "profile" else ""
    return addon


def _wizard_dialog(addon=None):
    return setup_wizard.SetupWizardDialog(
        "setup-wizard.xml",
        "",
        "Default",
        "1080i",
        addon=addon or _addon_with_settings(),
    )


def test_should_auto_run_until_completed():
    assert setup_wizard.should_auto_run(
        _addon_with_settings({"setup_wizard_completed": "false"})
    )
    assert setup_wizard.should_auto_run(_addon_with_settings({}))
    assert not setup_wizard.should_auto_run(
        _addon_with_settings({"setup_wizard_completed": "true"})
    )


def test_should_auto_run_skips_existing_configured_installations():
    assert not setup_wizard.should_auto_run(
        _addon_with_settings(
            {
                "setup_wizard_completed": "false",
                "nzbdav_api_key": "nzbdav-secret",
                "hydra_api_key": "hydra-secret",
            }
        )
    )
    assert not setup_wizard.should_auto_run(
        _addon_with_settings(
            {
                "setup_wizard_completed": "false",
                "prowlarr_enabled": "true",
            }
        )
    )


def test_should_auto_run_keeps_fresh_default_installations_enabled():
    assert setup_wizard.should_auto_run(
        _addon_with_settings(
            {
                "setup_wizard_completed": "false",
                "nzbdav_url": "http://localhost:3000",
                "webdav_url": "http://localhost:8080",
                "hydra_url": "http://localhost:5076",
                "prowlarr_host": "http://localhost:9696",
            }
        )
    )


def test_run_setup_wizard_notifies_on_finish():
    addon = _addon_with_settings({"setup_wizard_completed": "true"})

    with patch("resources.lib.setup_wizard.xbmcaddon.Addon", return_value=addon):
        with patch("resources.lib.setup_wizard.SetupWizardDialog") as dialog_cls:
            dialog = MagicMock()
            dialog.was_finished.return_value = True
            dialog_cls.return_value = dialog

            with patch("resources.lib.setup_wizard._notify") as notify:
                assert setup_wizard.run_setup_wizard()

    notify.assert_called_once()
    dialog.doModal.assert_called_once()


def test_run_setup_wizard_marks_completed_when_modal_finishes():
    addon = _addon_with_settings({"setup_wizard_completed": "false"})

    with patch("resources.lib.setup_wizard.xbmcaddon.Addon", return_value=addon):
        with patch("resources.lib.setup_wizard.SetupWizardDialog") as dialog_cls:
            dialog = MagicMock()
            dialog.was_finished.return_value = True
            dialog_cls.return_value = dialog

            with patch("resources.lib.setup_wizard._notify"):
                assert setup_wizard.run_setup_wizard()

    addon.setSetting.assert_called_once_with("setup_wizard_completed", "true")


def test_run_setup_wizard_replays_changed_settings_after_modal_closes():
    addon = _addon_with_settings(
        {
            "setup_wizard_completed": "true",
            "nzbdav_url": "http://updated:3000",
            "filter_720p": "false",
        }
    )
    fresh_addon = MagicMock()

    with patch("resources.lib.setup_wizard.xbmcaddon.Addon", return_value=fresh_addon):
        with patch("resources.lib.setup_wizard.SetupWizardDialog") as dialog_cls:
            dialog = MagicMock()
            dialog.was_finished.return_value = True
            dialog.changed_setting_ids.return_value = ["nzbdav_url", "filter_720p"]
            dialog_cls.return_value = dialog

            with patch("resources.lib.setup_wizard._notify"), patch(
                "resources.lib.setup_wizard._addon_name", return_value="NZB-DAV"
            ), patch("resources.lib.setup_wizard._string", return_value="done"):
                assert setup_wizard.run_setup_wizard(addon)

    fresh_addon.setSetting.assert_any_call("nzbdav_url", "http://updated:3000")
    fresh_addon.setSetting.assert_any_call("filter_720p", "false")


def test_run_setup_wizard_replays_captured_values_when_addon_reads_are_stale():
    addon = _addon_with_settings(
        {
            "setup_wizard_completed": "true",
            "nzbdav_url": "http://old:3000",
            "filter_720p": "true",
        }
    )
    fresh_addon = MagicMock()

    with patch("resources.lib.setup_wizard.xbmcaddon.Addon", return_value=fresh_addon):
        with patch("resources.lib.setup_wizard.SetupWizardDialog") as dialog_cls:
            dialog = MagicMock()
            dialog.was_finished.return_value = True
            dialog.changed_setting_ids.return_value = ["nzbdav_url", "filter_720p"]
            dialog.changed_settings.return_value = {
                "nzbdav_url": "http://updated:3000",
                "filter_720p": "false",
            }
            dialog_cls.return_value = dialog

            with patch("resources.lib.setup_wizard._notify"), patch(
                "resources.lib.setup_wizard._addon_name", return_value="NZB-DAV"
            ), patch("resources.lib.setup_wizard._string", return_value="done"):
                assert setup_wizard.run_setup_wizard(addon)

    fresh_addon.setSetting.assert_any_call("nzbdav_url", "http://updated:3000")
    fresh_addon.setSetting.assert_any_call("filter_720p", "false")


def test_run_setup_wizard_force_closes_lingering_native_settings_before_replay():
    addon = _addon_with_settings({"setup_wizard_completed": "true"})
    fresh_addon = MagicMock()

    with patch("resources.lib.setup_wizard.xbmcaddon.Addon", return_value=fresh_addon):
        with patch("resources.lib.setup_wizard.SetupWizardDialog") as dialog_cls:
            dialog = MagicMock()
            dialog.was_finished.return_value = True
            dialog.changed_setting_ids.return_value = ["nzbdav_url"]
            dialog.changed_settings.return_value = {"nzbdav_url": "http://updated:3000"}
            dialog_cls.return_value = dialog

            close_requested = {"value": False}

            def active_dialog():
                return not close_requested["value"]

            def close_dialog(_command):
                close_requested["value"] = True

            with patch("resources.lib.setup_wizard._notify"), patch(
                "resources.lib.setup_wizard._native_settings_dialog_active",
                side_effect=active_dialog,
            ), patch("resources.lib.setup_wizard.xbmc.Monitor") as monitor_cls, patch(
                "resources.lib.setup_wizard.xbmc.executebuiltin"
            ) as executebuiltin:
                executebuiltin.side_effect = close_dialog
                monitor_cls.return_value.waitForAbort.return_value = False
                assert setup_wizard.run_setup_wizard(addon)

    executebuiltin.assert_any_call("Dialog.Close(addonsettings,true)")
    fresh_addon.setSetting.assert_any_call("nzbdav_url", "http://updated:3000")


def test_run_setup_wizard_closes_stale_addon_info_after_finish():
    addon = _addon_with_settings({"setup_wizard_completed": "true"})

    with patch("resources.lib.setup_wizard.SetupWizardDialog") as dialog_cls:
        dialog = MagicMock()
        dialog.was_finished.return_value = True
        dialog_cls.return_value = dialog

        with patch("resources.lib.setup_wizard._notify"), patch(
            "resources.lib.setup_wizard.xbmc.executebuiltin"
        ) as executebuiltin:
            assert setup_wizard.run_setup_wizard(addon)

    executebuiltin.assert_any_call("Dialog.Close(addoninformation,true)")


def test_run_setup_wizard_reopens_fresh_settings_after_replay():
    addon = _addon_with_settings({"setup_wizard_completed": "true"})
    fresh_addon = MagicMock()
    calls = []

    with patch("resources.lib.setup_wizard.xbmcaddon.Addon", return_value=fresh_addon):
        with patch("resources.lib.setup_wizard.SetupWizardDialog") as dialog_cls:
            dialog = MagicMock()
            dialog.was_finished.return_value = True
            dialog.changed_setting_ids.return_value = ["nzbdav_url"]
            dialog.changed_settings.return_value = {"nzbdav_url": "http://updated:3000"}
            dialog_cls.return_value = dialog

            def record_close_info():
                calls.append("close-info")

            def record_replay(_addon, _ids, _values):
                calls.append("replay")

            def record_reopen():
                calls.append("reopen")

            with patch("resources.lib.setup_wizard._notify"), patch(
                "resources.lib.setup_wizard._ensure_native_settings_dialog_closed"
            ), patch(
                "resources.lib.setup_wizard._should_reopen_fresh_settings_dialog",
                return_value=True,
            ), patch(
                "resources.lib.setup_wizard._close_stale_addon_info_dialog",
                side_effect=record_close_info,
            ), patch(
                "resources.lib.setup_wizard._replay_changed_settings",
                side_effect=record_replay,
            ), patch(
                "resources.lib.setup_wizard._reopen_fresh_settings_dialog",
                side_effect=record_reopen,
            ):
                assert setup_wizard.run_setup_wizard(addon)

    assert calls == ["replay", "close-info", "reopen"]


def test_reopen_fresh_settings_dialog_uses_addon_open_settings():
    with patch("resources.lib.setup_wizard.xbmc.Monitor") as monitor_cls, patch(
        "resources.lib.setup_wizard.xbmc.executebuiltin"
    ) as executebuiltin:
        monitor_cls.return_value.waitForAbort.return_value = False

        assert setup_wizard._reopen_fresh_settings_dialog()

    monitor_cls.return_value.waitForAbort.assert_called_once_with(0.1)
    executebuiltin.assert_called_once_with("Addon.OpenSettings(plugin.video.nzbdav)")


def test_run_setup_wizard_does_not_mark_completed_on_cancel():
    addon = _addon_with_settings()

    with patch("resources.lib.setup_wizard.xbmcaddon.Addon", return_value=addon):
        with patch("resources.lib.setup_wizard.SetupWizardDialog") as dialog_cls:
            dialog = MagicMock()
            dialog.was_finished.return_value = False
            dialog_cls.return_value = dialog

            assert not setup_wizard.run_setup_wizard()

    addon.setSetting.assert_not_called()


def test_dialog_tracks_settings_changed_inside_wizard():
    addon = _addon_with_settings()
    dialog = _wizard_dialog(addon)

    dialog._set_setting("nzbdav_url", "http://updated:3000")
    dialog._set_bool("filter_720p", False)
    with patch("resources.lib.setup_wizard._select_provider") as select_provider:
        dialog._select_provider("prowlarr")

    addon.setSetting.assert_any_call("nzbdav_url", "http://updated:3000")
    addon.setSetting.assert_any_call("filter_720p", "false")
    select_provider.assert_called_once_with(addon, "prowlarr")
    assert dialog.changed_setting_ids() == [
        "filter_720p",
        "nzbdav_url",
        "nzbhydra_enabled",
        "prowlarr_enabled",
    ]


def test_dialog_reads_persisted_setting_when_addon_setting_is_stale(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.xml").write_text(
        '<settings version="2"><setting id="nzbdav_url">http://disk:3000</setting></settings>',
        encoding="utf-8",
    )
    addon = _addon_with_settings({"nzbdav_url": "http://stale:3000"})
    addon.getAddonInfo.side_effect = lambda key: (
        str(profile) if key == "profile" else ""
    )
    dialog = _wizard_dialog(addon)

    assert dialog._setting_value("nzbdav_url") == "http://disk:3000"


def test_dialog_translates_special_profile_before_reading_persisted_setting(tmp_path):
    profile = tmp_path / "translated-profile"
    profile.mkdir()
    (profile / "settings.xml").write_text(
        '<settings version="2"><setting id="nzbdav_url">http://translated:3000</setting></settings>',
        encoding="utf-8",
    )
    addon = _addon_with_settings({"nzbdav_url": "http://stale:3000"})
    addon.getAddonInfo.side_effect = lambda key: (
        "special://profile/addon_data/plugin.video.nzbdav/" if key == "profile" else ""
    )
    dialog = _wizard_dialog(addon)

    with patch("xbmcvfs.translatePath", return_value=str(profile)):
        assert dialog._setting_value("nzbdav_url") == "http://translated:3000"


def test_page_sequence_matches_requested_setup_sections():
    titles = [page["title_id"] for page in setup_wizard.PAGES]

    assert titles == [
        30198,
        30216,
        30217,
        30218,
        30207,
        30208,
        30225,
        30209,
        30210,
        30211,
    ]


def test_wizard_copy_uses_requested_polish():
    assert setup_wizard._string(30198) == "NZB-DAV Kodi Addon"
    assert setup_wizard._string(30227) == "Step {0} of {1}"
    assert setup_wizard._string(30226) == "PLEASE INSTALL TMDBHELPER BEFORE CONTINUING"
    assert "streams NZB search results through nzbdav" in setup_wizard._string(30199)
    assert "URLs" in setup_wizard._string(30199)
    assert "API keys" in setup_wizard._string(30199)
    assert "credentials" in setup_wizard._string(30199)


def test_connection_pages_have_test_actions():
    pages_by_key = {page["key"]: page for page in setup_wizard.PAGES}

    assert pages_by_key["nzbdav"]["test"] == "nzbdav"
    assert pages_by_key["webdav"]["test"] == "webdav"
    assert pages_by_key["index_manager"]["test"] == "index_manager"


def test_populated_rows_do_not_include_inline_helper_text():
    addon = _addon_with_settings(
        {
            "nzbdav_url": "http://nzbdav.local",
            "webdav_url": "http://webdav.local",
        }
    )
    dialog = _wizard_dialog(addon)
    dialog.page_index = 1

    dialog._render_page()

    list_control = dialog.getControl(setup_wizard.LIST_ID)
    first_item = list_control.addItems.call_args.args[0][0]

    first_item.setProperty.assert_any_call("value", "http://nzbdav.local")
    first_item.setProperty.assert_any_call("helper", "")

    dialog.page_index = 2
    dialog._render_page()

    webdav_items = list_control.addItems.call_args.args[0]
    webdav_items[0].setProperty.assert_any_call("value", "http://webdav.local")
    webdav_items[0].setProperty.assert_any_call("helper", "")
    webdav_items[1].setProperty.assert_any_call("helper", "")


def test_final_page_copy_explains_finish_completes_setup_only():
    pages_by_key = {page["key"]: page for page in setup_wizard.PAGES}
    body = setup_wizard._string(pages_by_key["tmdbhelper"]["body_id"])

    assert "Click Finish to complete setup" in body
    assert "Install NZB-DAV Player" in body
    assert "addon settings" in body
    assert "Finish will also install" not in body


def test_final_page_uses_finish_label_for_install_action():
    addon = _addon_with_settings()
    dialog = setup_wizard.SetupWizardDialog(
        "setup-wizard.xml",
        "",
        "Default",
        "1080i",
        addon=addon,
    )
    dialog.page_index = len(setup_wizard.PAGES) - 1

    dialog._render_page()

    assert dialog.getProperty("wizard.next_label") == setup_wizard._string(30203)
    assert dialog.getProperty("wizard.next_visible") == "true"
    assert dialog.getProperty("wizard.test_visible") == "false"
    assert dialog.getProperty("wizard.cancel_visible") == "true"
    assert dialog._focus_id == setup_wizard.NEXT_BUTTON_ID


def test_welcome_page_focuses_next_button_because_it_has_no_rows():
    addon = _addon_with_settings()
    dialog = setup_wizard.SetupWizardDialog(
        "setup-wizard.xml",
        "",
        "Default",
        "1080i",
        addon=addon,
    )

    with patch("resources.lib.setup_wizard._tmdbhelper_installed", return_value=True):
        dialog.onInit()

    assert dialog._focus_id == setup_wizard.NEXT_BUTTON_ID


def test_test_page_syncs_native_footer_navigation_through_test_button():
    addon = _addon_with_settings()
    dialog = setup_wizard.SetupWizardDialog(
        "setup-wizard.xml",
        "",
        "Default",
        "1080i",
        addon=addon,
    )
    dialog.page_index = 1

    dialog._render_page()

    previous = dialog.getControl(setup_wizard.PREVIOUS_BUTTON_ID)
    test = dialog.getControl(setup_wizard.TEST_BUTTON_ID)
    next_button = dialog.getControl(setup_wizard.NEXT_BUTTON_ID)
    cancel = dialog.getControl(setup_wizard.CANCEL_BUTTON_ID)

    previous.controlRight.assert_called_with(test)
    test.controlLeft.assert_called_with(previous)
    test.controlRight.assert_called_with(next_button)
    next_button.controlLeft.assert_called_with(test)
    next_button.controlRight.assert_called_with(cancel)
    cancel.controlLeft.assert_called_with(next_button)


def test_test_page_moves_down_from_rows_to_test_button():
    addon = _addon_with_settings()
    dialog = setup_wizard.SetupWizardDialog(
        "setup-wizard.xml",
        "",
        "Default",
        "1080i",
        addon=addon,
    )
    dialog.page_index = 1

    dialog._render_page()

    list_control = dialog.getControl(setup_wizard.LIST_ID)
    test = dialog.getControl(setup_wizard.TEST_BUTTON_ID)

    list_control.controlDown.assert_called_with(test)


def test_non_test_page_moves_down_from_rows_to_next_button():
    addon = _addon_with_settings()
    dialog = setup_wizard.SetupWizardDialog(
        "setup-wizard.xml",
        "",
        "Default",
        "1080i",
        addon=addon,
    )
    dialog.page_index = 4

    dialog._render_page()

    list_control = dialog.getControl(setup_wizard.LIST_ID)
    next_button = dialog.getControl(setup_wizard.NEXT_BUTTON_ID)

    list_control.controlDown.assert_called_with(next_button)


def test_last_page_syncs_native_footer_navigation_through_finish_button():
    addon = _addon_with_settings()
    dialog = setup_wizard.SetupWizardDialog(
        "setup-wizard.xml",
        "",
        "Default",
        "1080i",
        addon=addon,
    )
    dialog.page_index = len(setup_wizard.PAGES) - 1

    dialog._render_page()

    previous = dialog.getControl(setup_wizard.PREVIOUS_BUTTON_ID)
    finish = dialog.getControl(setup_wizard.NEXT_BUTTON_ID)
    cancel = dialog.getControl(setup_wizard.CANCEL_BUTTON_ID)

    previous.controlRight.assert_called_with(finish)
    finish.controlLeft.assert_called_with(previous)
    finish.controlRight.assert_called_with(cancel)
    cancel.controlLeft.assert_called_with(finish)


def test_on_focus_tracks_current_control_id():
    dialog = _wizard_dialog()

    dialog.onFocus(setup_wizard.NEXT_BUTTON_ID)

    assert dialog._focus_id == setup_wizard.NEXT_BUTTON_ID


def test_select_provider_enables_one_provider_and_disables_the_other():
    addon = _addon_with_settings()

    setup_wizard._select_provider(addon, "prowlarr")

    addon.setSetting.assert_any_call("prowlarr_enabled", "true")
    addon.setSetting.assert_any_call("nzbhydra_enabled", "false")

    addon.reset_mock()
    setup_wizard._select_provider(addon, "hydra")

    addon.setSetting.assert_any_call("nzbhydra_enabled", "true")
    addon.setSetting.assert_any_call("prowlarr_enabled", "false")


def test_test_page_dispatches_selected_provider_connection_check():
    addon = _addon_with_settings({"prowlarr_enabled": "true"})
    dialog = _wizard_dialog(addon)
    dialog.page_index = 3

    with patch(
        "resources.lib.setup_wizard._connection_check", return_value=(True, "")
    ) as check:
        dialog._test_current_page()

    check.assert_called_once_with("index_manager", addon)


def test_test_page_shows_success_modal_on_successful_connection_check():
    addon = _addon_with_settings({"prowlarr_enabled": "true"})
    dialog = _wizard_dialog(addon)
    dialog.page_index = 3

    with patch(
        "resources.lib.setup_wizard._connection_check", return_value=(True, "")
    ), patch("resources.lib.setup_wizard.xbmcgui.Dialog") as dialog_cls:
        dialog._test_current_page()

    dialog_cls.return_value.ok.assert_called_once_with(
        "Search Provider", "Connection successful."
    )


def test_test_page_shows_failure_modal_with_reason_on_failed_connection_check():
    addon = _addon_with_settings({"prowlarr_enabled": "true"})
    dialog = _wizard_dialog(addon)
    dialog.page_index = 3

    with patch(
        "resources.lib.setup_wizard._connection_check",
        return_value=(False, "API key denied"),
    ), patch("resources.lib.setup_wizard.xbmcgui.Dialog") as dialog_cls:
        dialog._test_current_page()

    dialog_cls.return_value.ok.assert_called_once_with(
        "Search Provider", "Connection failed: API key denied"
    )


def test_connection_check_reports_empty_url_reason():
    addon = _addon_with_settings({"nzbdav_url": "", "nzbdav_api_key": "secret"})

    assert setup_wizard._connection_check("nzbdav", addon) == (
        False,
        "URL not configured",
    )


def test_connection_check_reports_api_key_denied_for_http_auth_errors():
    from urllib.error import HTTPError

    addon = _addon_with_settings(
        {"nzbdav_url": "http://nzbdav.local", "nzbdav_api_key": "secret"}
    )
    error = HTTPError("http://nzbdav.local/api", 403, "Forbidden", {}, None)

    with patch("resources.lib.http_util.http_get", side_effect=error):
        assert setup_wizard._connection_check("nzbdav", addon) == (
            False,
            "API key denied",
        )


def test_connection_check_redacts_secrets_from_failure_log():
    addon = _addon_with_settings(
        {"hydra_url": "http://hydra.local", "hydra_api_key": "SUPERSECRET123"}
    )
    error = RuntimeError(
        "failed URL http://hydra.local/api?apikey=SUPERSECRET123&t=movie"
    )

    with patch("resources.lib.http_util.http_get", side_effect=error), patch(
        "resources.lib.setup_wizard.xbmc"
    ) as mock_xbmc:
        setup_wizard._connection_check("hydra", addon)

    logged = mock_xbmc.log.call_args.args[0]
    assert "SUPERSECRET123" not in logged
    assert "apikey=REDACTED" in logged


def test_toggle_preserves_selected_row_position():
    addon = _addon_with_settings({"filter_2160p": "true", "filter_1080p": "true"})
    dialog = setup_wizard.SetupWizardDialog(
        "setup-wizard.xml",
        "",
        "Default",
        "1080i",
        addon=addon,
    )
    dialog.page_index = 4
    dialog._render_page()
    list_control = dialog.getControl(setup_wizard.LIST_ID)
    list_control.getSelectedPosition.return_value = 1
    list_control.selectItem.reset_mock()

    dialog._activate_selected_row()

    addon.setSetting.assert_called_with("filter_1080p", "false")
    list_control.selectItem.assert_called_with(1)
    assert dialog._focus_id == setup_wizard.LIST_ID


def test_cancelled_text_edit_preserves_existing_setting():
    addon = _addon_with_settings({"nzbdav_api_key": "existing-secret"})
    dialog = _wizard_dialog(addon)
    row = {"kind": "text", "setting": "nzbdav_api_key", "label_id": 30003}

    with patch("resources.lib.setup_wizard.xbmcgui.Dialog") as dialog_cls:
        dialog_cls.return_value.select.return_value = 0
        dialog_cls.return_value.input.return_value = ""
        dialog._edit_text(row)

    addon.setSetting.assert_not_called()


def test_populated_text_edit_can_be_explicitly_cleared():
    addon = _addon_with_settings({"webdav_password": "existing-password"})
    dialog = _wizard_dialog(addon)
    row = {"kind": "text", "setting": "webdav_password", "label_id": 30009}

    with patch("resources.lib.setup_wizard.xbmcgui.Dialog") as dialog_cls:
        dialog_cls.return_value.select.return_value = 1
        dialog._edit_text(row)

    dialog_cls.return_value.input.assert_not_called()
    addon.setSetting.assert_called_once_with("webdav_password", "")


def test_empty_text_edit_can_clear_empty_setting():
    addon = _addon_with_settings({"hydra_api_key": ""})
    dialog = _wizard_dialog(addon)
    row = {"kind": "text", "setting": "hydra_api_key", "label_id": 30003}

    with patch("resources.lib.setup_wizard.xbmcgui.Dialog") as dialog_cls:
        dialog_cls.return_value.input.return_value = ""
        dialog._edit_text(row)

    addon.setSetting.assert_called_once_with("hydra_api_key", "")


def test_tmdbhelper_missing_install_shows_message_without_installing():
    dialog = _wizard_dialog()
    dialog.close = MagicMock()

    with patch("resources.lib.setup_wizard._tmdbhelper_installed", return_value=False):
        with patch("resources.lib.player_installer.install_player") as install, patch(
            "resources.lib.setup_wizard.xbmcgui.Dialog"
        ) as dialog_cls:
            dialog._install_player()

    install.assert_not_called()
    dialog_cls.return_value.ok.assert_called_once()
    assert dialog._finished is False
    dialog.addon.setSetting.assert_not_called()
    dialog.close.assert_called_once()


def test_tmdbhelper_present_install_uses_existing_player_installer():
    dialog = _wizard_dialog()
    dialog.close = MagicMock()

    with patch("resources.lib.setup_wizard._tmdbhelper_installed", return_value=True):
        with patch("resources.lib.player_installer.install_player") as install, patch(
            "resources.lib.setup_wizard.xbmcgui.Dialog"
        ) as dialog_cls:
            dialog._install_player()

    install.assert_called_once()
    dialog_cls.return_value.ok.assert_called_once()
    assert dialog._finished is True
    dialog.addon.setSetting.assert_called_once_with("setup_wizard_completed", "true")
    dialog.close.assert_called_once()


def test_install_button_completion_marks_wizard_completed():
    addon = _addon_with_settings()
    dialog = _wizard_dialog(addon)
    dialog.close = MagicMock()

    with patch("resources.lib.setup_wizard._tmdbhelper_installed", return_value=True):
        with patch("resources.lib.player_installer.install_player"), patch(
            "resources.lib.setup_wizard.xbmcgui.Dialog"
        ):
            dialog._install_player()

    addon.setSetting.assert_called_once_with("setup_wizard_completed", "true")


@pytest.mark.parametrize(
    "page_index,expected_footer_ids",
    [
        (0, [setup_wizard.NEXT_BUTTON_ID, setup_wizard.CANCEL_BUTTON_ID]),
        (
            1,
            [
                setup_wizard.PREVIOUS_BUTTON_ID,
                setup_wizard.TEST_BUTTON_ID,
                setup_wizard.NEXT_BUTTON_ID,
                setup_wizard.CANCEL_BUTTON_ID,
            ],
        ),
        (
            4,
            [
                setup_wizard.PREVIOUS_BUTTON_ID,
                setup_wizard.NEXT_BUTTON_ID,
                setup_wizard.CANCEL_BUTTON_ID,
            ],
        ),
        (
            len(setup_wizard.PAGES) - 1,
            [
                setup_wizard.PREVIOUS_BUTTON_ID,
                setup_wizard.NEXT_BUTTON_ID,
                setup_wizard.CANCEL_BUTTON_ID,
            ],
        ),
    ],
)
def test_footer_control_ids_use_one_stable_footer(page_index, expected_footer_ids):
    dialog = _wizard_dialog()
    dialog.page_index = page_index

    assert (
        dialog._footer_control_ids(setup_wizard.PAGES[page_index])
        == expected_footer_ids
    )


def test_last_page_native_navigation_links_cancel_left_to_finish_button():
    addon = _addon_with_settings()
    dialog = setup_wizard.SetupWizardDialog(
        "setup-wizard.xml",
        "",
        "Default",
        "1080i",
        addon=addon,
    )
    dialog.page_index = len(setup_wizard.PAGES) - 1

    dialog._render_page()

    cancel = dialog.getControl(setup_wizard.CANCEL_BUTTON_ID)
    finish = dialog.getControl(setup_wizard.NEXT_BUTTON_ID)

    cancel.controlLeft.assert_called_with(finish)


def test_nav_back_cancels_wizard():
    dialog = _wizard_dialog()
    dialog._cancel = MagicMock()

    action = MagicMock()
    action.getId.return_value = setup_wizard.ACTION_NAV_BACK

    dialog.onAction(action)

    dialog._cancel.assert_called_once_with()


def test_finish_without_tmdbhelper_completes_without_installing_player():
    dialog = _wizard_dialog()
    dialog.page_index = len(setup_wizard.PAGES) - 1
    dialog.close = MagicMock()

    with patch("resources.lib.setup_wizard._tmdbhelper_installed") as installed:
        with patch("resources.lib.player_installer.install_player") as install, patch(
            "resources.lib.setup_wizard.xbmcgui.Dialog"
        ) as dialog_cls:
            dialog._next_or_finish()

    installed.assert_not_called()
    install.assert_not_called()
    dialog_cls.return_value.ok.assert_not_called()
    assert dialog._finished is True
    dialog.addon.setSetting.assert_called_once_with("setup_wizard_completed", "true")
    dialog.close.assert_called_once()


def test_unknown_provider_selection_is_rejected():
    with pytest.raises(ValueError):
        setup_wizard._select_provider(_addon_with_settings(), "unknown")
