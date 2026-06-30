# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""HLS ffmpeg spawn, command build, restart and lifecycle helpers.

Stage-final mixin split of ``stream_proxy.HlsProducer``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``HlsProducer``; they keep using ``self`` for instance state, ``self._lock``
for the producer lock, and the ``_PREPARE_PRODUCTION_TIMEOUT_SECONDS`` class
attribute (which stays on ``HlsProducer``).
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _HlsProduceMixin:  # pylint: disable=too-few-public-methods
    """HLS ffmpeg spawn, command build, restart and lifecycle helpers."""

    def _spawn_for_init_if_dead(self):
        """If no ffmpeg is alive, (re)spawn it at its current target.

        Bootstrap (fresh session: target defaults to 0) or respawn at
        whatever target the last generation had. DO NOT hardcode 0 — a
        crashed mid-seek producer still has the right start_segment to
        resume at. If ffmpeg is alive, leave it alone (CRITICAL B).
        """
        with self._lock:
            proc = self._proc
            alive = proc is not None and proc.poll() is None
            current_target = self._start_segment
        if not alive:
            self._ensure_ffmpeg_headed_for(current_target)

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
            if not self._needs_ffmpeg_restart(seg_n):
                return
            self._stop_old_ffmpeg()
            self._fmp4_generation_boundary(seg_n)
            self._spawn_ffmpeg_at(seg_n)

    def _needs_ffmpeg_restart(self, seg_n):
        """Decide whether ffmpeg must be (re)started to reach seg_n.

        MUST be called with self._lock held. ffmpeg only produces
        segments >= start_segment in sequence; a request before that
        means a backward seek, and a request far ahead means a forward
        seek beyond the near-future buffer window — both restart.
        """
        proc = self._proc
        proc_alive = proc is not None and proc.poll() is None
        if not proc_alive:
            return True
        if seg_n < self._start_segment:
            return True
        if seg_n - self._start_segment > _sp._HLS_FORWARD_WAIT_SEGMENTS:
            return True
        return False

    def _stop_old_ffmpeg(self):
        """Kill + reap the current ffmpeg, then clear self._proc.

        MUST be called with self._lock held. 2s wait (was 5s):
        concurrency audit flagged this as the worst-case hold time on
        the HlsProducer lock, which blocks every concurrent
        wait_for_segment / wait_for_init / close() call. 2s is enough
        for SIGKILL to land on a healthy child; on a genuinely stuck
        one we log + let the OS reap rather than stalling the session.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except _sp.subprocess.TimeoutExpired:
                _sp.xbmc.log(
                    "NZB-DAV: HLS ffmpeg pid={} did not exit 2 s after kill; "
                    "leaking for the OS to reap".format(getattr(proc, "pid", "?")),
                    _sp.xbmc.LOGWARNING,
                )
            except (OSError, _sp.subprocess.SubprocessError):
                pass
        self._proc = None

    def _fmp4_generation_boundary(self, seg_n):
        """Mark the fmp4 generation boundary before a respawn.

        MUST be called with self._lock held. Unlink the new target
        segment file so the "seg_<start_segment>.m4s exists"
        completeness signal in _init_file_complete is unambiguously
        bound to the NEW ffmpeg. Do NOT blanket-sweep other segments —
        leaving prior-generation files in place preserves the
        backward-seek cache optimization in _segment_complete. Do NOT
        unlink init.mp4 either: the canonical bytes cache already
        committed to serving the first generation's init to every Kodi
        request, so whatever new ffmpeg writes to the on-disk init.mp4
        is irrelevant. Unlinking would just race the on-disk overwrite
        and momentarily fail _init_file_complete for no gain.
        """
        if self.segment_format != "fmp4":
            return
        first_seg_path = _sp.os.path.join(
            self.session_dir, "seg_{:06d}.m4s".format(seg_n)
        )
        try:
            _sp.os.unlink(first_seg_path)
        except FileNotFoundError:
            pass
        # Reset _init_ready so wait_for_init/wait_for_segment re-verify
        # the generation boundary (checks that seg_<new_target>.m4s
        # exists post-spawn) — but the canonical init bytes persist.
        self._init_ready = False

    def _spawn_ffmpeg_at(self, seg_n):
        """Spawn a new ffmpeg aimed at seg_n. Lock MUST be held."""
        start_time = seg_n * self.segment_seconds
        cmd = self._build_cmd(start_time, seg_n)
        _sp.xbmc.log(
            "NZB-DAV: HLS producer starting ffmpeg at seg {} (t={:.1f}s)".format(
                seg_n, start_time
            ),
            _sp.xbmc.LOGINFO,
        )
        try:
            # Set _spawn_time + _start_segment BEFORE Popen so a
            # concurrent _segment_complete() can't observe a stale
            # _spawn_time of 0 (which would accept a freshly-unlinked
            # prior-generation segment as complete). The tiny skew
            # before the actual spawn is harmless for that guard.
            self._start_segment = seg_n
            self._spawn_time = _sp.time.time()
            # Reopen the log if close() (or any caller) closed it, so the
            # new ffmpeg doesn't inherit a closed fd and swallow stderr.
            if self._ffmpeg_log.closed:
                self._ffmpeg_log = open(  # noqa: SIM115 — closed in close()
                    self._ffmpeg_log_path, "ab", buffering=0
                )
            # cwd=session_dir is REQUIRED for fmp4: ffmpeg 6.0.1 on
            # CoreELEC rejects absolute paths for -hls_fmp4_init_filename,
            # so _build_cmd passes relative filenames and relies on cwd.
            # mpegts passes absolute paths and tolerates either cwd, so
            # setting cwd unconditionally is safe. stdin=DEVNULL keeps the
            # child off the parent stdin (TODO.md §H.3 Low).
            self._proc = _sp.subprocess.Popen(  # nosec B603 — argv list, shell=False
                cmd,
                stdin=_sp.subprocess.DEVNULL,
                stdout=_sp.subprocess.DEVNULL,
                stderr=self._ffmpeg_log,
                shell=False,
                cwd=self.session_dir,
            )
        except OSError as e:
            _sp.xbmc.log(
                "NZB-DAV: HLS producer ffmpeg spawn failed: {}".format(e),
                _sp.xbmc.LOGERROR,
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
        cmd = self._build_base_input_args(start_time)
        if self.segment_format == "fmp4":
            self._append_fmp4_output_args(cmd, start_segment)
        else:
            self._append_mpegts_output_args(cmd, start_segment)
        return cmd

    def _append_mpegts_output_args(self, cmd, start_segment):
        """Append the legacy mpegts segment-muxer output args (unchanged)."""
        # mpegts branch — unchanged filename pattern.
        seg_pattern = _sp.os.path.join(self.session_dir, "seg_%06d.ts")
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

    def _build_base_input_args(self, start_time):
        """Build the shared ffmpeg input args (auth + map + copy) for _build_cmd."""
        _sp._validate_url(self.remote_url)
        # Pass auth via -headers (not URL-embedded) so credentials
        # don't leak into argv / ffmpeg.log / error messages. See
        # _ffmpeg_auth_args for the rationale.
        input_url = self.remote_url
        auth_args = _sp._ffmpeg_auth_args(self.auth_header)

        # -probesize / -analyzeduration: ffmpeg needs to read enough
        # input bytes AND enough media duration to determine codec
        # parameters before muxing starts. The original (1 MB / 0)
        # skipped analysis entirely, which broke audio frame-size
        # detection: ffmpeg logged "track N: codec frame size is
        # not set" and the mp4 muxer fell back to a default
        # per-packet duration that didn't match reality, producing
        # AV desync on DTS/TrueHD AND outright "no audio" on
        # E-AC-3 (DDP) sources.
        #
        # The first bump to 5 MB / 2 s helped DTS slightly but
        # didn't catch E-AC-3 in a sparsely-interleaved MKV — 2 s
        # of media time covers only a handful of audio packets in
        # a 4K REMUX where audio is interleaved between large
        # video keyframes. Bumping to 50 MB / 15 s gives ffmpeg a
        # comfortable margin to read dozens of audio packets and
        # determine the codec frame size for any practical source.
        # Costs ~3-5 s of extra startup latency on first spawn
        # (and on every seek respawn) — the playback-never-started
        # watchdog in service.py was raised to 30 s for exactly
        # this reason.
        cmd = [
            self.ffmpeg_path,
            "-v",
            "warning",
            "-probesize",
            "52428800",
            "-analyzeduration",
            "15000000",
            "-fflags",
            "+fastseek",
            "-ss",
            "{:.3f}".format(start_time),
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
        ]
        self._append_input_map_args(cmd, auth_args, input_url)
        return cmd

    @staticmethod
    def _append_input_map_args(cmd, auth_args, input_url):
        """Append auth headers + ``-i`` input and the v/a copy mapping."""
        # Auth headers MUST come before -i so they apply to the input.
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
                "-copyts",
            ]
        )

    def _append_fmp4_output_args(self, cmd, start_segment):
        """Append the fmp4 HLS output flags to ``cmd`` in their exact order."""
        # IMPORTANT: fmp4 arguments must be RELATIVE filenames, not
        # absolute paths. ffmpeg 6.0.1 on CoreELEC fails on absolute
        # paths for ``-hls_fmp4_init_filename`` with "Failed to open
        # segment <path>: No such file or directory", even when the
        # parent directory exists and is writable. Relative names
        # work reliably when ffmpeg is spawned with cwd set to the
        # session dir (see ``_ensure_ffmpeg_headed_for``'s ``Popen``
        # call). Reproduced 2026-04-14 on a 48 GB DV HEVC REMUX
        # and a 27 GB AVC REMUX; both failed with absolute paths,
        # both succeeded with relative.
        init_path = "init.mp4"
        seg_pattern = "seg_%06d.m4s"
        playlist_path = "ffmpeg_playlist.m3u8"
        self._append_fmp4_stability_args(cmd)
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
                "independent_segments+omit_endlist",
                "-start_number",
                str(start_segment),
                playlist_path,
            ]
        )

    @staticmethod
    def _append_fmp4_strict_args(cmd):
        """Append ``-strict -2`` so the fMP4 muxer accepts TrueHD/DTS-HD MA."""
        # -strict -2 (== -strict experimental) unlocks TrueHD and
        # DTS-HD MA output in the MP4/fMP4 muxer. ffmpeg 6.0.1
        # otherwise refuses with "truehd in MP4 support is
        # experimental, add '-strict -2' if you want to use it"
        # / "dts in MP4 support is experimental, ..." and fails
        # to write the init header at all. Virtually every UHD
        # REMUX uses one of those codecs, so without this flag
        # the fmp4 HLS path never produces a playable output
        # on real content. Verified 2026-04-14 against The
        # Machinist (TrueHD) — failed without -strict, succeeded
        # with it.
        cmd.extend(["-strict", "-2"])

    @staticmethod
    def _append_fmp4_stability_args(cmd):
        """Append the fmp4 codec/timestamp/movflags/tag flags in exact order."""
        _sp.HlsProducer._append_fmp4_strict_args(cmd)
        # Timestamp and fragment flags for seek-respawn stability:
        # -start_at_zero pairs with -copyts so seeked output starts from
        # a deterministic timeline, while avoid_negative_ts prevents
        # pre-roll from surfacing as negative fragment timestamps.
        # bitexact strips volatile muxer metadata, and the CMAF-style
        # movflags keep fragments self-relative across respawns. Do not
        # enable hls delete_segments here; this proxy owns segment
        # retention and may serve recently-produced files during a
        # reconnect or backward seek.
        cmd.extend(
            [
                "-start_at_zero",
                "-avoid_negative_ts",
                "make_zero",
                "-fflags",
                "+bitexact+flush_packets",
                "-flags",
                "+bitexact",
            ]
        )
        cmd.extend(
            [
                "-movflags",
                # nosemgrep
                "+frag_custom+dash+delay_moov+separate_moof"
                "+default_base_moof+omit_tfhd_offset",
            ]
        )
        # Force the HLS-spec sample entry tag on the video track.
        # fMP4 HLS mandates ``hvc1`` for HEVC (parameter sets in the
        # sample description box, not inband), and Amlogic's HLS
        # demuxer looks at ``hvc1``/``hev1`` to decide whether to
        # inspect the ``dvcC``/``dvvC`` DV configuration records in
        # the init segment. ``-tag:v hvc1`` is a metadata swap,
        # not a re-encode; ffmpeg pulls SPS/PPS/VPS into ``hvcC``
        # at the muxer and leaves the bitstream otherwise
        # untouched.
        cmd.extend(["-tag:v", "hvc1"])

    def prepare(self):
        """Eagerly spawn ffmpeg AND wait for it to actually produce
        init.mp4 + first segment before returning.

        Called from _register_session right after construction. For
        mpegts producers (the legacy lazy path) this is a no-op.
        For fmp4 producers this is the spawn-time validation that
        keeps the matroska late-binding fallback working — without
        it, ffmpeg's first spawn happens inside wait_for_init AFTER
        the HLS URL has already been returned to Kodi.

        Two failure-detection windows in sequence:

        1. **Argument rejection (~500 ms).** Catches "ffmpeg argv
           is wrong" failures: missing muxer, bad option, refused
           experimental codec, build mismatch, etc. ffmpeg exits
           with non-zero rc within ~10-100 ms in practice.

        2. **Production failure (up to _PREPARE_PRODUCTION_TIMEOUT
           _SECONDS).** Catches "ffmpeg started but never produced
           anything" failures: absolute path bug (a547a2d), -strict
           -2 missing (b8f09d6), analysis hang (1a56c36), and any
           future ffmpeg/source combo where output stalls after
           launch. Polls for init.mp4 + seg_000000.m4s on disk.
           If neither is on disk by the deadline, OR if ffmpeg has
           exited with non-zero rc in the meantime, raises so
           _register_session rewrites ctx to the matroska shape.

        Both checks must pass before prepare() returns successfully.
        Costs up to 30 s of latency on the first spawn for healthy
        sessions (typical: 2-5 s). That's the right tradeoff vs
        handing Kodi a URL that will never play — and the
        playback-never-started watchdog in service.py was raised
        to 30 s for exactly this latency budget.

        Raises:
            RuntimeError: ffmpeg failed to spawn, exited early, or
                produced no output within the production timeout.
        """
        if self.segment_format != "fmp4":
            return  # mpegts is lazy-spawned, no eager validation
        self._ensure_ffmpeg_headed_for(0)
        init_path = _sp.os.path.join(self.session_dir, "init.mp4")
        first_seg_path = _sp.os.path.join(self.session_dir, "seg_000000.m4s")

        ready, early_exit = self._prepare_argv_window(init_path, first_seg_path)
        if ready:
            return  # healthy — both files are on disk
        self._prepare_production_window(init_path, first_seg_path, early_exit)

    @staticmethod
    def _prepare_outputs_present(init_path, first_seg_path):
        """True if both prepare() output files are on disk."""
        return _sp.os.path.exists(init_path) and _sp.os.path.exists(first_seg_path)

    def _prepare_argv_window(self, init_path, first_seg_path):
        """Window 1: argument-rejection poll (500 ms).

        An early exit with rc != 0 is a hard failure (bad argv, missing
        muxer, refused experimental codec). An early exit with rc == 0
        is a SUCCESSFUL completion — possible when the source is shorter
        than 500 ms of stream-copy work (the synthetic test MKV). Either
        way, on early exit we drop straight to the production check.

        Returns (ready, early_exit): ready=True if both output files
        are already on disk (caller returns); early_exit tracks whether
        ffmpeg has already exited cleanly.
        """
        argv_deadline = _sp.time.monotonic() + 0.5
        while _sp.time.monotonic() < argv_deadline:
            with self._lock:
                proc = self._proc
            if proc is None:
                raise RuntimeError("ffmpeg failed to spawn — check ffmpeg.log")
            rc = proc.poll()
            if rc is not None:
                if rc != 0:
                    raise RuntimeError(
                        "ffmpeg exited immediately with code {} — fmp4 "
                        "HLS likely unsupported by this build".format(rc)
                    )
                return False, True
            if self._prepare_outputs_present(init_path, first_seg_path):
                _sp.xbmc.log(
                    "NZB-DAV: HlsProducer.prepare confirmed init.mp4 "
                    "and seg_000000.m4s on disk during argv window",
                    _sp.xbmc.LOGINFO,
                )
                return True, False
            # Monitor.waitForAbort instead of bare time.sleep so a Kodi
            # shutdown during HLS warmup unblocks the prepare argv-loop
            # immediately. TODO.md §H.3.
            if _sp.xbmc.Monitor().waitForAbort(0.05):
                raise RuntimeError("Kodi abort requested during HLS prepare")
        return False, False

    def _prepare_production_window(self, init_path, first_seg_path, early_exit):
        """Window 2: wait for actual output production.

        Polls the file system for init.mp4 + the first segment, AND
        watches ffmpeg liveness so a late crash surfaces immediately.
        If ffmpeg already exited cleanly in window 1 (early_exit), the
        output files should already exist; verify once instead of
        waiting. Raises RuntimeError on timeout or failure.
        """
        prod_deadline = _sp.time.monotonic() + self._PREPARE_PRODUCTION_TIMEOUT_SECONDS
        while _sp.time.monotonic() < prod_deadline:
            if self._prepare_outputs_present(init_path, first_seg_path):
                _sp.xbmc.log(
                    "NZB-DAV: HlsProducer.prepare confirmed init.mp4 "
                    "and seg_000000.m4s on disk",
                    _sp.xbmc.LOGINFO,
                )
                return  # healthy — both files are on disk
            if early_exit:
                # ffmpeg already finished; if the files aren't here,
                # they're never going to be. Fail immediately
                # instead of waiting for the full deadline.
                raise RuntimeError(
                    "ffmpeg exited cleanly but produced no init.mp4 / "
                    "seg_000000.m4s — check ffmpeg.log"
                )
            if self._prepare_ffmpeg_exited_clean():
                # ffmpeg exited mid-window with rc==0 — the source was
                # short enough to finish during the production wait.
                # Give the file-existence check one more iteration
                # before declaring failure.
                early_exit = True
                continue
            if _sp.xbmc.Monitor().waitForAbort(0.25):
                raise RuntimeError("Kodi abort requested during HLS prepare")
        raise RuntimeError(
            "ffmpeg did not produce init.mp4 + seg_000000.m4s within "
            "{:.0f}s — check ffmpeg.log".format(
                self._PREPARE_PRODUCTION_TIMEOUT_SECONDS
            )
        )

    def _prepare_ffmpeg_exited_clean(self):
        """Inspect ffmpeg liveness during the production window.

        Returns True iff ffmpeg has exited with rc==0 (caller should
        re-verify output files). Raises RuntimeError if ffmpeg
        disappeared or exited with a non-zero code. Returns False while
        ffmpeg is still running.
        """
        with self._lock:
            proc = self._proc
        if proc is None:
            raise RuntimeError("ffmpeg disappeared during prepare — check ffmpeg.log")
        rc = proc.poll()
        if rc is None:
            return False
        if rc != 0:
            raise RuntimeError(
                "ffmpeg exited with code {} before producing output "
                "— check ffmpeg.log".format(rc)
            )
        return True

    def _finish_close_after_kill(self, proc, wait_for_proc):
        """Finish HLS cleanup after close() has signaled ffmpeg to stop."""
        if wait_for_proc:
            try:
                proc.wait(timeout=5)
            except _sp.subprocess.TimeoutExpired:
                _sp.xbmc.log(
                    "NZB-DAV: HlsProducer.close: ffmpeg pid={} did not exit "
                    "5 s after kill; leaking for the OS to reap".format(
                        getattr(proc, "pid", "?")
                    ),
                    _sp.xbmc.LOGWARNING,
                )
            except (OSError, _sp.subprocess.SubprocessError):
                pass
        try:
            self._ffmpeg_log.close()
        except OSError:
            pass
        # Persist the session's ffmpeg.log to a stable rolling
        # location BEFORE the session dir is deleted. Otherwise
        # every "playback failed" debug session has to chase a
        # log that no longer exists — which has bitten us several
        # times already on the fmp4 spike. Keep the most recent
        # 10 logs, named by session_id so they're easy to
        # cross-reference with the kodi.log "session_id=..." lines.
        try:
            self._archive_ffmpeg_log()
        except Exception as e:  # pylint: disable=broad-except
            # _archive_ffmpeg_log's whole purpose is preserving the
            # session log for post-mortem debugging. Swallowing its
            # own failure silently defeats that goal — log at debug
            # so the user can diagnose "why isn't my ffmpeg.log
            # archived?" when it matters.
            _sp.xbmc.log(
                "NZB-DAV: Failed to archive ffmpeg.log for session {}: {}".format(
                    getattr(self, "session_dir", "?"), e
                ),
                _sp.xbmc.LOGDEBUG,
            )
        try:
            import shutil as _shutil

            _shutil.rmtree(self.session_dir, ignore_errors=True)
        except OSError:
            pass

    def _finish_close_after_kill_in_background(self, proc, wait_for_proc):
        thread = _sp.threading.Thread(
            target=self._finish_close_after_kill,
            args=(proc, wait_for_proc),
            name="nzbdav-old-hls-close",
        )
        thread.daemon = True
        try:
            thread.start()
        except RuntimeError:
            self._finish_close_after_kill(proc, wait_for_proc)

    def close(self, wait_for_process=True):
        """Kill ffmpeg and delete the session directory."""
        with self._lock:
            self._closed = True
            proc = self._proc
            self._proc = None
        wait_for_proc = False
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except (OSError, _sp.subprocess.SubprocessError):
                pass
            else:
                wait_for_proc = True
        if wait_for_process:
            self._finish_close_after_kill(proc, wait_for_proc)
        else:
            self._finish_close_after_kill_in_background(proc, wait_for_proc)

    @staticmethod
    def _resolve_ffmpeg_log_archive_dir():
        """Pick the archive directory for session ffmpeg logs.

        Prefers Kodi's ``special://temp/nzbdav-hls-logs/`` but only when
        translatePath yields a genuine string (in tests xbmcvfs is mocked
        and returns a MagicMock, which would leak a "MagicMock" dir in
        cwd). Falls back to the system temp dir. Returns the created
        directory path, or None if it could not be created.
        """
        archive_dir = None
        try:
            import xbmcvfs

            candidate = xbmcvfs.translatePath("special://temp/nzbdav-hls-logs/")
            if isinstance(candidate, str):
                archive_dir = candidate
        except Exception:  # pylint: disable=broad-except
            pass
        if not archive_dir:
            archive_dir = _sp.os.path.join(_sp.tempfile.gettempdir(), "nzbdav-hls-logs")
        try:
            _sp.os.makedirs(archive_dir, exist_ok=True)
        except OSError:
            return None
        return archive_dir

    @staticmethod
    def _trim_archived_ffmpeg_logs(archive_dir):
        """Keep only the 10 most recent ``ffmpeg-*.log`` files in dir."""
        try:
            entries = []
            for name in _sp.os.listdir(archive_dir):
                if not name.startswith("ffmpeg-") or not name.endswith(".log"):
                    continue
                full = _sp.os.path.join(archive_dir, name)
                try:
                    entries.append((_sp.os.path.getmtime(full), full))
                except OSError:
                    continue
            entries.sort(reverse=True)
            for _, path in entries[10:]:
                try:
                    _sp.os.unlink(path)
                except OSError:
                    pass
        except OSError:
            pass

    def _archive_ffmpeg_log(self):
        """Copy the session's ffmpeg.log to /storage/.kodi/temp/
        nzbdav-hls-logs/ and trim to the most recent 10."""
        import shutil as _shutil

        src = self._ffmpeg_log_path
        if not _sp.os.path.exists(src):
            return
        try:
            size = _sp.os.path.getsize(src)
        except OSError:
            return
        if size == 0:
            return  # empty log — nothing useful to preserve

        archive_dir = self._resolve_ffmpeg_log_archive_dir()
        if not archive_dir:
            return

        session_id = _sp.os.path.basename(self.session_dir)
        dst = _sp.os.path.join(archive_dir, "ffmpeg-{}.log".format(session_id))
        try:
            _shutil.copy2(src, dst)
        except OSError:
            return

        self._trim_archived_ffmpeg_logs(archive_dir)

        _sp.xbmc.log(
            "NZB-DAV: Archived session ffmpeg.log to {}".format(dst),
            _sp.xbmc.LOGINFO,
        )
