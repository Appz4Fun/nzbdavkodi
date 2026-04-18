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
        import sys

        db_dir = xbmcvfs.translatePath("special://database/")
        db_files = sorted(glob.glob(os.path.join(db_dir, "MyVideos*.db")))
        if not db_files:
            return
        db_path = db_files[-1]
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Failed to locate MyVideos DB for bookmark cleanup: {}".format(e),
            xbmc.LOGWARNING,
        )
        return

    # Escape LIKE wildcards so a tmdb_id containing %, _, or \ cannot
    # match unrelated rows. Applied with ESCAPE '\\' on the LIKE clause.
    def _like_escape(value):
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    try:
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            cur = conn.cursor()

            target_ids = set()

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
                id_pattern = re.compile(
                    r"tmdb_id=" + re.escape(tmdb_id) + r"(?:[^0-9]|$)"
                )
                for id_file, filename in cur.fetchall():
                    if id_pattern.search(filename):
                        target_ids.add(id_file)

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


# After a submit timeout, how many times to poll nzbdav before giving
# up on adoption and retrying the submit. 6 polls * 2 s = 12 s of
# total wait — enough headroom for nzbdav to finish fetching/parsing a
# moderately large NZB, short enough not to double the user's wait on
# a genuine network failure.
_SUBMIT_ADOPT_POLL_COUNT = 6
_SUBMIT_ADOPT_POLL_INTERVAL_SECONDS = 2

# UI pump cadence while submit_nzb is running on a background thread.
# Short enough that the progress dialog looks live and the cancel
# button is responsive; long enough that we're not burning CPU on the
# plugin thread while waiting for a remote HTTP call.
_SUBMIT_UI_PUMP_INTERVAL_SECONDS = 0.25


def _submit_nzb_with_ui_pump(nzb_url, title, dialog, monitor):
    """Run ``submit_nzb`` off the plugin thread, pump the dialog, and
    race a concurrent queue probe against the submit.

    submit_nzb issues a synchronous HTTP request to
    ``/api?mode=addurl`` which on a big NZB routinely takes 30–120 s
    (fetch the .nzb from the indexer, parse XML, enumerate
    segments). Running that on the Kodi plugin thread freezes the
    progress dialog for the duration; even with the dialog pump
    fix, the user still waits for nzbdav to finish parsing before
    polling can start.

    The fix is two-part:

    1. ``submit_nzb`` runs in a daemon worker thread. The plugin
       thread loops on ``monitor.waitForAbort`` at 250 ms cadence,
       advancing the dialog's progress bar and redrawing the
       message, and checking ``dialog.iscanceled`` every tick.

    2. A second daemon thread concurrently probes nzbdav's queue
       via ``find_queued_by_name``. As soon as nzbdav has created a
       queue entry for ``title`` — which typically happens seconds
       after the submit arrives, well before ``addurl`` actually
       replies — we adopt that ``nzo_id`` and return without
       waiting for the worker. In the common case this makes the
       "submitting" phase feel instant even when nzbdav's addurl
       reply is slow.

    Races:
    - Queue probe wins: return ``(nzo_id, None)`` immediately. The
      submit worker is left running; its submit is harmless if it
      completes anyway (nzbdav sees the job already enqueued).
    - Submit worker wins: return its result.
    - User cancels: ``(None, {"status": "cancelled", ...})`` — the
      caller bails out of the resolve.
    - Kodi shutdown: ``(None, {"status": "shutdown", ...})``.

    Side effects: spawns two daemon threads. Neither is joined on
    cancel/shutdown; any orphaned in-flight submit is picked up by
    the queue-adoption path on the next play attempt.
    """
    import threading

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

    queue_hit = [None]  # nzo_id if the concurrent probe finds a match
    queue_stop = threading.Event()

    def _queue_probe_worker():
        # Short grace before the first probe — nzbdav needs the
        # network round-trip to fetch the .nzb before it can enqueue
        # anything, so probing immediately just wastes an HTTP call
        # against an empty queue.
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
            # 2 s between probes — fast enough to feel responsive,
            # slow enough not to hammer nzbdav while it's parsing.
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

    elapsed = 0.0
    submit_msg = _string(30097)
    try:
        while not submit_done.is_set():
            # Concurrent probe beat the worker — adopt and go.
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
            elapsed += _SUBMIT_UI_PUMP_INTERVAL_SECONDS
            # Indeterminate progress bar — DialogProgress has no true
            # "pulse" mode on all Kodi skins, so we crawl 0 -> 99 over
            # the submit timeout window and reset to 0 on each pass.
            # The important thing is that the bar MOVES so the user
            # sees the addon is alive.
            pct = int((elapsed * 100) / max(_get_submit_timeout_seconds(), 1)) % 100
            try:
                dialog.update(
                    pct,
                    "{}\n{} ({}s)".format(submit_msg, title[:60], int(elapsed)),
                )
            except Exception:  # pylint: disable=broad-except
                pass
        # After submit_done fires, re-check queue_hit ONE more time
        # before returning the submit worker's result. Race window:
        # the queue probe might have found the job microseconds after
        # the main loop's last check and microseconds before the
        # submit worker set submit_done. Without this re-check, a
        # submit failure (None, error) returned by the worker would
        # win over a concurrent successful adoption — and the
        # resolver would fall through to the retry path, double-
        # submitting a job nzbdav already has. Prefer the adopted
        # nzo_id whenever the probe found one.
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
        queue_stop.set()


def _get_submit_timeout_seconds():
    """Read the same submit_timeout that nzbdav_api._get_submit_timeout
    uses. Duplicated here so resolver.py doesn't need to import a
    private name across module boundaries — keep them in sync if the
    default changes. Returns the int timeout or 120 on error."""
    try:
        import xbmcaddon

        raw = xbmcaddon.Addon().getSetting("submit_timeout")
        return int(raw) if raw else 120
    except (ValueError, TypeError, Exception):  # pylint: disable=broad-except
        return 120


def _adopt_queued_or_completed_job(title, monitor):
    """Return an existing nzbdav nzo_id for ``title`` if the submit we
    just timed out on actually reached nzbdav.

    After a client-side submit timeout, nzbdav may be:
    - Still fetching/parsing the NZB (no queue entry yet)
    - Processing it (queue entry exists under ``title``)
    - Already done (history entry exists under ``title``)

    We probe the queue and history a handful of times on a short
    interval. Returns the matching ``nzo_id`` on the first positive
    hit, ``None`` if nothing surfaces within the poll budget (caller
    should then retry the submit).

    ``monitor.waitForAbort`` is used for the inter-poll sleep so Kodi
    shutdown unwinds the loop immediately rather than stalling.
    """
    for poll in range(_SUBMIT_ADOPT_POLL_COUNT):
        # Queue first — most common hit for big NZBs where the submit
        # landed but the download hasn't completed in the timeout
        # window.
        queued = find_queued_by_name(title)
        if queued and queued.get("nzo_id"):
            return queued["nzo_id"]
        # History second — covers the unusual case where a very small
        # NZB actually completed during the submit-timeout window.
        completed = find_completed_by_name(title)
        if completed and completed.get("nzo_id"):
            return completed["nzo_id"]
        if poll < _SUBMIT_ADOPT_POLL_COUNT - 1:
            if monitor.waitForAbort(_SUBMIT_ADOPT_POLL_INTERVAL_SECONDS):
                return None
    return None


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
        nzo_id, submit_error = _submit_nzb_with_ui_pump(nzb_url, title, dialog, monitor)
        if nzo_id:
            break

        if submit_error:
            last_submit_error = submit_error
            status = submit_error["status"]
            # User hit cancel on the progress dialog or Kodi is shutting
            # down. Either way we stop immediately — no retry, no
            # queue-adoption probe, no error dialog. The in-flight
            # submit thread may still complete in the background; the
            # queue-adoption path on the next play attempt will pick up
            # anything that orphaned.
            if status in ("cancelled", "shutdown"):
                xbmc.log(
                    "NZB-DAV: Submit aborted ({}) for '{}'".format(status, title),
                    xbmc.LOGINFO,
                )
                return None, None
            # Client-side timeout on submit. nzbdav's /api?mode=addurl
            # handler can take > 30 s on big NZBs (fetch from indexer,
            # parse XML, enumerate segments) — longer than the default
            # HTTP timeout. A timeout does NOT mean the submit failed.
            # Probing the queue before retrying lets us adopt the job
            # nzbdav is already processing instead of either bouncing
            # off the duplicate-rejection path or orphaning the
            # in-progress job with a second nzo_id.
            if status == "timeout":
                xbmc.log(
                    "NZB-DAV: Submit attempt {}/{} timed out; probing nzbdav "
                    "queue for '{}' before retrying".format(
                        attempt, max_submit_retries, title
                    ),
                    xbmc.LOGWARNING,
                )
                adopted_nzo_id = _adopt_queued_or_completed_job(title, monitor)
                if adopted_nzo_id:
                    nzo_id = adopted_nzo_id
                    xbmc.log(
                        "NZB-DAV: Adopted existing nzbdav job nzo_id={} for "
                        "'{}' after submit timeout".format(adopted_nzo_id, title),
                        xbmc.LOGINFO,
                    )
                    break
                # Not in queue or history yet — fall through to retry
                # below. Could mean nzbdav is still in the fetch/parse
                # phase and hasn't created a queue entry, or the first
                # submit actually failed at the network level. Retrying
                # is the right call in both cases.
                xbmc.log(
                    "NZB-DAV: '{}' not found in nzbdav queue or history "
                    "after submit timeout; retrying".format(title),
                    xbmc.LOGWARNING,
                )
            elif status in _TRANSIENT_HTTP_STATUSES:
                # Transient gateway/service issues — preserve the existing
                # retry behavior. nzbdav restarting or its upstream proxy
                # hiccupping is exactly the case retry was designed for.
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
