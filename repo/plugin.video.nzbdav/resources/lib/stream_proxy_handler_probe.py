# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Unvalidated/prevalidated fallback-source classification and probe-base enumeration.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _FallbackProbeMixin:  # pylint: disable=too-few-public-methods
    """Fallback-source classification (un/prevalidated) and probe-base lists."""

    def _fallback_source_matches(self, ctx, source, failed_byte, range_end):
        """Classify a fallback as MATCH / MISMATCH / INCONCLUSIVE (F8-dropout).

        Returns one of:

        - ``_FALLBACK_MATCH`` (``True``): same file, usable now.
        - ``_FALLBACK_MISMATCH`` (``False``): provably a different file
          (same URL+auth as primary, a definitively different content
          length, or a fingerprint digest that is present and provably
          differs). The caller fails the source permanently.
        - ``_FALLBACK_INCONCLUSIVE``: a TRANSIENT condition — the needed
          range/probe is not yet available (empty digest, probe 5xx /
          timeout). The caller keeps the source eligible (bounded
          reconsider) instead of killing it for the whole session.

        The strict wrong-file safety is preserved: a definitively different
        length or a provably different digest is still MISMATCH and can
        never be selected.
        """
        source_url = self._fallback_source_stream_url(ctx, source)
        source_auth = self._fallback_source_auth_hint(ctx, source)
        primary_url = self._fallback_primary_url(ctx)
        source_auth, primary_auth, is_self = self._fallback_resolve_self_match(
            ctx, source, source_url, source_auth, primary_url
        )
        if is_self:
            # The source IS the primary — it can never be its own
            # recovery. A definitive, permanent MISMATCH.
            return _sp._FALLBACK_MISMATCH
        expected_length = self._fallback_expected_content_length(ctx)
        source_length = self._fallback_source_content_length(ctx, source)
        if expected_length <= 0 or source_length != expected_length:
            # A different WebDAV-reported final content length is a
            # different release — definitive wrong-file rejection.
            return _sp._FALLBACK_MISMATCH
        if source_auth is _sp._AUTH_HEADER_NOT_PROVIDED:
            source_auth = self._fallback_source_auth(source)
        probe_bases = self._fallback_probe_bases(ctx)
        if source.get("validated"):
            # Already fingerprint-proven this session. A failing
            # current-range probe now is transient (the peer just hasn't
            # downloaded THIS offset yet), not a wrong-file mismatch.
            if self._probe_fallback_current_range(
                source,
                failed_byte,
                range_end,
                expected_length,
                probe_bases,
                auth_header=source_auth,
                stream_url=source_url,
                cache_ctx=ctx,
            ):
                return _sp._FALLBACK_MATCH
            return _sp._FALLBACK_INCONCLUSIVE
        identity = (source_url, source_auth, primary_url, primary_auth)
        return self._classify_unvalidated_fallback_source(
            ctx,
            source,
            failed_byte,
            range_end,
            expected_length,
            probe_bases,
            identity,
        )

    def _classify_unvalidated_fallback_source(
        self,
        ctx,
        source,
        failed_byte,
        range_end,
        expected_length,
        probe_bases,
        identity,
    ):
        """Fingerprint-classify a not-yet-validated source; mark MATCH ones.

        ``identity`` is ``(source_url, source_auth, primary_url, primary_auth)``.
        """
        source_url, source_auth, primary_url, primary_auth = identity
        current_range = self._fetch_unvalidated_current_range(
            ctx, source, failed_byte, range_end, expected_length, probe_bases, identity
        )
        if current_range is _sp._FALLBACK_INCONCLUSIVE:
            # Range not yet available on the peer (still downloading) or a
            # probe hiccup — transient, do not condemn the source.
            return _sp._FALLBACK_INCONCLUSIVE
        classification = self._classify_fallback_fingerprint(
            ctx,
            source,
            expected_length,
            probe_bases,
            current_range,
            primary_url,
            fallback_url=source_url,
            fallback_auth=source_auth,
            primary_auth=primary_auth,
        )
        if classification is _sp._FALLBACK_INCONCLUSIVE:
            # A probe couldn't be completed (5xx / timeout / empty body) so
            # we can't PROVE same-or-different yet. Stay eligible.
            return _sp._FALLBACK_INCONCLUSIVE
        if classification is _sp._FALLBACK_MISMATCH:
            # Digests present and provably differ — a different file.
            return _sp._FALLBACK_MISMATCH
        self._mark_fallback_source_validated(source)
        return _sp._FALLBACK_MATCH

    def _fetch_unvalidated_current_range(
        self,
        ctx,
        source,
        failed_byte,
        range_end,
        expected_length,
        probe_bases,
        identity,
    ):
        """Return the reusable current_range, or INCONCLUSIVE when unfetchable.

        ``identity`` is ``(source_url, source_auth, primary_url, primary_auth)``.
        A returned ``None`` is a valid (truncated) current_range; the
        ``_FALLBACK_INCONCLUSIVE`` sentinel means the digest was unavailable.
        """
        source_url, source_auth = identity[0], identity[1]
        current_digest = self._fetch_fallback_current_range_digest(
            source,
            failed_byte,
            range_end,
            expected_length,
            probe_bases,
            auth_header=source_auth,
            stream_url=source_url,
            cache_ctx=ctx,
        )
        if not current_digest:
            return _sp._FALLBACK_INCONCLUSIVE
        return self._fallback_current_range_for_fingerprint(
            failed_byte, range_end, current_digest
        )

    @staticmethod
    def _mark_fallback_source_validated(source):
        """Mark a source byte-proven and clear any stale transient streak.

        A source that prevalidates after a few INCONCLUSIVE probes (it was
        still downloading when first probed) must not carry a stale transient
        streak that would later abandon it once it crosses
        _FALLBACK_SOURCE_TRANSIENT_MISS_MAX. Reaching validated proves it is
        readable, so clear the streak. The "stuck forever" bound still applies
        to sources that never validate.
        """
        source["validated"] = True
        if source.get("transient_miss_count"):
            source["transient_miss_count"] = 0

    @staticmethod
    def _fallback_current_range_for_fingerprint(failed_byte, range_end, current_digest):
        """Return a reusable current_range tuple, or None when truncated.

        Bug 4: only reuse the current_range digest in fingerprint validation
        when its (start, end) covers the WHOLE 4096-byte fingerprint range.
        When ``range_end`` is shorter than ``failed_byte + 4095`` (file-tail
        or short HTTP range), the digest was computed over fewer than 4096
        bytes — the equality cache in _fetch_fallback_fingerprint_digest would
        otherwise accept it as a match for a fingerprint sample whose natural
        end is failed_byte + 4095, producing a wrong proof. In the truncated
        case, return None so the fingerprint loop refetches the natural range.
        """
        natural_end = failed_byte + 4095
        if range_end < natural_end:
            return None
        return (failed_byte, natural_end, current_digest)

    def _prevalidate_ready_fallback_sources(self, ctx):
        """Fingerprint ready fallback URLs before an upstream failure happens."""
        expected_length = self._fallback_expected_content_length(ctx)
        if expected_length <= 0:
            return 0

        validated = 0
        primary_url = ctx.get("remote_url")
        primary_auth = ctx.get("auth_header")
        probe_bases = self._fallback_probe_bases(ctx)
        for source in ctx.get("fallback_sources", []):
            (
                eligible,
                source_url,
                source_auth,
                source_length,
            ) = self._prevalidate_source_eligibility(
                source, expected_length, primary_url, primary_auth
            )
            if not eligible:
                continue
            identity = {
                "primary_url": primary_url,
                "primary_auth": primary_auth,
                "source_url": source_url,
                "source_auth": source_auth,
                "source_length": source_length,
            }
            if self._prevalidate_validate_source(
                ctx, source, expected_length, probe_bases, identity
            ):
                validated += 1
        return validated

    def _prevalidate_validate_source(
        self, ctx, source, expected_length, probe_bases, identity
    ):
        """Seed selector hints, fingerprint one source, mark it validated.

        Returns True when the source proves a match (and is flagged
        ``validated``); restores the hint keys regardless of outcome.
        """
        hint_key = "_fallback_source_content_length_hint"
        auth_hint_key = "_fallback_source_auth_hint"
        stream_url_hint_key = _sp._FALLBACK_SOURCE_STREAM_URL_HINT_KEY
        primary_url_hint_key = _sp._FALLBACK_PRIMARY_URL_HINT_KEY
        primary_auth_hint_key = _sp._FALLBACK_PRIMARY_AUTH_HINT_KEY
        hint_snapshot = self._snapshot_fallback_hint_keys(
            ctx,
            (
                hint_key,
                auth_hint_key,
                stream_url_hint_key,
                primary_url_hint_key,
                primary_auth_hint_key,
            ),
        )
        ctx[hint_key] = (id(source), identity["source_length"])
        ctx[stream_url_hint_key] = (id(source), identity["source_url"])
        ctx[primary_url_hint_key] = identity["primary_url"]
        ctx[auth_hint_key] = (id(source), identity["source_auth"])
        ctx[primary_auth_hint_key] = identity["primary_auth"]
        try:
            if self._validate_fallback_fingerprint(
                ctx,
                source,
                expected_length,
                probe_bases,
                primary_url=identity["primary_url"],
                fallback_url=identity["source_url"],
                fallback_auth=identity["source_auth"],
                primary_auth=identity["primary_auth"],
                cache_fallback_range_bytes=True,
            ):
                source["validated"] = True
                # Prevalidation succeeded after earlier INCONCLUSIVE probes
                # (still-downloading at first contact): clear any transient
                # streak so it is not later abandoned at the miss bound.
                if source.get("transient_miss_count"):
                    source["transient_miss_count"] = 0
                return True
            return False
        finally:
            self._restore_fallback_hint_keys(ctx, hint_snapshot)

    def _prevalidate_source_eligibility(
        self, source, expected_length, primary_url, primary_auth
    ):
        """Gate a source for prevalidation; return (eligible, url, auth, length)."""
        if source.get("failed") or source.get("validated"):
            return False, None, None, 0
        source_url = source.get("stream_url")
        if not source_url:
            return False, None, None, 0
        source_length = self._coerce_source_length(source)
        if source_length != expected_length:
            return False, None, None, 0
        source_auth = self._fallback_source_auth(source)
        if source_url == primary_url and source_auth == primary_auth:
            return False, None, None, 0
        return True, source_url, source_auth, source_length

    @staticmethod
    def _snapshot_fallback_hint_keys(ctx, keys):
        """Capture presence + prior value of selector hint keys for restore."""
        snapshot = {}
        for key in keys:
            had = key in ctx
            snapshot[key] = (had, ctx.get(key) if had else None)
        return snapshot

    @staticmethod
    def _restore_fallback_hint_keys(ctx, snapshot):
        """Restore selector hint keys captured by ``_snapshot_fallback_hint_keys``."""
        for key, state in snapshot.items():
            had, previous = state
            if had:
                ctx[key] = previous
            else:
                ctx.pop(key, None)

    @staticmethod
    def _fallback_expected_content_length(ctx):
        cached_length = ctx.get("_fallback_expected_content_length")
        if cached_length is not None:
            try:
                return int(cached_length or 0)
            except (TypeError, ValueError):
                return 0
        try:
            return int(ctx.get("content_length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _fallback_source_content_length(ctx, source):
        hint = ctx.get("_fallback_source_content_length_hint")
        if isinstance(hint, tuple) and len(hint) == 2 and hint[0] == id(source):
            try:
                return int(hint[1] or 0)
            except (TypeError, ValueError):
                return 0
        try:
            return int(source.get("content_length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _fallback_source_auth(source):
        """Return the fallback source Authorization header, if present."""
        return (source.get("stream_headers") or {}).get("Authorization")

    @staticmethod
    def _fallback_source_stream_url(ctx, source):
        """Return a source stream URL already read by the selector."""
        hint = ctx.get(_sp._FALLBACK_SOURCE_STREAM_URL_HINT_KEY)
        if isinstance(hint, tuple) and len(hint) == 2 and hint[0] == id(source):
            return hint[1]
        return source.get("stream_url")

    @staticmethod
    def _fallback_primary_url(ctx):
        """Return the active stream URL already read by the selector."""
        if _sp._FALLBACK_PRIMARY_URL_HINT_KEY in ctx:
            return ctx[_sp._FALLBACK_PRIMARY_URL_HINT_KEY]
        return ctx.get("remote_url")

    @staticmethod
    def _fallback_source_auth_hint(ctx, source):
        """Return a source Authorization value already read by the selector."""
        hint = ctx.get("_fallback_source_auth_hint")
        if isinstance(hint, tuple) and len(hint) == 2 and hint[0] == id(source):
            return hint[1]
        return _sp._AUTH_HEADER_NOT_PROVIDED

    @staticmethod
    def _fallback_probe_bases(ctx):
        """Return cached stream bases used to validate fallback probe URLs.

        Augments the global ``configured_stream_probe_bases()`` (which
        only carries the user-configured nzbdav_url / webdav_url
        origins) with the origins of *this session's* own primary and
        fallback URLs. Those URLs were already accepted by the caller
        — either resolve_and_play built them from the user-configured
        WebDAV roots, or /direct_play HEAD-validated each one before
        prepare_stream — so peers from the same origin are by
        definition trusted for the lifetime of the session. Without
        this extension, a /direct_play test whose URLs live on
        127.0.0.1 (e.g. local-file rangeserve) gets rejected by
        _validated_probe_url and the 100×4 KiB fingerprint sweep can
        never run.
        """
        if "_fallback_probe_bases" not in ctx:
            from resources.lib.fallback_streams import (
                _PrecomputedProbeBase,
                configured_stream_probe_bases,
            )

            bases = list(configured_stream_probe_bases())
            if not all(isinstance(base, _PrecomputedProbeBase) for base in bases):
                ctx["_fallback_probe_bases"] = tuple(bases)
                return ctx["_fallback_probe_bases"]
            seen_origins = {b.origin for b in bases}
            session_urls = _sp._StreamHandler._fallback_session_urls(ctx)
            _sp._StreamHandler._extend_probe_bases_with_origins(
                bases, seen_origins, session_urls
            )
            ctx["_fallback_probe_bases"] = tuple(bases)
        return ctx["_fallback_probe_bases"]

    @staticmethod
    def _fallback_session_urls(ctx):
        """Return this session's primary + fallback stream URLs."""
        session_urls = []
        primary = ctx.get("remote_url")
        if isinstance(primary, str) and primary:
            session_urls.append(primary)
        for source in ctx.get("fallback_sources", []) or []:
            stream_url = _sp._StreamHandler._fallback_source_session_url(source)
            if stream_url:
                session_urls.append(stream_url)
        return session_urls

    @staticmethod
    def _fallback_source_session_url(source):
        """Return a source's non-empty string stream URL, else None."""
        if not isinstance(source, dict):
            return None
        stream_url = source["stream_url"] if "stream_url" in source else None
        if isinstance(stream_url, str) and stream_url:
            return stream_url
        return None

    @staticmethod
    def _extend_probe_bases_with_origins(bases, seen_origins, session_urls):
        """Append a trusted probe base for each not-yet-seen session origin."""
        from resources.lib.fallback_streams import (
            _origin_key,
            _PrecomputedProbeBase,
            _split_http_url,
        )

        for url in session_urls:
            parts = _split_http_url(url.rstrip("/"))
            if not parts:
                continue
            origin = _origin_key(parts)
            if origin in seen_origins:
                continue
            seen_origins.add(origin)
            # Path stays "/" — anything under this origin is in-scope
            # because the URL was already trusted at session-prepare time.
            bases.append(_PrecomputedProbeBase(parts, origin, "/"))
