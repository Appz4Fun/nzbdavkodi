# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Passthrough proxy stall/recovery/zero-fill steps and finalize.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _ProxyServeStallMixin:  # pylint: disable=too-few-public-methods
    """Passthrough proxy stall/recovery/zero-fill steps and finalize."""

    def _serve_proxy_stall_wait(self, ctx, st, now):
        """Log the stall and wait one abortable backoff. Verbatim move.

        Returns ``"return"`` on a Kodi abort (client teardown) or ``"continue"``
        to re-read after the backoff elapses.
        """
        _sp.xbmc.log(
            (
                "NZB-DAV: Established forward stream stalled at "
                "byte {} (result={}); holding client open and "
                "re-reading (elapsed={:.0f}s/{}s, "
                "reason=patient_forward_stall)"
            ).format(
                st.current,
                st.result,
                now - st.forward_stall_t0,
                st.stall_wait_budget,
            ),
            _sp.xbmc.LOGINFO,
        )
        # Abortable: a Kodi shutdown / session stop breaks the
        # wait immediately so the budget never blocks teardown.
        if _sp.xbmc.Monitor().waitForAbort(_sp._PASSTHROUGH_STALL_WAIT_BACKOFF_SECONDS):
            # waitForAbort True == Kodi closing the session: a
            # client-side teardown, never an upstream error. Mark
            # it benign (matches the byte-write client-disconnect
            # at the bottom of the loop) so the finally block logs
            # at INFO and never blames a pending fallback candidate
            # for a clean shutdown. Without this it stays "unknown"
            # and a clean teardown reads as a genuine failure.
            st.terminal_reason = "client_disconnected"
            return "return"
        # Reset the throughput watchdog window so the stale
        # pre-wait sample can't trip a spurious stall on the first
        # post-recovery read.
        ctx["passthrough_window_t0"] = _sp.time.monotonic()
        ctx["passthrough_window_bytes"] = 0
        return "continue"

    def _serve_proxy_capfire_step(self, ctx, st):
        """Close cleanly once the bounded-exhaustion cap fires. Verbatim move."""
        # BOUNDED EXHAUSTION (F4) cap-fire: now that the retry ladder
        # has run AND the progress-reset above has cleared the count on
        # any genuine byte, a still-positive fall-through count at the
        # cap means the primary spent its final ladder attempt with no
        # recovery and no validated candidate. Close cleanly with
        # fallback_exhausted instead of looping — WITHOUT reintroducing
        # e3a74a1's immediate hard-close. A recovering primary's count
        # is 0 here (the SM-1 reset fired), so this won't condemn it.
        # Guard: when the final ladder attempt returned a terminal
        # 401/403/contract-mismatch, fall through to the
        # CLIENT_ERROR/PROTOCOL_MISMATCH branch below so the real root
        # cause is surfaced instead of being masked as fallback_exhausted.
        if (
            st.fallback_pending_fallthroughs >= _sp._FALLBACK_PENDING_FALLTHROUGH_MAX
            and st.result
            not in (
                _sp._UPSTREAM_RANGE_CLIENT_ERROR,
                _sp._UPSTREAM_RANGE_PROTOCOL_MISMATCH,
            )
        ):
            st.terminal_reason = "fallback_exhausted"
            _sp.xbmc.log(
                "NZB-DAV: Fallback chain exhausted at byte {} "
                "after {} fruitless cutover re-entries with no "
                "validated source and no primary progress; "
                "closing cleanly (reason={})".format(
                    st.current,
                    st.fallback_pending_fallthroughs,
                    st.terminal_reason,
                ),
                _sp.xbmc.LOGERROR,
            )
            return "return"
        return None

    def _serve_proxy_abort_step(self, ctx, st):
        """Abort on a terminal client/protocol error result. Verbatim move."""
        if st.result in (
            _sp._UPSTREAM_RANGE_CLIENT_ERROR,
            _sp._UPSTREAM_RANGE_PROTOCOL_MISMATCH,
        ):
            st.terminal_reason = (
                "upstream_client_error"
                if st.result == _sp._UPSTREAM_RANGE_CLIENT_ERROR
                else "protocol_mismatch"
            )
            _sp.xbmc.log(
                "NZB-DAV: Aborting pass-through at byte {} "
                "(result={}, reason={})".format(
                    st.current, st.result, st.terminal_reason
                ),
                _sp.xbmc.LOGERROR,
            )
            return "return"
        return None

    def _serve_proxy_recovery_exhausted(self, st, skip, remaining):
        """Close when no probe is readable or the zero-fill budget is spent."""
        if skip is None or (
            st.zero_fill_budget_enabled
            and st.total_skipped + skip > _sp._MAX_TOTAL_ZERO_FILL
        ):
            st.terminal_reason = "recovery_exhausted"
            detail = (
                "no readable probe" if skip is None else "zero-fill budget exceeded"
            )
            _sp.xbmc.log(
                "NZB-DAV: Zero-fill recovery exhausted at byte {}; "
                "closing with {} bytes unread ({}, reason={})".format(
                    st.current, remaining, detail, st.terminal_reason
                ),
                _sp.xbmc.LOGERROR,
            )
            return True
        return False

    def _serve_proxy_density_breaker_tripped(self, st, skip):
        """Close when the recovery density breaker trips. Verbatim move."""
        if st.density_breaker_enabled and _sp._would_trip_density_breaker(
            st.density_window, skip
        ):
            st.terminal_reason = "density_breaker_tripped"
            _sp.xbmc.log(
                "NZB-DAV: Recovery density breaker tripped at byte {} "
                "(result={}, skip={}, ratio={:.2f}, reason={})".format(
                    st.current,
                    st.result,
                    skip,
                    _sp._density_ratio(st.density_window),
                    st.terminal_reason,
                ),
                _sp.xbmc.LOGWARNING,
            )
            try:
                _sp._notify(
                    "NZB-DAV",
                    "Stream aborted after repeated zero-fill recovery",
                )
            except (RuntimeError, OSError):
                pass
            return True
        return False

    def _serve_proxy_zerofill_step(self, ctx, st):
        """Probe forward and zero-fill the unreadable gap. Verbatim move."""
        remaining = st.end - st.current + 1
        skip = self._find_skip_offset(st.active_ctx, st.current, st.end)

        if self._serve_proxy_recovery_exhausted(st, skip, remaining):
            return "return"
        if self._serve_proxy_density_breaker_tripped(st, skip):
            return "return"
        if self._serve_proxy_session_budget_exceeded(ctx, st, skip):
            return "return"

        self._write_zeros(skip)
        st.total_skipped += skip
        st.recovery_count += 1
        st.current += skip
        _sp._record_density_window(st.density_window, "zero_fill", skip)
        _sp._update_session_recovery_state(
            self.server, ctx, zero_fill=skip, recoveries=1
        )
        _sp._maybe_notify_recovery_summary(self.server, ctx)
        _sp.xbmc.log(
            "NZB-DAV: Zero-filled {} bytes at offset {} to skip bad "
            "usenet articles (reason=zero_fill_resume)".format(skip, st.current - skip),
            _sp.xbmc.LOGWARNING,
        )
        return None

    def _serve_proxy_session_budget_exceeded(self, ctx, st, skip):
        """Check (and report) the session zero-fill ratio budget. Verbatim move.

        Returns True when the projected ratio exceeds the cap and the stream
        must close, else False.
        """
        projected_zero_fill = None
        projected_recoveries = None
        projected_ratio = None
        if st.zero_fill_budget_enabled:
            (
                projected_zero_fill,
                projected_recoveries,
                projected_ratio,
            ) = _sp._project_session_zero_fill_ratio(
                self.server, ctx, extra_zero_fill=skip, extra_recoveries=1
            )
            if projected_ratio > _sp._SESSION_ZERO_FILL_RATIO_MAX:
                st.terminal_reason = "session_zero_fill_budget_exceeded"
                _sp.xbmc.log(
                    "NZB-DAV: Session zero-fill budget exceeded at byte {} "
                    "(projected_ratio={:.3f}, skipped={}, recoveries={}, "
                    "reason={})".format(
                        st.current,
                        projected_ratio,
                        projected_zero_fill,
                        projected_recoveries,
                        st.terminal_reason,
                    ),
                    _sp.xbmc.LOGWARNING,
                )
                _sp._maybe_notify_recovery_summary(
                    self.server,
                    ctx,
                    zero_fill_bytes=projected_zero_fill,
                    recovery_count=projected_recoveries,
                )
                return True
        return False

    def _serve_proxy_classify_disconnect(self, st):
        """Classify a BrokenPipe/timeout teardown. Verbatim move of the except."""
        # socket.timeout has TWO causes here:
        #   1. Kodi stopped reading from us for longer than
        #      _REMUX_WRITE_TIMEOUT (DB vacuum, decoder stall) — surfaces
        #      as terminal_reason="client_disconnected".
        #   2. The throughput watchdog detected upstream-driven trickle
        #      that Kodi can't keep up with — surfaces as
        #      terminal_reason="passthrough_stall". The
        #      ``passthrough_stall_detected`` ctx flag is set right
        #      before the raise so we can tell them apart here.
        # Either way we unwind the handler and let BaseHTTPServer tear
        # down the socket; Kodi's CCurlFile will reconnect if it still
        # wants bytes.
        if st.active_ctx.get("passthrough_stall_detected"):
            st.terminal_reason = "passthrough_stall"
            _sp.xbmc.log(
                "NZB-DAV: Pass-through stall at byte {} "
                "(rate={:.0f} B/s over {:.1f}s; threshold={} B/s) — "
                "closing to force Kodi reconnect (reason={})".format(
                    st.current,
                    st.active_ctx.get("passthrough_stall_bps", 0.0),
                    st.active_ctx.get("passthrough_stall_window_seconds", 0.0),
                    _sp._PASSTHROUGH_MIN_THROUGHPUT_BPS,
                    st.terminal_reason,
                ),
                _sp.xbmc.LOGWARNING,
            )
        else:
            st.terminal_reason = "client_disconnected"
            # client_disconnected is Kodi closing a connection (a demuxer
            # probe/seek abandoning a range, or a normal stop) — a
            # client-side event, never an upstream error. Log at INFO so
            # routine startup-probe churn doesn't masquerade as warnings.
            _sp.xbmc.log(
                "NZB-DAV: Pass-through write aborted at byte {} "
                "(client stalled or disconnected, reason={})".format(
                    st.current, st.terminal_reason
                ),
                _sp.xbmc.LOGINFO,
            )

    def _serve_proxy_finalize(self, ctx, st):
        """Flush fallback toasts and emit the summary. Verbatim move of finally.

        Benign reasons log at INFO; failures at WARNING and blame a pending
        candidate. A candidate switched AWAY from is always failed; a still-
        pending never-delivered candidate too (the F11 hole) — EXCEPT on
        client_disconnected, which (per 4decdd4) can only arise at the client
        body write AFTER a non-empty read, so the candidate WAS serving bytes.
        """
        _benign_summary = st.terminal_reason in ("complete", "client_disconnected")
        if st.fallback_failed_to_notify is not None:
            _sp._notify_fallback_outcome(st.fallback_failed_to_notify, False)
            st.fallback_failed_to_notify = None
        if (
            st.fallback_pending_candidate is not None
            and not st.candidate_delivered
            and st.terminal_reason != "client_disconnected"
        ):
            _sp._notify_fallback_outcome(st.fallback_pending_candidate, False)
        st.fallback_pending_candidate = None
        _sp.xbmc.log(
            "NZB-DAV: Pass-through summary reason={} range={}-{} "
            "streamed={} zero_fill={} recoveries={} "
            "upstream_unreachable={} upstream_notified={} "
            "session_streamed={} session_zero_fill={}".format(
                st.terminal_reason,
                st.start,
                st.end,
                st.total_streamed,
                st.total_skipped,
                st.recovery_count,
                ctx.get("upstream_unreachable_count", 0),
                bool(ctx.get("upstream_down_notified")),
                ctx.get("session_streamed_bytes", 0),
                ctx.get("session_zero_fill_bytes", 0),
            ),
            _sp.xbmc.LOGINFO if _benign_summary else _sp.xbmc.LOGWARNING,
        )
        # Graceful-starvation guard: if this stream ended because the
        # backend could not keep up (a sustained outage or a throughput
        # stall), tell the user once instead of leaving a silent black
        # screen. No-op on a clean finish or a healthy user stop.
        _sp._maybe_notify_stream_starvation(
            self.server,
            ctx,
            st.terminal_reason,
            st.total_streamed,
            st.end - st.start + 1,
        )

    def _retry_original_range(self, ctx, start, end, contract_mode, first_byte=False):
        """Retry the still-unread upstream range before falling back to skip.

        Fast-fail when the session already knows upstream is down.
        Same reasoning as ``_find_skip_offset``'s circuit breaker: the
        retry ladder would otherwise sleep-and-retry through its entire
        delay schedule against a known-failing upstream on every range,
        turning a sustained outage into seconds of stall per seek.

        ``first_byte`` selects a short backoff schedule
        (``_FIRST_BYTE_RANGE_RETRY_DELAYS``) when no byte has been streamed
        yet: the long (2, 4, 8) ladder would hold Kodi's initial open silent
        past its first-read patience, so it disconnects at byte 0
        (``streamed=0``). The long ladder is kept for mid-stream rebuffering,
        where Kodi is already playing and will wait.
        """
        if ctx.get("upstream_down_notified"):
            _sp.xbmc.log(
                "NZB-DAV: Retry ladder short-circuited (upstream marked down) "
                "(reason=retry_ladder_circuit_breaker)",
                _sp.xbmc.LOGINFO,
            )
            return _sp._UPSTREAM_RANGE_UPSTREAM_ERROR, 0, start

        current = start
        total_written = 0
        last_result = _sp._UPSTREAM_RANGE_UPSTREAM_ERROR

        # ``waitForAbort`` instead of ``time.sleep`` so a Kodi shutdown
        # signal during the retry-ladder backoff aborts the wait and
        # returns immediately. The previous ``time.sleep`` would block
        # the handler for the full delay even after Kodi started
        # tearing down. TODO.md §H.2-M14.
        monitor = _sp.xbmc.Monitor()
        delays = (
            _sp._FIRST_BYTE_RANGE_RETRY_DELAYS
            if first_byte
            else _sp._RANGE_RETRY_DELAYS
        )
        for delay in delays:
            if monitor.waitForAbort(delay):
                return last_result, total_written, current
            result, written = self._stream_upstream_range(
                ctx, current, end, contract_mode=contract_mode
            )
            total_written += written
            current += written
            last_result = result
            if current > end:
                return result, total_written, current
            if result not in (
                _sp._UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
                _sp._UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD,
                _sp._UPSTREAM_RANGE_UPSTREAM_ERROR,
            ):
                return result, total_written, current

        return last_result, total_written, current

    def _serve_from_readahead(self, ctx, start, end):
        """Write the contiguous read-ahead prefix at ``start`` to the client.

        Returns the number of bytes written directly from the in-RAM window
        (0 on miss / no buffer). On a hit the served bytes are freed behind the
        play head and the served high-water is bumped (enabling free-behind so
        the prefetch thread reclaims room and advances its base). On a miss the
        caller falls through to today's untouched upstream-read path.

        BrokenPipeError / ConnectionResetError from wfile.write propagate
        exactly like the existing cached-prefix write so _serve_proxy's outer
        except still classifies a client disconnect.
        """
        buf = ctx.get(_sp._READAHEAD_BUFFER_KEY) if isinstance(ctx, dict) else None
        if buf is None:
            return 0
        body = buf.read_prefix(start, end)
        if not body:
            return 0
        self.wfile.write(body)
        served_to = start + len(body)
        buf.free_behind(served_to)
        buf.update_served_high_water(served_to)
        return len(body)
