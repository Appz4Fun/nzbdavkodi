# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

from resources.lib.results_dialog import (
    _AVAILABLE_LABEL,
    _BG_A,
    _BG_B,
    ACTION_CONTEXT_MENU,
    ACTION_SELECT,
    LIST_ID,
    ResultsDialog,
    _available_text,
    _build_result_item,
    _format_date,
    _format_size,
    _lang_short,
    show_results_dialog,
)


def _make_result(**overrides):
    base = {
        "title": "Movie.2024.1080p.x264",
        "size": "5000000000",
        "indexer": "test",
        "age": "1 day",
        "_meta": {
            "resolution": "1080p",
            "codec": "x264",
            "hdr": [],
            "audio": [],
            "languages": [],
            "group": "",
            "quality": "WEB-DL",
        },
    }
    base.update(overrides)
    return base


def test_available_label_is_ascii_for_skin_compatibility():
    assert _AVAILABLE_LABEL == "DL"
    assert _AVAILABLE_LABEL.isascii()


def test_available_label_renders_green():
    assert _available_text() == "[COLOR FF22C55E]DL[/COLOR]"


def test_build_result_item_sets_alternating_row_backgrounds():
    first_item = MagicMock()
    second_item = MagicMock()
    third_item = MagicMock()

    with patch(
        "resources.lib.results_dialog.xbmcgui.ListItem",
        side_effect=[first_item, second_item, third_item],
    ):
        _build_result_item(_make_result(), 0)
        _build_result_item(_make_result(), 1)
        _build_result_item(_make_result(), 2)

    first_item.setProperty.assert_any_call("row_bg", _BG_A)
    second_item.setProperty.assert_any_call("row_bg", _BG_B)
    third_item.setProperty.assert_any_call("row_bg", _BG_A)


def test_build_result_item_prefers_internal_display_title():
    item = MagicMock()
    result = _make_result(
        title="Spider-Noir.S01.Pack",
        _display_title="Already downloaded season pack - Episodes 1-8",
    )

    with patch(
        "resources.lib.results_dialog.xbmcgui.ListItem", return_value=item
    ) as cls:
        _build_result_item(result, 0)

    cls.assert_called_once_with(label="Already downloaded season pack - Episodes 1-8")


def test_pack_result_does_not_invent_sdr_or_mkv_metadata():
    item = MagicMock()
    result = _make_result(
        _meta={},
        _season_pack={"backend": "nzbdav", "job_id": "nzo-1"},
    )

    with patch("resources.lib.results_dialog.xbmcgui.ListItem", return_value=item):
        _build_result_item(result, 0)

    item.setProperty.assert_any_call("hdr", "")
    item.setProperty.assert_any_call("container", "")


def test_ordinary_result_keeps_sdr_and_mkv_defaults():
    item = MagicMock()
    result = _make_result(_meta={})

    with patch("resources.lib.results_dialog.xbmcgui.ListItem", return_value=item):
        _build_result_item(result, 0)

    item.setProperty.assert_any_call("hdr", "[COLOR FF6B7280]SDR[/COLOR]")
    item.setProperty.assert_any_call("container", "[COLOR FF34D399]MKV[/COLOR]")


# ---------------------------------------------------------------------------
# show_results_dialog (returns the selected dict from the dialog)
# ---------------------------------------------------------------------------


def test_show_results_dialog_returns_none_on_cancel():
    results = [_make_result()]
    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_result.return_value = None
        MockDialog.return_value = mock_instance
        assert show_results_dialog(results, title="Movie") is None
        mock_instance.doModal.assert_called_once()


def test_show_results_dialog_returns_selected_dict():
    selected = _make_result(link="http://nzb/123")
    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_result.return_value = selected
        MockDialog.return_value = mock_instance
        assert show_results_dialog([selected], title="Movie") is selected


def test_show_results_dialog_threads_all_results_kwarg():
    results = [_make_result()]
    all_results = [results[0], _make_result(title="Hidden.2024.720p")]
    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_result.return_value = None
        MockDialog.return_value = mock_instance
        show_results_dialog(results, all_results=all_results)
        assert MockDialog.call_args.kwargs["all_results"] is all_results


# ---------------------------------------------------------------------------
# ResultsDialog toggle behavior (real class via _FakeWindowXMLDialog)
# ---------------------------------------------------------------------------


class _Action:  # pylint: disable=too-few-public-methods
    def __init__(self, action_id):
        self._id = action_id

    def getId(self):
        return self._id


def _make_dialog(results, all_results):
    return ResultsDialog(
        "results-dialog.xml",
        "",
        "Default",
        "1080i",
        results=results,
        title="Movie",
        year="2024",
        total_count=len(all_results),
        all_results=all_results,
    )


def _row(title, reject=None):
    row = _make_result(title=title)
    row["_filter_reject"] = reject
    return row


def test_dialog_opens_filtered_and_toggles_to_show_all():
    kept = _row("Kept.2024.1080p.x265")
    hidden = _row("Hidden.2024.1080p.x264", reject="codec")
    dialog = _make_dialog([kept], [hidden, kept])
    dialog.onInit()

    list_control = dialog.getControl(LIST_ID)
    first_items = list_control.addItems.call_args.args[0]
    assert len(first_items) == 1
    assert "1 of" in dialog.getProperty("filter_info")
    assert "Show all (1 hidden)" in dialog.getProperty("footer_hints")

    list_control.getSelectedPosition.return_value = 0
    dialog.onAction(_Action(ACTION_CONTEXT_MENU))

    second_items = list_control.addItems.call_args.args[0]
    assert len(second_items) == 2
    assert dialog.getProperty("filter_info") == "Showing all 2 sources (filters off)"
    assert "Show filtered" in dialog.getProperty("footer_hints")
    assert dialog._closed is False


def test_toggle_preserves_focused_row_by_identity():
    row_a = _row("A.2024.1080p.x265")
    row_b = _row("B.2024.1080p.x265")
    hidden = _row("H.2024.1080p.x264", reject="codec")
    dialog = _make_dialog([row_a, row_b], [hidden, row_a, row_b])
    dialog.onInit()

    list_control = dialog.getControl(LIST_ID)
    list_control.getSelectedPosition.return_value = 1  # row_b focused
    dialog.onAction(_Action(ACTION_CONTEXT_MENU))
    list_control.selectItem.assert_called_with(2)  # row_b's index in show-all


def test_selection_from_show_all_returns_hidden_row():
    kept = _row("Kept.2024.1080p.x265")
    hidden = _row("Hidden.2024.1080p.x264", reject="codec")
    dialog = _make_dialog([kept], [hidden, kept])
    dialog.onInit()

    list_control = dialog.getControl(LIST_ID)
    list_control.getSelectedPosition.return_value = 0
    dialog.onAction(_Action(ACTION_CONTEXT_MENU))  # now showing all
    list_control.getSelectedPosition.return_value = 0  # the hidden row
    dialog.onAction(_Action(ACTION_SELECT))

    assert dialog.get_selected_result() is hidden
    assert dialog._closed is True


def test_context_menu_cancels_when_no_all_results():
    kept = _row("Kept.2024.1080p.x265")
    dialog = ResultsDialog(
        "results-dialog.xml",
        "",
        "Default",
        "1080i",
        results=[kept],
        title="Movie",
        total_count=1,
    )
    dialog.onInit()
    dialog.onAction(_Action(ACTION_CONTEXT_MENU))
    assert dialog._closed is True
    assert dialog.get_selected_result() is None


def test_zero_survivors_opens_in_show_all_with_toggle_disabled():
    hidden = _row("Hidden.2024.1080p.x264", reject="codec")
    dialog = _make_dialog([], [hidden])
    dialog.onInit()

    items = dialog.getControl(LIST_ID).addItems.call_args.args[0]
    assert len(items) == 1
    assert dialog.getProperty("filter_info") == "Showing all 1 sources (filters off)"
    assert (
        dialog.getProperty("footer_hints") == "[Enter] Download & Play     [Esc] Back"
    )
    dialog.onAction(_Action(ACTION_CONTEXT_MENU))  # no toggle -> cancel
    assert dialog._closed is True


def test_build_result_item_sets_filter_reason_chip():
    item = MagicMock()
    result = _make_result()
    result["_filter_reject"] = "codec"
    with patch("resources.lib.results_dialog.xbmcgui.ListItem", return_value=item):
        _build_result_item(result, 0)
    item.setProperty.assert_any_call(
        "filter_reason", "[COLOR FFFBBF24]FILTERED: codec[/COLOR]"
    )


def test_build_result_item_blank_filter_reason_for_kept_rows():
    item = MagicMock()
    with patch("resources.lib.results_dialog.xbmcgui.ListItem", return_value=item):
        _build_result_item(_make_result(), 0)
    item.setProperty.assert_any_call("filter_reason", "")


# ---------------------------------------------------------------------------
# _format_size
# ---------------------------------------------------------------------------


def test_format_size_gigabytes():
    assert _format_size(2 * 1024**3) == "2.0 GB"


def test_format_size_megabytes():
    assert _format_size(512 * 1024**2) == "512.0 MB"


def test_format_size_bytes():
    assert _format_size(1000) == "1000 B"


def test_format_size_none_returns_empty():
    assert _format_size(None) == ""


def test_format_size_zero_returns_empty():
    assert _format_size(0) == ""


def test_format_size_string_input():
    """_format_size should accept string input (as received from parsed NZB data)."""
    assert _format_size("1073741824") == "1.0 GB"


def test_format_size_malformed_string_returns_empty():
    """A malformed provider size must not crash the result picker."""
    assert _format_size("unknown") == ""


# ---------------------------------------------------------------------------
# _format_date
# ---------------------------------------------------------------------------


def test_format_date_rfc2822():
    result = _format_date("Mon, 01 Jan 2024 00:00:00 +0000")
    assert result == "2024-01-01"


def test_format_date_empty_returns_empty():
    assert _format_date("") == ""


def test_format_date_none_returns_empty():
    assert _format_date(None) == ""


def test_format_date_fallback_truncates():
    """For unparseable dates, return first 10 chars."""
    result = _format_date("2024-06-15 extra garbage")
    assert result == "2024-06-15"


# ---------------------------------------------------------------------------
# _lang_short
# ---------------------------------------------------------------------------


def test_lang_short_known_language():
    assert _lang_short("English") == "EN"
    assert _lang_short("French") == "FR"
    assert _lang_short("Japanese") == "JA"


def test_lang_short_unknown_language_uppercases_first_two():
    assert _lang_short("Klingon") == "KL"


def test_lang_short_empty_returns_empty():
    assert _lang_short("") == ""
