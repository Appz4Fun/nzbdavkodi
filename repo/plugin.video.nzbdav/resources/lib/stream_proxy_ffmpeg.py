# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""ffmpeg/ffprobe discovery, HLS workdir selection, and auth-arg helpers.

Extracted from ``stream_proxy.py`` (Stage 1 decomposition). Groups the
binary-discovery (``_find_ffmpeg`` / ``_find_ffprobe``), HLS-capability probe,
workdir free-space selection, ffmpeg auth-argument builders, duration parsing,
async process reaping, and auth-in-URL embedding helpers. All names are
re-exported by ``stream_proxy`` so existing references and test patches (e.g.
``stream_proxy._find_ffprobe``) keep resolving.

Plain constants are imported from ``stream_proxy``; parent helpers, the cached
private-temp-root singleton, and any monkeypatch target (``xbmc``,
``_HLS_WORKDIR_CANDIDATES``) are reached at call time via ``_sp.<name>`` so
patching keeps working.
"""

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
from urllib.parse import quote, urlsplit, urlunsplit  # noqa: E402

import resources.lib.stream_proxy as _sp  # noqa: E402
from resources.lib.stream_proxy import (  # noqa: E402
    _DURATION_RE,
    _FFMPEG_CAPABILITY_PROBE_TIMEOUT,
    _FFMPEG_PATHS,
    _FFPROBE_PATHS,
)


def _get_private_hls_temp_root():
    """Return a reusable private temp root for HLS work files.

    The cached singleton and its lock live on ``stream_proxy`` (tests patch
    ``stream_proxy._HLS_PRIVATE_TEMP_ROOT``), so the read and the write both go
    through ``_sp`` to mutate that module's global rather than a local one.
    """
    with _sp._HLS_PRIVATE_TEMP_ROOT_LOCK:
        cached = _sp._HLS_PRIVATE_TEMP_ROOT
        if cached and os.path.isdir(cached) and os.access(cached, os.W_OK):
            return cached

        temp_root = _sp.tempfile.mkdtemp(prefix="nzbdav-hls-")
        # 0o700 is restrictive (user-only); semgrep rule is a false positive.
        try:
            os.chmod(temp_root, 0o700)  # nosemgrep
        except OSError:
            pass

        _sp._HLS_PRIVATE_TEMP_ROOT = temp_root
        return temp_root


def _disk_free_bytes(path):
    usage = shutil.disk_usage(path)
    return getattr(usage, "free", usage[2])


def _workdir_has_free_space(base, required_bytes):
    """Return True when ``base`` has at least ``required_bytes`` free.

    Treats an unreadable free-space probe as "not enough" so callers can
    simply skip the candidate.
    """
    if not required_bytes:
        return True
    try:
        return _sp._disk_free_bytes(base) >= required_bytes
    except OSError:
        return False


def _choose_hls_workdir(required_bytes=0):
    """Return a writable base directory for HLS session working files.

    Walks the candidate list in order and returns the first entry
    whose parent exists, is writable, and has enough free space.
    Creates the leaf directory if missing. Falls back to a private
    temp directory as a last resort.
    """
    for base in _sp._HLS_WORKDIR_CANDIDATES:
        parent = os.path.dirname(base) or "/"
        if not os.path.isdir(parent):
            continue
        if not os.access(parent, os.W_OK):
            continue
        try:
            os.makedirs(base, exist_ok=True)
        except OSError:
            continue
        if not _workdir_has_free_space(base, required_bytes):
            continue
        return base
    fallback = _sp._get_private_hls_temp_root()
    if not _workdir_has_free_space(fallback, required_bytes):
        raise OSError(
            "No HLS workdir has at least {} bytes free space".format(required_bytes)
        )
    return fallback


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


def _run_ffmpeg_hls_muxer_probe(ffmpeg_path):
    """Run ``ffmpeg -h muxer=hls`` and return combined output, or None on error.

    Returns the decoded stdout+stderr text on success, or ``None`` when the
    probe fails to launch, times out, or yields malformed output.
    """
    cmd = [ffmpeg_path, "-hide_banner", "-h", "muxer=hls"]
    try:
        # nosemgrep
        proc = subprocess.Popen(  # nosec B603 — argv list, shell=False
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
        )
        try:
            output = proc.communicate(timeout=_FFMPEG_CAPABILITY_PROBE_TIMEOUT)
            if not isinstance(output, (tuple, list)) or len(output) != 2:
                raise ValueError("invalid ffmpeg capability probe output")
            stdout, stderr = output
        except subprocess.TimeoutExpired:
            _sp._drain_killed_ffmpeg_probe(proc, ffmpeg_path)
            return None
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        _sp.xbmc.log(
            "NZB-DAV: ffmpeg capability probe failed for {}: {}".format(ffmpeg_path, e),
            _sp.xbmc.LOGWARNING,
        )
        return None

    return ((stdout or b"") + b"\n" + (stderr or b"")).decode("utf-8", errors="ignore")


def _drain_killed_ffmpeg_probe(proc, ffmpeg_path):
    """Kill a timed-out probe process and bound the post-kill drain.

    If the kill itself hangs (uninterruptible I/O) we don't want service
    startup to wedge indefinitely waiting on ffmpeg.
    """
    proc.kill()
    try:
        proc.communicate(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    _sp.xbmc.log(
        "NZB-DAV: ffmpeg capability probe timed out for {}".format(ffmpeg_path),
        _sp.xbmc.LOGWARNING,
    )


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
    auth_header = _sp._validate_auth_header(auth_header)
    if not auth_header:
        return []
    return ["-headers", "Authorization: {}\r\n".format(auth_header)]


def _parse_ffmpeg_duration(stderr_text):
    """Parse 'Duration: HH:MM:SS.xx' from ffmpeg stderr output.

    Returns duration in seconds as a float, or None if not found.
    """
    match = _DURATION_RE.search(stderr_text)
    if not match:
        return None
    hours, minutes, seconds, frac = match.groups()
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + (int(frac) / (10 ** len(frac)) if frac else 0)
    )


def _reap_process_async(proc, label):
    """Wait for a killed child process in the background."""

    def _reap():
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _sp.xbmc.log(
                "NZB-DAV: {} pid={} did not exit within 2 s; "
                "leaking to OS reap".format(label, getattr(proc, "pid", "?")),
                _sp.xbmc.LOGWARNING,
            )
        except OSError:
            pass

    thread = threading.Thread(target=_reap, daemon=True)
    thread.start()
