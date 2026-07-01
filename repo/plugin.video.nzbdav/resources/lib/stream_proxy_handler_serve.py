# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""MP4 faststart / temp-file serving, seek resolution, and remux serving.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _ServeMixin:  # pylint: disable=too-few-public-methods
    """MP4 faststart / temp-file serving, seek resolution, and remux serving."""

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
        _sp._validate_url(input_url)
        auth_args = _sp._ffmpeg_auth_args(ctx.get("auth_header"))
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

        range_header = self.headers.get("Range")
        if range_header:
            start, end = self._parse_range(range_header, virtual_size)
            if start is None:
                self.send_error(416)
                return
        else:
            start, end = 0, virtual_size - 1

        length = self._send_mp4_range_headers(range_header, start, end, virtual_size)

        bytes_sent = 0
        pos = start
        try:
            while bytes_sent < length:
                remaining = length - bytes_sent

                if pos < header_len:
                    # Serve from cached header (ftyp + moov)
                    sent = self._faststart_write_header(
                        header_data, header_len, pos, remaining
                    )
                    bytes_sent += sent
                    pos += sent
                elif pos < header_len + payload_size:
                    bytes_sent, pos = self._faststart_stream_payload(
                        ctx,
                        header_len,
                        payload_remote_start,
                        payload_size,
                        length,
                        bytes_sent,
                        pos,
                    )
                    break  # done streaming
                else:
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError, _sp.HTTPException) as e:
            _sp.xbmc.log(
                "NZB-DAV: Faststart proxy error: {}".format(e), _sp.xbmc.LOGERROR
            )
            _sp._notify_error(e)

    def _faststart_write_header(self, header_data, header_len, pos, remaining):
        """Write the cached ftyp+moov header slice; return bytes written."""
        chunk_end = min(header_len, pos + remaining)
        self.wfile.write(header_data[pos:chunk_end])
        return chunk_end - pos

    def _faststart_stream_payload(
        self,
        ctx,
        header_len,
        payload_remote_start,
        payload_size,
        length,
        bytes_sent,
        pos,
    ):
        """Stream the remaining payload over a single range connection.

        One HTTP range request for the entire remaining payload, then stream
        chunks through to Kodi. This avoids per-chunk connection overhead that
        causes slow seeking. Returns the updated ``(bytes_sent, pos)``.
        """
        payload_offset = pos - header_len
        remote_pos = payload_remote_start + payload_offset
        payload_remaining = length - bytes_sent
        remote_end = min(
            payload_remote_start + payload_size - 1,
            remote_pos + payload_remaining - 1,
        )

        req = _sp.Request(ctx["remote_url"])
        _sp._add_request_headers(req, ctx.get("auth_header"))
        req.add_header("Range", "bytes={}-{}".format(remote_pos, remote_end))

        # nosemgrep
        with _sp.urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
            req, timeout=120
        ) as resp:
            while bytes_sent < length:
                chunk = resp.read(1048576)  # 1 MB read buffer
                if not chunk:
                    break
                remaining = length - bytes_sent
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                self.wfile.write(chunk)
                bytes_sent += len(chunk)
                pos += len(chunk)
        return bytes_sent, pos

    def _send_mp4_range_headers(self, range_header, start, end, total_size):
        """Send the status line + standard MP4 range headers.

        Emits 206 + Content-Range when range_header is set, else 200.
        Returns the byte length (end - start + 1) to be served.
        """
        length = end - start + 1
        if range_header:
            self.send_response(206)
            self.send_header(
                "Content-Range",
                "bytes {}-{}/{}".format(start, end, total_size),
            )
        else:
            self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        return length

    def _serve_temp_faststart(self, ctx):
        """Serve a temp-file faststart MP4 with range support."""
        temp_path = ctx["temp_path"]
        if not _sp.os.path.exists(temp_path):
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

        length = self._send_mp4_range_headers(range_header, start, end, file_size)
        self._stream_temp_file_range(temp_path, start, length)

    def _stream_temp_file_range(self, temp_path, start, length):
        """Stream ``length`` bytes from ``temp_path`` starting at ``start``."""
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
            _sp.xbmc.log(
                "NZB-DAV: Temp faststart error: {}".format(e), _sp.xbmc.LOGERROR
            )
            _sp._notify_error(e)

    def _resolve_seek(self, ctx, requested_start, total_bytes):
        """Compute seek position and kill prior ffmpeg if needed.

        Returns the seek offset in seconds, or None.
        """
        seekable = ctx.get("seekable", False)

        seek_seconds = None

        lock = self._ctx_lock(ctx, self.server)
        with lock:
            current_pos = ctx.get(
                "current_byte_pos", getattr(self.server, "current_byte_pos", 0)
            )
            is_seek = (
                seek_seconds is not None
                and seekable
                and requested_start > 0
                and _sp._is_seek_request(current_pos, requested_start)
            )
            if is_seek:
                _sp.xbmc.log(
                    "NZB-DAV: Seek to byte {} -> {:.1f}s".format(
                        requested_start, seek_seconds
                    ),
                    _sp.xbmc.LOGINFO,
                )
                self._kill_active_ffmpeg_for_seek(ctx)

        return seek_seconds

    def _kill_active_ffmpeg_for_seek(self, ctx):
        """Kill + async-reap the tracked ffmpeg before a seek respawn."""
        active_ffmpeg = ctx.get(
            "active_ffmpeg", getattr(self.server, "active_ffmpeg", None)
        )
        if not active_ffmpeg:
            return
        # kill() is cheap, but wait(timeout=2) under the session lock adds
        # visible latency to every seek. Send the signal now, clear the
        # tracked handle below, and reap the old child on a daemon thread
        # while the replacement ffmpeg can spawn immediately.
        try:
            active_ffmpeg.kill()
        except OSError:
            pass
        _sp._reap_process_async(active_ffmpeg, "Seek-kill ffmpeg")
        # Compare-and-swap on BOTH storage locations. The prior unconditional
        # ``= None`` assignments raced with a concurrent _start_remux_process
        # on another handler thread that had just written its own proc into
        # server.active_ffmpeg — we'd zero out that fresh reference, leaving
        # ``B`` streaming with no tracked handle for later cleanup. Use the
        # same ``is proc`` CAS pattern that _finish_remux uses.
        if ctx.get("active_ffmpeg") is active_ffmpeg:
            ctx["active_ffmpeg"] = None
        if getattr(self.server, "active_ffmpeg", None) is active_ffmpeg:
            self.server.active_ffmpeg = None

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

        # Everything from header-send through the streaming loop goes in
        # a single try/finally that always runs _finish_remux. Previously
        # _send_close_response_headers ran as a bare statement before the
        # try block; if it raised (socket dead, client disconnected mid-
        # handshake) ffmpeg + stderr thread leaked until garbage
        # collection, potentially hundreds of MB of RSS under heavy load.
        total = 0
        try:
            # Matroska-only response. Piped MKV has no Cues so advertising
            # byte-range would only disable Kodi's cache-based fallback
            # without enabling real seek. Stay on live-stream semantics;
            # duration is still embedded in the MKV header so Kodi's
            # progress bar is accurate.
            self._send_close_response_headers(200, "video/x-matroska", "none")

            # Give the socket a write timeout.  If Kodi stops consuming
            # bytes without closing the TCP connection — which happens
            # when Kodi's decoder is stalled by a long operation like a
            # DB vacuum and the player enters limbo instead of firing
            # onPlayBackStopped — the socket send buffer fills up and
            # wfile.write() would block forever.  A timeout here
            # guarantees the loop eventually raises, runs the finally
            # block, and kills ffmpeg instead of leaving a zombie.
            try:
                self.connection.settimeout(_sp._REMUX_WRITE_TIMEOUT)
            except (OSError, AttributeError):
                pass

            # Stream ffmpeg output to Kodi.  Duration is written into the
            # MKV header by ffmpeg via -metadata DURATION= (see
            # _build_ffmpeg_cmd).
            total = self._stream_remux_output(ctx, proc, lock, requested_start)
        finally:
            self._finish_remux(ctx, proc, lock, stderr_chunks, stderr_thread, total)
