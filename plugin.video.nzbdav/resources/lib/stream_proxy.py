# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Local HTTP proxy for nzbdav WebDAV streams.

For MP4 files, remuxes on the fly to MKV using ffmpeg (-c copy, no
re-encoding).  This bypasses a Kodi CFileCache bug where parsing large
MP4 moov atoms over HTTP fails with 'corrupted STCO atom'.

For MKV and other files, proxies range requests directly to the remote
WebDAV server with proper 206 responses.
"""

import math
import os
import re
import shutil
import socket as _socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn as _ThreadingMixIn
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import xbmc

# mp4_parser functions are imported here so tests can patch them at this
# module's namespace.  They have no Kodi dependencies, so the import is safe
# at module load time.  If mp4_parser is unavailable (e.g. during a partial
# install) we fall back gracefully to None, which prepare_stream treats as a
# failed faststart parse.
try:
    from resources.lib.mp4_parser import (  # noqa: E402
        RangeCache,
        build_faststart_layout,
        fetch_remote_mp4_layout,
    )
except (ImportError, ModuleNotFoundError):
    RangeCache = None  # type: ignore[assignment,misc]
    build_faststart_layout = None  # type: ignore[assignment]
    fetch_remote_mp4_layout = None  # type: ignore[assignment]

from resources.lib.http_util import notify as _notify

# Singleton proxy instance
_proxy = None
_proxy_lock = threading.Lock()
_HLS_PRIVATE_TEMP_ROOT = None
_HLS_PRIVATE_TEMP_ROOT_LOCK = threading.Lock()
_MAX_STREAM_SESSIONS = 8
_SESSION_TTL_SECONDS = 6 * 3600
_PARSE_ERRORS = (
    ImportError,
    OSError,
    ValueError,
    KeyError,
    struct.error,
    HTTPException,
)
_KODI_SETTING_ERRORS = (
    AttributeError,
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)
_HLS_CLOSE_ERRORS = (
    AttributeError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    subprocess.SubprocessError,
)

# Common ffmpeg paths on CoreELEC / LibreELEC
_FFMPEG_PATHS = [
    "ffmpeg",
    "/storage/.kodi/addons.bak/tools.ffmpeg-tools/bin/ffmpeg",
    "/storage/.kodi/addons/tools.ffmpeg-tools/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    "/storage/.opt/bin/ffmpeg",
]

# ffprobe paths (same locations, swap the binary). ffprobe gives a clean
# `format=duration` response in one line and avoids parsing a wall of
# per-stream probe warnings from ffmpeg's stderr — critical for files with
# many subtitle tracks where those warnings push the `Duration:` header
# past any reasonable stderr buffer budget.
_FFPROBE_PATHS = [
    "ffprobe",
    "/storage/.kodi/addons.bak/tools.ffmpeg-tools/bin/ffprobe",
    "/storage/.kodi/addons/tools.ffmpeg-tools/bin/ffprobe",
    "/usr/bin/ffprobe",
    "/storage/.opt/bin/ffprobe",
]

# Pass-through proxy recovery constants
_UPSTREAM_OPEN_TIMEOUT = 30
_SKIP_PROBE_TIMEOUT = 10
# Geometric skip sizes for probing past a bad article region. 1 MB covers a
# single missing article (~700 KB). 16 MB covers a cluster of ~20 articles.
_SKIP_PROBE_SIZES = (1048576, 4194304, 16777216)
# When a probe fails fast (ConnectionRefused from docker-proxy during nzbdav
# restart, TCP RST, or immediate HTTP error) we back off and retry before
# moving to the next skip size. This gives a briefly-unavailable upstream a
# chance to recover instead of declaring the stream dead in milliseconds.
_PROBE_RETRY_DELAYS = (2, 4, 6, 8)
# Wall-clock budget for a single recovery attempt. After this the proxy
# zero-fills the remainder so the client response always completes.
_MAX_RECOVERY_SECONDS = 30
# Cap zero-filled bytes per response to prevent runaway silent playback when
# an NZB is mostly corrupt. 64 MB ≈ several seconds of 4K REMUX video.
_MAX_TOTAL_ZERO_FILL = 67108864
# Density breaker: abort if the recent recovery window becomes mostly synthetic
# data instead of real upstream bytes.
_DENSITY_BREAKER_WINDOW_BYTES = 16 * 1024 * 1024
_DENSITY_BREAKER_ZERO_FILL_RATIO = 0.5
# Chunk size for reading from the upstream HTTP response in _serve_proxy.
# Kept small (64 KB) because on 32-bit Kodi the address space is ~3 GB and
# Kodi's CFileCache can reserve up to ~1.5 GB on its own. A 1 MB read
# buffer has been observed to hit MemoryError when a second proxy
# connection opens during Kodi's CCurlFile reconnect-on-error recovery.
_UPSTREAM_READ_CHUNK = 65536

_STRICT_CONTRACT_MODE_OFF = "off"
_STRICT_CONTRACT_MODE_WARN = "warn"
_STRICT_CONTRACT_MODE_ENFORCE = "enforce"

_UPSTREAM_RANGE_OK = "OK"
_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE = "SHORT_READ_RECOVERABLE"
_UPSTREAM_RANGE_PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
_UPSTREAM_RANGE_UPSTREAM_ERROR = "UPSTREAM_ERROR"

_SESSION_ZERO_FILL_RATIO_MAX = 0.05
_RECOVERY_NOTIFY_DEBOUNCE_SECONDS = 60.0
_RANGE_RETRY_DELAYS = (2, 4, 8)

# Shared zero buffer reused across all pass-through responses.
_ZERO_FILL_BUFFER = bytes(65536)

# Socket write timeout for _serve_remux.  If Kodi stops reading from the
# proxy socket without closing it (decoder stalls for too long, e.g. during
# a long DB vacuum) wfile.write() would block forever and ffmpeg would keep
# producing output into the void.  60s comfortably exceeds any normal
# buffering stall on a healthy client while still bounding zombie lifetime.
_REMUX_WRITE_TIMEOUT = 60

# HLS segment length. Shorter segments (6 s) minimize the playlist-
# vs-actual drift that breaks seek accuracy and A/V sync on the fmp4
# path. The playlist emits fixed-duration EXTINF values based on
# this constant, but ffmpeg's `-hls_time` only places cuts at the
# next IDR after the target, so real segment durations drift ±GOP
# around the nominal. With 30 s segments and 3-5 s source GOPs that
# drift accumulates into visible A/V desync and seek misses over a
# 2-hour movie; with 6 s segments the per-segment error is the same
# but the accumulation window is shorter and a seek respawn lands
# much closer to the requested timestamp. The price is more segment
# file churn and more HTTP round-trips during linear playback, but
# HlsProducer uses ONE ffmpeg across many segments so cold-start
# amortization still holds. Also 6 s is the CMAF / Apple HLS author
# guide recommended default.
_HLS_SEGMENT_SECONDS = 6.0

# Disk-backed HLS session working directory. Must be on a filesystem
# with enough free space for the full remuxed output of any active
# session (~5 GB per 30 minutes at typical 4K REMUX bitrates). Each
# session gets its own subdirectory which is rm -rf'd on cleanup.
# Candidate paths in order — first one that exists + is writable wins.
# If none are available, fall back to a private mkdtemp() directory
# instead of a fixed shared /tmp path.
_HLS_WORKDIR_CANDIDATES = (
    "/var/media/CACHE_DRIVE/nzbdav-hls",
    "/var/media/STORAGE/nzbdav-hls",
    "/storage/nzbdav-hls",
)

# How long to wait for a segment file to appear on disk before
# declaring the fetch failed. Must exceed ffmpeg cold-start + a seek's
# worth of container parsing on the largest supported input.
_HLS_SEGMENT_WAIT_SECONDS = 90.0

# Segment file is considered complete when the next segment exists
# OR when its mtime has been stable for this many milliseconds.
_HLS_SEGMENT_MTIME_STABLE_MS = 500

# Hard wall-clock deadline for ffmpeg-based probes (duration, DV
# profile). These probes spawn ``ffmpeg -v info -i <url> -f null -``
# and scan stderr for a specific line. If ffmpeg hangs on the network
# read (slow upstream, auth negotiation, stalled header parse) it may
# never emit stderr output at all — without a wall-clock guard, the
# reader loop blocks forever. 20 s is very generous for a healthy
# LAN probe (typical: <2 s to Duration line on a 4K REMUX) and still
# bounded enough that a stuck probe can't wedge the prepare_stream
# path past the plugin client's 60 s /prepare timeout.
_PROBE_DEADLINE_SECONDS = 20.0


def _get_private_hls_temp_root():
    """Return a reusable private temp root for HLS work files."""
    global _HLS_PRIVATE_TEMP_ROOT  # pylint: disable=global-statement

    with _HLS_PRIVATE_TEMP_ROOT_LOCK:
        cached = _HLS_PRIVATE_TEMP_ROOT
        if cached and os.path.isdir(cached) and os.access(cached, os.W_OK):
            return cached

        temp_root = tempfile.mkdtemp(prefix="nzbdav-hls-")
        # 0o700 is restrictive (user-only); semgrep rule is a false positive.
        try:
            os.chmod(temp_root, 0o700)  # nosemgrep
        except OSError:
            pass

        _HLS_PRIVATE_TEMP_ROOT = temp_root
        return temp_root


def _choose_hls_workdir():
    """Return a writable base directory for HLS session working files.

    Walks the candidate list in order and returns the first entry
    whose parent exists, is writable, and has enough free space.
    Creates the leaf directory if missing. Falls back to a private
    temp directory as a last resort.
    """
    for base in _HLS_WORKDIR_CANDIDATES:
        parent = os.path.dirname(base) or "/"
        if not os.path.isdir(parent):
            continue
        if not os.access(parent, os.W_OK):
            continue
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            continue
        return base
    return _get_private_hls_temp_root()


def _find_ffmpeg():
    """Find an ffmpeg binary on the system."""
    for path in _FFMPEG_PATHS:
        found = shutil.which(path)
        if found:
            return found
    return None


def _find_ffprobe():
    """Find an ffprobe binary on the system."""
    for path in _FFPROBE_PATHS:
        found = shutil.which(path)
        if found:
            return found
    return None


# Default threshold above which non-MP4 files are force-remuxed through
# ffmpeg instead of served as HTTP pass-through.  0 disables force-remux
# entirely.
#
# History: an earlier branch disabled force-remux by default because 12 GB
# MKV pass-through tested clean on a 32-bit Amlogic CoreELEC build. A later
# 58 GB Shawshank REMUX (and a reproduced 15.8 GB Mayor of Kingstown remux)
# both crashed with `Open - Unhandled exception` in `CVideoPlayer::
# OpenInputStream`, even though the proxy's HTTP/206 range responses are
# byte-correct under curl. The crash is deterministic at byte 0, so it isn't
# file corruption or transport — it's a 32-bit overflow somewhere in Kodi's
# cache/offset math when the advertised Content-Length is large enough.
# The existing "pass-through works for 12 GB" data point and the "58 GB
# crashes" data point put the real ceiling somewhere between those, which
# is why the default is set generously below the lowest known-bad size.
#
# ffmpeg-remux is strictly worse on files that would have passed through
# fine — seeks go through ffmpeg `-ss` instead of the source's own Cue
# index, missing Usenet articles no longer zero-fill transparently, and
# there is real CPU cost — so the threshold is kept high enough that only
# genuinely huge files get remuxed.  Users who see false positives can
# set `force_remux_threshold_mb` in the addon settings to raise the bar
# further (or to 0 to disable entirely and restore pure pass-through).
_DEFAULT_FORCE_REMUX_THRESHOLD_MB = 20000
_FORCE_REMUX_THRESHOLD_MB_MAX = 1048576
_PREPARE_REQUEST_MAX_BYTES = 64 * 1024
_FFMPEG_CAPABILITY_PROBE_TIMEOUT = 5
_FMP4_HLS_CAPABILITY_MARKERS = (
    "-hls_segment_type",
    "-hls_fmp4_init_filename",
)


def _get_addon_setting(setting_id, default=None):
    """Best-effort Kodi addon setting lookup safe for tests and CLI."""
    try:
        import xbmcaddon

        value = xbmcaddon.Addon().getSetting(setting_id)
    except _KODI_SETTING_ERRORS:
        return default
    return default if value is None else value


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


def _get_server_context_lock(server):
    """Return the proxy's context lock when the handler is attached to one."""
    server_state = getattr(server, "__dict__", None)
    if not isinstance(server_state, dict):
        return None
    owner_proxy = server_state.get("owner_proxy")
    return getattr(owner_proxy, "_context_lock", None)


def _get_force_remux_threshold_bytes():
    """Return the remux-force threshold in bytes, or 0 to disable."""
    raw = _get_addon_setting("force_remux_threshold_mb")
    try:
        mb = int(raw) if raw not in (None, "") else _DEFAULT_FORCE_REMUX_THRESHOLD_MB
    except (TypeError, ValueError):
        mb = _DEFAULT_FORCE_REMUX_THRESHOLD_MB
    mb = _clamp_int_setting(
        "force_remux_threshold_mb", mb, 0, _FORCE_REMUX_THRESHOLD_MB_MAX
    )
    if mb == 0:
        return 0
    return mb * 1024 * 1024


def _get_force_remux_mode():
    """Return 'matroska' or 'hls_fmp4' for the force-remux branch.

    Empty string, unset, or '0' -> 'matroska' (default, control path).
    '1' -> 'hls_fmp4' (experimental, DV-capable).
    Any other value -> 'matroska' (safe fall-through).
    """
    raw = _get_addon_setting("force_remux_mode")
    if raw is None:
        return "matroska"
    return "hls_fmp4" if raw == "1" else "matroska"


def _get_strict_contract_mode():
    """Return off/warn/enforce for upstream response validation."""
    raw = _get_addon_setting("strict_contract_mode")
    key = str(raw).strip().lower() if raw is not None else ""
    mapping = {
        "0": _STRICT_CONTRACT_MODE_OFF,
        _STRICT_CONTRACT_MODE_OFF: _STRICT_CONTRACT_MODE_OFF,
        "1": _STRICT_CONTRACT_MODE_WARN,
        _STRICT_CONTRACT_MODE_WARN: _STRICT_CONTRACT_MODE_WARN,
        "2": _STRICT_CONTRACT_MODE_ENFORCE,
        _STRICT_CONTRACT_MODE_ENFORCE: _STRICT_CONTRACT_MODE_ENFORCE,
    }
    return mapping.get(key, _STRICT_CONTRACT_MODE_WARN)


def _get_bool_setting(setting_id, default=False):
    """Return a Kodi bool-like setting with a safe default."""
    raw = _get_addon_setting(setting_id)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _density_breaker_enabled(contract_mode=None):
    """Return True when the recovery density breaker should run."""
    mode = contract_mode or _get_strict_contract_mode()
    if mode == _STRICT_CONTRACT_MODE_OFF:
        return False
    return _get_bool_setting("density_breaker_enabled", default=False)


def _zero_fill_budget_enabled():
    return _get_bool_setting("zero_fill_budget_enabled", default=True)


def _retry_ladder_enabled():
    return _get_bool_setting("retry_ladder_enabled", default=True)


def _send_200_no_range_enabled():
    return _get_bool_setting("send_200_no_range", default=False)


def _expected_content_range(start, end, content_length):
    return "bytes {}-{}/{}".format(start, end, content_length)


def _get_header(resp, name):
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    return headers.get(name)


def _classify_contract_mismatch(
    status, content_range, content_length, start, end, total
):
    """Classify upstream header contract issues as hard or soft mismatches."""
    expected_length = end - start + 1
    expected_range = _expected_content_range(start, end, total)
    is_full_object = start == 0 and end == total - 1
    # HTTP/1.1 (RFC 9110) permits optional leading/trailing whitespace in
    # header values. Strip so an upstream that emits "Content-Length: 1024 "
    # (trailing space) doesn't get flagged as a protocol mismatch.
    if isinstance(content_range, str):
        content_range = content_range.strip()
    if isinstance(content_length, str):
        content_length = content_length.strip()
    problems = []
    hard = False

    if status != 206:
        problems.append("status={} expected=206".format(status))
        if not (status == 200 and is_full_object):
            hard = True

    if status == 206:
        if content_range in (None, ""):
            problems.append(
                "Content-Range missing expected={!r}".format(expected_range)
            )
        elif content_range != expected_range:
            problems.append(
                "Content-Range={!r} expected={!r}".format(content_range, expected_range)
            )
            hard = True
    elif (
        status != 206
        and content_range not in (None, "")
        and content_range != expected_range
    ):
        problems.append(
            "Content-Range={!r} expected={!r}".format(content_range, expected_range)
        )
        if not (status == 200 and is_full_object):
            hard = True

    if content_length != str(expected_length):
        problems.append(
            "Content-Length={!r} expected={!r}".format(
                content_length, str(expected_length)
            )
        )

    if not problems:
        return None, False
    return "; ".join(problems), hard


def _log_contract_mismatch(start, end, status, content_range, content_length, detail):
    xbmc.log(
        "NZB-DAV: Upstream contract mismatch for {}-{} status={} "
        "Content-Range={!r} Content-Length={!r} detail={} "
        "(reason=protocol_mismatch)".format(
            start, end, status, content_range, content_length, detail
        ),
        xbmc.LOGWARNING,
    )


def _record_density_window(window, kind, count):
    """Track recent forward progress vs. zero-fill bytes in a fixed window."""
    if count <= 0:
        return
    window.append([kind, count])
    total = sum(item[1] for item in window)
    while total > _DENSITY_BREAKER_WINDOW_BYTES and window:
        overflow = total - _DENSITY_BREAKER_WINDOW_BYTES
        head = window[0]
        trim = min(head[1], overflow)
        head[1] -= trim
        total -= trim
        if head[1] == 0:
            window.popleft()


def _density_ratio(window):
    total = sum(item[1] for item in window)
    if total <= 0:
        return 0.0
    zero_fill = sum(item[1] for item in window if item[0] == "zero_fill")
    return float(zero_fill) / float(total)


def _would_trip_density_breaker(window, skip):
    if skip <= 0:
        return False
    trial = deque([item[:] for item in window])
    _record_density_window(trial, "zero_fill", skip)
    return _density_ratio(trial) > _DENSITY_BREAKER_ZERO_FILL_RATIO


def _read_session_recovery_state(ctx):
    return {
        "streamed": int(ctx.get("session_streamed_bytes", 0) or 0),
        "zero_fill": int(ctx.get("session_zero_fill_bytes", 0) or 0),
        "recoveries": int(ctx.get("session_recovery_count", 0) or 0),
        "last_notify": float(ctx.get("last_recovery_notify_at", 0) or 0),
    }


def _update_session_recovery_state(server, ctx, streamed=0, zero_fill=0, recoveries=0):
    """Apply session-level recovery counters under the proxy context lock."""
    context_lock = _get_server_context_lock(server)

    def _update():
        state = _read_session_recovery_state(ctx)
        state["streamed"] += streamed
        state["zero_fill"] += zero_fill
        state["recoveries"] += recoveries
        ctx["session_streamed_bytes"] = state["streamed"]
        ctx["session_zero_fill_bytes"] = state["zero_fill"]
        ctx["session_recovery_count"] = state["recoveries"]
        return state

    if context_lock is None:
        return _update()
    with context_lock:
        return _update()


def _project_session_zero_fill_ratio(
    server, ctx, extra_zero_fill=0, extra_recoveries=0
):
    """Return the projected session zero-fill ratio if another gap is skipped."""
    context_lock = _get_server_context_lock(server)

    def _project():
        state = _read_session_recovery_state(ctx)
        projected_zero_fill = state["zero_fill"] + extra_zero_fill
        projected_recoveries = state["recoveries"] + extra_recoveries
        denominator = max(
            int(ctx.get("content_length", 0) or 0),
            state["streamed"] + projected_zero_fill,
        )
        ratio = float(projected_zero_fill) / float(denominator or 1)
        return projected_zero_fill, projected_recoveries, ratio

    if context_lock is None:
        return _project()
    with context_lock:
        return _project()


def _maybe_notify_recovery_summary(
    server, ctx, zero_fill_bytes=None, recovery_count=None
):
    """Send a debounced recovery summary notification for this session."""
    context_lock = _get_server_context_lock(server)
    now = time.time()

    def _prepare():
        state = _read_session_recovery_state(ctx)
        skipped = state["zero_fill"] if zero_fill_bytes is None else zero_fill_bytes
        recoveries = state["recoveries"] if recovery_count is None else recovery_count
        if recoveries <= 0:
            return None
        if state["last_notify"] and (
            now - state["last_notify"] < _RECOVERY_NOTIFY_DEBOUNCE_SECONDS
        ):
            return None
        ctx["last_recovery_notify_at"] = now
        return skipped, recoveries

    if context_lock is None:
        payload = _prepare()
    else:
        with context_lock:
            payload = _prepare()

    if payload is None:
        return False
    skipped, recoveries = payload
    try:
        _notify(
            "NZB-DAV",
            "Skipped {} bytes across {} recoveries".format(skipped, recoveries),
        )
    except (RuntimeError, OSError):
        return False
    return True


def _validate_url(url):
    """Reject URLs with unexpected schemes to prevent command injection."""
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError("Invalid URL scheme: {}".format(repr(url)[:30]))


def _notify_error(message):
    """Best-effort notification helper safe to call from proxy threads."""
    try:
        _notify("NZB-DAV", str(message)[:80])
    except (RuntimeError, OSError):
        pass


def _embed_auth_in_url(url, auth_header):
    """Embed Basic auth credentials into a URL for ffmpeg.

    DEPRECATED for new code paths — prefer ``_ffmpeg_auth_args``,
    which passes the Authorization header to ffmpeg via ``-headers``
    instead of splicing ``user:password@host`` into the URL. The URL
    form leaks credentials into ffmpeg's argv, where they're visible
    via ``ps`` and ``/proc/<pid>/cmdline``, and (worse) into ffmpeg
    error messages that can end up in the persistent ffmpeg.log
    archive. Kept here only for callers that still embed-then-pass.
    """
    if auth_header and auth_header.startswith("Basic "):
        import base64

        try:
            decoded = base64.b64decode(auth_header[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return url

        username, sep, password = decoded.partition(":")
        if not sep:
            return url

        parsed = urlsplit(url)
        host_part = parsed.netloc.rsplit("@", 1)[-1]
        userinfo = "{}:{}".format(quote(username, safe=""), quote(password, safe=""))
        return urlunsplit(
            (
                parsed.scheme,
                "{}@{}".format(userinfo, host_part),
                parsed.path,
                parsed.query,
                parsed.fragment,
            )
        )
    return url


def _ffmpeg_auth_args(auth_header):
    """Return ffmpeg ``-headers ...`` argv fragment for an
    Authorization header, or an empty list if no auth is present.

    Pass the result to ``cmd.extend(...)`` BEFORE the ``-i URL``
    pair. ffmpeg's HTTP demuxer reads ``-headers`` as a string of
    HTTP headers separated by ``\\r\\n``; the trailing ``\\r\\n``
    is required to terminate the header line.

    Why this exists: the URL-embedding form (``_embed_auth_in_url``)
    splices ``user:password@host`` into argv, where the cleartext
    credentials are visible to other local processes via ``ps`` /
    ``/proc/cmdline``, and end up in ffmpeg error messages and
    therefore in the persistent ffmpeg.log archive. The ``-headers``
    form keeps the URL clean for logging and only puts the (still
    base64-encoded) Authorization line into argv. On a single-user
    Kodi appliance this is mostly a defense-in-depth + log-redaction
    win, but on multi-user systems the difference is meaningful.
    """
    if not auth_header:
        return []
    if not isinstance(auth_header, str):
        return []
    return ["-headers", "Authorization: {}\r\n".format(auth_header)]


def _parse_ffmpeg_duration(stderr_text):
    """Parse 'Duration: HH:MM:SS.xx' from ffmpeg stderr output.

    Returns duration in seconds as a float, or None if not found.
    """
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", stderr_text)
    if not match:
        return None
    hours, minutes, seconds, frac = match.groups()
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(frac) / (10 ** len(frac))
    )


def _parse_ffmpeg_dv_profile(stderr_text):
    """Parse the Dolby Vision profile from ffmpeg stderr output.

    When the source video track carries a Dolby Vision configuration
    record, ffmpeg prints it as stream side data in the header, e.g.::

        Side data:
          DOVI configuration record: version: 1.0, profile: 7, level: 6,
          rpu flag: 1, el flag: 1, bl flag: 1, compatibility id: 0

    Returns the integer profile (5, 7, 8, ...) if found, else None.

    A ``None`` return covers both "no DV metadata" and "could not parse"
    — callers should treat it as "assume the fmp4 path is safe" rather
    than "definitely no DV". Only a confirmed profile 7 (dual-layer
    FEL with base + enhancement layer + RPU) must be routed around
    fmp4 HLS, because fmp4 has no standard way to carry two HEVC
    layers in a single track.
    """
    match = re.search(
        r"DOVI configuration record:[^\n]*?profile:\s*(\d+)",
        stderr_text,
    )
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


# Byte-offset delta used to distinguish a Kodi buffer-reconnect from a
# user-initiated seek.  When Kodi reconnects after a brief network hiccup it
# resumes very close to where it left off; a true seek jumps much further.
# 10 MB was chosen empirically: large enough to ignore normal buffering
# overlap, small enough to catch seeks that would noticeably re-position
# the stream.  Adjust if you observe unnecessary ffmpeg restarts in logs.
_SEEK_THRESHOLD = 10 * 1024 * 1024


def _is_seek_request(current_byte_pos, requested_byte_pos):
    """Determine if a range request is a genuine seek or a continuation.

    Returns True if the request is far from the current position (>10MB
    gap or backward), meaning ffmpeg should be restarted with -ss.
    """
    delta = requested_byte_pos - current_byte_pos
    if delta < 0:
        return True  # backward seek
    return delta > _SEEK_THRESHOLD


class _StreamHandler(BaseHTTPRequestHandler):
    """HTTP handler that remuxes MP4 to MKV or proxies other formats."""

    protocol_version = "HTTP/1.1"
    close_connection = False

    def log_message(self, fmt, *args):  # pylint: disable=arguments-differ
        xbmc.log("NZB-DAV: Proxy: {}".format(fmt % args), xbmc.LOGDEBUG)

    def _get_stream_context(self):
        """Look up the active stream context for the current request path.

        Recognizes both direct-stream paths (``/stream`` and
        ``/stream/<session_id>``) and HLS paths
        (``/hls/<session_id>/playlist.m3u8`` and
        ``/hls/<session_id>/seg_<N>.ts``). The HLS parsing layer uses this
        to resolve a session; the playlist/segment dispatch then branches
        on the trailing resource in ``_handle_hls``.
        """
        raw_path = getattr(self, "path", "/stream")
        path = raw_path.split("?", 1)[0]
        context_lock = _get_server_context_lock(getattr(self, "server", None))
        if path in ("", "/stream"):
            if context_lock is None:
                return getattr(self.server, "stream_context", None)
            with context_lock:
                return getattr(self.server, "stream_context", None)

        session_id = None
        if path.startswith("/stream/"):
            session_id = path[len("/stream/") :]
            if not session_id or "/" in session_id:
                return None
        elif path.startswith("/hls/"):
            parts = path[len("/hls/") :].split("/", 1)
            if len(parts) != 2 or not parts[0]:
                return None
            session_id = parts[0]
        else:
            return None

        if context_lock is None:
            sessions = getattr(self.server, "stream_sessions", {})
            ctx = sessions.get(session_id)
            if ctx is not None:
                ctx["last_access"] = time.time()
            return ctx

        with context_lock:
            sessions = getattr(self.server, "stream_sessions", {})
            ctx = sessions.get(session_id)
            if ctx is not None:
                ctx["last_access"] = time.time()
            return ctx

    @staticmethod
    def _parse_hls_resource(path):
        """Extract (session_id, resource) from an /hls/ path, or None.

        Returns a tuple ``(session_id, resource)`` where ``resource``
        is one of:

        - ``"playlist"`` — ``/hls/<session>/playlist.m3u8``
        - ``"init"`` — ``/hls/<session>/init.mp4`` (fmp4 path)
        - ``("segment", N, "ts")`` — legacy mpegts segment
        - ``("segment", N, "m4s")`` — fmp4 segment

        Returns ``None`` for malformed paths so the caller can 404.

        The parser is extension-permissive for segments — it accepts
        both .ts and .m4s regardless of session state. Handler-level
        validation (``do_HEAD`` / ``_handle_hls``) enforces that the
        returned extension matches the session's
        ``hls_segment_format``, returning 404 on mismatch.
        """
        if not path.startswith("/hls/"):
            return None
        parts = path[len("/hls/") :].split("/", 1)
        if len(parts) != 2 or not parts[0]:
            return None
        session_id, resource = parts
        if resource == "playlist.m3u8":
            return session_id, "playlist"
        if resource == "init.mp4":
            return session_id, "init"
        if resource.startswith("seg_"):
            for ext in ("ts", "m4s"):
                suffix = "." + ext
                if resource.endswith(suffix):
                    try:
                        seg_n = int(resource[len("seg_") : -len(suffix)])
                    except ValueError:
                        return None
                    if seg_n < 0:
                        return None
                    return session_id, ("segment", seg_n, ext)
        return None

    @staticmethod
    def _ctx_lock(ctx, server):
        """Get the remux lock for this stream context."""
        return ctx.get("ffmpeg_lock") or getattr(server, "ffmpeg_lock")

    def _send_close_response_headers(self, status_code, content_type, accept_ranges):
        """Send a streaming response that explicitly closes the socket."""
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", accept_ranges)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def _start_remux_process(self, ctx, requested_start, seek_seconds):
        """Launch ffmpeg for a remux response and register it on the session."""
        cmd = self._build_ffmpeg_cmd(ctx, seek_seconds=seek_seconds)
        if not self._is_safe_ffmpeg_cmd(cmd):
            xbmc.log("NZB-DAV: Refusing to start unsafe ffmpeg command", xbmc.LOGERROR)
            _notify_error("Failed to start ffmpeg")
            self.send_error(500)
            return None, None
        xbmc.log(
            "NZB-DAV: Remuxing to MKV (seek={})".format(seek_seconds),
            xbmc.LOGINFO,
        )
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False
            )
        except OSError as error:
            xbmc.log("NZB-DAV: Failed to start ffmpeg: {}".format(error), xbmc.LOGERROR)
            _notify_error("Failed to start ffmpeg")
            self.send_error(500)
            return None, None

        lock = self._ctx_lock(ctx, self.server)
        with lock:
            ctx["active_ffmpeg"] = proc
            ctx["current_byte_pos"] = requested_start
            self.server.active_ffmpeg = proc
            self.server.current_byte_pos = requested_start
        return proc, lock

    @staticmethod
    def _start_stderr_drain(proc):
        """Drain ffmpeg stderr in a background thread to avoid pipe stalls."""
        stderr_chunks = deque(maxlen=50)

        def _drain_stderr():
            try:
                while True:
                    data = proc.stderr.read(4096)
                    if not data:
                        break
                    stderr_chunks.append(data)
            except (OSError, ValueError):
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()
        return stderr_chunks, stderr_thread

    def _update_current_byte_pos(self, ctx, lock, current_pos):
        """Keep session and server byte positions in sync while remuxing."""
        with lock:
            ctx["current_byte_pos"] = current_pos
            self.server.current_byte_pos = current_pos

    def _stream_remux_output(self, ctx, proc, lock, requested_start):
        """Copy ffmpeg stdout to Kodi until EOF or client disconnect."""
        total = 0
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    return total
                self.wfile.write(chunk)
                total += len(chunk)
                self._update_current_byte_pos(ctx, lock, requested_start + total)
        except (BrokenPipeError, ConnectionResetError, _socket.timeout):
            xbmc.log(
                "NZB-DAV: Remux client disconnected after {} MB".format(
                    total // 1048576
                ),
                xbmc.LOGDEBUG,
            )
            return total

    def _finish_remux(self, ctx, proc, lock, stderr_chunks, stderr_thread, total):
        """Tear down ffmpeg and emit completion logs for a remux request."""
        try:
            proc.kill()
            proc.wait()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

        with lock:
            if ctx.get("active_ffmpeg") is proc:
                ctx["active_ffmpeg"] = None
            if self.server.active_ffmpeg is proc:
                self.server.active_ffmpeg = None

        stderr_thread.join(timeout=5)
        stderr = b"".join(stderr_chunks).decode(errors="replace")
        if stderr.strip():
            xbmc.log("NZB-DAV: ffmpeg: {}".format(stderr[:300]), xbmc.LOGDEBUG)
        xbmc.log(
            "NZB-DAV: Remux done: {} MB sent".format(total // 1048576),
            xbmc.LOGINFO,
        )

    @staticmethod
    def _is_safe_ffmpeg_cmd(cmd):
        """Validate command shape and executable before subprocess execution."""
        if not isinstance(cmd, (list, tuple)) or not cmd:
            return False
        if not all(isinstance(arg, str) for arg in cmd):
            return False
        exe = cmd[0]
        exe_name = os.path.basename(exe).lower()
        if exe_name != "ffmpeg":
            return False
        for arg in cmd:
            if "\x00" in arg or "\n" in arg or "\r" in arg:
                return False
        return True

    @staticmethod
    def _append_mpegts_output_args(cmd):
        """Append ffmpeg output args for the MPEG-TS remux path."""
        cmd.extend(
            [
                "-sn",
                "-f",
                "mpegts",
                "-fflags",
                "+genpts",
                "-mpegts_copyts",
                "1",
                "pipe:1",
            ]
        )
        return cmd

    @staticmethod
    def _append_subtitle_args(cmd, input_url):
        """Append subtitle mapping flags for MKV remux output."""
        if _get_addon_setting("proxy_convert_subs") == "false":
            return
        src_is_mkv = input_url.split("?", 1)[0].lower().endswith(".mkv")
        sub_codec = "copy" if src_is_mkv else "srt"
        cmd.extend(["-map", "0:s?", "-c:s", sub_codec])

    @staticmethod
    def _append_duration_metadata(cmd, duration_secs, seek_seconds):
        """Append a DURATION tag so Kodi gets a finite timeline."""
        if duration_secs is None:
            return
        remaining = duration_secs
        if seek_seconds is not None and seek_seconds > 0:
            remaining = max(0, duration_secs - seek_seconds)
        hours = int(remaining // 3600)
        mins = int((remaining % 3600) // 60)
        secs = remaining % 60
        cmd.extend(
            [
                "-metadata",
                "DURATION={:02d}:{:02d}:{:06.3f}".format(hours, mins, secs),
            ]
        )

    def do_POST(self):
        """Handle POST /prepare — plugin sends stream config via HTTP."""
        import json

        if self.path.split("?", 1)[0] != "/prepare":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self.send_error(400)
            return
        if length < 0:
            self.send_error(400)
            return
        if length > _PREPARE_REQUEST_MAX_BYTES:
            self.send_error(413)
            return
        body = self.rfile.read(length) if length else b""
        try:
            data = json.loads(body)
        except (ValueError, KeyError):
            self.send_error(400)
            return

        remote_url = data.get("remote_url", "")
        auth_header = data.get("auth_header")
        if not remote_url:
            self.send_error(400)
            return

        proxy = self.server.owner_proxy
        try:
            proxy_url, stream_info = proxy.prepare_stream(remote_url, auth_header)
        except ValueError:
            self.send_error(400)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        result = {"proxy_url": proxy_url}
        result.update(stream_info)
        resp = json.dumps(result).encode()
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_HEAD(self):
        """Respond to HEAD with content metadata (type, length, ranges)."""
        raw_path = getattr(self, "path", "/stream").split("?", 1)[0]
        if raw_path.startswith("/hls/"):
            parsed = self._parse_hls_resource(raw_path)
            if parsed is None:
                self.send_error(404)
                return
            ctx = self._get_stream_context()
            if ctx is None or ctx.get("mode") != "hls":
                self.send_error(404)
                return
            _session_id, resource = parsed
            seg_fmt = ctx.get("hls_segment_format", "mpegts")

            if resource == "playlist":
                content_type = "application/vnd.apple.mpegurl"
            elif resource == "init":
                if seg_fmt != "fmp4":
                    self.send_error(404)
                    return
                content_type = "video/mp4"
            elif isinstance(resource, tuple) and resource[0] == "segment":
                _, _seg_n, ext = resource
                expected_ext = "m4s" if seg_fmt == "fmp4" else "ts"
                if ext != expected_ext:
                    self.send_error(404)
                    return
                content_type = "video/mp4" if seg_fmt == "fmp4" else "video/mp2t"
            else:
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Connection", "close")
            self.end_headers()
            return

        ctx = self._get_stream_context()
        if ctx is None:
            self.send_error(404)
            return
        if ctx.get("faststart"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(ctx["virtual_size"]))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        elif ctx.get("temp_faststart"):
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(ctx["content_length"]))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        elif ctx.get("remux"):
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Accept-Ranges", "none")
            self.send_header("Connection", "close")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", ctx["content_type"])
            self.send_header("Content-Length", str(ctx["content_length"]))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

    def do_GET(self):
        """Route requests to the appropriate handler."""
        raw_path = getattr(self, "path", "/stream").split("?", 1)[0]
        if raw_path.startswith("/hls/"):
            self._handle_hls(raw_path)
            return

        ctx = self._get_stream_context()
        if ctx is None:
            self.send_error(404)
            return

        if ctx.get("faststart"):
            self._serve_mp4_faststart(ctx)
        elif ctx.get("temp_faststart"):
            self._serve_temp_faststart(ctx)
        elif ctx.get("remux"):
            self._serve_remux(ctx)
        else:
            self._serve_proxy(ctx)

    def _handle_hls(self, path):
        """Dispatch an /hls/<session>/... GET to playlist, init, or
        segment. Enforces strict extension↔ctx-mode validation so a
        request with the wrong extension for the session's segment
        format returns 404 rather than being silently served.
        """
        parsed = self._parse_hls_resource(path)
        if parsed is None:
            self.send_error(404)
            return
        ctx = self._get_stream_context()
        if ctx is None or ctx.get("mode") != "hls":
            self.send_error(404)
            return
        _session_id, resource = parsed
        seg_fmt = ctx.get("hls_segment_format", "mpegts")

        if resource == "playlist":
            self._serve_hls_playlist(ctx)
            return
        if resource == "init":
            if seg_fmt != "fmp4":
                self.send_error(404)
                return
            self._serve_hls_init(ctx)
            return
        if isinstance(resource, tuple) and resource[0] == "segment":
            _, seg_n, ext = resource
            expected_ext = "m4s" if seg_fmt == "fmp4" else "ts"
            if ext != expected_ext:
                self.send_error(404)
                return
            self._serve_hls_segment(ctx, seg_n)
            return
        self.send_error(404)

    def _build_ffmpeg_cmd(self, ctx, seek_seconds=None):
        """Build the ffmpeg remux command list.

        Output format is driven by ``ctx["output_format"]``:

        - ``"mpegts"`` — force-remux path for huge MKVs that overflow
          32-bit Kodi's CFileCache. No subtitles (MPEG-TS can't carry
          PGS/HDMV), no duration metadata (TS has no container-level
          duration field), seek is handled HTTP-side via restart-on-Range.
        - ``"matroska"`` (default) — MP4 fallback path. Subtitles copy
          through, duration is written into the MKV header so Kodi's
          progress bar is accurate.
        """
        ffmpeg = ctx["ffmpeg_path"]
        input_url = ctx["remote_url"]
        _validate_url(input_url)
        auth_args = _ffmpeg_auth_args(ctx.get("auth_header"))
        output_format = ctx.get("output_format", "matroska")

        cmd = [ffmpeg]
        if seek_seconds is not None and seek_seconds > 0:
            cmd.extend(["-ss", "{:.3f}".format(seek_seconds)])
        cmd.extend(
            [
                "-v",
                "warning",
                "-reconnect",
                "1",
                "-reconnect_streamed",
                "1",
            ]
        )
        if auth_args:
            cmd.extend(auth_args)
        cmd.extend(
            [
                "-i",
                input_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a",
            ]
        )

        # Use explicit per-stream copy to avoid -c copy overriding -c:s srt
        cmd.extend(["-c:v", "copy", "-c:a", "copy"])

        if output_format == "mpegts":
            # MPEG-TS can carry DVB subs/teletext but not PGS or HDMV
            # bitmap subs, and ffmpeg can't transcode between those. Drop
            # subtitles entirely for the TS path — simpler, robust, and
            # external .srt files still work via Kodi's own loader.
            return self._append_mpegts_output_args(cmd)

        # Subtitle handling (toggleable via setting).
        # For MP4 input we convert text subs (mov_text/TX3G) to SRT so MKV
        # output is more compatible.  For MKV input we must use `copy` —
        # PGS/DVD/HDMV bitmap subs can't be re-encoded to SRT and would
        # abort the remux; ASS/SSA/SRT all copy fine into MKV anyway.
        self._append_subtitle_args(cmd, input_url)

        # Write duration into MKV Segment Info so Kodi knows the total
        # length.  Without this, piped MKV has no Duration element and
        # Kodi treats the stream as live (no progress bar, no seeking,
        # no pause).  -metadata DURATION= makes ffmpeg's matroska muxer
        # write the Duration element in the header.
        self._append_duration_metadata(cmd, ctx.get("duration_seconds"), seek_seconds)

        cmd.extend(
            [
                "-f",
                "matroska",
                "-fflags",
                "+genpts+flush_packets",
                "pipe:1",
            ]
        )
        return cmd

    def _serve_mp4_faststart(self, ctx):
        """Serve MP4 with virtual faststart layout (moov before mdat)."""
        header_data = ctx["header_data"]
        virtual_size = ctx["virtual_size"]
        payload_remote_start = ctx["payload_remote_start"]
        payload_size = ctx["payload_size"]
        header_len = len(header_data)

        # Parse Range header
        range_header = self.headers.get("Range")
        if range_header:
            start, end = self._parse_range(range_header, virtual_size)
            if start is None:
                self.send_error(416)
                return
        else:
            start, end = 0, virtual_size - 1

        length = end - start + 1
        if range_header:
            self.send_response(206)
            self.send_header(
                "Content-Range",
                "bytes {}-{}/{}".format(start, end, virtual_size),
            )
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        bytes_sent = 0
        pos = start

        try:
            while bytes_sent < length:
                remaining = length - bytes_sent

                if pos < header_len:
                    # Serve from cached header (ftyp + moov)
                    chunk_end = min(header_len, pos + remaining)
                    self.wfile.write(header_data[pos:chunk_end])
                    sent = chunk_end - pos
                    bytes_sent += sent
                    pos += sent

                elif pos < header_len + payload_size:
                    # Serve from remote payload via a single streaming connection.
                    # One HTTP range request for the entire remaining payload,
                    # then stream chunks through to Kodi.  This avoids per-chunk
                    # connection overhead that causes slow seeking.
                    payload_offset = pos - header_len
                    remote_pos = payload_remote_start + payload_offset
                    remote_end = payload_remote_start + payload_size - 1

                    req = Request(ctx["remote_url"])
                    req.add_header(
                        "Range", "bytes={}-{}".format(remote_pos, remote_end)
                    )
                    if ctx.get("auth_header"):
                        req.add_header("Authorization", ctx["auth_header"])

                    # nosemgrep
                    with urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
                        req, timeout=120
                    ) as resp:
                        while bytes_sent < length:
                            chunk = resp.read(1048576)  # 1 MB read buffer
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            bytes_sent += len(chunk)
                            pos += len(chunk)
                    break  # done streaming
                else:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError, HTTPException) as e:
            xbmc.log("NZB-DAV: Faststart proxy error: {}".format(e), xbmc.LOGERROR)
            _notify_error(e)

    def _serve_temp_faststart(self, ctx):
        """Serve a temp-file faststart MP4 with range support."""
        temp_path = ctx["temp_path"]
        if not os.path.exists(temp_path):
            self.send_error(404)
            return

        file_size = ctx["content_length"]
        range_header = self.headers.get("Range")
        if range_header:
            start, end = self._parse_range(range_header, file_size)
            if start is None:
                self.send_error(416)
                return
        else:
            start, end = 0, file_size - 1

        length = end - start + 1
        if range_header:
            self.send_response(206)
            self.send_header(
                "Content-Range",
                "bytes {}-{}/{}".format(start, end, file_size),
            )
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            with open(temp_path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(remaining, 1048576))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            xbmc.log("NZB-DAV: Temp faststart error: {}".format(e), xbmc.LOGERROR)
            _notify_error(e)

    def _resolve_seek(self, ctx, requested_start, total_bytes):
        """Compute seek position and kill prior ffmpeg if needed.

        Returns the seek offset in seconds, or None.
        """
        duration = ctx.get("duration_seconds")
        seekable = ctx.get("seekable", False)

        seek_seconds = None
        if seekable and duration is not None and total_bytes and requested_start > 0:
            seek_seconds = (requested_start / total_bytes) * duration

        lock = self._ctx_lock(ctx, self.server)
        with lock:
            current_pos = ctx.get(
                "current_byte_pos", getattr(self.server, "current_byte_pos", 0)
            )
            is_seek = (
                seekable
                and requested_start > 0
                and _is_seek_request(current_pos, requested_start)
            )
            if is_seek:
                xbmc.log(
                    "NZB-DAV: Seek to byte {} -> {:.1f}s".format(
                        requested_start, seek_seconds
                    ),
                    xbmc.LOGINFO,
                )
                active_ffmpeg = ctx.get(
                    "active_ffmpeg", getattr(self.server, "active_ffmpeg", None)
                )
                if active_ffmpeg:
                    try:
                        active_ffmpeg.kill()
                        active_ffmpeg.wait()
                    except OSError:
                        pass
                    ctx["active_ffmpeg"] = None
                    self.server.active_ffmpeg = None

        return seek_seconds

    def _serve_remux(self, ctx):
        """Remux MP4 input to piped MKV on the fly, with cache-bounded seek.

        This path is used by the MP4 fallback tier (Tier 3 after faststart
        fails). Piped MKV has no Cues so Kodi's MKV demuxer can only do
        cache-bounded seek; duration is embedded in the MKV header so the
        progress bar is accurate. Large MKV sources take a different path
        entirely: they are routed through the HLS playlist/segment
        machinery (``mode="hls"``) rather than this handler.
        """
        total_bytes = ctx.get("total_bytes", 0)

        # Parse range request
        range_header = self.headers.get("Range")
        requested_start = 0
        if range_header:
            requested_start, _requested_end = self._parse_range(
                range_header, total_bytes or 1
            )
            if requested_start is None:
                self.send_error(416)
                return

        seek_seconds = self._resolve_seek(ctx, requested_start, total_bytes)
        proc, lock = self._start_remux_process(ctx, requested_start, seek_seconds)
        if proc is None:
            return

        # Drain stderr in a background thread to prevent ffmpeg from blocking
        # when the stderr pipe buffer fills up (~64KB).  Without this, ffmpeg
        # stalls mid-stream, the proxy stops sending data, and Kodi freezes
        # once its playback buffer drains.
        # Thread safety: list.append() is atomic under CPython's GIL, and
        # stderr_thread.join() in the finally block provides a happens-before
        # guarantee before the main thread reads stderr_chunks.
        stderr_chunks, stderr_thread = self._start_stderr_drain(proc)

        # Send response headers.
        # Matroska-only response. Piped MKV has no Cues so advertising
        # byte-range would only disable Kodi's cache-based fallback
        # without enabling real seek. Stay on live-stream semantics;
        # duration is still embedded in the MKV header so Kodi's
        # progress bar is accurate.
        self._send_close_response_headers(200, "video/x-matroska", "none")

        # Give the socket a write timeout.  If Kodi stops consuming bytes
        # without closing the TCP connection — which happens when Kodi's
        # decoder is stalled by a long operation like a DB vacuum and the
        # player enters limbo instead of firing onPlayBackStopped — the
        # socket send buffer fills up and wfile.write() would block forever.
        # A timeout here guarantees the loop eventually raises, runs the
        # finally block, and kills ffmpeg instead of leaving a zombie.
        try:
            self.connection.settimeout(_REMUX_WRITE_TIMEOUT)
        except (OSError, AttributeError):
            pass

        # Stream ffmpeg output to Kodi.  Duration is written into the MKV
        # header by ffmpeg via -metadata DURATION= (see _build_ffmpeg_cmd).
        total = 0
        try:
            total = self._stream_remux_output(ctx, proc, lock, requested_start)
        finally:
            self._finish_remux(ctx, proc, lock, stderr_chunks, stderr_thread, total)

    # ------------------------------------------------------------------
    # HLS playlist/segment handlers
    #
    # For the force-remux-huge-file path we expose the remuxed output as
    # an HLS VOD playlist (``/hls/<session>/playlist.m3u8``) with fixed-
    # duration MPEG-TS segments (``/hls/<session>/seg_<N>.ts``). Kodi's
    # HLS demuxer reads the ``#EXTINF`` values to compute the timeline
    # and translates a user seek into a segment request — no tail probe,
    # no in-file index needed, and each segment is an independent fresh
    # ffmpeg invocation with ``-ss <segment_start> -t <segment_length>``
    # so playback resumes correctly at any point in a multi-GB source.
    # This is the same pattern Plex/Jellyfin/Emby use for transcoded seek.
    # ------------------------------------------------------------------

    def _serve_hls_playlist(self, ctx):
        """Emit a VOD-type HLS playlist covering the full source duration.

        For fmp4 sessions, bumps EXT-X-VERSION to 7 (per ffmpeg HLS muxer
        recommendation for fMP4) and adds an EXT-X-MAP tag pointing at
        init.mp4. Segment URIs use the right extension for the session's
        segment_format (m4s vs ts), unpadded so they're readable in
        Kodi's logs — the URL parser absorbs leading zeros either way.
        """
        duration = ctx.get("duration_seconds") or 0.0
        seg_dur = ctx.get("hls_segment_duration", _HLS_SEGMENT_SECONDS)
        if duration <= 0 or seg_dur <= 0:
            self.send_error(500)
            return

        total_segs = int(math.ceil(duration / seg_dur))
        target = int(math.ceil(seg_dur))

        is_fmp4 = ctx.get("hls_segment_format") == "fmp4"
        seg_ext = "m4s" if is_fmp4 else "ts"
        version = "7" if is_fmp4 else "3"

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:{}".format(version),
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-TARGETDURATION:{}".format(target),
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        if is_fmp4:
            lines.append('#EXT-X-MAP:URI="init.mp4"')
        for i in range(total_segs):
            start = i * seg_dur
            remaining = max(0.0, duration - start)
            this_dur = min(seg_dur, remaining)
            lines.append("#EXTINF:{:.6f},".format(this_dur))
            lines.append("seg_{}.{}".format(i, seg_ext))
        lines.append("#EXT-X-ENDLIST")
        body = ("\n".join(lines) + "\n").encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_hls_init(self, ctx):
        """Serve the fMP4 init segment.

        Blocks on ``producer.wait_for_init()`` until the current
        ffmpeg generation has written init.mp4 AND produced its
        first segment (the ordering proof that init.mp4 is
        complete). Returns 504 on timeout, 500 if the producer is
        missing, 404 if the session is not fmp4.
        """
        producer = ctx.get("hls_producer")
        if producer is None:
            self.send_error(500)
            return
        if ctx.get("hls_segment_format") != "fmp4":
            self.send_error(404)
            return
        init_path = producer.wait_for_init()
        if init_path is None:
            xbmc.log("NZB-DAV: HLS init wait timed out", xbmc.LOGWARNING)
            self.send_error(504)
            return
        # Serve the canonical bytes cached in the producer, not whatever
        # is on disk at this moment. On a seek respawn ffmpeg rewrites
        # init.mp4 with a different edit list (the ``elst`` box entries
        # differ per seek position); the ``hvcC``/``mp4a`` codec config
        # is byte-identical, but HLS clients load the init segment once
        # and keep it cached, so Kodi would be playing later segments
        # against an ``elst`` that referenced a different base time.
        # The canonical-bytes cache guarantees every Kodi fetch returns
        # the first init's bytes regardless of respawn state — which
        # makes the init compatible with every segment the producer
        # emits.
        body = getattr(producer, "_canonical_init_bytes", None)
        if body is None:
            # Very early fetch: wait_for_init returned a path but the
            # cache hasn't been populated yet (shouldn't happen now
            # that wait_for_init populates it, but keep the disk-read
            # fallback for robustness).
            try:
                with open(init_path, "rb") as f:
                    body = f.read()
            except OSError as e:
                xbmc.log(
                    "NZB-DAV: HLS init read failed: {}".format(e),
                    xbmc.LOGERROR,
                )
                self.send_error(500)
                return

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.close_connection = True  # pylint: disable=attribute-defined-outside-init
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_hls_segment(self, ctx, seg_n):
        """Serve an HLS segment by reading from the session's on-disk
        segment file, produced by the persistent ffmpeg in
        ``HlsProducer``.

        The producer runs ONE ffmpeg per session using ffmpeg's
        segment muxer, so linear playback doesn't pay a cold-start
        per segment — ffmpeg keeps producing segments at ~5× real
        time as long as Kodi drains them. The only cold start is on
        session open and on seek.

        Seeks land here as a segment request whose index is far from
        the currently-producing segment; ``HlsProducer.wait_for_segment``
        detects that and kills/restarts ffmpeg at the new position.
        """
        producer = ctx.get("hls_producer")
        if producer is None:
            self.send_error(500)
            return
        duration = ctx.get("duration_seconds") or 0.0
        seg_dur = ctx.get("hls_segment_duration", _HLS_SEGMENT_SECONDS)
        if duration <= 0 or seg_dur <= 0:
            self.send_error(500)
            return
        start = seg_n * seg_dur
        if start >= duration:
            self.send_error(404)
            return
        this_dur = min(seg_dur, duration - start)

        segment_path = producer.wait_for_segment(seg_n)
        if segment_path is None:
            xbmc.log(
                "NZB-DAV: HLS seg {} wait timed out".format(seg_n),
                xbmc.LOGWARNING,
            )
            self.send_error(504)
            return

        # Open the segment file FIRST, then read its size via fstat(). A
        # previous version did getsize() → send_header → open(), which
        # opened a TOCTOU window: a respawn-driven unlink between
        # getsize() and open() would leave the handler advertising a
        # size that no longer exists. Holding the fd from the open
        # pins the underlying inode even if the dir entry is later
        # unlinked, so Content-Length stays in sync with what we read.
        try:
            seg_file = open(
                segment_path, "rb"
            )  # noqa: SIM115 — closed in finally below
        except OSError as e:
            xbmc.log(
                "NZB-DAV: HLS seg {} open failed: {}".format(seg_n, e),
                xbmc.LOGERROR,
            )
            self.send_error(500)
            return
        try:
            content_length = os.fstat(seg_file.fileno()).st_size
        except OSError as e:
            seg_file.close()
            xbmc.log(
                "NZB-DAV: HLS seg {} fstat failed: {}".format(seg_n, e),
                xbmc.LOGERROR,
            )
            self.send_error(500)
            return

        # Pick Content-Type based on session segment format so HEAD
        # and GET agree. fmp4 segments are video/mp4; legacy mpegts
        # segments are video/mp2t.
        seg_fmt = ctx.get("hls_segment_format", "mpegts")
        content_type = "video/mp4" if seg_fmt == "fmp4" else "video/mp2t"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        try:
            self.connection.settimeout(_REMUX_WRITE_TIMEOUT)
        except (OSError, AttributeError):
            pass

        total = 0
        try:
            with seg_file as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    total += len(chunk)
        except (BrokenPipeError, ConnectionResetError, _socket.timeout):
            xbmc.log(
                "NZB-DAV: HLS seg {} client disconnected after {} KB".format(
                    seg_n, total // 1024
                ),
                xbmc.LOGDEBUG,
            )
        except OSError as e:
            xbmc.log(
                "NZB-DAV: HLS seg {} read error: {}".format(seg_n, e),
                xbmc.LOGWARNING,
            )
        else:
            xbmc.log(
                "NZB-DAV: HLS seg {} done (start={:.1f}s dur={:.1f}s {} KB)".format(
                    seg_n, start, this_dur, total // 1024
                ),
                xbmc.LOGINFO,
            )

    @staticmethod
    def _build_hls_segment_cmd(ctx, start, duration):
        """Unused legacy helper preserved only to satisfy existing
        tests that assert the persistent producer's ffmpeg command
        shape (probesize, fastseek, -sn, etc.). The real command is
        now built by ``HlsProducer._build_cmd``.
        """
        ffmpeg = ctx["ffmpeg_path"]
        input_url = ctx["remote_url"]
        _validate_url(input_url)
        auth_args = _ffmpeg_auth_args(ctx.get("auth_header"))
        cmd = [
            ffmpeg,
            "-v",
            "warning",
            "-probesize",
            "1048576",
            "-analyzeduration",
            "0",
            "-fflags",
            "+fastseek",
            "-ss",
            "{:.3f}".format(start),
            "-t",
            "{:.3f}".format(duration),
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
        ]
        if auth_args:
            cmd.extend(auth_args)
        cmd.extend(
            [
                "-i",
                input_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-sn",
                "-f",
                "mpegts",
                "pipe:1",
            ]
        )
        return cmd

    def _serve_proxy(self, ctx):
        """Proxy range requests to remote with missing-article recovery.

        Missing or unfetchable usenet articles cause nzbdav to either 416 or
        hang mid-stream on the byte ranges that depend on them. Rather than
        killing playback with a black screen, this routine streams what
        upstream can serve, probes forward to locate a readable offset past
        the bad region, zero-fills the gap, and resumes. MKV/MP4 demuxers
        typically tolerate a few seconds of corrupted bytes as a brief
        playback glitch.
        """
        content_length = ctx["content_length"]
        range_header = self.headers.get("Range")

        if range_header:
            start, end = self._parse_range(range_header, content_length)
            if start is None:
                self.send_error(416)
                return
        else:
            start, end = 0, content_length - 1

        total_bytes = end - start + 1
        no_range_status = range_header is None and _send_200_no_range_enabled()
        self.send_response(200 if no_range_status else 206)
        self.send_header("Content-Type", ctx["content_type"])
        self.send_header("Content-Length", str(total_bytes))
        self.send_header("Accept-Ranges", "bytes")
        if not no_range_status:
            self.send_header(
                "Content-Range", "bytes {}-{}/{}".format(start, end, content_length)
            )
        # Force Connection: close on pass-through.  Kodi's CCurlFile opens a
        # fresh TCP connection on every seek / retry, so keep-alive provides
        # no benefit here.  But when Kodi reconnects after a CCurlFile error,
        # keep-alive left the OLD handler thread holding its upstream HTTP
        # response + multi-megabyte TCP buffers, doubling our memory footprint
        # and eventually triggering MemoryError in the second handler's 1 MB
        # chunk read.  Connection: close guarantees the previous handler
        # unwinds as soon as Kodi finishes reading its current range.
        #
        # The response header alone is advisory — BaseHTTPServer decides
        # close_connection based on the REQUEST's Connection header, not the
        # response's.  So we also set self.close_connection = True to
        # actually tear down the socket after handle() returns.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        # Write timeout so a stalled Kodi (DB vacuum, audio sync error, etc.)
        # can't block this handler in wfile.write() forever.  Without this,
        # a 14 s Kodi vacuum recreated the exact zombie pattern we fixed in
        # _serve_remux: first handler stuck writing into a full socket, Kodi
        # opens a second connection, two handlers + two upstream HTTP
        # responses live at once, MemoryError hits the second handler.
        try:
            self.connection.settimeout(_REMUX_WRITE_TIMEOUT)
        except (OSError, AttributeError):
            pass

        current = start
        total_streamed = 0
        total_skipped = 0
        recovery_count = 0
        terminal_reason = "complete"
        contract_mode = _get_strict_contract_mode()
        density_breaker_enabled = _density_breaker_enabled(contract_mode)
        zero_fill_budget_enabled = _zero_fill_budget_enabled()
        retry_ladder_enabled = _retry_ladder_enabled()
        density_window = deque()

        try:
            while current <= end:
                result, written = self._stream_upstream_range(
                    ctx, current, end, contract_mode=contract_mode
                )
                total_streamed += written
                _update_session_recovery_state(self.server, ctx, streamed=written)
                _record_density_window(density_window, "progress", written)
                current += written
                if current > end:
                    return

                if retry_ladder_enabled and result in (
                    _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
                    _UPSTREAM_RANGE_UPSTREAM_ERROR,
                ):
                    (
                        result,
                        retry_written,
                        current,
                    ) = self._retry_original_range(ctx, current, end, contract_mode)
                    total_streamed += retry_written
                    _update_session_recovery_state(
                        self.server, ctx, streamed=retry_written
                    )
                    _record_density_window(density_window, "progress", retry_written)
                    if current > end:
                        return

                remaining = end - current + 1
                skip = self._find_skip_offset(ctx, current, end)

                if skip is None or (
                    zero_fill_budget_enabled
                    and total_skipped + skip > _MAX_TOTAL_ZERO_FILL
                ):
                    terminal_reason = "recovery_exhausted"
                    xbmc.log(
                        "NZB-DAV: Zero-fill recovery exhausted at byte {} "
                        "(filling remaining {} bytes, reason={})".format(
                            current, remaining, terminal_reason
                        ),
                        xbmc.LOGERROR,
                    )
                    self._write_zeros(remaining)
                    total_skipped += remaining
                    return

                if density_breaker_enabled and _would_trip_density_breaker(
                    density_window, skip
                ):
                    terminal_reason = "density_breaker_tripped"
                    xbmc.log(
                        "NZB-DAV: Recovery density breaker tripped at byte {} "
                        "(result={}, skip={}, ratio={:.2f}, reason={})".format(
                            current,
                            result,
                            skip,
                            _density_ratio(density_window),
                            terminal_reason,
                        ),
                        xbmc.LOGWARNING,
                    )
                    try:
                        _notify(
                            "NZB-DAV",
                            "Stream aborted after repeated zero-fill recovery",
                        )
                    except (RuntimeError, OSError):
                        pass
                    return

                projected_zero_fill = None
                projected_recoveries = None
                projected_ratio = None
                if zero_fill_budget_enabled:
                    (
                        projected_zero_fill,
                        projected_recoveries,
                        projected_ratio,
                    ) = _project_session_zero_fill_ratio(
                        self.server, ctx, extra_zero_fill=skip, extra_recoveries=1
                    )
                    if projected_ratio > _SESSION_ZERO_FILL_RATIO_MAX:
                        terminal_reason = "session_zero_fill_budget_exceeded"
                        xbmc.log(
                            "NZB-DAV: Session zero-fill budget exceeded at byte {} "
                            "(projected_ratio={:.3f}, skipped={}, recoveries={}, "
                            "reason={})".format(
                                current,
                                projected_ratio,
                                projected_zero_fill,
                                projected_recoveries,
                                terminal_reason,
                            ),
                            xbmc.LOGWARNING,
                        )
                        _maybe_notify_recovery_summary(
                            self.server,
                            ctx,
                            zero_fill_bytes=projected_zero_fill,
                            recovery_count=projected_recoveries,
                        )
                        return

                self._write_zeros(skip)
                total_skipped += skip
                recovery_count += 1
                current += skip
                _record_density_window(density_window, "zero_fill", skip)
                _update_session_recovery_state(
                    self.server, ctx, zero_fill=skip, recoveries=1
                )
                _maybe_notify_recovery_summary(self.server, ctx)
                xbmc.log(
                    "NZB-DAV: Zero-filled {} bytes at offset {} to skip bad "
                    "usenet articles (reason=zero_fill_resume)".format(
                        skip, current - skip
                    ),
                    xbmc.LOGWARNING,
                )
        except (BrokenPipeError, ConnectionResetError, _socket.timeout):
            terminal_reason = "client_disconnected"
            # socket.timeout means Kodi stopped reading from us for longer
            # than _REMUX_WRITE_TIMEOUT — usually a long DB vacuum or the
            # decoder otherwise stalling.  Unwind the handler and let
            # BaseHTTPServer tear down the socket; Kodi's CCurlFile will
            # reconnect if it still wants bytes.
            xbmc.log(
                "NZB-DAV: Pass-through write aborted at byte {} "
                "(client stalled or disconnected, reason={})".format(
                    current, terminal_reason
                ),
                xbmc.LOGWARNING,
            )
        finally:
            xbmc.log(
                "NZB-DAV: Pass-through summary reason={} range={}-{} "
                "streamed={} zero_fill={} recoveries={}".format(
                    terminal_reason,
                    start,
                    end,
                    total_streamed,
                    total_skipped,
                    recovery_count,
                ),
                xbmc.LOGINFO if terminal_reason == "complete" else xbmc.LOGWARNING,
            )

    def _retry_original_range(self, ctx, start, end, contract_mode):
        """Retry the still-unread upstream range before falling back to skip."""
        current = start
        total_written = 0
        last_result = _UPSTREAM_RANGE_UPSTREAM_ERROR

        for delay in _RANGE_RETRY_DELAYS:
            time.sleep(delay)
            result, written = self._stream_upstream_range(
                ctx, current, end, contract_mode=contract_mode
            )
            total_written += written
            current += written
            last_result = result
            if current > end:
                return result, total_written, current
            if result not in (
                _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
                _UPSTREAM_RANGE_UPSTREAM_ERROR,
            ):
                return result, total_written, current

        return last_result, total_written, current

    def _stream_upstream_range(self, ctx, start, end, contract_mode=None):
        """Stream bytes from upstream to the client.

        Returns ``(result_enum, written_bytes)`` where ``result_enum`` is one
        of OK / SHORT_READ_RECOVERABLE / PROTOCOL_MISMATCH / UPSTREAM_ERROR.
        BrokenPipeError / ConnectionResetError propagate out so the caller can
        abort cleanly.
        """
        req = Request(ctx["remote_url"])
        req.add_header("Range", "bytes={}-{}".format(start, end))
        if ctx.get("auth_header"):
            req.add_header("Authorization", ctx["auth_header"])

        contract_mode = contract_mode or _get_strict_contract_mode()
        requested = end - start + 1
        written = 0
        try:
            # nosemgrep
            resp = (
                urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
                    req, timeout=_UPSTREAM_OPEN_TIMEOUT
                )
            )
        except (OSError, ValueError) as e:
            xbmc.log(
                "NZB-DAV: Proxy upstream open failed at byte {}: {} "
                "(reason=upstream_open_failed)".format(start, e),
                xbmc.LOGWARNING,
            )
            return _UPSTREAM_RANGE_UPSTREAM_ERROR, 0

        try:
            status = getattr(resp, "status", None) or resp.getcode()
            content_range = _get_header(resp, "Content-Range")
            content_length = _get_header(resp, "Content-Length")
            mismatch_detail = None
            hard_mismatch = False

            if contract_mode != _STRICT_CONTRACT_MODE_OFF:
                mismatch_detail, hard_mismatch = _classify_contract_mismatch(
                    status,
                    content_range,
                    content_length,
                    start,
                    end,
                    ctx["content_length"],
                )
                if mismatch_detail:
                    _log_contract_mismatch(
                        start,
                        end,
                        status,
                        content_range,
                        content_length,
                        mismatch_detail,
                    )
                    if contract_mode == _STRICT_CONTRACT_MODE_ENFORCE or hard_mismatch:
                        return _UPSTREAM_RANGE_PROTOCOL_MISMATCH, 0

            while True:
                try:
                    # 64 KB chunks — on 32-bit Kodi the whole process has
                    # ~3 GB of address space, and Kodi's CFileCache alone can
                    # reserve up to 1.5 GB (cachemembuffersize * readbufferfactor).
                    # A 1 MB read buffer used to hit MemoryError when a second
                    # connection opened during recovery doubled the proxy's
                    # live allocations. 64 KB matches the zero-fill buffer
                    # size and is allocation-friendly on a fragmented heap.
                    chunk = resp.read(_UPSTREAM_READ_CHUNK)
                except (MemoryError, OSError, ValueError) as e:
                    xbmc.log(
                        "NZB-DAV: Proxy upstream read failed at byte {}: {}".format(
                            start + written, e
                        )
                        + " (reason=upstream_read_failed)",
                        xbmc.LOGWARNING,
                    )
                    if written:
                        xbmc.log(
                            "NZB-DAV: Upstream short read for {}-{} wrote={} "
                            "status={} Content-Range={!r} Content-Length={!r} "
                            "(reason=short_read_recoverable)".format(
                                start,
                                end,
                                written,
                                status,
                                content_range,
                                content_length,
                            ),
                            xbmc.LOGWARNING,
                        )
                        return _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, written
                    return _UPSTREAM_RANGE_UPSTREAM_ERROR, 0
                if not chunk:
                    if written == requested:
                        if mismatch_detail:
                            return _UPSTREAM_RANGE_PROTOCOL_MISMATCH, written
                        return _UPSTREAM_RANGE_OK, written
                    xbmc.log(
                        "NZB-DAV: Upstream short read for {}-{} wrote={} "
                        "expected={} status={} Content-Range={!r} "
                        "Content-Length={!r} (reason=short_read_recoverable)".format(
                            start,
                            end,
                            written,
                            requested,
                            status,
                            content_range,
                            content_length,
                        ),
                        xbmc.LOGWARNING,
                    )
                    return _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, written
                remaining = requested - written
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                    mismatch_detail = mismatch_detail or "read beyond requested range"
                    if contract_mode != _STRICT_CONTRACT_MODE_OFF:
                        _log_contract_mismatch(
                            start,
                            end,
                            status,
                            content_range,
                            content_length,
                            mismatch_detail,
                        )
                self.wfile.write(chunk)
                written += len(chunk)
        finally:
            try:
                resp.close()
            except OSError:
                pass

    @staticmethod
    def _find_skip_offset(ctx, failed_byte, range_end):
        """Probe forward to find a skip size past a bad article region.

        Tries progressively larger skips and confirms upstream can serve a
        small range starting at the new offset. Each skip size is retried
        with backoff so a briefly-unavailable upstream (restart, transient
        network blip) has a chance to come back before we declare the
        region unrecoverable. Returns the skip in bytes or None if the
        recovery budget is exhausted.
        """
        start_time = time.time()
        for skip in _SKIP_PROBE_SIZES:
            target = failed_byte + skip
            if target > range_end:
                return None
            probe_end = min(target + 1023, range_end)

            delays = (0,) + _PROBE_RETRY_DELAYS
            for delay in delays:
                if time.time() - start_time >= _MAX_RECOVERY_SECONDS:
                    return None
                if delay:
                    time.sleep(delay)
                req = Request(ctx["remote_url"])
                req.add_header("Range", "bytes={}-{}".format(target, probe_end))
                if ctx.get("auth_header"):
                    req.add_header("Authorization", ctx["auth_header"])
                try:
                    # nosemgrep
                    with urlopen(  # nosec B310 — URL from user-configured stream
                        req, timeout=_SKIP_PROBE_TIMEOUT
                    ) as resp:
                        status = getattr(resp, "status", None) or resp.getcode()
                        if status in (200, 206):
                            # Validate the probe actually returned bytes —
                            # an upstream that 206s with an empty body would
                            # otherwise be accepted as recovered, sending
                            # the main loop straight back into the same
                            # bad region on the next range read.
                            body = resp.read(64)
                            if not body:
                                xbmc.log(
                                    "NZB-DAV: Probe at +{} bytes returned "
                                    "status={} but empty body; treating as "
                                    "probe failure".format(skip, status),
                                    xbmc.LOGWARNING,
                                )
                                continue
                            elapsed = time.time() - start_time
                            xbmc.log(
                                "NZB-DAV: Probe succeeded at +{} bytes after "
                                "{:.1f}s".format(skip, elapsed),
                                xbmc.LOGINFO,
                            )
                            return skip
                except (OSError, ValueError) as e:
                    xbmc.log(
                        "NZB-DAV: Probe at +{} bytes failed ({}): {}".format(
                            skip, type(e).__name__, e
                        ),
                        xbmc.LOGDEBUG,
                    )
                    continue
        return None

    def _write_zeros(self, count):
        """Write 'count' zero bytes to the client in fixed-size chunks."""
        remaining = count
        while remaining > 0:
            chunk_size = min(remaining, len(_ZERO_FILL_BUFFER))
            self.wfile.write(_ZERO_FILL_BUFFER[:chunk_size])
            remaining -= chunk_size

    @staticmethod
    def _parse_range(range_header, content_length):
        """Parse Range header, return (start, end) or (None, None)."""
        try:
            if content_length <= 0:
                return None, None
            if not isinstance(range_header, str) or not range_header.startswith(
                "bytes="
            ):
                return None, None
            range_spec = range_header[len("bytes=") :].strip()
            if "," in range_spec or "-" not in range_spec:
                return None, None
            if range_spec.startswith("-"):
                suffix = int(range_spec[1:])
                if suffix <= 0 or suffix > content_length:
                    return None, None
                return content_length - suffix, content_length - 1
            start_text, end_text = range_spec.split("-", 1)
            if not start_text:
                return None, None
            start = int(start_text)
            if start < 0 or start >= content_length:
                return None, None
            end = int(end_text) if end_text else content_length - 1
            if end < start:
                return None, None
            return start, min(end, content_length - 1)
        except (ValueError, IndexError):
            return None, None


class _ThreadedHTTPServer(_ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.stream_context = None
        self.stream_sessions = {}
        self.active_ffmpeg = None
        self.current_byte_pos = 0
        self.ffmpeg_lock = threading.Lock()
        self.owner_proxy = None
        super().__init__(*args, **kwargs)


class HlsProducer:
    """Persistent ffmpeg + disk-backed HLS segment producer for a
    single session.

    The original per-segment approach (one ffmpeg cold start per
    segment request) made Kodi cache constantly: each segment paid
    ~10-15 s of container parsing against a remote 58 GB MKV, which
    is longer than the 30 s segment duration, so Kodi's HLS demuxer
    ran out of buffered data every time. The fix is to keep one
    ffmpeg running using the ``segment`` muxer, writing
    ``seg_000000.ts`` files directly to a session directory on disk.
    Kodi's segment requests become simple file reads — no cold start
    between consecutive segments, just once per seek.

    Seeks are handled by killing the current ffmpeg and restarting
    with ``-ss <target>`` and ``-segment_start_number <seg_n>`` so
    the new ffmpeg writes ``seg_%06d.ts`` files at the right index.
    Backward seeks to an already-produced segment just read the
    existing file without restarting ffmpeg at all.

    Thread safety: mutation of the ffmpeg process pointer and
    ``start_segment`` is guarded by ``_lock``. Segment file reads
    are stateless and don't need locking.
    """

    def __init__(self, ctx, base_workdir):
        self.ctx = ctx
        self.remote_url = ctx["remote_url"]
        self.auth_header = ctx.get("auth_header")
        self.ffmpeg_path = ctx["ffmpeg_path"]
        self.duration_seconds = float(ctx["duration_seconds"])
        self.segment_seconds = float(
            ctx.get("hls_segment_duration", _HLS_SEGMENT_SECONDS)
        )
        self.total_segments = int(
            math.ceil(self.duration_seconds / self.segment_seconds)
        )
        self.segment_format = ctx.get("hls_segment_format", "mpegts")
        self.session_dir = os.path.join(base_workdir, ctx["session_id"])
        os.makedirs(self.session_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._proc = None
        self._start_segment = 0  # -segment_start_number of the live ffmpeg
        self._closed = False
        self._spawn_time = 0.0  # time.time() of the most recent ffmpeg spawn
        # _init_ready MUST be set here, not only in the spawn path:
        # wait_for_init reads it before the first spawn and would
        # AttributeError on a fresh session otherwise.
        self._init_ready = False
        # Canonical init segment bytes. Populated the first time
        # wait_for_init observes a complete init.mp4 on disk. After
        # that, ``_serve_hls_init`` returns these bytes for every
        # Kodi request, ignoring whatever ffmpeg writes to the disk
        # file on subsequent generations. Rationale: on a seek
        # respawn, ffmpeg produces a new init.mp4 with a different
        # edit list (``elst`` box) — the codec config (``hvcC``,
        # ``mp4a``) is byte-identical, so from a decoder
        # compatibility standpoint the first init works for every
        # generation. But HLS fmp4 clients only load ``EXT-X-MAP``
        # once per playlist, so Kodi has already cached the first
        # init's bytes. Serving a different init on a later request
        # — or worse, letting Kodi re-parse a half-written disk
        # file mid-respawn — would be either a no-op (if Kodi
        # ignores the second fetch) or a decoder stall (if it
        # accepts it). Caching the bytes here makes the behavior
        # deterministic regardless of what Kodi does.
        self._canonical_init_bytes = None
        # Session-wide stderr log. Opened once at session construction,
        # reused across every ffmpeg spawn (fixing the stderr=PIPE
        # deadlock from the persistent-producer era), closed in close().
        # Binary append + unbuffered so a caller can tail the file live
        # during a stall.
        self._ffmpeg_log_path = os.path.join(self.session_dir, "ffmpeg.log")
        self._ffmpeg_log = open(  # noqa: SIM115 — closed in close()
            self._ffmpeg_log_path, "ab", buffering=0
        )

    def segment_path(self, seg_n):
        """Return the disk path for a segment index, with the extension
        determined by this producer's segment_format."""
        ext = "m4s" if self.segment_format == "fmp4" else "ts"
        return os.path.join(self.session_dir, "seg_{:06d}.{}".format(seg_n, ext))

    def _segment_complete(self, seg_n):
        """True if seg_n.ts exists and is no longer being written.

        Completion is detected by either: the next segment file also
        exists (ffmpeg has moved on), or the file's mtime has been
        stable for more than _HLS_SEGMENT_MTIME_STABLE_MS.

        For fMP4, the "next segment exists" signal is only trusted
        if the next segment was created after the current ffmpeg
        spawn — otherwise a stale seg_n+1 from a prior generation
        can make this return True while the new seg_n is still
        being written.
        """
        path = self.segment_path(seg_n)
        if not os.path.exists(path):
            return False
        next_path = self.segment_path(seg_n + 1)
        if os.path.exists(next_path):
            # In fMP4 mode, verify the next segment belongs to the
            # current generation (created after the latest spawn).
            if self.segment_format == "fmp4":
                try:
                    next_mtime = os.path.getmtime(next_path)
                except OSError:
                    pass
                else:
                    if next_mtime < self._spawn_time:
                        # Stale segment from a prior generation —
                        # ignore it and fall through to mtime check.
                        pass
                    else:
                        return True
            else:
                return True
        # Final segment (or ffmpeg briefly mid-transition) — fall back
        # to mtime stability.
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        # In fMP4 mode, also require that THIS segment was written by
        # the current ffmpeg generation. Without this guard, a backward
        # seek can read a stale ``seg_n.m4s`` from a prior generation
        # whose mtime is far in the past — the mtime-stability check
        # is trivially true for such a file, and ``_segment_complete``
        # would return True. The bytes are technically valid but they
        # were produced against a different edit list / timestamp
        # base than the current generation's segments, so Kodi's HLS
        # demuxer either glitches or stalls when it tries to splice
        # them. The "next segment exists" branch above already has
        # this guard; this is the matching guard for the mtime path.
        if self.segment_format == "fmp4" and mtime < self._spawn_time:
            return False
        if (time.time() - mtime) * 1000.0 > _HLS_SEGMENT_MTIME_STABLE_MS:
            return True
        # If this is the terminal segment (no N+1 will ever exist),
        # ffmpeg should have exited by now.
        if seg_n >= self.total_segments - 1:
            with self._lock:
                proc = self._proc
            if proc is not None and proc.poll() is not None:
                return True
        return False

    def _init_file_complete(self):
        """True iff init.mp4 was written by the current ffmpeg
        generation AND ffmpeg has moved on to segment output.

        Generation boundary: _ensure_ffmpeg_headed_for unlinks
        BOTH init.mp4 AND seg_<new_target>.m4s before every
        spawn. So any init.mp4 on disk post-spawn is from the
        current generation, and any seg_<start_segment>.m4s on
        disk post-spawn was written by the current ffmpeg too
        (a prior generation cannot have produced a file we just
        unlinked).

        The "seg_<start_segment>.m4s exists" signal proves ffmpeg
        has finished the init box — the fMP4 HLS muxer writes
        init.mp4 fully before opening any segment file.
        """
        if self.segment_format != "fmp4":
            return False
        init_path = os.path.join(self.session_dir, "init.mp4")
        if not os.path.exists(init_path):
            return False
        # Deliberately reading self._start_segment WITHOUT self._lock.
        #
        # Why it's safe today:
        #   * CPython stores Python ints as PyObject*; assignment is a
        #     single pointer store and reads of that pointer are atomic
        #     under the GIL. A reader never sees a half-written int.
        #   * The caller (``wait_for_init`` / poll loop) tolerates a
        #     stale read: if ``_start_segment`` has just advanced, the
        #     stale value points at a segment path that already exists
        #     on disk (the previous target) — returning True early is
        #     correct because init.mp4 is complete in both generations.
        #     If we read the stale value and return False, the next
        #     poll cycle (~50 ms later) reads the fresh value.
        #   * Holding self._lock here would serialize the polling reader
        #     against the respawn writer and defeat the purpose of the
        #     fast-path existence check.
        #
        # Why future refactors should revisit this:
        #   * If this module ever runs under a no-GIL interpreter (PEP
        #     703) or switches to asyncio with thread-pool executors,
        #     the "atomic int read" assumption weakens.
        #   * If ``_start_segment`` ever grows into a tuple / object
        #     (e.g. (generation_id, seg_n)), the read is no longer
        #     atomic and a reader can see a torn value.
        #   * Drop-in mitigation when that day comes: replace the bare
        #     int with a ``threading.Event`` that the respawn path
        #     sets() after publishing the new ``_start_segment``, and
        #     have this method wait() on the event before reading.
        first_seg_path = os.path.join(
            self.session_dir,
            "seg_{:06d}.m4s".format(self._start_segment),
        )
        return os.path.exists(first_seg_path)

    def wait_for_init(self, timeout=_HLS_SEGMENT_WAIT_SECONDS):
        """Block until init.mp4 for the current producer generation
        exists and seg_<start_segment>.m4s proves ffmpeg moved past
        the init write phase. Returns the init path on success or
        None on timeout.

        CRITICAL A: this method must actively spawn ffmpeg if none
        is running. Kodi typically fetches #EXT-X-MAP BEFORE any
        segment, so a poll-only implementation would deadlock on
        the very first request.

        CRITICAL B: if ffmpeg IS running (e.g. Kodi re-fetches the
        init after a forward seek to seg 40), this method must NOT
        rewind the producer back to seg 0. Any running ffmpeg is
        left at its current _start_segment target.
        """
        if self.segment_format != "fmp4":
            return None
        init_path = os.path.join(self.session_dir, "init.mp4")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._closed:
                return None
            # Fast path: files already on disk for the current
            # generation. The on-disk check IS the truth-source —
            # _init_ready is just a redundant cached flag we set
            # below for any downstream consumer that wants to skip
            # the file syscall on subsequent calls.
            if self._init_file_complete():
                self._init_ready = True
                # Cache the first init.mp4 we see so later requests
                # (and respawn generations with different edit lists)
                # serve byte-identical data. See the docstring on
                # self._canonical_init_bytes for the full rationale.
                if self._canonical_init_bytes is None:
                    try:
                        with open(init_path, "rb") as f:
                            self._canonical_init_bytes = f.read()
                        xbmc.log(
                            "NZB-DAV: Cached canonical init.mp4 "
                            "({} bytes) for session".format(
                                len(self._canonical_init_bytes)
                            ),
                            xbmc.LOGINFO,
                        )
                    except OSError as e:
                        xbmc.log(
                            "NZB-DAV: Failed to cache canonical "
                            "init.mp4: {}".format(e),
                            xbmc.LOGWARNING,
                        )
                return init_path
            with self._lock:
                proc = self._proc
                alive = proc is not None and proc.poll() is None
                current_target = self._start_segment
            if not alive:
                # No live ffmpeg — bootstrap (fresh session: target
                # defaults to 0) or respawn at whatever target the
                # last generation had. DO NOT hardcode 0 here; a
                # crashed mid-seek producer still has the right
                # start_segment to resume at.
                self._ensure_ffmpeg_headed_for(current_target)
            # If ffmpeg is alive, leave it alone — it's either
            # already headed toward the right segment, or the init
            # re-fetch is racing a valid seek that's already
            # produced init.mp4 once and will produce it again
            # after the seek-restart cleans up.
            if self._init_file_complete():
                self._init_ready = True
                return init_path
            time.sleep(0.25)
        return None

    def wait_for_segment(self, seg_n, timeout=_HLS_SEGMENT_WAIT_SECONDS):
        """Block until seg_n is complete on disk, or timeout expires.

        If ffmpeg is either not running or running in a position that
        will never produce seg_n, kicks off a restart aimed at seg_n.
        Returns the segment file path on success, or None on timeout.

        For fmp4 producers, the loop additionally gates on
        _init_file_complete so a seg_n read can't race a
        still-being-written init.mp4.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._closed:
                return None
            # fmp4 init gate: seg_n cannot be served until the current
            # generation's init is on disk AND ffmpeg has moved past
            # the init write phase. For segment requests we DO want to
            # head toward seg_n specifically — the caller is asking for
            # a specific segment, so the "seg_n < start_segment"
            # restart behavior in _ensure_ffmpeg_headed_for is the
            # right call (unlike wait_for_init, which preserves the
            # current generation).
            if self.segment_format == "fmp4" and not self._init_ready:
                if self._init_file_complete():
                    self._init_ready = True
                else:
                    self._ensure_ffmpeg_headed_for(seg_n)
                    time.sleep(0.25)
                    continue
            if self._segment_complete(seg_n):
                return self.segment_path(seg_n)
            # Do we need to (re)start ffmpeg to eventually reach seg_n?
            self._ensure_ffmpeg_headed_for(seg_n)
            time.sleep(0.25)
        return None

    def _ensure_ffmpeg_headed_for(self, seg_n):
        """Start or restart ffmpeg so that it will produce seg_n.

        If ffmpeg is already running and its start segment is <= seg_n
        (i.e. the live process will eventually reach this segment as
        it streams forward), do nothing.

        Otherwise — ffmpeg is dead, or started at a segment index
        greater than seg_n (seek backward), or far before seg_n (seek
        far forward) — kill the current ffmpeg and start a new one
        whose ``-ss`` matches seg_n.
        """
        with self._lock:
            if self._closed:
                return
            proc = self._proc
            proc_alive = proc is not None and proc.poll() is None
            need_restart = False
            if not proc_alive:
                need_restart = True
            else:
                # We want ffmpeg's live production segments to eventually
                # include seg_n. ffmpeg only produces segments >=
                # start_segment in sequence; a request for a segment
                # before that means the user seeked backward.
                if seg_n < self._start_segment:
                    need_restart = True
                elif seg_n - self._start_segment > 60:
                    # Very far forward: a 30-minute jump while ffmpeg is
                    # near the beginning. Cheaper to restart at the
                    # target than to stream through.
                    need_restart = True

            if not need_restart:
                return

            # Stop the old ffmpeg if any.
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # SIGKILL was sent but the child didn't reap within 5 s
                    # (uninterruptible I/O or a truly stuck process). The
                    # OS will reap it eventually; log so the leak is
                    # observable instead of silent.
                    xbmc.log(
                        "NZB-DAV: HLS ffmpeg pid={} did not exit 5 s after kill; "
                        "leaking for the OS to reap".format(getattr(proc, "pid", "?")),
                        xbmc.LOGWARNING,
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
            self._proc = None

            # fmp4 generation boundary: unlink the new target segment
            # file so the "seg_<start_segment>.m4s exists"
            # completeness signal in _init_file_complete is
            # unambiguously bound to the NEW ffmpeg. Do NOT blanket-
            # sweep other segments — leaving prior-generation files
            # in place preserves the backward-seek cache optimization
            # in _segment_complete. Do NOT unlink init.mp4 either:
            # the canonical bytes cache in _canonical_init_bytes
            # already committed to serving the first generation's
            # init to every Kodi request, so whatever new ffmpeg
            # writes to the on-disk init.mp4 is irrelevant. Unlinking
            # would just race the on-disk overwrite and momentarily
            # fail _init_file_complete for no gain.
            if self.segment_format == "fmp4":
                first_seg_path = os.path.join(
                    self.session_dir, "seg_{:06d}.m4s".format(seg_n)
                )
                try:
                    os.unlink(first_seg_path)
                except FileNotFoundError:
                    pass
                # Reset _init_ready so wait_for_init/wait_for_segment
                # re-verify the generation boundary (checks that
                # seg_<new_target>.m4s exists post-spawn) — but the
                # canonical init bytes persist across the reset.
                self._init_ready = False

            # Start a new one aimed at seg_n.
            start_time = seg_n * self.segment_seconds
            cmd = self._build_cmd(start_time, seg_n)
            xbmc.log(
                "NZB-DAV: HLS producer starting ffmpeg at seg {} (t={:.1f}s)".format(
                    seg_n, start_time
                ),
                xbmc.LOGINFO,
            )
            try:
                # Set _spawn_time + _start_segment BEFORE Popen so a
                # concurrent _segment_complete() can't observe a stale
                # _spawn_time of 0 between the Popen return and the
                # assignment (which would accept a freshly-unlinked
                # segment from the previous generation as complete).
                # time.time() is a few ns; the tiny skew where
                # _spawn_time is slightly before the actual spawn is
                # harmless for the stale-segment guard.
                self._start_segment = seg_n
                self._spawn_time = time.time()
                # If close() previously ran and closed self._ffmpeg_log,
                # or any other caller closed it, reopen it before spawning
                # so the new ffmpeg doesn't inherit a closed fd and swallow
                # all its stderr into OSError on the first write.
                if self._ffmpeg_log.closed:
                    self._ffmpeg_log = open(  # noqa: SIM115 — closed in close()
                        self._ffmpeg_log_path, "ab", buffering=0
                    )
                # cwd=session_dir is REQUIRED for fmp4 mode: ffmpeg
                # 6.0.1 on CoreELEC rejects absolute paths for
                # ``-hls_fmp4_init_filename``, so _build_cmd passes
                # relative filenames (init.mp4, seg_%06d.m4s,
                # ffmpeg_playlist.m3u8) and relies on the process cwd
                # to place them in the session directory. mpegts mode
                # still passes absolute segment paths and tolerates
                # either cwd, so setting cwd unconditionally is safe.
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=self._ffmpeg_log,
                    shell=False,
                    cwd=self.session_dir,
                )
            except OSError as e:
                xbmc.log(
                    "NZB-DAV: HLS producer ffmpeg spawn failed: {}".format(e),
                    xbmc.LOGERROR,
                )
                self._proc = None

    def _build_cmd(self, start_time, start_segment):
        """Build the persistent-ffmpeg command.

        Two output shapes, driven by self.segment_format:

        - "mpegts" (default, legacy): ``-f segment -segment_format mpegts``
          writes ``seg_%06d.ts`` directly via ffmpeg's segment muxer.
        - "fmp4" (new): ``-f hls -hls_segment_type fmp4`` writes
          ``init.mp4`` (once per process start) plus ``seg_%06d.m4s``
          fragments. This is the DV-capable branch — DV RPU SEI NALs
          survive fmp4 fragment boundaries (vs mpegts PES packetization,
          which breaks them).

        Filename padding: both branches use ``seg_%06d.<ext>`` so the
        existing producer tests that construct segment files by name
        (``seg_000005.ts``, etc.) continue to work, and the URL parser's
        ``int()`` coercion absorbs leading zeros either way.

        Timestamp handling: ``-copyts`` is set so each output frame
        keeps the source PTS. No ``-reset_timestamps`` — an earlier
        attempt used ``-reset_timestamps 1`` to normalize each
        segment's PTS to near-zero, but Kodi's Amlogic HW decoder
        interpreted the repeated near-zero PTS values as
        non-monotonic, flagged ``messy timestamps``, and eventually
        emitted a continuous stream of ``CAMLCodec::GetPicture:
        decoder timeout - elf:[5021ms]`` errors until playback froze
        (seen on the 2026-04-13 Shawshank test run). With ``-copyts``
        and default timestamp continuity, a single running ffmpeg
        emits seg 0 at PTS 0-30, seg 1 at PTS 30-60, ... — perfectly
        monotonic. On seek-restart, the new ffmpeg's ``-ss T`` gives
        first-frame PTS near T, matching Kodi's EXTINF-based global
        time at ``seg_T/segment_seconds``. The per-segment keyframe-
        snap overlap that bit us with the earlier fresh-ffmpeg-per-
        segment design doesn't apply here: adjacent segments come
        from the SAME ffmpeg process in the persistent model, so
        only the seek boundary has any chance of overlap — and at a
        seek Kodi expects a discontinuity anyway.
        """
        _validate_url(self.remote_url)
        # Pass auth via -headers (not URL-embedded) so credentials
        # don't leak into argv / ffmpeg.log / error messages. See
        # _ffmpeg_auth_args for the rationale.
        input_url = self.remote_url
        auth_args = _ffmpeg_auth_args(self.auth_header)

        # -probesize / -analyzeduration: ffmpeg needs to read enough
        # input bytes AND enough media duration to determine codec
        # parameters before muxing starts. The original (1 MB / 0)
        # skipped analysis entirely, which broke audio frame-size
        # detection: ffmpeg logged "track N: codec frame size is
        # not set" and the mp4 muxer fell back to a default
        # per-packet duration that didn't match reality, producing
        # AV desync on DTS/TrueHD AND outright "no audio" on
        # E-AC-3 (DDP) sources.
        #
        # The first bump to 5 MB / 2 s helped DTS slightly but
        # didn't catch E-AC-3 in a sparsely-interleaved MKV — 2 s
        # of media time covers only a handful of audio packets in
        # a 4K REMUX where audio is interleaved between large
        # video keyframes. Bumping to 50 MB / 15 s gives ffmpeg a
        # comfortable margin to read dozens of audio packets and
        # determine the codec frame size for any practical source.
        # Costs ~3-5 s of extra startup latency on first spawn
        # (and on every seek respawn) — the playback-never-started
        # watchdog in service.py was raised to 30 s for exactly
        # this reason.
        cmd = [
            self.ffmpeg_path,
            "-v",
            "warning",
            "-probesize",
            "52428800",
            "-analyzeduration",
            "15000000",
            "-fflags",
            "+fastseek",
            "-ss",
            "{:.3f}".format(start_time),
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
        ]
        # Auth headers MUST come before -i so they apply to the input.
        cmd.extend(auth_args)
        cmd.extend(
            [
                "-i",
                input_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-sn",
                "-copyts",
            ]
        )

        if self.segment_format == "fmp4":
            # IMPORTANT: fmp4 arguments must be RELATIVE filenames, not
            # absolute paths. ffmpeg 6.0.1 on CoreELEC fails on absolute
            # paths for ``-hls_fmp4_init_filename`` with "Failed to open
            # segment <path>: No such file or directory", even when the
            # parent directory exists and is writable. Relative names
            # work reliably when ffmpeg is spawned with cwd set to the
            # session dir (see ``_ensure_ffmpeg_headed_for``'s ``Popen``
            # call). Reproduced 2026-04-14 on a 48 GB DV HEVC REMUX
            # and a 27 GB AVC REMUX; both failed with absolute paths,
            # both succeeded with relative.
            init_path = "init.mp4"
            seg_pattern = "seg_%06d.m4s"
            playlist_path = "ffmpeg_playlist.m3u8"
            # -strict -2 (== -strict experimental) unlocks TrueHD and
            # DTS-HD MA output in the MP4/fMP4 muxer. ffmpeg 6.0.1
            # otherwise refuses with "truehd in MP4 support is
            # experimental, add '-strict -2' if you want to use it"
            # / "dts in MP4 support is experimental, ..." and fails
            # to write the init header at all. Virtually every UHD
            # REMUX uses one of those codecs, so without this flag
            # the fmp4 HLS path never produces a playable output
            # on real content. Verified 2026-04-14 against The
            # Machinist (TrueHD) — failed without -strict, succeeded
            # with it.
            cmd.extend(["-strict", "-2"])
            # Force the HLS-spec sample entry tag on the video track.
            # fMP4 HLS mandates ``hvc1`` for HEVC (parameter sets in the
            # sample description box, not inband), and Amlogic's HLS
            # demuxer looks at ``hvc1``/``hev1`` to decide whether to
            # inspect the ``dvcC``/``dvvC`` DV configuration records in
            # the init segment. ``-tag:v hvc1`` is a metadata swap,
            # not a re-encode; ffmpeg pulls SPS/PPS/VPS into ``hvcC``
            # at the muxer and leaves the bitstream otherwise
            # untouched.
            cmd.extend(["-tag:v", "hvc1"])
            cmd.extend(
                [
                    "-f",
                    "hls",
                    "-hls_time",
                    "{:.3f}".format(self.segment_seconds),
                    "-hls_segment_type",
                    "fmp4",
                    "-hls_fmp4_init_filename",
                    init_path,
                    "-hls_segment_filename",
                    seg_pattern,
                    "-hls_playlist_type",
                    "vod",
                    "-hls_flags",
                    "independent_segments",
                    "-start_number",
                    str(start_segment),
                    playlist_path,
                ]
            )
            return cmd

        # mpegts branch — unchanged filename pattern.
        seg_pattern = os.path.join(self.session_dir, "seg_%06d.ts")
        cmd.extend(
            [
                "-f",
                "segment",
                "-segment_format",
                "mpegts",
                "-segment_time",
                "{:.3f}".format(self.segment_seconds),
                "-segment_start_number",
                str(start_segment),
                seg_pattern,
            ]
        )
        return cmd

    # How long prepare() will wait for ffmpeg to actually produce
    # init.mp4 + the first segment before declaring the fmp4 path
    # broken and falling back to matroska. Has to comfortably exceed
    # ffmpeg's analyzeduration (15 s) plus header write time, plus a
    # safety margin for slow upstream reads. 30 s is the smallest
    # value that doesn't false-trip on a healthy 50 Mbps WEB-DL.
    _PREPARE_PRODUCTION_TIMEOUT_SECONDS = 30.0

    def prepare(self):
        """Eagerly spawn ffmpeg AND wait for it to actually produce
        init.mp4 + first segment before returning.

        Called from _register_session right after construction. For
        mpegts producers (the legacy lazy path) this is a no-op.
        For fmp4 producers this is the spawn-time validation that
        keeps the matroska late-binding fallback working — without
        it, ffmpeg's first spawn happens inside wait_for_init AFTER
        the HLS URL has already been returned to Kodi.

        Two failure-detection windows in sequence:

        1. **Argument rejection (~500 ms).** Catches "ffmpeg argv
           is wrong" failures: missing muxer, bad option, refused
           experimental codec, build mismatch, etc. ffmpeg exits
           with non-zero rc within ~10-100 ms in practice.

        2. **Production failure (up to _PREPARE_PRODUCTION_TIMEOUT
           _SECONDS).** Catches "ffmpeg started but never produced
           anything" failures: absolute path bug (a547a2d), -strict
           -2 missing (b8f09d6), analysis hang (1a56c36), and any
           future ffmpeg/source combo where output stalls after
           launch. Polls for init.mp4 + seg_000000.m4s on disk.
           If neither is on disk by the deadline, OR if ffmpeg has
           exited with non-zero rc in the meantime, raises so
           _register_session rewrites ctx to the matroska shape.

        Both checks must pass before prepare() returns successfully.
        Costs up to 30 s of latency on the first spawn for healthy
        sessions (typical: 2-5 s). That's the right tradeoff vs
        handing Kodi a URL that will never play — and the
        playback-never-started watchdog in service.py was raised
        to 30 s for exactly this latency budget.

        Raises:
            RuntimeError: ffmpeg failed to spawn, exited early, or
                produced no output within the production timeout.
        """
        if self.segment_format != "fmp4":
            return  # mpegts is lazy-spawned, no eager validation
        self._ensure_ffmpeg_headed_for(0)

        # Window 1: argument-rejection poll (500 ms).
        # An early exit with rc != 0 is a hard failure (bad argv,
        # missing muxer, refused experimental codec). An early exit
        # with rc == 0 is a SUCCESSFUL completion — possible when
        # the source is shorter than 500 ms of stream-copy work,
        # which happens with the synthetic test MKV in the
        # integration suite. Either way, on early exit we drop
        # straight to the production check and let it verify the
        # output files exist.
        argv_deadline = time.monotonic() + 0.5
        early_exit = False
        while time.monotonic() < argv_deadline:
            with self._lock:
                proc = self._proc
            if proc is None:
                raise RuntimeError("ffmpeg failed to spawn — check ffmpeg.log")
            rc = proc.poll()
            if rc is not None:
                if rc != 0:
                    raise RuntimeError(
                        "ffmpeg exited immediately with code {} — fmp4 "
                        "HLS likely unsupported by this build".format(rc)
                    )
                early_exit = True
                break
            time.sleep(0.05)

        # Window 2: wait for actual output production. Polls the
        # file system for init.mp4 + the first segment, AND watches
        # ffmpeg liveness so a late crash surfaces immediately.
        # If ffmpeg already exited cleanly in window 1 (rc==0), the
        # output files should already exist; we just need to verify
        # them once instead of waiting.
        init_path = os.path.join(self.session_dir, "init.mp4")
        first_seg_path = os.path.join(self.session_dir, "seg_000000.m4s")
        prod_deadline = time.monotonic() + self._PREPARE_PRODUCTION_TIMEOUT_SECONDS
        while time.monotonic() < prod_deadline:
            if os.path.exists(init_path) and os.path.exists(first_seg_path):
                xbmc.log(
                    "NZB-DAV: HlsProducer.prepare confirmed init.mp4 "
                    "and seg_000000.m4s on disk",
                    xbmc.LOGINFO,
                )
                return  # healthy — both files are on disk
            if early_exit:
                # ffmpeg already finished; if the files aren't here,
                # they're never going to be. Fail immediately
                # instead of waiting for the full deadline.
                raise RuntimeError(
                    "ffmpeg exited cleanly but produced no init.mp4 / "
                    "seg_000000.m4s — check ffmpeg.log"
                )
            with self._lock:
                proc = self._proc
            if proc is None:
                raise RuntimeError(
                    "ffmpeg disappeared during prepare — check ffmpeg.log"
                )
            rc = proc.poll()
            if rc is not None:
                # ffmpeg exited mid-window. rc==0 means the source
                # was short enough to finish during the production
                # wait — give the file-existence check one more
                # iteration before declaring failure.
                if rc != 0:
                    raise RuntimeError(
                        "ffmpeg exited with code {} before producing output "
                        "— check ffmpeg.log".format(rc)
                    )
                early_exit = True
                continue
            time.sleep(0.25)
        raise RuntimeError(
            "ffmpeg did not produce init.mp4 + seg_000000.m4s within "
            "{:.0f}s — check ffmpeg.log".format(
                self._PREPARE_PRODUCTION_TIMEOUT_SECONDS
            )
        )

    def close(self):
        """Kill ffmpeg and delete the session directory."""
        with self._lock:
            self._closed = True
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xbmc.log(
                    "NZB-DAV: HlsProducer.close: ffmpeg pid={} did not exit "
                    "5 s after kill; leaking for the OS to reap".format(
                        getattr(proc, "pid", "?")
                    ),
                    xbmc.LOGWARNING,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            self._ffmpeg_log.close()
        except OSError:
            pass
        # Persist the session's ffmpeg.log to a stable rolling
        # location BEFORE the session dir is deleted. Otherwise
        # every "playback failed" debug session has to chase a
        # log that no longer exists — which has bitten us several
        # times already on the fmp4 spike. Keep the most recent
        # 10 logs, named by session_id so they're easy to
        # cross-reference with the kodi.log "session_id=..." lines.
        try:
            self._archive_ffmpeg_log()
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            import shutil as _shutil

            _shutil.rmtree(self.session_dir, ignore_errors=True)
        except OSError:
            pass

    def _archive_ffmpeg_log(self):
        """Copy the session's ffmpeg.log to /storage/.kodi/temp/
        nzbdav-hls-logs/ and trim to the most recent 10."""
        import shutil as _shutil

        src = self._ffmpeg_log_path
        if not os.path.exists(src):
            return
        try:
            size = os.path.getsize(src)
        except OSError:
            return
        if size == 0:
            return  # empty log — nothing useful to preserve

        archive_dir = None
        try:
            import xbmcvfs

            candidate = xbmcvfs.translatePath("special://temp/nzbdav-hls-logs/")
            # In tests xbmcvfs is mocked and translatePath returns a
            # MagicMock. Only accept genuine string results so we
            # don't leak a "MagicMock" directory in cwd.
            if isinstance(candidate, str):
                archive_dir = candidate
        except Exception:  # pylint: disable=broad-except
            pass
        if not archive_dir:
            archive_dir = os.path.join(tempfile.gettempdir(), "nzbdav-hls-logs")
        try:
            os.makedirs(archive_dir, exist_ok=True)
        except OSError:
            return

        session_id = os.path.basename(self.session_dir)
        dst = os.path.join(archive_dir, "ffmpeg-{}.log".format(session_id))
        try:
            _shutil.copy2(src, dst)
        except OSError:
            return

        # Trim: keep the 10 most recent archived logs.
        try:
            entries = []
            for name in os.listdir(archive_dir):
                if not name.startswith("ffmpeg-") or not name.endswith(".log"):
                    continue
                full = os.path.join(archive_dir, name)
                try:
                    entries.append((os.path.getmtime(full), full))
                except OSError:
                    continue
            entries.sort(reverse=True)
            for _, path in entries[10:]:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        except OSError:
            pass

        xbmc.log(
            "NZB-DAV: Archived session ffmpeg.log to {}".format(dst),
            xbmc.LOGINFO,
        )


class StreamProxy:
    """Local HTTP proxy server for nzbdav streams."""

    def __init__(self):
        self._server = None
        self._thread = None
        self.port = 0
        self._context_lock = threading.RLock()
        self._ffmpeg_capabilities = None

    def start(self):
        """Start the proxy server on a random port."""
        self._server = _ThreadedHTTPServer(("127.0.0.1", 0), _StreamHandler)
        self._server.owner_proxy = self
        self.port = self._server.server_address[1]
        self._refresh_ffmpeg_capabilities()
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True
        self._thread.start()
        xbmc.log(
            "NZB-DAV: Stream proxy started on port {}".format(self.port),
            xbmc.LOGINFO,
        )

    def stop(self):
        """Stop the proxy server."""
        self.clear_sessions()
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def clear_sessions(self):
        """Tear down every registered session and kill its ffmpeg process.

        Called from:
        - stop() on service shutdown
        - prepare_stream() on each new play, so a zombie ffmpeg from a
          previous stream that Kodi abandoned without firing onPlayBackStopped
          (e.g. DB-vacuum stall that freezes the decoder) doesn't keep
          writing into a half-dead TCP socket forever
        - NzbdavPlayer stop/end hooks for clean-stop cases
        """
        if not self._server:
            return
        with self._context_lock:
            sessions = list(getattr(self._server, "stream_sessions", {}).values())
            self._server.stream_sessions = {}
            self._server.stream_context = None
            self._server.active_ffmpeg = None
        for ctx in sessions:
            self._cleanup_session(ctx)

    @staticmethod
    def _try_faststart_layout(remote_url, content_length, auth_header):
        """Attempt virtual moov-relocation for an MP4.

        Returns the faststart dict, or None on failure.
        """
        try:
            if fetch_remote_mp4_layout is None:
                raise ImportError("mp4_parser not available")
            layout_info = fetch_remote_mp4_layout(
                remote_url, content_length, auth_header
            )
            if layout_info:
                xbmc.log(
                    "NZB-DAV: MP4 layout: moov_before_mdat={}, moov={}B".format(
                        layout_info.get("moov_before_mdat"),
                        len(layout_info.get("moov_data", b"")),
                    ),
                    xbmc.LOGINFO,
                )
                faststart = build_faststart_layout(layout_info)
                if faststart is None:
                    xbmc.log(
                        "NZB-DAV: stco overflow — moov relocation failed "
                        "(file >4GB with 32-bit chunk offsets)",
                        xbmc.LOGWARNING,
                    )
                return faststart
            xbmc.log("NZB-DAV: MP4 layout fetch returned None", xbmc.LOGWARNING)
            return None
        except _PARSE_ERRORS as e:
            xbmc.log(
                "NZB-DAV: MP4 faststart parse failed: {}".format(e), xbmc.LOGWARNING
            )
            return None

    @staticmethod
    def _cleanup_session(ctx):
        """Release resources associated with a stream session."""
        active_ffmpeg = ctx.get("active_ffmpeg")
        if active_ffmpeg:
            try:
                active_ffmpeg.kill()
                active_ffmpeg.wait()
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        temp_path = ctx.get("temp_path")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

        hls_producer = ctx.get("hls_producer")
        if hls_producer is not None:
            try:
                hls_producer.close()
            except _HLS_CLOSE_ERRORS as e:
                xbmc.log(
                    "NZB-DAV: HLS producer close failed: {}".format(e),
                    xbmc.LOGWARNING,
                )

    @staticmethod
    def _probe_hls_fmp4_capability(ffmpeg_path):
        """Return True when ffmpeg exposes the HLS fMP4 muxer flags we use."""
        if not ffmpeg_path:
            return False
        cmd = [ffmpeg_path, "-hide_banner", "-h", "muxer=hls"]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            try:
                output = proc.communicate(timeout=_FFMPEG_CAPABILITY_PROBE_TIMEOUT)
                if not isinstance(output, (tuple, list)) or len(output) != 2:
                    raise ValueError("invalid ffmpeg capability probe output")
                stdout, stderr = output
            except subprocess.TimeoutExpired:
                proc.kill()
                # Bound the post-kill drain: if the kill itself hangs
                # (uninterruptible I/O) we don't want service startup to
                # wedge indefinitely waiting on ffmpeg.
                try:
                    stdout, stderr = proc.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    stdout, stderr = b"", b""
                xbmc.log(
                    "NZB-DAV: ffmpeg capability probe timed out for {}".format(
                        ffmpeg_path
                    ),
                    xbmc.LOGWARNING,
                )
                return False
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            xbmc.log(
                "NZB-DAV: ffmpeg capability probe failed for {}: {}".format(
                    ffmpeg_path, e
                ),
                xbmc.LOGWARNING,
            )
            return False

        output = ((stdout or b"") + b"\n" + (stderr or b"")).decode(
            "utf-8", errors="ignore"
        )
        supported = all(marker in output for marker in _FMP4_HLS_CAPABILITY_MARKERS)
        xbmc.log(
            "NZB-DAV: ffmpeg fmp4 HLS capability {} ({})".format(
                "present" if supported else "absent", ffmpeg_path
            ),
            xbmc.LOGINFO if supported else xbmc.LOGWARNING,
        )
        return supported

    def _refresh_ffmpeg_capabilities(self):
        """Discover ffmpeg once so service-start logs show the active muxers."""
        ffmpeg_path = _find_ffmpeg()
        capabilities = {
            "ffmpeg_path": ffmpeg_path,
            "hls_fmp4": self._probe_hls_fmp4_capability(ffmpeg_path),
        }
        self._ffmpeg_capabilities = capabilities
        return capabilities

    def _get_ffmpeg_capabilities(self):
        """Return cached ffmpeg capabilities, probing lazily if needed."""
        capabilities = getattr(self, "_ffmpeg_capabilities", None)
        if isinstance(capabilities, dict):
            return capabilities
        ffmpeg_path = _find_ffmpeg()
        return {
            "ffmpeg_path": ffmpeg_path,
            "hls_fmp4": bool(ffmpeg_path),
        }

    def _assert_context_lock_owned(self):
        """Best-effort debug guard for helpers that require _context_lock."""
        if not __debug__:
            return
        context_lock = getattr(self, "_context_lock", None)
        is_owned = getattr(context_lock, "_is_owned", None)
        if callable(is_owned) and not is_owned():
            raise AssertionError("_prune_sessions_locked requires _context_lock")

    def _register_session(self, ctx):
        """Store a per-stream context and return its unique proxy URL.

        The returned URL shape depends on ``ctx["mode"]``:

        - ``"hls"`` → ``/hls/<session>/playlist.m3u8`` so Kodi's HLS
          demuxer takes over and drives segment fetches.
        - default → ``/stream/<session>`` for the existing faststart /
          temp-faststart / remux / pass-through handlers.

        For HLS sessions, an ``HlsProducer`` is attached to the ctx
        (``ctx["hls_producer"]``) which owns the persistent ffmpeg
        process and the on-disk segment directory.
        """
        session_id = uuid.uuid4().hex
        now = time.time()
        ctx["session_id"] = session_id
        ctx["created_at"] = now
        ctx["last_access"] = now
        ctx["ffmpeg_lock"] = threading.Lock()
        ctx["active_ffmpeg"] = None
        ctx["current_byte_pos"] = 0

        if ctx.get("mode") == "hls":
            workdir = _choose_hls_workdir()
            producer = None
            try:
                producer = HlsProducer(ctx, workdir)
                # Eager spawn-time validation: catches ffmpeg builds
                # that reject -hls_segment_type fmp4 BEFORE the HLS
                # URL is returned to Kodi, so the matroska fallback
                # below actually fires for the most likely failure
                # mode. No-op for mpegts (lazy spawn).
                producer.prepare()
                ctx["hls_producer"] = producer
            except Exception as e:  # noqa: BLE001 — fall back either way
                xbmc.log(
                    "NZB-DAV: HLS producer setup failed ({}), "
                    "rewriting session to matroska fallback".format(e),
                    xbmc.LOGWARNING,
                )
                # Best-effort cleanup of the partially initialized
                # producer. HlsProducer.__init__ owns disk resources
                # (session_dir, ffmpeg.log) that need close()'ing on
                # the prepare()-failure path; otherwise opt-in fmp4
                # plays against an unsupported ffmpeg build orphan
                # the session directory and rely on GC for the file
                # handle. The `producer = None` sentinel above
                # protects against AttributeError when the
                # constructor itself raised (no producer ever
                # assigned).
                if producer is not None:
                    try:
                        producer.close()
                    except Exception:  # noqa: BLE001
                        pass
                # Rewrite ctx in-place to the known-good matroska shape.
                # ctx already has ffmpeg_path / total_bytes /
                # duration_seconds from prepare_stream's fmp4 branch,
                # so _serve_remux has everything it needs.
                ctx.pop("mode", None)
                ctx.pop("hls_segment_format", None)
                ctx.pop("hls_segment_duration", None)
                ctx.pop("hls_producer", None)
                ctx["content_type"] = "video/x-matroska"
                ctx["seekable"] = (
                    ctx.get("duration_seconds") is not None
                    and ctx.get("total_bytes", 0) > 0
                )

        with self._context_lock:
            if not isinstance(getattr(self._server, "stream_sessions", None), dict):
                self._server.stream_sessions = {}
            self._server.stream_context = ctx
            self._server.stream_sessions[session_id] = ctx
            self._prune_sessions_locked(keep_session=session_id)

        if ctx.get("mode") == "hls":
            return "http://127.0.0.1:{}/hls/{}/playlist.m3u8".format(
                self.port, session_id
            )
        return "http://127.0.0.1:{}/stream/{}".format(self.port, session_id)

    def _prune_sessions_locked(self, keep_session=None):
        """Drop expired sessions and cap the total number retained."""
        self._assert_context_lock_owned()
        sessions = getattr(self._server, "stream_sessions", {})
        now = time.time()

        expired = [
            session_id
            for session_id, ctx in sessions.items()
            if session_id != keep_session
            and now - ctx.get("last_access", ctx.get("created_at", now))
            > _SESSION_TTL_SECONDS
        ]
        for session_id in expired:
            ctx = sessions.pop(session_id, None)
            if ctx is not None:
                self._cleanup_session(ctx)

        while len(sessions) > _MAX_STREAM_SESSIONS:
            removable = sorted(
                (
                    ctx.get("last_access", ctx.get("created_at", 0)),
                    session_id,
                )
                for session_id, ctx in sessions.items()
                if session_id != keep_session
            )
            if not removable:
                break
            _, session_id = removable[0]
            ctx = sessions.pop(session_id, None)
            if ctx is not None:
                self._cleanup_session(ctx)

    def prepare_stream(self, remote_url, auth_header=None):
        """Set up proxy for a new stream.

        Returns (local_proxy_url, stream_info_dict).
        stream_info_dict contains duration_seconds, total_bytes, seekable, remux,
        faststart, and virtual_size.
        """
        _validate_url(remote_url)
        # Tear down any previous session before starting a new one. Kodi only
        # ever plays one stream at a time, so anything still in the table is
        # garbage from a prior play — possibly with a zombie ffmpeg attached
        # to a half-dead socket if Kodi stalled without firing
        # onPlayBackStopped. Cleaning up here guarantees the next play gets a
        # fresh proxy state and no stale ffmpeg hogging the upstream.
        self.clear_sessions()
        content_type = self._detect_content_type(remote_url)
        lower_url = remote_url.lower()
        is_mp4 = lower_url.endswith((".mp4", ".m4v"))

        if is_mp4:
            content_length = self._get_content_length(remote_url, auth_header)
            faststart = self._try_faststart_layout(
                remote_url, content_length, auth_header
            )

            if faststart is not None and not faststart.get("already_faststart"):
                ctx = {
                    "remote_url": remote_url,
                    "auth_header": auth_header,
                    "content_type": "video/mp4",
                    "faststart": True,
                    "remux": False,
                    "header_data": faststart["header_data"],
                    "virtual_size": faststart["virtual_size"],
                    "payload_remote_start": faststart["payload_remote_start"],
                    "payload_remote_end": faststart["payload_remote_end"],
                    "payload_size": faststart["payload_size"],
                    "range_cache": RangeCache(),
                }
                xbmc.log(
                    "NZB-DAV: MP4 faststart proxy (virtual={}B, header={}B)".format(
                        faststart["virtual_size"], len(faststart["header_data"])
                    ),
                    xbmc.LOGINFO,
                )
            elif faststart is not None and faststart.get("already_faststart"):
                # Already faststart — redirect directly to WebDAV URL.
                # No proxy needed: moov is at the front, Kodi can seek natively.
                # This follows the Stremio ecosystem pattern: expose the direct
                # byte-servable URL when the backend stream is already good.
                xbmc.log(
                    "NZB-DAV: MP4 already faststart, direct redirect", xbmc.LOGINFO
                )
                stream_info = {
                    "duration_seconds": None,
                    "total_bytes": content_length,
                    "virtual_size": 0,
                    "seekable": True,
                    "remux": False,
                    "faststart": False,
                    "direct": True,
                }
                return remote_url, stream_info
            else:
                # Tier 2: Try temp-file faststart (ffmpeg -movflags +faststart)
                # Skip for large files (>4GB) — temp remux would take too long
                # and would time out the prepare_stream_via_service call.
                _TEMP_FASTSTART_MAX = 4 * 1073741824  # 4 GB
                ffmpeg_path = self._get_ffmpeg_capabilities().get("ffmpeg_path")
                if content_length > _TEMP_FASTSTART_MAX:
                    xbmc.log(
                        "NZB-DAV: File too large for temp-file faststart "
                        "({}B > {}B), skipping to MKV remux".format(
                            content_length, _TEMP_FASTSTART_MAX
                        ),
                        xbmc.LOGINFO,
                    )
                    temp_path = None
                else:
                    temp_path = (
                        self._prepare_tempfile_faststart(
                            ffmpeg_path, remote_url, auth_header
                        )
                        if ffmpeg_path
                        else None
                    )

                if temp_path:
                    temp_size = os.path.getsize(temp_path)
                    ctx = {
                        "remote_url": remote_url,
                        "auth_header": auth_header,
                        "content_type": "video/mp4",
                        "faststart": False,
                        "remux": False,
                        "temp_faststart": True,
                        "temp_path": temp_path,
                        "content_length": temp_size,
                    }
                    xbmc.log(
                        "NZB-DAV: MP4 temp-file faststart ({}B)".format(temp_size),
                        xbmc.LOGINFO,
                    )
                elif ffmpeg_path:
                    # Tier 3: MKV remux fallback (existing behavior)
                    duration = self._probe_duration(
                        ffmpeg_path, remote_url, auth_header
                    )
                    ctx = {
                        "remote_url": remote_url,
                        "auth_header": auth_header,
                        "content_type": "video/x-matroska",
                        "remux": True,
                        "faststart": False,
                        "ffmpeg_path": ffmpeg_path,
                        "total_bytes": content_length,
                        "duration_seconds": duration,
                        "seekable": duration is not None and content_length > 0,
                    }
                    xbmc.log("NZB-DAV: MP4 fallback to MKV remux", xbmc.LOGWARNING)
                else:
                    # Last resort: direct proxy (may fail for large files)
                    ctx = {
                        "remote_url": remote_url,
                        "auth_header": auth_header,
                        "content_length": content_length,
                        "content_type": "video/mp4",
                        "remux": False,
                        "faststart": False,
                    }
        else:
            content_length = self._get_content_length(remote_url, auth_header)
            threshold = _get_force_remux_threshold_bytes()
            needs_remux = bool(threshold) and content_length >= threshold
            ffmpeg_caps = self._get_ffmpeg_capabilities() if needs_remux else {}
            ffmpeg_path = ffmpeg_caps.get("ffmpeg_path")
            if ffmpeg_path:
                # 32-bit Kodi builds (Amlogic CoreELEC and similar) throw
                # `Open - Unhandled exception` on pass-through HTTP when the
                # advertised Content-Length exceeds ~4 GB — a cache/offset
                # overflow inside Kodi itself that no proxy tweak can fix.
                # Force a remux through ffmpeg so Kodi sees a streamed
                # file shape without the problematic Content-Length.
                #
                # Two output shapes, driven by the force_remux_mode
                # setting:
                #
                # - "matroska" (default): piped MKV via _serve_remux,
                #   cache-bounded seek (~3 min forward), ffmpeg
                #   restart on large seeks. Known-good on DV HEVC.
                # - "hls_fmp4" (experimental): fragmented-MP4 HLS VOD
                #   playlist, full random seek. DV RPU SEI NALs survive
                #   fmp4 fragment boundaries (unlike mpegts PES
                #   packetization), so this is the DV-capable path.
                #   Gated behind a setting because fmp4 HLS on Amlogic
                #   Kodi is unproven in the field.
                duration = self._probe_duration(ffmpeg_path, remote_url, auth_header)
                use_fmp4 = (
                    _get_force_remux_mode() == "hls_fmp4"
                    and ffmpeg_caps.get("hls_fmp4", False)
                    and duration is not None
                    and duration > 0
                )
                if _get_force_remux_mode() == "hls_fmp4" and not ffmpeg_caps.get(
                    "hls_fmp4", False
                ):
                    xbmc.log(
                        "NZB-DAV: ffmpeg lacks required fmp4 HLS flags; "
                        "falling back to piped Matroska",
                        xbmc.LOGWARNING,
                    )
                if use_fmp4:
                    # Gate fmp4 HLS on DV profile. The original gate only
                    # rejected profile 7 (dual-layer FEL) on the theory
                    # that single-layer profiles 5 and 8 would pass
                    # through fmp4 cleanly. 2026-04-15 testing on a DV
                    # Profile 8 source (Evangelion.3.0+1.0.Thrice.Upon.a.
                    # Time.2021.2160p.BluRay...DV.HDR.H.265) proved that
                    # wrong: the CAMLCodec HW decoder opened with
                    # ``DOVI: version 1.0, profile 8, el type 0``, ffmpeg
                    # produced 14+ segments cleanly, but ``onAVStarted``
                    # never fired and the addon's 30 s watchdog tripped
                    # at 175 s. The HW decoder was stuck in init state
                    # with partial YUV planes (half-green screen) and
                    # Kodi froze trying to close the player.
                    #
                    # Broadened the gate: ANY confirmed DV profile
                    # routes to matroska. fmp4 HLS is reserved for
                    # sources the probe reports as non-DV (or can't
                    # read, in which case we assume non-DV because
                    # genuinely unparseable headers are rare and the
                    # cost of a wrong guess toward fmp4 is low now
                    # that prepare() has the runtime production
                    # watchdog to fall back anyway).
                    dv_profile = self._probe_dv_profile(
                        ffmpeg_path, remote_url, auth_header
                    )
                    if dv_profile is not None:
                        xbmc.log(
                            "NZB-DAV: Source is Dolby Vision profile {}; "
                            "fmp4 HLS does not decode DV on this Amlogic "
                            "build — falling back to piped Matroska "
                            "despite force_remux_mode=hls_fmp4".format(dv_profile),
                            xbmc.LOGWARNING,
                        )
                        use_fmp4 = False
                    else:
                        xbmc.log(
                            "NZB-DAV: DV profile probe: none/unknown "
                            "— proceeding with fmp4 HLS",
                            xbmc.LOGDEBUG,
                        )
                if use_fmp4:
                    ctx = {
                        "remote_url": remote_url,
                        "auth_header": auth_header,
                        "content_type": "application/vnd.apple.mpegurl",
                        "mode": "hls",
                        "remux": True,
                        "faststart": False,
                        "ffmpeg_path": ffmpeg_path,
                        "total_bytes": content_length,
                        "duration_seconds": duration,
                        "seekable": True,
                        "hls_segment_duration": _HLS_SEGMENT_SECONDS,
                        "hls_segment_format": "fmp4",
                    }
                    xbmc.log(
                        "NZB-DAV: Force-remuxing {}B file via fMP4 HLS "
                        "(experimental, duration={:.1f}s)".format(
                            content_length, duration
                        ),
                        xbmc.LOGWARNING,
                    )
                else:
                    ctx = {
                        "remote_url": remote_url,
                        "auth_header": auth_header,
                        "content_type": "video/x-matroska",
                        "remux": True,
                        "faststart": False,
                        "ffmpeg_path": ffmpeg_path,
                        "total_bytes": content_length,
                        "duration_seconds": duration,
                        "seekable": duration is not None and content_length > 0,
                    }
                    xbmc.log(
                        "NZB-DAV: Force-remuxing large {}B file via piped MKV "
                        "(duration={}, threshold={}B)".format(
                            content_length,
                            "{:.1f}s".format(duration) if duration else "unknown",
                            threshold,
                        ),
                        xbmc.LOGWARNING,
                    )
            else:
                if needs_remux:
                    xbmc.log(
                        "NZB-DAV: {}B file exceeds remux threshold but no "
                        "ffmpeg found — falling back to pass-through, "
                        "playback may fail on 32-bit Kodi".format(content_length),
                        xbmc.LOGWARNING,
                    )
                ctx = {
                    "remote_url": remote_url,
                    "auth_header": auth_header,
                    "content_length": content_length,
                    "content_type": content_type,
                    "remux": False,
                }

        local_url = self._register_session(ctx)
        xbmc.log(
            "NZB-DAV: Proxy ready (remux={}, faststart={}): {}".format(
                ctx.get("remux", False), ctx.get("faststart", False), local_url
            ),
            xbmc.LOGINFO,
        )
        stream_info = {
            "duration_seconds": ctx.get("duration_seconds"),
            "total_bytes": ctx.get("total_bytes", ctx.get("content_length", 0)),
            "virtual_size": ctx.get("virtual_size", 0),
            "seekable": (
                ctx.get("seekable", False)
                or ctx.get("faststart", False)
                or ctx.get("temp_faststart", False)
            ),
            "remux": ctx.get("remux", False),
            "faststart": ctx.get("faststart", False),
        }
        return local_url, stream_info

    @staticmethod
    def _probe_duration(ffmpeg_path, url, auth_header):
        """Probe file duration. Returns seconds or None.

        Two strategies, tried in order:

        1. ``ffprobe -show_entries format=duration`` — the clean path. One
           number on stdout, no stream-probe warnings. This is the only
           reliable approach for files with many subtitle streams: a 30-
           subtitle Blu-ray remux produces a wall of ``Could not find
           codec parameters for stream N (Subtitle: hdmv_pgs_subtitle)``
           warnings from ffmpeg that can trivially push ``Duration:`` past
           any stderr buffer budget before it gets a chance to print.
        2. ``ffmpeg -i <url> -f null -`` parsed out of stderr — the
           fallback path when ffprobe isn't installed. Budget is 64 KB
           (up from the original 8 KB) so the subtitle-warning wall
           doesn't evict the Duration line on pathological inputs.

        Args:
            ffmpeg_path: Path to the ffmpeg binary. Used by the fallback
                path and as the starting point for ffprobe discovery.
            url: Remote HTTP URL to probe.
            auth_header: Optional Basic auth header; passed to the child
                process via ``-headers`` so the input URL stays clean.
        """
        _validate_url(url)
        auth_args = _ffmpeg_auth_args(auth_header)

        ffprobe_path = _find_ffprobe()
        if ffprobe_path:
            result = StreamProxy._probe_duration_ffprobe(
                ffprobe_path, url, auth_args=auth_args
            )
            if result is not None:
                return result

        return StreamProxy._probe_duration_ffmpeg(ffmpeg_path, url, auth_args=auth_args)

    @staticmethod
    def _probe_duration_ffprobe(ffprobe_path, input_url, auth_args=None):
        """Run ffprobe to get duration. Returns seconds or None."""
        try:
            cmd = [
                ffprobe_path,
                "-v",
                "error",
            ]
            if auth_args:
                cmd.extend(auth_args)
            cmd.extend(
                [
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nokey=1:noprint_wrappers=1",
                    input_url,
                ]
            )
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            try:
                stdout, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                xbmc.log("NZB-DAV: ffprobe duration timed out", xbmc.LOGWARNING)
                return None
            if proc.returncode != 0:
                return None
            text = stdout.decode(errors="replace").strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return None
        except (OSError, subprocess.SubprocessError) as e:
            xbmc.log("NZB-DAV: ffprobe failed: {}".format(e), xbmc.LOGWARNING)
            return None

    @staticmethod
    def _probe_duration_ffmpeg(ffmpeg_path, input_url, auth_args=None):
        """Parse Duration out of ``ffmpeg -i`` stderr. Returns seconds or None.

        Uses the bounded-reader-thread pattern: a daemon thread reads
        stderr line-by-line into a shared buffer and signals an Event
        as soon as ``Duration:`` is matched or the 64 KB byte budget
        is exhausted. The main thread waits on the Event with a
        hard wall-clock deadline of ``_PROBE_DEADLINE_SECONDS`` so a
        stuck ffmpeg (slow upstream, stalled header parse, auth hang)
        can't wedge the probe forever. Either way — match, budget,
        deadline — the ffmpeg process is killed before returning.
        """
        return StreamProxy._probe_ffmpeg_stderr(
            ffmpeg_path,
            input_url,
            _parse_ffmpeg_duration,
            "Duration",
            auth_args=auth_args,
        )

    @staticmethod
    def _probe_ffmpeg_stderr(ffmpeg_path, input_url, parser, label, auth_args=None):
        """Shared body of ``_probe_duration_ffmpeg`` and
        ``_probe_dv_profile``. Spawns ``ffmpeg -v info -i <url> -f null
        -`` and runs the parser against collected stderr under a
        bounded reader thread + wall-clock deadline.

        ``auth_args`` is an optional list of ffmpeg argv pieces (from
        ``_ffmpeg_auth_args``) that gets inserted before ``-i`` so the
        Authorization header is passed via ``-headers`` instead of
        being spliced into the input URL. Callers that build the URL
        with ``_embed_auth_in_url`` should leave this None; new
        callers should prefer the ``-headers`` form.
        """
        cmd = [ffmpeg_path, "-v", "info"]
        if auth_args:
            cmd.extend(auth_args)
        cmd.extend(["-i", input_url, "-f", "null", "-"])

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            xbmc.log(
                "NZB-DAV: {} probe spawn failed: {}".format(label, e),
                xbmc.LOGWARNING,
            )
            return None

        collected = [""]
        done = threading.Event()
        # 64 KB budget: large enough that a 30-subtitle Blu-ray remux's
        # wall of per-stream probe warnings can't push the match line out.
        budget = 65536

        def _reader():
            try:
                for line in proc.stderr:
                    collected[0] += line.decode(errors="replace")
                    if parser(collected[0]) is not None:
                        return
                    if len(collected[0]) > budget:
                        return
            except Exception:  # pylint: disable=broad-except
                pass
            finally:
                done.set()

        reader = threading.Thread(
            target=_reader, name="nzbdav-probe-reader", daemon=True
        )
        reader.start()

        if not done.wait(timeout=_PROBE_DEADLINE_SECONDS):
            xbmc.log(
                "NZB-DAV: {} probe wall-clock deadline ({}s) exceeded, "
                "killing ffmpeg".format(label, _PROBE_DEADLINE_SECONDS),
                xbmc.LOGWARNING,
            )

        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass

        result = parser(collected[0])
        if result is None and len(collected[0]) > budget:
            xbmc.log(
                "NZB-DAV: {} not found in first {}B of ffmpeg output".format(
                    label, budget
                ),
                xbmc.LOGWARNING,
            )
        return result

    @staticmethod
    def _probe_dv_profile(ffmpeg_path, url, auth_header):
        """Probe the Dolby Vision profile of the source stream.

        Runs ``ffmpeg -i <url> -f null -`` under the same bounded
        reader-thread + wall-clock deadline pattern as the duration
        probe (via ``_probe_ffmpeg_stderr``), parsing the
        ``DOVI configuration record`` line out of stderr. Returns the
        profile integer (5, 7, 8, ...) or None.

        None means either "no DV metadata present" or "probe could
        not read the header" (including the deadline-exceeded case)
        — both of which are safe defaults for the fmp4 HLS dispatch:
        only a confirmed profile 7 (dual-layer FEL) has to be routed
        away from fmp4, because fmp4 HLS cannot carry two HEVC
        layers in one track and the EL would be silently dropped.
        """
        _validate_url(url)
        # Pass auth via -headers (clean URL, no credential leak into
        # ffmpeg.log on probe failure). See _ffmpeg_auth_args.
        return StreamProxy._probe_ffmpeg_stderr(
            ffmpeg_path,
            url,
            _parse_ffmpeg_dv_profile,
            "DV profile",
            auth_args=_ffmpeg_auth_args(auth_header),
        )

    @staticmethod
    def _prepare_tempfile_faststart(ffmpeg_path, url, auth_header):
        """Remux MP4 with faststart to a temp file. Returns path or None."""
        if not ffmpeg_path:
            return None

        _validate_url(url)
        auth_args = _ffmpeg_auth_args(auth_header)
        fd, temp_path = tempfile.mkstemp(
            prefix="nzbdav_faststart_",
            suffix=".mp4",
        )
        os.close(fd)

        cmd = [
            ffmpeg_path,
            "-v",
            "warning",
            "-y",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
        ]
        if auth_args:
            cmd.extend(auth_args)
        cmd.extend(
            [
                "-i",
                url,
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                temp_path,
            ]
        )

        try:
            xbmc.log("NZB-DAV: Temp-file faststart remux starting", xbmc.LOGINFO)
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False
            )
            _, stderr = proc.communicate(timeout=600)  # 10 min timeout
            if proc.returncode != 0:
                xbmc.log(
                    "NZB-DAV: Temp faststart failed: {}".format(
                        stderr.decode(errors="replace")[:300]
                    ),
                    xbmc.LOGWARNING,
                )
                return None
            if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
                return temp_path
        except (OSError, subprocess.SubprocessError) as e:
            xbmc.log("NZB-DAV: Temp faststart error: {}".format(e), xbmc.LOGWARNING)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return None

    @staticmethod
    def _get_content_length(url, auth_header):
        """Get file size via HEAD or range probe."""
        req = Request(url, method="HEAD")
        if auth_header:
            req.add_header("Authorization", auth_header)
        try:
            # nosemgrep
            with urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
                req, timeout=10
            ) as resp:
                return int(resp.headers.get("Content-Length", 0))
        except (OSError, ValueError):
            pass
        try:
            req = Request(url)
            req.add_header("Range", "bytes=-1")
            if auth_header:
                req.add_header("Authorization", auth_header)
            # nosemgrep
            with urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
                req, timeout=10
            ) as resp:
                cr = resp.headers.get("Content-Range", "")
                return int(cr.split("/")[1]) if "/" in cr else 0
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _detect_content_type(url):
        """Detect content type from URL extension."""
        lower = url.lower()
        if lower.endswith(".mkv"):
            return "video/x-matroska"
        if lower.endswith((".mp4", ".m4v")):
            return "video/mp4"
        if lower.endswith(".avi"):
            return "video/x-msvideo"
        return "video/mp4"


def get_service_proxy_port():
    """Get the proxy port from the background service, or 0 if not running."""
    try:
        import xbmcgui

        home = xbmcgui.Window(10000)
        port_str = home.getProperty("nzbdav.proxy_port")
        return int(port_str) if port_str else 0
    except _KODI_SETTING_ERRORS:
        return 0


def prepare_stream_via_service(port, remote_url, auth_header=None):
    """Ask the service's proxy to prepare a stream.

    Returns (proxy_url, stream_info) where stream_info contains
    duration_seconds, total_bytes, seekable, remux.
    """
    import json

    url = "http://127.0.0.1:{}/prepare".format(port)
    data = json.dumps({"remote_url": remote_url, "auth_header": auth_header})
    req = Request(url, data=data.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    # nosemgrep
    with urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
        req, timeout=60
    ) as resp:
        result = json.loads(resp.read())
        proxy_url = result.pop("proxy_url")
        return proxy_url, result


def get_proxy():
    """Get or create the singleton stream proxy."""
    global _proxy
    with _proxy_lock:
        if _proxy is None:
            _proxy = StreamProxy()
            _proxy.start()
        return _proxy
