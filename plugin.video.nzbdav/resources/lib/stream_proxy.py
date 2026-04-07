# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Local HTTP proxy for nzbdav WebDAV streams.

For MP4 files, remuxes on the fly to MKV using ffmpeg (-c copy, no
re-encoding).  This bypasses a Kodi CFileCache bug where parsing large
MP4 moov atoms over HTTP fails with 'corrupted STCO atom'.

For MKV and other files, proxies range requests directly to the remote
WebDAV server with proper 206 responses.
"""

import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn as _ThreadingMixIn
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
        _http_range,
        build_faststart_layout,
        fetch_remote_mp4_layout,
    )
except Exception:  # noqa: BLE001
    RangeCache = None  # type: ignore[assignment,misc]
    _http_range = None  # type: ignore[assignment]
    build_faststart_layout = None  # type: ignore[assignment]
    fetch_remote_mp4_layout = None  # type: ignore[assignment]

# Singleton proxy instance
_proxy = None
_proxy_lock = threading.Lock()

# Common ffmpeg paths on CoreELEC / LibreELEC
_FFMPEG_PATHS = [
    "ffmpeg",
    "/storage/.kodi/addons.bak/tools.ffmpeg-tools/bin/ffmpeg",
    "/storage/.kodi/addons/tools.ffmpeg-tools/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    "/storage/.opt/bin/ffmpeg",
]


def _find_ffmpeg():
    """Find an ffmpeg binary on the system."""
    for path in _FFMPEG_PATHS:
        found = shutil.which(path)
        if found:
            return found
    return None


def _validate_url(url):
    """Reject URLs with unexpected schemes to prevent command injection."""
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError("Invalid URL scheme: {}".format(repr(url)[:30]))


def _embed_auth_in_url(url, auth_header):
    """Embed Basic auth credentials into a URL for ffmpeg."""
    if auth_header and auth_header.startswith("Basic "):
        import base64

        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        return url.replace("://", "://{}@".format(decoded), 1)
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

    def do_POST(self):
        """Handle POST /prepare — plugin sends stream config via HTTP."""
        import json

        if "/prepare" not in self.path:
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
        proxy_url, stream_info = proxy.prepare_stream(remote_url, auth_header)

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
        ctx = self.server.stream_context
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
        ctx = self.server.stream_context
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

    @staticmethod
    def _build_ffmpeg_cmd(ctx, seek_seconds=None):
        """Build the ffmpeg remux command list."""
        ffmpeg = ctx["ffmpeg_path"]
        input_url = ctx["remote_url"]
        _validate_url(input_url)
        input_url = _embed_auth_in_url(input_url, ctx.get("auth_header"))

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

        # Subtitle conversion (toggleable via setting)
        try:
            import xbmcaddon

            convert_subs = xbmcaddon.Addon().getSetting("proxy_convert_subs")
            if convert_subs != "false":
                cmd.extend(["-map", "0:s?", "-c:s", "srt"])
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
        except Exception as e:
            xbmc.log("NZB-DAV: Faststart proxy error: {}".format(e), xbmc.LOGERROR)

    def _serve_temp_faststart(self, ctx):
        """Serve a temp-file faststart MP4 with range support."""
        import os

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
        except Exception as e:
            xbmc.log("NZB-DAV: Temp faststart error: {}".format(e), xbmc.LOGERROR)

    def _serve_remux(self, ctx):
        """Remux MP4 to MKV on the fly, with optional seeking."""
        total_bytes = ctx.get("total_bytes", 0)
        duration = ctx.get("duration_seconds")
        seekable = ctx.get("seekable", False)

        # Parse range request
        range_header = self.headers.get("Range")
        requested_start = 0
        if range_header:
            parsed = self._parse_range(range_header, total_bytes or 1)
            if parsed[0] is not None:
                requested_start = parsed[0]

        # Calculate seek position for any non-zero Range request when we have
        # duration info.  This covers both explicit seeks (user skips ahead)
        # and continuations (Kodi reconnects mid-stream after a broken pipe).
        # Each GET spawns a fresh ffmpeg, so we must always tell it where to
        # start — otherwise continuations would restart from byte 0.
        seek_seconds = None
        if seekable and duration is not None and total_bytes and requested_start > 0:
            seek_seconds = (requested_start / total_bytes) * duration

        with self.server.ffmpeg_lock:
            current_pos = self.server.current_byte_pos
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
                # Kill existing ffmpeg (may still be alive from a prior GET)
                if self.server.active_ffmpeg:
                    try:
                        self.server.active_ffmpeg.kill()
                        self.server.active_ffmpeg.wait()
                    except OSError:
                        pass
                    self.server.active_ffmpeg = None

        cmd = self._build_ffmpeg_cmd(ctx, seek_seconds=seek_seconds)
        xbmc.log(
            "NZB-DAV: Remuxing MP4->MKV (seek={})".format(seek_seconds),
            xbmc.LOGINFO,
        )

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False
            )
        except OSError as e:
            xbmc.log("NZB-DAV: Failed to start ffmpeg: {}".format(e), xbmc.LOGERROR)
            self.send_error(500)
            return

        with self.server.ffmpeg_lock:
            self.server.active_ffmpeg = proc
            self.server.current_byte_pos = requested_start

        # Send response headers.
        # Do NOT advertise Accept-Ranges: bytes — the piped MKV has no Cues
        # (seek index), so Kodi's demuxer cannot seek by byte offset and will
        # hang trying.  Duration is embedded via -metadata DURATION= in the
        # MKV header, which gives Kodi a correct progress bar.  Seeking is
        # handled by stopping and restarting playback with a new -ss offset.
        self.send_response(200)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Accept-Ranges", "none")
        self.send_header("Connection", "close")
        self.end_headers()

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
                with self.server.ffmpeg_lock:
                    self.server.current_byte_pos = requested_start + total
        except (BrokenPipeError, ConnectionResetError):
            xbmc.log(
                "NZB-DAV: Remux client disconnected after {} MB".format(
                    total // 1048576
                ),
                xbmc.LOGDEBUG,
            )
        finally:
            proc.kill()
            proc.wait()
            stderr = proc.stderr.read().decode(errors="replace")
            if stderr.strip():
                xbmc.log("NZB-DAV: ffmpeg: {}".format(stderr[:300]), xbmc.LOGDEBUG)
            xbmc.log(
                "NZB-DAV: Remux done: {} MB sent".format(total // 1048576),
                xbmc.LOGINFO,
            )

    def _serve_proxy(self, ctx):
        """Proxy range requests directly to remote."""
        content_length = ctx["content_length"]
        range_header = self.headers.get("Range")

        if range_header:
            start, end = self._parse_range(range_header, content_length)
            if start is None:
                self.send_error(416)
                return
        else:
            start, end = 0, content_length - 1

        try:
            req = Request(ctx["remote_url"])
            req.add_header("Range", "bytes={}-{}".format(start, end))
            if ctx.get("auth_header"):
                req.add_header("Authorization", ctx["auth_header"])

            # 2-minute timeout: large WebDAV files over a local LAN can be
            # slow to begin transferring; 120 s avoids a premature error while
            # still bounding the hang if the server goes silent mid-stream.
            with urlopen(req, timeout=120) as resp:  # nosec B310
                self.send_response(206)
                self.send_header("Content-Type", ctx["content_type"])
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header(
                    "Content-Range",
                    "bytes {}-{}/{}".format(start, end, content_length),
                )
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                while True:
                    # 1 MB: balances memory pressure and the number of write()
                    # syscalls when proxying large files.
                    chunk = resp.read(1048576)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError) as e:
            xbmc.log("NZB-DAV: Proxy range failed: {}".format(e), xbmc.LOGERROR)

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
        self.active_ffmpeg = None
        self.current_byte_pos = 0
        self.ffmpeg_lock = threading.Lock()
        self.owner_proxy = None
        super().__init__(*args, **kwargs)


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
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def prepare_stream(self, remote_url, auth_header=None):
        """Set up proxy for a new stream.

        Returns (local_proxy_url, stream_info_dict).
        stream_info_dict contains duration_seconds, total_bytes, seekable, remux,
        faststart, and virtual_size.
        """
        content_type = self._detect_content_type(remote_url)
        lower_url = remote_url.lower()
        is_mp4 = lower_url.endswith((".mp4", ".m4v"))

        if is_mp4:
            content_length = self._get_content_length(remote_url, auth_header)

            # Tier 1: Try virtual moov-relocation (pure Python, no temp files)
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
                else:
                    xbmc.log(
                        "NZB-DAV: MP4 layout fetch returned None",
                        xbmc.LOGWARNING,
                    )
                    faststart = None
            except Exception as e:
                xbmc.log(
                    "NZB-DAV: MP4 faststart parse failed: {}".format(e), xbmc.LOGWARNING
                )
                faststart = None

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
                    import os

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
            ctx = {
                "remote_url": remote_url,
                "auth_header": auth_header,
                "content_length": content_length,
                "content_type": content_type,
                "remux": False,
            }

        with self._context_lock:
            self._server.stream_context = ctx
        local_url = "http://127.0.0.1:{}/stream".format(self.port)
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
            "seekable": ctx.get("seekable", False)
            or ctx.get("faststart", False)
            or ctx.get("temp_faststart", False),
            "remux": ctx.get("remux", False),
            "faststart": ctx.get("faststart", False),
        }
        return local_url, stream_info

    @staticmethod
    def _probe_duration(ffmpeg_path, url, auth_header):
        """Probe file duration using ffmpeg. Returns seconds or None."""
        _validate_url(url)
        input_url = url
        input_url = _embed_auth_in_url(input_url, auth_header)

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
            for line in proc.stderr:
                collected += line.decode(errors="replace")
                result = _parse_ffmpeg_duration(collected)
                if result is not None:
                    proc.kill()
                    proc.wait()
                    return result
                # Safety: if we've read too much stderr without finding
                # Duration, the header is missing — bail out before ffmpeg
                # decodes the entire file.
                if len(collected) > 8192:
                    xbmc.log(
                        "NZB-DAV: Duration not found in first 8KB of ffmpeg output",
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
        import os
        import tempfile

        if not ffmpeg_path:
            return None

        _validate_url(url)
        input_url = _embed_auth_in_url(url, auth_header)
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, "nzbdav_faststart.mp4")

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
