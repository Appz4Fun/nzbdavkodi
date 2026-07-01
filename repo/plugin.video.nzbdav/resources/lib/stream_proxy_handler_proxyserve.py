# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Passthrough proxy serve loop: state init, read/cutover/retry steps.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _ProxyServeMixin:  # pylint: disable=too-few-public-methods
    """Passthrough proxy serve loop: state init, read/cutover/retry steps."""

    def _serve_proxy(self, ctx):
        """Proxy a byte-range request to upstream with missing-article recovery.

        Reads the requested range using ``ctx["content_length"]`` /
        ``ctx["content_type"]``, streams whatever upstream can serve, probes
        forward past unreadable regions, zero-fills the gaps, and — per the
        runtime pass-through settings — may switch to a validated fallback
        source or retry the original range. Updates the session recovery
        counters, emits per-fallback / recovery-summary toasts, sends the final
        HTTP response (headers and body), and returns no value.
        """
        content_length = ctx["content_length"]
        range_header = self.headers.get("Range")

        if range_header:
            start, end = self._parse_range(range_header, content_length)
            if start is None:
                self.send_error(416)
                return
        else:
            start, end = 0, content_length - 1

        # Notify the read-ahead window of the requested start so a SEEK outside
        # the buffered window discards it (no-op when read-ahead is disabled).
        readahead_buffer = ctx.get(_sp._READAHEAD_BUFFER_KEY)
        if readahead_buffer is not None:
            readahead_buffer.note_seek(start)

        runtime_settings = self._serve_proxy_send_headers(
            ctx, range_header, start, end, content_length
        )
        self._serve_proxy_set_write_timeout()

        st = self._serve_proxy_init_state(ctx, start, end)

        try:
            if self._serve_proxy_emit_prefetch_prefix(ctx, st, range_header):
                return
            self._serve_proxy_unpack_runtime(ctx, st, runtime_settings)

            while st.current <= st.end:
                if self._serve_proxy_loop_body(ctx, st) == "return":
                    return
            # Normal loop exit means ``current > end`` — every requested byte
            # was delivered (streamed and/or zero-filled): a genuine completion.
            st.terminal_reason = "complete"
        except (BrokenPipeError, ConnectionResetError, _sp._socket.timeout):
            self._serve_proxy_classify_disconnect(st)
        finally:
            self._serve_proxy_finalize(ctx, st)

    def _serve_proxy_send_headers(self, ctx, range_header, start, end, content_length):
        """Emit the pass-through response status line and headers.

        Returns the resolved ``runtime_settings`` dict (already loaded when the
        no-Range 200/206 decision needed it, else None) so the caller can reuse
        it without a second lookup. Verbatim move of the header phase.
        """
        total_bytes = end - start + 1
        runtime_settings = None
        no_range_status = False
        if range_header is None:
            runtime_settings = _sp._passthrough_runtime_settings(ctx)
            if "send_200_no_range_enabled" in runtime_settings:
                no_range_status = runtime_settings["send_200_no_range_enabled"]
            else:
                no_range_status = _sp._send_200_no_range_enabled()
        self.send_response(200 if no_range_status else 206)
        self.send_header("Content-Type", ctx["content_type"])
        self.send_header("Content-Length", str(total_bytes))
        self.send_header("Accept-Ranges", "bytes")
        if not no_range_status:
            self.send_header(
                "Content-Range", "bytes {}-{}/{}".format(start, end, content_length)
            )
        # Force Connection: close on pass-through.  Kodi's CCurlFile opens a
        # fresh TCP connection on every seek / retry, so keep-alive provides
        # no benefit here.  But when Kodi reconnects after a CCurlFile error,
        # keep-alive left the OLD handler thread holding its upstream HTTP
        # response + multi-megabyte TCP buffers, doubling our memory footprint
        # and eventually triggering MemoryError in the second handler's 1 MB
        # chunk read.  Connection: close guarantees the previous handler
        # unwinds as soon as Kodi finishes reading its current range.
        #
        # The response header alone is advisory — BaseHTTPServer decides
        # close_connection based on the REQUEST's Connection header, not the
        # response's.  So we also set self.close_connection = True to
        # actually tear down the socket after handle() returns.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        return runtime_settings

    def _serve_proxy_set_write_timeout(self):
        """Apply the per-handler socket write timeout. Verbatim move."""
        # Write timeout so a stalled Kodi (DB vacuum, audio sync error, etc.)
        # can't block this handler in wfile.write() forever.  Without this,
        # a 14 s Kodi vacuum recreated the exact zombie pattern we fixed in
        # _serve_remux: first handler stuck writing into a full socket, Kodi
        # opens a second connection, two handlers + two upstream HTTP
        # responses live at once, MemoryError hits the second handler.
        try:
            self.connection.settimeout(_sp._REMUX_WRITE_TIMEOUT)
        except (OSError, AttributeError):
            pass

    def _serve_proxy_init_state(self, ctx, start, end):
        """Build the per-request loop state. Verbatim move of the local init.

        ``terminal_reason`` defaults to a DISTINCT "unknown" sentinel (NOT
        "complete") so any early return/raise reads as a non-benign exit and
        the finally block still reports a still-pending fallback candidate as
        failed instead of silently swallowing it. The fall-through /
        AWAITING_DOWNLOAD no-progress counters bound the indefinite-hang
        regression (e3a74a1 + F4 / 58f3d4f); ``awaiting_download_no_progress``
        seeds from ctx so it survives Kodi's Connection: close reconnects.
        """
        st = _sp._ProxyStreamState()
        st.start = start
        st.end = end
        st.current = start
        st.total_streamed = 0
        st.total_skipped = 0
        st.recovery_count = 0
        st.terminal_reason = "unknown"
        st.fallback_pending_fallthroughs = 0
        st.last_fallthrough_streamed = -1
        st.awaiting_download_no_progress = int(
            ctx.get("_awaiting_download_no_progress", 0) or 0
        )
        st.density_window = _sp.deque()
        # Reset the throughput watchdog window per request. ctx may be reused
        # across requests for the same session, so explicit re-init avoids
        # stale state from a prior wedge polluting the current sample.
        ctx["passthrough_window_t0"] = _sp.time.monotonic()
        ctx["passthrough_window_bytes"] = 0
        ctx["passthrough_stall_detected"] = False
        st.active_ctx = ctx
        # fallback_pending_candidate: the 1-based candidate we switched to and
        # await first bytes from. candidate_delivered flips True only at the
        # success-toast sites; fallback_failed_to_notify defers a dead
        # candidate's toast until the successor read starts so a slow Kodi
        # notification never stalls the cutover. The finally block reports a
        # still-pending, never-delivered candidate as failed (the F11/F12 hole).
        st.fallback_pending_candidate = None
        st.candidate_delivered = False
        st.fallback_failed_to_notify = None
        return st

    def _serve_proxy_emit_prefetch_prefix(self, ctx, st, range_header):
        """Serve the cached byte-0 prefetch prefix when present. Verbatim move.

        Returns True when the whole range was satisfied by the prefix (caller
        must return immediately), else False to proceed into the read loop.
        """
        waited_for_initial_prefetch = False
        if range_header and st.current == 0:
            cached_prefix = self._pop_cached_fallback_range(ctx, st.current, st.end)
            if not cached_prefix:
                self._wait_for_initial_range_prefetch(ctx, st.current)
                waited_for_initial_prefetch = True
                cached_prefix = self._pop_cached_fallback_range(ctx, st.current, st.end)
            if cached_prefix:
                self.wfile.write(cached_prefix)
                written = len(cached_prefix)
                st.total_streamed += written
                st.current += written
                _sp._update_session_recovery_state(self.server, ctx, streamed=written)
                _sp._record_density_window(st.density_window, "progress", written)
                if st.current > st.end:
                    st.terminal_reason = "complete"
                    return True

        if waited_for_initial_prefetch:
            ctx["_initial_range_prefetch_wait_consumed"] = True
        return False

    def _serve_proxy_unpack_runtime(self, ctx, st, runtime_settings):
        """Resolve and unpack the pass-through runtime settings. Verbatim move."""
        if runtime_settings is None:
            runtime_settings = _sp._passthrough_runtime_settings(ctx)
        st.contract_mode = runtime_settings["contract_mode"]
        st.density_breaker_enabled = runtime_settings["density_breaker_enabled"]
        st.zero_fill_budget_enabled = runtime_settings["zero_fill_budget_enabled"]
        st.retry_ladder_enabled = runtime_settings["retry_ladder_enabled"]

        # Whether any REAL upstream bytes have been delivered in this request
        # yet. The byte-0 prefetch prefix is served instantly from cache and
        # advances ``current`` WITHOUT counting as buffered playback, so the
        # first-byte retry schedule keys on this (not ``current``) to tell a
        # genuine first content read from a mid-stream rebuffer.
        st.streamed_real_upstream_bytes = False
        # Patient forward-stall clock: monotonic time when an ESTABLISHED
        # stream first stalled with NO forward progress (None while it is
        # advancing; set on the first stalled pass and CLEARED on any genuine
        # streamed byte below). The budget is consumed only by a truly-stuck
        # stream, never by a slow-but-healthy one.
        st.forward_stall_t0 = None
        st.stall_wait_budget = runtime_settings.get(
            "passthrough_stall_wait_seconds",
            _sp._DEFAULT_PASSTHROUGH_STALL_WAIT_SECONDS,
        )

    def _serve_proxy_loop_body(self, ctx, st):
        """Run one pass-through loop iteration via its ordered phase steps.

        Each step reads and mutates ``st`` exactly as the inline loop body did
        and returns a control signal: ``"return"`` (exit ``_serve_proxy``),
        ``"continue"`` (restart the loop), or None (proceed to the next step).
        The first non-None signal short-circuits the rest of the iteration —
        identical to the original ``return``/``continue`` control flow.
        """
        steps = (
            self._serve_proxy_read_step,
            self._serve_proxy_cutover_step,
            self._serve_proxy_retry_step,
            self._serve_proxy_progress_step,
            self._serve_proxy_awaiting_step,
            self._serve_proxy_stall_step,
            self._serve_proxy_capfire_step,
            self._serve_proxy_abort_step,
            self._serve_proxy_zerofill_step,
        )
        for step in steps:
            signal = step(ctx, st)
            if signal:
                return signal
        return None

    def _serve_proxy_mark_candidate_delivered(self, st, wrote):
        """Emit the success toast when the pending candidate delivered bytes."""
        if st.fallback_pending_candidate is not None and wrote:
            st.candidate_delivered = True
            _sp._notify_fallback_outcome(st.fallback_pending_candidate, True)
            st.fallback_pending_candidate = None

    def _serve_proxy_read_step(self, ctx, st):
        """Read upstream once, account for bytes, and notify candidates."""
        result, written = self._stream_upstream_range(
            st.active_ctx, st.current, st.end, contract_mode=st.contract_mode
        )
        st.result = result
        st.total_streamed += written
        if written:
            st.streamed_real_upstream_bytes = True
        _sp._update_session_recovery_state(self.server, ctx, streamed=written)
        _sp._record_density_window(st.density_window, "progress", written)
        st.current += written
        if st.fallback_failed_to_notify is not None:
            # The prior candidate failed; emit its toast now — after
            # this (successor) read has already begun — so it never
            # delays the cutover.
            _sp._notify_fallback_outcome(st.fallback_failed_to_notify, False)
            st.fallback_failed_to_notify = None
        # The candidate we switched to just delivered playable bytes — the
        # cutover worked.
        self._serve_proxy_mark_candidate_delivered(st, written)
        if st.current > st.end:
            st.terminal_reason = "complete"
            return "return"

        # F-route: track consecutive AWAITING_DOWNLOAD iterations that
        # made NO forward progress. The reset happens here for the main
        # read; the retry ladder below feeds back into this counter via
        # ``awaiting_download_stuck`` so a primary that the ladder can
        # still coax forward (genuinely downloading, per 58f3d4f) keeps
        # waiting, while a stuck/dead primary escalates to failover.
        st.progressed_this_iter = bool(written)
        return None

    def _serve_proxy_cutover_step(self, ctx, st):
        """Switch to a validated live fallback, or note a pending fall-through."""
        if st.current <= st.end and st.result in (
            _sp._UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
            _sp._UPSTREAM_RANGE_UPSTREAM_ERROR,
        ):
            fallback = self._select_live_fallback_source(ctx, st.current, st.end)
            if fallback:
                self._activate_fallback_source(ctx, fallback, st.current)
                self._serve_proxy_begin_pending_candidate(ctx, st)
                return "continue"
            if ctx.get("fallback_sources"):
                self._serve_proxy_note_pending_fallthrough(st)
        return None

    def _serve_proxy_note_pending_fallthrough(self, st):
        """Count a fruitless cutover re-entry and log it. Verbatim move.

        Fallback sources are attached but none validated yet, so instead of a
        hard close (which made a fallback-enabled stream MORE brittle than a
        plain one — the "Silence of the Lambs went dark" regression) we fall
        through to give the primary a fresh retry-ladder chance; the existing
        skip-probe / zero-fill safeguards bound the damage. The pending
        candidate stays set so the finally block still emits its failure toast.
        BOUNDED EXHAUSTION (F4): count CONSECUTIVE fall-throughs that streamed
        NO new REAL upstream bytes (zero-fill is black frames, not recovery, so
        must NOT reset the bound); any genuine streamed byte resets the count.
        The cap-fire runs AFTER the retry ladder + progress-reset, so the
        primary spends its FINAL ladder attempt before fallback_exhausted is
        declared — WITHOUT reintroducing e3a74a1's immediate hard-close.
        """
        if st.total_streamed == st.last_fallthrough_streamed:
            st.fallback_pending_fallthroughs += 1
        else:
            st.fallback_pending_fallthroughs = 1
            st.last_fallthrough_streamed = st.total_streamed
        _sp.xbmc.log(
            "NZB-DAV: No validated fallback source available at "
            "byte {}; re-entering retry ladder on the primary "
            "instead of closing (attempt {}/{}) "
            "(reason=fallback_pending_retry_primary)".format(
                st.current,
                st.fallback_pending_fallthroughs,
                _sp._FALLBACK_PENDING_FALLTHROUGH_MAX,
            ),
            _sp.xbmc.LOGWARNING,
        )

    def _serve_proxy_retry_step(self, ctx, st):
        """Run the retry ladder on the still-unread range. Verbatim move."""
        if st.retry_ladder_enabled and st.result in (
            _sp._UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
            _sp._UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD,
            _sp._UPSTREAM_RANGE_UPSTREAM_ERROR,
        ):
            (
                st.result,
                retry_written,
                st.current,
            ) = self._retry_original_range(
                st.active_ctx,
                st.current,
                st.end,
                st.contract_mode,
                # The player's first content read takes the SHORT,
                # first-read-patient schedule; a mid-stream rebuffer keeps
                # the long wait-on-primary ladder (58f3d4f). "First read"
                # = a front-of-file open (start == 0) on which no REAL
                # upstream bytes have streamed yet. We must NOT key on
                # ``current`` alone: a byte-0 open serves its cached ~64KB
                # prefetch prefix first, advancing ``current`` to 65536
                # before this ladder runs, so ``current == 0`` wrongly
                # took the long ladder for the genuine first content read
                # (Interstellar 64KB-then-EOF). Nor on ``start`` alone:
                # once real bytes have streamed from byte 0, a later stall
                # is a mid-stream rebuffer and must keep the long ladder.
                first_byte=(st.start == 0 and not st.streamed_real_upstream_bytes),
            )
            st.total_streamed += retry_written
            if retry_written:
                st.streamed_real_upstream_bytes = True
            _sp._update_session_recovery_state(self.server, ctx, streamed=retry_written)
            _sp._record_density_window(st.density_window, "progress", retry_written)
            # The candidate delivered its first bytes via the retry ladder
            # (its initial read was a download-high-water short read) — the
            # cutover worked.
            self._serve_proxy_mark_candidate_delivered(st, retry_written)
            if retry_written:
                st.progressed_this_iter = True
            if st.current > st.end:
                st.terminal_reason = "complete"
                return "return"
        return None

    def _serve_proxy_progress_step(self, ctx, st):
        """Reset or bump the no-progress streak after the ladder. Verbatim move."""
        # F-route: the retry ladder has now had its chance to coax the
        # primary forward. If this whole iteration delivered bytes, the
        # primary IS still downloading — reset the streak and keep
        # waiting on it (58f3d4f's intent). If the result is STILL a
        # clean AWAITING_DOWNLOAD short read with no progress, the
        # primary's needed region is stuck/dead; after a bounded number
        # of such no-progress passes, fail over to a validated fallback
        # (the same live-cutover path the recoverable cases use) rather
        # than spinning the ladder / zero-filling toward EOF forever.
        if st.progressed_this_iter:
            st.awaiting_download_no_progress = 0
            ctx["_awaiting_download_no_progress"] = 0
            # SM-1 (F4): genuine streamed progress means the chain is
            # alive — reset the bounded-exhaustion counters so a later
            # fruitless read starts from a fresh budget, not a stale count.
            st.fallback_pending_fallthroughs = 0
            st.last_fallthrough_streamed = -1
            # Genuine forward progress — drop the patient-stall clock so a
            # healthy still-downloading stream never consumes the wait
            # budget (only CONSECUTIVE no-progress time is counted).
            st.forward_stall_t0 = None
        else:
            st.awaiting_download_no_progress = self._bump_awaiting_no_progress(
                ctx, st.result, st.awaiting_download_no_progress, st.current
            )

    def _serve_proxy_awaiting_stuck(self, ctx, st):
        """Whether a no-progress AWAITING_DOWNLOAD streak has hit its cap."""
        return (
            not st.progressed_this_iter
            and st.current <= st.end
            and st.result == _sp._UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD
            and st.awaiting_download_no_progress
            >= _sp._AWAITING_DOWNLOAD_NO_PROGRESS_MAX
            and ctx.get("fallback_sources")
        )

    def _serve_proxy_begin_pending_candidate(self, ctx, st):
        """Bookkeeping after a cutover activation. Verbatim move shared by both sites.

        Resets the bounded-exhaustion counters, repoints ``active_ctx``, defers
        any prior candidate's failure toast, and arms the new pending candidate.
        """
        st.awaiting_download_no_progress = 0
        # SM-1 (F4): a successful cutover is genuine progress on the fallback
        # chain — reset the bounded-exhaustion counters so a freshly-switched
        # source gets its FULL fruitless-read budget instead of inheriting the
        # stale primary-driven count (which could trip fallback_exhausted after
        # ~one fruitless read).
        st.fallback_pending_fallthroughs = 0
        st.last_fallthrough_streamed = -1
        st.active_ctx = ctx
        if st.fallback_pending_candidate is not None:
            # Switching away from a candidate that never delivered a byte — it
            # failed. Defer the toast to after the next candidate's read starts
            # (handled at the top of the loop) so a slow notification can't
            # stall the cutover.
            st.fallback_failed_to_notify = st.fallback_pending_candidate
        st.fallback_pending_candidate = (
            ctx["fallback_active_index"] + 1
            if ctx["fallback_active_index"] >= 0
            else int(ctx.get("fallback_switch_count", 0) or 0)
        )
        st.candidate_delivered = False

    def _serve_proxy_awaiting_step(self, ctx, st):
        """Fail over when a no-progress AWAITING_DOWNLOAD streak caps. Verbatim."""
        awaiting_fallback = (
            self._select_live_fallback_source(ctx, st.current, st.end)
            if self._serve_proxy_awaiting_stuck(ctx, st)
            else None
        )
        if awaiting_fallback:
            self._activate_fallback_source(
                ctx, awaiting_fallback, st.current, stuck_awaiting=True
            )
            self._serve_proxy_begin_pending_candidate(ctx, st)
            return "continue"
        return None

    def _serve_proxy_stall_step(self, ctx, st):
        """Patiently wait out an established forward stall. Verbatim move.

        An ESTABLISHED forward stream (real bytes already delivered) that
        stalls on a RECOVERABLE backend condition must NOT close on the spot:
        the session breaker short-circuits the retry ladder / skip-probe to an
        instant give-up that Kodi reads as demuxer EOF (the 4K REMUX black
        screen). Hold the client open and re-read with abortable backoff until
        the budget elapses, then fall through to the existing give-up paths.
        forward_stall_t0 resets on genuine progress, so only a truly-stuck
        stream exhausts the budget. Scope: established only (issue-#214
        fast-fail preserved for fresh seeks) and AWAITING_DOWNLOAD /
        UPSTREAM_ERROR only; runs AFTER the cutover routes so a validated
        alternate is always preferred over waiting.
        """
        if (
            st.stall_wait_budget > 0
            and st.streamed_real_upstream_bytes
            and st.current <= st.end
            and st.result
            in (
                _sp._UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD,
                _sp._UPSTREAM_RANGE_UPSTREAM_ERROR,
            )
        ):
            now = _sp.time.monotonic()
            if st.forward_stall_t0 is None:
                st.forward_stall_t0 = now
            if now - st.forward_stall_t0 < st.stall_wait_budget:
                return self._serve_proxy_stall_wait(ctx, st, now)
            # Budget exhausted: genuinely stuck. Fall through to the
            # existing F4 cap-fire / skip-probe give-up so the close is
            # reported through the established taxonomy (no new exit path).
            # Mark the session so the terminal starvation guard fires even
            # on a pure slow-backend give-up (AWAITING with no 5xx outage
            # recorded) — otherwise that give-up would be silent.
            ctx["forward_stall_exhausted"] = True
            _sp.xbmc.log(
                "NZB-DAV: Patient forward-stall budget exhausted at byte "
                "{} after {}s with no progress (result={}); giving up "
                "(reason=patient_forward_stall_exhausted)".format(
                    st.current, st.stall_wait_budget, st.result
                ),
                _sp.xbmc.LOGWARNING,
            )
        return None
