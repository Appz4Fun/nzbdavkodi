# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Local HTTP proxy for nzbdav WebDAV streams.

For MP4 files with moov atom at the end, performs on-the-fly faststart:
serves ftyp + moov + mdat so Kodi can stream sequentially without seeking.
Adjusts stco/co64 chunk offsets in the moov to account for relocation.

For MKV and other files, proxies directly with keep-alive support.
"""

import struct
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn as _ThreadingMixIn
from urllib.request import Request, urlopen

import xbmc

# Singleton proxy instance
_proxy = None
_proxy_lock = threading.Lock()


def _read_box_header(data, offset):
    """Read an MP4 box header at the given offset.

    Returns (box_type, box_size, header_size) or (None, 0, 0) on failure.
    """
    if offset + 8 > len(data):
        return None, 0, 0
    size = struct.unpack(">I", data[offset : offset + 4])[0]
    box_type = data[offset + 4 : offset + 8]
    header_size = 8
    if size == 1:
        if offset + 16 > len(data):
            return None, 0, 0
        size = struct.unpack(">Q", data[offset + 8 : offset + 16])[0]
        header_size = 16
    return box_type, size, header_size


def _adjust_moov_offsets(moov_data, offset_delta):
    """Adjust stco/co64 chunk offsets in a moov atom by offset_delta.

    Walks the moov box tree to find all stco and co64 atoms and adjusts
    their chunk offset entries. Returns the modified moov data.
    """
    data = bytearray(moov_data)
    _adjust_box_recursive(data, 0, len(data), offset_delta)
    return bytes(data)


def _adjust_box_recursive(data, start, end, delta):
    """Recursively walk MP4 boxes and adjust stco/co64."""
    pos = start
    while pos < end:
        if pos + 8 > end:
            break
        size = struct.unpack(">I", data[pos : pos + 4])[0]
        box_type = data[pos + 4 : pos + 8]
        header_size = 8

        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            header_size = 16
        elif size == 0:
            size = end - pos

        if size < header_size or pos + size > end:
            break

        if box_type == b"stco":
            # stco: version(1) + flags(3) + entry_count(4) + entries(4 each)
            content_start = pos + header_size
            if content_start + 8 <= pos + size:
                entry_count = struct.unpack(
                    ">I", data[content_start + 4 : content_start + 8]
                )[0]
                offset_pos = content_start + 8
                for _ in range(entry_count):
                    if offset_pos + 4 > pos + size:
                        break
                    old_val = struct.unpack(">I", data[offset_pos : offset_pos + 4])[0]
                    new_val = old_val + delta
                    struct.pack_into(">I", data, offset_pos, new_val)
                    offset_pos += 4

        elif box_type == b"co64":
            # co64: version(1) + flags(3) + entry_count(4) + entries(8 each)
            content_start = pos + header_size
            if content_start + 8 <= pos + size:
                entry_count = struct.unpack(
                    ">I", data[content_start + 4 : content_start + 8]
                )[0]
                offset_pos = content_start + 8
                for _ in range(entry_count):
                    if offset_pos + 8 > pos + size:
                        break
                    old_val = struct.unpack(">Q", data[offset_pos : offset_pos + 8])[0]
                    new_val = old_val + delta
                    struct.pack_into(">Q", data, offset_pos, new_val)
                    offset_pos += 8

        elif box_type in (
            b"moov",
            b"trak",
            b"mdia",
            b"minf",
            b"stbl",
            b"edts",
            b"udta",
            b"mvex",
        ):
            # Container box — recurse into children
            _adjust_box_recursive(data, pos + header_size, pos + size, delta)

        pos += size


class _StreamHandler(BaseHTTPRequestHandler):
    """HTTP handler that serves streams, with MP4 faststart support."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # pylint: disable=arguments-renamed
        pass

    def do_HEAD(self):
        ctx = self.server.stream_context
        if ctx is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctx["content_type"])
        self.send_header("Content-Length", str(ctx["virtual_length"]))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def do_GET(self):
        ctx = self.server.stream_context
        if ctx is None:
            self.send_error(404)
            return

        if ctx.get("faststart"):
            self._serve_faststart(ctx)
        else:
            self._serve_passthrough(ctx)

    def _serve_faststart(self, ctx):
        """Serve MP4 with moov relocated to the beginning (faststart).

        Layout: ftyp + moov(adjusted) + mdat
        All served sequentially — no seeking required by Kodi.
        """
        ftyp = ctx["ftyp_data"]
        moov = ctx["moov_adjusted"]
        mdat_offset = ctx["mdat_offset"]
        mdat_size = ctx["mdat_size"]
        total_length = ctx["virtual_length"]

        range_header = self.headers.get("Range")
        if range_header and not range_header.startswith("bytes=0-"):
            # For non-initial range requests, serve from appropriate section
            start, end = self._parse_range(range_header, total_length)
            if start is None:
                self.send_error(416)
                return
            self._serve_faststart_range(ctx, start, end)
            return

        # Serve full file: ftyp + moov + mdat (streamed)
        self.send_response(200)
        self.send_header("Content-Type", ctx["content_type"])
        self.send_header("Content-Length", str(total_length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        mdat_prefetch = ctx.get("mdat_prefetch", b"")
        prefetch_len = len(mdat_prefetch)

        try:
            # 1. Write ftyp
            self.wfile.write(ftyp)

            # 2. Write adjusted moov
            self.wfile.write(moov)

            # 3. Write pre-fetched mdat head (zero gap after moov)
            if mdat_prefetch:
                self.wfile.write(mdat_prefetch)

            # 4. Stream remainder of mdat from remote
            remaining_offset = mdat_offset + prefetch_len
            remaining_end = mdat_offset + mdat_size - 1
            if remaining_offset <= remaining_end:
                req = Request(ctx["remote_url"])
                req.add_header(
                    "Range",
                    "bytes={}-{}".format(remaining_offset, remaining_end),
                )
                if ctx.get("auth_header"):
                    req.add_header("Authorization", ctx["auth_header"])

                with urlopen(req, timeout=60) as resp:
                    while True:
                        chunk = resp.read(1048576)
                        if not chunk:
                            break
                        try:
                            self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            break
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            xbmc.log("NZB-DAV: Faststart stream failed: {}".format(e), xbmc.LOGERROR)

    def _serve_faststart_range(self, ctx, start, end):
        """Serve a range within the faststarted layout."""
        ftyp = ctx["ftyp_data"]
        moov = ctx["moov_adjusted"]
        total_length = ctx["virtual_length"]
        ftyp_len = len(ftyp)
        moov_len = len(moov)
        header_len = ftyp_len + moov_len

        length = end - start + 1

        self.send_response(206)
        self.send_header("Content-Type", ctx["content_type"])
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header(
            "Content-Range",
            "bytes {}-{}/{}".format(start, end, total_length),
        )
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            pos = start
            remaining = length

            # Serve from ftyp if position overlaps
            if pos < ftyp_len and remaining > 0:
                chunk = ftyp[pos : pos + remaining]
                self.wfile.write(chunk)
                remaining -= len(chunk)
                pos += len(chunk)

            # Serve from moov if position overlaps
            if pos < header_len and remaining > 0:
                moov_start = pos - ftyp_len
                chunk = moov[moov_start : moov_start + remaining]
                self.wfile.write(chunk)
                remaining -= len(chunk)
                pos += len(chunk)

            # Serve from mdat (remote) if position overlaps
            if pos >= header_len and remaining > 0:
                remote_start = ctx["mdat_offset"] + (pos - header_len)
                remote_end = remote_start + remaining - 1

                req = Request(ctx["remote_url"])
                req.add_header("Range", "bytes={}-{}".format(remote_start, remote_end))
                if ctx.get("auth_header"):
                    req.add_header("Authorization", ctx["auth_header"])

                with urlopen(req, timeout=30) as resp:
                    while remaining > 0:
                        chunk = resp.read(min(1048576, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            xbmc.log("NZB-DAV: Faststart range failed: {}".format(e), xbmc.LOGERROR)

    def _serve_passthrough(self, ctx):
        """Pass through non-MP4 streams directly."""
        content_length = ctx["virtual_length"]
        range_header = self.headers.get("Range")

        if range_header:
            start, end = self._parse_range(range_header, content_length)
            if start is None:
                self.send_error(416)
                return
            if start == 0 and end >= content_length - 1:
                self._proxy_full(ctx)
            else:
                self._proxy_range(ctx, start, end, content_length)
        else:
            self._proxy_full(ctx)

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

    def _proxy_full(self, ctx):
        """Stream the full file from remote."""
        try:
            req = Request(ctx["remote_url"])
            if ctx.get("auth_header"):
                req.add_header("Authorization", ctx["auth_header"])

            self.send_response(200)
            self.send_header("Content-Type", ctx["content_type"])
            self.send_header("Content-Length", str(ctx["virtual_length"]))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            with urlopen(req, timeout=30) as resp:
                while True:
                    chunk = resp.read(1048576)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
        except Exception as e:
            xbmc.log("NZB-DAV: Proxy full stream failed: {}".format(e), xbmc.LOGERROR)

    def _proxy_range(self, ctx, start, end, content_length):
        """Proxy a range request to remote."""
        try:
            req = Request(ctx["remote_url"])
            req.add_header("Range", "bytes={}-{}".format(start, end))
            if ctx.get("auth_header"):
                req.add_header("Authorization", ctx["auth_header"])

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

            with urlopen(req, timeout=30) as resp:
                while True:
                    chunk = resp.read(1048576)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as e:
            xbmc.log("NZB-DAV: Proxy range failed: {}".format(e), xbmc.LOGERROR)


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
        self._server.stream_context = (
            None  # pylint: disable=attribute-defined-outside-init
        )
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

        For MP4 files, performs faststart (moov relocation) so Kodi
        can stream without seeking. Returns the local proxy URL.
        """
        content_length = self._get_content_length(remote_url, auth_header)
        content_type = self._detect_content_type(remote_url)

        lower_url = remote_url.lower()
        is_mp4 = lower_url.endswith((".mp4", ".m4v"))

        ctx = {
            "remote_url": remote_url,
            "auth_header": auth_header,
            "content_length": content_length,
            "content_type": content_type,
            "virtual_length": content_length,
            "faststart": False,
        }

        if is_mp4 and content_length > 0:
            self._prepare_faststart(ctx, remote_url, auth_header, content_length)

        with self._context_lock:
            self._server.stream_context = (
                ctx  # pylint: disable=attribute-defined-outside-init
            )
        local_url = "http://127.0.0.1:{}/stream".format(self.port)
        xbmc.log(
            "NZB-DAV: Proxy ready (faststart={}): {}".format(
                ctx["faststart"], local_url
            ),
            xbmc.LOGINFO,
        )
        return local_url

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

    def _prepare_faststart(self, ctx, url, auth_header, content_length):
        """Read ftyp + moov, adjust offsets, set up faststart context."""
        # 1. Read first 64 bytes to get ftyp box
        ftyp_data = self._range_read(url, auth_header, 0, 63)
        if not ftyp_data or len(ftyp_data) < 8:
            return

        ftyp_type, ftyp_size, _ = _read_box_header(ftyp_data, 0)
        if ftyp_type != b"ftyp":
            xbmc.log("NZB-DAV: Not an ftyp box, skipping faststart", xbmc.LOGWARNING)
            return

        # Re-read the exact ftyp
        ftyp_data = self._range_read(url, auth_header, 0, ftyp_size - 1)
        if not ftyp_data:
            return

        # 2. Read the box header after ftyp to find mdat
        next_header = self._range_read(url, auth_header, ftyp_size, ftyp_size + 15)
        if not next_header:
            return
        mdat_type, mdat_size, mdat_header_size = _read_box_header(next_header, 0)
        if mdat_type != b"mdat":
            xbmc.log(
                "NZB-DAV: Expected mdat after ftyp, got {}".format(mdat_type),
                xbmc.LOGWARNING,
            )
            return

        mdat_offset = ftyp_size
        # mdat_size includes its header

        # 3. moov should be right after mdat
        moov_offset = mdat_offset + mdat_size
        moov_size = content_length - moov_offset

        if moov_size <= 0 or moov_size > 100000000:  # sanity: max 100MB moov
            xbmc.log(
                "NZB-DAV: Unexpected moov size {}, skipping faststart".format(
                    moov_size
                ),
                xbmc.LOGWARNING,
            )
            return

        # 4. Pre-fetch the moov
        xbmc.log(
            "NZB-DAV: Faststart: ftyp={}B mdat={}B@{} moov={}B@{}".format(
                ftyp_size, mdat_size, mdat_offset, moov_size, moov_offset
            ),
            xbmc.LOGINFO,
        )

        moov_data = self._range_read(url, auth_header, moov_offset, content_length - 1)
        if not moov_data:
            xbmc.log(
                "NZB-DAV: Failed to read moov, skipping faststart", xbmc.LOGWARNING
            )
            return

        # Verify it's actually a moov
        moov_type, _, _ = _read_box_header(moov_data, 0)
        if moov_type != b"moov":
            xbmc.log(
                "NZB-DAV: Expected moov, got {}, skipping faststart".format(moov_type),
                xbmc.LOGWARNING,
            )
            return

        # 5. Adjust chunk offsets: moov moves from end to right after ftyp
        # In new layout: ftyp(ftyp_size) + moov(moov_size) + mdat(mdat_size)
        # mdat moves from offset ftyp_size to offset ftyp_size + moov_size
        # So all chunk offsets increase by moov_size
        offset_delta = len(moov_data)
        xbmc.log(
            "NZB-DAV: Adjusting chunk offsets by {} bytes".format(offset_delta),
            xbmc.LOGINFO,
        )
        moov_adjusted = _adjust_moov_offsets(moov_data, offset_delta)

        # 6. Set up faststart context
        # Virtual layout: ftyp + moov + mdat
        virtual_length = ftyp_size + len(moov_adjusted) + mdat_size

        # 7. Pre-fetch the first 5MB of mdat so ffmpeg can read frames
        # immediately after moov parsing (no gap waiting for nzbdav connection)
        mdat_prefetch_size = min(5242880, mdat_size)
        mdat_prefetch = self._range_read(
            url, auth_header, mdat_offset, mdat_offset + mdat_prefetch_size - 1
        )
        if mdat_prefetch:
            xbmc.log(
                "NZB-DAV: Pre-fetched {} bytes of mdat head".format(len(mdat_prefetch)),
                xbmc.LOGINFO,
            )
        else:
            mdat_prefetch = b""

        ctx["faststart"] = True
        ctx["ftyp_data"] = ftyp_data
        ctx["moov_adjusted"] = moov_adjusted
        ctx["mdat_offset"] = mdat_offset  # original mdat position in remote file
        ctx["mdat_size"] = mdat_size
        ctx["mdat_prefetch"] = mdat_prefetch
        ctx["virtual_length"] = virtual_length

        xbmc.log(
            "NZB-DAV: Faststart ready: virtual_length={} (orig={})".format(
                virtual_length, content_length
            ),
            xbmc.LOGINFO,
        )

    def _range_read(self, url, auth_header, start, end):
        """Read a byte range from the remote URL."""
        try:
            req = Request(url)
            req.add_header("Range", "bytes={}-{}".format(start, end))
            if auth_header:
                req.add_header("Authorization", auth_header)
            with urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as e:
            xbmc.log(
                "NZB-DAV: Range read failed ({}-{}): {}".format(start, end, e),
                xbmc.LOGERROR,
            )
            return None


def get_proxy():
    """Get or create the singleton stream proxy."""
    global _proxy  # pylint: disable=global-statement
    with _proxy_lock:
        if _proxy is None:
            _proxy = StreamProxy()
            _proxy.start()
        return _proxy
