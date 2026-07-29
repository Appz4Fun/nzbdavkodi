# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from resources.lib.resolver import (
    _POLL_CONTINUE,
    PollContext,
    _poll_observation_unavailable,
    _poll_until_ready,
)


def _run_unobservable_poll(settings_getter=None):
    dialog = MagicMock()
    monitor = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "resources.lib.resolver._existing_completed_stream",
                return_value=None,
            )
        )
        stack.enter_context(
            patch(
                "resources.lib.resolver_pollloop._submit_and_announce",
                return_value="job-1",
            )
        )
        stack.enter_context(
            patch("resources.lib.resolver.xbmc.Monitor", return_value=monitor)
        )
        stack.enter_context(
            patch("resources.lib.resolver.time.time", return_value=1000)
        )
        stack.enter_context(
            patch("resources.lib.resolver.time.monotonic", side_effect=[0, 0])
        )
        stack.enter_context(
            patch("resources.lib.resolver._POLL_OBSERVABILITY_TIMEOUT_SECONDS", 0)
        )
        stack.enter_context(
            patch("resources.lib.resolver._abort_poll_before_fetch", return_value=False)
        )
        stack.enter_context(
            patch(
                "resources.lib.resolver._poll_once",
                return_value=(None, None, "server_error"),
            )
        )
        stack.enter_context(
            patch(
                "resources.lib.resolver_pollloop._poll_observation_unavailable",
                return_value=True,
            )
        )
        stack.enter_context(
            patch(
                "resources.lib.resolver._handle_job_status",
                return_value=(False, "Queued"),
            )
        )
        stack.enter_context(
            patch(
                "resources.lib.resolver._handle_history_result",
                return_value=(False, None, None, 0),
            )
        )
        stack.enter_context(
            patch("resources.lib.resolver._handle_webdav_error", return_value=False)
        )
        stack.enter_context(
            patch(
                "resources.lib.resolver._poll_wait_after_status",
                return_value=(1, 0),
            )
        )
        stack.enter_context(
            patch(
                "resources.lib.resolver_pollloop._wait_between_polls",
                return_value=_POLL_CONTINUE,
            )
        )
        notify = stack.enter_context(patch("resources.lib.resolver._notify"))
        dialog_factory = stack.enter_context(
            patch("resources.lib.resolver.xbmcgui.Dialog")
        )
        cancel_job = stack.enter_context(patch("resources.lib.resolver.cancel_job"))
        result = _poll_until_ready(
            "https://indexer.invalid/release",
            "Release",
            dialog,
            1,
            3600,
            poll_ctx=PollContext(settings_getter=settings_getter),
        )
    return result, notify, dialog_factory, cancel_job


def test_poll_observation_classifier_requires_total_backend_failure():
    assert _poll_observation_unavailable(None, None, "server_error")
    assert _poll_observation_unavailable(None, None, "connection_error")
    assert not _poll_observation_unavailable({"status": "Queued"}, None, "server_error")
    assert not _poll_observation_unavailable(None, {"status": "Failed"}, "server_error")
    assert not _poll_observation_unavailable(None, None, None)


def test_stuck_queued_backend_errors_stop_and_surface_final_notification():
    result, notify, dialog_factory, cancel_job = _run_unobservable_poll(
        settings_getter=MagicMock()
    )
    assert result == (None, None)
    notify.assert_called_once()
    dialog_factory.assert_not_called()
    cancel_job.assert_not_called()


def test_stuck_queued_handle_path_surfaces_final_modal_without_remote_cancel():
    result, notify, dialog_factory, cancel_job = _run_unobservable_poll()
    assert result == (None, None)
    notify.assert_not_called()
    dialog_factory.return_value.ok.assert_called_once()
    cancel_job.assert_not_called()
