# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

from resources.lib.dead_candidates import DeadCandidates
from resources.lib.resolver import PollContext, _poll_with_release_retries
from resources.lib.router_play import _attach_retry_candidates


def _row(title, link, **extra):
    row = {"title": title, "link": link}
    row.update(extra)
    return row


def test_retry_candidates_are_distinct_and_capped():
    selected = _row("Movie.2026.1080p-GROUP", "https://indexer/primary")
    duplicate = _row(
        "Movie 2026 1080p GROUP [fallback-2-deadbeef]",
        "https://indexer/duplicate",
    )
    distinct = [
        _row(
            "Movie.2026.{}p-GROUP{}".format(720 + index, index),
            "https://i/{}".format(index),
        )
        for index in range(7)
    ]
    params = {}
    _attach_retry_candidates(params, selected, [selected, duplicate] + distinct)
    assert params["_retry_candidates"] == distinct[:5]


def test_release_retry_runs_only_after_provably_dead_attempt():
    dead = DeadCandidates()
    context = PollContext(dead=dead)
    dialog = MagicMock()

    def poll(url, _title, _dialog, _interval, _timeout, poll_ctx=None):
        if url.endswith("primary"):
            poll_ctx.dead.add(nzb_url=url)
            return None, None
        return "https://webdav/movie.mkv", {"Authorization": "Basic x"}

    with patch("resources.lib.resolver._poll_until_ready", side_effect=poll) as mocked:
        result = _poll_with_release_retries(
            "https://indexer/primary",
            "Movie.2026.1080p-GROUP",
            [_row("Movie.2026.2160p-GROUP", "https://indexer/retry")],
            dialog,
            2,
            600,
            context,
        )

    assert result[0] == "https://webdav/movie.mkv"
    assert [item.args[0] for item in mocked.call_args_list] == [
        "https://indexer/primary",
        "https://indexer/retry",
    ]


def test_release_retry_stops_after_nonterminal_failure():
    context = PollContext(dead=DeadCandidates())
    dialog = MagicMock()
    with patch(
        "resources.lib.resolver._poll_until_ready", return_value=(None, None)
    ) as mocked:
        result = _poll_with_release_retries(
            "https://indexer/primary",
            "Movie.2026.1080p-GROUP",
            [_row("Movie.2026.2160p-GROUP", "https://indexer/retry")],
            dialog,
            2,
            600,
            context,
        )
    assert result == (None, None)
    assert mocked.call_count == 1
