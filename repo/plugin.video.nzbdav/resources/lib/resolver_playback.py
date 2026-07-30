# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Kodi playback-state, bookmark-DB, ListItem and stream-hint helpers.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import re

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import

# Pre-compile the MyVideos DB schema version regex at the module level.
# `_kodi_video_db_version` is used as a sort key (`key=_kodi_video_db_version`)
# against a potentially large list of DB files. Pre-compiling avoids a CPython
# dictionary lookup in the `re` module cache for every file during the sort loop,
# reducing overhead in a performance-critical path.
_MYVIDEOS_DB_RE = re.compile(r"MyVideos(\d+)\.db$")


def _resolve_stage(message):
    # One caller (resolver_entry) threads raw exception text into the stage
    # label; redact before it reaches the Kodi log AND the persisted stage file.
    safe_message = _redact_log(message)
    _resolver.xbmc.log(
        "NZB-DAV: Resolve stage: {}".format(safe_message), _resolver.xbmc.LOGINFO
    )
    try:
        import os

        with open(
            _resolver._SCRIPT_PLAY_STAGE_PATH, "a", encoding="utf-8"
        ) as stage_file:
            stage_file.write("resolve: " + safe_message + "\n")
            stage_file.flush()
            os.fsync(stage_file.fileno())
    except OSError:
        # Best-effort stage breadcrumb only (debug aid). The xbmc.log line
        # above is the real record; a missing/unwritable temp path must never
        # break resolve.
        pass


def _redact_log(value):
    """Redact URLs/credentials out of a value before it reaches Kodi logs.

    Backend/WebDAV exception strings and stream URLs can embed signed query
    strings or inline credentials; mirror the lower-level API helpers'
    redaction (see http_util.redact_text / direct_indexers / hydra).
    """
    from resources.lib.http_util import redact_text

    return redact_text(str(value))


def _clamp_int_setting(setting_id, value, lo, hi):
    """Clamp an integer setting and log when user input was out of range."""
    clamped = value
    if value < lo:
        clamped = lo
    elif value > hi:
        clamped = hi
    if clamped != value:
        key = (setting_id, value)
        if key not in _resolver._CLAMP_LOGGED:
            _resolver._CLAMP_LOGGED.add(key)
            _resolver.xbmc.log(
                "NZB-DAV: Setting {}={} out of range [{}..{}]; clamping to {}".format(
                    setting_id, value, lo, hi, clamped
                ),
                _resolver.xbmc.LOGWARNING,
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
    except (OSError, ValueError, _resolver.http.client.HTTPException):
        return False


def _completed_stream_body_available(url, headers, probe_bytes=65536, timeout=20):
    """Best-effort: can a supposedly-Completed stream serve its mid-file body?

    nzbdav sometimes reports a download ``Completed`` when its middle article
    bodies are actually missing/unretained on the backend. The container header
    (byte 0) and the tail (cues) still read, so the file *looks* valid and
    playback starts — then the demuxer EOFs the instant it reaches the body.
    That empty-stream playback is what preceded the Kodi crash on
    "The Good, the Bad and the Ugly". A byte-0 check can't catch it (the header
    is present), so this probes an actual byte range from the middle of the file.

    Returns ``False`` ONLY on a definitive failure: the mid-file range GET
    returns HTTP >= 400 or yields zero body bytes. On any ambiguity — unknown
    length, a file too small to have a meaningful middle, a timeout, or any
    other network error — returns ``True`` (fail-open) so a slow-but-valid
    stream is never blocked from playing.

    Env-gated fault injection: NZBDAV_FAULT_REJECT_COMPLETED forces this to
    return False so the resolver takes the re-download path (which attaches
    validated fallback sources), used to stage a live fallback cutover. Inert
    unless the env var is set.
    """
    import os

    if os.environ.get("NZBDAV_FAULT_REJECT_COMPLETED"):
        return False

    length = _completed_stream_head_length(url, headers, timeout)
    if length is None or length <= probe_bytes * 2:
        # Unknown length, or too small to distinguish a missing "middle" from
        # header/tail — fail open.
        return True
    return _completed_stream_midfile_present(url, headers, length, probe_bytes, timeout)


def _add_request_headers(req, headers):
    if headers:
        for key, value in headers.items():
            req.add_header(key, value)


def _completed_stream_head_length(url, headers, timeout):
    """Return the Content-Length via HEAD, or None on any ambiguous failure."""
    from urllib.request import Request, urlopen

    from resources.lib.http_util import prefer_ipv4_connections

    # Any failure here is ambiguous (e.g. server rejects HEAD) — don't block.
    try:
        head = Request(url, method="HEAD")
        _add_request_headers(head, headers)
        with prefer_ipv4_connections():
            # nosemgrep
            with urlopen(head, timeout=timeout) as resp:  # nosec B310
                return int(resp.headers.get("Content-Length", 0) or 0)
    except (OSError, ValueError, _resolver.http.client.HTTPException):
        return None


def _completed_stream_midfile_present(url, headers, length, probe_bytes, timeout):
    """Probe a mid-file byte range: False only on a definitive empty/4xx body."""
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen

    from resources.lib.http_util import prefer_ipv4_connections

    start = length // 2
    end = min(start + probe_bytes - 1, length - 1)
    try:
        req = Request(url)
        req.add_header("Range", "bytes={}-{}".format(start, end))
        _add_request_headers(req, headers)
        with prefer_ipv4_connections():
            # nosemgrep
            with urlopen(req, timeout=timeout) as resp:  # nosec B310
                if resp.getcode() >= 400:
                    return False
                # A success status that delivers no body == the article body
                # is gone even though the metadata claims the file exists.
                return bool(resp.read(1))
    except HTTPError:
        # Definitive: the backend cannot serve the mid-file body (e.g. the
        # 404/500 seen when articles are missing).
        return False
    except (OSError, ValueError, _resolver.http.client.HTTPException):
        # Ambiguous (timeout, connection reset) — fail open.
        return True


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
        _resolver.time.time_ns()
        if hasattr(_resolver.time, "time_ns")
        else int(_resolver.time.time() * 1000) * 1_000_000
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
        return 0.0

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
                    return 0.0

                # Narrowest possible mutation: only clear bookmark rows. The
                # files/settings/streamdetails rows stay intact — Kodi will
                # treat the file as "never resumed" on the next play, which is
                # exactly the state we want.
                bookmark_columns = _bookmark_columns(cur)
                resume_seconds = 0.0
                for id_file in target_ids:
                    resume_seconds = max(
                        resume_seconds,
                        _captured_bookmark_resume_seconds(
                            cur, id_file, bookmark_columns
                        ),
                    )
                    cur.execute("DELETE FROM bookmark WHERE idFile = ?", (id_file,))

        _resolver.xbmc.log(
            "NZB-DAV: Cleared bookmark for {} file(s)".format(len(target_ids)),
            _resolver.xbmc.LOGINFO,
        )
        return resume_seconds
    except sqlite3.OperationalError as e:
        # "database is locked" / busy timeout. Kodi holds the writer; we
        # skip this cleanup and let the next resolve retry.
        _resolver.xbmc.log(
            "NZB-DAV: MyVideos DB busy, skipping bookmark cleanup: {}".format(e),
            _resolver.xbmc.LOGDEBUG,
        )
    except sqlite3.Error as e:
        _resolver.xbmc.log(
            "NZB-DAV: SQLite error during bookmark cleanup: {}".format(e),
            _resolver.xbmc.LOGWARNING,
        )
    return 0.0


def _bookmark_columns(cur):
    cur.execute("PRAGMA table_info(bookmark)")
    return {row[1] for row in cur.fetchall()}


def _bookmark_resume_query(bookmark_columns):
    """Pick the bookmark SELECT matching the available columns."""
    total_col = (
        "totalTimeInSeconds" if "totalTimeInSeconds" in bookmark_columns else "NULL"
    )
    type_filter = " AND type = 1" if "type" in bookmark_columns else ""
    return (
        "SELECT timeInSeconds, "  # nosec B608 — params bound, trusted local DB
        + total_col
        + " FROM bookmark WHERE idFile = ?"
        + type_filter
    )


def _captured_bookmark_resume_seconds(cur, id_file, bookmark_columns):
    # nosemgrep
    cur.execute(_bookmark_resume_query(bookmark_columns), (id_file,))
    resume_seconds = 0.0
    for row in cur.fetchall():
        time_in_seconds = row[0]
        total_time = row[1] if len(row) > 1 else None
        if not _resolver.resume_store.is_useful_resume(time_in_seconds, total_time):
            continue
        try:
            resume_seconds = max(resume_seconds, float(time_in_seconds))
        except (TypeError, ValueError):
            # A non-numeric/NULL bookmark row is simply skipped; resume falls
            # back to other rows (or 0.0). Best-effort — never abort cleanup.
            pass
    return resume_seconds


def _start_playback_state_cleanup(params=None):
    """Start bookmark cleanup in the background and return its state."""
    done = _resolver.threading.Event()
    state = {"done": done, "error": None, "resume_seconds": 0.0, "thread": None}

    def _worker():
        try:
            state["resume_seconds"] = _resolver._clear_kodi_playback_state(params)
        except Exception as error:  # pylint: disable=broad-except
            state["error"] = error
            _resolver.xbmc.log(
                "NZB-DAV: Playback-state cleanup worker failed: {}".format(error),
                _resolver.xbmc.LOGWARNING,
            )
        finally:
            done.set()

    thread = _resolver.threading.Thread(
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
    state, wait_seconds=_resolver._PLAYBACK_CLEANUP_HANDOFF_GRACE_SECONDS
):
    """Wait briefly for bookmark cleanup without blocking playback handoff."""
    if not state:
        return 0.0
    done = state.get("done")
    if done:
        if not done.wait(max(0, wait_seconds)):
            _resolver.xbmc.log(
                "NZB-DAV: Playback-state cleanup still running; "
                "continuing playback handoff",
                _resolver.xbmc.LOGWARNING,
            )
            return 0.0
    error = state.get("error")
    if error is not None:
        raise error
    return _coerce_resume_seconds(state.get("resume_seconds"))


def _coerce_resume_seconds(value):
    """Return a positive numeric resume offset or zero."""
    if not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, float(value))


def _kodi_video_db_version(path):
    """Numeric MyVideos schema version from a DB filename (-1 if unparseable).

    Used as a sort key so the NEWEST DB wins by integer version; a plain
    lexicographic sort picks MyVideos99.db over MyVideos131.db on upgraded
    installs, which would target a stale DB for bookmark cleanup.
    """
    import os

    match = _MYVIDEOS_DB_RE.search(os.path.basename(path))
    return int(match.group(1)) if match else -1


def _locate_kodi_video_db():
    """Return the newest MyVideos DB path, or None when unavailable."""
    try:
        # Skip DB access while something is playing to avoid contending
        # with Kodi's internal vacuum (Textures13.db / MyVideos131.db)
        # which can stall the decoder and freeze playback.
        if _resolver.xbmc.Player().isPlayingVideo():
            _resolver.xbmc.log(
                "NZB-DAV: Skipping playback-state cleanup — video is playing",
                _resolver.xbmc.LOGDEBUG,
            )
            return None

        import glob
        import os

        db_dir = _resolver.xbmcvfs.translatePath("special://database/")
        db_files = sorted(
            glob.glob(os.path.join(db_dir, "MyVideos*.db")),
            key=_kodi_video_db_version,
        )
    except _resolver._DB_DISCOVERY_ERRORS as error:
        _resolver.xbmc.log(
            "NZB-DAV: Failed to locate MyVideos DB for bookmark cleanup: {}".format(
                error
            ),
            _resolver.xbmc.LOGWARNING,
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
    from urllib.parse import parse_qs, urlsplit

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
        if id_pattern.search(filename) and _tmdb_helper_url_matches_params(
            parse_qs(urlsplit(filename).query), params
        ):
            target_ids.add(id_file)


def _numeric_query_param_matches(query, params, name):
    expected = (params or {}).get(name)
    values = query.get(name)
    if expected in ("", None):
        return not any(value not in ("", None) for value in (values or []))
    if not values:
        return False
    actual = values[-1]
    try:
        return int(actual) == int(expected)
    except (TypeError, ValueError):
        return str(actual) == str(expected)


def _tmdb_helper_url_matches_params(query, params):
    return _numeric_query_param_matches(
        query, params, "season"
    ) and _numeric_query_param_matches(query, params, "episode")


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


def _playback_fallback_sources_for_stream(stream_url, fallback_jobs, dead=None):
    if dead is not None:
        fallback_jobs = [
            job
            for job in fallback_jobs
            if not (dead.has_nzo(job.get("nzo_id")) or dead.has_url(job.get("nzb_url")))
        ]
    fallback_sources = _resolver.build_prepare_fallback_payload(fallback_jobs)
    return fallback_sources


def _arm_live_fallback_push(prepared, fallback_state, primary_stream_url, dead=None):
    """Push fallbacks adopted AFTER /prepare into the live proxy session.

    The /prepare fallback snapshot is one-shot: when the primary resolves
    instantly (e.g. an already-downloaded copy), the fallback submit worker
    hasn't adopted the alternate copies yet, so the session starts with an
    empty fallback list and the live cutover has nothing to switch to. The
    worker keeps adopting for tens of seconds afterward. This installs an
    ``on_append`` hook so each newly-adopted job is POSTed to
    ``/stream/<id>/fallbacks``, and flushes whatever was already adopted
    between the snapshot and now. No-ops for non-service (direct) playback.
    """
    if not prepared or not fallback_state:
        return
    service_port = prepared.get("service_port")
    proxy_url = prepared.get("proxy_url")
    if not service_port or not proxy_url:
        return
    from resources.lib.stream_proxy import (
        _extract_session_id_from_proxy_url,
        update_stream_fallbacks_via_service,
    )

    session_id = _extract_session_id_from_proxy_url(proxy_url)
    if not session_id:
        return
    _, prepare_token = _resolver._direct_playback_service_config()

    def _push():
        jobs = _resolver._fallback_submit_jobs_snapshot(fallback_state, wait_seconds=0)
        sources = _resolver._playback_fallback_sources_for_stream(
            primary_stream_url, jobs, dead=dead
        )
        if not sources:
            return
        try:
            update_stream_fallbacks_via_service(
                service_port, session_id, sources, prepare_token=prepare_token
            )
        except Exception as error:  # pylint: disable=broad-except
            _resolver.xbmc.log(
                "NZB-DAV: live fallback push failed: {}".format(error),
                _resolver.xbmc.LOGWARNING,
            )

    fallback_state["on_append"] = _push
    _push()


def _video_mime_for_path(path):
    """Map a (lowercased) URL path to a Kodi mime type, defaulting to MKV."""
    if path.endswith(".mp4") or path.endswith(".m4v"):
        return "video/mp4"
    if path.endswith(".avi"):
        return "video/x-msvideo"
    if path.endswith((".ts", ".m2ts")):
        return "video/mp2t"
    return "video/x-matroska"


def _make_playable_listitem(url, headers):
    """Create a ListItem with URL and optional HTTP auth headers.

    Uses Kodi's pipe-separated header syntax on the URL.
    """
    play_url = _build_play_url(url, headers)

    _resolver.xbmc.log("NZB-DAV: Play URL set (redacted)", _resolver.xbmc.LOGDEBUG)
    li = _resolver.xbmcgui.ListItem(path=play_url)
    # Skip HEAD request — nzbdav doesn't advertise Accept-Ranges on HEAD
    # which causes CFileCache to fail. Kodi will discover range support
    # on the first GET request instead.
    li.setContentLookup(False)
    # Set mime type based on file extension so Kodi doesn't need HEAD.
    # Strip query/fragment first so cache-busted URLs still detect correctly.
    li.setMimeType(_video_mime_for_path(_url_path(url)))
    return li


def _apply_remux_proxy_mime(li, stream_info):
    """Set the remux-proxy mime type and optional duration metadata."""
    is_hls = (
        stream_info.get("mode") == "hls"
        or stream_info.get("content_type") == "application/vnd.apple.mpegurl"
    )
    li.setMimeType("application/vnd.apple.mpegurl" if is_hls else "video/x-matroska")
    duration = stream_info.get("duration_seconds")
    if duration:
        li.getVideoInfoTag().setDuration(int(duration))


def _apply_proxy_mime(li, stream_url, stream_info):
    """Set mime type and any info metadata on a proxy ListItem."""
    proxy_url = li.getPath()
    if stream_info.get("remux"):
        _resolver.xbmc.log(
            "NZB-DAV: Playing via remux proxy: {}".format(proxy_url),
            _resolver.xbmc.LOGINFO,
        )
        _apply_remux_proxy_mime(li, stream_info)
    elif stream_info.get("faststart"):
        _resolver.xbmc.log(
            "NZB-DAV: Playing via faststart proxy: {}".format(proxy_url),
            _resolver.xbmc.LOGINFO,
        )
        li.setMimeType("video/mp4")
    else:
        _resolver.xbmc.log(
            "NZB-DAV: Playing via pass-through proxy: {}".format(proxy_url),
            _resolver.xbmc.LOGINFO,
        )
        li.setMimeType(_video_mime_for_path(_url_path(stream_url)))


def _stream_auth_header(stream_headers):
    if stream_headers and "Authorization" in stream_headers:
        return stream_headers["Authorization"]
    return None


def _stream_content_length_hint_key(stream_url, auth_header):
    return stream_url, auth_header or ""


def _remember_stream_content_length_hint(stream_url, auth_header, size):
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        return
    if not stream_url or size <= 0:
        return
    key = _stream_content_length_hint_key(stream_url, auth_header)
    expires_at = (
        _resolver.time.monotonic() + _resolver._STREAM_CONTENT_LENGTH_HINT_TTL_SECONDS
    )
    with _resolver._STREAM_CONTENT_LENGTH_HINTS_LOCK:
        _resolver._STREAM_CONTENT_LENGTH_HINTS[key] = (expires_at, size)
        while (
            len(_resolver._STREAM_CONTENT_LENGTH_HINTS)
            > _resolver._STREAM_CONTENT_LENGTH_HINTS_MAX
        ):
            _resolver._STREAM_CONTENT_LENGTH_HINTS.pop(
                min(
                    _resolver._STREAM_CONTENT_LENGTH_HINTS,
                    key=lambda key: _resolver._STREAM_CONTENT_LENGTH_HINTS[key][0],
                ),
                None,
            )


def _remember_resolved_stream_content_length_hint(
    video_path, stream_url, stream_headers
):
    try:
        from resources.lib import webdav as _webdav

        size = _webdav.get_video_file_size_hint(video_path)
    except Exception:  # pylint: disable=broad-except
        return
    _resolver._remember_stream_content_length_hint(
        stream_url, _stream_auth_header(stream_headers), size
    )


def _get_stream_content_length_hint(stream_url, auth_header):
    now = _resolver.time.monotonic()
    key = _stream_content_length_hint_key(stream_url, auth_header)
    with _resolver._STREAM_CONTENT_LENGTH_HINTS_LOCK:
        cached = _resolver._STREAM_CONTENT_LENGTH_HINTS.get(key)
        if cached is None:
            return 0
        expires_at, size = cached
        if expires_at <= now:
            _resolver._STREAM_CONTENT_LENGTH_HINTS.pop(key, None)
            return 0
        return size
