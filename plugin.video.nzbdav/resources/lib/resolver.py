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
)

MAX_POLL_ITERATIONS = 720  # 1 hour at 5s interval


def _validate_stream_url(url, headers):
    """Verify the stream URL supports range requests (seekable streaming).

    Validates the actual resolved URL rather than building one from a title.
    Returns True if range requests are supported, False otherwise.
    """
    from urllib.request import Request, urlopen

    req = Request(url, method="HEAD")
    req.add_header("Range", "bytes=0-0")
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)
    try:
        with urlopen(req, timeout=10) as resp:  # nosec B310
            return resp.getcode() == 206 or "bytes" in resp.headers.get(
                "Accept-Ranges", ""
            )
    except (OSError, ValueError):
        return False


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
    """Play a stream — MP4 via faststart proxy (preferred) or MKV remux fallback.

    MP4 files go through the service proxy which tries:
    1. Virtual moov-relocation (pure Python, video/mp4, full seeking)
    2. Temp-file faststart (ffmpeg -movflags +faststart, video/mp4, full seeking)
    3. MKV remux (ffmpeg pipe, video/x-matroska, duration but no seeking)

    MKV and other formats play the WebDAV URL directly.
    """

    lower_url = stream_url.lower()
    is_mp4 = lower_url.endswith((".mp4", ".m4v"))

    if is_mp4:
        from resources.lib.stream_proxy import (
            get_service_proxy_port,
            prepare_stream_via_service,
        )

        auth_header = None
        if stream_headers and "Authorization" in stream_headers:
            auth_header = stream_headers["Authorization"]

        service_port = get_service_proxy_port()
        if service_port:
            proxy_url, stream_info = prepare_stream_via_service(
                service_port, stream_url, auth_header
            )

            li = xbmcgui.ListItem(path=proxy_url)
            li.setContentLookup(False)

            if stream_info.get("remux"):
                xbmc.log(
                    "NZB-DAV: Playing MP4 via remux proxy: {}".format(proxy_url),
                    xbmc.LOGINFO,
                )
                li.setMimeType("video/x-matroska")

                duration = stream_info.get("duration_seconds")
                if duration:
                    info_tag = li.getVideoInfoTag()
                    info_tag.setDuration(int(duration))
            else:
                xbmc.log(
                    "NZB-DAV: Playing MP4 via faststart proxy: {}".format(proxy_url),
                    xbmc.LOGINFO,
                )
                li.setMimeType("video/mp4")

            xbmcplugin.setResolvedUrl(handle, True, li)

            home = xbmcgui.Window(10000)
            home.setProperty("nzbdav.stream_url", proxy_url)
            home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
            home.setProperty("nzbdav.active", "true")
            return

    # MKV and fallback: play directly
    play_url = _build_play_url(stream_url, stream_headers)
    xbmc.log("NZB-DAV: Playing direct: {}".format(stream_url), xbmc.LOGINFO)

    li = _make_playable_listitem(stream_url, stream_headers)
    xbmcplugin.setResolvedUrl(handle, True, li)

    home = xbmcgui.Window(10000)
    home.setProperty("nzbdav.stream_url", play_url)
    home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
    home.setProperty("nzbdav.active", "true")


def _play_via_proxy(stream_url, stream_headers):
    """Play a stream (for resolve_and_play path).

    MP4 via faststart proxy (preferred) or MKV remux fallback, others direct.
    """

    lower_url = stream_url.lower()
    is_mp4 = lower_url.endswith((".mp4", ".m4v"))

    if is_mp4:
        from resources.lib.stream_proxy import (
            get_service_proxy_port,
            prepare_stream_via_service,
        )

        auth_header = None
        if stream_headers and "Authorization" in stream_headers:
            auth_header = stream_headers["Authorization"]

        service_port = get_service_proxy_port()
        if service_port:
            proxy_url, stream_info = prepare_stream_via_service(
                service_port, stream_url, auth_header
            )

            li = xbmcgui.ListItem(path=proxy_url)
            li.setContentLookup(False)

            if stream_info.get("remux"):
                xbmc.log(
                    "NZB-DAV: Playing MP4 via remux proxy: {}".format(proxy_url),
                    xbmc.LOGINFO,
                )
                li.setMimeType("video/x-matroska")

                duration = stream_info.get("duration_seconds")
                if duration:
                    info_tag = li.getVideoInfoTag()
                    info_tag.setDuration(int(duration))
            else:
                xbmc.log(
                    "NZB-DAV: Playing MP4 via faststart proxy: {}".format(proxy_url),
                    xbmc.LOGINFO,
                )
                li.setMimeType("video/mp4")

            xbmc.Player().play(proxy_url, li)
            return

    li = _make_playable_listitem(stream_url, stream_headers)
    xbmc.log("NZB-DAV: Playing direct: {}".format(stream_url), xbmc.LOGINFO)
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

    Args:
        nzo_id: nzbdav job identifier to poll.
        title: Human-readable title used for the WebDAV filename check.

    Returns:
        A tuple of (job_status, history_status, error_type):
        - job_status: Dict from the queue API when the job is active, or None
          when the job is missing from the queue.
        - history_status: Dict from the history API when the job completed, or
          None when not present.
        - error_type: None when polling succeeds; otherwise the error string
          returned by check_file_available_with_retry() when both APIs return
          None (e.g., "not_found", "auth_failed", "server_error",
          "connection_error").

    Side effects:
        Spawns two threads to call get_job_status() and get_job_history().
        Performs HTTP requests to nzbdav queue/history endpoints and, when
        neither returns data, a WebDAV availability probe.
        Logs poll results to the Kodi log.
    """
    job_status = [None]
    history_status = [None]
    error_type = [None]

    def check_queue():
        job_status[0] = get_job_status(nzo_id)

    def check_history():
        history_status[0] = get_job_history(nzo_id)

    t1 = threading.Thread(target=check_queue)
    t2 = threading.Thread(target=check_history)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    # Only probe WebDAV for errors after both threads have finished,
    # so we don't falsely conclude the job is missing
    if history_status[0] is None and job_status[0] is None:
        _, error = check_file_available_with_retry(title, max_retries=1, retry_delay=1)
        error_type[0] = error

    xbmc.log(
        "NZB-DAV: Poll result - job_status={} history_status={} error_type={}".format(
            job_status[0], history_status[0], error_type[0]
        ),
        xbmc.LOGDEBUG,
    )
    return job_status[0], history_status[0], error_type[0]


def _poll_until_ready(nzb_url, title, dialog, poll_interval, download_timeout):
    """Submit NZB and poll until download completes.

    Returns ``(stream_url, stream_headers)`` on success, or ``(None, None)``
    on failure (timeout, cancellation, server error, etc.).  All user
    notifications are issued inside this function; the caller only needs to
    decide what to do with the resulting stream URL.
    """
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
            return get_webdav_stream_url_for_path(video_path)

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
                return None, None

    if not nzo_id:
        xbmc.log(
            "NZB-DAV: All {} submit attempts failed for '{}'. "
            "Check nzbdav URL and API key in settings.".format(
                max_submit_retries, title
            ),
            xbmc.LOGERROR,
        )
        _notify(_addon_name(), _string(30098))
        return None, None

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
            return None, None

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
            return None, None

        if dialog.iscanceled():
            xbmc.log(
                "NZB-DAV: User cancelled resolve for nzo_id={}".format(nzo_id),
                xbmc.LOGINFO,
            )
            return None, None

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
                return None, None

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
            return None, None

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
                if not _validate_stream_url(stream_url, stream_headers):
                    xbmc.log(
                        "NZB-DAV: Stream validation failed for '{}', "
                        "attempting playback anyway".format(video_path),
                        xbmc.LOGWARNING,
                    )

                return stream_url, stream_headers
            else:
                xbmc.log(
                    "NZB-DAV: Completed but no video found at '{}', retrying...".format(
                        webdav_folder
                    ),
                    xbmc.LOGWARNING,
                )

        # Handle WebDAV error types
        if webdav_error == "auth_failed":
            xbmc.log(
                "NZB-DAV: WebDAV authentication failed for nzo_id={}. "
                "Check WebDAV username and password in addon settings.".format(nzo_id),
                xbmc.LOGERROR,
            )
            _notify(_addon_name(), _string(_ERROR_MESSAGES["auth_failed"]))
            return None, None

        if webdav_error == "server_error":
            xbmc.log(
                "NZB-DAV: WebDAV server error, will retry on next poll",
                xbmc.LOGWARNING,
            )

        if monitor.waitForAbort(poll_interval):
            # Kodi is shutting down
            xbmc.log("NZB-DAV: Kodi shutdown detected, aborting resolve", xbmc.LOGINFO)
            return None, None


def resolve(handle, params):
    """Handle plugin:// URL resolution (TMDBHelper integration).

    Decodes parameters, polls until the stream is ready, then calls
    setResolvedUrl() — True on success, False on any failure — so Kodi
    always receives a resolution response and does not hang.
    """
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
        stream_url, stream_headers = _poll_until_ready(
            nzb_url, title, dialog, poll_interval, download_timeout
        )
        if stream_url:
            _play_direct(handle, stream_url, stream_headers)
        else:
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
    except Exception as e:
        xbmc.log("NZB-DAV: Unexpected error in resolve: {}".format(e), xbmc.LOGERROR)
        _notify(_addon_name(), "Error: {}".format(str(e)[:80]))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
    finally:
        dialog.close()


def resolve_and_play(nzb_url, title):
    """Handle direct execution (executebuiltin://RunPlugin calls).

    Polls until the stream is ready, then plays via xbmc.Player().
    Unlike resolve(), there is no plugin handle so setResolvedUrl() is not
    called; playback simply does not start on failure.
    """
    poll_interval, download_timeout = _get_poll_settings()

    dialog = xbmcgui.DialogProgress()
    dialog.create(_addon_name(), _string(30097))

    try:
        stream_url, stream_headers = _poll_until_ready(
            nzb_url, title, dialog, poll_interval, download_timeout
        )
        if stream_url:
            _play_via_proxy(stream_url, stream_headers)
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Unexpected error in resolve_and_play: {}".format(e), xbmc.LOGERROR
        )
        _notify(_addon_name(), "Error: {}".format(str(e)[:80]))
    finally:
        dialog.close()
