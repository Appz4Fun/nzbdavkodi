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


class _StreamHandler(BaseHTTPRequestHandler):
    """HTTP handler that remuxes MP4 to MKV or proxies other formats."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # pylint: disable=arguments-renamed
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
        except Exception:
            self.send_error(400)
            return

        remote_url = data.get("remote_url", "")
        auth_header = data.get("auth_header")
        if not remote_url:
            self.send_error(400)
            return

        proxy = self.server.owner_proxy
        proxy_url = proxy.prepare_stream(remote_url, auth_header)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        resp = json.dumps({"proxy_url": proxy_url}).encode()
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_HEAD(self):
        ctx = self.server.stream_context
        if ctx is None:
            self.send_error(404)
            return
        if ctx.get("remux"):
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
        ctx = self.server.stream_context
        if ctx is None:
            self.send_error(404)
            return

        if ctx.get("remux"):
            self._serve_remux(ctx)
        else:
            self._serve_proxy(ctx)

    def _serve_remux(self, ctx):
        """Remux MP4 to MKV on the fly using ffmpeg -c copy."""
        ffmpeg = ctx["ffmpeg_path"]
        remote_url = ctx["remote_url"]

        # Build ffmpeg input URL with auth if needed
        input_url = remote_url
        if ctx.get("auth_header"):
            # Embed basic auth in the URL for ffmpeg
            auth = ctx["auth_header"]
            if auth.startswith("Basic "):
                import base64

                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                # Insert user:pass into URL
                input_url = remote_url.replace("://", "://{}@".format(decoded), 1)

        xbmc.log(
            "NZB-DAV: Remuxing MP4->MKV: {}".format(remote_url[:80]),
            xbmc.LOGINFO,
        )

        cmd = [
            ffmpeg,
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
            "-c",
            "copy",
            "-f",
            "matroska",
            "-fflags",
            "+genpts+flush_packets",
            "pipe:1",
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception as e:
            xbmc.log("NZB-DAV: Failed to start ffmpeg: {}".format(e), xbmc.LOGERROR)
            self.send_error(500)
            return

        self.send_response(200)
        self.send_header("Content-Type", "video/x-matroska")
        self.send_header("Accept-Ranges", "none")
        self.send_header("Connection", "close")
        self.end_headers()

        total = 0
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                total += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            xbmc.log(
                "NZB-DAV: Remux client disconnected after {} MB".format(
                    total // 1048576
                ),
                xbmc.LOGDEBUG,
            )
        finally:
            proc.kill()
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

            with urlopen(req, timeout=120) as resp:
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
                    chunk = resp.read(1048576)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            xbmc.log("NZB-DAV: Proxy range failed: {}".format(e), xbmc.LOGERROR)

    def _parse_range(self, range_header, content_length):
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
        self._server.stream_context = None
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
        """Set up proxy for a new stream. Returns the local proxy URL."""
        content_type = self._detect_content_type(remote_url)
        lower_url = remote_url.lower()
        is_mp4 = lower_url.endswith((".mp4", ".m4v"))

        ffmpeg_path = _find_ffmpeg() if is_mp4 else None
        use_remux = is_mp4 and ffmpeg_path is not None

        if use_remux:
            content_length = self._get_content_length(remote_url, auth_header)
            duration = self._probe_duration(ffmpeg_path, remote_url, auth_header)
            seekable = duration is not None and content_length > 0
            ctx = {
                "remote_url": remote_url,
                "auth_header": auth_header,
                "content_type": "video/x-matroska",
                "remux": True,
                "ffmpeg_path": ffmpeg_path,
                "total_bytes": content_length,
                "duration_seconds": duration,
                "seekable": seekable,
            }
            xbmc.log(
                "NZB-DAV: Will remux MP4->MKV via {} (seekable={}, duration={})".format(
                    ffmpeg_path, seekable, duration
                ),
                xbmc.LOGINFO,
            )
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
            "NZB-DAV: Proxy ready (remux={}): {}".format(use_remux, local_url),
            xbmc.LOGINFO,
        )
        return local_url

    def _probe_duration(self, ffmpeg_path, url, auth_header):
        """Probe file duration using ffmpeg. Returns seconds or None."""
        input_url = url
        if auth_header and auth_header.startswith("Basic "):
            import base64

            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            input_url = url.replace("://", "://{}@".format(decoded), 1)

        try:
            proc = subprocess.Popen(
                [ffmpeg_path, "-v", "warning", "-i", input_url, "-f", "null", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, stderr = proc.communicate(timeout=120)
            return _parse_ffmpeg_duration(stderr.decode(errors="replace"))
        except Exception as e:
            xbmc.log("NZB-DAV: Duration probe failed: {}".format(e), xbmc.LOGWARNING)
            return None

    def _get_content_length(self, url, auth_header):
        """Get file size via HEAD or range probe."""
        req = Request(url, method="HEAD")
        if auth_header:
            req.add_header("Authorization", auth_header)
        try:
            with urlopen(req, timeout=10) as resp:
                return int(resp.headers.get("Content-Length", 0))
        except Exception:
            pass
        try:
            req = Request(url)
            req.add_header("Range", "bytes=-1")
            if auth_header:
                req.add_header("Authorization", auth_header)
            with urlopen(req, timeout=10) as resp:
                cr = resp.headers.get("Content-Range", "")
                return int(cr.split("/")[1]) if "/" in cr else 0
        except Exception:
            return 0

    def _detect_content_type(self, url):
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
    except Exception:
        return 0


def prepare_stream_via_service(port, remote_url, auth_header=None):
    """Ask the service's proxy to prepare a stream. Returns the proxy URL."""
    import json

    url = "http://127.0.0.1:{}/prepare".format(port)
    data = json.dumps({"remote_url": remote_url, "auth_header": auth_header})
    req = Request(url, data=data.encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
        return result["proxy_url"]


def get_proxy():
    """Get or create the singleton stream proxy."""
    global _proxy  # pylint: disable=global-statement
    with _proxy_lock:
        if _proxy is None:
            _proxy = StreamProxy()
            _proxy.start()
        return _proxy
