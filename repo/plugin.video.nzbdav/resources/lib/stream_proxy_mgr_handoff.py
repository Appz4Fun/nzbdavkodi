# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""prepare_stream classification, registration, and handoff.

Stage-3 mixin split of ``stream_proxy.StreamProxy``. These methods were moved
verbatim; every reference to a ``stream_proxy`` module-level name is reached at
call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``StreamProxy``; they keep using ``self`` for instance state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _MgrPrepareMixin:  # pylint: disable=too-few-public-methods
    """prepare_stream classification, registration, and handoff."""

    def _validate_and_classify(
        self,
        remote_url,
        auth_header,
        fallback_sources,
        content_length_hint,
        settings_snapshot,
    ):
        """Validate/normalize inputs, clear stale sessions, classify the URL.

        Returns the normalized inputs plus content_type and is_mp4.
        """
        _sp._validate_url(remote_url)
        auth_header = _sp._validate_auth_header(auth_header)
        fallback_sources = _sp._normalize_fallback_sources(fallback_sources)
        content_length_hint = _sp._normalize_content_length_hint(content_length_hint)
        settings_snapshot = _sp.normalize_settings_snapshot(settings_snapshot)
        # Tear down any previous session before starting a new one. Kodi only
        # ever plays one stream at a time, so anything still in the table is
        # garbage from a prior play — possibly with a zombie ffmpeg attached
        # to a half-dead socket if Kodi stalled without firing
        # onPlayBackStopped. Cleaning up here guarantees the next play gets a
        # fresh proxy state and no stale ffmpeg hogging the upstream.
        self.clear_sessions(wait_for_process=False)
        content_type = self._detect_content_type(remote_url)
        lower_url = remote_url.lower()
        is_mp4 = lower_url.endswith((".mp4", ".m4v"))
        return (
            auth_header,
            fallback_sources,
            content_length_hint,
            settings_snapshot,
            content_type,
            is_mp4,
        )

    def _finalize_and_register(self, ctx, fallback_sources, settings_snapshot, started):
        """Attach runtime fields, start prefetch, register session, return info."""
        _sp._attach_fallback_context_fields(ctx, fallback_sources)
        if settings_snapshot:
            ctx[_sp._PASSTHROUGH_RUNTIME_SETTINGS_KEY] = (
                _sp._passthrough_runtime_settings_from_snapshot(settings_snapshot)
            )
        self._start_passthrough_runtime_settings_prefetch(ctx)
        self._start_initial_range_prefetch(ctx)
        self._start_readahead_prefetch(ctx)
        self._start_tail_prewarm(ctx)
        self._start_fallback_prevalidation(ctx)

        local_url = self._register_session(ctx)
        _sp.xbmc.log(
            "NZB-DAV: Proxy ready (remux={}, faststart={}): {}".format(
                ctx.get("remux", False), ctx.get("faststart", False), local_url
            ),
            _sp.xbmc.LOGINFO,
        )
        stream_info = {
            "duration_seconds": ctx.get("duration_seconds"),
            "total_bytes": ctx.get("total_bytes", ctx.get("content_length", 0)),
            "virtual_size": ctx.get("virtual_size", 0),
            "seekable": (
                ctx.get("seekable", False)
                or ctx.get("faststart", False)
                or ctx.get("temp_faststart", False)
            ),
            "remux": ctx.get("remux", False),
            "faststart": ctx.get("faststart", False),
            "direct": False,
            "mode": ctx.get("mode"),
            "content_type": ctx.get("content_type"),
        }
        _sp._attach_fallback_context_fields(
            stream_info, ctx.get("fallback_sources", fallback_sources)
        )
        _sp.telemetry.log_timing(
            "prepare_stream",
            (_sp.time.monotonic() - started) * 1000.0,
            content_type=ctx.get("content_type"),
            faststart=ctx.get("faststart", False),
            remux=ctx.get("remux", False),
        )
        return local_url, stream_info

    def prepare_stream(
        self,
        remote_url,
        auth_header=None,
        fallback_sources=None,
        content_length_hint=None,
        settings_snapshot=None,
    ):
        """Set up proxy for a new stream.

        Returns (local_proxy_url, stream_info_dict).
        stream_info_dict contains duration_seconds, total_bytes, seekable, remux,
        faststart, and virtual_size.
        """
        started = _sp.time.monotonic()
        (
            auth_header,
            fallback_sources,
            content_length_hint,
            settings_snapshot,
            content_type,
            is_mp4,
        ) = self._validate_and_classify(
            remote_url,
            auth_header,
            fallback_sources,
            content_length_hint,
            settings_snapshot,
        )

        ctx = self._build_stream_context(
            remote_url,
            auth_header,
            content_length_hint,
            content_type,
            is_mp4,
            fallback_sources,
            settings_snapshot,
        )
        return self._finalize_and_register(
            ctx, fallback_sources, settings_snapshot, started
        )

    def _build_stream_context(
        self,
        remote_url,
        auth_header,
        content_length_hint,
        content_type,
        is_mp4,
        fallback_sources,
        settings_snapshot,
    ):
        """Decide playback mode and build the matching stream context."""
        if fallback_sources:
            return self._build_ctx_fallback(
                remote_url, auth_header, content_length_hint, content_type, is_mp4
            )
        if is_mp4:
            return self._build_ctx_mp4(remote_url, auth_header, content_length_hint)
        return self._build_ctx_default(
            remote_url,
            auth_header,
            content_length_hint,
            content_type,
            settings_snapshot,
        )
