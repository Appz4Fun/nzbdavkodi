# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Standby fallback-source capture, refresh, and self-match resolution.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _FallbackStandbyMixin:  # pylint: disable=too-few-public-methods
    """Standby fallback-source capture, refresh, and self-match resolution."""

    @staticmethod
    def _coerce_source_length(source):
        """Return a source's content_length coerced to a non-negative int."""
        try:
            return int(source.get("content_length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _coerce_ctx_content_length(ctx):
        """Return ctx['content_length'] coerced to a non-negative int."""
        try:
            return int(ctx.get("content_length", 0) or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _capture_first_standby(source, index, first_standby, first_nzo_id):
        """Record the first unresolved standby source for later refresh."""
        if first_standby is None or first_standby:
            return
        nzo_id = first_nzo_id if first_nzo_id is not None else source.get("nzo_id")
        if nzo_id:
            first_standby.update({"index": index, "stream_url": "", "nzo_id": nzo_id})

    def _match_resolved_source_with_hints(
        self, ctx, source, failed_byte, range_end, hint_values
    ):
        """Run fingerprint matching for a resolved source under selector hints."""
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
        source_auth = hint_values["source_auth"]
        primary_auth = hint_values["primary_auth"]
        ctx[hint_key] = (id(source), hint_values["source_length"])
        ctx[stream_url_hint_key] = (id(source), hint_values["stream_url"])
        ctx[primary_url_hint_key] = hint_values["primary_url"]
        if source_auth is not _sp._AUTH_HEADER_NOT_PROVIDED:
            ctx[auth_hint_key] = (id(source), source_auth)
        if primary_auth is not _sp._AUTH_HEADER_NOT_PROVIDED:
            ctx[primary_auth_hint_key] = primary_auth
        try:
            return self._fallback_source_matches(ctx, source, failed_byte, range_end)
        finally:
            self._restore_fallback_hint_keys(ctx, hint_snapshot)

    def _refresh_standby_fallback_sources(self, ctx):
        """Resolve completed standby nzo_ids into usable WebDAV stream URLs."""
        for source in ctx.get("fallback_sources", []):
            self._refresh_standby_fallback_source(ctx, source)

    def _refresh_standby_fallback_source(
        self,
        ctx,
        source,
        nzo_id=None,
        known_stream_url=_sp._FALLBACK_SOURCE_STATE_NOT_PROVIDED,
        known_failed=_sp._FALLBACK_SOURCE_STATE_NOT_PROVIDED,
    ):
        """Resolve one completed standby nzo_id into a WebDAV stream URL."""
        nzo_id = self._standby_refresh_nzo_id(
            source, nzo_id, known_stream_url, known_failed
        )
        if not nzo_id:
            return False

        from resources.lib.fallback_streams import fetch_content_length
        from resources.lib.webdav import get_webdav_stream_url_for_path

        video_path = self._resolve_standby_video_path(source, nzo_id)
        if not video_path:
            return False
        stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
        auth_header = stream_headers.get("Authorization") if stream_headers else None
        if self._record_unusable_standby_url(
            ctx, source, stream_url, stream_headers, auth_header
        ):
            return False
        content_length = fetch_content_length(
            stream_url,
            auth_header,
            probe_bases=self._fallback_probe_bases(ctx),
        )
        return self._finalize_refreshed_standby_source(
            ctx, source, stream_url, stream_headers, auth_header, content_length
        )

    def _finalize_refreshed_standby_source(
        self, ctx, source, stream_url, stream_headers, auth_header, content_length
    ):
        """Store a freshly-resolved standby URL + seed selector hints.

        Rejects a provable positive-length mismatch; otherwise records the
        selector hints and reports whether a usable stream URL was stored.
        """
        try:
            content_length = int(content_length or 0)
        except (TypeError, ValueError):
            content_length = 0
        source.update(
            {
                "stream_url": stream_url,
                "stream_headers": stream_headers or {},
                "content_length": content_length,
            }
        )
        if self._standby_length_is_mismatch(ctx, content_length):
            source["failed"] = True
            return False
        ctx["_fallback_source_content_length_hint"] = (id(source), content_length)
        ctx["_fallback_source_auth_hint"] = (id(source), auth_header)
        ctx[_sp._FALLBACK_SOURCE_STREAM_URL_HINT_KEY] = (id(source), stream_url)
        return bool(stream_url)

    @staticmethod
    def _record_unusable_standby_url(
        ctx, source, stream_url, stream_headers, auth_header
    ):
        """Record + reject a standby URL that is empty or equals the primary."""
        if not stream_url:
            source.update(
                {
                    "stream_url": stream_url,
                    "stream_headers": stream_headers or {},
                    "content_length": 0,
                }
            )
            return True
        if stream_url == ctx.get("remote_url") and auth_header == ctx.get(
            "auth_header"
        ):
            source.update(
                {
                    "stream_url": stream_url,
                    "stream_headers": stream_headers or {},
                    "content_length": 0,
                    "failed": True,
                }
            )
            return True
        return False

    @staticmethod
    def _standby_refresh_nzo_id(source, nzo_id, known_stream_url, known_failed):
        """Return the nzo_id to refresh, or "" when the source is not eligible."""
        existing_stream_url = (
            source.get("stream_url")
            if known_stream_url is _sp._FALLBACK_SOURCE_STATE_NOT_PROVIDED
            else known_stream_url
        )
        existing_failed = (
            source.get("failed")
            if known_failed is _sp._FALLBACK_SOURCE_STATE_NOT_PROVIDED
            else known_failed
        )
        if existing_stream_url or existing_failed:
            return ""
        if nzo_id is None:
            nzo_id = source.get("nzo_id", "")
        return nzo_id or ""

    @staticmethod
    def _resolve_standby_video_path(source, nzo_id):
        """Resolve a completed standby nzo_id to a WebDAV video path, if ready."""
        from resources.lib.nzbdav_api import get_job_history
        from resources.lib.webdav import find_video_file

        history = get_job_history(nzo_id)
        history_status = history.get("status") if isinstance(history, dict) else ""
        if history_status != "Completed":
            if (
                isinstance(history_status, str)
                and history_status.strip().lower() == "failed"
            ):
                source["failed"] = True
            return None
        storage = history.get("storage", "")
        if not storage:
            return None
        return find_video_file(
            _sp._storage_to_webdav_path(storage),
            title_hint=source.get("title") or None,
        )

    def _standby_length_is_mismatch(self, ctx, content_length):
        """Return True only for a provable positive-length standby mismatch."""
        cached_expected_length = ctx.get("_fallback_expected_content_length")
        if cached_expected_length is not None:
            try:
                expected_length = int(cached_expected_length or 0)
            except (TypeError, ValueError):
                expected_length = 0
        else:
            expected_length = self._fallback_expected_content_length(ctx)
        # Only a POSITIVE, different length is a provable mismatch. A
        # transient HEAD probe (5xx/timeout) makes fetch_content_length
        # return 0; coercing that to a "mismatch" would permanently fail an
        # otherwise-valid standby fallback for the rest of the session over a
        # momentary WebDAV blip. Leave length 0 INCONCLUSIVE and fall through
        # so fingerprint validation can gate usability on a later pass.
        return (
            expected_length > 0
            and content_length > 0
            and content_length != expected_length
        )

    def _fallback_resolve_self_match(
        self, ctx, source, source_url, source_auth, primary_url
    ):
        """Resolve auth hints and whether the source is the primary itself.

        Returns ``(source_auth, primary_auth, is_self)``. ``primary_auth``
        stays ``_AUTH_HEADER_NOT_PROVIDED`` unless the source shares the
        primary URL.
        """
        primary_auth = _sp._AUTH_HEADER_NOT_PROVIDED
        if source_url != primary_url:
            return source_auth, primary_auth, False
        if source_auth is _sp._AUTH_HEADER_NOT_PROVIDED:
            source_auth = self._fallback_source_auth(source)
        primary_auth = ctx.get(
            _sp._FALLBACK_PRIMARY_AUTH_HINT_KEY, _sp._AUTH_HEADER_NOT_PROVIDED
        )
        if primary_auth is _sp._AUTH_HEADER_NOT_PROVIDED:
            primary_auth = ctx.get("auth_header")
        return source_auth, primary_auth, source_auth == primary_auth
