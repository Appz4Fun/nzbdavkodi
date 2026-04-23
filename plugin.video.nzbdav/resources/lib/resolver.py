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
    find_queued_by_name,
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
_POLL_INTERVAL_MIN = 1
_POLL_INTERVAL_MAX = 60
_DOWNLOAD_TIMEOUT_MIN = 60
_DOWNLOAD_TIMEOUT_MAX = 86400
# HTTP status codes the submit retry loop treats as transient and worth
# retrying. RFC 9110 explicitly calls 408 retry-friendly ("client may
# assume the server closed the connection due to inactivity and retry").
# 502/503/504 are classic gateway/service-layer transients. 429 is
# deliberately excluded because the current 2s retry spacing would just
# stack rate-limit violations — if 429 ever becomes a real failure mode
# we'll need backoff first.
_TRANSIENT_HTTP_STATUSES = (408, 502, 503, 504)
_DB_DISCOVERY_ERRORS = (
    AttributeError,
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)
_RESOLVE_RUNTIME_ERRORS = (
    AttributeError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


def _clamp_int_setting(setting_id, value, lo, hi):
    """Clamp an integer setting and log when user input was out of range."""
    clamped = value
    if value < lo:
        clamped = lo
    elif value > hi:
        clamped = hi
    if clamped != value:
        xbmc.log(
            "NZB-DAV: Setting {}={} out of range [{}..{}]; clamping to {}".format(
                setting_id, value, lo, hi, clamped
            ),
            xbmc.LOGWARNING,
        )
    return clamped


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
    """Delete Kodi's stored resume bookmark for this play.

    Kodi saves a bookmark (resume point) keyed on the *outer* plugin URL —
    the URL Kodi first tried to play, not the resolved stream URL. When the
    user replays the same plugin URL, Kodi auto-resumes from the bookmark,
    which triggers a bug where CVideoPlayer tries to reopen the plugin URL
    itself as an input stream and fails with
    ``OpenInputStream - error opening [plugin://...]``. Playback never
    starts and the user sees dialog 30121.

    Deleting the bookmark before each play forces Kodi to treat every play
    as a fresh first play, which bypasses the broken resume pipeline.

    Called from the resolve flow with the params that led to this play so
    we can also target the TMDBHelper URL (not just our own plugin URL).

    Safety model: this code mutates Kodi's primary video database, so the
    mutation surface is kept as narrow as possible:

    * Only the ``bookmark`` table is modified. The ``files``, ``settings``,
      and ``streamdetails`` tables are left alone — a row in ``files``
      without a matching ``bookmark`` row is the "fresh play" state Kodi
      already handles correctly, and not touching the foreign-key parent
      avoids cascading into unrelated library state.
    * The SQLite busy timeout is short (2s). If Kodi is actively writing we
      bail out rather than contend — a missed cleanup is recoverable; a
      long stall on the resolve path is not.
    * LIKE wildcards (``%``, ``_``, ``\\``) in ``tmdb_id`` are escaped so
      an odd TMDBHelper param value cannot match unrelated rows.
    * ``sqlite3.OperationalError`` (the "database is locked" case) is
      caught separately and logged at DEBUG; everything else is logged at
      WARNING so real problems surface in the Kodi log.
    """
    import sqlite3

    db_path = _locate_kodi_video_db()
    if not db_path:
        return

    try:
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            cur = conn.cursor()
            target_ids = _collect_kodi_playback_target_ids(cur, params)

            if not target_ids:
                return

            # Narrowest possible mutation: only clear bookmark rows. The
            # files/settings/streamdetails rows stay intact — Kodi will
            # treat the file as "never resumed" on the next play, which is
            # exactly the state we want.
            for id_file in target_ids:
                cur.execute("DELETE FROM bookmark WHERE idFile = ?", (id_file,))

        xbmc.log(
            "NZB-DAV: Cleared bookmark for {} file(s)".format(len(target_ids)),
            xbmc.LOGINFO,
        )
    except sqlite3.OperationalError as e:
        # "database is locked" / busy timeout. Kodi holds the writer; we
        # skip this cleanup and let the next resolve retry.
        xbmc.log(
            "NZB-DAV: MyVideos DB busy, skipping bookmark cleanup: {}".format(e),
            xbmc.LOGDEBUG,
        )
    except sqlite3.Error as e:
        xbmc.log(
            "NZB-DAV: SQLite error during bookmark cleanup: {}".format(e),
            xbmc.LOGWARNING,
        )


def _locate_kodi_video_db():
    """Return the newest MyVideos DB path, or None when unavailable."""
    try:
        # Skip DB access while something is playing to avoid contending
        # with Kodi's internal vacuum (Textures13.db / MyVideos131.db)
        # which can stall the decoder and freeze playback.
        if xbmc.Player().isPlayingVideo():
            xbmc.log(
                "NZB-DAV: Skipping playback-state cleanup — video is playing",
                xbmc.LOGDEBUG,
            )
            return None

        import glob
        import os

        db_dir = xbmcvfs.translatePath("special://database/")
        db_files = sorted(glob.glob(os.path.join(db_dir, "MyVideos*.db")))
    except _DB_DISCOVERY_ERRORS as error:
        xbmc.log(
            "NZB-DAV: Failed to locate MyVideos DB for bookmark cleanup: {}".format(
                error
            ),
            xbmc.LOGWARNING,
        )
        return None

    if not db_files:
        return None
    return db_files[-1]


def _like_escape(value):
    """Escape SQLite LIKE wildcards using ESCAPE '\\'."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _add_own_plugin_target_ids(cur, target_ids):
    """Add bookmark targets for the current plugin URL."""
    import sys

    if not sys.argv:
        return
    own_url = sys.argv[0]
    if len(sys.argv) > 2 and sys.argv[2]:
        own_url += sys.argv[2]
    cur.execute("SELECT idFile FROM files WHERE strFilename = ?", (own_url,))
    for (id_file,) in cur.fetchall():
        target_ids.add(id_file)


def _add_tmdb_helper_target_ids(cur, target_ids, params):
    """Add bookmark targets for matching TMDBHelper URLs."""
    import re

    tmdb_id = (params or {}).get("tmdb_id", "")
    if not tmdb_id:
        return

    safe_tmdb_id = _like_escape(tmdb_id)
    cur.execute(
        "SELECT idFile, strFilename FROM files "
        "WHERE strFilename LIKE ? ESCAPE '\\' "
        "AND strFilename LIKE ? ESCAPE '\\'",
        (
            "plugin://plugin.video.themoviedb.helper/%",
            "%tmdb_id=" + safe_tmdb_id + "%",
        ),
    )
    id_pattern = re.compile(r"tmdb_id=" + re.escape(tmdb_id) + r"(?:[^0-9]|$)")
    for id_file, filename in cur.fetchall():
        if id_pattern.search(filename):
            target_ids.add(id_file)


def _collect_kodi_playback_target_ids(cur, params):
    """Collect bookmark row ids that should be cleared for the next play."""
    target_ids = set()
    _add_own_plugin_target_ids(cur, target_ids)
    _add_tmdb_helper_target_ids(cur, target_ids, params)
    return target_ids


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
    interval = _clamp_int_setting(
        "poll_interval", interval, _POLL_INTERVAL_MIN, _POLL_INTERVAL_MAX
    )
    timeout = _clamp_int_setting(
        "download_timeout",
        timeout,
        _DOWNLOAD_TIMEOUT_MIN,
        _DOWNLOAD_TIMEOUT_MAX,
    )
    return interval, timeout


def _storage_to_webdav_path(storage):
    """Convert nzbdav storage path to WebDAV content path.

    Handles two server flavours that return different ``storage`` values
    in their SABnzbd history:

    * Upstream nzbdav (Node): returns a filesystem path like
      ``/mnt/nzbdav/completed-symlinks/uncategorized/Name``. Strip the
      mount prefix and re-root under ``/content/``.
    * nzbdav-rs (Rust port): returns the WebDAV path directly, e.g.
      ``/content/uncategorized/Name/`` or (no-category submit) just
      ``/content/Name/``. Pass through as-is with trailing slash.

    Fallback (unknown shape): take the last two path components as
    ``{category}/{name}`` under ``/content/``. Good enough for
    SABnzbd-style layouts we haven't seen yet.
    """
    # nzbdav-rs already returns a /content/... path.
    if storage.startswith("/content/"):
        return storage.rstrip("/") + "/"

    # Upstream nzbdav's completed-symlinks layout.
    prefix = "/mnt/nzbdav/completed-symlinks/"
    if storage.startswith(prefix):
        relative = storage[len(prefix) :]
    else:
        # Fallback: use the last two path components (category/name).
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


def _existing_completed_stream(title):
    """Return an already-downloaded stream URL when the title exists."""
    existing = find_completed_by_name(title)
    if not existing:
        return None

    xbmc.log(
        "NZB-DAV: '{}' already downloaded, streaming directly".format(title),
        xbmc.LOGINFO,
    )
    webdav_folder = _storage_to_webdav_path(existing["storage"])
    video_path = find_video_file(webdav_folder)
    if not video_path:
        return None
    return get_webdav_stream_url_for_path(video_path)


def _submit_nzb_with_retries(nzb_url, title, monitor, max_submit_retries=3):
    """Submit an NZB with the existing retry and error-dialog behavior."""
    xbmc.log("NZB-DAV: Submitting NZB for '{}'".format(title), xbmc.LOGINFO)
    last_submit_error = None

    for attempt in range(1, max_submit_retries + 1):
        nzo_id, submit_error = _submit_nzb_with_ui_pump(nzb_url, title, dialog, monitor)
        if nzo_id:
            return nzo_id

        if submit_error:
            last_submit_error = submit_error
            status = submit_error["status"]
            if status in _TRANSIENT_HTTP_STATUSES:
                xbmc.log(
                    "NZB-DAV: Submit attempt {}/{} hit transient HTTP {}: {}".format(
                        attempt, max_submit_retries, status, submit_error["message"]
                    ),
                    xbmc.LOGWARNING,
                )
            else:
                xbmc.log(
                    "NZB-DAV: Submit failed with HTTP {}, not retrying: {}".format(
                        status, submit_error["message"]
                    ),
                    xbmc.LOGERROR,
                )
                _show_submit_error_dialog(submit_error)
                return None
        else:
            xbmc.log(
                "NZB-DAV: Submit attempt {}/{} failed for '{}'".format(
                    attempt, max_submit_retries, title
                ),
                xbmc.LOGWARNING,
            )

        if attempt < max_submit_retries and monitor.waitForAbort(2):
            return None

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
        return None

    xbmc.log(
        "NZB-DAV: All {} submit attempts failed for '{}'. "
        "Check nzbdav URL and API key in settings.".format(max_submit_retries, title),
        xbmc.LOGERROR,
    )
    xbmcgui.Dialog().ok(_addon_name(), _string(30098))
    return None


def _abort_poll_before_fetch(
    iteration, elapsed, download_timeout, dialog, nzo_id, title
):
    """Handle the early-return poll abort conditions."""
    if iteration > MAX_POLL_ITERATIONS:
        xbmc.log(
            "NZB-DAV: Max poll iterations ({}) reached for nzo_id={}".format(
                MAX_POLL_ITERATIONS, nzo_id
            ),
            xbmc.LOGERROR,
        )
        xbmcgui.Dialog().ok(_addon_name(), _string(30099))
        cancel_job(nzo_id)
        return True

    if elapsed >= download_timeout:
        xbmc.log(
            "NZB-DAV: Download timed out after {}s for nzo_id={} (title='{}'). "
            "Check the nzbdav queue for stalled jobs or increase the "
            "download timeout in addon settings.".format(int(elapsed), nzo_id, title),
            xbmc.LOGERROR,
        )
        xbmcgui.Dialog().ok(_addon_name(), _fmt(30099, int(elapsed)))
        cancel_job(nzo_id)
        return True

    if dialog.iscanceled():
        xbmc.log(
            "NZB-DAV: User cancelled resolve for nzo_id={}".format(nzo_id),
            xbmc.LOGINFO,
        )
        cancel_job(nzo_id)
        return True

    return False


def _status_dialog_message(status, percentage):
    """Return the progress-dialog text for a queue status update."""
    msg_id = _STATUS_MESSAGES.get(status)
    if not msg_id:
        return "Status: {}".format(status)
    if msg_id == 30105:
        return _fmt(msg_id, percentage)
    return _string(msg_id)


def _handle_job_status(job_status, nzo_id, dialog, last_status):
    """Apply queue-status updates and detect terminal failed states."""
    if not job_status:
        return False, last_status

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
        return True, last_status

    progress = min(int(percentage or 0), 100)
    dialog.update(progress, _status_dialog_message(status, percentage))
    return False, last_status


def _handle_history_result(history, title, no_video_retries, max_no_video_retries):
    """Handle history-based completion and failure states."""
    if not history:
        return False, None, None, no_video_retries

    if history["status"] == "Failed":
        fail_msg = history.get("fail_message", "")
        xbmc.log(
            "NZB-DAV: Download failed for nzo_id={} (title='{}'): {}".format(
                history.get("nzo_id", "unknown"), title, fail_msg or "unknown reason"
            ),
            xbmc.LOGERROR,
        )
        error_text = fail_msg if fail_msg else _string(30100)
        xbmcgui.Dialog().ok(_addon_name(), error_text)
        return True, None, None, no_video_retries

    if history["status"] != "Completed":
        return False, None, None, no_video_retries

    storage = history["storage"]
    webdav_folder = _storage_to_webdav_path(storage)
    video_path = find_video_file(webdav_folder)
    if video_path:
        stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
        xbmc.log(
            "NZB-DAV: File available, streaming '{}' via WebDAV".format(video_path),
            xbmc.LOGINFO,
        )
        if not _validate_stream_url(stream_url, stream_headers):
            xbmc.log(
                "NZB-DAV: Stream validation failed for '{}', "
                "attempting playback anyway".format(video_path),
                xbmc.LOGWARNING,
            )
        return True, stream_url, stream_headers, no_video_retries

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
        return True, None, None, no_video_retries

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
    return False, None, None, no_video_retries


def _handle_webdav_error(nzo_id, webdav_error):
    """Handle terminal WebDAV auth failures and retryable server errors."""
    if webdav_error == "auth_failed":
        xbmc.log(
            "NZB-DAV: WebDAV authentication failed for nzo_id={}. "
            "Check WebDAV username and password in addon settings.".format(nzo_id),
            xbmc.LOGERROR,
        )
        xbmcgui.Dialog().ok(_addon_name(), _string(_ERROR_MESSAGES["auth_failed"]))
        return True

    if webdav_error == "server_error":
        xbmc.log(
            "NZB-DAV: WebDAV server error, will retry on next poll",
            xbmc.LOGWARNING,
        )
    return False


def _handle_resolve_exception(label, error, handle=None):
    """Log and surface a non-fatal resolve error to Kodi."""
    xbmc.log("NZB-DAV: Unexpected error in {}: {}".format(label, error), xbmc.LOGERROR)
    xbmcgui.Dialog().ok(_addon_name(), "Error: {}".format(str(error)))
    if handle is not None:
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()


def _poll_until_ready(nzb_url, title, dialog, poll_interval, download_timeout):
    """Submit NZB and poll until download completes.

    Returns ``(stream_url, stream_headers)`` on success, or ``(None, None)``
    on failure (timeout, cancellation, server error, etc.).  All user
    notifications are issued inside this function; the caller only needs to
    decide what to do with the resulting stream URL.
    """
    existing_stream = _existing_completed_stream(title)
    if existing_stream is not None:
        return existing_stream

    monitor = xbmc.Monitor()
    nzo_id = _submit_nzb_with_retries(nzb_url, title, monitor)
    if not nzo_id:
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
        elapsed = time.time() - start_time
        if _abort_poll_before_fetch(
            iteration, elapsed, download_timeout, dialog, nzo_id, title
        ):
            return None, None

        job_status, history, webdav_error = _poll_once(nzo_id, title, monitor)

        should_stop, last_status = _handle_job_status(
            job_status, nzo_id, dialog, last_status
        )
        if should_stop:
            return None, None

        should_stop, stream_url, stream_headers, no_video_retries = (
            _handle_history_result(
                history, title, no_video_retries, max_no_video_retries
            )
        )
        if stream_url:
            return stream_url, stream_headers
        if should_stop:
            return None, None

        if _handle_webdav_error(nzo_id, webdav_error):
            # Deliberately NOT calling cancel_job here. The WebDAV auth
            # failure is an addon-side observation problem (the addon
            # can't read the file the job produced), not a job-side
            # problem. The job is presumably running fine on nzbdav and
            # cancelling it would be destructive — the user's nzbdav UI
            # would show a vanished download for no apparent reason.
            return None, None

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
    except _RESOLVE_RUNTIME_ERRORS as error:
        _handle_resolve_exception("resolve", error, handle=handle)
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
    except _RESOLVE_RUNTIME_ERRORS as error:
        _handle_resolve_exception("resolve_and_play", error)
    finally:
        dialog.close()
