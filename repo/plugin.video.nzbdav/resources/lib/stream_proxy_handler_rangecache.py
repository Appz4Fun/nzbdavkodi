# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Byte-range digest fetching and the per-session fallback range cache.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

from typing import NamedTuple

import resources.lib.stream_proxy as _sp  # noqa: E402


class FallbackRangeProbe(NamedTuple):
    """Byte-span + sizing values threaded through fallback range probes."""

    failed_byte: int
    range_end: int
    content_length: int
    probe_bases: tuple = None


class _RangeCacheMixin:  # pylint: disable=too-few-public-methods
    """Byte-range digest fetching and the per-session fallback range cache."""

    @staticmethod
    def _fetch_fallback_range_digest(
        url,
        auth_header,
        start,
        end,
        content_length,
        probe_bases=None,
    ):
        """Return a range digest, treating probe errors as a failed match."""
        from resources.lib.fallback_streams import fetch_range_digest

        try:
            return fetch_range_digest(
                url,
                auth_header,
                start,
                end,
                content_length=content_length,
                probe_bases=probe_bases,
            )
        except Exception as exc:  # defensive guard for probe helpers
            _sp.xbmc.log(
                "NZB-DAV: Fallback range probe failed at bytes {}-{}: {}".format(
                    start,
                    end,
                    exc,
                ),
                _sp.xbmc.LOGWARNING,
            )
            return None

    def _probe_fallback_current_range(
        self,
        source,
        probe,
        auth_header=_sp._AUTH_HEADER_NOT_PROVIDED,
        stream_url=None,
        cache_ctx=None,
    ):
        """Verify fallback can serve bytes at the failing offset.

        ``probe`` is a :class:`FallbackRangeProbe` carrying the byte span and
        sizing values for the current-range check.
        """
        return bool(
            self._fetch_fallback_current_range_digest(
                source,
                probe,
                auth_header=auth_header,
                stream_url=stream_url,
                cache_ctx=cache_ctx,
            )
        )

    def _fetch_fallback_current_range_digest(
        self,
        source,
        probe,
        auth_header=_sp._AUTH_HEADER_NOT_PROVIDED,
        stream_url=None,
        cache_ctx=None,
    ):
        """Return the digest proving fallback can serve bytes at the failing offset.

        ``probe`` is a :class:`FallbackRangeProbe` carrying the byte span and
        sizing values for the current-range check.
        """
        failed_byte = probe.failed_byte
        range_end = probe.range_end
        content_length = probe.content_length
        probe_bases = probe.probe_bases
        probe_end = min(range_end, failed_byte + 4095)
        if auth_header is _sp._AUTH_HEADER_NOT_PROVIDED:
            auth_header = self._fallback_source_auth(source)
        if stream_url is None:
            stream_url = source["stream_url"]
        cached_body = self._cached_fallback_range_body(
            cache_ctx,
            stream_url,
            auth_header,
            content_length,
            failed_byte,
            probe_end,
        )
        if cached_body:
            return _sp.hashlib.sha256(cached_body).hexdigest()
        return self._fetch_and_cache_current_range_digest(
            stream_url,
            auth_header,
            failed_byte,
            probe_end,
            content_length,
            probe_bases,
            cache_ctx,
        )

    def _fetch_and_cache_current_range_digest(
        self,
        stream_url,
        auth_header,
        failed_byte,
        probe_end,
        content_length,
        probe_bases,
        cache_ctx,
    ):
        """Fetch the current-range body (caching it), else fall back to a digest."""
        body = self._fetch_fallback_range_bytes(
            stream_url,
            auth_header,
            failed_byte,
            probe_end,
            content_length=content_length,
            probe_bases=probe_bases,
        )
        if body is not None:
            self._cache_fallback_range(
                cache_ctx,
                stream_url,
                auth_header,
                content_length,
                failed_byte,
                body,
            )
            return _sp.hashlib.sha256(body).hexdigest()
        return self._fetch_fallback_range_digest(
            stream_url,
            auth_header,
            failed_byte,
            probe_end,
            content_length=content_length,
            probe_bases=probe_bases,
        )

    @staticmethod
    def _fetch_fallback_range_bytes(
        url,
        auth_header,
        start,
        end,
        content_length,
        probe_bases=None,
    ):
        """Return a validated range body, treating probe errors as no match."""
        from resources.lib.fallback_streams import fetch_range_bytes

        try:
            return fetch_range_bytes(
                url,
                auth_header,
                start,
                end,
                content_length=content_length,
                probe_bases=probe_bases,
            )
        except Exception as exc:  # defensive guard for probe helpers
            _sp.xbmc.log(
                "NZB-DAV: Fallback range body probe failed at bytes {}-{}: {}".format(
                    start,
                    end,
                    exc,
                ),
                _sp.xbmc.LOGWARNING,
            )
            return None

    @staticmethod
    def _fetch_primary_range_bytes(url, auth_header, start, end, content_length):
        """Return validated range bytes from the already-selected primary URL."""
        content_length = _sp._StreamHandler._coerce_primary_range_bounds(
            start, end, content_length
        )
        if content_length is None:
            return None

        expected_length = end - start + 1
        try:
            req = _sp.Request(url)
            _sp._add_request_headers(req, auth_header)
            req.add_header("Range", "bytes={}-{}".format(start, end))
            # nosemgrep
            with _sp.urlopen(req, timeout=10) as resp:  # nosec B310
                if not _sp._StreamHandler._primary_range_response_ok(
                    resp, start, end, content_length, expected_length
                ):
                    return None
                body = resp.read(expected_length)
        except (OSError, TypeError, ValueError):
            return None
        if len(body) != expected_length:
            return None
        return body

    @staticmethod
    def _coerce_primary_range_bounds(start, end, content_length):
        """Validate a primary range request; return coerced length or None."""
        if not isinstance(start, int) or not isinstance(end, int):
            return None
        try:
            content_length = int(content_length or 0)
        except (TypeError, ValueError):
            return None
        in_bounds = 0 <= start <= end < content_length
        if content_length <= 0 or not in_bounds:
            return None
        return content_length

    @staticmethod
    def _primary_range_response_ok(resp, start, end, content_length, expected_length):
        """Return True when a 206 response matches the requested range exactly."""
        status = getattr(resp, "status", None) or resp.getcode()
        content_range = _sp._get_header(resp, "Content-Range")
        header_length = _sp._get_header(resp, "Content-Length")
        if isinstance(content_range, str):
            content_range = content_range.strip()
        if isinstance(header_length, str):
            header_length = header_length.strip()
        if status != 206:
            return False
        if content_range != _sp._expected_content_range(start, end, content_length):
            return False
        return header_length == str(expected_length)

    @staticmethod
    def _cache_fallback_range(
        ctx, stream_url, auth_header, content_length, start, body
    ):
        """Remember verified fallback bytes that can start a future stream read."""
        if ctx is None or not body:
            return
        cache = ctx.setdefault(_sp._FALLBACK_CURRENT_RANGE_CACHE_KEY, {})
        cache[
            (
                stream_url,
                auth_header,
                content_length,
                start,
                start + len(body) - 1,
            )
        ] = body

    @staticmethod
    def _cached_range_entry_matches(key, body, prefix, start, end, require_cover_end):
        """Return True when a cache entry is a usable prefix for this range."""
        if not isinstance(key, tuple) or len(key) != 5:
            return False
        if key[:4] != prefix or not isinstance(body, bytes):
            return False
        cached_end = key[4]
        if require_cover_end and cached_end < end:
            return False
        return cached_end == start + len(body) - 1

    @staticmethod
    def _best_cached_fallback_range(cache, prefix, start, end, require_cover_end):
        """Find the longest cached fallback body matching prefix for this range.

        Returns ``(selected_key, selected_body)`` where ``selected_key`` is
        ``None`` when no cached entry matches.
        """
        selected_key = None
        selected_body = b""
        requested_length = end - start + 1
        for key, body in list(cache.items()):
            if not _sp._StreamHandler._cached_range_entry_matches(
                key, body, prefix, start, end, require_cover_end
            ):
                continue
            candidate = body[:requested_length]
            if len(candidate) > len(selected_body):
                selected_key = key
                selected_body = candidate
        return selected_key, selected_body

    @staticmethod
    def _cached_fallback_range_body(
        ctx, stream_url, auth_header, content_length, start, end
    ):
        """Return cached fallback bytes that cover the requested range prefix."""
        if ctx is None:
            return b""
        cache = ctx.get(_sp._FALLBACK_CURRENT_RANGE_CACHE_KEY)
        if not isinstance(cache, dict):
            return b""
        prefix = (stream_url, auth_header, content_length, start)
        _, selected_body = _sp._StreamHandler._best_cached_fallback_range(
            cache, prefix, start, end, True
        )
        return selected_body

    @staticmethod
    def _pop_cached_fallback_range(ctx, start, end):
        """Return cached verified fallback bytes for the start of this range."""
        cache = ctx.get(_sp._FALLBACK_CURRENT_RANGE_CACHE_KEY)
        if not isinstance(cache, dict):
            return b""
        try:
            content_length = int(ctx.get("content_length", 0) or 0)
        except (TypeError, ValueError):
            return b""
        prefix = (ctx.get("remote_url"), ctx.get("auth_header"), content_length, start)
        selected_key, selected_body = _sp._StreamHandler._best_cached_fallback_range(
            cache, prefix, start, end, False
        )
        if selected_key is None:
            return b""
        cache.pop(selected_key, None)
        return selected_body

    @staticmethod
    def _wait_for_initial_range_prefetch(ctx, start):
        """Briefly wait for prepare-time byte-0 prefetch to populate the cache."""
        if start != 0:
            return
        thread = ctx.get("_initial_range_prefetch_thread")
        if not thread or thread is _sp.threading.current_thread():
            return
        try:
            if not thread.is_alive():
                return
            thread.join(_sp._INITIAL_RANGE_PREFETCH_WAIT_SECONDS)
        except RuntimeError:
            return
