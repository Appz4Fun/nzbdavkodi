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

MAX_POLL_ITERATIONS = 720  # 1 hour at 5s interval

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


def _build_play_url(url, headers):
    """Build a play URL with optional pipe-separated HTTP headers."""
    from urllib.parse import quote as _quote

    all_headers = dict(headers) if headers else {}
    if all_headers:
        header_str = "&".join(
            "{}={}".format(k, _quote(v, safe=" /")) for k, v in all_headers.items()
        )
        return "{}|{}".format(url, header_str)
    return url


def _make_playable_listitem(url, headers):
    """Create a ListItem with URL and optional HTTP auth headers.

    Uses Kodi's pipe-separated header syntax on the URL.
    """
    play_url = _build_play_url(url, headers)

    xbmc.log("NZB-DAV: Play URL: {}".format(play_url), xbmc.LOGDEBUG)
    li = xbmcgui.ListItem(path=play_url)
    # Skip HEAD request — nzbdav doesn't advertise Accept-Ranges on HEAD
    # which causes CFileCache to fail. Kodi will discover range support
    # on the first GET request instead.
    li.setContentLookup(False)
    # Set mime type based on file extension so Kodi doesn't need HEAD
    lower_url = url.lower()
    if lower_url.endswith(".mkv"):
        li.setMimeType("video/x-matroska")
    elif lower_url.endswith(".mp4") or lower_url.endswith(".m4v"):
        li.setMimeType("video/mp4")
    elif lower_url.endswith(".avi"):
        li.setMimeType("video/x-msvideo")
    else:
        li.setMimeType("video/x-matroska")
    return li


def _play_direct(handle, stream_url, stream_headers):
    """Play stream — uses proxy for MP4 (faststart), direct for MKV/other.

    MP4 files need the proxy for faststart (moov relocation) because nzbdav's
    connection:close causes OOM/timeout when CFileCache seeks to the moov atom.
    MKV files work fine with setResolvedUrl since their index is at the start.
    """
    lower_url = stream_url.lower()
    is_mp4 = lower_url.endswith((".mp4", ".m4v"))

    if is_mp4:
        _play_via_proxy_resolved(handle, stream_url, stream_headers)
    else:
        # MKV and other formats — use setResolvedUrl directly (works fine)
        li = _make_playable_listitem(stream_url, stream_headers)
        xbmcplugin.setResolvedUrl(handle, True, li)


def _play_via_proxy_resolved(handle, stream_url, stream_headers):
    """Play MP4 via local faststart proxy."""
    from resources.lib.stream_proxy import get_proxy

    proxy = get_proxy()

    auth_header = None
    if stream_headers and "Authorization" in stream_headers:
        auth_header = stream_headers["Authorization"]

    proxy_url = proxy.prepare_stream(stream_url, auth_header)
    xbmc.log("NZB-DAV: Playing via proxy: {}".format(proxy_url), xbmc.LOGINFO)

    li = xbmcgui.ListItem(path=proxy_url)
    li.setContentLookup(False)
    li.setMimeType("video/mp4")

    # Close the resolution pipeline — we'll play directly instead
    xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
    xbmc.Player().play(proxy_url, li)


def _play_via_proxy(stream_url, stream_headers):
    """Play a stream (for resolve_and_play path).

    Uses proxy for MP4 (faststart), direct for MKV/other.
    """
    lower_url = stream_url.lower()
    is_mp4 = lower_url.endswith((".mp4", ".m4v"))

    if is_mp4:
        from resources.lib.stream_proxy import get_proxy

        proxy = get_proxy()
        auth_header = None
        if stream_headers and "Authorization" in stream_headers:
            auth_header = stream_headers["Authorization"]

        proxy_url = proxy.prepare_stream(stream_url, auth_header)
        xbmc.log("NZB-DAV: Playing via proxy: {}".format(proxy_url), xbmc.LOGINFO)

        li = xbmcgui.ListItem(path=proxy_url)
        li.setContentLookup(False)
        li.setMimeType("video/mp4")
        xbmc.Player().play(proxy_url, li)
    else:
        li = _make_playable_listitem(stream_url, stream_headers)
        xbmc.Player().play(li.getPath(), li)


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


def _resolve_inner(handle, nzb_url, title, dialog, poll_interval, download_timeout):
    """Core resolve logic — runs inside a try/finally in resolve()."""
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
            stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
            _play_direct(handle, stream_url, stream_headers)
            return

    xbmc.log("NZB-DAV: Submitting NZB for '{}'".format(title), xbmc.LOGINFO)
    max_submit_retries = 3
    nzo_id = None
    monitor = xbmc.Monitor()
    for attempt in range(1, max_submit_retries + 1):
        nzo_id = submit_nzb(nzb_url, title)
        if nzo_id:
            break
        xbmc.log(
            "NZB-DAV: Submit attempt {}/{} failed for '{}'".format(
                attempt, max_submit_retries, title
            ),
            xbmc.LOGWARNING,
        )
        if attempt < max_submit_retries:
            if monitor.waitForAbort(2):
                return

    if not nzo_id:
        xbmc.log(
            "NZB-DAV: All {} submit attempts failed for '{}'. "
            "Check nzbdav URL and API key in settings.".format(
                max_submit_retries, title
            ),
            xbmc.LOGERROR,
        )
        _notify(_addon_name(), _string(30098))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        return

    xbmc.log(
        "NZB-DAV: NZB submitted, nzo_id={}, polling every {}s (timeout={}s)".format(
            nzo_id, poll_interval, download_timeout
        ),
        xbmc.LOGINFO,
    )
    start_time = time.time()
    last_status = None
    iteration = 0

    while True:
        iteration += 1
        if iteration > MAX_POLL_ITERATIONS:
            xbmc.log(
                "NZB-DAV: Max poll iterations ({}) reached for nzo_id={}".format(
                    MAX_POLL_ITERATIONS, nzo_id
                ),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _string(30099))
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

        elapsed = time.time() - start_time

        if elapsed >= download_timeout:
            xbmc.log(
                "NZB-DAV: Download timed out after {}s for nzo_id={} (title='{}'). "
                "Check the nzbdav queue for stalled jobs or increase the "
                "download timeout in addon settings.".format(
                    int(elapsed), nzo_id, title
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

        # Check history for failed download
        if history and history["status"] == "Failed":
            xbmc.log(
                "NZB-DAV: Download failed on the server side for nzo_id={} "
                "(title='{}'). Check the nzbdav history for the failure reason.".format(
                    nzo_id, title
                ),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _string(30100))
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            return

        # Check history for completed download
        if history and history["status"] == "Completed":
            storage = history["storage"]
            webdav_folder = _storage_to_webdav_path(storage)
            video_path = find_video_file(webdav_folder)
            if video_path:
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

                _play_direct(handle, stream_url, stream_headers)
                return

        # Handle WebDAV error types
        if webdav_error == "auth_failed":
            xbmc.log(
                "NZB-DAV: WebDAV authentication failed for nzo_id={}. "
                "Check WebDAV username and password in addon settings.".format(nzo_id),
                xbmc.LOGERROR,
            )
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
            return


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

    try:
        _resolve_inner(handle, nzb_url, title, dialog, poll_interval, download_timeout)
    except Exception as e:
        xbmc.log("NZB-DAV: Unexpected error in resolve: {}".format(e), xbmc.LOGERROR)
        _notify(_addon_name(), "Error: {}".format(str(e)[:80]))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
    finally:
        dialog.close()


def _resolve_and_play_inner(nzb_url, title, dialog, poll_interval, download_timeout):
    """Core resolve_and_play logic — runs inside a try/finally in resolve_and_play()."""
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
            stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
            _play_via_proxy(stream_url, stream_headers)
            return

    xbmc.log("NZB-DAV: Submitting NZB for '{}'".format(title), xbmc.LOGINFO)
    max_submit_retries = 3
    nzo_id = None
    monitor = xbmc.Monitor()
    for attempt in range(1, max_submit_retries + 1):
        nzo_id = submit_nzb(nzb_url, title)
        if nzo_id:
            break
        xbmc.log(
            "NZB-DAV: Submit attempt {}/{} failed for '{}'".format(
                attempt, max_submit_retries, title
            ),
            xbmc.LOGWARNING,
        )
        if attempt < max_submit_retries:
            if monitor.waitForAbort(2):
                return

    if not nzo_id:
        xbmc.log(
            "NZB-DAV: All {} submit attempts failed for '{}'. "
            "Check nzbdav URL and API key in settings.".format(
                max_submit_retries, title
            ),
            xbmc.LOGERROR,
        )
        _notify(_addon_name(), _string(30098))
        return

    xbmc.log("NZB-DAV: NZB submitted, nzo_id={}, polling".format(nzo_id), xbmc.LOGINFO)
    start_time = time.time()
    iteration = 0

    while True:
        iteration += 1
        if iteration > MAX_POLL_ITERATIONS:
            xbmc.log(
                "NZB-DAV: Max poll iterations ({}) reached for nzo_id={}".format(
                    MAX_POLL_ITERATIONS, nzo_id
                ),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _string(30099))
            return

        elapsed = time.time() - start_time

        if elapsed >= download_timeout:
            xbmc.log(
                "NZB-DAV: Download timed out after {}s for nzo_id={} (title='{}'). "
                "Check the nzbdav queue for stalled jobs or increase the "
                "download timeout in addon settings.".format(
                    int(elapsed), nzo_id, title
                ),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _string(30101))
            return

        if dialog.iscanceled():
            return

        job_status, history, webdav_error = _poll_once(nzo_id, title)

        if job_status:
            status = job_status.get("status", "Unknown")
            percentage = job_status.get("percentage", "0")

            if status.lower() in ("failed", "deleted"):
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
            xbmc.log(
                "NZB-DAV: WebDAV authentication failed for nzo_id={}. "
                "Check WebDAV username and password in addon settings.".format(nzo_id),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _string(_ERROR_MESSAGES["auth_failed"]))
            return

        # Check history for failed download
        if history and history["status"] == "Failed":
            xbmc.log(
                "NZB-DAV: Download failed on the server side for nzo_id={} "
                "(title='{}'). Check the nzbdav history for the failure reason.".format(
                    nzo_id, title
                ),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _string(30100))
            return

        # Check history for completed download
        if history and history["status"] == "Completed":
            storage = history["storage"]
            webdav_folder = _storage_to_webdav_path(storage)
            video_path = find_video_file(webdav_folder)
            if video_path:
                stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
                xbmc.log("NZB-DAV: Playing '{}'".format(stream_url), xbmc.LOGINFO)
                _play_via_proxy(stream_url, stream_headers)
                return

        if monitor.waitForAbort(poll_interval):
            return


def resolve_and_play(nzb_url, title):
    """Submit NZB, poll until ready, play directly via xbmc.Player.

    Used when called via executebuiltin://RunPlugin (not plugin:// directory).
    """
    poll_interval, download_timeout = _get_poll_settings()

    dialog = xbmcgui.DialogProgress()
    dialog.create(_addon_name(), _string(30097))

    try:
        _resolve_and_play_inner(nzb_url, title, dialog, poll_interval, download_timeout)
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Unexpected error in resolve_and_play: {}".format(e), xbmc.LOGERROR
        )
        _notify(_addon_name(), "Error: {}".format(str(e)[:80]))
    finally:
        dialog.close()
