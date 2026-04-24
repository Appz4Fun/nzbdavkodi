# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for cache_prompt — first-play advancedsettings.xml dialog."""

from unittest.mock import MagicMock, patch

from resources.lib.cache_prompt import (
    maybe_show_cache_prompt,
    should_show_cache_prompt,
)


def test_should_show_when_remux_triggered_and_cache_missing():
    assert (
        should_show_cache_prompt(
            stream_remux=True,
            cache_is_set=False,
            session_shown=False,
            persistent_dismissed=False,
        )
        is True
    )


def test_should_not_show_when_remux_not_triggered():
    """Small file -- force_remux didn't kick in, so passthrough would
    make no difference. No prompt."""
    assert (
        should_show_cache_prompt(
            stream_remux=False,
            cache_is_set=False,
            session_shown=False,
            persistent_dismissed=False,
        )
        is False
    )


def test_should_not_show_when_cache_is_already_set():
    """User already has cache=0 applied -- passthrough runs natively,
    no need to prompt."""
    assert (
        should_show_cache_prompt(
            stream_remux=True,
            cache_is_set=True,
            session_shown=False,
            persistent_dismissed=False,
        )
        is False
    )


def test_should_not_show_when_session_already_shown():
    """Once-per-Kodi-session semantics: the dialog doesn't re-fire on
    subsequent large-file plays within the same session."""
    assert (
        should_show_cache_prompt(
            stream_remux=True,
            cache_is_set=False,
            session_shown=True,
            persistent_dismissed=False,
        )
        is False
    )


def test_should_not_show_when_user_said_never_ask():
    """Persistent 'Never ask' dismissal survives Kodi restarts."""
    assert (
        should_show_cache_prompt(
            stream_remux=True,
            cache_is_set=False,
            session_shown=False,
            persistent_dismissed=True,
        )
        is False
    )


def _make_addon(dismissed="false"):
    addon = MagicMock()
    values = {"cache_dialog_dismissed": dismissed}
    addon.getSetting.side_effect = lambda key: values.get(key, "")
    return addon


def _make_window(shown=False):
    window = MagicMock()
    props = {"nzbdav.cache_dialog.shown_this_session": "true" if shown else ""}
    window.getProperty.side_effect = lambda key: props.get(key, "")
    return window


@patch("resources.lib.cache_prompt.xbmcgui")
@patch("resources.lib.cache_prompt.xbmcaddon")
@patch("resources.lib.cache_prompt.has_cache_memorysize_zero")
def test_maybe_show_fires_dialog_and_marks_session_shown(
    mock_has_cache, mock_xbmcaddon, mock_xbmcgui
):
    """Happy path: dialog conditions met -> yesnocustom fires, window
    property flips to true so subsequent plays in the session skip."""
    mock_has_cache.return_value = False
    addon = _make_addon(dismissed="false")
    mock_xbmcaddon.Addon.return_value = addon
    window = _make_window(shown=False)
    mock_xbmcgui.Window.return_value = window
    dialog = MagicMock()
    dialog.yesnocustom.return_value = 0  # Not now
    mock_xbmcgui.Dialog.return_value = dialog

    stream_info = {"remux": True, "total_bytes": 58 * 1024**3}
    maybe_show_cache_prompt(stream_info)

    dialog.yesnocustom.assert_called_once()
    window.setProperty.assert_any_call("nzbdav.cache_dialog.shown_this_session", "true")


@patch("resources.lib.cache_prompt.xbmcgui")
@patch("resources.lib.cache_prompt.xbmcaddon")
@patch("resources.lib.cache_prompt.has_cache_memorysize_zero")
def test_maybe_show_never_ask_sets_persistent_flag(
    mock_has_cache, mock_xbmcaddon, mock_xbmcgui
):
    """User clicks the 'Never ask' button (return code 2) -> the
    persistent cache_dialog_dismissed setting flips to true."""
    mock_has_cache.return_value = False
    addon = _make_addon(dismissed="false")
    mock_xbmcaddon.Addon.return_value = addon
    window = _make_window(shown=False)
    mock_xbmcgui.Window.return_value = window
    dialog = MagicMock()
    dialog.yesnocustom.return_value = 2  # Never ask
    mock_xbmcgui.Dialog.return_value = dialog

    stream_info = {"remux": True, "total_bytes": 58 * 1024**3}
    maybe_show_cache_prompt(stream_info)

    addon.setSetting.assert_any_call("cache_dialog_dismissed", "true")


@patch("resources.lib.cache_prompt._show_instructions_dialog")
@patch("resources.lib.cache_prompt.xbmcgui")
@patch("resources.lib.cache_prompt.xbmcaddon")
@patch("resources.lib.cache_prompt.has_cache_memorysize_zero")
def test_maybe_show_show_instructions_opens_instructions_dialog(
    mock_has_cache, mock_xbmcaddon, mock_xbmcgui, mock_instructions
):
    """User clicks 'Show instructions' (return code 1) -> the second
    dialog with the XML snippet is opened."""
    mock_has_cache.return_value = False
    addon = _make_addon(dismissed="false")
    mock_xbmcaddon.Addon.return_value = addon
    window = _make_window(shown=False)
    mock_xbmcgui.Window.return_value = window
    dialog = MagicMock()
    dialog.yesnocustom.return_value = 1  # Show instructions
    mock_xbmcgui.Dialog.return_value = dialog

    stream_info = {"remux": True, "total_bytes": 58 * 1024**3}
    maybe_show_cache_prompt(stream_info)

    mock_instructions.assert_called_once()
    # persistent flag NOT set on Show instructions
    for call in addon.setSetting.call_args_list:
        assert call.args[0] != "cache_dialog_dismissed"


@patch("resources.lib.cache_prompt.xbmcgui")
@patch("resources.lib.cache_prompt.xbmcaddon")
@patch("resources.lib.cache_prompt.has_cache_memorysize_zero")
def test_maybe_show_skips_when_remux_false(
    mock_has_cache, mock_xbmcaddon, mock_xbmcgui
):
    """Direct-play / faststart path (remux=False) -> no dialog."""
    mock_has_cache.return_value = False
    mock_xbmcaddon.Addon.return_value = _make_addon(dismissed="false")
    mock_xbmcgui.Window.return_value = _make_window(shown=False)
    dialog = MagicMock()
    mock_xbmcgui.Dialog.return_value = dialog

    maybe_show_cache_prompt({"remux": False, "total_bytes": 0})

    dialog.yesnocustom.assert_not_called()


@patch("resources.lib.cache_prompt.xbmcgui")
@patch("resources.lib.cache_prompt.xbmcaddon")
@patch("resources.lib.cache_prompt.has_cache_memorysize_zero")
def test_maybe_show_skips_when_persistent_dismissed(
    mock_has_cache, mock_xbmcaddon, mock_xbmcgui
):
    """User previously picked 'Never ask' -> no dialog on any file."""
    mock_has_cache.return_value = False
    mock_xbmcaddon.Addon.return_value = _make_addon(dismissed="true")
    mock_xbmcgui.Window.return_value = _make_window(shown=False)
    dialog = MagicMock()
    mock_xbmcgui.Dialog.return_value = dialog

    maybe_show_cache_prompt({"remux": True, "total_bytes": 58 * 1024**3})

    dialog.yesnocustom.assert_not_called()
