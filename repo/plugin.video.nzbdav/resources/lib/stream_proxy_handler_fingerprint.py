# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Fallback-content fingerprint validation (serial and parallel probes).

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _FingerprintMixin:  # pylint: disable=too-few-public-methods
    """Fallback-content fingerprint validation (serial and parallel probes)."""

    @staticmethod
    def _fallback_fingerprint_ranges(ctx, content_length):
        """Return cached byte ranges used to compare fallback streams."""
        cache = ctx.setdefault("_fallback_fingerprint_ranges", {})
        if content_length not in cache:
            from resources.lib.fallback_streams import fingerprint_ranges

            cache[content_length] = tuple(fingerprint_ranges(content_length))
        return cache[content_length]

    def _validate_fallback_fingerprint(
        self,
        ctx,
        source,
        content_length,
        probe_bases,
        current_range=None,
        primary_url=None,
        fallback_url=None,
        fallback_auth=_sp._AUTH_HEADER_NOT_PROVIDED,
        primary_auth=_sp._AUTH_HEADER_NOT_PROVIDED,
        cache_fallback_range_bytes=False,
    ):
        """Return True only when every sampled range provably matches.

        Bool wrapper over :meth:`_classify_fallback_fingerprint`. A
        transient INCONCLUSIVE probe is treated as a non-match here — the
        prevalidation caller only wants to mark a source ``validated`` when
        it is byte-proven, and a transient miss simply stays unvalidated to
        be retried later.
        """
        return (
            self._classify_fallback_fingerprint(
                ctx,
                source,
                content_length,
                probe_bases,
                current_range,
                primary_url,
                fallback_url,
                fallback_auth,
                primary_auth,
                cache_fallback_range_bytes,
            )
            is _sp._FALLBACK_MATCH
        )

    def _resolve_fingerprint_identity(
        self, ctx, source, primary_auth, fallback_url, fallback_auth
    ):
        """Default the primary auth / fallback URL+auth from ctx and source."""
        if primary_auth is _sp._AUTH_HEADER_NOT_PROVIDED:
            primary_auth = ctx.get("auth_header")
        if fallback_url is None:
            fallback_url = self._fallback_source_stream_url(ctx, source)
        if fallback_auth is _sp._AUTH_HEADER_NOT_PROVIDED:
            fallback_auth = self._fallback_source_auth(source)
        return primary_auth, fallback_url, fallback_auth

    def _classify_fallback_fingerprint(
        self,
        ctx,
        source,
        content_length,
        probe_bases,
        current_range=None,
        primary_url=None,
        fallback_url=None,
        fallback_auth=_sp._AUTH_HEADER_NOT_PROVIDED,
        primary_auth=_sp._AUTH_HEADER_NOT_PROVIDED,
        cache_fallback_range_bytes=False,
    ):
        """Classify sampled bytes as MATCH / MISMATCH / INCONCLUSIVE.

        A digest that is PRESENT on both sides and differs is a definitive
        MISMATCH (wrong file). A digest that cannot be fetched (empty body,
        probe 5xx / timeout) is INCONCLUSIVE — we can't prove same-or-
        different yet, so the caller keeps the source eligible.
        """
        primary_auth, fallback_url, fallback_auth = self._resolve_fingerprint_identity(
            ctx, source, primary_auth, fallback_url, fallback_auth
        )
        ranges = tuple(self._fallback_fingerprint_ranges(ctx, content_length))
        cfg = self._fingerprint_probe_cfg(
            content_length,
            probe_bases,
            current_range,
            primary_url,
            fallback_url,
            fallback_auth,
            primary_auth,
            cache_fallback_range_bytes,
        )
        if len(ranges) > 1 and _sp._FALLBACK_FINGERPRINT_WORKERS > 1:
            return self._classify_parallel_from_cfg(ctx, ranges, cfg)
        for start, end in ranges:
            verdict = self._classify_one_fingerprint_range(start, end, ctx, cfg)
            if verdict != _sp._FALLBACK_MATCH:
                return verdict
        return _sp._FALLBACK_MATCH

    def _classify_parallel_from_cfg(self, ctx, ranges, cfg):
        """Dispatch to the parallel classifier, unpacking the shared cfg dict."""
        return self._classify_fallback_fingerprint_parallel(
            ctx,
            ranges,
            cfg["content_length"],
            cfg["probe_bases"],
            cfg["current_range"],
            cfg["primary_url"],
            cfg["fallback_url"],
            cfg["fallback_auth"],
            cfg["primary_auth"],
            cfg["cache_fallback_range_bytes"],
        )

    @staticmethod
    def _fingerprint_probe_cfg(
        content_length,
        probe_bases,
        current_range,
        primary_url,
        fallback_url,
        fallback_auth,
        primary_auth,
        cache_fallback_range_bytes,
    ):
        """Bundle the shared fingerprint probe parameters into one dict."""
        return {
            "content_length": content_length,
            "probe_bases": probe_bases,
            "current_range": current_range,
            "primary_url": primary_url,
            "fallback_url": fallback_url,
            "fallback_auth": fallback_auth,
            "primary_auth": primary_auth,
            "cache_fallback_range_bytes": cache_fallback_range_bytes,
        }

    def _classify_one_fingerprint_range(self, start, end, ctx, cfg):
        """Classify a single sampled range; returns MATCH/MISMATCH/INCONCLUSIVE.

        ``cfg`` carries the shared probe parameters (lengths, URLs, auths,
        current-range short circuit, byte-caching flag).
        """
        fallback_digest = self._fetch_fallback_fingerprint_digest(
            (start, end),
            cfg["content_length"],
            cfg["probe_bases"],
            cfg["current_range"],
            cfg["fallback_url"],
            cfg["fallback_auth"],
            cache_ctx=ctx,
            cache_range_bytes=cfg["cache_fallback_range_bytes"],
        )
        if not fallback_digest:
            return _sp._FALLBACK_INCONCLUSIVE
        primary_digest = self._fetch_primary_fallback_range_digest(
            ctx,
            cfg["primary_auth"],
            start,
            end,
            cfg["content_length"],
            cfg["probe_bases"],
            cfg["primary_url"],
        )
        if not primary_digest:
            return _sp._FALLBACK_INCONCLUSIVE
        if primary_digest != fallback_digest:
            return _sp._FALLBACK_MISMATCH
        return _sp._FALLBACK_MATCH

    def _fetch_fallback_fingerprint_digest(
        self,
        byte_range,
        content_length,
        probe_bases,
        current_range,
        fallback_url,
        fallback_auth,
        cache_ctx=None,
        cache_range_bytes=False,
    ):
        """Return the fallback digest for one sampled byte range."""
        start, end = byte_range
        if current_range and current_range[:2] == (start, end):
            return current_range[2]
        if cache_range_bytes:
            body = self._fetch_fallback_range_bytes(
                fallback_url,
                fallback_auth,
                start,
                end,
                content_length=content_length,
                probe_bases=probe_bases,
            )
            if body is not None:
                self._cache_fallback_range(
                    cache_ctx,
                    fallback_url,
                    fallback_auth,
                    content_length,
                    start,
                    body,
                )
                return _sp.hashlib.sha256(body).hexdigest()
        return self._fetch_fallback_range_digest(
            fallback_url,
            fallback_auth,
            start,
            end,
            content_length=content_length,
            probe_bases=probe_bases,
        )

    def _primary_fingerprint_range_matches(
        self,
        ctx,
        start,
        end,
        fallback_digest,
        content_length,
        probe_bases,
        primary_url,
        primary_auth,
        cache_lock,
    ):
        """Return whether one sampled primary range matches fallback."""
        primary_digest = self._fetch_primary_fallback_range_digest_threadsafe(
            ctx,
            primary_auth,
            start,
            end,
            content_length,
            probe_bases,
            primary_url,
            cache_lock,
        )
        return bool(primary_digest and primary_digest == fallback_digest)

    def _fetch_primary_fallback_range_digest_threadsafe(
        self,
        ctx,
        auth_header,
        start,
        end,
        content_length,
        probe_bases,
        primary_url,
        cache_lock,
    ):
        """Return a primary digest while preserving the shared selection cache."""
        if cache_lock is None:
            return self._fetch_primary_fallback_range_digest(
                ctx, auth_header, start, end, content_length, probe_bases, primary_url
            )

        # Bug 5: same bounded OrderedDict cap as the non-threadsafe path.
        # Lazy-create under the lock so two workers can't both install
        # different cache instances on a fresh ctx.
        with cache_lock:
            cache = ctx.get("_fallback_primary_digest_cache")
            if cache is None:
                cache = _sp.OrderedDict()
                ctx["_fallback_primary_digest_cache"] = cache
        key = (primary_url, auth_header, content_length, start, end)
        with cache_lock:
            if key in cache:
                return cache[key]

        digest = self._fetch_fallback_range_digest(
            primary_url,
            auth_header,
            start,
            end,
            content_length=content_length,
            probe_bases=probe_bases,
        )
        if digest:
            with cache_lock:
                cache[key] = digest
                while len(cache) > _sp._FALLBACK_PRIMARY_DIGEST_CACHE_MAX:
                    cache.popitem(last=False)
        return digest

    @staticmethod
    def _primary_fingerprint_future_matches(pending):
        start, end, fallback_digest, future = pending
        try:
            primary_digest = future.result()
        except Exception as exc:  # defensive guard for threaded probes
            _sp.xbmc.log(
                "NZB-DAV: Primary fallback range probe failed at bytes "
                "{}-{}: {}".format(start, end, exc),
                _sp.xbmc.LOGWARNING,
            )
            return False
        return bool(primary_digest and primary_digest == fallback_digest)

    @staticmethod
    def _shutdown_executor_now(executor, futures):
        for future in futures:
            future.cancel()
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)

    def _validate_fallback_fingerprint_parallel(
        self,
        ctx,
        ranges,
        content_length,
        probe_bases,
        current_range,
        primary_url,
        fallback_url,
        fallback_auth,
        primary_auth,
        cache_fallback_range_bytes=False,
    ):
        """Return True only when every parallel-probed range provably matches.

        Bool wrapper over :meth:`_classify_fallback_fingerprint_parallel`.
        """
        return (
            self._classify_fallback_fingerprint_parallel(
                ctx,
                ranges,
                content_length,
                probe_bases,
                current_range,
                primary_url,
                fallback_url,
                fallback_auth,
                primary_auth,
                cache_fallback_range_bytes,
            )
            is _sp._FALLBACK_MATCH
        )

    def _classify_fallback_fingerprint_parallel(
        self,
        ctx,
        ranges,
        content_length,
        probe_bases,
        current_range,
        primary_url,
        fallback_url,
        fallback_auth,
        primary_auth,
        cache_fallback_range_bytes=False,
    ):
        """Classify fingerprint ranges with bounded parallel range probes."""
        workers = min(_sp._FALLBACK_FINGERPRINT_WORKERS, len(ranges))
        fallback_digests = {}
        # Shared lock for the primary-digest cache: parallel workers
        # would otherwise race on `ctx["_fallback_primary_digest_cache"]`
        # writes. The threadsafe sibling guards both reads and writes
        # with this lock.
        cache_lock = _sp.threading.Lock()
        # Manage the executor explicitly so the early-return paths can
        # cancel pending probes instead of blocking on shutdown(wait=True).
        # A single mismatched range otherwise pays full latency for all
        # in-flight probes.
        executor = _sp.ThreadPoolExecutor(max_workers=workers)

        cfg = self._fingerprint_probe_cfg(
            content_length,
            probe_bases,
            current_range,
            primary_url,
            fallback_url,
            fallback_auth,
            primary_auth,
            cache_fallback_range_bytes,
        )
        return self._run_parallel_fingerprint_probes(
            ctx, ranges, cfg, executor, cache_lock, fallback_digests
        )

    def _run_parallel_fingerprint_probes(
        self, ctx, ranges, cfg, executor, cache_lock, fallback_digests
    ):
        """Drive the fallback/primary probe submission + comparison on executor."""
        fallback_futures = {}
        primary_futures = {}
        try:
            self._submit_parallel_fallback_futures(
                executor, ranges, ctx, cfg, fallback_digests, fallback_futures
            )
            if not self._collect_parallel_fallback_digests(
                fallback_futures, fallback_digests
            ):
                return _sp._FALLBACK_INCONCLUSIVE

            self._submit_parallel_primary_futures(
                executor, ranges, ctx, cfg, cache_lock, primary_futures
            )
            return self._compare_parallel_primary_digests(
                primary_futures, fallback_digests
            )
        finally:
            # `cancel_futures` requires Python 3.9+. Cancel explicitly first
            # so Python 3.8 stops any queued probes before nonblocking shutdown.
            self._shutdown_executor_now(
                executor, tuple(fallback_futures) + tuple(primary_futures)
            )

    def _submit_parallel_fallback_futures(
        self, executor, ranges, ctx, cfg, fallback_digests, fallback_futures
    ):
        """Submit one fallback-digest probe per range (or short-circuit current)."""
        current_range = cfg["current_range"]
        for start, end in ranges:
            if current_range and current_range[:2] == (start, end):
                fallback_digests[(start, end)] = current_range[2]
                continue
            future = executor.submit(
                self._fetch_fallback_fingerprint_digest,
                (start, end),
                cfg["content_length"],
                cfg["probe_bases"],
                current_range,
                cfg["fallback_url"],
                cfg["fallback_auth"],
                cache_ctx=ctx,
                cache_range_bytes=cfg["cache_fallback_range_bytes"],
            )
            fallback_futures[future] = (start, end)

    def _submit_parallel_primary_futures(
        self, executor, ranges, ctx, cfg, cache_lock, primary_futures
    ):
        """Submit one threadsafe primary-digest probe per range."""
        for start, end in ranges:
            primary_futures[
                executor.submit(
                    self._fetch_primary_fallback_range_digest_threadsafe,
                    ctx,
                    cfg["primary_auth"],
                    start,
                    end,
                    cfg["content_length"],
                    cfg["probe_bases"],
                    cfg["primary_url"],
                    cache_lock,
                )
            ] = (start, end)

    @staticmethod
    def _collect_parallel_fallback_digests(fallback_futures, fallback_digests):
        """Drain fallback futures into ``fallback_digests``.

        Returns False on the first empty digest (INCONCLUSIVE), True when
        every probe produced a digest.
        """
        for future in _sp.as_completed(fallback_futures):
            start, end = fallback_futures[future]
            digest = future.result()
            if not digest:
                return False
            fallback_digests[(start, end)] = digest
        return True

    @staticmethod
    def _compare_parallel_primary_digests(primary_futures, fallback_digests):
        """Compare primary futures against collected fallback digests."""
        for future in _sp.as_completed(primary_futures):
            start, end = primary_futures[future]
            primary_digest = future.result()
            if not primary_digest:
                return _sp._FALLBACK_INCONCLUSIVE
            if primary_digest != fallback_digests.get((start, end)):
                return _sp._FALLBACK_MISMATCH
        return _sp._FALLBACK_MATCH

    def _fetch_primary_fallback_range_digest(
        self, ctx, auth_header, start, end, content_length, probe_bases, primary_url
    ):
        """Return cached primary range digest for one live fallback selection."""
        # Bug 5: cap the cache. OrderedDict preserves insertion order so
        # the oldest entry is dropped when the cap is exceeded — bounding
        # memory growth on long-lived sessions with many validation
        # rounds. Use setdefault with OrderedDict() so legacy {} caches
        # are upgraded transparently if missing.
        cache = ctx.get("_fallback_primary_digest_cache")
        if cache is None:
            cache = _sp.OrderedDict()
            ctx["_fallback_primary_digest_cache"] = cache
        key = (primary_url, auth_header, content_length, start, end)
        if key in cache:
            return cache[key]
        digest = self._fetch_fallback_range_digest(
            primary_url,
            auth_header,
            start,
            end,
            content_length=content_length,
            probe_bases=probe_bases,
        )
        if digest:
            cache[key] = digest
            while len(cache) > _sp._FALLBACK_PRIMARY_DIGEST_CACHE_MAX:
                cache.popitem(last=False)
        return digest
