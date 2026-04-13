# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Resolve flow: submit NZB to nzbdav, poll until stream is ready, play."""

import threading
import time
from urllib.parse import unquote

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string
from resources.lib.nzbdav_api import (
    cancel_job,
    find_completed_by_name,
    get_job_history,
    get_job_status,
    submit_nzb,
)
from resources.lib.webdav import (
    find_video_file,
    get_webdav_stream_url_for_path,
    probe_webdav_reachable,
)

MAX_POLL_ITERATIONS = 720  # 1 hour at 5s interval
# HTTP status codes the submit retry loop treats as transient and worth
# retrying. RFC 9110 explicitly calls 408 retry-friendly ("client may
# assume the server closed the connection due to inactivity and retry").
# 502/503/504 are classic gateway/service-layer transients. 429 is
# deliberately excluded because the current 2s retry spacing would just
# stack rate-limit violations — if 429 ever becomes a real failure mode
# we'll need backoff first.
_TRANSIENT_HTTP_STATUSES = (408, 502, 503, 504)


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
            "{}={}".format(k, _quote(v, safe=" /=+")) for k, v in all_headers.items()
        )
        return "{}|{}".format(url, header_str)
    return url


def _cache_bust_url(url):
    """Append a unique query parameter so Kodi treats each play as a fresh URL.

    Replaying the same resolved URL after a stop causes Kodi to try to open
    the outer plugin:// URL as an input stream, and playback never starts.
    Appending a unique query parameter gives Kodi a unique cache key each
    time. nzbdav ignores unknown query parameters on file requests.
    """
    separator = "&" if "?" in url else "?"
    return "{}{}nzbdav_play={}".format(url, separator, int(time.time() * 1000))


def _clear_kodi_playback_state(params=None):
    """Delete Kodi's stored bookmarks, settings, and streamdetails for this play.

    Kodi saves a bookmark (resume point), video settings, and stream details
    keyed on the *outer* plugin URL — the URL Kodi first tried to play, not
    the resolved stream URL. When the user replays the same plugin URL, Kodi
    auto-resumes from the bookmark, which triggers a bug where CVideoPlayer
    tries to reopen the plugin URL itself as an input stream and fails with
    ``OpenInputStream - error opening [plugin://...]``. Playback never
    starts and the user sees dialog 30121.

    Deleting the stored state before each play forces Kodi to treat every
    play as a fresh first play, which bypasses the broken resume pipeline.

    Called from the resolve flow with the params that led to this play so
    we can also target the TMDBHelper URL (not just our own plugin URL).
    """
    try:
        # Skip DB access while something is playing to avoid contending
        # with Kodi's internal vacuum (Textures13.db / MyVideos131.db)
        # which can stall the decoder and freeze playback.
        if xbmc.Player().isPlayingVideo():
            xbmc.log(
                "NZB-DAV: Skipping playback-state cleanup — video is playing",
                xbmc.LOGDEBUG,
            )
            return

        import glob
        import os
        import re
        import sqlite3
        import sys

        db_dir = xbmcvfs.translatePath("special://database/")
        db_files = sorted(glob.glob(os.path.join(db_dir, "MyVideos*.db")))
        if not db_files:
            return
        db_path = db_files[-1]

        target_ids = set()
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            cur = conn.cursor()

            # 1. Our own plugin URL — exact match
            if sys.argv and len(sys.argv) >= 1:
                own_url = sys.argv[0]
                if len(sys.argv) > 2 and sys.argv[2]:
                    own_url += sys.argv[2]
                cur.execute(
                    "SELECT idFile FROM files WHERE strFilename = ?", (own_url,)
                )
                for (id_file,) in cur.fetchall():
                    target_ids.add(id_file)

            # 2. TMDBHelper outer URL — match by tmdb_id (param order varies)
            tmdb_id = (params or {}).get("tmdb_id", "")
            if tmdb_id:
                cur.execute(
                    "SELECT idFile, strFilename FROM files "
                    "WHERE strFilename LIKE ? AND strFilename LIKE ?",
                    (
                        "plugin://plugin.video.themoviedb.helper/%",
                        "%tmdb_id=" + tmdb_id + "%",
                    ),
                )
                id_pattern = re.compile(
                    r"tmdb_id=" + re.escape(tmdb_id) + r"(?:[^0-9]|$)"
                )
                for id_file, filename in cur.fetchall():
                    if id_pattern.search(filename):
                        target_ids.add(id_file)

            if not target_ids:
                return

            for id_file in target_ids:
                cur.execute("DELETE FROM bookmark WHERE idFile = ?", (id_file,))
                cur.execute("DELETE FROM settings WHERE idFile = ?", (id_file,))
                cur.execute("DELETE FROM streamdetails WHERE idFile = ?", (id_file,))
                cur.execute("DELETE FROM files WHERE idFile = ?", (id_file,))
            conn.commit()

        xbmc.log(
            "NZB-DAV: Cleared Kodi playback state for {} file(s)".format(
                len(target_ids)
            ),
            xbmc.LOGINFO,
        )
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Failed to clear Kodi playback state: {}".format(e),
            xbmc.LOGWARNING,
        )


def _url_path(url):
    """Return the path portion of a URL, lowercased, for mime detection."""
    from urllib.parse import urlsplit

    return urlsplit(url).path.lower()


def _make_playable_listitem(url, headers):
    """Create a ListItem with URL and optional HTTP auth headers.

    Uses Kodi's pipe-separated header syntax on the URL.
    """
    play_url = _build_play_url(url, headers)

    xbmc.log("NZB-DAV: Play URL set (redacted)", xbmc.LOGDEBUG)
    li = xbmcgui.ListItem(path=play_url)
    # Skip HEAD request — nzbdav doesn't advertise Accept-Ranges on HEAD
    # which causes CFileCache to fail. Kodi will discover range support
    # on the first GET request instead.
    li.setContentLookup(False)
    # Set mime type based on file extension so Kodi doesn't need HEAD.
    # Strip query/fragment first so cache-busted URLs still detect correctly.
    path = _url_path(url)
    if path.endswith(".mkv"):
        li.setMimeType("video/x-matroska")
    elif path.endswith(".mp4") or path.endswith(".m4v"):
        li.setMimeType("video/mp4")
    elif path.endswith(".avi"):
        li.setMimeType("video/x-msvideo")
    else:
        li.setMimeType("video/x-matroska")
    return li


def _apply_proxy_mime(li, stream_url, stream_info):
    """Set mime type and any info metadata on a proxy ListItem."""
    proxy_url = li.getPath()
    if stream_info.get("remux"):
        xbmc.log(
            "NZB-DAV: Playing via remux proxy: {}".format(proxy_url),
            xbmc.LOGINFO,
        )
        li.setMimeType("video/x-matroska")
        duration = stream_info.get("duration_seconds")
        if duration:
            info_tag = li.getVideoInfoTag()
            info_tag.setDuration(int(duration))
    elif stream_info.get("faststart"):
        xbmc.log(
            "NZB-DAV: Playing via faststart proxy: {}".format(proxy_url),
            xbmc.LOGINFO,
        )
        li.setMimeType("video/mp4")
    else:
        xbmc.log(
            "NZB-DAV: Playing via pass-through proxy: {}".format(proxy_url),
            xbmc.LOGINFO,
        )
        path = _url_path(stream_url)
        if path.endswith(".mp4") or path.endswith(".m4v"):
            li.setMimeType("video/mp4")
        elif path.endswith(".avi"):
            li.setMimeType("video/x-msvideo")
        else:
            li.setMimeType("video/x-matroska")


def _play_direct(handle, stream_url, stream_headers):
    """Play a stream through the local service proxy.

    Every file type routes through the service proxy so Kodi never opens the
    remote WebDAV URL directly. This avoids Kodi's PROPFIND scan of the
    parent directory (nzbdav's WebDAV returns localhost:8080 hrefs that
    break Kodi's directory parser and cascade into an Open failure) and
    sidesteps pipe-header auth quirks on MKV.

    The proxy picks the right mode per file: MP4 gets Tier 1-3 faststart or
    MKV remux; MKV/AVI/other get a range-capable pass-through.
    """
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

        if stream_info.get("direct"):
            xbmc.log(
                "NZB-DAV: MP4 already faststart, direct play: {}".format(stream_url),
                xbmc.LOGINFO,
            )
            bust_url = _cache_bust_url(stream_url)
            li = _make_playable_listitem(bust_url, stream_headers)
            xbmcplugin.setResolvedUrl(handle, True, li)

            home = xbmcgui.Window(10000)
            play_url = _build_play_url(bust_url, stream_headers)
            home.setProperty("nzbdav.stream_url", play_url)
            home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
            home.setProperty("nzbdav.active", "true")
            return

        li = xbmcgui.ListItem(path=proxy_url)
        li.setContentLookup(False)
        _apply_proxy_mime(li, stream_url, stream_info)

        xbmcplugin.setResolvedUrl(handle, True, li)

        home = xbmcgui.Window(10000)
        home.setProperty("nzbdav.stream_url", proxy_url)
        home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
        home.setProperty("nzbdav.active", "true")
        return

    bust_url = _cache_bust_url(stream_url)
    play_url = _build_play_url(bust_url, stream_headers)
    xbmc.log(
        "NZB-DAV: Playing direct (no proxy) (handle={}): {}".format(handle, bust_url),
        xbmc.LOGINFO,
    )

    li = _make_playable_listitem(bust_url, stream_headers)
    xbmcplugin.setResolvedUrl(handle, True, li)

    home = xbmcgui.Window(10000)
    home.setProperty("nzbdav.stream_url", play_url)
    home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
    home.setProperty("nzbdav.active", "true")


def _play_via_proxy(stream_url, stream_headers):
    """Play a stream for the resolve_and_play (service-side) path.

    Routes everything through the service proxy for the same reasons as
    _play_direct — see that function's docstring.
    """
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

        if stream_info.get("direct"):
            xbmc.log(
                "NZB-DAV: MP4 already faststart, direct play: {}".format(stream_url),
                xbmc.LOGINFO,
            )
            bust_url = _cache_bust_url(stream_url)
            li = _make_playable_listitem(bust_url, stream_headers)
            xbmc.Player().play(li.getPath(), li)
            return

        li = xbmcgui.ListItem(path=proxy_url)
        li.setContentLookup(False)
        _apply_proxy_mime(li, stream_url, stream_info)
        xbmc.Player().play(proxy_url, li)
        return

    bust_url = _cache_bust_url(stream_url)
    li = _make_playable_listitem(bust_url, stream_headers)
    xbmc.log("NZB-DAV: Playing direct (no proxy): {}".format(stream_url), xbmc.LOGINFO)
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


def _poll_once(nzo_id, title, monitor):
    """Poll nzbdav queue API and history API in parallel.

    Args:
        nzo_id: nzbdav job identifier to poll.
        title: Human-readable title used for log messages.
        monitor: xbmc.Monitor instance passed through to
            probe_webdav_reachable so the probe's retry wait
            cooperates with Kodi shutdown.

    Returns:
        A tuple of (job_status, history_status, error_type):
        - job_status: Dict from the queue API when the job is active, or None
          when the job is missing from the queue.
        - history_status: Dict from the history API when the job completed, or
          None when not present.
        - error_type: None when polling succeeds; otherwise the error string
          returned by probe_webdav_reachable() when both APIs return None.
          One of "auth_failed", "server_error", or "connection_error".

    Side effects:
        Spawns two threads to call get_job_status() and get_job_history().
        Performs HTTP requests to nzbdav queue/history endpoints and, when
        neither returns data, a WebDAV reachability probe.
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
    # so we don't falsely conclude the job is missing.
    if history_status[0] is None and job_status[0] is None:
        _, error = probe_webdav_reachable(monitor=monitor, max_retries=1, retry_delay=1)
        error_type[0] = error

    xbmc.log(
        "NZB-DAV: Poll result - job_status={} history_status={} error_type={}".format(
            job_status[0], history_status[0], error_type[0]
        ),
        xbmc.LOGDEBUG,
    )
    return job_status[0], history_status[0], error_type[0]


def _show_submit_error_dialog(submit_error):
    """Show a Kodi modal dialog reporting nzbdav's actual error message.

    Truncates the message to 200 chars (on top of the 500-char cap
    already applied in submit_nzb) and falls back to a clear placeholder
    when nzbdav returned an empty body.
    """
    message = submit_error["message"][:200] or "(no error message)"
    xbmcgui.Dialog().ok(
        _addon_name(),
        _fmt(30124, submit_error["status"], message),
    )


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
    last_submit_error = None  # tracks the most recent HTTP error during retries
    monitor = xbmc.Monitor()
    for attempt in range(1, max_submit_retries + 1):
        nzo_id, submit_error = submit_nzb(nzb_url, title)
        if nzo_id:
            break

        if submit_error:
            last_submit_error = submit_error
            status = submit_error["status"]
            # Transient gateway/service issues — preserve the existing
            # retry behavior. nzbdav restarting or its upstream proxy
            # hiccupping is exactly the case retry was designed for.
            if status in _TRANSIENT_HTTP_STATUSES:
                xbmc.log(
                    "NZB-DAV: Submit attempt {}/{} hit transient HTTP {}: {}".format(
                        attempt, max_submit_retries, status, submit_error["message"]
                    ),
                    xbmc.LOGWARNING,
                )
                # fall through to the retry-wait below
            else:
                # Non-transient HTTP error: 4xx (bad apikey, malformed,
                # conflict), 500/501 (definite server-side rejection —
                # duplicate, internal error, not implemented), or any
                # other unclassified 5xx. Retrying these is futile and
                # just delays the diagnostic.
                xbmc.log(
                    "NZB-DAV: Submit failed with HTTP {}, not retrying: {}".format(
                        status, submit_error["message"]
                    ),
                    xbmc.LOGERROR,
                )
                _show_submit_error_dialog(submit_error)
                return None, None
        else:
            # (None, None) — non-HTTP transient (connection refused,
            # JSON decode error, etc.). Retry as before.
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
        # Retries exhausted. If the last error we saw was a transient
        # HTTP error (502/503/504), surface its actual body — that's
        # more useful than the generic "check your settings" string.
        # Otherwise (pure connection errors), fall back to the generic
        # dialog.
        if last_submit_error:
            xbmc.log(
                "NZB-DAV: All {} submit attempts failed for '{}', "
                "last HTTP {}: {}".format(
                    max_submit_retries,
                    title,
                    last_submit_error["status"],
                    last_submit_error["message"],
                ),
                xbmc.LOGERROR,
            )
            _show_submit_error_dialog(last_submit_error)
            return None, None
        xbmc.log(
            "NZB-DAV: All {} submit attempts failed for '{}'. "
            "Check nzbdav URL and API key in settings.".format(
                max_submit_retries, title
            ),
            xbmc.LOGERROR,
        )
        xbmcgui.Dialog().ok(_addon_name(), _string(30098))
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
    no_video_retries = 0
    max_no_video_retries = 5

    while True:
        iteration += 1
        if iteration > MAX_POLL_ITERATIONS:
            xbmc.log(
                "NZB-DAV: Max poll iterations ({}) reached for nzo_id={}".format(
                    MAX_POLL_ITERATIONS, nzo_id
                ),
                xbmc.LOGERROR,
            )
            xbmcgui.Dialog().ok(_addon_name(), _string(30099))
            cancel_job(nzo_id)
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
            xbmcgui.Dialog().ok(_addon_name(), _fmt(30099, int(elapsed)))
            cancel_job(nzo_id)
            return None, None

        if dialog.iscanceled():
            xbmc.log(
                "NZB-DAV: User cancelled resolve for nzo_id={}".format(nzo_id),
                xbmc.LOGINFO,
            )
            cancel_job(nzo_id)
            return None, None

        job_status, history, webdav_error = _poll_once(nzo_id, title, monitor)

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
                xbmcgui.Dialog().ok(_addon_name(), _string(30100))
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
            fail_msg = history.get("fail_message", "")
            xbmc.log(
                "NZB-DAV: Download failed for nzo_id={} (title='{}'): {}".format(
                    nzo_id, title, fail_msg or "unknown reason"
                ),
                xbmc.LOGERROR,
            )
            error_text = fail_msg if fail_msg else _string(30100)
            xbmcgui.Dialog().ok(_addon_name(), error_text)
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
                no_video_retries += 1
                if no_video_retries >= max_no_video_retries:
                    xbmc.log(
                        "NZB-DAV: Download completed but no video file found "
                        "at '{}' after {} attempts (storage='{}')".format(
                            webdav_folder, no_video_retries, storage
                        ),
                        xbmc.LOGERROR,
                    )
                    xbmcgui.Dialog().ok(_addon_name(), _string(30120))
                    return None, None
                xbmc.log(
                    "NZB-DAV: Completed but no video found at '{}', "
                    "retry {}/{} (storage='{}')...".format(
                        webdav_folder,
                        no_video_retries,
                        max_no_video_retries,
                        storage,
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
            xbmcgui.Dialog().ok(_addon_name(), _string(_ERROR_MESSAGES["auth_failed"]))
            # Deliberately NOT calling cancel_job here. The WebDAV auth
            # failure is an addon-side observation problem (the addon
            # can't read the file the job produced), not a job-side
            # problem. The job is presumably running fine on nzbdav and
            # cancelling it would be destructive — the user's nzbdav UI
            # would show a vanished download for no apparent reason.
            return None, None

        if webdav_error == "server_error":
            xbmc.log(
                "NZB-DAV: WebDAV server error, will retry on next poll",
                xbmc.LOGWARNING,
            )

        if monitor.waitForAbort(poll_interval):
            # Kodi is shutting down
            xbmc.log("NZB-DAV: Kodi shutdown detected, aborting resolve", xbmc.LOGINFO)
            cancel_job(nzo_id)
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
        xbmcgui.Dialog().ok(_addon_name(), _string(30096))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
        return

    poll_interval, download_timeout = _get_poll_settings()

    dialog = xbmcgui.DialogProgress()
    dialog.create(_addon_name(), _string(30097))

    try:
        stream_url, stream_headers = _poll_until_ready(
            nzb_url, title, dialog, poll_interval, download_timeout
        )
        if stream_url:
            _clear_kodi_playback_state(params)
            _play_direct(handle, stream_url, stream_headers)
        else:
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
    except Exception as e:
        xbmc.log("NZB-DAV: Unexpected error in resolve: {}".format(e), xbmc.LOGERROR)
        xbmcgui.Dialog().ok(_addon_name(), "Error: {}".format(str(e)))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
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
            _clear_kodi_playback_state()
            _play_via_proxy(stream_url, stream_headers)
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Unexpected error in resolve_and_play: {}".format(e), xbmc.LOGERROR
        )
        xbmcgui.Dialog().ok(_addon_name(), "Error: {}".format(str(e)))
    finally:
        dialog.close()
