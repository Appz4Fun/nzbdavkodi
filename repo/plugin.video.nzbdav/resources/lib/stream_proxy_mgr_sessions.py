# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Session registration, pruning, and fallback merge.

Stage-3 mixin split of ``stream_proxy.StreamProxy``. These methods were moved
verbatim; every reference to a ``stream_proxy`` module-level name is reached at
call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``StreamProxy``; they keep using ``self`` for instance state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _MgrSessionsMixin:  # pylint: disable=too-few-public-methods
    """Session registration, pruning, and fallback merge."""

    def _assert_context_lock_owned(self):
        """Best-effort debug guard for helpers that require _context_lock."""
        if not __debug__:
            return
        context_lock = getattr(self, "_context_lock", None)
        is_owned = getattr(context_lock, "_is_owned", None)
        # not-callable: the call is guarded by callable(); the mixin split lost
        # the RLock type inference that __init__ gave on the original class.
        if callable(is_owned) and not is_owned():  # pylint: disable=not-callable
            raise AssertionError("_prune_sessions_locked requires _context_lock")

    def _add_pending_context(self, session_id, ctx):
        """Register ctx in the server's pending-context map (locked)."""
        with self._context_lock:
            pending = getattr(self._server, "pending_stream_contexts", None)
            if not isinstance(pending, dict):
                pending = {}
                self._server.pending_stream_contexts = pending
            pending[session_id] = ctx

    def _drop_pending_context(self, session_id):
        """Remove session_id from the server's pending-context map (locked)."""
        with self._context_lock:
            pending = getattr(self._server, "pending_stream_contexts", {})
            if isinstance(pending, dict):
                pending.pop(session_id, None)

    @staticmethod
    def _rewrite_ctx_to_matroska(ctx):
        """Rewrite an HLS ctx in-place to the known-good matroska shape.

        ctx already has ffmpeg_path / total_bytes / duration_seconds from
        prepare_stream's fmp4 branch, so _serve_remux has everything it
        needs once the HLS-specific keys are dropped.
        """
        ctx.pop("mode", None)
        ctx.pop("hls_segment_format", None)
        ctx.pop("hls_segment_duration", None)
        ctx.pop("hls_producer", None)
        ctx["content_type"] = "video/x-matroska"
        ctx["seekable"] = (
            ctx.get("duration_seconds") is not None and ctx.get("total_bytes", 0) > 0
        )

    def _register_hls_session(self, ctx, session_id):
        """Spawn the HLS producer for ctx, falling back to matroska.

        Eager spawn-time validation catches ffmpeg builds that reject
        ``-hls_segment_type fmp4`` BEFORE the HLS URL is returned to Kodi,
        so the matroska rewrite fires for the most likely failure mode
        (no-op for mpegts, which spawns lazily). Raises RuntimeError if
        the prepare was cancelled mid-flight.
        """
        self._add_pending_context(session_id, ctx)
        workdir = _sp._choose_hls_workdir(ctx.get("total_bytes", 0) or 0)
        producer = None
        try:
            producer = _sp.HlsProducer(ctx, workdir)
            ctx["hls_producer"] = producer
            producer.prepare()
        except Exception as e:  # noqa: BLE001 — fall back either way
            if ctx.get("_cleanup_started"):
                self._drop_pending_context(session_id)
                raise RuntimeError("HLS prepare was cancelled") from e
            _sp.xbmc.log(
                "NZB-DAV: HLS producer setup failed ({}), "
                "rewriting session to matroska fallback".format(e),
                _sp.xbmc.LOGWARNING,
            )
            # Best-effort cleanup of the partially initialized producer.
            # HlsProducer.__init__ owns disk resources (session_dir,
            # ffmpeg.log) that need close()'ing on the prepare()-failure
            # path; the `producer = None` sentinel protects against
            # AttributeError when the constructor itself raised.
            if producer is not None:
                try:
                    producer.close()
                except Exception:  # noqa: BLE001
                    pass
            self._rewrite_ctx_to_matroska(ctx)
        finally:
            self._drop_pending_context(session_id)
        if ctx.get("_cleanup_started"):
            raise RuntimeError("HLS prepare was cancelled")

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
        session_id = _sp.uuid.uuid4().hex
        now = _sp.time.time()
        ctx["session_id"] = session_id
        ctx["created_at"] = now
        ctx["last_access"] = now
        ctx["ffmpeg_lock"] = _sp.threading.Lock()
        ctx["active_ffmpeg"] = None
        ctx["current_byte_pos"] = 0
        ctx["_active_handlers"] = 0
        ctx["_cleanup_pending"] = False
        ctx["_cleanup_started"] = False

        if ctx.get("mode") == "hls":
            self._register_hls_session(ctx, session_id)

        with self._context_lock:
            if not isinstance(getattr(self._server, "stream_sessions", None), dict):
                self._server.stream_sessions = {}
            self._server.stream_context = ctx
            self._server.stream_sessions[session_id] = ctx
            evicted = self._prune_sessions_locked(keep_session=session_id)
        # Cleanup outside the lock — `_cleanup_session` does proc.kill +
        # proc.wait, which on a stuck ffmpeg can block other lock waiters.
        for evicted_ctx in evicted:
            self._cleanup_session_or_defer(evicted_ctx)

        if ctx.get("mode") == "hls":
            return "http://127.0.0.1:{}/hls/{}/playlist.m3u8".format(
                self.port, session_id
            )
        return "http://127.0.0.1:{}/stream/{}".format(self.port, session_id)

    def _prune_sessions_locked(self, keep_session=None):
        """Drop expired sessions and cap the total number retained.

        Returns the list of evicted ctx dicts so the *caller* can do the
        actual `_cleanup_session(ctx)` work OUTSIDE `_context_lock`. The
        cleanup path runs `proc.kill()` + `proc.wait()` and on a stuck
        ffmpeg can block long enough to wedge any other thread waiting
        on `_context_lock`. Closes TODO.md §H.2-H1e.
        """
        self._assert_context_lock_owned()
        sessions = getattr(self._server, "stream_sessions", {})
        now = _sp.time.time()
        evicted = []

        expired = _sp._expired_session_ids(sessions, keep_session, now)
        for session_id in expired:
            ctx = sessions.pop(session_id, None)
            if ctx is not None:
                evicted.append(ctx)

        while len(sessions) > _sp._MAX_STREAM_SESSIONS:
            session_id = _sp._least_recently_used_session(sessions, keep_session)
            if session_id is None:
                break
            ctx = sessions.pop(session_id, None)
            if ctx is not None:
                evicted.append(ctx)

        return evicted

    def merge_session_fallbacks(self, session_id, fallback_sources):
        """Merge late-adopted fallback sources into a live session context.

        Returns the count of NEW sources added, or None when the session is
        unknown (torn down / never existed). Dedups by (nzo_id, stream_url).
        Existing source dicts are preserved BY IDENTITY so in-place
        failed/validated marks the live cutover writes survive a merge, and
        the list is swapped atomically so a concurrent _serve_proxy reader
        never observes a half-mutated list.
        """
        normalized = _sp._normalize_fallback_sources(fallback_sources)

        with self._context_lock:
            sessions = getattr(self._server, "stream_sessions", None)
            if not isinstance(sessions, dict):
                return None
            ctx = sessions.get(session_id)
            if not isinstance(ctx, dict):
                return None
            existing = list(ctx.get("fallback_sources") or [])
            added = _sp._merge_new_fallback_sources(existing, normalized)
            if added:
                ctx["fallback_sources"] = existing
        if added:
            # Warm the freshly-pushed fallbacks in the background (resolve
            # nzo-only standbys + fingerprint) so a later primary failure cuts
            # over instantly instead of cold-resolving under Kodi's timeout.
            self._start_fallback_prevalidation(ctx)
        return added
