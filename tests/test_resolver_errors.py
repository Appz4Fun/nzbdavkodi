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
    mock_submit.return_value = "SABnzbd_nzo_failed"
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


@patch("resources.lib.resolver.check_file_available_with_retry")
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
    mock_submit.return_value = "SABnzbd_nzo_timeout"
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
