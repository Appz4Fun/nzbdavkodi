# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""HLS segment-path, playlist and on-disk completeness helpers.

Stage-final mixin split of ``stream_proxy.HlsProducer``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``HlsProducer``; they keep using ``self`` for instance state, ``self._lock``
for the producer lock, and capture ``_HLS_SEGMENT_WAIT_SECONDS`` as a
default-argument value (``timeout=_sp._HLS_SEGMENT_WAIT_SECONDS``) in
``wait_for_init`` / ``wait_for_segment``.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _HlsSegmentMixin:  # pylint: disable=too-few-public-methods
    """HLS segment-path, playlist and on-disk completeness helpers."""

    def segment_path(self, seg_n):
        """Return the disk path for a segment index, with the extension
        determined by this producer's segment_format."""
        ext = "m4s" if self.segment_format == "fmp4" else "ts"
        return _sp.os.path.join(self.session_dir, "seg_{:06d}.{}".format(seg_n, ext))

    def playlist_path(self):
        """Return the ffmpeg-generated playlist path for fMP4 HLS."""
        return _sp.os.path.join(self.session_dir, "ffmpeg_playlist.m3u8")

    def generated_playlist_body(self):
        """Return ffmpeg's playlist with proxy-friendly segment names."""
        path = self.playlist_path()
        try:
            with open(path, "r", encoding="utf-8") as playlist_file:
                text = playlist_file.read()
        except OSError:
            return None
        if "#EXTINF:" not in text:
            return None

        text = _sp._SEGMENT_NORMALIZE_RE.sub(r"seg_\1.\2", text)
        return text.encode("utf-8")

    def _segment_complete(self, seg_n):
        """True if seg_n.ts exists and is no longer being written.

        Completion is detected by either: the next segment file also
        exists (ffmpeg has moved on), or the file's mtime has been
        stable for more than _HLS_SEGMENT_MTIME_STABLE_MS.

        For fMP4, the "next segment exists" signal is only trusted
        if the next segment was created after the current ffmpeg
        spawn — otherwise a stale seg_n+1 from a prior generation
        can make this return True while the new seg_n is still
        being written.
        """
        # Snapshot _spawn_time under the lock so a concurrent respawn
        # can't update it between our two checks below. The atomic
        # getattr-after-lock pattern guarantees we compare every mtime
        # against a single consistent generation boundary.
        with self._lock:
            spawn_time = self._spawn_time
        path = self.segment_path(seg_n)
        if not _sp.os.path.exists(path):
            return False
        if self._next_segment_signals_complete(seg_n, spawn_time):
            return True
        # Final segment (or ffmpeg briefly mid-transition) — fall back
        # to mtime stability.
        try:
            mtime = _sp.os.path.getmtime(path)
        except OSError:
            return False
        if self.segment_format == "fmp4":
            return self._fmp4_segment_complete(seg_n, mtime, spawn_time)
        if (_sp.time.time() - mtime) * 1000.0 > _sp._HLS_SEGMENT_MTIME_STABLE_MS:
            return True
        # If this is the terminal segment (no N+1 will ever exist),
        # ffmpeg should have exited by now.
        if seg_n >= self.total_segments - 1:
            return self._terminal_ffmpeg_exited()
        return False

    def _next_segment_signals_complete(self, seg_n, spawn_time):
        """True if seg_n+1's existence proves seg_n is complete.

        In fMP4 mode the next segment is only trusted if it was created
        after the latest spawn (a stale seg_n+1 from a prior generation
        must not mark the freshly-written seg_n complete).
        """
        next_path = self.segment_path(seg_n + 1)
        if not _sp.os.path.exists(next_path):
            return False
        if self.segment_format != "fmp4":
            return True
        try:
            next_mtime = _sp.os.path.getmtime(next_path)
        except OSError:
            return False
        return next_mtime >= spawn_time

    def _fmp4_segment_complete(self, seg_n, mtime, spawn_time):
        """fMP4 mtime-path completeness check for seg_n.

        Requires THIS segment to be from the current ffmpeg generation
        (mtime >= spawn_time). Without this guard a backward seek can
        read a stale ``seg_n.m4s`` from a prior generation whose mtime
        is far in the past — the mtime-stability check is trivially
        true for such a file. The bytes are valid but were produced
        against a different edit list / timestamp base, so Kodi's HLS
        demuxer glitches or stalls when splicing them.
        """
        if mtime < spawn_time:
            return False
        if seg_n >= self.total_segments - 1:
            return self._terminal_ffmpeg_exited()
        return False

    def _terminal_ffmpeg_exited(self):
        """True if the current ffmpeg process has exited (terminal seg)."""
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is not None

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
        init_path = _sp.os.path.join(self.session_dir, "init.mp4")
        if not _sp.os.path.exists(init_path):
            return False
        # Deliberately reading self._start_segment WITHOUT self._lock.
        #
        # Why it's safe today:
        #   * CPython stores Python ints as PyObject*; assignment is a
        #     single pointer store and reads of that pointer are atomic
        #     under the GIL. A reader never sees a half-written int.
        #   * The caller (``wait_for_init`` / poll loop) tolerates a
        #     stale read: if ``_start_segment`` has just advanced, the
        #     stale value points at a segment path that already exists
        #     on disk (the previous target) — returning True early is
        #     correct because init.mp4 is complete in both generations.
        #     If we read the stale value and return False, the next
        #     poll cycle (~50 ms later) reads the fresh value.
        #   * Holding self._lock here would serialize the polling reader
        #     against the respawn writer and defeat the purpose of the
        #     fast-path existence check.
        #
        # Why future refactors should revisit this:
        #   * If this module ever runs under a no-GIL interpreter (PEP
        #     703) or switches to asyncio with thread-pool executors,
        #     the "atomic int read" assumption weakens.
        #   * If ``_start_segment`` ever grows into a tuple / object
        #     (e.g. (generation_id, seg_n)), the read is no longer
        #     atomic and a reader can see a torn value.
        #   * Drop-in mitigation when that day comes: replace the bare
        #     int with a ``threading.Event`` that the respawn path
        #     sets() after publishing the new ``_start_segment``, and
        #     have this method wait() on the event before reading.
        first_seg_path = _sp.os.path.join(
            self.session_dir,
            "seg_{:06d}.m4s".format(self._start_segment),
        )
        return _sp.os.path.exists(first_seg_path)

    def _cache_canonical_init_bytes(self, init_path):
        """Cache the first init.mp4 we see so later requests (and respawn
        generations with different edit lists) serve byte-identical data.
        See the docstring on self._canonical_init_bytes for the full
        rationale. No-op once the cache is populated.
        """
        if self._canonical_init_bytes is not None:
            return
        try:
            with open(init_path, "rb") as f:
                self._canonical_init_bytes = f.read()
            _sp.xbmc.log(
                "NZB-DAV: Cached canonical init.mp4 "
                "({} bytes) for session".format(len(self._canonical_init_bytes)),
                _sp.xbmc.LOGINFO,
            )
        except OSError as e:
            _sp.xbmc.log(
                "NZB-DAV: Failed to cache canonical init.mp4: {}".format(e),
                _sp.xbmc.LOGWARNING,
            )

    def wait_for_init(self, timeout=_sp._HLS_SEGMENT_WAIT_SECONDS):
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
        init_path = _sp.os.path.join(self.session_dir, "init.mp4")
        deadline = _sp.time.monotonic() + timeout
        while _sp.time.monotonic() < deadline:
            if self._closed:
                return None
            # Fast path: files already on disk for the current
            # generation. The on-disk check IS the truth-source —
            # _init_ready is just a redundant cached flag we set
            # below for any downstream consumer that wants to skip
            # the file syscall on subsequent calls.
            if self._init_file_complete():
                self._init_ready = True
                self._cache_canonical_init_bytes(init_path)
                return init_path
            self._spawn_for_init_if_dead()
            # If ffmpeg is alive, leave it alone — it's either
            # already headed toward the right segment, or the init
            # re-fetch is racing a valid seek that's already
            # produced init.mp4 once and will produce it again
            # after the seek-restart cleans up.
            if self._init_file_complete():
                self._init_ready = True
                return init_path
            # Use Monitor.waitForAbort instead of bare time.sleep so a
            # Kodi shutdown during HLS warmup unblocks immediately.
            # waitForAbort returns True iff Kodi is shutting down — bail
            # out early in that case. TODO.md §H.3.
            if _sp.xbmc.Monitor().waitForAbort(0.25):
                return None
        return None

    def wait_for_segment(self, seg_n, timeout=_sp._HLS_SEGMENT_WAIT_SECONDS):
        """Block until seg_n is complete on disk, or timeout expires.

        If ffmpeg is either not running or running in a position that
        will never produce seg_n, kicks off a restart aimed at seg_n.
        Returns the segment file path on success, or None on timeout.

        For fmp4 producers, the loop additionally gates on
        _init_file_complete so a seg_n read can't race a
        still-being-written init.mp4.
        """
        deadline = _sp.time.monotonic() + timeout
        while _sp.time.monotonic() < deadline:
            if self._closed:
                return None
            gate = self._wait_for_segment_init_gate(seg_n)
            if gate == "abort":
                return None
            if gate == "retry":
                continue
            if self._segment_complete(seg_n):
                return self.segment_path(seg_n)
            # Do we need to (re)start ffmpeg to eventually reach seg_n?
            self._ensure_ffmpeg_headed_for(seg_n)
            # Monitor.waitForAbort instead of time.sleep so a Kodi shutdown
            # during HLS segment wait unblocks immediately. TODO.md §H.3.
            if _sp.xbmc.Monitor().waitForAbort(0.25):
                return None
        return None

    def _wait_for_segment_init_gate(self, seg_n):
        """fmp4 init gate for wait_for_segment.

        seg_n cannot be served until the current generation's init is
        on disk AND ffmpeg has moved past the init write phase. For
        segment requests we DO want to head toward seg_n specifically —
        the caller asks for a specific segment, so the "seg_n <
        start_segment" restart in _ensure_ffmpeg_headed_for is the right
        call (unlike wait_for_init, which preserves the generation).

        Returns "ok" to proceed to the segment check, "retry" to
        re-enter the wait loop, or "abort" on Kodi shutdown.
        """
        if self.segment_format != "fmp4" or self._init_ready:
            return "ok"
        if self._init_file_complete():
            self._init_ready = True
            return "ok"
        self._ensure_ffmpeg_headed_for(seg_n)
        if _sp.xbmc.Monitor().waitForAbort(0.25):
            return "abort"
        return "retry"
