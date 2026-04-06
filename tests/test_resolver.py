# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

from resources.lib.resolver import MAX_POLL_ITERATIONS, _storage_to_webdav_path, resolve


def _make_monitor(abort_after=None):
    """Make a mock xbmc.Monitor. Returns False until abort_after calls, then True."""
    monitor = MagicMock()
    if abort_after is None:
        monitor.waitForAbort.return_value = False
    else:
        side_effects = [False] * abort_after + [True]
        monitor.waitForAbort.side_effect = side_effects
    return monitor


# --- _storage_to_webdav_path tests ---


def test_storage_to_webdav_path_standard():
    """Standard storage path converts to /content/ WebDAV path."""
    result = _storage_to_webdav_path(
        "/mnt/nzbdav/completed-symlinks/uncategorized/Send Help 2026 1080p"
    )
    assert result == "/content/uncategorized/Send Help 2026 1080p/"


def test_storage_to_webdav_path_different_category():
    """Storage path with non-uncategorized category converts correctly."""
    result = _storage_to_webdav_path(
        "/mnt/nzbdav/completed-symlinks/movies/The Matrix 1999"
    )
    assert result == "/content/movies/The Matrix 1999/"


def test_storage_to_webdav_path_fallback():
    """Fallback for non-standard storage path uses last two components."""
    result = _storage_to_webdav_path("/some/other/path/category/name")
    assert result == "/content/category/name/"


def test_storage_to_webdav_path_trailing_slash():
    """Storage path with trailing slash is handled correctly."""
    result = _storage_to_webdav_path(
        "/mnt/nzbdav/completed-symlinks/uncategorized/Movie Name/"
    )
    assert result == "/content/uncategorized/Movie Name//"


# --- resolve() tests ---


@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=0)
@patch("resources.lib.stream_proxy.get_proxy")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_success(
    mock_poll,
    mock_validate,
    mock_stream_url,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_get_proxy,
    mock_service_port,
):
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = "SABnzbd_nzo_abc123"
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "name": "movie",
    }
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )
    mock_validate.return_value = True
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_proxy = MagicMock()
    mock_proxy.prepare_stream.return_value = "http://127.0.0.1:57800/stream"
    mock_get_proxy.return_value = mock_proxy

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_submit.assert_called_once()
    mock_plugin.setResolvedUrl.assert_called_once()
    resolve_call = mock_plugin.setResolvedUrl.call_args
    assert resolve_call[0][1] is True


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_submit_failure(
    mock_poll, mock_submit, mock_plugin, mock_gui, mock_xbmc, mock_find_completed
):
    """All submit retries fail — setResolvedUrl called with False."""
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = None
    mock_find_completed.return_value = None
    mock_xbmc.Monitor.return_value = MagicMock()
    mock_xbmc.Monitor.return_value.waitForAbort.return_value = False

    dialog = MagicMock()
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())
    assert mock_submit.call_count == 3


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_job_failed(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = "SABnzbd_nzo_abc123"
    mock_status.return_value = {"status": "Failed", "percentage": "0"}
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_user_cancels(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = "SABnzbd_nzo_abc123"
    mock_status.return_value = {"status": "Downloading", "percentage": "50"}
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    dialog.iscanceled.return_value = True
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())


@patch("resources.lib.resolver._notify")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_no_nzb_url(mock_poll, mock_plugin, mock_gui, mock_notify):
    """Resolve with no NZB URL should fail immediately."""
    mock_poll.return_value = (2, 60)

    resolve(1, {"nzburl": "", "title": "movie.mkv"})

    mock_notify.assert_called_once_with("NZB-DAV", "No NZB URL provided")
    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())


@patch("resources.lib.resolver._notify")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.time")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_timeout(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_time,
    mock_xbmc,
    mock_notify,
):
    """Resolve should time out after download_timeout seconds."""
    mock_poll.return_value = (2, 5)  # 5 second timeout
    mock_submit.return_value = "SABnzbd_nzo_abc123"
    mock_status.return_value = {"status": "Downloading", "percentage": "10"}
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    # Simulate time passing beyond timeout
    mock_time.time.side_effect = [0.0, 10.0]

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())
    # Check timeout notification was shown
    mock_notify.assert_called()
    notify_msg = mock_notify.call_args[0][1]
    assert "timed out" in notify_msg


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_deleted_status(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    """'Deleted' status should be treated as failure."""
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = "SABnzbd_nzo_abc123"
    mock_status.return_value = {"status": "Deleted", "percentage": "0"}
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())


# --- New tests ---


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_url_encoded_special_characters(
    mock_poll,
    mock_validate,
    mock_stream_url,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    """resolve() URL-decodes nzburl and title before passing to submit_nzb."""
    from urllib.parse import quote

    mock_poll.return_value = (2, 60)
    mock_submit.return_value = "SABnzbd_nzo_xyz789"
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "name": "movie",
    }
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )
    mock_validate.return_value = True
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    raw_url = "http://hydra:5076/getnzb/abc?apikey=testkey&extra=foo bar"
    raw_title = "Spider-Man: No Way Home (2021) 1080p"
    encoded_url = quote(raw_url, safe="")
    encoded_title = quote(raw_title, safe="")

    resolve(1, {"nzburl": encoded_url, "title": encoded_title})

    submit_call_args = mock_submit.call_args[0]
    assert (
        "hydra:5076" in submit_call_args[0]
    ), "NZB URL should be decoded before submit"
    assert "Spider-Man" in submit_call_args[1], "Title should be decoded before submit"
    mock_plugin.setResolvedUrl.assert_called_once()


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_poll_interval_respected(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    """resolve() calls monitor.waitForAbort with the configured poll_interval."""
    poll_interval = 7
    mock_poll.return_value = (poll_interval, 3600)
    mock_submit.return_value = "SABnzbd_nzo_poll123"
    mock_status.return_value = {"status": "Downloading", "percentage": "50"}
    mock_history.return_value = None

    dialog = MagicMock()
    dialog.iscanceled.side_effect = [False, True]
    mock_gui.DialogProgress.return_value = dialog

    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    mock_xbmc.Monitor.return_value = monitor

    resolve(1, {"nzburl": "http://hydra/getnzb/poll", "title": "polltest.mkv"})

    monitor.waitForAbort.assert_called_with(poll_interval)


@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=0)
@patch("resources.lib.stream_proxy.get_proxy")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_status_transitions_queued_to_downloading_to_completed(
    mock_poll,
    mock_validate,
    mock_stream_url,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_get_proxy,
    mock_service_port,
):
    """resolve() handles Queued -> Downloading -> Completed via history."""
    mock_poll.return_value = (1, 3600)
    mock_submit.return_value = "SABnzbd_nzo_trans456"
    mock_status.side_effect = [
        {"status": "Queued", "percentage": "0"},
        {"status": "Downloading", "percentage": "50"},
        None,  # No longer in queue when completed
    ]
    mock_history.side_effect = [
        None,
        None,
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/downloaded",
            "name": "downloaded",
        },
    ]
    mock_find.return_value = "/content/uncategorized/downloaded/downloaded.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/downloaded/downloaded.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )
    mock_validate.return_value = True
    mock_proxy = MagicMock()
    mock_proxy.prepare_stream.return_value = "http://127.0.0.1:57800/stream"
    mock_get_proxy.return_value = mock_proxy

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    mock_xbmc.Monitor.return_value = _make_monitor()

    resolve(1, {"nzburl": "http://hydra/getnzb/trans", "title": "downloaded.mkv"})

    assert (
        mock_history.call_count == 3
    ), "get_job_history should be polled three times before completing"
    mock_plugin.setResolvedUrl.assert_called_once()
    resolve_call = mock_plugin.setResolvedUrl.call_args
    assert resolve_call[0][1] is True


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_dialog_closed_on_exception(
    mock_poll, mock_submit, mock_plugin, mock_gui, mock_xbmc, mock_find
):
    """Dialog must be closed even if an exception occurs during resolve."""
    mock_poll.return_value = (2, 60)
    mock_find.return_value = None
    mock_submit.side_effect = RuntimeError("unexpected crash")

    dialog = MagicMock()
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    dialog.close.assert_called()
    mock_plugin.setResolvedUrl.assert_called_once()


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_max_iterations_safeguard(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_find,
):
    """Resolve loop exits after MAX_POLL_ITERATIONS even without timeout."""
    mock_poll.return_value = (0, 999999)  # Very long timeout, 0s interval
    mock_find.return_value = None
    mock_submit.return_value = "SABnzbd_nzo_stuck"
    mock_status.return_value = {"status": "Queued", "percentage": "0"}
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = MagicMock()
    mock_xbmc.Monitor.return_value.waitForAbort.return_value = False

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/stuck", "title": "stuck.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once()
    assert mock_plugin.setResolvedUrl.call_args[0][1] is False
    assert mock_status.call_count <= MAX_POLL_ITERATIONS


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_retries_submit_on_transient_failure(
    mock_poll,
    mock_validate,
    mock_stream_url,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_find_completed,
):
    """resolve should retry submit_nzb if it fails the first time."""
    mock_poll.return_value = (2, 60)
    mock_find_completed.return_value = None
    # First call fails, second succeeds
    mock_submit.side_effect = [None, "SABnzbd_nzo_retry123"]
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "name": "movie",
    }
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )
    mock_validate.return_value = True
    mock_xbmc.Monitor.return_value = MagicMock()
    mock_xbmc.Monitor.return_value.waitForAbort.return_value = False

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    assert mock_submit.call_count == 2
