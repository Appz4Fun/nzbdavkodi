# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

from resources.lib.results_dialog import (
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


# ---------------------------------------------------------------------------
# show_results_dialog
# ---------------------------------------------------------------------------


def test_show_results_dialog_returns_none_on_cancel():
    """show_results_dialog returns None when user cancels (selected_index -1)."""
    results = [_make_result()]

    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = -1
        MockDialog.return_value = mock_instance

        result = show_results_dialog(
            results, title="Movie", year="2024", total_count=10
        )
        assert result is None


def test_show_results_dialog_returns_selected():
    """show_results_dialog returns selected result dict when user picks a row."""
    selected = _make_result(link="http://nzb/123")
    results = [selected]

    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = 0
        MockDialog.return_value = mock_instance

        result = show_results_dialog(results, title="Movie", year="2024", total_count=1)
        assert result == selected


def test_show_results_dialog_calls_doModal():
    """show_results_dialog must call doModal() on the dialog."""
    results = [_make_result()]

    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = -1
        MockDialog.return_value = mock_instance

        show_results_dialog(results)
        mock_instance.doModal.assert_called_once()


def test_show_results_dialog_empty_results_returns_none():
    """show_results_dialog returns None for empty results list."""
    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = -1
        MockDialog.return_value = mock_instance

        result = show_results_dialog([], title="Movie", year="2024", total_count=0)
        assert result is None


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
