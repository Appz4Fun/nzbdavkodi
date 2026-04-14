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
import threading
import time
import uuid
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
# Chunk size for reading from the upstream HTTP response in _serve_proxy.
# Kept small (64 KB) because on 32-bit Kodi the address space is ~3 GB and
# Kodi's CFileCache can reserve up to ~1.5 GB on its own. A 1 MB read
# buffer has been observed to hit MemoryError when a second proxy
# connection opens during Kodi's CCurlFile reconnect-on-error recovery.
_UPSTREAM_READ_CHUNK = 65536

# Shared zero buffer reused across all pass-through responses.
_ZERO_FILL_BUFFER = bytes(65536)

# Socket write timeout for _serve_remux.  If Kodi stops reading from the
# proxy socket without closing it (decoder stalls for too long, e.g. during
# a long DB vacuum) wfile.write() would block forever and ffmpeg would keep
# producing output into the void.  60s comfortably exceeds any normal
# buffering stall on a healthy client while still bounding zombie lifetime.
_REMUX_WRITE_TIMEOUT = 60

# HLS segment length. Chosen to balance seek granularity (coarser
# segments mean the HLS demuxer can only land on 30-second boundaries
# when seeking) against ffmpeg cold-start amortization (each ffmpeg
# restart on seek costs ~10-15 s on remote huge files, so segments
# much shorter than that would mean constant buffering on every
# seek). 30 s is a reasonable compromise.
_HLS_SEGMENT_SECONDS = 30.0

# Disk-backed HLS session working directory. Must be on a filesystem
# with enough free space for the full remuxed output of any active
# session (~5 GB per 30 minutes at typical 4K REMUX bitrates). Each
# session gets its own subdirectory which is rm -rf'd on cleanup.
# Candidate paths in order — first one that exists + is writable wins.
_HLS_WORKDIR_CANDIDATES = (
    "/var/media/CACHE_DRIVE/nzbdav-hls",
    "/var/media/STORAGE/nzbdav-hls",
    "/storage/nzbdav-hls",
    "/tmp/nzbdav-hls",
)

# How long to wait for a segment file to appear on disk before
# declaring the fetch failed. Must exceed ffmpeg cold-start + a seek's
# worth of container parsing on the largest supported input.
_HLS_SEGMENT_WAIT_SECONDS = 90.0

# Segment file is considered complete when the next segment exists
# OR when its mtime has been stable for this many milliseconds.
_HLS_SEGMENT_MTIME_STABLE_MS = 500


def _choose_hls_workdir():
    """Return a writable base directory for HLS session working files.

    Walks the candidate list in order and returns the first entry
    whose parent exists, is writable, and has enough free space.
    Creates the leaf directory if missing. Falls back to /tmp as a
    last resort.
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
    # Fallback: OS temp dir (usually /tmp)
    import tempfile

    fallback = os.path.join(tempfile.gettempdir(), "nzbdav-hls")
    try:
        os.makedirs(fallback, exist_ok=True)
    except OSError:
        pass
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


def _get_force_remux_threshold_bytes():
    """Return the remux-force threshold in bytes, or 0 to disable."""
    try:
        import xbmcaddon

        raw = xbmcaddon.Addon().getSetting("force_remux_threshold_mb")
    except Exception:  # noqa: BLE001 — Kodi module may not exist
        raw = None
    try:
        mb = int(raw) if raw not in (None, "") else _DEFAULT_FORCE_REMUX_THRESHOLD_MB
    except (TypeError, ValueError):
        mb = _DEFAULT_FORCE_REMUX_THRESHOLD_MB
    if mb <= 0:
        return 0
    return mb * 1024 * 1024


def _get_force_remux_mode():
    """Return 'matroska' or 'hls_fmp4' for the force-remux branch.

    Empty string, unset, or '0' -> 'matroska' (default, control path).
    '1' -> 'hls_fmp4' (experimental, DV-capable).
    Any other value -> 'matroska' (safe fall-through).
    """
    try:
        import xbmcaddon

        raw = xbmcaddon.Addon().getSetting("force_remux_mode")
    except Exception:  # noqa: BLE001 — Kodi module may not exist
        return "matroska"
    return "hls_fmp4" if raw == "1" else "matroska"


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
    """Embed Basic auth credentials into a URL for ffmpeg."""
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
        if path in ("", "/stream"):
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

        sessions = getattr(self.server, "stream_sessions", {})
        ctx = sessions.get(session_id)
        if ctx is not None:
            ctx["last_access"] = time.time()
        return ctx

    @staticmethod
    def _parse_hls_resource(path):
        """Extract (session_id, resource) from an /hls/ path, or None.

        Returns a tuple ``(session_id, resource)`` where ``resource`` is
        one of ``"playlist"`` or ``("segment", N)``. Returns ``None`` for
        malformed paths so the caller can 404.
        """
        if not path.startswith("/hls/"):
            return None
        parts = path[len("/hls/") :].split("/", 1)
        if len(parts) != 2 or not parts[0]:
            return None
        session_id, resource = parts
        if resource == "playlist.m3u8":
            return session_id, "playlist"
        if resource.startswith("seg_") and resource.endswith(".ts"):
            try:
                seg_n = int(resource[len("seg_") : -len(".ts")])
            except ValueError:
                return None
            if seg_n < 0:
                return None
            return session_id, ("segment", seg_n)
        return None

    @staticmethod
    def _ctx_lock(ctx, server):
        """Get the remux lock for this stream context."""
        return ctx.get("ffmpeg_lock") or getattr(server, "ffmpeg_lock")

    def do_POST(self):
        """Handle POST /prepare — plugin sends stream config via HTTP."""
        import json

        if self.path.split("?", 1)[0] != "/prepare":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
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
            if resource == "playlist":
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Connection", "close")
                self.end_headers()
            else:
                # Segment HEAD — Kodi's HLS demuxer rarely issues these
                # but the response is harmless if it does.
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
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
        """Dispatch an /hls/<session>/... GET to playlist or segment."""
        parsed = self._parse_hls_resource(path)
        if parsed is None:
            self.send_error(404)
            return
        ctx = self._get_stream_context()
        if ctx is None or ctx.get("mode") != "hls":
            self.send_error(404)
            return
        _session_id, resource = parsed
        if resource == "playlist":
            self._serve_hls_playlist(ctx)
            return
        if isinstance(resource, tuple) and resource[0] == "segment":
            self._serve_hls_segment(ctx, resource[1])
            return
        self.send_error(404)

    @staticmethod
    def _build_ffmpeg_cmd(ctx, seek_seconds=None):
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
        input_url = _embed_auth_in_url(input_url, ctx.get("auth_header"))
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
            cmd.extend(["-sn"])
            cmd.extend(
                [
                    "-f",
                    "mpegts",
                    # +genpts rebuilds timestamps when the source's are
                    # missing or invalid (common on seek-from-middle). The
                    # TS muxer already flushes per-packet so no need for
                    # flush_packets.
                    "-fflags",
                    "+genpts",
                    "-mpegts_copyts",
                    "1",
                    "pipe:1",
                ]
            )
            return cmd

        # Subtitle handling (toggleable via setting).
        # For MP4 input we convert text subs (mov_text/TX3G) to SRT so MKV
        # output is more compatible.  For MKV input we must use `copy` —
        # PGS/DVD/HDMV bitmap subs can't be re-encoded to SRT and would
        # abort the remux; ASS/SSA/SRT all copy fine into MKV anyway.
        try:
            import xbmcaddon

            convert_subs = xbmcaddon.Addon().getSetting("proxy_convert_subs")
            if convert_subs != "false":
                src_is_mkv = input_url.split("?", 1)[0].lower().endswith(".mkv")
                sub_codec = "copy" if src_is_mkv else "srt"
                cmd.extend(["-map", "0:s?", "-c:s", sub_codec])
        except Exception:  # noqa: BLE001 — Kodi module may not exist
            pass  # outside Kodi context (tests), skip subtitle setting

        # Write duration into MKV Segment Info so Kodi knows the total
        # length.  Without this, piped MKV has no Duration element and
        # Kodi treats the stream as live (no progress bar, no seeking,
        # no pause).  -metadata DURATION= makes ffmpeg's matroska muxer
        # write the Duration element in the header.
        duration_secs = ctx.get("duration_seconds")
        if duration_secs is not None:
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

                    with urlopen(req, timeout=120) as resp:  # nosec B310
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
            parsed = self._parse_range(range_header, total_bytes or 1)
            if parsed[0] is not None:
                requested_start = parsed[0]

        seek_seconds = self._resolve_seek(ctx, requested_start, total_bytes)

        cmd = self._build_ffmpeg_cmd(ctx, seek_seconds=seek_seconds)
        xbmc.log(
            "NZB-DAV: Remuxing to MKV (seek={})".format(seek_seconds),
            xbmc.LOGINFO,
        )

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False
            )
        except OSError as e:
            xbmc.log("NZB-DAV: Failed to start ffmpeg: {}".format(e), xbmc.LOGERROR)
            _notify_error("Failed to start ffmpeg")
            self.send_error(500)
            return

        lock = self._ctx_lock(ctx, self.server)
        with lock:
            ctx["active_ffmpeg"] = proc
            ctx["current_byte_pos"] = requested_start
            self.server.active_ffmpeg = proc
            self.server.current_byte_pos = requested_start

        # Drain stderr in a background thread to prevent ffmpeg from blocking
        # when the stderr pipe buffer fills up (~64KB).  Without this, ffmpeg
        # stalls mid-stream, the proxy stops sending data, and Kodi freezes
        # once its playback buffer drains.
        # Thread safety: list.append() is atomic under CPython's GIL, and
        # stderr_thread.join() in the finally block provides a happens-before
        # guarantee before the main thread reads stderr_chunks.
        stderr_chunks = []

        def _drain_stderr():
            try:
                while True:
                    data = proc.stderr.read(4096)
                    if not data:
                        break
                    stderr_chunks.append(data)
            except (OSError, ValueError):
                pass

        stderr_thread = threading.Thread(target=_drain_stderr)
        stderr_thread.daemon = True
        stderr_thread.start()

        # Send response headers.
        # Matroska-only response. Piped MKV has no Cues so advertising
        # byte-range would only disable Kodi's cache-based fallback
        # without enabling real seek. Stay on live-stream semantics;
        # duration is still embedded in the MKV header so Kodi's
        # progress bar is accurate.
        self.send_response(200)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Accept-Ranges", "none")
        self.send_header("Connection", "close")
        self.close_connection = True  # pylint: disable=attribute-defined-outside-init
        self.end_headers()

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
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                total += len(chunk)
                with lock:
                    current_pos = requested_start + total
                    ctx["current_byte_pos"] = current_pos
                    self.server.current_byte_pos = current_pos
        except (BrokenPipeError, ConnectionResetError, _socket.timeout):
            xbmc.log(
                "NZB-DAV: Remux client disconnected after {} MB".format(
                    total // 1048576
                ),
                xbmc.LOGDEBUG,
            )
        finally:
            proc.kill()
            proc.wait()
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
        """Emit a VOD-type HLS playlist covering the full source duration."""
        duration = ctx.get("duration_seconds") or 0.0
        seg_dur = ctx.get("hls_segment_duration", _HLS_SEGMENT_SECONDS)
        if duration <= 0 or seg_dur <= 0:
            self.send_error(500)
            return

        import math

        total_segs = int(math.ceil(duration / seg_dur))
        target = int(math.ceil(seg_dur))

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-TARGETDURATION:{}".format(target),
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        for i in range(total_segs):
            start = i * seg_dur
            remaining = max(0.0, duration - start)
            this_dur = min(seg_dur, remaining)
            lines.append("#EXTINF:{:.6f},".format(this_dur))
            lines.append("seg_{}.ts".format(i))
        lines.append("#EXT-X-ENDLIST")
        body = ("\n".join(lines) + "\n").encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
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

        try:
            content_length = os.path.getsize(segment_path)
        except OSError as e:
            xbmc.log(
                "NZB-DAV: HLS seg {} stat failed: {}".format(seg_n, e),
                xbmc.LOGERROR,
            )
            self.send_error(500)
            return

        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Connection", "close")
        self.close_connection = True  # pylint: disable=attribute-defined-outside-init
        self.end_headers()

        try:
            self.connection.settimeout(_REMUX_WRITE_TIMEOUT)
        except (OSError, AttributeError):
            pass

        total = 0
        try:
            with open(segment_path, "rb") as f:
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
        input_url = _embed_auth_in_url(input_url, ctx.get("auth_header"))
        return [
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
        self.send_response(206)
        self.send_header("Content-Type", ctx["content_type"])
        self.send_header("Content-Length", str(total_bytes))
        self.send_header("Accept-Ranges", "bytes")
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
        self.close_connection = True  # pylint: disable=attribute-defined-outside-init
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
        total_skipped = 0

        try:
            while current <= end:
                written = self._stream_upstream_range(ctx, current, end)
                current += written
                if current > end:
                    return

                remaining = end - current + 1
                skip = self._find_skip_offset(ctx, current, end)

                if skip is None or total_skipped + skip > _MAX_TOTAL_ZERO_FILL:
                    xbmc.log(
                        "NZB-DAV: Zero-fill recovery exhausted at byte {} "
                        "(filling remaining {} bytes)".format(current, remaining),
                        xbmc.LOGERROR,
                    )
                    self._write_zeros(remaining)
                    return

                self._write_zeros(skip)
                total_skipped += skip
                current += skip
                xbmc.log(
                    "NZB-DAV: Zero-filled {} bytes at offset {} to skip bad "
                    "usenet articles".format(skip, current - skip),
                    xbmc.LOGWARNING,
                )
        except (BrokenPipeError, ConnectionResetError, _socket.timeout):
            # socket.timeout means Kodi stopped reading from us for longer
            # than _REMUX_WRITE_TIMEOUT — usually a long DB vacuum or the
            # decoder otherwise stalling.  Unwind the handler and let
            # BaseHTTPServer tear down the socket; Kodi's CCurlFile will
            # reconnect if it still wants bytes.
            xbmc.log(
                "NZB-DAV: Pass-through write aborted at byte {} "
                "(client stalled or disconnected)".format(current),
                xbmc.LOGWARNING,
            )

    def _stream_upstream_range(self, ctx, start, end):
        """Stream bytes from upstream to the client.

        Returns the count of bytes successfully written to the client.
        A short return indicates upstream failed or went silent; the caller
        is responsible for recovery. BrokenPipeError / ConnectionResetError
        propagate out so the caller can abort cleanly.
        """
        req = Request(ctx["remote_url"])
        req.add_header("Range", "bytes={}-{}".format(start, end))
        if ctx.get("auth_header"):
            req.add_header("Authorization", ctx["auth_header"])

        written = 0
        try:
            resp = urlopen(req, timeout=_UPSTREAM_OPEN_TIMEOUT)  # nosec B310
        except (OSError, ValueError) as e:
            xbmc.log(
                "NZB-DAV: Proxy upstream open failed at byte {}: {}".format(start, e),
                xbmc.LOGWARNING,
            )
            return 0

        try:
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
                        ),
                        xbmc.LOGWARNING,
                    )
                    return written
                if not chunk:
                    return written
                self.wfile.write(chunk)
                written += len(chunk)
        finally:
            try:
                resp.close()
            except OSError:
                pass

    def _find_skip_offset(self, ctx, failed_byte, range_end):
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
                    with urlopen(
                        req, timeout=_SKIP_PROBE_TIMEOUT
                    ) as resp:  # nosec B310
                        status = getattr(resp, "status", None) or resp.getcode()
                        if status in (200, 206):
                            resp.read(64)
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
            range_spec = range_header.replace("bytes=", "")
            if range_spec.startswith("-"):
                suffix = int(range_spec[1:])
                return content_length - suffix, content_length - 1
            parts = range_spec.split("-")
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else content_length - 1
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
        # _init_ready MUST be set here, not only in the spawn path:
        # wait_for_init reads it before the first spawn and would
        # AttributeError on a fresh session otherwise.
        self._init_ready = False
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
        """
        path = self.segment_path(seg_n)
        if not os.path.exists(path):
            return False
        next_path = self.segment_path(seg_n + 1)
        if os.path.exists(next_path):
            return True
        # Final segment (or ffmpeg briefly mid-transition) — fall back
        # to mtime stability.
        try:
            mtime = os.path.getmtime(path)
        except OSError:
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
        # Reading self._start_segment without the lock is safe:
        # int reads are atomic under the GIL, and a stale read
        # is benign — the next poll iteration converges on the
        # fresh value.
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
                except (OSError, subprocess.SubprocessError):
                    pass
            self._proc = None

            # fmp4 generation boundary: unlink both init.mp4 and the
            # new target segment file so the "seg_<start_segment>.m4s
            # exists" completeness signal in _init_file_complete is
            # unambiguously bound to the NEW ffmpeg. Do NOT blanket-
            # sweep other segments — leaving prior-generation files
            # in place preserves the backward-seek cache optimization
            # in _segment_complete.
            if self.segment_format == "fmp4":
                init_path = os.path.join(self.session_dir, "init.mp4")
                try:
                    os.unlink(init_path)
                except FileNotFoundError:
                    pass
                first_seg_path = os.path.join(
                    self.session_dir, "seg_{:06d}.m4s".format(seg_n)
                )
                try:
                    os.unlink(first_seg_path)
                except FileNotFoundError:
                    pass
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
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=self._ffmpeg_log,
                    shell=False,
                )
                self._start_segment = seg_n
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
        input_url = _embed_auth_in_url(self.remote_url, self.auth_header)

        cmd = [
            self.ffmpeg_path,
            "-v",
            "warning",
            "-probesize",
            "1048576",
            "-analyzeduration",
            "0",
            "-fflags",
            "+fastseek",
            "-ss",
            "{:.3f}".format(start_time),
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
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

        if self.segment_format == "fmp4":
            init_path = os.path.join(self.session_dir, "init.mp4")
            seg_pattern = os.path.join(self.session_dir, "seg_%06d.m4s")
            playlist_path = os.path.join(self.session_dir, "ffmpeg_playlist.m3u8")
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

    def prepare(self):
        """Eagerly spawn ffmpeg and verify it didn't immediately exit.

        Called from _register_session right after construction. For
        mpegts producers (the legacy lazy path) this is a no-op,
        preserving today's behavior. For fmp4 producers this is the
        spawn-time validation that keeps the matroska late-binding
        fallback working — without it, ffmpeg's first spawn happens
        inside wait_for_init AFTER the HLS URL has already been
        returned to Kodi, so a build that rejects fmp4 HLS would
        surface as a 504 from /hls/<sess>/init.mp4 instead of a
        clean session rewrite.

        The 500 ms poll catches argument-rejection and missing-muxer
        failures (~10-100 ms typical). It does NOT catch later
        runtime failures (codec init, input I/O after the 500 ms
        window) — those still surface via the wait_for_init /
        wait_for_segment timeout path. Consistent with the
        "experimental" label on the setting.

        Raises:
            RuntimeError: ffmpeg failed to spawn or exited within
                the poll window.
        """
        if self.segment_format != "fmp4":
            return  # mpegts is lazy-spawned, no eager validation
        self._ensure_ffmpeg_headed_for(0)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            with self._lock:
                proc = self._proc
            if proc is None:
                raise RuntimeError("ffmpeg failed to spawn — check ffmpeg.log")
            rc = proc.poll()
            if rc is not None:
                raise RuntimeError(
                    "ffmpeg exited immediately with code {} — fmp4 "
                    "HLS likely unsupported by this build".format(rc)
                )
            time.sleep(0.05)
        return  # proc still alive after 500 ms — assume healthy

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
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            self._ffmpeg_log.close()
        except OSError:
            pass
        try:
            import shutil as _shutil

            _shutil.rmtree(self.session_dir, ignore_errors=True)
        except OSError:
            pass


class StreamProxy:
    """Local HTTP proxy server for nzbdav streams."""

    def __init__(self):
        self._server = None
        self._thread = None
        self.port = 0
        self._context_lock = threading.Lock()

    def start(self):
        """Start the proxy server on a random port."""
        self._server = _ThreadedHTTPServer(("127.0.0.1", 0), _StreamHandler)
        self._server.owner_proxy = self
        self.port = self._server.server_address[1]
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
            except Exception as e:  # noqa: BLE001
                xbmc.log(
                    "NZB-DAV: HLS producer close failed: {}".format(e),
                    xbmc.LOGWARNING,
                )

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
                ffmpeg_path = _find_ffmpeg()
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
            ffmpeg_path = _find_ffmpeg() if needs_remux else None
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
                if _get_force_remux_mode() == "hls_fmp4" and duration is not None:
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
            auth_header: Optional Basic auth header; embedded into the
                URL for the child process.
        """
        _validate_url(url)
        input_url = _embed_auth_in_url(url, auth_header)

        ffprobe_path = _find_ffprobe()
        if ffprobe_path:
            result = StreamProxy._probe_duration_ffprobe(ffprobe_path, input_url)
            if result is not None:
                return result

        return StreamProxy._probe_duration_ffmpeg(ffmpeg_path, input_url)

    @staticmethod
    def _probe_duration_ffprobe(ffprobe_path, input_url):
        """Run ffprobe to get duration. Returns seconds or None."""
        try:
            proc = subprocess.Popen(
                [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nokey=1:noprint_wrappers=1",
                    input_url,
                ],
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
    def _probe_duration_ffmpeg(ffmpeg_path, input_url):
        """Parse Duration out of ``ffmpeg -i`` stderr. Returns seconds or None."""
        try:
            proc = subprocess.Popen(
                [ffmpeg_path, "-v", "info", "-i", input_url, "-f", "null", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            # Read stderr line-by-line; Duration appears in the header.
            # Kill ffmpeg as soon as we have it to avoid reading the whole file.
            collected = ""
            # 64 KB budget: large enough that a 30-subtitle Blu-ray remux's
            # wall of per-stream probe warnings can't push `Duration:` out.
            budget = 65536
            for line in proc.stderr:
                collected += line.decode(errors="replace")
                result = _parse_ffmpeg_duration(collected)
                if result is not None:
                    proc.kill()
                    proc.wait()
                    return result
                if len(collected) > budget:
                    xbmc.log(
                        "NZB-DAV: Duration not found in first {}B of ffmpeg "
                        "output".format(budget),
                        xbmc.LOGWARNING,
                    )
                    proc.kill()
                    proc.wait()
                    return None
            # 30 s: generous upper bound for ffmpeg to finish reading the file
            # header on a slow/remote source; the normal path exits early via
            # proc.kill() once Duration is found in stderr.
            proc.wait(timeout=30)
            return _parse_ffmpeg_duration(collected)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            xbmc.log("NZB-DAV: Duration probe failed: {}".format(e), xbmc.LOGWARNING)
            return None

    @staticmethod
    def _prepare_tempfile_faststart(ffmpeg_path, url, auth_header):
        """Remux MP4 with faststart to a temp file. Returns path or None."""
        import tempfile

        if not ffmpeg_path:
            return None

        _validate_url(url)
        input_url = _embed_auth_in_url(url, auth_header)
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
            "-i",
            input_url,
            "-map",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            temp_path,
        ]

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
            with urlopen(req, timeout=10) as resp:  # nosec B310
                return int(resp.headers.get("Content-Length", 0))
        except (OSError, ValueError):
            pass
        try:
            req = Request(url)
            req.add_header("Range", "bytes=-1")
            if auth_header:
                req.add_header("Authorization", auth_header)
            with urlopen(req, timeout=10) as resp:  # nosec B310
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
    except Exception:  # noqa: BLE001 — Kodi module may not exist
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
    with urlopen(req, timeout=60) as resp:  # nosec B310
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
