# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Picker-row preparation for the RunScript (TMDBHelper) play flow."""

from unittest.mock import MagicMock, patch

from resources.lib.router_scriptplay import (
    _script_play_available_rows,
    _script_play_filter_autoselect_tag,
    _script_play_picker_and_resolve,
)


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


def test_available_rows_zero_survivors_returns_filtered_without_prompt():
    hidden = [_result("Hidden.1080p.x264", reject="codec")]
    notify = MagicMock()
    assert _script_play_available_rows([], hidden, "Movie", notify, None) == []
    notify.assert_not_called()


def test_available_rows_nothing_at_all_notifies_and_stops():
    notify = MagicMock()
    rows = _script_play_available_rows([], [], "Movie", notify, None)
    assert rows is None
    notify.assert_called_once()


def test_deleted_scriptplay_prompt_helper_is_gone():
    import resources.lib.router_scriptplay as scriptplay

    assert not hasattr(scriptplay, "_script_play_filtered_or_prompt")


@patch("resources.lib.router_scriptplay._script_play_completed_jobs")
def test_filter_autoselect_tag_returns_all_rows_and_tags_superset(mock_jobs):
    import resources.lib.router as _router

    kept = [_result("Kept.1080p.x265")]
    hidden = [_result("Hidden.1080p.x264", reject="codec")]
    mock_jobs.return_value = {"done": True}
    with patch(
        "resources.lib.filter.filter_results", return_value=(kept, kept + hidden)
    ), patch.object(_router, "_get_script_setting", lambda key, default="": default):
        prepared = _script_play_filter_autoselect_tag(
            None, {}, kept + hidden, "Movie", MagicMock(), pack_result=None
        )
    filtered, all_rows, total_count, completed_jobs = prepared
    assert filtered == kept
    assert all_rows == kept + hidden
    assert total_count == 2
    assert completed_jobs == {"done": True}
    # The tagging pass must receive the SUPERSET, not just the filtered rows.
    assert mock_jobs.call_args.args[1] == kept + hidden


def test_picker_and_resolve_passes_all_results():
    kept = [_result("Kept.1080p.x265")]
    all_rows = [kept[0], _result("Hidden.1080p.x264", reject="codec")]
    with patch(
        "resources.lib.results_dialog.show_results_dialog", return_value=None
    ) as dialog:
        _script_play_picker_and_resolve({}, kept, all_rows, "Movie", "2000", 2, None)
    assert dialog.call_args.kwargs["all_results"] is all_rows
