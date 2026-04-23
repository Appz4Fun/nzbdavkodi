# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import patch

from resources.lib.resolver import resolve


def test_resolve_aborts_on_nzbdav_failed_status(resolver_mocks):
    """When nzbdav reports job Failed, resolve() should show error dialog and
    call setResolvedUrl(False)."""
    resolver_mocks.submit.return_value = ("SABnzbd_nzo_failed", None)
    resolver_mocks.status.return_value = {"status": "Downloading", "percentage": "20"}
    resolver_mocks.history.return_value = {
        "status": "Failed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Failed",
        "name": "Failed Job",
    }

    resolve(1, {"nzburl": "http://hydra/getnzb/fail", "title": "failed.mkv"})

    resolver_mocks.plugin.setResolvedUrl.assert_called_once_with(
        1, False, resolver_mocks.gui.ListItem()
    )
    resolver_mocks.gui.Dialog.return_value.ok.assert_called_once()


def test_resolve_times_out_gracefully(resolver_mocks):
    """When polling exceeds timeout, resolve() should notify and not hang
    even if status calls return None."""
    resolver_mocks.poll.return_value = (1, 5)  # override: 5s timeout
    resolver_mocks.submit.return_value = ("SABnzbd_nzo_timeout", None)
    resolver_mocks.status.return_value = None  # simulate connection timeout
    resolver_mocks.history.return_value = None
    resolver_mocks.probe.return_value = (False, "connection_error")
    # Override the pinned time so the timeout branch actually fires.
    resolver_mocks.time.time.side_effect = [0.0, 6.0]

    resolve(1, {"nzburl": "http://hydra/getnzb/timeout", "title": "timeout.mkv"})

    resolver_mocks.plugin.setResolvedUrl.assert_called_once_with(
        1, False, resolver_mocks.gui.ListItem()
    )
    resolver_mocks.gui.Dialog.return_value.ok.assert_called_once()


def test_resolve_aborts_on_webdav_auth_failed_when_nzbdav_apis_silent(resolver_mocks):
    """Primary C3 regression test: when both nzbdav APIs return None and
    the WebDAV probe reports auth_failed, the resolver must show the
    auth dialog and call setResolvedUrl(False) within a single poll
    iteration — not spin until the download timeout."""
    resolver_mocks.submit.return_value = ("SABnzbd_nzo_silent", None)
    # Both nzbdav APIs silent — triggers the probe branch in _poll_once.
    resolver_mocks.status.return_value = None
    resolver_mocks.history.return_value = None
    # The newly-classified auth failure case that used to be "not_found".
    resolver_mocks.probe.return_value = (False, "auth_failed")

    resolve(1, {"nzburl": "http://hydra/getnzb/silent", "title": "silent.mkv"})

    # The auth dialog fired.
    resolver_mocks.gui.Dialog.return_value.ok.assert_called_once()
    # Resolve aborted.
    resolver_mocks.plugin.setResolvedUrl.assert_called_once_with(
        1, False, resolver_mocks.gui.ListItem()
    )
    # The probe was actually reached — proves the code path the test
    # claims to cover.
    assert resolver_mocks.probe.call_count >= 1


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
@patch("resources.lib.resolver._play_direct")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
def test_resolve_continues_polling_when_webdav_reachable_and_apis_silent(
    mock_find_video,
    mock_stream_url,
    mock_validate,
    mock_play_direct,
    mock_find_completed,
    resolver_mocks,
):
    """Complementary no-false-positive test: when the nzbdav APIs are
    silent but the WebDAV probe reports (True, None), the resolver must
    NOT fire any error dialog — it must just loop back and poll again.
    On the second iteration the history API returns Completed and the
    resolve succeeds."""
    resolver_mocks.submit.return_value = ("SABnzbd_nzo_reachable", None)
    # First iteration: both APIs silent. Second iteration: history
    # returns Completed, nzbdav's queue is also empty.
    resolver_mocks.status.return_value = None
    resolver_mocks.history.side_effect = [
        None,
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Test",
            "name": "Test",
        },
    ]
    # Probe says the server is reachable — the silent APIs are not an
    # error, the job is just not queued yet.
    resolver_mocks.probe.return_value = (True, None)
    mock_find_video.return_value = "/content/uncategorized/Test/test.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/Test/test.mkv",
        {"Authorization": "Basic dGVzdDp0ZXN0"},
    )
    mock_validate.return_value = True

    resolve(1, {"nzburl": "http://hydra/getnzb/reachable", "title": "reachable.mkv"})

    # No error dialog fired — the probe's (True, None) must NOT reach
    # the auth_failed branch.
    resolver_mocks.gui.Dialog.return_value.ok.assert_not_called()
    # The probe ran on the first iteration (where both APIs were
    # silent).
    assert resolver_mocks.probe.call_count >= 1
    # The resolve landed successfully (history came back Completed on
    # the second iteration and _play_direct was invoked).
    assert mock_play_direct.called


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
def test_resolve_surfaces_http_500_body_to_user(
    mock_find_completed,
    resolver_mocks,
):
    """End-to-end: when nzbdav returns HTTP 500 on submit, the user
    sees a dialog with nzbdav's actual error message and resolve()
    aborts cleanly via setResolvedUrl(False)."""
    resolver_mocks.submit.return_value = (
        None,
        {"status": 500, "message": "duplicate nzo_id 9b7e0ea0"},
    )

    resolve(1, {"nzburl": "http://hydra/nzb", "title": "movie.mkv"})

    # Dialog fired, resolve aborted via setResolvedUrl(False)
    resolver_mocks.gui.Dialog.return_value.ok.assert_called_once()
    resolver_mocks.plugin.setResolvedUrl.assert_called_once_with(
        1, False, resolver_mocks.gui.ListItem()
    )
    # Single submit attempt — no retry on 500
    assert resolver_mocks.submit.call_count == 1
