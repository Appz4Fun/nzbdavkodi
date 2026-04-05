# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Resolve flow: submit NZB to nzbdav, poll until stream is ready, play."""

import threading
import time
from urllib.parse import unquote

import xbmc
import xbmcgui
import xbmcplugin

from resources.lib.http_util import notify as _notify
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string
from resources.lib.nzbdav_api import (
    find_completed_by_name,
    get_job_history,
    get_job_status,
    submit_nzb,
)
from resources.lib.webdav import (
    check_file_available_with_retry,
    find_video_file,
    get_webdav_stream_url_for_path,
    validate_stream,
)

_STATUS_MESSAGES = {
    "Queued": 30102,
    "Fetching": 30103,
    "Propagating": 30104,
    "Downloading": 30105,
    "Paused": 30106,
}

_ERROR_MESSAGES = {
    "auth_failed": 30107,
    "server_error": 30108,
    "connection_error": 30109,
}


def _make_playable_listitem(url, headers):
    """Create a ListItem with URL and optional HTTP auth headers.

    Uses Kodi's | header syntax on the URL for curl-based HTTP access.
    """
    from urllib.parse import quote as _quote

    # Always request keep-alive and byte serving for seekable playback
    all_headers = dict(headers) if headers else {}
    all_headers["Connection"] = "Keep-Alive"

    # Kodi expects: url|Header1=Value1&Header2=Value2
    # URL-encode header values so base64 padding '=' doesn't break parsing
    header_str = "&".join(
        "{}={}".format(k, _quote(v, safe=" /")) for k, v in all_headers.items()
    )
    play_url = "{}|{}".format(url, header_str)
    xbmc.log("NZB-DAV: Play URL: {}".format(play_url), xbmc.LOGDEBUG)
    li = xbmcgui.ListItem(path=play_url)
    return li


def _get_poll_settings():
    import xbmcaddon

    addon = xbmcaddon.Addon()
    interval = int(addon.getSetting("poll_interval") or "5")
    timeout = int(addon.getSetting("download_timeout") or "3600")
    return interval, timeout


def _storage_to_webdav_path(storage):
    """Convert nzbdav storage path to WebDAV content path.

    /mnt/nzbdav/completed-symlinks/uncategorized/Name -> /content/uncategorized/Name/
    """
    prefix = "/mnt/nzbdav/completed-symlinks/"
    if storage.startswith(prefix):
        relative = storage[len(prefix) :]
    else:
        # Fallback: use the last two path components (category/name)
        parts = storage.rstrip("/").split("/")
        relative = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return "/content/{}/".format(relative)


def _poll_once(nzo_id, title):
    """Poll nzbdav queue API and history API in parallel.

    Returns (job_status, history_status, error_type).
    job_status is from the queue API (active downloads).
    history_status is from the history API (completed downloads).
    error_type is set if a WebDAV check encounters auth/server errors.
    """
    job_status = [None]
    history_status = [None]
    error_type = [None]

    def check_queue():
        job_status[0] = get_job_status(nzo_id)

    def check_history():
        history = get_job_history(nzo_id)
        history_status[0] = history
        # If not in history and not in queue, check WebDAV availability
        # using the legacy method to surface auth/server errors
        if history is None and job_status[0] is None:
            _, error = check_file_available_with_retry(
                title, max_retries=1, retry_delay=1
            )
            error_type[0] = error

    t1 = threading.Thread(target=check_queue)
    t2 = threading.Thread(target=check_history)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    xbmc.log(
        "NZB-DAV: Poll result - job_status={} history_status={} error_type={}".format(
            job_status[0], history_status[0], error_type[0]
        ),
        xbmc.LOGDEBUG,
    )
    return job_status[0], history_status[0], error_type[0]


def resolve(handle, params):
    nzb_url = unquote(params.get("nzburl", ""))
    title = unquote(params.get("title", ""))

    if not nzb_url:
        _notify(_addon_name(), _string(30096))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    poll_interval, download_timeout = _get_poll_settings()

    dialog = xbmcgui.DialogProgress()
    dialog.create(_addon_name(), _string(30097))

    # Check if this title was already downloaded — skip re-downloading
    existing = find_completed_by_name(title)
    if existing:
        xbmc.log(
            "NZB-DAV: '{}' already downloaded, streaming directly".format(title),
            xbmc.LOGINFO,
        )
        webdav_folder = _storage_to_webdav_path(existing["storage"])
        video_path = find_video_file(webdav_folder)
        if video_path:
            dialog.close()
            stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
            li = _make_playable_listitem(stream_url, stream_headers)
            xbmc.Player().play(li.getPath(), li)
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

    xbmc.log("NZB-DAV: Submitting NZB for '{}'".format(title), xbmc.LOGINFO)
    nzo_id = submit_nzb(nzb_url, title)
    if not nzo_id:
        dialog.close()
        _notify(_addon_name(), _string(30098))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    xbmc.log(
        "NZB-DAV: NZB submitted, nzo_id={}, polling every {}s (timeout={}s)".format(
            nzo_id, poll_interval, download_timeout
        ),
        xbmc.LOGINFO,
    )

    monitor = xbmc.Monitor()
    start_time = time.time()
    last_status = None

    while True:
        elapsed = time.time() - start_time

        if elapsed >= download_timeout:
            dialog.close()
            xbmc.log(
                "NZB-DAV: Download timed out after {}s for nzo_id={}".format(
                    int(elapsed), nzo_id
                ),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _fmt(30099, int(elapsed)))
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

        if dialog.iscanceled():
            xbmc.log(
                "NZB-DAV: User cancelled resolve for nzo_id={}".format(nzo_id),
                xbmc.LOGINFO,
            )
            dialog.close()
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

        job_status, history, webdav_error = _poll_once(nzo_id, title)

        if job_status:
            status = job_status.get("status", "Unknown")
            percentage = job_status.get("percentage", "0")

            if status != last_status:
                xbmc.log(
                    "NZB-DAV: Job {} status changed: {} -> {}".format(
                        nzo_id, last_status, status
                    ),
                    xbmc.LOGINFO,
                )
                last_status = status

            if status.lower() in ("failed", "deleted"):
                dialog.close()
                xbmc.log(
                    "NZB-DAV: Job {} failed/deleted (status={})".format(nzo_id, status),
                    xbmc.LOGERROR,
                )
                _notify(_addon_name(), _string(30100))
                xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
                return

            msg_id = _STATUS_MESSAGES.get(status)
            if not msg_id:
                msg = "Status: {}".format(status)
            elif msg_id == 30105:
                msg = _fmt(msg_id, percentage)
            else:
                msg = _string(msg_id)
            progress = min(int(percentage or 0), 100)
            dialog.update(progress, msg)

        # Check history for completed download
        if history and history["status"] == "Completed":
            storage = history["storage"]
            webdav_folder = _storage_to_webdav_path(storage)
            video_path = find_video_file(webdav_folder)
            if video_path:
                dialog.close()
                stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
                xbmc.log(
                    "NZB-DAV: File available, streaming '{}' via WebDAV".format(
                        video_path
                    ),
                    xbmc.LOGINFO,
                )

                # Validate stream supports range requests before playback
                if not validate_stream(title):
                    xbmc.log(
                        "NZB-DAV: Stream validation failed for '{}', "
                        "attempting playback anyway".format(title),
                        xbmc.LOGWARNING,
                    )

                # Play directly via xbmc.Player — setResolvedUrl uses
                # CFileCache which crashes on CoreELEC with HTTP streams
                li = _make_playable_listitem(stream_url, stream_headers)
                xbmc.Player().play(li.getPath(), li)
                # Signal that plugin is done (False = don't resolve via
                # Kodi's pipeline which would open CFileCache again)
                xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
                return

        # Handle WebDAV error types
        if webdav_error == "auth_failed":
            dialog.close()
            xbmc.log("NZB-DAV: WebDAV auth failed, stopping resolve", xbmc.LOGERROR)
            _notify(_addon_name(), _string(_ERROR_MESSAGES["auth_failed"]))
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

        if webdav_error == "server_error":
            xbmc.log(
                "NZB-DAV: WebDAV server error, will retry on next poll",
                xbmc.LOGWARNING,
            )

        if monitor.waitForAbort(poll_interval):
            # Kodi is shutting down
            xbmc.log("NZB-DAV: Kodi shutdown detected, aborting resolve", xbmc.LOGINFO)
            dialog.close()
            return


def resolve_and_play(nzb_url, title):
    """Submit NZB, poll until ready, play directly via xbmc.Player.

    Used when called via executebuiltin://RunPlugin (not plugin:// directory).
    """
    poll_interval, download_timeout = _get_poll_settings()

    dialog = xbmcgui.DialogProgress()
    dialog.create(_addon_name(), _string(30097))

    # Check if this title was already downloaded — skip re-downloading
    existing = find_completed_by_name(title)
    if existing:
        xbmc.log(
            "NZB-DAV: '{}' already downloaded, streaming directly".format(title),
            xbmc.LOGINFO,
        )
        webdav_folder = _storage_to_webdav_path(existing["storage"])
        video_path = find_video_file(webdav_folder)
        if video_path:
            dialog.close()
            stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
            li = _make_playable_listitem(stream_url, stream_headers)
            xbmc.Player().play(li.getPath(), li)
            return

    xbmc.log("NZB-DAV: Submitting NZB for '{}'".format(title), xbmc.LOGINFO)
    nzo_id = submit_nzb(nzb_url, title)
    if not nzo_id:
        dialog.close()
        _notify(_addon_name(), _string(30098))
        return

    xbmc.log("NZB-DAV: NZB submitted, nzo_id={}, polling".format(nzo_id), xbmc.LOGINFO)

    monitor = xbmc.Monitor()
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time

        if elapsed >= download_timeout:
            dialog.close()
            _notify(_addon_name(), _string(30101))
            return

        if dialog.iscanceled():
            dialog.close()
            return

        job_status, history, webdav_error = _poll_once(nzo_id, title)

        if job_status:
            status = job_status.get("status", "Unknown")
            percentage = job_status.get("percentage", "0")

            if status.lower() in ("failed", "deleted"):
                dialog.close()
                _notify(_addon_name(), _string(30100))
                return

            msg_id = _STATUS_MESSAGES.get(status)
            if not msg_id:
                msg = "Status: {}".format(status)
            elif msg_id == 30105:
                msg = _fmt(msg_id, percentage)
            else:
                msg = _string(msg_id)
            progress = min(int(percentage or 0), 100)
            dialog.update(progress, msg)

        if webdav_error == "auth_failed":
            dialog.close()
            _notify(_addon_name(), _string(_ERROR_MESSAGES["auth_failed"]))
            return

        # Check history for completed download
        if history and history["status"] == "Completed":
            storage = history["storage"]
            webdav_folder = _storage_to_webdav_path(storage)
            video_path = find_video_file(webdav_folder)
            if video_path:
                dialog.close()
                stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
                xbmc.log("NZB-DAV: Playing '{}'".format(stream_url), xbmc.LOGINFO)
                li = _make_playable_listitem(stream_url, stream_headers)
                xbmc.Player().play(stream_url, li)
                return

        if monitor.waitForAbort(poll_interval):
            dialog.close()
            return
