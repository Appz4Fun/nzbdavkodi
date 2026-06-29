# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""ffmpeg remux-process lifecycle and command-safety helpers.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _RemuxMixin:  # pylint: disable=too-few-public-methods
    """ffmpeg remux-process lifecycle and command-safety helpers."""

    def _get_stream_context(self, acquire=False):
        """Look up the active stream context for the current request path.

        Recognizes both direct-stream paths (``/stream`` and
        ``/stream/<session_id>``) and HLS paths
        (``/hls/<session_id>/playlist.m3u8`` and
        ``/hls/<session_id>/seg_<N>.ts``). The HLS parsing layer uses this
        to resolve a session; the playlist/segment dispatch then branches
        on the trailing resource in ``_handle_hls``.
        """

        def _touch(ctx):
            return _sp._touch_stream_context(ctx, acquire)

        raw_path = getattr(self, "path", "/stream")
        path = raw_path.split("?", 1)[0]
        context_lock = _sp._get_server_context_lock(getattr(self, "server", None))
        if path in ("", "/stream"):
            if context_lock is None:
                return _touch(getattr(self.server, "stream_context", None))
            with context_lock:
                return _touch(getattr(self.server, "stream_context", None))

        session_id = _sp._stream_context_session_id(path)
        if session_id is None:
            return None

        if context_lock is None:
            sessions = getattr(self.server, "stream_sessions", {})
            return _touch(sessions.get(session_id))

        with context_lock:
            sessions = getattr(self.server, "stream_sessions", {})
            return _touch(sessions.get(session_id))

    def _release_stream_context(self, ctx):
        """Release a request lease and run deferred cleanup when safe."""
        if ctx is None:
            return

        def _release():
            return _sp._release_handler_lease(ctx)

        context_lock = _sp._get_server_context_lock(getattr(self, "server", None))
        if context_lock is None:
            cleanup_now = _release()
        else:
            with context_lock:
                cleanup_now = _release()

        if cleanup_now:
            owner_proxy = getattr(getattr(self, "server", None), "owner_proxy", None)
            cleanup = getattr(owner_proxy, "_cleanup_session", None)
            if cleanup is None:
                cleanup = _sp.StreamProxy._cleanup_session
            cleanup(ctx)

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
        segment = _sp._parse_hls_segment_resource(resource)
        if segment is None:
            return None
        return session_id, segment

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
        """Launch ffmpeg for a remux response and register it on the session.

        Uses a CAS-style guard against the H1d race: two concurrent
        range requests on the same session can both pass the
        "no active ffmpeg" check in `_resolve_seek` and reach the
        spawn line below. Whichever thread reaches the lock-protected
        store last would otherwise orphan the other thread's ffmpeg
        process (still running, no one tracking it for cleanup). We
        instead snapshot the current ctx["active_ffmpeg"] under the
        lock before storing; if a winner is already present we kill
        our just-spawned proc and pretend the winner's spawn is ours.
        Closes TODO.md §H.2-H1d.
        """
        cmd = self._build_ffmpeg_cmd(ctx, seek_seconds=seek_seconds)
        if not self._is_safe_ffmpeg_cmd(cmd):
            _sp.xbmc.log(
                "NZB-DAV: Refusing to start unsafe ffmpeg command", _sp.xbmc.LOGERROR
            )
            _sp._notify_error("Failed to start ffmpeg")
            self.send_error(500)
            return None, None
        _sp.xbmc.log(
            "NZB-DAV: Remuxing to MKV (seek={})".format(seek_seconds),
            _sp.xbmc.LOGINFO,
        )
        proc = self._spawn_remux_proc(cmd)
        if proc is None:
            return None, None

        lock = self._ctx_lock(ctx, self.server)
        with lock:
            existing = ctx.get("active_ffmpeg")
            if existing is not None and existing.poll() is None:
                self._kill_cas_loser(proc, existing)
                self.send_error(409)
                return None, None
            ctx["active_ffmpeg"] = proc
            ctx["current_byte_pos"] = requested_start
            self.server.active_ffmpeg = proc
            self.server.current_byte_pos = requested_start
        return proc, lock

    def _spawn_remux_proc(self, cmd):
        """Spawn the remux ffmpeg process; send 500 + return None on failure."""
        try:
            return _sp.subprocess.Popen(  # nosec B603 — argv list, shell=False
                cmd,
                stdin=_sp.subprocess.DEVNULL,
                stdout=_sp.subprocess.PIPE,
                stderr=_sp.subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            _sp.xbmc.log(
                "NZB-DAV: Failed to start ffmpeg: {}".format(error), _sp.xbmc.LOGERROR
            )
            _sp._notify_error("Failed to start ffmpeg")
            self.send_error(500)
            return None

    @staticmethod
    def _kill_cas_loser(proc, existing):
        """Kill the just-spawned ffmpeg that lost the CAS race against existing."""
        # Another thread won the race. Kill our orphan.
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except (_sp.subprocess.TimeoutExpired, OSError):
            pass
        _sp.xbmc.log(
            "NZB-DAV: CAS-killed duplicate ffmpeg pid={} "
            "(winner pid={})".format(
                getattr(proc, "pid", "?"),
                getattr(existing, "pid", "?"),
            ),
            _sp.xbmc.LOGWARNING,
        )

    @staticmethod
    def _start_stderr_drain(proc):
        """Drain ffmpeg stderr in a background thread to avoid pipe stalls."""
        stderr_chunks = _sp.deque(maxlen=50)

        def _drain_stderr():
            try:
                while True:
                    data = proc.stderr.read(4096)
                    if not data:
                        break
                    stderr_chunks.append(data)
            except (OSError, ValueError):
                pass

        stderr_thread = _sp.threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()
        return stderr_chunks, stderr_thread

    def _update_current_byte_pos(self, ctx, lock, current_pos):
        """Keep session and server byte positions in sync while remuxing."""
        with lock:
            ctx["current_byte_pos"] = current_pos
            self.server.current_byte_pos = current_pos

    @staticmethod
    def _read_remux_stdout(proc):
        """Read ffmpeg stdout with an idle guard for real pipe fds."""
        stdout = proc.stdout
        fileno = getattr(stdout, "fileno", None)
        if callable(fileno):
            try:
                fd = fileno()
                ready, _, _ = _sp._select.select(
                    [fd], [], [], _sp._REMUX_STDOUT_IDLE_TIMEOUT
                )
            except (OSError, TypeError, ValueError):
                return stdout.read(65536)
            if not ready:
                if proc.poll() is not None:
                    return b""
                raise _sp._socket.timeout("ffmpeg stdout idle")
        return stdout.read(65536)

    def _stream_remux_output(self, ctx, proc, lock, requested_start):
        """Copy ffmpeg stdout to Kodi until EOF or client disconnect."""
        total = 0
        try:
            while True:
                chunk = self._read_remux_stdout(proc)
                if not chunk:
                    return total
                self.wfile.write(chunk)
                total += len(chunk)
                self._update_current_byte_pos(ctx, lock, requested_start + total)
        except (BrokenPipeError, ConnectionResetError, _sp._socket.timeout) as exc:
            if isinstance(exc, _sp._socket.timeout):
                ctx["remux_stdout_idle_detected"] = True
            _sp.xbmc.log(
                "NZB-DAV: Remux client disconnected after {} MB".format(
                    total // 1048576
                ),
                _sp.xbmc.LOGDEBUG,
            )
            return total
        except (OSError, ValueError) as exc:
            # ffmpeg crash or its stdout pipe getting closed under us
            # raises OSError (BadFileDescriptor) or ValueError (operation
            # on closed file). The previous narrow catch let those escape
            # into the request handler, leaving Kodi waiting on an open
            # response while ffmpeg was already a zombie. Treat the same
            # as a client disconnect for stream-cleanup purposes; the
            # request handler's finally block will reap the proc.
            # TODO.md §H.3 (proc.stdout.read() too narrow) + ffmpeg-
            # mid-stream-crash scenario.
            _sp.xbmc.log(
                "NZB-DAV: Remux ffmpeg pipe failed after {} MB: {!r} "
                "(reason=ffmpeg_pipe_closed)".format(total // 1048576, exc),
                _sp.xbmc.LOGWARNING,
            )
            return total

    def _finish_remux(self, ctx, proc, lock, stderr_chunks, stderr_thread, total):
        """Tear down ffmpeg and emit completion logs for a remux request."""
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (OSError, ValueError, _sp.subprocess.SubprocessError):
            pass

        with lock:
            if ctx.get("active_ffmpeg") is proc:
                ctx["active_ffmpeg"] = None
            if self.server.active_ffmpeg is proc:
                self.server.active_ffmpeg = None

        # 30 s join window. ffmpeg has already exited at this point;
        # the drain thread just has to flush whatever's still in the
        # pipe buffer. 5 s was tight on slow disks during a verbose
        # warning-level stderr flush. TODO.md §H.3.
        stderr_thread.join(timeout=30)
        stderr = b"".join(stderr_chunks).decode(errors="replace")
        if stderr.strip():
            # ffmpeg's HTTP demuxer echoes the failing input URL — including
            # any apikey=... query and user:pass@ userinfo — into stderr on
            # 4xx/5xx errors. Run through redact_text before logging.
            _sp.xbmc.log(
                "NZB-DAV: ffmpeg: {}".format(_sp._redact_text(stderr[:300])),
                _sp.xbmc.LOGDEBUG,
            )
        _sp.xbmc.log(
            "NZB-DAV: Remux done: {} MB sent".format(total // 1048576),
            _sp.xbmc.LOGINFO,
        )

    @staticmethod
    def _ffmpeg_argv_shape_ok(cmd):
        """Reject non-list/empty argv or a non-ffmpeg executable name."""
        if not isinstance(cmd, (list, tuple)) or not cmd:
            return False
        if not all(isinstance(arg, str) for arg in cmd):
            return False
        exe_name = _sp.os.path.basename(cmd[0]).lower()
        return exe_name == "ffmpeg"

    @staticmethod
    def _ffmpeg_arg_control_chars_ok(arg, prev_arg):
        """Reject NUL anywhere and CR/LF outside the ``-headers`` value.

        The value following ``-headers`` legitimately ends in ``\\r\\n`` as the
        HTTP header separator; CR/LF is rejected everywhere else.
        """
        if "\x00" in arg:
            return False
        if prev_arg == "-headers":
            if not arg.endswith("\r\n"):
                return False
            return not ("\n" in arg[:-2] or "\r" in arg[:-2])
        return not ("\n" in arg or "\r" in arg)

    @staticmethod
    def _is_safe_ffmpeg_cmd(cmd):
        """Validate command shape and executable before subprocess execution.

        Rejects NUL in any argv element (execve-level hazard). Rejects CR/LF
        in every argv element EXCEPT the value that follows ``-headers``,
        which legitimately contains ``\\r\\n`` as the HTTP header separator
        in ffmpeg's HTTP demuxer (see ``_ffmpeg_auth_args``). Without this
        exemption the force-remux path 500s on every Authorization-carrying
        stream (regression introduced in PR #83's security hardening).
        """
        if not _sp._StreamHandler._ffmpeg_argv_shape_ok(cmd):
            return False
        prev_arg = None
        for arg in cmd:
            if not _sp._StreamHandler._ffmpeg_arg_control_chars_ok(arg, prev_arg):
                return False
            prev_arg = arg
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
        if _sp._get_addon_setting("proxy_convert_subs") == "false":
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
