# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Prefetch / prewarm / prevalidation background warmers.

Stage-3 mixin split of ``stream_proxy.StreamProxy``. These methods were moved
verbatim; every reference to a ``stream_proxy`` module-level name is reached at
call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``StreamProxy``; they keep using ``self`` for instance state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _MgrPrefetchMixin:  # pylint: disable=too-few-public-methods
    """Prefetch / prewarm / prevalidation background warmers."""

    def _refresh_session_standby_fallbacks(self, ctx):
        """Resolve nzo-only standby fallbacks into usable WebDAV stream URLs.

        Runs the same resolution the failure-time cutover would, but AHEAD of
        the failure, so a primary stall becomes an instant source swap instead
        of a multi-second cold resolve under Kodi's read timeout.
        """
        handler = _sp._StreamHandler.__new__(_sp._StreamHandler)
        handler.server = self._server
        try:
            handler._refresh_standby_fallback_sources(ctx)
        except Exception as exc:  # pylint: disable=broad-except
            _sp.xbmc.log(
                "NZB-DAV: standby fallback resolve failed: {}".format(exc),
                _sp.xbmc.LOGWARNING,
            )

    def _prevalidate_fallback_sources(self, ctx):
        handler = _sp._StreamHandler.__new__(_sp._StreamHandler)
        handler.server = self._server
        try:
            validated = handler._prevalidate_ready_fallback_sources(ctx)
        except Exception as exc:  # pylint: disable=broad-except
            _sp.xbmc.log(
                "NZB-DAV: Fallback prevalidation failed: {}".format(exc),
                _sp.xbmc.LOGWARNING,
            )
            return
        if validated:
            _sp.xbmc.log(
                "NZB-DAV: Prevalidated {} fallback stream(s)".format(validated),
                _sp.xbmc.LOGINFO,
            )

    def _start_fallback_prevalidation(self, ctx):
        expected_length = _sp._StreamHandler._fallback_expected_content_length(ctx)
        if expected_length <= 0:
            _sp.xbmc.log(
                "NZB-DAV: prevalidation skipped (expected_length={})".format(
                    expected_length
                ),
                _sp.xbmc.LOGINFO,
            )
            return
        sources = ctx.get("fallback_sources") or []
        pending = [s for s in sources if _sp._fallback_source_needs_prevalidation(s)]
        _sp.xbmc.log(
            "NZB-DAV: prevalidation pending sources={}/{} expected_length={}".format(
                len(pending), len(sources), expected_length
            ),
            _sp.xbmc.LOGINFO,
        )
        if not pending:
            return

        # Coalesce bursts: fallbacks are pushed one-per-adopted-job, so mark
        # the session dirty and let a single running warmer re-loop to pick up
        # late arrivals, instead of spawning a thread per push.
        ctx["_fallback_prevalidation_dirty"] = True
        if _sp._thread_is_alive(ctx.get("_fallback_prevalidation_thread")):
            return

        def _warm():
            self._run_fallback_prevalidation_warmer(ctx)

        thread = _sp.threading.Thread(target=_warm, name="nzbdav-fallback-prevalidate")
        thread.daemon = True
        ctx["_fallback_prevalidation_thread"] = thread
        try:
            thread.start()
        except RuntimeError:
            # Out of thread budget — match the sibling prefetch spawns and fail
            # soft so prevalidation never wedges /prepare.
            ctx.pop("_fallback_prevalidation_thread", None)

    def _run_fallback_prevalidation_warmer(self, ctx):
        """Warmer-thread body: drain the dirty flag, resolving + fingerprinting.

        Resolves nzo-only standbys into WebDAV URLs, then fingerprints the
        resolved sources so the live cutover is an instant pointer swap. The
        dirty flag lets a merge that lands mid-pass trigger one more pass; it
        terminates once no new push has arrived.
        """
        prefetch_thread = ctx.get("_initial_range_prefetch_thread")
        if prefetch_thread and prefetch_thread is not _sp.threading.current_thread():
            try:
                prefetch_thread.join()
            except RuntimeError:
                pass
        while ctx.pop("_fallback_prevalidation_dirty", False):
            self._refresh_session_standby_fallbacks(ctx)
            self._prevalidate_fallback_sources(ctx)

    @staticmethod
    def _initial_range_prefetchable(ctx):
        """Return whether this pass-through context can prefetch byte 0."""
        if (
            ctx.get("remux")
            or ctx.get("faststart")
            or ctx.get("temp_faststart")
            or ctx.get("mode") == "hls"
            or not ctx.get("remote_url")
        ):
            return False
        try:
            return int(ctx.get("content_length", 0) or 0) > 0
        except (TypeError, ValueError):
            return False

    def _prefetch_initial_range(self, ctx):
        """Prime the first pass-through bytes for Kodi's initial range GET."""
        try:
            content_length = int(ctx.get("content_length", 0) or 0)
        except (TypeError, ValueError):
            return
        if content_length <= 0:
            return
        end = min(content_length - 1, _sp._UPSTREAM_READ_CHUNK - 1)
        try:
            body = _sp._StreamHandler._fetch_primary_range_bytes(
                ctx["remote_url"],
                ctx.get("auth_header"),
                0,
                end,
                content_length,
            )
        except Exception as exc:  # pylint: disable=broad-except
            _sp.xbmc.log(
                "NZB-DAV: Initial range prefetch failed: {}".format(exc),
                _sp.xbmc.LOGDEBUG,
            )
            return
        if body:
            _sp._StreamHandler._cache_fallback_range(
                ctx,
                ctx["remote_url"],
                ctx.get("auth_header"),
                content_length,
                0,
                body,
            )

    def _start_initial_range_prefetch(self, ctx):
        """Start byte-0 prefetch without delaying proxy prepare."""
        if not self._initial_range_prefetchable(ctx):
            return
        thread = _sp.threading.Thread(
            target=self._prefetch_initial_range,
            args=(ctx,),
            name="nzbdav-initial-range-prefetch",
        )
        thread.daemon = True
        ctx["_initial_range_prefetch_thread"] = thread
        try:
            thread.start()
        except RuntimeError:
            ctx.pop("_initial_range_prefetch_thread", None)

    def _run_readahead_prefetch(self, ctx):
        """Daemon target: fill the read-ahead window forward of the play head.

        Reads sequentially AHEAD of the highest-served offset in 64KB chunks
        using the same upstream-fetch primitive the serve path uses, throttles
        when the window is full (so it keeps filling WHILE PAUSED yet bounded),
        and resumes as free-behind reclaims room. Best-effort: a prefetch
        upstream error simply backs off and retries WITHOUT tearing down
        playback or tripping the user-facing recovery taxonomy / notifications /
        session counters. NEVER raises into anything.
        """
        setup = self._readahead_prefetch_setup(ctx)
        if setup is None:
            return
        buf, monitor, content_length = setup
        if self._readahead_defer_start(buf, monitor):
            return
        while not buf.should_stop() and not monitor.waitForAbort(0):
            try:
                if self._readahead_prefetch_once(ctx, buf, monitor, content_length):
                    return
            except Exception as exc:  # pylint: disable=broad-except
                _sp.xbmc.log(
                    "NZB-DAV: Read-ahead prefetch loop error: {}".format(exc),
                    _sp.xbmc.LOGDEBUG,
                )
                if monitor.waitForAbort(_sp._READAHEAD_ERROR_BACKOFF_SECONDS):
                    return

    @staticmethod
    def _readahead_prefetch_setup(ctx):
        """Validate ctx and build the read-ahead loop inputs.

        Returns ``(buf, monitor, content_length)`` when prefetch should run,
        or None when the ctx is missing a buffer / remote_url / positive
        content_length (the daemon then exits without looping).
        """
        buf = ctx.get(_sp._READAHEAD_BUFFER_KEY) if isinstance(ctx, dict) else None
        if buf is None:
            return None
        try:
            content_length = int(ctx.get("content_length", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not ctx.get("remote_url") or content_length <= 0:
            return None
        return buf, _sp.xbmc.Monitor(), content_length

    @staticmethod
    def _readahead_defer_start(buf, monitor):
        """Yield to startup before the first read-ahead fetch; True to abort.

        Lets the byte-0 prefetch and Kodi's first range fetch win nzbdav's
        connection budget before the read-ahead issues its first upstream
        read. Abortable so a shutdown during the defer exits cleanly. Returns
        True when the loop should not start (stop requested or abort).
        """
        return buf.should_stop() or monitor.waitForAbort(
            _sp._READAHEAD_START_DEFER_SECONDS
        )

    @staticmethod
    def _readahead_prefetch_once(ctx, buf, monitor, content_length):
        """Run one read-ahead fetch iteration; return True to stop the loop.

        Returns True when the lead has reached EOF or an abort was signalled
        during a backoff wait (the loop should `return`); False to continue
        to the next iteration. Mirrors the original inline body's throttle /
        error-backoff branches exactly.
        """
        fetch_offset = buf.next_fetch_offset()
        if fetch_offset >= content_length:
            # Lead is fully built to EOF; nothing left to prefetch.
            return True
        if buf.is_full():
            # Throttle: wait (abortably) for free-behind to reclaim room as
            # the play head advances. This is what keeps the buffer filling
            # while paused yet strictly bounded.
            return monitor.waitForAbort(_sp._READAHEAD_THROTTLE_BACKOFF_SECONDS)
        want = min(
            _sp._READAHEAD_FETCH_CHUNK,
            buf.space_remaining(),
            content_length - fetch_offset,
        )
        if want <= 0:
            return monitor.waitForAbort(_sp._READAHEAD_THROTTLE_BACKOFF_SECONDS)
        end = fetch_offset + want - 1
        # Re-read the source each iteration: a live fallback cutover mutates
        # ctx["remote_url"]/["auth_header"] mid-stream, so a once-hoisted local
        # would keep hammering the dead primary and the lead would stop growing
        # from the live source. The buffer is offset-addressed, so bytes from
        # either source are interchangeable for a given offset.
        remote_url = ctx.get("remote_url")
        auth_header = ctx.get("auth_header")
        if not remote_url:
            return monitor.waitForAbort(_sp._READAHEAD_ERROR_BACKOFF_SECONDS)
        body = _sp._StreamHandler._fetch_primary_range_bytes(
            remote_url, auth_header, fetch_offset, end, content_length
        )
        if not body:
            # Best-effort upstream error or awaiting-download: back off and
            # retry. The real serve path owns recovery; do not touch the
            # recovery taxonomy / notifications / counters here.
            return monitor.waitForAbort(_sp._READAHEAD_ERROR_BACKOFF_SECONDS)
        buf.append(fetch_offset, body)
        return False

    def _start_readahead_prefetch(self, ctx):
        """Spawn the per-session read-ahead prefetch daemon (gated + soft).

        Gated on the same passthrough-only guard as the byte-0 prefetch AND on
        readahead_buffer_mb>0 (read from the prefetched settings snapshot, never
        a per-request getSetting). RuntimeError-tolerant start matching the
        sibling prefetch spawns.
        """
        if not self._initial_range_prefetchable(ctx):
            return
        settings = _sp._passthrough_runtime_settings(ctx)
        mb = _sp._coerce_nonneg_int(settings.get("readahead_buffer_mb", 0))
        if mb <= 0:
            return
        content_length = _sp._coerce_nonneg_int(ctx.get("content_length", 0))
        if content_length <= 0:
            return
        cap_bytes = mb * 1024 * 1024
        buf = _sp.ReadAheadBuffer(cap_bytes, content_length)
        ctx[_sp._READAHEAD_BUFFER_KEY] = buf
        thread = _sp.threading.Thread(
            target=self._run_readahead_prefetch,
            args=(ctx,),
            name="nzbdav-readahead",
        )
        thread.daemon = True
        ctx[_sp._READAHEAD_THREAD_KEY] = thread
        try:
            thread.start()
        except RuntimeError:
            ctx.pop(_sp._READAHEAD_THREAD_KEY, None)
            ctx.pop(_sp._READAHEAD_BUFFER_KEY, None)

    def _prewarm_tail_range(self, ctx):
        """Warm nzbdav's FILE-TAIL article cache (MKV cues) for instant startup.

        Kodi reads the MKV cues/SeekHead at the file tail before it can play. For
        a usenet-backed file nzbdav fetches those end-of-file articles on demand,
        so Kodi's first tail read otherwise stalls 1-4s mid-startup — long enough
        to drain its not-yet-full cache and wedge the CoreELEC audio clock
        (permanent black screen). Issue a throwaway read of the last
        _TAIL_PREWARM_BYTES here, during the prepare gap, so nzbdav has the tail
        articles cached before Kodi asks. No proxy-side caching: the exact tail
        offset Kodi requests isn't fixed, and nzbdav serves subsequent tail reads
        fast once the articles are fetched. The body is discarded.
        """
        try:
            content_length = int(ctx.get("content_length", 0) or 0)
        except (TypeError, ValueError):
            return
        # Only warm a tail that is distinct from the byte-0 prefetch window;
        # tiny files are already fully covered by _prefetch_initial_range.
        if content_length <= _sp._TAIL_PREWARM_BYTES + _sp._UPSTREAM_READ_CHUNK:
            return
        # Yield to startup playback: hold the tail read back a short, abortable
        # beat so the byte-0 prefetch and Kodi's first-byte range request win
        # nzbdav's connection budget first. Without this the tail read RACED
        # them at prepare time and widened the very mid-startup stall window the
        # prewarm exists to close (transient black-screen). waitForAbort lets a
        # Kodi shutdown / session stop cancel the prewarm cleanly during the
        # defer instead of blocking the daemon thread or wasting a connection.
        if _sp.xbmc.Monitor().waitForAbort(_sp._TAIL_PREWARM_DEFER_SECONDS):
            return
        tail_start = content_length - _sp._TAIL_PREWARM_BYTES
        try:
            _sp._StreamHandler._fetch_primary_range_bytes(
                ctx["remote_url"],
                ctx.get("auth_header"),
                tail_start,
                content_length - 1,
                content_length,
            )
        except Exception as exc:  # pylint: disable=broad-except
            _sp.xbmc.log(
                "NZB-DAV: Tail prewarm failed: {}".format(exc),
                _sp.xbmc.LOGDEBUG,
            )

    def _start_tail_prewarm(self, ctx):
        """Warm the file tail in parallel with the byte-0 prefetch at prepare.

        Runs on its own thread so it neither delays /prepare nor serializes
        behind the byte-0 prefetch — Kodi reads the tail FIRST, so warming it
        must start as early as possible. Fails soft when out of thread budget,
        matching the sibling prefetch spawns.
        """
        if not self._initial_range_prefetchable(ctx):
            return
        thread = _sp.threading.Thread(
            target=self._prewarm_tail_range,
            args=(ctx,),
            name="nzbdav-tail-prewarm",
        )
        thread.daemon = True
        ctx["_tail_prewarm_thread"] = thread
        try:
            thread.start()
        except RuntimeError:
            ctx.pop("_tail_prewarm_thread", None)

    def _start_passthrough_runtime_settings_prefetch(self, ctx):
        """Read pass-through recovery settings during the player handoff gap."""
        if isinstance(ctx.get(_sp._PASSTHROUGH_RUNTIME_SETTINGS_KEY), dict):
            return
        if not self._initial_range_prefetchable(ctx):
            return
        done = _sp.threading.Event()
        ctx[_sp._PASSTHROUGH_RUNTIME_SETTINGS_DONE_KEY] = done

        def _worker():
            try:
                ctx[_sp._PASSTHROUGH_RUNTIME_SETTINGS_KEY] = (
                    _sp._read_passthrough_runtime_settings()
                )
            except Exception as exc:  # pylint: disable=broad-except
                ctx[_sp._PASSTHROUGH_RUNTIME_SETTINGS_ERROR_KEY] = exc
            finally:
                done.set()

        thread = _sp.threading.Thread(
            target=_worker,
            name="nzbdav-passthrough-settings-prefetch",
        )
        thread.daemon = True
        ctx["_passthrough_runtime_settings_thread"] = thread
        try:
            thread.start()
        except RuntimeError:
            ctx.pop("_passthrough_runtime_settings_thread", None)
            ctx.pop(_sp._PASSTHROUGH_RUNTIME_SETTINGS_DONE_KEY, None)
