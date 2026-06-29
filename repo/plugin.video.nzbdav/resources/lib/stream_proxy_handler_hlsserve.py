# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""HLS playlist, init-segment, and media-segment serving.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _HlsServeMixin:  # pylint: disable=too-few-public-methods
    """HLS playlist, init-segment, and media-segment serving."""

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
        producer = ctx.get("hls_producer")
        if producer is not None:
            body = self._producer_generated_playlist_body(producer)
            if isinstance(body, bytes) and body:
                self._send_hls_playlist_body(body)
                return

        duration = ctx.get("duration_seconds") or 0.0
        seg_dur = ctx.get("hls_segment_duration", _sp._HLS_SEGMENT_SECONDS)
        if duration <= 0 or seg_dur <= 0:
            self.send_error(500)
            return

        is_fmp4 = ctx.get("hls_segment_format") == "fmp4"
        body = self._build_hls_playlist_body(duration, seg_dur, is_fmp4)
        self._send_hls_playlist_body(body)

    @staticmethod
    def _producer_generated_playlist_body(producer):
        """Return the producer's pre-generated playlist bytes, or None."""
        generated_playlist_body = getattr(producer, "generated_playlist_body", None)
        if callable(generated_playlist_body):
            return generated_playlist_body()
        return None

    @staticmethod
    def _build_hls_playlist_body(duration, seg_dur, is_fmp4):
        """Build the VOD HLS playlist body bytes for the given timing."""
        total_segs = int(_sp.math.ceil(duration / seg_dur))
        target = int(_sp.math.ceil(seg_dur))
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
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _send_hls_playlist_body(self, body):
        """Send a 200 playlist response with the given body bytes."""
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
            _sp.xbmc.log("NZB-DAV: HLS init wait timed out", _sp.xbmc.LOGWARNING)
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
                _sp.xbmc.log(
                    "NZB-DAV: HLS init read failed: {}".format(e),
                    _sp.xbmc.LOGERROR,
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
        timing = self._hls_segment_timing(ctx, seg_n)
        if timing is None:
            return
        start, this_dur = timing

        segment_path = producer.wait_for_segment(seg_n)
        if segment_path is None:
            _sp.xbmc.log(
                "NZB-DAV: HLS seg {} wait timed out".format(seg_n),
                _sp.xbmc.LOGWARNING,
            )
            self.send_error(504)
            return

        opened = self._open_hls_segment_file(segment_path, seg_n)
        if opened is None:
            return
        seg_file, content_length = opened

        # Pick Content-Type based on session segment format so HEAD
        # and GET agree. fmp4 segments are video/mp4; legacy mpegts
        # segments are video/mp2t.
        seg_fmt = ctx.get("hls_segment_format", "mpegts")
        content_type = "video/mp4" if seg_fmt == "fmp4" else "video/mp2t"
        self._stream_hls_segment_file(
            seg_file, content_length, content_type, seg_n, start, this_dur
        )

    def _hls_segment_timing(self, ctx, seg_n):
        """Resolve (start_seconds, this_dur) for seg_n, or None on error.

        Sends 500 when duration/segment length are unset, 404 when
        seg_n starts past the source duration.
        """
        duration = ctx.get("duration_seconds") or 0.0
        seg_dur = ctx.get("hls_segment_duration", _sp._HLS_SEGMENT_SECONDS)
        if duration <= 0 or seg_dur <= 0:
            self.send_error(500)
            return None
        start = seg_n * seg_dur
        if start >= duration:
            self.send_error(404)
            return None
        return start, min(seg_dur, duration - start)

    def _open_hls_segment_file(self, segment_path, seg_n):
        """Open a segment file and fstat its size.

        Returns (file_object, content_length) or None on error (after
        sending a 500). Opens FIRST then fstat()s to avoid a TOCTOU
        window: a respawn-driven unlink between getsize() and open()
        would leave the handler advertising a size that no longer
        exists. Holding the fd pins the inode even if the dir entry is
        later unlinked, so Content-Length stays in sync with what we
        read.
        """
        try:
            seg_file = open(
                segment_path, "rb"
            )  # noqa: SIM115 — closed by _stream_hls_segment_file / caller
        except OSError as e:
            _sp.xbmc.log(
                "NZB-DAV: HLS seg {} open failed: {}".format(seg_n, e),
                _sp.xbmc.LOGERROR,
            )
            self.send_error(500)
            return None
        try:
            content_length = _sp.os.fstat(seg_file.fileno()).st_size
        except OSError as e:
            seg_file.close()
            _sp.xbmc.log(
                "NZB-DAV: HLS seg {} fstat failed: {}".format(seg_n, e),
                _sp.xbmc.LOGERROR,
            )
            self.send_error(500)
            return None
        return seg_file, content_length

    def _stream_hls_segment_file(
        self, seg_file, content_length, content_type, seg_n, start, this_dur
    ):
        """Send headers and stream a segment file to the client.

        Wraps everything from send_response through the read loop in
        ``with seg_file`` so the fd is closed on every exit path,
        including a header call that raises (socket dead, client gone).
        """
        total = 0
        try:
            with seg_file as f:
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(content_length))
                self.send_header("Connection", "close")
                self.close_connection = True
                self.end_headers()

                try:
                    self.connection.settimeout(_sp._REMUX_WRITE_TIMEOUT)
                except (OSError, AttributeError):
                    pass

                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    total += len(chunk)
        except (BrokenPipeError, ConnectionResetError, _sp._socket.timeout):
            _sp.xbmc.log(
                "NZB-DAV: HLS seg {} client disconnected after {} KB".format(
                    seg_n, total // 1024
                ),
                _sp.xbmc.LOGDEBUG,
            )
        except OSError as e:
            _sp.xbmc.log(
                "NZB-DAV: HLS seg {} read error: {}".format(seg_n, e),
                _sp.xbmc.LOGWARNING,
            )
        else:
            _sp.xbmc.log(
                "NZB-DAV: HLS seg {} done (start={:.1f}s dur={:.1f}s {} KB)".format(
                    seg_n, start, this_dur, total // 1024
                ),
                _sp.xbmc.LOGINFO,
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
        _sp._validate_url(input_url)
        auth_args = _sp._ffmpeg_auth_args(ctx.get("auth_header"))
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
