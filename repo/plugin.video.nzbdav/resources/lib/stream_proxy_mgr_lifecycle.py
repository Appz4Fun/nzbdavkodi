# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Server start/stop lifecycle, session teardown, ffmpeg capabilities.

Stage-3 mixin split of ``stream_proxy.StreamProxy``. These methods were moved
verbatim; every reference to a ``stream_proxy`` module-level name is reached at
call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``StreamProxy``; they keep using ``self`` for instance state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _MgrLifecycleMixin:  # pylint: disable=too-few-public-methods
    """Server start/stop lifecycle, session teardown, ffmpeg capabilities."""

    def start(self):
        """Start the proxy server on a random port."""
        self._server = _sp._ThreadedHTTPServer(("127.0.0.1", 0), _sp._StreamHandler)
        self._server.owner_proxy = self
        self._server.prepare_token = self.prepare_token
        self.port = self._server.server_address[1]
        self._refresh_ffmpeg_capabilities()
        self._thread = _sp.threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True
        self._thread.start()
        _sp.xbmc.log(
            "NZB-DAV: Stream proxy started on port {}".format(self.port),
            _sp.xbmc.LOGINFO,
        )

    def is_alive(self):
        """True iff the proxy's HTTP server thread is still serving.

        Returns False when either:
        - The proxy hasn't been started yet (``_thread`` is None).
        - The serve_forever thread has exited for any reason —
          normal stop(), or an unhandled exception in the socket
          accept loop (rare but has happened historically on
          memory-pressure paths).

        The service loop polls this once a second and restarts the
        proxy when it drops to False so a crashed listener doesn't
        silently wedge every subsequent /prepare call.
        """
        thread = self._thread
        if thread is None:
            return False
        return thread.is_alive()

    def stop(self):
        """Stop the proxy server.

        ``TCPServer.shutdown()`` only signals ``serve_forever`` to exit;
        it does NOT close the listening socket. Pair it with
        ``server_close()`` so the socket file descriptor is released
        immediately. Otherwise the listener lingers until Python's GC
        runs, which surfaces as ``ResourceWarning: unclosed socket`` in
        the test suite and (more importantly) holds the port across a
        rapid stop/start cycle on the real service.
        """
        self.clear_sessions()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def clear_sessions(self, wait_for_process=True):
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
            pending = list(
                getattr(self._server, "pending_stream_contexts", {}).values()
            )
            self._server.stream_sessions = {}
            self._server.pending_stream_contexts = {}
            self._server.stream_context = None
            self._server.active_ffmpeg = None
        seen = set()
        for ctx in sessions + pending:
            marker = id(ctx)
            if marker in seen:
                continue
            seen.add(marker)
            self._cleanup_session_or_defer(ctx, wait_for_process=wait_for_process)

    def cleanup_session_by_id(self, session_id):
        """Tear down a single session by id, cleanup outside the lock.

        Used by the /prepare write-failure path when the plugin client
        disconnects before the response is delivered (e.g. 60 s urlopen
        timeout firing during a slow tempfile-faststart remux). Without
        this, the session lingers until the next ``prepare_stream`` call
        runs ``clear_sessions``, or the 6-hour TTL prune fires —
        meaning a tempfile from a give-up'd play could occupy disk for
        hours. Closes TODO.md §H.2-H12.
        """
        if not self._server or not session_id:
            return
        with self._context_lock:
            sessions = getattr(self._server, "stream_sessions", {})
            ctx = sessions.pop(session_id, None)
            if ctx is not None and getattr(self._server, "stream_context", None) is ctx:
                self._server.stream_context = None
        if ctx is not None:
            self._cleanup_session_or_defer(ctx)

    def _cleanup_session_or_defer(self, ctx, wait_for_process=True):
        """Cleanup now, or wait until in-flight handlers release the ctx."""
        with self._context_lock:
            if ctx.get("_cleanup_started"):
                return
            if int(ctx.get("_active_handlers", 0) or 0) > 0:
                ctx["_cleanup_pending"] = True
                return
            ctx["_cleanup_started"] = True
        self._cleanup_session(ctx, wait_for_process=wait_for_process)

    @staticmethod
    def _wait_for_active_ffmpeg(active_ffmpeg):
        try:
            active_ffmpeg.wait(timeout=5)
        except (OSError, _sp.subprocess.SubprocessError, ValueError):
            pass

    @staticmethod
    def _wait_for_active_ffmpeg_in_background(active_ffmpeg):
        thread = _sp.threading.Thread(
            target=_sp.StreamProxy._wait_for_active_ffmpeg,
            args=(active_ffmpeg,),
            name="nzbdav-old-ffmpeg-reap",
        )
        thread.daemon = True
        thread.start()

    @staticmethod
    def _cleanup_session(ctx, wait_for_process=True):
        """Release resources associated with a stream session."""
        # Signal the read-ahead prefetch daemon to stop (per-session close; the
        # thread is daemon and also aborts on waitForAbort for global shutdown).
        # This is the single sink reached by every close route, so all sessions
        # are covered. Never block teardown on the thread — daemon is the
        # backstop.
        readahead_buffer = ctx.get(_sp._READAHEAD_BUFFER_KEY)
        if readahead_buffer is not None:
            readahead_buffer.stop()

        _sp.StreamProxy._kill_session_ffmpeg(ctx, wait_for_process)
        _sp.StreamProxy._remove_session_tempfile(ctx)

        hls_producer = ctx.get("hls_producer")
        if hls_producer is not None:
            try:
                hls_producer.close(wait_for_process=wait_for_process)
            except _sp._HLS_CLOSE_ERRORS as e:
                _sp.xbmc.log(
                    "NZB-DAV: HLS producer close failed: {}".format(e),
                    _sp.xbmc.LOGWARNING,
                )

    @staticmethod
    def _kill_session_ffmpeg(ctx, wait_for_process):
        """Kill the session's ffmpeg process and reap it (sync or background)."""
        active_ffmpeg = ctx.get("active_ffmpeg")
        if not active_ffmpeg:
            return
        try:
            active_ffmpeg.kill()
        except (OSError, _sp.subprocess.SubprocessError, ValueError):
            return
        if wait_for_process:
            _sp.StreamProxy._wait_for_active_ffmpeg(active_ffmpeg)
        else:
            _sp.StreamProxy._wait_for_active_ffmpeg_in_background(active_ffmpeg)

    @staticmethod
    def _remove_session_tempfile(ctx):
        """Best-effort removal of the session's temp faststart file."""
        temp_path = ctx.get("temp_path")
        if temp_path and _sp.os.path.exists(temp_path):
            try:
                _sp.os.remove(temp_path)
            except OSError:
                pass

    @staticmethod
    def _probe_hls_fmp4_capability(ffmpeg_path):
        """Return True when ffmpeg exposes the HLS fMP4 muxer flags we use."""
        if not ffmpeg_path:
            return False
        output = _sp._run_ffmpeg_hls_muxer_probe(ffmpeg_path)
        if output is None:
            return False
        supported = all(marker in output for marker in _sp._FMP4_HLS_CAPABILITY_MARKERS)
        _sp.xbmc.log(
            "NZB-DAV: ffmpeg fmp4 HLS capability {} ({})".format(
                "present" if supported else "absent", ffmpeg_path
            ),
            _sp.xbmc.LOGINFO if supported else _sp.xbmc.LOGWARNING,
        )
        return supported

    def _refresh_ffmpeg_capabilities(self):
        """Discover ffmpeg once so service-start logs show the active muxers."""
        ffmpeg_path = _sp._find_ffmpeg()
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
        ffmpeg_path = _sp._find_ffmpeg()
        return {
            "ffmpeg_path": ffmpeg_path,
            "hls_fmp4": bool(ffmpeg_path),
        }
