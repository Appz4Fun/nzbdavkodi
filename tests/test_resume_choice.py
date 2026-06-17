# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the resume-or-restart choice helper."""

import json
from unittest.mock import MagicMock, patch

from resources.lib import resume_choice

# ---------------------------------------------------------------------------
# release_identity
# ---------------------------------------------------------------------------


def test_release_identity_full_metadata():
    """Title plus size and pubdate compose the full identity."""
    params = {
        "title": "Some%20Movie%202024",
        "_download_size": "1500000000",
        "_download_pubdate": "Mon, 01 Jan 2024 00:00:00 +0000",
    }
    assert release_identity_for(params) == (
        "Some Movie 2024|1500000000|Mon, 01 Jan 2024 00:00:00 +0000"
    )


def release_identity_for(params):
    return resume_choice.release_identity(params)


def test_release_identity_title_only_fallback():
    """Missing size/pubdate degrade to a title-anchored identity."""
    params = {"title": "Some%20Movie%202024"}
    assert resume_choice.release_identity(params) == "Some Movie 2024||"


def test_release_identity_empty_when_no_title():
    """No usable title yields an empty identity so callers skip resume."""
    assert resume_choice.release_identity({}) == ""
    assert resume_choice.release_identity({"title": ""}) == ""
    assert resume_choice.release_identity({"title": "   "}) == ""


def test_release_identity_is_deterministic():
    """The same params always map to the same identity."""
    params = {
        "title": "Some%20Movie",
        "_download_size": "42",
        "_download_pubdate": "yesterday",
    }
    first = resume_choice.release_identity(dict(params))
    second = resume_choice.release_identity(dict(params))
    assert first == second
    assert first == "Some Movie|42|yesterday"


def test_release_identity_decodes_title_via_unquote():
    """Title is unquoted the same way the resolver decodes it."""
    params = {"title": "The%20Matrix%20%281999%29"}
    assert resume_choice.release_identity(params) == "The Matrix (1999)||"


# ---------------------------------------------------------------------------
# format_resume_label
# ---------------------------------------------------------------------------


def test_format_resume_label_under_one_hour():
    """Positions below an hour render as MM:SS."""
    assert resume_choice.format_resume_label(0) == "00:00"
    assert resume_choice.format_resume_label(5) == "00:05"
    assert resume_choice.format_resume_label(65) == "01:05"
    assert resume_choice.format_resume_label(599) == "09:59"
    assert resume_choice.format_resume_label(3599) == "59:59"


def test_format_resume_label_one_hour_and_over():
    """Positions of an hour or more render as H:MM:SS."""
    assert resume_choice.format_resume_label(3600) == "1:00:00"
    assert resume_choice.format_resume_label(3661) == "1:01:01"
    assert resume_choice.format_resume_label(7322) == "2:02:02"


def test_format_resume_label_truncates_fractional_seconds():
    """Float positions are floored to whole seconds."""
    assert resume_choice.format_resume_label(65.9) == "01:05"
    assert resume_choice.format_resume_label(3600.9) == "1:00:00"


# ---------------------------------------------------------------------------
# native_resume_action
# ---------------------------------------------------------------------------


def _rpc_response(value):
    return json.dumps({"id": 1, "jsonrpc": "2.0", "result": {"value": value}})


def _rpc_error():
    return json.dumps({"id": 1, "jsonrpc": "2.0", "error": {"code": -32602}})


def _settings_rpc(values):
    """executeJSONRPC side_effect mapping setting id -> integer value.

    Ids absent from ``values`` respond with a JSON-RPC error, mirroring how
    Kodi answers a query for a setting that build does not have.
    """

    def _call(payload):
        setting = json.loads(payload)["params"]["setting"]
        if setting in values:
            return _rpc_response(values[setting])
        return _rpc_error()

    return _call


def _queried_settings(xbmc_mock):
    return [
        json.loads(call.args[0])["params"]["setting"]
        for call in xbmc_mock.executeJSONRPC.call_args_list
    ]


def test_native_resume_action_playaction_resume():
    """playaction RESUME (2) auto-resumes without consulting selectaction."""
    with patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.executeJSONRPC.side_effect = _settings_rpc({"myvideos.playaction": 2})
        assert resume_choice.native_resume_action() == "resume"
    assert _queried_settings(xbmc_mock) == ["myvideos.playaction"]


def test_native_resume_action_playaction_play_or_resume_is_ask():
    """playaction PLAY_OR_RESUME (1) prompts."""
    with patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.executeJSONRPC.side_effect = _settings_rpc({"myvideos.playaction": 1})
        assert resume_choice.native_resume_action() == "ask"


def test_native_resume_action_falls_back_to_selectaction_resume():
    """Without playaction, selectaction RESUME (2) still auto-resumes."""
    with patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.executeJSONRPC.side_effect = _settings_rpc(
            {"myvideos.selectaction": 2}
        )
        assert resume_choice.native_resume_action() == "resume"
    assert _queried_settings(xbmc_mock) == [
        "myvideos.playaction",
        "myvideos.selectaction",
    ]


def test_native_resume_action_selectaction_play_still_prompts():
    """Without playaction, selectaction Play (5) still offers resume (ask)."""
    with patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.executeJSONRPC.side_effect = _settings_rpc(
            {"myvideos.selectaction": 5}
        )
        assert resume_choice.native_resume_action() == "ask"


def test_native_resume_action_both_settings_missing_is_ask():
    """Neither setting present (older/locked Kodi) degrades to ask."""
    with patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.executeJSONRPC.side_effect = _settings_rpc({})
        assert resume_choice.native_resume_action() == "ask"


def test_native_resume_action_rpc_failure_is_ask():
    """A raising RPC call maps to ask without propagating the error."""
    with patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.executeJSONRPC.side_effect = RuntimeError("boom")
        assert resume_choice.native_resume_action() == "ask"


def test_native_resume_action_parse_failure_is_ask():
    """Malformed JSON maps to ask."""
    with patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.executeJSONRPC.return_value = "not json{"
        assert resume_choice.native_resume_action() == "ask"


def test_native_resume_action_prefers_playaction_setting():
    """The primary RPC asks Kodi for the resume-specific playaction setting."""
    with patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.executeJSONRPC.side_effect = _settings_rpc({"myvideos.playaction": 2})
        resume_choice.native_resume_action()
    first = json.loads(xbmc_mock.executeJSONRPC.call_args_list[0].args[0])
    assert first["method"] == "Settings.GetSettingValue"
    assert first["params"]["setting"] == "myvideos.playaction"


# ---------------------------------------------------------------------------
# choose_resume_seconds
# ---------------------------------------------------------------------------


def test_choose_resume_seconds_non_positive_returns_zero_no_prompt():
    """Zero or negative offsets short-circuit to 0.0 with no dialog."""
    dialog = MagicMock()
    with patch("resources.lib.resume_choice.native_resume_action") as action:
        assert resume_choice.choose_resume_seconds("id", 0, dialog=dialog) == 0.0
        assert resume_choice.choose_resume_seconds("id", -5, dialog=dialog) == 0.0
    action.assert_not_called()
    dialog.contextmenu.assert_not_called()


def test_choose_resume_seconds_action_resume_bypasses_dialog():
    """A native resume preference returns the offset without prompting."""
    dialog = MagicMock()
    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="resume"
    ):
        assert resume_choice.choose_resume_seconds("id", 90.0, dialog=dialog) == 90.0
    dialog.contextmenu.assert_not_called()


def test_choose_resume_seconds_action_beginning_bypasses_dialog():
    """A native beginning preference returns 0.0 without prompting."""
    dialog = MagicMock()
    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="beginning"
    ):
        assert resume_choice.choose_resume_seconds("id", 90.0, dialog=dialog) == 0.0
    dialog.contextmenu.assert_not_called()


def test_choose_resume_seconds_ask_index_zero_resumes():
    """Selecting the resume entry (index 0) returns the offset."""
    dialog = MagicMock()
    dialog.contextmenu.return_value = 0
    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="ask"
    ), patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.getLocalizedString.return_value = ""
        assert resume_choice.choose_resume_seconds("id", 90.0, dialog=dialog) == 90.0
    dialog.contextmenu.assert_called_once()


def test_choose_resume_seconds_ask_index_one_restarts():
    """Selecting the beginning entry (index 1) returns 0.0."""
    dialog = MagicMock()
    dialog.contextmenu.return_value = 1
    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="ask"
    ), patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.getLocalizedString.return_value = ""
        assert resume_choice.choose_resume_seconds("id", 90.0, dialog=dialog) == 0.0


def test_choose_resume_seconds_ask_cancel_returns_none():
    """Cancelling the dialog (index -1) returns None."""
    dialog = MagicMock()
    dialog.contextmenu.return_value = -1
    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="ask"
    ), patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.getLocalizedString.return_value = ""
        assert resume_choice.choose_resume_seconds("id", 90.0, dialog=dialog) is None


def test_choose_resume_seconds_ask_uses_localized_labels():
    """Non-empty built-ins fill the resume/beginning labels."""
    dialog = MagicMock()
    dialog.contextmenu.return_value = 0

    def _localized(string_id):
        return {12022: "Resume from %s", 12021: "Start from beginning"}[string_id]

    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="ask"
    ), patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.getLocalizedString.side_effect = _localized
        resume_choice.choose_resume_seconds("id", 3661.0, dialog=dialog)

    labels = dialog.contextmenu.call_args[0][0]
    assert labels == ["Resume from 1:01:01", "Start from beginning"]


def test_choose_resume_seconds_ask_falls_back_when_builtins_empty():
    """Empty built-in strings fall back to bundled English labels."""
    dialog = MagicMock()
    dialog.contextmenu.return_value = 0
    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="ask"
    ), patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.getLocalizedString.return_value = ""
        resume_choice.choose_resume_seconds("id", 65.0, dialog=dialog)

    labels = dialog.contextmenu.call_args[0][0]
    assert labels == ["Resume from 01:05", "Start from beginning"]


def test_choose_resume_seconds_ask_handles_kodi21_format_placeholder():
    """Kodi 21 core string #12022 is 'Resume from {0:s}' (str.format style),
    not printf '%s'. The label must fill without raising 'not all arguments
    converted during string formatting' (the crash in resolve_and_play)."""
    dialog = MagicMock()
    dialog.contextmenu.return_value = 0

    def _localized(string_id):
        return {12022: "Resume from {0:s}", 12021: "Play from beginning"}[string_id]

    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="ask"
    ), patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.getLocalizedString.side_effect = _localized
        result = resume_choice.choose_resume_seconds("id", 3661.0, dialog=dialog)

    assert result == 3661.0
    labels = dialog.contextmenu.call_args[0][0]
    assert labels == ["Resume from 1:01:01", "Play from beginning"]


def test_choose_resume_seconds_ask_handles_template_without_placeholder():
    """A localized template with no placeholder must not raise; append the time."""
    dialog = MagicMock()
    dialog.contextmenu.return_value = 0

    def _localized(string_id):
        return {12022: "Resume from", 12021: "Play from beginning"}[string_id]

    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="ask"
    ), patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.getLocalizedString.side_effect = _localized
        resume_choice.choose_resume_seconds("id", 65.0, dialog=dialog)

    labels = dialog.contextmenu.call_args[0][0]
    assert labels == ["Resume from 01:05", "Play from beginning"]


def test_choose_resume_seconds_does_not_clear_store_on_beginning():
    """Choosing beginning never touches resume_store."""
    dialog = MagicMock()
    dialog.contextmenu.return_value = 1
    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="ask"
    ), patch("resources.lib.resume_choice.xbmc") as xbmc_mock:
        xbmc_mock.getLocalizedString.return_value = ""
        # No resume_store import exists in the module, so there is nothing to
        # clear; the contract is simply "return 0.0 and leave storage alone".
        assert resume_choice.choose_resume_seconds("id", 90.0, dialog=dialog) == 0.0
    assert not hasattr(resume_choice, "resume_store")


def test_choose_resume_seconds_defaults_dialog_to_xbmcgui():
    """When no dialog is injected, xbmcgui.Dialog() is used."""
    with patch(
        "resources.lib.resume_choice.native_resume_action", return_value="ask"
    ), patch("resources.lib.resume_choice.xbmcgui") as gui_mock, patch(
        "resources.lib.resume_choice.xbmc"
    ) as xbmc_mock:
        xbmc_mock.getLocalizedString.return_value = ""
        gui_mock.Dialog.return_value.contextmenu.return_value = 0
        assert resume_choice.choose_resume_seconds("id", 90.0) == 90.0
    gui_mock.Dialog.return_value.contextmenu.assert_called_once()
