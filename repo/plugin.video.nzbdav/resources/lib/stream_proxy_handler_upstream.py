# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Upstream range-request opening, contract checks, and chunk relay.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _UpstreamRelayMixin:  # pylint: disable=too-few-public-methods
    """Upstream range-request opening, contract checks, and chunk relay."""

    def _stream_upstream_prelude(self, ctx, start, end):
        """Serve any in-RAM read-ahead / cached prefix before the upstream open.

        Returns ``(start, written, early)`` where ``start``/``written`` are the
        advanced positions and ``early`` is ``None`` or a final
        ``(result_enum, written)`` tuple the caller must return immediately.
        Writes to ``self.wfile`` happen in the SAME order as the inline code.
        """
        written = 0
        # Read-ahead consult (additive, FIRST): serve the contiguous in-RAM
        # prefix the prefetch daemon built ahead of the play head. On a hit,
        # advance start/written; on a partial/empty result fall straight through
        # to today's untouched upstream-read / retry-ladder / patient-stall /
        # fallback-cutover / 404-awaiting path below. When the setting is 0 there
        # is no buffer on ctx, so this returns 0 immediately (identical to
        # today). The window only ever satisfies a contiguous-forward prefix, so
        # every existing branch keyed on result/written sees identical inputs on
        # the miss path.
        prefix_from_window = self._serve_from_readahead(ctx, start, end)
        if prefix_from_window:
            written += prefix_from_window
            start += prefix_from_window
            if start > end:
                return start, written, (_sp._UPSTREAM_RANGE_OK, written)
        cached_prefix = self._pop_cached_fallback_range(ctx, start, end)
        wait_consumed = bool(ctx.pop("_initial_range_prefetch_wait_consumed", False))
        if not cached_prefix and not wait_consumed:
            self._wait_for_initial_range_prefetch(ctx, start)
            cached_prefix = self._pop_cached_fallback_range(ctx, start, end)
        if cached_prefix:
            self.wfile.write(cached_prefix)
            written += len(cached_prefix)
            start += len(cached_prefix)
            if start > end:
                return start, written, (_sp._UPSTREAM_RANGE_OK, written)
        return start, written, None

    def _open_upstream_range_response(self, ctx, start, end, written):
        """Open the upstream Range request, classifying any open-time error.

        Returns ``(resp, early)``: on success ``resp`` is the live response and
        ``early`` is ``None``; on failure ``resp`` is ``None`` and ``early`` is
        the final ``(result_enum, written)`` tuple. Performs the post-open
        recovery signal + read-timeout arming verbatim on the success path.
        """
        req = _sp.Request(ctx["remote_url"])
        _sp._add_request_headers(req, ctx.get("auth_header"))
        req.add_header("Range", "bytes={}-{}".format(start, end))

        # Capture the observation timestamp BEFORE urlopen for the
        # flag-clearing path below. A post-urlopen time.time() would race a
        # concurrent thread that recorded a failure between socket-open and
        # response-processing — the earlier timestamp is conservative.
        observed_at = _sp.time.time()
        try:
            # nosemgrep
            resp = _sp.urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
                req, timeout=_sp._UPSTREAM_OPEN_TIMEOUT
            )
        except (OSError, ValueError) as e:
            return None, self._classify_upstream_open_error(ctx, e, start, written)

        # urlopen returned without raising → nzbdav gave a 2xx/3xx. Clear the
        # session "upstream down" flag so a later outage can re-notify (4xx/5xx
        # never reaches here — HTTPError is caught above). ``server`` may be
        # absent when a test builds __new__ directly; tolerate that. observed_at
        # lets the helper drop this signal if a concurrent thread recorded a
        # more-recent failure (notifier-flap race from the concurrency audit).
        _sp._record_upstream_recovered(
            getattr(self, "server", None), ctx, observed_at=observed_at
        )

        # Arm a tighter recv() deadline on the body socket now headers are in,
        # so a stalled backend surfaces as a recoverable read error within
        # _UPSTREAM_READ_TIMEOUT (driving live fallback) instead of blocking on
        # the inherited 60 s timeout and mis-logging as client_disconnected. #214
        _sp._set_upstream_read_timeout(resp, _sp._UPSTREAM_READ_TIMEOUT)
        return resp, None

    def _classify_upstream_open_error(self, ctx, e, start, written):
        """Map an open-time OSError/ValueError to a final ``(enum, written)``."""
        if _sp._is_terminal_http_client_error(e):
            code = getattr(e, "code", "?")
            # A 404 on an ESTABLISHED read (start > 0) is nzbdav saying "not
            # downloaded yet" (past its download high-water), NOT a permanent
            # path error: treat as a still-downloading short read so the retry
            # ladder + forward-stall wait + fallback cutover engage instead of a
            # hard CLIENT_ERROR abort (the "Dune died on a 404" incident). A 404
            # on the INITIAL open (start == 0, byte 0 must exist) is a genuine
            # missing path and stays terminal; 401/403 are always terminal
            # (waiting can't fix bad creds). An already-written prefix is real
            # progress → report a recoverable short read.
            if code == 404 and start > 0:
                _sp.xbmc.log(
                    "NZB-DAV: Upstream 404 at byte {} on an established "
                    "stream; treating as awaiting-download (nzbdav past "
                    "its download high-water) "
                    "(reason=client_error_awaiting_download)".format(start),
                    _sp.xbmc.LOGWARNING,
                )
                if written:
                    return _sp._UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, written
                return _sp._UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD, 0
            _sp.xbmc.log(
                "NZB-DAV: Proxy upstream client error at byte {}: HTTP {} "
                "(reason=upstream_client_error)".format(start, code),
                _sp.xbmc.LOGERROR,
            )
            if code in (401, 403):
                _sp._notify_error(
                    "WebDAV returned HTTP {}; check credentials/path".format(code)
                )
            return _sp._UPSTREAM_RANGE_CLIENT_ERROR, written
        category = _sp._classify_upstream_error(e)
        _sp.xbmc.log(
            "NZB-DAV: Proxy upstream open failed at byte {}: {} "
            "(reason=upstream_open_failed category={})".format(start, e, category),
            _sp.xbmc.LOGWARNING,
        )
        if category in (
            _sp._UPSTREAM_REACHABILITY_UNREACHABLE_NETWORK,
            _sp._UPSTREAM_REACHABILITY_HTTP_SERVER_ERROR,
        ):
            _sp._record_upstream_unreachable(self.server, ctx, e)
        if written:
            return _sp._UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, written
        return _sp._UPSTREAM_RANGE_UPSTREAM_ERROR, 0

    @staticmethod
    def _check_upstream_contract(resp, ctx, start, end, contract_mode):
        """Inspect response headers for a Range-contract mismatch.

        Returns ``(headers, early)`` where ``headers`` is
        ``(status, content_range, content_length, mismatch_detail,
        hard_mismatch)`` and ``early`` is ``None`` or a final
        ``(result_enum, written)`` tuple (hard mismatch rejection).
        """
        status = getattr(resp, "status", None) or resp.getcode()
        content_range = _sp._get_header(resp, "Content-Range")
        content_length = _sp._get_header(resp, "Content-Length")
        mismatch_detail = None
        hard_mismatch = False

        if contract_mode != _sp._STRICT_CONTRACT_MODE_OFF:
            mismatch_detail, hard_mismatch = _sp._classify_contract_mismatch(
                status,
                content_range,
                content_length,
                start,
                end,
                ctx["content_length"],
            )
            if mismatch_detail:
                _sp._log_contract_mismatch(
                    start,
                    end,
                    status,
                    content_range,
                    content_length,
                    mismatch_detail,
                )
                if hard_mismatch:
                    # Hard mismatch (e.g. 206 with wrong Content-Range)
                    # would feed wrong bytes to Kodi at wrong offsets —
                    # silent corruption. Reject regardless of mode.
                    # Soft mismatches (status 200 + valid range covering
                    # the full object, which nzbdav legitimately produces
                    # for `Range: bytes=0-`) fall through and stream so
                    # ENFORCE doesn't kill playback at byte 0.
                    # Per TODO.md §D.8.1.
                    return None, (_sp._UPSTREAM_RANGE_PROTOCOL_MISMATCH, 0)
        headers = (
            status,
            content_range,
            content_length,
            mismatch_detail,
            hard_mismatch,
        )
        return headers, None

    @staticmethod
    def _read_upstream_chunk(resp, headers, start, end, written):
        """Read one upstream chunk, classifying a read-time failure.

        Returns ``(chunk, early)``: ``chunk`` is the bytes read (possibly empty
        on EOF) with ``early`` ``None``, or ``chunk`` is ``None`` and ``early``
        is the final ``(result_enum, written)`` tuple on a read error.
        """
        status, content_range, content_length = headers[0], headers[1], headers[2]
        try:
            # 64 KB chunks — on 32-bit Kodi the whole process has
            # ~3 GB of address space, and Kodi's CFileCache alone can
            # reserve up to 1.5 GB (cachemembuffersize * readbufferfactor).
            # A 1 MB read buffer used to hit MemoryError when a second
            # connection opened during recovery doubled the proxy's
            # live allocations. 64 KB matches the zero-fill buffer
            # size and is allocation-friendly on a fragmented heap.
            chunk = resp.read(_sp._UPSTREAM_READ_CHUNK)
        except (MemoryError, OSError, ValueError) as e:
            _sp.xbmc.log(
                "NZB-DAV: Proxy upstream read failed at byte {}: {}".format(
                    start + written, e
                )
                + " (reason=upstream_read_failed)",
                _sp.xbmc.LOGWARNING,
            )
            if written:
                _sp.xbmc.log(
                    "NZB-DAV: Upstream short read for {}-{} wrote={} "
                    "status={} Content-Range={!r} Content-Length={!r} "
                    "(reason=short_read_recoverable)".format(
                        start,
                        end,
                        written,
                        status,
                        content_range,
                        content_length,
                    ),
                    _sp.xbmc.LOGWARNING,
                )
                return None, (_sp._UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, written)
            return None, (_sp._UPSTREAM_RANGE_UPSTREAM_ERROR, 0)
        return chunk, None

    @staticmethod
    def _handle_upstream_eof(headers, requested, start, end, written):
        """Classify a clean EOF (empty chunk) into a final ``(enum, written)``."""
        status, content_range, content_length = headers[0], headers[1], headers[2]
        mismatch_detail, hard_mismatch = headers[3], headers[4]
        if written == requested:
            if mismatch_detail and hard_mismatch:
                return _sp._UPSTREAM_RANGE_PROTOCOL_MISMATCH, written
            return _sp._UPSTREAM_RANGE_OK, written
        _sp.xbmc.log(
            (
                "NZB-DAV: Upstream short read for {}-{} wrote={} "
                "expected={} status={} Content-Range={!r} "
                "Content-Length={!r} "
                "(reason=short_read_awaiting_download)"
            ).format(
                start,
                end,
                written,
                requested,
                status,
                content_range,
                content_length,
            ),
            _sp.xbmc.LOGWARNING,
        )
        # Clean EOF before the full range: the upload is still
        # downloading and hasn't reached this byte yet. Wait on the
        # primary (retry ladder) instead of declaring fallbacks
        # exhausted. See _UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD.
        return _sp._UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD, written

    def _passthrough_throughput_watchdog(self, ctx, start, written, chunk_len):
        """Sample the per-chunk throughput watchdog; may return early or raise.

        Returns ``None`` to continue the loop or a final ``(enum, written)``
        tuple (recoverable cutover). Raises ``socket.timeout`` on a sub-floor
        stall with no fallback attached, exactly as the inline code did.
        """
        if not _sp._passthrough_watchdog_applies(ctx):
            return None
        # Self-initialize (tests call this directly; _serve_proxy resets per
        # request). Explicit `in` check rather than ctx.setdefault(...,
        # time.monotonic()) which would evaluate time.monotonic() every
        # iteration even when the key exists — wasted clock reads on the hot
        # per-chunk path AND non-deterministic for finite mocked side_effects.
        if "passthrough_window_t0" not in ctx:
            ctx["passthrough_window_t0"] = _sp.time.monotonic()
        ctx["passthrough_window_bytes"] = (
            ctx.get("passthrough_window_bytes", 0) + chunk_len
        )
        window_elapsed = _sp.time.monotonic() - ctx["passthrough_window_t0"]
        if window_elapsed < _sp._PASSTHROUGH_THROUGHPUT_WINDOW_SECONDS:
            return None
        bps = ctx["passthrough_window_bytes"] / window_elapsed
        if bps < _sp._PASSTHROUGH_MIN_THROUGHPUT_BPS:
            return self._passthrough_stall_action(
                ctx, start, written, bps, window_elapsed
            )
        ctx["passthrough_window_t0"] = _sp.time.monotonic()
        ctx["passthrough_window_bytes"] = 0
        return None

    @staticmethod
    def _passthrough_stall_action(ctx, start, written, bps, window_elapsed):
        """React to a confirmed sub-floor window: cut over or raise a stall.

        Returns a recoverable ``(enum, written)`` when a fallback is attached,
        else marks the stall on ``ctx`` and raises ``socket.timeout`` exactly
        as the inline code did.
        """
        # With a live fallback attached, a sustained sub-floor trickle should
        # switch sources rather than reconnect to the SAME stalled upload
        # (which just wedges again). Returning recoverable hands control to
        # _serve_proxy's cutover; the no-fallback path is unchanged so
        # passthrough_stall reconnect + audio-skip holds. #214.
        if ctx.get("fallback_sources"):
            _sp.xbmc.log(
                "NZB-DAV: Pass-through trickle below floor "
                "at byte {} ({:.0f} B/s over {:.1f}s) with "
                "fallback attached; returning recoverable to "
                "trigger cutover "
                "(reason=passthrough_stall_fallback)".format(
                    start + written, bps, window_elapsed
                ),
                _sp.xbmc.LOGWARNING,
            )
            return _sp._UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, written
        # Mark before raising so _serve_proxy distinguishes stall-induced unwind
        # from a real Kodi disconnect. socket.timeout (an OSError subclass)
        # bypasses the inner read-loop except (which would mis-classify it as
        # upstream short read) and reaches the outer handler.
        ctx["passthrough_stall_detected"] = True
        ctx["passthrough_stall_bps"] = bps
        ctx["passthrough_stall_window_seconds"] = window_elapsed
        raise _sp._socket.timeout(
            "passthrough throughput stall: "
            "{:.0f} B/s over {:.1f}s".format(bps, window_elapsed)
        )

    @staticmethod
    def _trim_chunk_to_range(headers, chunk, remaining, start, end, contract_mode):
        """Clip a chunk to the bytes still requested, logging an overshoot."""
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
            mismatch_detail = headers[3] or "read beyond requested range"
            if contract_mode != _sp._STRICT_CONTRACT_MODE_OFF:
                _sp._log_contract_mismatch(
                    start,
                    end,
                    headers[0],
                    headers[1],
                    headers[2],
                    mismatch_detail,
                )
        return chunk

    def _relay_upstream_chunk(self, ctx, headers, chunk, requested, geom):
        """Trim, write, and account one non-empty chunk; run mid-stream checks.

        ``geom`` is ``(requested_start, start, end, written, contract_mode)``.
        Returns ``(written, early)``: ``written`` is the updated byte count and
        ``early`` is ``None`` to continue or a final ``(enum, written)`` tuple.
        May raise ``socket.timeout`` from the throughput watchdog.
        """
        requested_start, start, end, written, contract_mode = geom
        chunk = self._trim_chunk_to_range(
            headers, chunk, requested - written, start, end, contract_mode
        )
        self.wfile.write(chunk)
        written += len(chunk)
        # Tell the read-ahead buffer how far the play head has been served
        # straight from upstream, so the prefetch daemon advances its base
        # instead of re-fetching bytes already behind the play head. The hit
        # path bumps the high-water inside _serve_from_readahead; this covers
        # upstream-served bytes. Idempotent (max-based) so no double-count.
        readahead_buf = ctx.get(_sp._READAHEAD_BUFFER_KEY)
        if readahead_buf is not None:
            readahead_buf.update_served_high_water(requested_start + written)
        # Env-gated fault injection: a long-lived connection opened below the
        # threshold can stream past it, so re-check the absolute position
        # mid-stream. Inert unless the fault env var is set.
        if _sp._fault_forced_primary_failure(ctx, requested_start + written):
            _sp.xbmc.log(
                "NZB-DAV: [FAULT] forcing primary upstream failure "
                "mid-stream at byte {} ({}) (reason=fault_injection)".format(
                    requested_start + written,
                    _sp._FAULT_PRIMARY_FAIL_AFTER_BYTES_ENV,
                ),
                _sp.xbmc.LOGWARNING,
            )
            return written, (_sp._UPSTREAM_RANGE_UPSTREAM_ERROR, written)
        # Throughput watchdog: only sampled inside the read loop because that's
        # where bytes-in-flight match what Kodi sees (a _serve_proxy-level check
        # would mis-attribute zero-fill bytes as real progress). Video-only —
        # audio bit rates legitimately fall below the 100 KB/s floor.
        early = self._passthrough_throughput_watchdog(ctx, start, written, len(chunk))
        return written, early

    def _stream_upstream_read_loop(self, ctx, resp, headers, requested, geom):
        """Drive the per-chunk read/write loop until a final result.

        ``geom`` is ``(requested_start, start, end, written, contract_mode)``
        where ``written`` seeds the byte count with the prelude/prefix bytes the
        caller already accounted. Returns the final ``(result_enum, written)``
        tuple.
        """
        requested_start, start, end, written, contract_mode = geom
        while True:
            chunk, early = self._read_upstream_chunk(resp, headers, start, end, written)
            if early is not None:
                return early
            if not chunk:
                return self._handle_upstream_eof(
                    headers, requested, start, end, written
                )
            written, early = self._relay_upstream_chunk(
                ctx,
                headers,
                chunk,
                requested,
                (requested_start, start, end, written, contract_mode),
            )
            if early is not None:
                return early

    def _stream_upstream_range(self, ctx, start, end, contract_mode=None):
        """Stream bytes from upstream to the client.

        Returns ``(result_enum, written_bytes)`` where ``result_enum`` is one
        of OK / SHORT_READ_RECOVERABLE / PROTOCOL_MISMATCH / UPSTREAM_ERROR.
        BrokenPipeError / ConnectionResetError propagate out so the caller can
        abort cleanly.
        """
        if _sp._fault_forced_primary_failure(ctx, start):
            _sp.xbmc.log(
                "NZB-DAV: [FAULT] forcing primary upstream failure at byte {} "
                "({}) (reason=fault_injection)".format(
                    start, _sp._FAULT_PRIMARY_FAIL_AFTER_BYTES_ENV
                ),
                _sp.xbmc.LOGWARNING,
            )
            return _sp._UPSTREAM_RANGE_UPSTREAM_ERROR, 0
        requested_start = start
        start, written, early = self._stream_upstream_prelude(ctx, start, end)
        if early is not None:
            return early

        contract_mode = contract_mode or _sp._get_strict_contract_mode()
        requested = end - requested_start + 1

        resp, early = self._open_upstream_range_response(ctx, start, end, written)
        if early is not None:
            return early

        try:
            headers, early = self._check_upstream_contract(
                resp, ctx, start, end, contract_mode
            )
            if early is not None:
                return early
            return self._stream_upstream_read_loop(
                ctx,
                resp,
                headers,
                requested,
                (requested_start, start, end, written, contract_mode),
            )
        finally:
            try:
                resp.close()
            except OSError:
                pass

    @staticmethod
    def _find_skip_offset(ctx, failed_byte, range_end):
        """Probe forward to find a skip size past a bad article region.

        Tries progressively larger skips and confirms upstream can serve a
        small range starting at the new offset. Each skip size is retried
        with backoff so a briefly-unavailable upstream (restart, transient
        network blip) has a chance to come back before we declare the
        region unrecoverable. Returns the skip in bytes or None if the
        recovery budget is exhausted.

        Fast-fail when the session already knows upstream is down:
        ``_record_upstream_unreachable`` sets ``upstream_down_notified``
        the first time an outage is detected, and the flag stays set
        until a subsequent successful urlopen clears it via
        ``_record_upstream_recovered``. While the flag is set, every
        probe in this function would hit the same DNS/TCP failure and
        burn the full 30 s recovery budget per byte-range request —
        turning a single outage into 30 s of stall on every seek.
        Short-circuit to None so the caller zero-fills immediately and
        Kodi can abort cleanly instead of grinding through probes.
        """
        if ctx.get("upstream_down_notified"):
            _sp.xbmc.log(
                "NZB-DAV: Skip-probe short-circuited (upstream marked down; "
                "session will recover on next successful byte-range) "
                "(reason=skip_probe_circuit_breaker)",
                _sp.xbmc.LOGINFO,
            )
            return None

        # Use monotonic for elapsed-time tracking — wall-clock NTP jumps
        # would otherwise either prematurely abort recovery (backward
        # jump) or stretch the deadline indefinitely (forward jump).
        start_time = _sp.time.monotonic()
        for skip in _sp._SKIP_PROBE_SIZES:
            target = failed_byte + skip
            if target > range_end:
                return None
            probe_end = min(target + 1023, range_end)
            succeeded, aborted = _sp._StreamHandler._retry_skip_probe(
                ctx, skip, target, probe_end, start_time
            )
            if succeeded:
                return skip
            if aborted:
                return None
        return None
