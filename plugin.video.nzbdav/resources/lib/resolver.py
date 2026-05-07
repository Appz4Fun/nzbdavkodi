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
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib.fallback_streams import (
    FALLBACK_CANDIDATES_DISABLED,
    build_fallback_job_name,
    build_prepare_fallback_payload,
)
from resources.lib.http_util import notify as _notify
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string
from resources.lib.nzbdav_api import (
    cancel_job,
    find_completed_by_name,
    find_completed_by_names,
    find_queued_by_name,
    find_queued_by_names,
    get_job_history,
    get_job_status,
    submit_nzb,
)
from resources.lib.webdav import (
    find_video_file,
    find_video_stream_for_folder,
    get_video_file_size_hint,
    get_webdav_stream_url_for_path,
    probe_webdav_reachable,
)

_POLL_INTERVAL_MIN = 1
_POLL_INTERVAL_MAX = 60
_DOWNLOAD_TIMEOUT_MIN = 60
_DOWNLOAD_TIMEOUT_MAX = 86400
MAX_POLL_ITERATIONS = _DOWNLOAD_TIMEOUT_MAX // _POLL_INTERVAL_MIN
_FALLBACK_SHUTDOWN_JOIN_TIMEOUT = 10
_POLL_NEAR_COMPLETE_PERCENTAGE = 99.0
_POLL_LATE_ACTIVE_HISTORY_GRACE_PERCENTAGE = 95.0
_POLL_LATE_ACTIVE_HISTORY_GRACE_SECONDS = 0.025
_POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS = 0.1
_POLL_FULL_PROGRESS_HISTORY_GRACE_SECONDS = 0.14
_POLL_NEAR_COMPLETE_FAST_REPOLL_SECONDS = 0.1
_POLL_NEAR_COMPLETE_FAST_REPOLL_COUNT = 5
_PLAYBACK_CLEANUP_HANDOFF_GRACE_SECONDS = 0.25
_PLAYBACK_PREPARE_HANDOFF_GRACE_SECONDS = 3.0
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
_STREAM_CONTENT_LENGTH_HINTS_MAX = 32
_STREAM_CONTENT_LENGTH_HINTS = {}
_DIALOG_UPDATE_LOCK = threading.Lock()
_DIALOG_UPDATE_INFLIGHT = {}
_SCRIPT_PLAY_STAGE_PATH = "/storage/.kodi/temp/nzbdav-script-play-stage.log"


def _resolve_stage(message):
    xbmc.log("NZB-DAV: Resolve stage: {}".format(message), xbmc.LOGINFO)
    try:
        import os

        with open(_SCRIPT_PLAY_STAGE_PATH, "a", encoding="utf-8") as stage_file:
            stage_file.write("resolve: " + message + "\n")
            stage_file.flush()
            os.fsync(stage_file.fileno())
    except OSError:
        pass


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


def _start_playback_state_cleanup(params=None):
    """Start bookmark cleanup in the background and return its state."""
    done = threading.Event()
    state = {"done": done, "error": None, "thread": None}

    def _worker():
        try:
            _clear_kodi_playback_state(params)
        except Exception as error:  # pylint: disable=broad-except
            state["error"] = error
            xbmc.log(
                "NZB-DAV: Playback-state cleanup worker failed: {}".format(error),
                xbmc.LOGWARNING,
            )
        finally:
            done.set()

    thread = threading.Thread(
        target=_worker, name="nzbdav-playback-state-cleanup", daemon=True
    )
    state["thread"] = thread
    try:
        thread.start()
    except RuntimeError:
        state["thread"] = None
        _worker()
    return state


def _wait_playback_state_cleanup(
    state, wait_seconds=_PLAYBACK_CLEANUP_HANDOFF_GRACE_SECONDS
):
    """Wait briefly for bookmark cleanup without blocking playback handoff."""
    if not state:
        return True
    done = state.get("done")
    if done:
        if not done.wait(max(0, wait_seconds)):
            xbmc.log(
                "NZB-DAV: Playback-state cleanup still running; "
                "continuing playback handoff",
                xbmc.LOGWARNING,
            )
            return False
    error = state.get("error")
    if error is not None:
        raise error
    return True


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


def _stream_auth_header(stream_headers):
    if stream_headers and "Authorization" in stream_headers:
        return stream_headers["Authorization"]
    return None


def _remember_stream_content_length_hint(stream_url, content_length):
    try:
        content_length = int(content_length or 0)
    except (TypeError, ValueError):
        return
    if not stream_url or content_length <= 0:
        return
    _STREAM_CONTENT_LENGTH_HINTS[stream_url] = content_length
    while len(_STREAM_CONTENT_LENGTH_HINTS) > _STREAM_CONTENT_LENGTH_HINTS_MAX:
        _STREAM_CONTENT_LENGTH_HINTS.pop(next(iter(_STREAM_CONTENT_LENGTH_HINTS)), None)


def _stream_content_length_hint(stream_url):
    try:
        return int(_STREAM_CONTENT_LENGTH_HINTS.get(stream_url, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _remember_webdav_stream_content_length_hint(stream_url, video_path):
    _remember_stream_content_length_hint(
        stream_url, get_video_file_size_hint(video_path)
    )


def _prepare_direct_playback(
    stream_url,
    stream_headers,
    fallback_sources=None,
    service_port=None,
    prepare_token=None,
):
    """Prepare resolver playback without touching Kodi UI state."""
    from resources.lib.stream_proxy import (
        get_service_proxy_port,
        prepare_stream_via_service,
    )

    if service_port is None:
        service_port = get_service_proxy_port()

    prepared = {
        "service_port": service_port,
        "stream_url": stream_url,
        "stream_headers": stream_headers,
        "proxy_url": "",
        "stream_info": {},
    }
    if not service_port:
        return prepared

    auth_header = _stream_auth_header(stream_headers)
    prepare_kwargs = {"fallback_sources": fallback_sources}
    content_length_hint = _stream_content_length_hint(stream_url)
    if content_length_hint > 0:
        prepare_kwargs["content_length_hint"] = content_length_hint
    if prepare_token is not None:
        prepare_kwargs["prepare_token"] = prepare_token
    proxy_url, stream_info = prepare_stream_via_service(
        service_port, stream_url, auth_header, **prepare_kwargs
    )
    prepared["proxy_url"] = proxy_url
    prepared["stream_info"] = stream_info
    return prepared


def _direct_playback_service_config():
    """Read proxy connection details on the resolver thread."""
    from resources.lib import stream_proxy

    if getattr(stream_proxy, "get_service_proxy_port", None) is getattr(
        stream_proxy, "_ORIGINAL_GET_SERVICE_PROXY_PORT", None
    ) and getattr(stream_proxy, "get_service_proxy_token", None) is getattr(
        stream_proxy, "_ORIGINAL_GET_SERVICE_PROXY_TOKEN", None
    ):
        return stream_proxy.get_service_proxy_config()

    from resources.lib.stream_proxy import (
        get_service_proxy_port,
        get_service_proxy_token,
    )

    service_port = get_service_proxy_port()
    prepare_token = get_service_proxy_token() if service_port else ""
    return service_port, prepare_token


def _ready_direct_playback_service_config_state(service_port, prepare_token):
    done = threading.Event()
    done.set()
    return {
        "done": done,
        "error": None,
        "service_port": service_port,
        "prepare_token": prepare_token,
        "thread": None,
    }


def _start_direct_playback_service_config_lookup():
    """Start proxy service config lookup before stream readiness."""
    done = threading.Event()
    state = {
        "done": done,
        "error": None,
        "service_port": None,
        "prepare_token": "",
        "thread": None,
    }

    def _worker():
        try:
            state["service_port"], state["prepare_token"] = (
                _direct_playback_service_config()
            )
        except Exception as error:  # pylint: disable=broad-except
            state["error"] = error
        finally:
            done.set()

    thread = threading.Thread(
        target=_worker, name="nzbdav-direct-playback-service-config", daemon=True
    )
    state["thread"] = thread
    try:
        thread.start()
    except RuntimeError:
        try:
            service_port, prepare_token = _direct_playback_service_config()
            return _ready_direct_playback_service_config_state(
                service_port, prepare_token
            )
        except Exception as error:  # pylint: disable=broad-except
            state["error"] = error
            done.set()
    return state


def _wait_direct_playback_service_config(state):
    if not state:
        return _direct_playback_service_config()
    done = state.get("done")
    if done:
        done.wait()
    error = state.get("error")
    if error is not None:
        raise error
    return state.get("service_port") or 0, state.get("prepare_token") or ""


def _prepare_direct_playback_with_service_config(
    stream_url, stream_headers, fallback_sources, service_config_state
):
    from resources.lib.stream_proxy import ServiceProxyUnavailableError

    service_port, prepare_token = _wait_direct_playback_service_config(
        service_config_state
    )
    try:
        return _prepare_direct_playback(
            stream_url,
            stream_headers,
            fallback_sources=fallback_sources,
            service_port=service_port,
            prepare_token=prepare_token,
        )
    except ServiceProxyUnavailableError:
        fresh_service_port, fresh_prepare_token = _direct_playback_service_config()
        if (fresh_service_port, fresh_prepare_token) == (service_port, prepare_token):
            raise
        return _prepare_direct_playback(
            stream_url,
            stream_headers,
            fallback_sources=fallback_sources,
            service_port=fresh_service_port,
            prepare_token=fresh_prepare_token,
        )


def _ready_direct_playback_prepare_state(prepared):
    done = threading.Event()
    done.set()
    return {"done": done, "error": None, "prepared": prepared, "thread": None}


def _direct_playback_fallback_prepared(stream_url, stream_headers):
    return {
        "service_port": 0,
        "stream_url": stream_url,
        "stream_headers": stream_headers,
        "proxy_url": "",
        "stream_info": {},
    }


def _should_skip_proxy_prepare(stream_url, fallback_sources):
    """Return true when proxy prepare adds risk without helping playback."""
    return not fallback_sources and _url_path(stream_url).endswith(".mkv")


def _monitor_abort_requested(monitor):
    """Return Kodi's abort flag without entering a wait call."""
    try:
        return monitor.abortRequested() is True
    except (AttributeError, RuntimeError, TypeError):
        return False


def _wait_for_abort_or_timeout(monitor, wait_seconds, tick_seconds=0.05):
    import time as real_time

    deadline = real_time.monotonic() + max(0, wait_seconds)
    while True:
        if _monitor_abort_requested(monitor):
            return True
        remaining = deadline - real_time.monotonic()
        if remaining <= 0:
            return False
        threading.Event().wait(min(tick_seconds, remaining))


def _settings_getter_kwargs(settings_getter):
    return {"settings_getter": settings_getter} if settings_getter is not None else {}


def _safe_dialog_update(dialog, progress, message):
    """Best-effort progress update that cannot block the resolver loop."""
    key = id(dialog)
    with _DIALOG_UPDATE_LOCK:
        inflight = _DIALOG_UPDATE_INFLIGHT.get(key)
        if inflight is not None and not inflight.is_set():
            return False
        done = threading.Event()
        _DIALOG_UPDATE_INFLIGHT[key] = done

    def _worker():
        try:
            dialog.update(progress, message)
        except Exception as error:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: progress dialog update failed: {}".format(error),
                xbmc.LOGDEBUG,
            )
        finally:
            done.set()
            with _DIALOG_UPDATE_LOCK:
                if _DIALOG_UPDATE_INFLIGHT.get(key) is done:
                    _DIALOG_UPDATE_INFLIGHT.pop(key, None)

    try:
        threading.Thread(
            target=_worker, name="nzbdav-dialog-progress-update", daemon=True
        ).start()
        return True
    except RuntimeError as error:
        done.set()
        with _DIALOG_UPDATE_LOCK:
            if _DIALOG_UPDATE_INFLIGHT.get(key) is done:
                _DIALOG_UPDATE_INFLIGHT.pop(key, None)
        xbmc.log(
            "NZB-DAV: progress dialog update thread failed: {}".format(error),
            xbmc.LOGDEBUG,
        )
        return False


def _start_direct_playback_prepare(
    stream_url, stream_headers, fallback_sources=None, service_config_state=None
):
    """Start proxy prepare in the background and return its state."""
    if service_config_state is None:
        service_port, prepare_token = _direct_playback_service_config()
    else:
        service_port, prepare_token = None, None
    if service_config_state is None and not service_port:
        prepared = _prepare_direct_playback(
            stream_url,
            stream_headers,
            fallback_sources=fallback_sources,
            service_port=service_port,
            prepare_token=prepare_token,
        )
        return _ready_direct_playback_prepare_state(prepared)

    done = threading.Event()
    state = {
        "done": done,
        "error": None,
        "prepared": None,
        "thread": None,
        "fallback_prepared": _direct_playback_fallback_prepared(
            stream_url, stream_headers
        ),
    }

    def _worker():
        try:
            if service_config_state is None:
                state["prepared"] = _prepare_direct_playback(
                    stream_url,
                    stream_headers,
                    fallback_sources=fallback_sources,
                    service_port=service_port,
                    prepare_token=prepare_token,
                )
            else:
                state["prepared"] = _prepare_direct_playback_with_service_config(
                    stream_url,
                    stream_headers,
                    fallback_sources,
                    service_config_state,
                )
        except Exception as error:  # pylint: disable=broad-except
            state["error"] = error
        finally:
            done.set()

    thread = threading.Thread(
        target=_worker, name="nzbdav-direct-playback-prepare", daemon=True
    )
    state["thread"] = thread
    try:
        thread.start()
    except RuntimeError:
        state["thread"] = None
        _worker()
    return state


def _wait_direct_playback_prepare(
    state, wait_seconds=_PLAYBACK_PREPARE_HANDOFF_GRACE_SECONDS
):
    done = state.get("done")
    if done:
        if not done.wait(max(0, wait_seconds)):
            xbmc.log(
                "NZB-DAV: Proxy prepare still running; "
                "falling back to direct WebDAV handoff",
                xbmc.LOGWARNING,
            )
            return state.get("fallback_prepared")
    error = state.get("error")
    if error is not None:
        raise error
    return state.get("prepared")


def _show_cache_prompt_after_playback(stream_info):
    """Show the advisory cache prompt after Kodi has the playable URL."""
    try:
        from resources.lib.cache_prompt import maybe_show_cache_prompt

        maybe_show_cache_prompt(stream_info)
    except _RESOLVE_RUNTIME_ERRORS as error:
        xbmc.log(
            "NZB-DAV: cache prompt skipped after playback handoff: {}".format(error),
            xbmc.LOGWARNING,
        )


def _finish_direct_playback(handle, prepared):
    """Finish resolver playback on the Kodi thread."""
    stream_url = prepared["stream_url"]
    stream_headers = prepared["stream_headers"]
    service_port = prepared.get("service_port")

    if service_port:
        proxy_url = prepared["proxy_url"]
        stream_info = prepared["stream_info"]

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

        li = xbmcgui.ListItem(path=proxy_url)
        li.setContentLookup(False)
        _apply_proxy_mime(li, stream_url, stream_info)

        home.setProperty("nzbdav.stream_url", proxy_url)
        home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
        home.setProperty("nzbdav.active", "true")
        xbmcplugin.setResolvedUrl(handle, True, li)
        _show_cache_prompt_after_playback(stream_info)
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


def _finish_player_playback(prepared):
    """Finish service-side playback on the Kodi thread."""
    stream_url = prepared["stream_url"]
    stream_headers = prepared["stream_headers"]
    service_port = prepared.get("service_port")
    home = xbmcgui.Window(10000)
    title = stream_url.rsplit("/", 1)[-1]

    if service_port:
        proxy_url = prepared["proxy_url"]
        stream_info = prepared["stream_info"]

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

        li = xbmcgui.ListItem(path=proxy_url)
        li.setContentLookup(False)
        _apply_proxy_mime(li, stream_url, stream_info)
        home.setProperty("nzbdav.stream_url", proxy_url)
        home.setProperty("nzbdav.stream_title", title)
        home.setProperty("nzbdav.active", "true")
        xbmc.Player().play(proxy_url, li)
        _show_cache_prompt_after_playback(stream_info)
        return

    bust_url = _cache_bust_url(stream_url)
    li = _make_playable_listitem(bust_url, stream_headers)
    play_url = _build_play_url(bust_url, stream_headers)
    xbmc.log("NZB-DAV: Playing direct (no proxy): {}".format(stream_url), xbmc.LOGINFO)
    home.setProperty("nzbdav.stream_url", play_url)
    home.setProperty("nzbdav.stream_title", title)
    home.setProperty("nzbdav.active", "true")
    xbmc.Player().play(li.getPath(), li)


def _play_direct(handle, stream_url, stream_headers, fallback_sources=None):
    """Play a stream through the local service proxy.

    Every file type routes through the service proxy so Kodi never opens the
    remote WebDAV URL directly. This avoids Kodi's PROPFIND scan of the
    parent directory (nzbdav's WebDAV returns localhost:8080 hrefs that
    break Kodi's directory parser and cascade into an Open failure) and
    sidesteps pipe-header auth quirks on MKV.

    The proxy picks the right mode per file: MP4 gets Tier 1-3 faststart or
    MKV remux; MKV/AVI/other get a range-capable pass-through.
    """
    _finish_direct_playback(
        handle,
        _prepare_direct_playback(
            stream_url, stream_headers, fallback_sources=fallback_sources
        ),
    )


def _play_via_proxy(stream_url, stream_headers, fallback_sources=None):
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
    _finish_player_playback(
        _prepare_direct_playback(
            stream_url, stream_headers, fallback_sources=fallback_sources
        )
    )


def _get_poll_settings(settings_getter=None):
    try:
        if settings_getter is None:
            addon = xbmcaddon.Addon()
            interval_raw = addon.getSetting("poll_interval")
            timeout_raw = addon.getSetting("download_timeout")
        else:
            interval_raw = settings_getter("poll_interval", "1")
            timeout_raw = settings_getter("download_timeout", "3600")
        interval = int(interval_raw or "1")
        timeout = int(timeout_raw or "3600")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        interval = 1
        timeout = 3600
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


def _history_status_is_terminal(history_status):
    """Return whether a history row is enough to stop waiting on queue state."""
    if not isinstance(history_status, dict):
        return False
    return history_status.get("status") in ("Completed", "Failed")


_ACTIVE_QUEUE_STATUSES = frozenset(
    (
        "queued",
        "downloading",
        "paused",
        "quickcheck",
        "verifying",
        "repairing",
        "extracting",
        "moving",
    )
)


def _queue_status_is_clearly_active(job_status):
    """Return whether queue status is enough to defer history to the next poll."""
    if not _queue_status_has_active_status(job_status):
        return False
    try:
        return float(job_status.get("percentage", 0) or 0) < 100
    except (TypeError, ValueError):
        return True


def _queue_status_has_active_status(job_status):
    """Return whether the queue row describes an active nzbdav job."""
    if not isinstance(job_status, dict):
        return False
    status = str(job_status.get("status", "") or "").strip().lower()
    return status in _ACTIVE_QUEUE_STATUSES


def _queue_status_is_nearly_complete(job_status):
    """Return whether queue progress is close enough to briefly await history."""
    if not isinstance(job_status, dict):
        return False
    try:
        percentage = float(job_status.get("percentage", 0) or 0)
    except (TypeError, ValueError):
        return False
    return percentage >= _POLL_NEAR_COMPLETE_PERCENTAGE


def _queue_status_is_late_active(job_status):
    """Return whether an active queue row is late enough to catch history."""
    if not _queue_status_has_active_status(job_status):
        return False
    try:
        percentage = float(job_status.get("percentage", 0) or 0)
    except (TypeError, ValueError):
        return False
    return percentage >= _POLL_LATE_ACTIVE_HISTORY_GRACE_PERCENTAGE


def _queue_status_history_grace_seconds(job_status):
    if not isinstance(job_status, dict):
        return _POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS
    try:
        percentage = float(job_status.get("percentage", 0) or 0)
    except (TypeError, ValueError):
        return _POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS
    if percentage >= 100.0:
        return _POLL_FULL_PROGRESS_HISTORY_GRACE_SECONDS
    return _POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS


def _poll_wait_after_status(job_status, poll_interval, fast_repolls_used):
    """Return the next poll wait and updated near-complete fast-repoll count."""
    if _queue_status_is_nearly_complete(job_status):
        if fast_repolls_used < _POLL_NEAR_COMPLETE_FAST_REPOLL_COUNT:
            return (
                min(poll_interval, _POLL_NEAR_COMPLETE_FAST_REPOLL_SECONDS),
                fast_repolls_used + 1,
            )
        return poll_interval, fast_repolls_used
    return poll_interval, 0


def _wait_for_nearly_complete_history(
    history_ready, history_done, deadline, grace_seconds=None
):
    """Give completed history a small chance to beat the next poll interval."""
    if grace_seconds is None:
        grace_seconds = _POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS
    grace_deadline = min(
        deadline,
        time.monotonic() + max(0, grace_seconds),
    )
    while True:
        if history_ready.is_set() or history_done.is_set():
            return
        remaining = grace_deadline - time.monotonic()
        if remaining <= 0:
            return
        history_ready.wait(min(0.01, remaining))


def _poll_once(nzo_id, title, monitor, settings_getter=None):
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
    history_ready = threading.Event()
    queue_done = threading.Event()
    history_done = threading.Event()

    def check_queue():
        try:
            job_status[0] = get_job_status(
                nzo_id, **_settings_getter_kwargs(settings_getter)
            )
        finally:
            queue_done.set()

    def check_history():
        try:
            history_status[0] = get_job_history(
                nzo_id, **_settings_getter_kwargs(settings_getter)
            )
            if _history_status_is_terminal(history_status[0]):
                history_ready.set()
        finally:
            history_done.set()

    # daemon=True so a stalled worker thread doesn't block the plugin
    # interpreter from exiting on Kodi shutdown.
    t1 = threading.Thread(target=check_queue, daemon=True)
    t2 = threading.Thread(target=check_history, daemon=True)
    t1.start()
    t2.start()
    deadline = time.monotonic() + 10
    while True:
        if history_ready.is_set():
            break
        if queue_done.is_set() and _queue_status_is_clearly_active(job_status[0]):
            if _queue_status_is_nearly_complete(job_status[0]):
                _wait_for_nearly_complete_history(
                    history_ready,
                    history_done,
                    deadline,
                    _queue_status_history_grace_seconds(job_status[0]),
                )
            elif _queue_status_is_late_active(job_status[0]):
                _wait_for_nearly_complete_history(
                    history_ready,
                    history_done,
                    deadline,
                    _POLL_LATE_ACTIVE_HISTORY_GRACE_SECONDS,
                )
            break
        if queue_done.is_set() and _queue_status_has_active_status(job_status[0]):
            if _queue_status_is_nearly_complete(job_status[0]):
                _wait_for_nearly_complete_history(
                    history_ready,
                    history_done,
                    deadline,
                    _queue_status_history_grace_seconds(job_status[0]),
                )
                break
        if queue_done.is_set() and history_done.is_set():
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        history_ready.wait(min(0.05, remaining))

    # Only probe WebDAV for errors after both APIs returned no data within the
    # bounded wait, so we don't falsely conclude the job is missing.
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


def _submit_error_is_too_many_requests(submit_error):
    message = str(submit_error.get("message", "") or "")
    normalized = message.replace(" ", "").replace("-", "").lower()
    return "toomanyrequests" in normalized or "429" in normalized


def _show_submit_error_dialog(submit_error):
    """Show a Kodi modal dialog reporting nzbdav's actual error message.

    Truncates the message to 200 chars (on top of the 500-char cap
    already applied in submit_nzb) and falls back to a clear placeholder
    when nzbdav returned an empty body.
    """
    if _submit_error_is_too_many_requests(submit_error):
        indexer = str(submit_error.get("indexer", "") or "").strip()
        message = _fmt(30193, indexer) if indexer else _string(30194)
        xbmcgui.Dialog().notification(_addon_name(), message, "", 7000)
        return

    message = submit_error["message"][:200] or "(no error message)"
    xbmcgui.Dialog().ok(
        _addon_name(),
        _fmt(30124, submit_error["status"], message),
    )


def _submit_error_with_indexer(submit_error, selected_indexer):
    if not selected_indexer:
        return submit_error
    enriched = dict(submit_error)
    enriched["indexer"] = selected_indexer
    return enriched


def _close_dialog_before_submit_error(dialog):
    """Close the progress dialog before displaying a terminal submit error."""
    try:
        dialog.close()
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: progress dialog close before submit error failed: {}".format(
                error
            ),
            xbmc.LOGDEBUG,
        )


def _start_existing_completed_cleanup(title, on_existing_completed):
    """Start existing-completed cleanup callback without failing resolve."""
    if on_existing_completed is None:
        return
    try:
        on_existing_completed()
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Existing completed cleanup start failed for '{}': {}".format(
                title, error
            ),
            xbmc.LOGWARNING,
        )


def _find_video_stream_for_folder(webdav_folder, settings_getter=None):
    """Return video path, URL, and headers for a completed WebDAV folder."""
    try:
        from resources.lib import webdav as _webdav

        if (
            find_video_stream_for_folder is _webdav.find_video_stream_for_folder
            and find_video_file is _webdav.find_video_file
            and get_webdav_stream_url_for_path is _webdav.get_webdav_stream_url_for_path
        ):
            return find_video_stream_for_folder(
                webdav_folder, **_settings_getter_kwargs(settings_getter)
            )
    except (AttributeError, ImportError):
        pass

    kwargs = _settings_getter_kwargs(settings_getter)
    video_path = find_video_file(webdav_folder, **kwargs)
    if not video_path:
        return None, None, None
    stream_url, stream_headers = get_webdav_stream_url_for_path(video_path, **kwargs)
    return video_path, stream_url, stream_headers


def _completed_job_stream(
    title, completed_job, on_existing_completed=None, settings_getter=None
):
    """Return a WebDAV stream URL from a completed nzbdav history row."""
    if not isinstance(completed_job, dict):
        return None
    status = completed_job.get("status", "")
    if status and status != "Completed":
        return None
    name = completed_job.get("name", "")
    if name and name != title:
        return None

    xbmc.log(
        "NZB-DAV: '{}' already downloaded, streaming directly".format(title),
        xbmc.LOGINFO,
    )
    storage = completed_job.get("storage")
    if not storage:
        xbmc.log(
            "NZB-DAV: Completed history row for '{}' has no storage path".format(title),
            xbmc.LOGWARNING,
        )
        return None
    webdav_folder = _storage_to_webdav_path(storage)
    _start_existing_completed_cleanup(title, on_existing_completed)
    video_path, stream_url, stream_headers = _find_video_stream_for_folder(
        webdav_folder, settings_getter=settings_getter
    )
    if not video_path:
        return None
    _remember_webdav_stream_content_length_hint(stream_url, video_path)
    return stream_url, stream_headers


def _existing_completed_stream(
    title,
    on_existing_completed=None,
    completed_job_hint=None,
    completed_job_lookup_done=False,
    settings_getter=None,
):
    """Return an already-downloaded stream URL when the title exists."""
    hinted_stream = _completed_job_stream(
        title,
        completed_job_hint,
        on_existing_completed=on_existing_completed,
        settings_getter=settings_getter,
    )
    if hinted_stream is not None:
        return hinted_stream

    if completed_job_lookup_done:
        return None

    existing = find_completed_by_name(title, **_settings_getter_kwargs(settings_getter))
    return _completed_job_stream(
        title,
        existing,
        on_existing_completed=on_existing_completed,
        settings_getter=settings_getter,
    )


def _picker_completed_stream(
    title, params, on_existing_completed=None, settings_getter=None
):
    """Return a picker-provided completed stream before opening progress UI."""
    if not params:
        return None
    has_hint = "_completed_job" in params
    lookup_done = _picker_completed_lookup_done(params)
    if not has_hint and not lookup_done:
        return None
    return _existing_completed_stream(
        title,
        on_existing_completed=on_existing_completed,
        completed_job_hint=params.get("_completed_job"),
        completed_job_lookup_done=lookup_done,
        settings_getter=settings_getter,
    )


def _picker_completed_lookup_done(params):
    """Return whether picker metadata already covered completed-history lookup."""
    if not params:
        return False
    return bool(params.get("_completed_job_lookup_done") or "_completed_job" in params)


# UI update cadence while submit_nzb is running on a background thread.
# Kept slower than adoption checks so the progress dialog looks live without
# redrawing for every queue-probe poll.
_SUBMIT_UI_PUMP_INTERVAL_SECONDS = 0.25
_SUBMIT_ADOPTION_CHECK_INTERVAL_SECONDS = 0.05
_SUBMIT_QUEUE_PROBE_INITIAL_DELAY_SECONDS = 0.0
_SUBMIT_QUEUE_PROBE_FAST_INTERVAL_SECONDS = 0.05
_SUBMIT_QUEUE_PROBE_FAST_WINDOW_SECONDS = 2.0
_SUBMIT_QUEUE_PROBE_INTERVAL_SECONDS = 0.25
_SUBMIT_HISTORY_PROBE_PARALLEL_GRACE_SECONDS = 0.01
_COMPLETED_NO_VIDEO_RECHECK_DELAYS_SECONDS = (0.025, 0.075, 0.1)


def _job_nzo_id(match):
    if isinstance(match, dict) and match.get("nzo_id"):
        return match["nzo_id"]
    return None


def _find_adoptable_job_during_submit(title, settings_getter=None):
    """Return queue/history nzo_id without serializing behind a slow queue miss."""
    result = {"nzo_id": None, "done_count": 0}
    lock = threading.Lock()
    progress = threading.Event()

    def _record_match(match):
        nzo_id = _job_nzo_id(match)
        with lock:
            if nzo_id and result["nzo_id"] is None:
                result["nzo_id"] = nzo_id
            result["done_count"] += 1
        progress.set()

    def _probe_queue():
        try:
            match = find_queued_by_name(
                title, **_settings_getter_kwargs(settings_getter)
            )
        except Exception as e:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: concurrent queue probe raised: {}".format(e),
                xbmc.LOGWARNING,
            )
            match = None
        _record_match(match)

    def _probe_history():
        try:
            match = find_completed_by_name(
                title, **_settings_getter_kwargs(settings_getter)
            )
        except Exception as e:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: concurrent history probe raised: {}".format(e),
                xbmc.LOGWARNING,
            )
            match = None
        _record_match(match)

    queue_thread = threading.Thread(
        target=_probe_queue, name="nzbdav-submit-queue-probe", daemon=True
    )
    try:
        queue_thread.start()
    except RuntimeError:
        _probe_queue()

    progress.wait(_SUBMIT_HISTORY_PROBE_PARALLEL_GRACE_SECONDS)
    with lock:
        nzo_id = result["nzo_id"]
        done_count = result["done_count"]
    if nzo_id:
        return nzo_id
    if done_count:
        _probe_history()
        with lock:
            return result["nzo_id"]

    history_thread = threading.Thread(
        target=_probe_history, name="nzbdav-submit-history-probe", daemon=True
    )
    try:
        history_thread.start()
        expected_done = 2
    except RuntimeError:
        _probe_history()
        expected_done = 2

    while True:
        with lock:
            nzo_id = result["nzo_id"]
            done_count = result["done_count"]
        if nzo_id or done_count >= expected_done:
            return nzo_id
        progress.wait(0.01)
        progress.clear()


def _submit_nzb_with_ui_pump(nzb_url, title, dialog, monitor, settings_getter=None):
    """Run ``submit_nzb`` off the plugin thread, pump the dialog, and
    race a concurrent queue probe against the submit.

    ``submit_nzb`` issues a synchronous HTTP request to ``/api?mode=addurl``
    which on a big NZB routinely takes 30-300 s. Running it on the Kodi
    plugin thread freezes the progress dialog. The fix is two-part:

    1. ``submit_nzb`` runs in a daemon worker thread; the plugin thread
       loops on ``monitor.waitForAbort`` at 250 ms cadence, advances the
       dialog progress bar, and checks ``dialog.iscanceled`` every tick.
    2. Daemon probe threads concurrently watch nzbdav's queue/history via
       ``find_queued_by_name`` / ``find_completed_by_name`` and short-circuit
       as soon as the job for ``title`` appears — usually well before
       ``addurl`` replies.

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
    activity_ready = threading.Event()
    submit_timeout_seconds = max(
        _get_submit_timeout_seconds(**_settings_getter_kwargs(settings_getter)), 1
    )

    def _submit_worker():
        try:
            submit_kwargs = _settings_getter_kwargs(settings_getter)
            if settings_getter is not None:
                submit_kwargs["submit_timeout"] = submit_timeout_seconds
            submit_result[0], submit_result[1] = submit_nzb(
                nzb_url,
                title,
                **submit_kwargs,
            )
        except Exception as e:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: submit_nzb worker raised: {}".format(e),
                xbmc.LOGERROR,
            )
            submit_result[0], submit_result[1] = None, None
        finally:
            submit_done.set()
            activity_ready.set()

    queue_hit = [None]
    adoption_status = [""]
    queue_hit_lock = threading.Lock()
    adopted_during_submit = [False]
    queue_stop = threading.Event()
    first_queue_probe_done = threading.Event()

    def _current_adoption_hit():
        with queue_hit_lock:
            return queue_hit[0]

    def _record_adoption_hit(match):
        nzo_id = _job_nzo_id(match)
        if not nzo_id or queue_stop.is_set():
            return False
        with queue_hit_lock:
            if queue_hit[0]:
                return True
            queue_hit[0] = nzo_id
            if isinstance(match, dict):
                adoption_status[0] = str(match.get("status", "") or "")
        activity_ready.set()
        return True

    def _queue_probe_worker():
        # Probe immediately for already-visible retry/duplicate jobs. A miss
        # still falls through to the fast retry cadence while nzbdav receives
        # the addurl request.
        if queue_stop.wait(_SUBMIT_QUEUE_PROBE_INITIAL_DELAY_SECONDS):
            return
        probe_started = time.monotonic()
        first_probe = True
        while not queue_stop.is_set() and not submit_done.is_set():
            try:
                match = find_queued_by_name(
                    title, **_settings_getter_kwargs(settings_getter)
                )
            except Exception as e:  # pylint: disable=broad-except
                xbmc.log(
                    "NZB-DAV: concurrent queue probe raised: {}".format(e),
                    xbmc.LOGWARNING,
                )
                match = None
            try:
                if _record_adoption_hit(match):
                    return
            finally:
                if first_probe:
                    first_probe = False
                    first_queue_probe_done.set()
            elapsed = time.monotonic() - probe_started
            interval = (
                _SUBMIT_QUEUE_PROBE_FAST_INTERVAL_SECONDS
                if elapsed < _SUBMIT_QUEUE_PROBE_FAST_WINDOW_SECONDS
                else _SUBMIT_QUEUE_PROBE_INTERVAL_SECONDS
            )
            if queue_stop.wait(interval):
                return

    def _wait_for_history_probe_start():
        deadline = time.monotonic() + max(
            0, _SUBMIT_HISTORY_PROBE_PARALLEL_GRACE_SECONDS
        )
        while not queue_stop.is_set() and not _current_adoption_hit():
            if first_queue_probe_done.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            first_queue_probe_done.wait(min(0.01, remaining))
        return False

    def _history_probe_worker():
        # Start shortly after the queue probe so an immediate queue hit wins
        # without a history request. If the first queue probe misses quickly,
        # do not make completed-history adoption wait out the full grace.
        if not _wait_for_history_probe_start():
            return
        probe_started = time.monotonic()
        while (
            not queue_stop.is_set()
            and not submit_done.is_set()
            and not _current_adoption_hit()
        ):
            try:
                match = find_completed_by_name(
                    title, **_settings_getter_kwargs(settings_getter)
                )
            except Exception as e:  # pylint: disable=broad-except
                xbmc.log(
                    "NZB-DAV: concurrent history probe raised: {}".format(e),
                    xbmc.LOGWARNING,
                )
                match = None
            if _record_adoption_hit(match):
                return
            elapsed = time.monotonic() - probe_started
            interval = (
                _SUBMIT_QUEUE_PROBE_FAST_INTERVAL_SECONDS
                if elapsed < _SUBMIT_QUEUE_PROBE_FAST_WINDOW_SECONDS
                else _SUBMIT_QUEUE_PROBE_INTERVAL_SECONDS
            )
            if queue_stop.wait(interval):
                return

    submit_t = threading.Thread(
        target=_submit_worker, name="nzbdav-submit", daemon=True
    )
    probe_t = threading.Thread(
        target=_queue_probe_worker, name="nzbdav-submit-probe", daemon=True
    )
    history_probe_t = threading.Thread(
        target=_history_probe_worker, name="nzbdav-submit-history-probe", daemon=True
    )
    started_threads = []

    def _start_submit_thread(thread, label):
        try:
            thread.start()
        except RuntimeError as error:
            xbmc.log(
                "NZB-DAV: Could not start {} thread for '{}': {}".format(
                    label, title, error
                ),
                xbmc.LOGWARNING,
            )
            return False
        started_threads.append(thread)
        return True

    if not _start_submit_thread(submit_t, "submit"):
        xbmc.log(
            "NZB-DAV: Falling back to synchronous submit for '{}'".format(title),
            xbmc.LOGWARNING,
        )
        _submit_worker()
    elif not submit_done.is_set():
        _start_submit_thread(probe_t, "queue probe")
        _start_submit_thread(history_probe_t, "history probe")

    # Anchor elapsed to wall-clock via time.monotonic() instead of
    # accumulating _SUBMIT_UI_PUMP_INTERVAL_SECONDS per loop; the per-loop
    # accumulation under-reports on slow skins because dialog.update()
    # itself can block for tens of milliseconds.
    loop_start = time.monotonic()
    last_dialog_update = loop_start
    submit_msg = _string(30097)

    def _probe_adoption_result():
        nzo_id = _current_adoption_hit()
        if not nzo_id:
            return None
        adopted_during_submit[0] = True
        if adoption_status[0] == "Completed":
            _safe_dialog_update(
                dialog,
                100,
                "Already completed in nzbdav\nPreparing stream: {}".format(title[:60]),
            )
        else:
            _safe_dialog_update(
                dialog,
                1,
                "Found in nzbdav\nChecking download status: {}".format(title[:60]),
            )
        xbmc.log(
            "NZB-DAV: Concurrent queue/history probe found '{}' under "
            "nzo_id={}; adopting without waiting for addurl response".format(
                title, nzo_id
            ),
            xbmc.LOGINFO,
        )
        return nzo_id, None

    def _wait_for_submit_activity_or_abort(wait_seconds):
        deadline = time.monotonic() + max(0, wait_seconds)
        while not submit_done.is_set() and not _current_adoption_hit():
            if _monitor_abort_requested(monitor):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            activity_ready.wait(min(0.01, remaining))
            activity_ready.clear()
        return False

    try:
        while not submit_done.is_set():
            probe_result = _probe_adoption_result()
            if probe_result:
                return probe_result
            if dialog.iscanceled():
                xbmc.log(
                    "NZB-DAV: User cancelled during submit for '{}'".format(title),
                    xbmc.LOGINFO,
                )
                return None, {"status": "cancelled", "message": ""}
            if _wait_for_submit_activity_or_abort(
                _SUBMIT_ADOPTION_CHECK_INTERVAL_SECONDS
            ):
                return None, {"status": "shutdown", "message": ""}
            probe_result = _probe_adoption_result()
            if probe_result:
                return probe_result
            now = time.monotonic()
            elapsed = now - loop_start
            if now - last_dialog_update < _SUBMIT_UI_PUMP_INTERVAL_SECONDS:
                continue
            last_dialog_update = now
            pct = int((elapsed * 100) / submit_timeout_seconds) % 100
            _safe_dialog_update(
                dialog,
                pct,
                "{}\n{} ({}s)".format(submit_msg, title[:60], int(elapsed)),
            )
        # Race window re-check: prefer adopted nzo_id over a failed submit.
        nzo_id = _current_adoption_hit()
        if nzo_id and not submit_result[0]:
            xbmc.log(
                "NZB-DAV: Queue probe found '{}' under nzo_id={} just as "
                "submit worker finished; preferring the adopted job over "
                "the submit result".format(title, nzo_id),
                xbmc.LOGINFO,
            )
            return nzo_id, None
        return submit_result[0], submit_result[1]
    finally:
        # Signal the probe worker to exit its wait loop, then give cleanup a
        # brief bounded window. If we already adopted while addurl is still
        # blocked, waiting on that uninterruptible HTTP worker only adds
        # latency; it is daemon=True and will die with the plugin interpreter.
        queue_stop.set()
        for t in started_threads:
            if t is submit_t and adopted_during_submit[0] and not submit_done.is_set():
                continue
            if t in (probe_t, history_probe_t) and (
                _current_adoption_hit() or submit_result[0] or submit_result[1]
            ):
                # A successful addurl response is authoritative. The adoption
                # probe may still be blocked in a read-only queue/history API
                # call, so do not keep the post-picker submit path waiting on
                # cleanup after we already have the nzo_id or submitted result.
                # A terminal submit error is just as authoritative for the
                # immediate UI path; retries/adoption happen in the caller.
                continue
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


def _get_submit_timeout_seconds(settings_getter=None):
    """Read submit_timeout setting; returns int or 300 on error."""
    try:
        if settings_getter is None:
            raw = xbmcaddon.Addon().getSetting("submit_timeout")
        else:
            raw = settings_getter("submit_timeout", "")
        return int(raw) if raw else 300
    except Exception:  # pylint: disable=broad-except
        # xbmcaddon import failures, unexpected setting shapes, int() on
        # a MagicMock in tests — all funnel to the documented default.
        # ``Exception`` on its own (the previous ``(ValueError, TypeError,
        # Exception)`` tuple was dead code — Exception subsumes the other
        # two) keeps the safety net without the misleading tuple.
        return 300


# After a submit timeout, how many times to poll nzbdav before giving up
# on adoption and retrying the submit. 6 polls * 2 s = 12 s of total wait
# — enough headroom for nzbdav to finish fetching/parsing a moderately
# large NZB, short enough not to double the user's wait on a genuine
# network failure.
_SUBMIT_ADOPT_POLL_COUNT = 6
_SUBMIT_ADOPT_POLL_INTERVAL_SECONDS = 2


def _adopt_queued_or_completed_job(title, monitor, settings_getter=None):
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
        nzo_id = _find_adoptable_job_during_submit(
            title, settings_getter=settings_getter
        )
        if nzo_id:
            return nzo_id
        if poll < _SUBMIT_ADOPT_POLL_COUNT - 1:
            if monitor.waitForAbort(_SUBMIT_ADOPT_POLL_INTERVAL_SECONDS):
                return None
    return None


def _submit_nzb_with_retries(
    nzb_url,
    title,
    dialog,
    monitor,
    max_submit_retries=3,
    settings_getter=None,
    selected_indexer=None,
):
    """Submit an NZB with the existing retry and error-dialog behavior."""
    xbmc.log("NZB-DAV: Submitting NZB for '{}'".format(title), xbmc.LOGINFO)
    last_submit_error = None

    for attempt in range(1, max_submit_retries + 1):
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            nzb_url, title, dialog, monitor, settings_getter=settings_getter
        )
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
                adopted_nzo_id = _adopt_queued_or_completed_job(
                    title, monitor, settings_getter=settings_getter
                )
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
                _close_dialog_before_submit_error(dialog)
                _show_submit_error_dialog(
                    _submit_error_with_indexer(submit_error, selected_indexer)
                )
                return None
            elif isinstance(status, int) and 400 <= status < 500:
                # HTTP 4xx means nzbdav reached upstream and got a terminal
                # client/indexer-side rejection (for example Hydra 429 mapped
                # to nzbdav's HTTP 400). There is no nzbdav job to adopt, and
                # probing queue/history just leaves the progress dialog stuck.
                xbmc.log(
                    "NZB-DAV: Submit failed with HTTP {}, not probing queue: "
                    "{}".format(status, submit_error["message"]),
                    xbmc.LOGERROR,
                )
                _close_dialog_before_submit_error(dialog)
                _show_submit_error_dialog(
                    _submit_error_with_indexer(submit_error, selected_indexer)
                )
                return None
            else:
                # Non-transient HTTP error (often 500 "duplicate nzo_id").
                # Before surfacing the error to the user, probe the queue:
                # if the job is already running, attach to it. This covers
                # the race where a concurrent submit (e.g. retried play of
                # the same title) beat us to nzbdav.
                adopted_nzo_id = _adopt_queued_or_completed_job(
                    title, monitor, settings_getter=settings_getter
                )
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
                _close_dialog_before_submit_error(dialog)
                _show_submit_error_dialog(
                    _submit_error_with_indexer(submit_error, selected_indexer)
                )
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
        _close_dialog_before_submit_error(dialog)
        _show_submit_error_dialog(
            _submit_error_with_indexer(last_submit_error, selected_indexer)
        )
        return None

    xbmc.log(
        "NZB-DAV: All {} submit attempts failed for '{}'. "
        "Check nzbdav URL and API key in settings.".format(max_submit_retries, title),
        xbmc.LOGERROR,
    )
    _close_dialog_before_submit_error(dialog)
    xbmcgui.Dialog().ok(_addon_name(), _string(30098))
    return None


def _submit_fallback_candidates(
    candidates, monitor, stop_event=None, on_job=None, settings_getter=None
):
    """Submit duplicate fallback candidates as standby nzbdav jobs."""
    fallback_jobs = []
    candidate_jobs = []
    for index, candidate in enumerate(candidates or [], start=1):
        if stop_event is not None and stop_event.is_set():
            break
        if not isinstance(candidate, dict):
            continue
        nzb_url = candidate.get("link")
        title = candidate.get("title")
        if not nzb_url or not title:
            continue
        job_name = build_fallback_job_name(title, nzb_url, index)
        candidate_jobs.append((candidate, nzb_url, title, job_name))

    job_names = [row[3] for row in candidate_jobs]
    completed_jobs = find_completed_by_names(
        job_names, **_settings_getter_kwargs(settings_getter)
    )
    queue_names = [name for name in job_names if name not in completed_jobs]
    queued_jobs = find_queued_by_names(
        queue_names, **_settings_getter_kwargs(settings_getter)
    )
    existing_jobs = dict(completed_jobs)
    existing_jobs.update(queued_jobs)

    for _candidate, nzb_url, title, job_name in candidate_jobs:
        if stop_event is not None and stop_event.is_set():
            break
        existing_job = existing_jobs.get(job_name)
        if existing_job and existing_job.get("nzo_id"):
            xbmc.log(
                "NZB-DAV: Adopting existing fallback job '{}' nzo_id={}".format(
                    job_name, existing_job["nzo_id"]
                ),
                xbmc.LOGINFO,
            )
            fallback_jobs.append(
                {
                    "title": title,
                    "nzb_url": nzb_url,
                    "job_name": job_name,
                    "nzo_id": existing_job["nzo_id"],
                    "stream_url": "",
                    "stream_headers": {},
                    "content_length": 0,
                    "status": existing_job.get("status", ""),
                }
            )
            if on_job is not None:
                on_job(dict(fallback_jobs[-1]))
            continue
        try:
            nzo_id, submit_error = submit_nzb(
                nzb_url, job_name, **_settings_getter_kwargs(settings_getter)
            )
        except Exception as error:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: Fallback submit failed for '{}': {}".format(job_name, error),
                xbmc.LOGWARNING,
            )
            continue
        if not nzo_id and submit_error:
            status = submit_error.get("status")
            if status == "timeout":
                xbmc.log(
                    "NZB-DAV: Fallback submit timed out for '{}'; probing "
                    "queue/history in background".format(job_name),
                    xbmc.LOGWARNING,
                )
                nzo_id = _adopt_queued_or_completed_job(
                    job_name, monitor, settings_getter=settings_getter
                )
            if not nzo_id:
                xbmc.log(
                    "NZB-DAV: Fallback submit skipped for '{}' (status={}): {}".format(
                        job_name, status, submit_error.get("message", "")
                    ),
                    xbmc.LOGWARNING,
                )
                continue
        if not nzo_id:
            xbmc.log(
                "NZB-DAV: Fallback submit did not create job for '{}'".format(job_name),
                xbmc.LOGWARNING,
            )
            continue
        fallback_jobs.append(
            {
                "title": title,
                "nzb_url": nzb_url,
                "job_name": job_name,
                "nzo_id": nzo_id,
                "stream_url": "",
                "stream_headers": {},
                "content_length": 0,
            }
        )
        if on_job is not None:
            on_job(dict(fallback_jobs[-1]))
    return fallback_jobs


def _fallback_streams_enabled(settings_getter=None):
    """Return whether fallback streams are enabled in Kodi settings."""
    try:
        if settings_getter is None:
            raw = xbmcaddon.Addon().getSetting("fallback_streams_enabled")
        else:
            raw = settings_getter("fallback_streams_enabled", "true")
    except (AttributeError, RuntimeError, TypeError):
        return True
    return str(raw or "").strip().lower() != "false"


def _prefetch_fallback_candidate_loader(candidate_loader):
    """Start fallback candidate discovery now and return a cached loader.

    The returned loader is still consumed by the fallback submit worker after
    primary submit/adoption, so this overlaps Hydra/NZB manifest discovery
    without submitting standby nzbdav jobs before the primary is accepted.
    """
    if candidate_loader is None:
        return None

    done = threading.Event()
    state = {"candidates": [], "disabled": False}
    errors = []

    def _worker():
        try:
            loaded = candidate_loader()
            if loaded is FALLBACK_CANDIDATES_DISABLED:
                state["disabled"] = True
                state["candidates"] = []
            else:
                state["candidates"] = list(loaded or [])
        except Exception as error:  # pylint: disable=broad-except
            errors.append(error)
        finally:
            done.set()

    thread = threading.Thread(
        target=_worker, name="nzbdav-fallback-candidate-prefetch", daemon=True
    )
    try:
        thread.start()
    except RuntimeError:
        return candidate_loader

    def _load_prefetched_candidates():
        done.wait()
        if errors:
            raise errors[0]
        if state["disabled"]:
            return FALLBACK_CANDIDATES_DISABLED
        return list(state["candidates"])

    return _load_prefetched_candidates


def _start_fallback_submit_worker(
    candidates=None, candidate_loader=None, settings_getter=None
):
    """Start background fallback submits and return shared state."""
    state = {
        "lock": threading.Lock(),
        "jobs": [],
        "stop": threading.Event(),
        "finished": threading.Event(),
        "thread": None,
        "cancel_job": cancel_job,
    }
    candidate_list = list(candidates or [])
    if not candidate_list and candidate_loader is None:
        state["finished"].set()
        return state

    def _append_job(job):
        should_cancel = False
        with state["lock"]:
            if state["stop"].is_set():
                should_cancel = True
            else:
                state["jobs"].append(job)
        if should_cancel:
            _cancel_fallback_job(state, job)

    def _worker():
        try:
            active_candidates = candidate_list
            candidate_lookup_disabled = False
            if candidate_loader is not None:
                try:
                    loaded_candidates = candidate_loader()
                    if loaded_candidates is FALLBACK_CANDIDATES_DISABLED:
                        candidate_lookup_disabled = True
                        active_candidates = []
                    else:
                        active_candidates = list(loaded_candidates or [])
                except Exception as error:  # pylint: disable=broad-except
                    xbmc.log(
                        "NZB-DAV: Fallback candidate lookup failed: {}".format(error),
                        xbmc.LOGWARNING,
                    )
                    active_candidates = []
            if state["stop"].is_set():
                return
            if not active_candidates:
                if not candidate_lookup_disabled and _fallback_streams_enabled(
                    settings_getter=settings_getter
                ):
                    try:
                        _notify(_addon_name(), _string(30187), 4000)
                    except (RuntimeError, OSError):
                        pass
                return
            _submit_fallback_candidates(
                active_candidates,
                xbmc.Monitor(),
                stop_event=state["stop"],
                on_job=_append_job,
                settings_getter=settings_getter,
            )
        except Exception as error:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: Fallback submit worker failed: {}".format(error),
                xbmc.LOGWARNING,
            )
            _cancel_fallback_submitted_jobs(state)
        finally:
            state["finished"].set()

    thread = threading.Thread(
        target=_worker, name="nzbdav-fallback-submit", daemon=True
    )
    state["thread"] = thread
    thread.start()
    return state


_FALLBACK_TERMINAL_STATUSES = frozenset(
    (
        "aborted",
        "cancelled",
        "canceled",
        "completed",
        "complete",
        "deleted",
        "failed",
        "failure",
        "finished",
        "history",
        "success",
    )
)


def _fallback_job_value(job, key, default=None):
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


def _fallback_job_pending(job):
    status = _fallback_job_value(job, "status")
    if status is None:
        status = _fallback_job_value(job, "state")
    if status is None:
        return True
    return str(status).strip().lower() not in _FALLBACK_TERMINAL_STATUSES


def _cancel_fallback_job(state, job):
    cancel_callable = state.get("cancel_job") or cancel_job
    if not _fallback_job_pending(job):
        return False
    nzo_id = _fallback_job_value(job, "nzo_id")
    try:
        if nzo_id and cancel_callable:
            cancel_callable(nzo_id)
            return True
        if hasattr(job, "cancel"):
            job.cancel()
            return True
        if hasattr(job, "abort"):
            job.abort()
            return True
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Failed to cancel fallback submit job {}: {}".format(
                nzo_id or job, error
            ),
            xbmc.LOGWARNING,
        )
    return False


def _cancel_fallback_submitted_jobs(state):
    """Cancel submitted fallback jobs that are still pending or running."""
    if not state:
        return []
    with state["lock"]:
        jobs_to_cancel = list(state["jobs"])

    cancelled = []
    for job in jobs_to_cancel:
        if _cancel_fallback_job(state, job):
            cancelled.append(job)
    return cancelled


def _fallback_submit_jobs_snapshot(state, wait_seconds=0.5):
    """Return fallback jobs submitted so far, waiting briefly for completion."""
    if not state:
        return []
    thread = state.get("thread")
    stop_event = state.get("stop")
    finished = state.get("finished")
    stop_requested = bool(stop_event and stop_event.is_set())
    if thread and stop_requested:
        deadline = time.monotonic() + max(0, wait_seconds)
        while thread.is_alive() and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_for = min(0.05, remaining)
            if finished and finished.wait(wait_for):
                break

    with state["lock"]:
        return [dict(job) if isinstance(job, dict) else job for job in state["jobs"]]


def _stop_fallback_submit_worker(
    state, cancel_submitted=False, join_timeout=_FALLBACK_SHUTDOWN_JOIN_TIMEOUT
):
    """Signal the fallback worker to stop and optionally cancel known jobs."""
    if not state:
        return []
    state["stop"].set()
    thread = state.get("thread")
    if thread:
        timeout = max(0, join_timeout)
        thread.join(timeout=timeout)
        if thread.is_alive():
            xbmc.log(
                "NZB-DAV: Fallback submit worker still running after {:.2f}s; "
                "resolve shutdown is continuing".format(timeout),
                xbmc.LOGWARNING,
            )
        else:
            state["thread"] = None
    if cancel_submitted:
        _cancel_fallback_submitted_jobs(state)
    return _fallback_submit_jobs_snapshot(state, wait_seconds=0)


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
    _safe_dialog_update(dialog, progress, _status_dialog_message(status, percentage))
    return False, last_status


def _find_completed_video_stream_with_rechecks(
    webdav_folder, monitor=None, settings_getter=None
):
    """Return a completed WebDAV stream, briefly rechecking symlink visibility."""
    video_path, stream_url, stream_headers = _find_video_stream_for_folder(
        webdav_folder, settings_getter=settings_getter
    )
    if video_path or monitor is None:
        return video_path, stream_url, stream_headers

    for delay_seconds in _COMPLETED_NO_VIDEO_RECHECK_DELAYS_SECONDS:
        if monitor.waitForAbort(delay_seconds):
            return None, None, None
        video_path, stream_url, stream_headers = _find_video_stream_for_folder(
            webdav_folder, settings_getter=settings_getter
        )
        if video_path:
            return video_path, stream_url, stream_headers
    return None, None, None


def _handle_history_result(
    history,
    title,
    no_video_retries,
    max_no_video_retries,
    monitor=None,
    settings_getter=None,
):
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
    video_path, stream_url, stream_headers = _find_completed_video_stream_with_rechecks(
        webdav_folder, monitor=monitor, settings_getter=settings_getter
    )
    if video_path:
        _remember_webdav_stream_content_length_hint(stream_url, video_path)
        xbmc.log(
            "NZB-DAV: File available, streaming '{}' via WebDAV".format(video_path),
            xbmc.LOGINFO,
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


def _poll_until_ready(
    nzb_url,
    title,
    dialog,
    poll_interval,
    download_timeout,
    on_primary_submitted=None,
    on_existing_completed=None,
    completed_job_hint=None,
    completed_job_lookup_done=False,
    settings_getter=None,
    selected_indexer=None,
):
    """Submit NZB and poll until download completes.

    Returns ``(stream_url, stream_headers)`` on success, or ``(None, None)``
    on failure (timeout, cancellation, server error, etc.).  All user
    notifications are issued inside this function; the caller only needs to
    decide what to do with the resulting stream URL.
    """
    existing_stream = _existing_completed_stream(
        title,
        on_existing_completed=on_existing_completed,
        completed_job_hint=completed_job_hint,
        completed_job_lookup_done=completed_job_lookup_done,
        settings_getter=settings_getter,
    )
    if existing_stream is not None:
        return existing_stream

    monitor = xbmc.Monitor()
    nzo_id = _submit_nzb_with_retries(
        nzb_url,
        title,
        dialog,
        monitor,
        settings_getter=settings_getter,
        selected_indexer=selected_indexer,
    )
    if not nzo_id:
        return None, None
    if on_primary_submitted is not None:
        try:
            on_primary_submitted(nzo_id)
        except Exception as error:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: Fallback submit worker start failed: {}".format(error),
                xbmc.LOGWARNING,
            )

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
    near_complete_fast_repolls = 0

    while True:
        iteration += 1
        elapsed = time.monotonic() - start_time
        if _abort_poll_before_fetch(
            iteration, elapsed, download_timeout, dialog, nzo_id, title
        ):
            return None, None

        job_status, history, webdav_error = _poll_once(
            nzo_id, title, monitor, settings_getter=settings_getter
        )

        should_stop, last_status = _handle_job_status(
            job_status, nzo_id, dialog, last_status
        )
        if should_stop:
            return None, None

        should_stop, stream_url, stream_headers, no_video_retries = (
            _handle_history_result(
                history,
                title,
                no_video_retries,
                max_no_video_retries,
                monitor=monitor,
                settings_getter=settings_getter,
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

        wait_seconds, near_complete_fast_repolls = _poll_wait_after_status(
            job_status, poll_interval, near_complete_fast_repolls
        )
        if _wait_for_abort_or_timeout(monitor, wait_seconds):
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
    fallback_state = None
    playback_cleanup_state = None

    if not nzb_url:
        xbmcgui.Dialog().ok(_addon_name(), _string(30096))
        xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
        xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
        return

    dialog = None
    try:
        fallback_candidates = params.get("_fallback_candidates", [])
        selected_indexer = params.get("_selected_indexer", "")
        fallback_candidate_loader = _prefetch_fallback_candidate_loader(
            params.get("_fallback_candidate_loader")
        )
        picker_completed_lookup_done = _picker_completed_lookup_done(params)

        def _start_playback_cleanup_once():
            nonlocal playback_cleanup_state
            if playback_cleanup_state is None:
                playback_cleanup_state = _start_playback_state_cleanup(params)

        def _start_fallback_after_primary(_nzo_id):
            nonlocal fallback_state
            _start_playback_cleanup_once()
            if fallback_state is None:
                fallback_state = _start_fallback_submit_worker(
                    fallback_candidates,
                    candidate_loader=fallback_candidate_loader,
                )

        completed_stream = _picker_completed_stream(
            title, params, on_existing_completed=_start_playback_cleanup_once
        )
        if completed_stream is not None:
            stream_url, stream_headers = completed_stream
        else:
            poll_interval, download_timeout = _get_poll_settings()
            dialog = xbmcgui.DialogProgress()
            dialog.create(_addon_name(), _string(30097))
            # Bookmark cleanup does not depend on the accepted nzo_id. Start it
            # before the submit/poll wait so selected-result latency hides the
            # DB scan/write instead of paying it after WebDAV readiness.
            _start_playback_cleanup_once()
            stream_url, stream_headers = _poll_until_ready(
                nzb_url,
                title,
                dialog,
                poll_interval,
                download_timeout,
                on_primary_submitted=_start_fallback_after_primary,
                on_existing_completed=_start_playback_cleanup_once,
                completed_job_hint=(
                    None
                    if picker_completed_lookup_done
                    else params.get("_completed_job")
                ),
                completed_job_lookup_done=picker_completed_lookup_done,
                selected_indexer=selected_indexer,
            )
        if stream_url:
            if fallback_state is None:
                _start_fallback_after_primary(None)
            fallback_sources = build_prepare_fallback_payload(
                _fallback_submit_jobs_snapshot(fallback_state)
            )
            prepared = None
            if _should_skip_proxy_prepare(stream_url, fallback_sources):
                prepared = _direct_playback_fallback_prepared(
                    stream_url, stream_headers
                )
                playback_prepare_state = None
            else:
                playback_prepare_state = _start_direct_playback_prepare(
                    stream_url,
                    stream_headers,
                    fallback_sources=fallback_sources,
                    service_config_state=None,
                )
            _wait_playback_state_cleanup(playback_cleanup_state)
            if playback_prepare_state is not None:
                prepared = _wait_direct_playback_prepare(playback_prepare_state)
            _finish_direct_playback(handle, prepared)
        else:
            _stop_fallback_submit_worker(fallback_state, cancel_submitted=True)
            xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
            xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()
    except _RESOLVE_RUNTIME_ERRORS as error:
        _stop_fallback_submit_worker(fallback_state, cancel_submitted=True)
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
    fallback_state = None
    playback_cleanup_state = None
    try:
        _resolve_stage("enter resolve_and_play")
        resolve_params = params or {}
        settings_getter = resolve_params.get("_settings_getter")
        selected_indexer = resolve_params.get("_selected_indexer", "")
        fallback_candidates = resolve_params.get("_fallback_candidates", [])
        fallback_candidate_loader = resolve_params.get("_fallback_candidate_loader")
        _resolve_stage("fallback lookup deferred")
        _resolve_stage("service config lookup deferred")
        picker_completed_lookup_done = _picker_completed_lookup_done(resolve_params)

        def _start_playback_cleanup_once():
            nonlocal playback_cleanup_state
            if playback_cleanup_state is None:
                playback_cleanup_state = _start_playback_state_cleanup(params)

        def _start_fallback_after_primary(_nzo_id):
            nonlocal fallback_state
            _start_playback_cleanup_once()
            if fallback_state is None:
                fallback_submit_kwargs = {
                    "candidate_loader": fallback_candidate_loader,
                }
                fallback_submit_kwargs.update(_settings_getter_kwargs(settings_getter))
                fallback_state = _start_fallback_submit_worker(
                    fallback_candidates,
                    **fallback_submit_kwargs,
                )

        completed_stream = _picker_completed_stream(
            title,
            resolve_params,
            on_existing_completed=_start_playback_cleanup_once,
            settings_getter=settings_getter,
        )
        _resolve_stage("picker completed stream checked")
        if completed_stream is not None:
            stream_url, stream_headers = completed_stream
        else:
            _resolve_stage("poll settings start")
            poll_interval, download_timeout = _get_poll_settings(
                settings_getter=settings_getter
            )
            _resolve_stage("poll settings done")
            _resolve_stage("progress create start")
            dialog = xbmcgui.DialogProgress()
            dialog.create(_addon_name(), _string(30097))
            _resolve_stage("progress create done")
            # Bookmark cleanup does not depend on the accepted nzo_id. Start it
            # before the submit/poll wait so selected-result latency hides the
            # DB scan/write instead of paying it after WebDAV readiness.
            _start_playback_cleanup_once()
            _resolve_stage("poll until ready start")
            stream_url, stream_headers = _poll_until_ready(
                nzb_url,
                title,
                dialog,
                poll_interval,
                download_timeout,
                on_primary_submitted=_start_fallback_after_primary,
                on_existing_completed=_start_playback_cleanup_once,
                completed_job_hint=(
                    None
                    if picker_completed_lookup_done
                    else resolve_params.get("_completed_job")
                ),
                completed_job_lookup_done=picker_completed_lookup_done,
                settings_getter=settings_getter,
                selected_indexer=selected_indexer,
            )
            _resolve_stage("poll until ready done stream={}".format(bool(stream_url)))
        if stream_url:
            if fallback_state is None:
                _start_fallback_after_primary(None)
            fallback_sources = build_prepare_fallback_payload(
                _fallback_submit_jobs_snapshot(fallback_state)
            )
            if _should_skip_proxy_prepare(stream_url, fallback_sources):
                _resolve_stage("proxy prepare skipped for plain mkv")
                prepared = _direct_playback_fallback_prepared(
                    stream_url, stream_headers
                )
                playback_prepare_state = None
            else:
                _resolve_stage("prepare playback start")
                playback_prepare_state = _start_direct_playback_prepare(
                    stream_url,
                    stream_headers,
                    fallback_sources=fallback_sources,
                    service_config_state=None,
                )
            _resolve_stage("cleanup wait start")
            _wait_playback_state_cleanup(playback_cleanup_state)
            _resolve_stage("cleanup wait done")
            _resolve_stage("finish playback start")
            if playback_prepare_state is not None:
                _resolve_stage("prepare wait start")
                prepared = _wait_direct_playback_prepare(playback_prepare_state)
                _resolve_stage(
                    "prepare wait done service_port={}".format(
                        prepared.get("service_port") if prepared else ""
                    )
                )
            _finish_player_playback(prepared)
            _resolve_stage("player playback started")
        else:
            _stop_fallback_submit_worker(fallback_state, cancel_submitted=True)
    except _RESOLVE_RUNTIME_ERRORS as error:
        _stop_fallback_submit_worker(fallback_state, cancel_submitted=True)
        _handle_resolve_exception("resolve_and_play", error)
    finally:
        if dialog is not None:
            dialog.close()
