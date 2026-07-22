# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Picker-row preparation for the handle-based /play and /search flows."""

from unittest.mock import MagicMock, patch

from resources.lib.router_play import _prepare_picker_rows


def _result(title, reject=None):
    return {
        "title": title,
        "link": "http://example.com/{}".format(title),
        "size": "5000000000",
        "indexer": "test",
        "pubdate": "",
        "age": "1 day",
        "_filter_reject": reject,
    }


def _filter_results_stub(kept, hidden):
    """Mimic filter_results: same dict objects in both lists."""
    return kept, kept + hidden


def test_prepare_picker_rows_returns_filtered_and_all_rows():
    kept = [_result("Kept.1080p.x265")]
    hidden = [_result("Hidden.1080p.x264", reject="codec")]
    with patch(
        "resources.lib.filter.filter_results",
        return_value=_filter_results_stub(kept, hidden),
    ):
        prepared = _prepare_picker_rows(kept + hidden, "Movie", MagicMock(), None)
    picker_rows, all_rows, total_count = prepared
    assert picker_rows == kept
    assert all_rows == kept + hidden
    assert total_count == 2


def test_prepare_picker_rows_zero_survivors_no_prompt():
    """Zero survivors returns empty picker rows + full all-rows, no yes/no."""
    hidden = [_result("Hidden.1080p.x264", reject="codec")]
    notify = MagicMock()
    with patch(
        "resources.lib.filter.filter_results",
        return_value=_filter_results_stub([], hidden),
    ), patch("resources.lib.router_play.xbmcgui") as gui:
        prepared = _prepare_picker_rows(hidden, "Movie", notify, None)
    picker_rows, all_rows, _total = prepared
    assert picker_rows == []
    assert all_rows == hidden
    gui.Dialog.assert_not_called()
    notify.assert_not_called()


def test_prepare_picker_rows_nothing_at_all_notifies_and_stops():
    notify = MagicMock()
    with patch("resources.lib.filter.filter_results", return_value=([], [])):
        prepared = _prepare_picker_rows([], "Movie", notify, None)
    assert prepared is None
    notify.assert_called_once()


def test_prepare_picker_rows_prepends_pack_to_both_views():
    pack = {"_season_pack": {"backend": "nzbdav"}, "title": "Pack", "link": ""}
    kept = [_result("Kept.1080p.x265")]
    hidden = [_result("Hidden.1080p.x264", reject="codec")]
    with patch(
        "resources.lib.filter.filter_results",
        return_value=_filter_results_stub(kept, hidden),
    ):
        prepared = _prepare_picker_rows(kept + hidden, "Movie", MagicMock(), pack)
    picker_rows, all_rows, total_count = prepared
    assert picker_rows[0] is pack and all_rows[0] is pack
    assert picker_rows[1:] == kept
    assert all_rows[1:] == kept + hidden
    assert total_count == 3  # 2 provider results + the pack row


def test_deleted_prompt_helpers_are_gone():
    from resources.lib import router_play

    assert not hasattr(router_play, "_filtered_or_prompt")
    assert not hasattr(router_play, "_available_filtered_rows")
