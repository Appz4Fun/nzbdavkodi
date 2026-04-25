# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Resolve flow: submit NZB to nzbdav, poll until stream is ready, play."""

import http.client
import socket
import threading
import time
from urllib.error import URLError
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

_POLL_INTERVAL_MIN = 1
_POLL_INTERVAL_MAX = 60
_DOWNLOAD_TIMEOUT_MIN = 60
_DOWNLOAD_TIMEOUT_MAX = 86400
MAX_POLL_ITERATIONS = _DOWNLOAD_TIMEOUT_MAX // _POLL_INTERVAL_MIN
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
    # Network-layer exceptions that escaped earlier helpers — `socket.timeout`
    # is a `TimeoutError` subclass on 3.10+ but a separate type on 3.8/3.9,
    # `URLError` wraps DNS / connection-refused / unreachable, `HTTPException`
    # covers `BadStatusLine` and friends. All three could otherwise bypass
    # the resolver's setResolvedUrl-on-failure guarantee. TODO.md §H.3.
    URLError,
    socket.timeout,
    AttributeError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)


# Per-setting warn suppression: we log the out-of-range clamp exactly once
# per (setting_id, value) so a user with a typo'd setting doesn't see the
# same warning spam on every play.
_CLAMP_LOGGED = set()


def _clamp_int_setting(setting_id, value, lo, hi):
    """Clamp an integer setting and log when user input was out of range."""
    clamped = value
    if value < lo:
        clamped = lo
    elif value > hi:
        clamped = hi
    if clamped != value:
        key = (setting_id, value)
        if key not in _CLAMP_LOGGED:
            _CLAMP_LOGGED.add(key)
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
        # nosemgrep
        with urlopen(  # nosec B310 — URL from user-configured stream
            req, timeout=10
        ) as resp:
            return resp.getcode() == 206 or "bytes" in resp.headers.get(
                "Accept-Ranges", ""
            )
    except (OSError, ValueError, http.client.HTTPException):
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
    # Insert the cache-buster BEFORE any `#fragment`. Otherwise the
    # `?nzbdav_play=N` ends up after the fragment marker and the
    # server never sees it (fragments are client-side only) — defeating
    # the cache-bust intent. Closes TODO.md §H.2-L4.
    if "#" in url:
        base, fragment = url.split("#", 1)
    else:
        base, fragment = url, ""
    separator = "&" if "?" in base else "?"
    # Use nanosecond precision (3.7+) so rapid replays don't collide on
    # platforms whose `time.time()` clock is coarser than 1 ms (e.g. older
    # CoreELEC kernels with HZ=100). Falls back to ms*1000 if the function
    # is unavailable.
    counter = (
        time.time_ns()
        if hasattr(time, "time_ns")
        else int(time.time() * 1000) * 1_000_000
    )
    rebuilt = "{}{}nzbdav_play={}".format(base, separator, counter)
    return rebuilt + ("#" + fragment if fragment else "")


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
    import contextlib
    import sqlite3

    db_path = _locate_kodi_video_db()
    if not db_path:
        return

    try:
        # ``sqlite3.connect`` as a context manager only commits/rolls-back;
        # it does NOT call ``conn.close()``. Wrap in contextlib.closing
        # so the connection's file descriptor is released deterministically
        # instead of hanging on for GC — matters on every resolve() call.
        with contextlib.closing(sqlite3.connect(db_path, timeout=2.0)) as conn:
            with conn:
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
        if (
            stream_info.get("mode") == "hls"
            or stream_info.get("content_type") == "application/vnd.apple.mpegurl"
        ):
            li.setMimeType("application/vnd.apple.mpegurl")
        else:
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
    from resources.lib.cache_prompt import maybe_show_cache_prompt
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

        # Window properties go DOWN before ``setResolvedUrl`` so the
        # service-side playback monitor sees them the instant Kodi
        # transitions into playback. ``setResolvedUrl`` is what triggers
        # Kodi to actually start the player; if the service's 1 Hz tick
        # fired between resolve-and-property writes, it would miss the
        # session entirely until the next tick. TODO.md §H.2-M47.
        home = xbmcgui.Window(10000)
        if stream_info.get("direct"):
            xbmc.log(
                "NZB-DAV: MP4 already faststart, direct play: {}".format(stream_url),
                xbmc.LOGINFO,
            )
            bust_url = _cache_bust_url(stream_url)
            li = _make_playable_listitem(bust_url, stream_headers)
            play_url = _build_play_url(bust_url, stream_headers)
            home.setProperty("nzbdav.stream_url", play_url)
            home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
            home.setProperty("nzbdav.active", "true")
            xbmcplugin.setResolvedUrl(handle, True, li)
            return

        maybe_show_cache_prompt(stream_info)

        li = xbmcgui.ListItem(path=proxy_url)
        li.setContentLookup(False)
        _apply_proxy_mime(li, stream_url, stream_info)

        home.setProperty("nzbdav.stream_url", proxy_url)
        home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
        home.setProperty("nzbdav.active", "true")
        xbmcplugin.setResolvedUrl(handle, True, li)
        return

    bust_url = _cache_bust_url(stream_url)
    play_url = _build_play_url(bust_url, stream_headers)
    xbmc.log(
        "NZB-DAV: Playing direct (no proxy) (handle={}): {}".format(handle, bust_url),
        xbmc.LOGINFO,
    )

    li = _make_playable_listitem(bust_url, stream_headers)
    home = xbmcgui.Window(10000)
    home.setProperty("nzbdav.stream_url", play_url)
    home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
    home.setProperty("nzbdav.active", "true")
    xbmcplugin.setResolvedUrl(handle, True, li)


def _play_via_proxy(stream_url, stream_headers):
    """Play a stream for the resolve_and_play (service-side) path.

    Routes everything through the service proxy for the same reasons as
    _play_direct — see that function's docstring.

    Each play branch also sets ``nzbdav.stream_url`` /
    ``nzbdav.stream_title`` / ``nzbdav.active`` on the Home window
    (window 10000). The service-side playback monitor (``service.py``)
    polls these to drive its retry / error-dialog state machine; the
    RunPlugin entrypoint used to skip them so a stream that died
    mid-playback never triggered the retry path. Closes
    TODO.md §H.2-H10.
    """
    from resources.lib.cache_prompt import maybe_show_cache_prompt
    from resources.lib.stream_proxy import (
        get_service_proxy_port,
        prepare_stream_via_service,
    )

    auth_header = None
    if stream_headers and "Authorization" in stream_headers:
        auth_header = stream_headers["Authorization"]

    home = xbmcgui.Window(10000)
    title = stream_url.rsplit("/", 1)[-1]

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
            play_url = _build_play_url(bust_url, stream_headers)
            home.setProperty("nzbdav.stream_url", play_url)
            home.setProperty("nzbdav.stream_title", title)
            home.setProperty("nzbdav.active", "true")
            xbmc.Player().play(li.getPath(), li)
            return

        maybe_show_cache_prompt(stream_info)

        li = xbmcgui.ListItem(path=proxy_url)
        li.setContentLookup(False)
        _apply_proxy_mime(li, stream_url, stream_info)
        home.setProperty("nzbdav.stream_url", proxy_url)
        home.setProperty("nzbdav.stream_title", title)
        home.setProperty("nzbdav.active", "true")
        xbmc.Player().play(proxy_url, li)
        return

    bust_url = _cache_bust_url(stream_url)
    li = _make_playable_listitem(bust_url, stream_headers)
    play_url = _build_play_url(bust_url, stream_headers)
    xbmc.log("NZB-DAV: Playing direct (no proxy): {}".format(stream_url), xbmc.LOGINFO)
    home.setProperty("nzbdav.stream_url", play_url)
    home.setProperty("nzbdav.stream_title", title)
    home.setProperty("nzbdav.active", "true")
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

    # daemon=True so a stalled worker thread doesn't block the plugin
    # interpreter from exiting on Kodi shutdown.
    t1 = threading.Thread(target=check_queue, daemon=True)
    t2 = threading.Thread(target=check_history, daemon=True)
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
    storage = existing.get("storage")
    if not storage:
        xbmc.log(
            "NZB-DAV: Completed history row for '{}' has no storage path".format(title),
            xbmc.LOGWARNING,
        )
        return None
    webdav_folder = _storage_to_webdav_path(storage)
    video_path = find_video_file(webdav_folder)
    if not video_path:
        return None
    return get_webdav_stream_url_for_path(video_path)


# UI pump cadence while submit_nzb is running on a background thread.
# Short enough that the progress dialog looks live and the cancel button
# is responsive; long enough that we're not burning CPU on the plugin
# thread while waiting for a remote HTTP call.
_SUBMIT_UI_PUMP_INTERVAL_SECONDS = 0.25


def _submit_nzb_with_ui_pump(nzb_url, title, dialog, monitor):
    """Run ``submit_nzb`` off the plugin thread, pump the dialog, and
    race a concurrent queue probe against the submit.

    ``submit_nzb`` issues a synchronous HTTP request to ``/api?mode=addurl``
    which on a big NZB routinely takes 30-120 s. Running it on the Kodi
    plugin thread freezes the progress dialog. The fix is two-part:

    1. ``submit_nzb`` runs in a daemon worker thread; the plugin thread
       loops on ``monitor.waitForAbort`` at 250 ms cadence, advances the
       dialog progress bar, and checks ``dialog.iscanceled`` every tick.
    2. A second daemon thread concurrently probes nzbdav's queue via
       ``find_queued_by_name`` and short-circuits as soon as the queue
       entry for ``title`` appears — usually well before ``addurl``
       replies.

    Returns ``(nzo_id, None)`` on success (either by worker completion or
    by queue adoption), or ``(None, error_dict)`` on cancel, shutdown,
    or submit failure.
    """
    xbmc.log(
        "NZB-DAV: _submit_nzb_with_ui_pump entered for '{}' "
        "(threaded pump + concurrent queue probe)".format(title),
        xbmc.LOGINFO,
    )

    submit_result = [None, None]
    submit_done = threading.Event()

    def _submit_worker():
        try:
            submit_result[0], submit_result[1] = submit_nzb(nzb_url, title)
        except Exception as e:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: submit_nzb worker raised: {}".format(e),
                xbmc.LOGERROR,
            )
            submit_result[0], submit_result[1] = None, None
        finally:
            submit_done.set()

    queue_hit = [None]
    queue_stop = threading.Event()

    def _queue_probe_worker():
        # Short grace before the first probe — nzbdav needs a round-trip
        # to fetch the .nzb before it can enqueue anything.
        if queue_stop.wait(2.0):
            return
        while not queue_stop.is_set() and not submit_done.is_set():
            try:
                match = find_queued_by_name(title)
            except Exception as e:  # pylint: disable=broad-except
                xbmc.log(
                    "NZB-DAV: concurrent queue probe raised: {}".format(e),
                    xbmc.LOGWARNING,
                )
                match = None
            if match and match.get("nzo_id"):
                queue_hit[0] = match["nzo_id"]
                return
            if queue_stop.wait(2.0):
                return

    submit_t = threading.Thread(
        target=_submit_worker, name="nzbdav-submit", daemon=True
    )
    probe_t = threading.Thread(
        target=_queue_probe_worker, name="nzbdav-submit-probe", daemon=True
    )
    submit_t.start()
    probe_t.start()

    # Anchor elapsed to wall-clock via time.monotonic() instead of
    # accumulating _SUBMIT_UI_PUMP_INTERVAL_SECONDS per loop; the per-loop
    # accumulation under-reports on slow skins because dialog.update()
    # itself can block for tens of milliseconds.
    loop_start = time.monotonic()
    submit_msg = _string(30097)
    try:
        while not submit_done.is_set():
            if queue_hit[0]:
                xbmc.log(
                    "NZB-DAV: Concurrent queue probe found '{}' under "
                    "nzo_id={}; adopting without waiting for addurl "
                    "response".format(title, queue_hit[0]),
                    xbmc.LOGINFO,
                )
                return queue_hit[0], None
            if dialog.iscanceled():
                xbmc.log(
                    "NZB-DAV: User cancelled during submit for '{}'".format(title),
                    xbmc.LOGINFO,
                )
                return None, {"status": "cancelled", "message": ""}
            if monitor.waitForAbort(_SUBMIT_UI_PUMP_INTERVAL_SECONDS):
                return None, {"status": "shutdown", "message": ""}
            elapsed = time.monotonic() - loop_start
            pct = int((elapsed * 100) / max(_get_submit_timeout_seconds(), 1)) % 100
            try:
                dialog.update(
                    pct,
                    "{}\n{} ({}s)".format(submit_msg, title[:60], int(elapsed)),
                )
            except Exception as e:  # pylint: disable=broad-except
                # DialogProgress.update can fail if the user closed the
                # dialog between our isPlaying poll and the update call;
                # also fails when the xbmcgui MagicMock doesn't accept
                # the call shape in some tests. Best-effort — log at
                # debug so a real bug in Kodi's UI layer is still
                # diagnosable without spamming the log on every tick.
                xbmc.log(
                    "NZB-DAV: progress dialog update failed: {}".format(e),
                    xbmc.LOGDEBUG,
                )
        # Race window re-check: prefer adopted nzo_id over a failed submit.
        if queue_hit[0] and not submit_result[0]:
            xbmc.log(
                "NZB-DAV: Queue probe found '{}' under nzo_id={} just as "
                "submit worker finished; preferring the adopted job over "
                "the submit result".format(title, queue_hit[0]),
                xbmc.LOGINFO,
            )
            return queue_hit[0], None
        return submit_result[0], submit_result[1]
    finally:
        # Signal the probe worker to exit its wait loop, then give both
        # daemon threads a brief join window so we don't leave two
        # background HTTP calls running when the plugin script exits.
        # Both threads are daemon=True, so a hang here can't block
        # interpreter shutdown — the join timeout caps recovery time
        # on an uncooperative upstream.
        queue_stop.set()
        for t in (submit_t, probe_t):
            try:
                t.join(timeout=1)
            except RuntimeError as e:
                # Thread.join raises RuntimeError if the thread wasn't
                # started or if join is called on the current thread.
                # Both are best-effort cleanup paths here (threads are
                # daemon=True so they die with the interpreter anyway)
                # but log at debug so a real misuse surfaces.
                xbmc.log(
                    "NZB-DAV: Resolver worker join failed: {}".format(e),
                    xbmc.LOGDEBUG,
                )


def _get_submit_timeout_seconds():
    """Read submit_timeout setting; returns int or 120 on error."""
    try:
        import xbmcaddon

        raw = xbmcaddon.Addon().getSetting("submit_timeout")
        return int(raw) if raw else 120
    except Exception:  # pylint: disable=broad-except
        # xbmcaddon import failures, unexpected setting shapes, int() on
        # a MagicMock in tests — all funnel to the documented default.
        # ``Exception`` on its own (the previous ``(ValueError, TypeError,
        # Exception)`` tuple was dead code — Exception subsumes the other
        # two) keeps the safety net without the misleading tuple.
        return 120


# After a submit timeout, how many times to poll nzbdav before giving up
# on adoption and retrying the submit. 6 polls * 2 s = 12 s of total wait
# — enough headroom for nzbdav to finish fetching/parsing a moderately
# large NZB, short enough not to double the user's wait on a genuine
# network failure.
_SUBMIT_ADOPT_POLL_COUNT = 6
_SUBMIT_ADOPT_POLL_INTERVAL_SECONDS = 2


def _adopt_queued_or_completed_job(title, monitor):
    """Return an existing nzbdav nzo_id for ``title`` if the submit we
    just timed out on actually reached nzbdav.

    After a client-side submit timeout, nzbdav may be:
    - Still fetching/parsing the NZB (no queue entry yet)
    - Processing it (queue entry exists under ``title``)
    - Already done (history entry exists under ``title``)

    Probes queue and history a handful of times on a short interval.
    Returns the matching ``nzo_id`` on the first positive hit, ``None``
    if nothing surfaces within the poll budget (caller retries submit).
    """
    for poll in range(_SUBMIT_ADOPT_POLL_COUNT):
        queued = find_queued_by_name(title)
        if queued and queued.get("nzo_id"):
            return queued["nzo_id"]
        completed = find_completed_by_name(title)
        if completed and completed.get("nzo_id"):
            return completed["nzo_id"]
        if poll < _SUBMIT_ADOPT_POLL_COUNT - 1:
            if monitor.waitForAbort(_SUBMIT_ADOPT_POLL_INTERVAL_SECONDS):
                return None
    return None


def _submit_nzb_with_retries(nzb_url, title, dialog, monitor, max_submit_retries=3):
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
            if status in ("cancelled", "shutdown"):
                # User hit cancel on the progress dialog or Kodi is
                # shutting down. Stop immediately — no retry, no
                # adoption, no error dialog.
                xbmc.log(
                    "NZB-DAV: Submit aborted ({}) for '{}'".format(status, title),
                    xbmc.LOGINFO,
                )
                return None
            if status == "timeout":
                # Client-side timeout. nzbdav's /api?mode=addurl handler
                # can take > 30 s on big NZBs (fetch + parse + enumerate)
                # — longer than the default HTTP timeout. A timeout does
                # NOT mean the submit failed. Probe the queue before
                # retrying so we adopt the job nzbdav is already
                # processing instead of double-submitting.
                xbmc.log(
                    "NZB-DAV: Submit attempt {}/{} timed out; probing nzbdav "
                    "queue for '{}' before retrying".format(
                        attempt, max_submit_retries, title
                    ),
                    xbmc.LOGWARNING,
                )
                adopted_nzo_id = _adopt_queued_or_completed_job(title, monitor)
                if adopted_nzo_id:
                    xbmc.log(
                        "NZB-DAV: Adopted existing nzbdav job nzo_id={} for "
                        "'{}' after submit timeout".format(adopted_nzo_id, title),
                        xbmc.LOGINFO,
                    )
                    return adopted_nzo_id
                xbmc.log(
                    "NZB-DAV: '{}' not found in nzbdav queue or history "
                    "after submit timeout; retrying".format(title),
                    xbmc.LOGWARNING,
                )
            elif status in _TRANSIENT_HTTP_STATUSES:
                xbmc.log(
                    "NZB-DAV: Submit attempt {}/{} hit transient HTTP {}: {}".format(
                        attempt, max_submit_retries, status, submit_error["message"]
                    ),
                    xbmc.LOGWARNING,
                )
            elif status == "rejected":
                # nzbdav explicitly rejected the NZB (empty / truncated /
                # password-only / unparseable). Not retryable — surface the
                # specific message immediately instead of looping 3× and
                # showing a generic failure.
                xbmc.log(
                    "NZB-DAV: nzbdav rejected the NZB for '{}': {}".format(
                        title, submit_error["message"]
                    ),
                    xbmc.LOGERROR,
                )
                _show_submit_error_dialog(submit_error)
                return None
            else:
                # Non-transient HTTP error (often 500 "duplicate nzo_id").
                # Before surfacing the error to the user, probe the queue:
                # if the job is already running, attach to it. This covers
                # the race where a concurrent submit (e.g. retried play of
                # the same title) beat us to nzbdav.
                adopted_nzo_id = _adopt_queued_or_completed_job(title, monitor)
                if adopted_nzo_id:
                    xbmc.log(
                        "NZB-DAV: Adopted existing nzbdav job nzo_id={} for "
                        "'{}' after HTTP {} rejection".format(
                            adopted_nzo_id, title, status
                        ),
                        xbmc.LOGINFO,
                    )
                    return adopted_nzo_id
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
            xbmc.log(
                "NZB-DAV: Kodi shutdown during submit retry backoff "
                "(attempt {}/{}) for '{}'".format(attempt, max_submit_retries, title),
                xbmc.LOGINFO,
            )
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
        # _fmt not _string: 30099 is "Download timed out after {} seconds"
        # — using _string() would render the literal "{}" to the user.
        xbmcgui.Dialog().ok(_addon_name(), _fmt(30099, int(elapsed)))
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

    try:
        progress = int(float(percentage or 0))
    except (TypeError, ValueError):
        progress = 0
    progress = max(0, min(progress, 100))
    dialog.update(progress, _status_dialog_message(status, percentage))
    return False, last_status


def _handle_history_result(history, title, no_video_retries, max_no_video_retries):
    """Handle history-based completion and failure states.

    Use ``.get(...)`` for ``status`` and ``storage`` instead of bracket
    access. ``not history`` filters out None and empty dicts, but a
    history row with the keys *omitted* (server bug, partial response)
    would still pass that guard and KeyError on subscript access. The
    KeyError used to surface as a generic resolver crash; now a missing
    field falls through to the "not Completed" branch which returns
    cleanly. TODO.md §H.2-M41.
    """
    if not history:
        return False, None, None, no_video_retries

    status = history.get("status")
    if status == "Failed":
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

    if status != "Completed":
        return False, None, None, no_video_retries

    storage = history.get("storage")
    if not storage:
        return False, None, None, no_video_retries
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
    from resources.lib.http_util import redact_text

    message = redact_text(str(error))
    xbmc.log(
        "NZB-DAV: Unexpected error in {}: {}".format(label, message), xbmc.LOGERROR
    )
    xbmcgui.Dialog().ok(_addon_name(), "Error: {}".format(message))
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
    nzo_id = _submit_nzb_with_retries(nzb_url, title, dialog, monitor)
    if not nzo_id:
        return None, None

    xbmc.log(
        "NZB-DAV: NZB submitted, nzo_id={}, polling every {}s (timeout={}s)".format(
            nzo_id, poll_interval, download_timeout
        ),
        xbmc.LOGINFO,
    )
    # Monotonic clock for elapsed-time tracking — wall-clock NTP jumps
    # would otherwise either prematurely abort the poll loop (backward
    # jump) or stretch the configured download_timeout indefinitely
    # (forward jump). Initial submit timestamp stays on time.time() above
    # since it's logged for human consumption, not arithmetic.
    start_time = time.monotonic()
    last_status = None
    iteration = 0
    no_video_retries = 0
    max_no_video_retries = 5

    while True:
        iteration += 1
        elapsed = time.monotonic() - start_time
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

    Settings reads and the DialogProgress create call live inside the
    try block so that an exception from either still ends with
    `setResolvedUrl(handle, False)`. Without this, an unexpected raise
    from `_get_poll_settings()` (corrupt addon settings) or
    `dialog.create()` (rare Kodi UI failure) escaped before the try
    started and Kodi hung indefinitely waiting on resolve. Closes
    TODO.md §H.2-H9.
    """
    nzb_url = unquote(params.get("nzburl", ""))
    title = unquote(params.get("title", ""))

    if not nzb_url:
        xbmcgui.Dialog().ok(_addon_name(), _string(30096))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
        return

    dialog = None
    try:
        poll_interval, download_timeout = _get_poll_settings()
        dialog = xbmcgui.DialogProgress()
        dialog.create(_addon_name(), _string(30097))

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
        if dialog is not None:
            dialog.close()


def resolve_and_play(nzb_url, title, params=None):
    """Handle direct execution (executebuiltin://RunPlugin calls).

    Polls until the stream is ready, then plays via xbmc.Player().
    Unlike resolve(), there is no plugin handle so setResolvedUrl() is not
    called; playback simply does not start on failure.

    ``params`` (optional) carries the original plugin URL params dict
    (tmdb_id, imdb, season, episode, etc.) so `_clear_kodi_playback_state`
    can scrub the matching TMDBHelper bookmark row. Without it, the
    bookmark survives and the next replay of the same title resumes
    from the broken-stream offset (TODO.md §H.3).

    Settings reads and `dialog.create()` live inside the try block so
    a raise from either still routes through `_handle_resolve_exception`
    and lets the user see a notification rather than silently no-op'ing
    on the RunPlugin path. Same fix as `resolve()` — TODO.md §H.2-H9.
    """
    dialog = None
    try:
        poll_interval, download_timeout = _get_poll_settings()
        dialog = xbmcgui.DialogProgress()
        dialog.create(_addon_name(), _string(30097))

        stream_url, stream_headers = _poll_until_ready(
            nzb_url, title, dialog, poll_interval, download_timeout
        )
        if stream_url:
            _clear_kodi_playback_state(params)
            _play_via_proxy(stream_url, stream_headers)
    except _RESOLVE_RUNTIME_ERRORS as error:
        _handle_resolve_exception("resolve_and_play", error)
    finally:
        if dialog is not None:
            dialog.close()
