# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

from resources.lib.resolver import resolve


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_aborts_on_nzbdav_failed_status(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    """When nzbdav reports job Failed, resolve() should show error dialog and
    call setResolvedUrl(False)."""
    mock_poll.return_value = (1, 60)
    mock_submit.return_value = ("SABnzbd_nzo_failed", None)
    mock_status.return_value = {"status": "Downloading", "percentage": "20"}
    mock_history.return_value = {
        "status": "Failed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Failed",
        "name": "Failed Job",
    }
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    mock_xbmc.Monitor.return_value = monitor

    resolve(1, {"nzburl": "http://hydra/getnzb/fail", "title": "failed.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())
    mock_gui.Dialog.return_value.ok.assert_called_once()


@patch("resources.lib.resolver.probe_webdav_reachable")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.time")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_times_out_gracefully(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_time,
    mock_xbmc,
    mock_check_webdav,
):
    """When polling exceeds timeout, resolve() should notify and not hang
    even if status calls return None."""
    mock_poll.return_value = (1, 5)  # 5s timeout
    mock_submit.return_value = ("SABnzbd_nzo_timeout", None)
    mock_status.return_value = None  # Simulate connection timeout to nzbdav
    mock_history.return_value = None
    mock_check_webdav.return_value = (False, "connection_error")
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    mock_xbmc.Monitor.return_value = monitor
    mock_time.time.side_effect = [0.0, 6.0]

    resolve(1, {"nzburl": "http://hydra/getnzb/timeout", "title": "timeout.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())
    mock_gui.Dialog.return_value.ok.assert_called_once()


@patch("resources.lib.resolver.probe_webdav_reachable")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.time")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_aborts_on_webdav_auth_failed_when_nzbdav_apis_silent(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_time,
    mock_xbmc,
    mock_probe,
):
    """Primary C3 regression test: when both nzbdav APIs return None and
    the WebDAV probe reports auth_failed, the resolver must show the
    auth dialog and call setResolvedUrl(False) within a single poll
    iteration — not spin until the download timeout."""
    mock_poll.return_value = (1, 60)  # 1s poll interval, 60s timeout
    mock_submit.return_value = ("SABnzbd_nzo_silent", None)
    # Both nzbdav APIs silent — triggers the probe branch in _poll_once.
    mock_status.return_value = None
    mock_history.return_value = None
    # The newly-classified auth failure case that used to be "not_found".
    mock_probe.return_value = (False, "auth_failed")
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    mock_xbmc.Monitor.return_value = monitor
    # Pin time.time() to 0.0 so elapsed always evaluates to 0.0, keeping
    # the loop far below the 60s download_timeout. This means the only way
    # out of _poll_until_ready is the probe's auth_failed branch — the
    # timeout branch can never fire, so the test proves the right path.
    mock_time.time.return_value = 0.0

    resolve(1, {"nzburl": "http://hydra/getnzb/silent", "title": "silent.mkv"})

    # The auth dialog fired.
    mock_gui.Dialog.return_value.ok.assert_called_once()
    # Resolve aborted.
    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())
    # The probe was actually reached — proves the code path the test
    # claims to cover.
    assert mock_probe.call_count >= 1


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
@patch("resources.lib.resolver._play_direct")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.probe_webdav_reachable")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.time")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_continues_polling_when_webdav_reachable_and_apis_silent(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_time,
    mock_xbmc,
    mock_probe,
    mock_find_video,
    mock_stream_url,
    mock_validate,
    mock_play_direct,
    mock_find_completed,
):
    """Complementary no-false-positive test: when the nzbdav APIs are
    silent but the WebDAV probe reports (True, None), the resolver must
    NOT fire any error dialog — it must just loop back and poll again.
    On the second iteration the history API returns Completed and the
    resolve succeeds."""
    mock_poll.return_value = (1, 60)
    mock_submit.return_value = ("SABnzbd_nzo_reachable", None)
    # First iteration: both APIs silent. Second iteration: history
    # returns Completed, nzbdav's queue is also empty.
    mock_status.return_value = None
    mock_history.side_effect = [
        None,
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Test",
            "name": "Test",
        },
    ]
    # Probe says the server is reachable — the silent APIs are not an
    # error, the job is just not queued yet.
    mock_probe.return_value = (True, None)
    mock_find_video.return_value = "/content/uncategorized/Test/test.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/Test/test.mkv",
        {"Authorization": "Basic dGVzdDp0ZXN0"},
    )
    mock_validate.return_value = True
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    mock_xbmc.Monitor.return_value = monitor
    # Pin time.time() to 0.0 so elapsed is always 0.0, well below the 60s
    # timeout, ensuring we loop until the history API returns Completed.
    mock_time.time.return_value = 0.0

    resolve(1, {"nzburl": "http://hydra/getnzb/reachable", "title": "reachable.mkv"})

    # No error dialog fired — the probe's (True, None) must NOT reach
    # the auth_failed branch.
    mock_gui.Dialog.return_value.ok.assert_not_called()
    # The probe ran on the first iteration (where both APIs were
    # silent).
    assert mock_probe.call_count >= 1
    # The resolve landed successfully (history came back Completed on
    # the second iteration and _play_direct was invoked).
    assert mock_play_direct.called


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.time")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_surfaces_http_500_body_to_user(
    mock_poll,
    mock_plugin,
    mock_gui,
    mock_time,
    mock_xbmc,
    mock_submit,
    mock_find_completed,
):
    """End-to-end: when nzbdav returns HTTP 500 on submit, the user
    sees a dialog with nzbdav's actual error message and resolve()
    aborts cleanly via setResolvedUrl(False)."""
    mock_poll.return_value = (1, 60)
    mock_submit.return_value = (
        None,
        {"status": 500, "message": "duplicate nzo_id 9b7e0ea0"},
    )
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    mock_xbmc.Monitor.return_value = monitor
    mock_time.time.return_value = 0.0  # pin elapsed at 0 (per Task 4 v0.6.20 lesson)

    resolve(1, {"nzburl": "http://hydra/nzb", "title": "movie.mkv"})

    # Dialog fired, resolve aborted via setResolvedUrl(False)
    mock_gui.Dialog.return_value.ok.assert_called_once()
    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())
    # Single submit attempt — no retry on 500
    assert mock_submit.call_count == 1
