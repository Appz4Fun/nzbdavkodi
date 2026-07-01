# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Stream-context construction.

Stage-3 mixin split of ``stream_proxy.StreamProxy``. These methods were moved
verbatim; every reference to a ``stream_proxy`` module-level name is reached at
call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``StreamProxy``; they keep using ``self`` for instance state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _MgrContextBuildMixin:  # pylint: disable=too-few-public-methods
    """Stream-context construction."""

    @staticmethod
    def _try_faststart_layout(remote_url, content_length, auth_header):
        """Attempt virtual moov-relocation for an MP4.

        Returns the faststart dict, or None on failure.
        """
        try:
            if _sp.fetch_remote_mp4_layout is None:
                raise ImportError("mp4_parser not available")
            layout_info = _sp.fetch_remote_mp4_layout(
                remote_url, content_length, auth_header
            )
            if layout_info:
                _sp.xbmc.log(
                    "NZB-DAV: MP4 layout: moov_before_mdat={}, moov={}B".format(
                        layout_info.get("moov_before_mdat"),
                        len(layout_info.get("moov_data", b"")),
                    ),
                    _sp.xbmc.LOGINFO,
                )
                faststart = _sp.build_faststart_layout(layout_info)
                if faststart is None:
                    _sp.xbmc.log(
                        "NZB-DAV: stco overflow — moov relocation failed "
                        "(file >4GB with 32-bit chunk offsets)",
                        _sp.xbmc.LOGWARNING,
                    )
                return faststart
            _sp.xbmc.log("NZB-DAV: MP4 layout fetch returned None", _sp.xbmc.LOGWARNING)
            return None
        except _sp._PARSE_ERRORS as e:
            _sp.xbmc.log(
                "NZB-DAV: MP4 faststart parse failed: {}".format(e), _sp.xbmc.LOGWARNING
            )
            return None

    @staticmethod
    def _probe_dv_source(remote_url, auth_header, content_length):
        """Run the pure-Python DV source probe, failing safe on crash."""
        try:
            dv_result = _sp.probe_dolby_vision_source(
                remote_url,
                auth_header,
                file_size=content_length if content_length else None,
            )
        except Exception as exc:  # pylint: disable=broad-except
            _sp.xbmc.log(
                "NZB-DAV: DV probe crashed -- failing safe to "
                "matroska: {!r}".format(exc),
                _sp.xbmc.LOGWARNING,
            )
            from resources.lib.dv_source import DolbyVisionSourceResult

            dv_result = DolbyVisionSourceResult("dv_unknown", "probe_crashed")
        _sp.xbmc.log(
            "NZB-DAV: dv_probe classification={} reason={} "
            "profile={} el_type={}".format(
                dv_result.classification,
                dv_result.reason,
                dv_result.profile,
                dv_result.el_type,
            ),
            _sp.xbmc.LOGDEBUG,
        )
        return dv_result

    def _dv_route_allows_fmp4(self, remote_url, auth_header, content_length):
        """Gate fmp4 HLS on DV profile via the pure-Python source probe.

        Returns True when the source may use fmp4 HLS, False when it must
        fall back to matroska. Probes the first HEVC access unit to classify
        DV profile/EL and applies the 2026-04-23 routing matrix verbatim.
        """
        dv_result = self._probe_dv_source(remote_url, auth_header, content_length)
        if dv_result.classification == "dv_profile_7_fel":
            _sp.xbmc.log(
                "NZB-DAV: dv_route=matroska reason={} "
                "profile={} el_type={}".format(
                    dv_result.reason,
                    dv_result.profile,
                    dv_result.el_type,
                ),
                _sp.xbmc.LOGWARNING,
            )
            return False
        if (
            dv_result.classification == "dv_allowed_for_fmp4"
            and dv_result.profile == 7
            and dv_result.el_type == "MEL"
        ):
            _sp.xbmc.log(
                "NZB-DAV: dv_route=fmp4 reason={} profile=7 "
                "el_type=MEL (experimental -- metadata-only EL "
                "does not exercise CAMLCodec dual-layer init)".format(dv_result.reason),
                _sp.xbmc.LOGINFO,
            )
            return True
        return self._dv_route_tail(dv_result)

    @staticmethod
    def _dv_route_tail(dv_result):
        """Tail of the DV routing matrix: non-P7 fmp4, non_dv, unknown."""
        if dv_result.classification == "dv_allowed_for_fmp4":
            _sp.xbmc.log(
                "NZB-DAV: dv_route=matroska reason={} "
                "profile={} (non-P7 DV hangs CAMLCodec "
                "onAVStarted on fmp4 per 2026-04-15 "
                "testing)".format(dv_result.reason, dv_result.profile),
                _sp.xbmc.LOGWARNING,
            )
            return False
        if dv_result.classification == "non_dv":
            _sp.xbmc.log(
                "NZB-DAV: dv_route=fmp4 reason={}".format(dv_result.reason),
                _sp.xbmc.LOGDEBUG,
            )
            return True
        _sp.xbmc.log(
            "NZB-DAV: dv_route=matroska reason={} "
            "profile={} el_type={} (unknown DV state -- "
            "failing safe)".format(
                dv_result.reason,
                dv_result.profile,
                dv_result.el_type,
            ),
            _sp.xbmc.LOGWARNING,
        )
        return False

    def _build_ctx_fallback(
        self, remote_url, auth_header, content_length_hint, content_type, is_mp4
    ):
        """Pass-through proxy context for fallback-enabled streams."""
        content_length = self._get_content_length(
            remote_url, auth_header, content_length_hint=content_length_hint
        )
        if content_length <= 0:
            raise OSError(
                "Unable to determine content length for fallback-enabled stream"
            )
        ctx = {
            "remote_url": remote_url,
            "auth_header": auth_header,
            "content_length": content_length,
            "content_type": "video/mp4" if is_mp4 else content_type,
            "remux": False,
            "faststart": False,
            "seekable": True,
        }
        _sp.xbmc.log(
            "NZB-DAV: Fallback streams attached; using pass-through proxy "
            "before MP4 repair or remux rescue tiers",
            _sp.xbmc.LOGINFO,
        )
        return ctx

    def _resolve_mp4_temp_path(
        self,
        ffmpeg_path,
        remote_url,
        auth_header,
        content_length,
        content_length_unknown,
    ):
        """Tier 2 decision: temp-file faststart path, or None to skip.

        Skip for large files (>4GB) — temp remux would take too long and
        would time out the prepare_stream_via_service call.
        """
        _TEMP_FASTSTART_MAX = 4 * 1073741824  # 4 GB
        if content_length_unknown:
            _sp.xbmc.log(
                "NZB-DAV: MP4 content length unknown; skipping temp-file faststart",
                _sp.xbmc.LOGWARNING,
            )
            return None
        if content_length > _TEMP_FASTSTART_MAX:
            _sp.xbmc.log(
                "NZB-DAV: File too large for temp-file faststart "
                "({}B > {}B), skipping to MKV remux".format(
                    content_length, _TEMP_FASTSTART_MAX
                ),
                _sp.xbmc.LOGINFO,
            )
            return None
        if ffmpeg_path:
            return self._prepare_tempfile_faststart(
                ffmpeg_path, remote_url, auth_header
            )
        return None

    def _build_ctx_mp4_tempfile_or_remux(
        self, remote_url, auth_header, content_length, content_length_unknown
    ):
        """MP4 tiers 2/3: temp-file faststart, MKV remux, or direct proxy."""
        # Tier 2: Try temp-file faststart (ffmpeg -movflags +faststart)
        ffmpeg_path = self._get_ffmpeg_capabilities().get("ffmpeg_path")
        temp_path = self._resolve_mp4_temp_path(
            ffmpeg_path,
            remote_url,
            auth_header,
            content_length,
            content_length_unknown,
        )

        if temp_path:
            temp_size = _sp.os.path.getsize(temp_path)
            ctx = {
                "remote_url": remote_url,
                "auth_header": auth_header,
                "content_type": "video/mp4",
                "faststart": False,
                "remux": False,
                "temp_faststart": True,
                "temp_path": temp_path,
                "content_length": temp_size,
            }
            _sp.xbmc.log(
                "NZB-DAV: MP4 temp-file faststart ({}B)".format(temp_size),
                _sp.xbmc.LOGINFO,
            )
            return ctx
        if ffmpeg_path:
            return self._ctx_mp4_mkv_remux(
                remote_url, auth_header, ffmpeg_path, content_length
            )
        if content_length_unknown:
            raise OSError(
                "Unable to determine content length for MP4 stream "
                "and ffmpeg unavailable"
            )
        # Last resort: direct proxy (may fail for large files)
        return {
            "remote_url": remote_url,
            "auth_header": auth_header,
            "content_length": content_length,
            "content_type": "video/mp4",
            "remux": False,
            "faststart": False,
        }

    def _ctx_mp4_mkv_remux(self, remote_url, auth_header, ffmpeg_path, content_length):
        """Tier 3: MKV remux fallback for an MP4 source (existing behavior)."""
        duration = self._probe_duration(ffmpeg_path, remote_url, auth_header)
        ctx = {
            "remote_url": remote_url,
            "auth_header": auth_header,
            "content_type": "video/x-matroska",
            "remux": True,
            "faststart": False,
            "ffmpeg_path": ffmpeg_path,
            "total_bytes": content_length,
            "duration_seconds": duration,
            "seekable": duration is not None and content_length > 0,
        }
        _sp.xbmc.log("NZB-DAV: MP4 fallback to MKV remux", _sp.xbmc.LOGWARNING)
        return ctx

    def _build_ctx_mp4(self, remote_url, auth_header, content_length_hint):
        """MP4 proxy context: faststart layout, then temp/remux tiers."""
        content_length = self._get_content_length(
            remote_url, auth_header, content_length_hint=content_length_hint
        )
        content_length_unknown = content_length <= 0
        faststart = self._try_faststart_layout(remote_url, content_length, auth_header)

        if faststart is not None and not faststart.get("already_faststart"):
            ctx = {
                "remote_url": remote_url,
                "auth_header": auth_header,
                "content_type": "video/mp4",
                "faststart": True,
                "remux": False,
                "header_data": faststart["header_data"],
                "virtual_size": faststart["virtual_size"],
                "payload_remote_start": faststart["payload_remote_start"],
                "payload_remote_end": faststart["payload_remote_end"],
                "payload_size": faststart["payload_size"],
                "range_cache": _sp.RangeCache(),
            }
            _sp.xbmc.log(
                "NZB-DAV: MP4 faststart proxy (virtual={}B, header={}B)".format(
                    faststart["virtual_size"], len(faststart["header_data"])
                ),
                _sp.xbmc.LOGINFO,
            )
            return ctx
        if faststart is not None and faststart.get("already_faststart"):
            _sp.xbmc.log(
                "NZB-DAV: MP4 already faststart; using pass-through proxy",
                _sp.xbmc.LOGINFO,
            )
            return {
                "remote_url": remote_url,
                "auth_header": auth_header,
                "content_length": content_length,
                "content_type": "video/mp4",
                "remux": False,
                "faststart": False,
                "seekable": True,
            }
        return self._build_ctx_mp4_tempfile_or_remux(
            remote_url, auth_header, content_length, content_length_unknown
        )

    def _build_ctx_default_remux(
        self,
        remote_url,
        auth_header,
        content_length,
        force_mode,
        ffmpeg_caps,
        threshold,
    ):
        """ffmpeg-available remux context: fmp4 HLS or piped MKV.

        Force-remux exists for 32-bit Kodi builds (Amlogic CoreELEC and
        similar) that throw ``Open - Unhandled exception`` on pass-through
        HTTP above ~4 GB Content-Length. force_remux_mode picks the shape:
        "matroska" (default, piped MKV, cache-bounded seek) or "hls_fmp4"
        (experimental fragmented-MP4 HLS VOD, full random seek, DV-capable).
        """
        ffmpeg_path = ffmpeg_caps.get("ffmpeg_path")
        duration = self._probe_duration(ffmpeg_path, remote_url, auth_header)
        use_fmp4 = (
            force_mode == "hls_fmp4"
            and ffmpeg_caps.get("hls_fmp4", False)
            and duration is not None
            and duration > 0
        )
        if force_mode == "hls_fmp4" and not ffmpeg_caps.get("hls_fmp4", False):
            _sp.xbmc.log(
                "NZB-DAV: ffmpeg lacks required fmp4 HLS flags; "
                "falling back to piped Matroska",
                _sp.xbmc.LOGWARNING,
            )
        if use_fmp4:
            use_fmp4 = self._dv_route_allows_fmp4(
                remote_url, auth_header, content_length
            )
        if use_fmp4:
            return self._ctx_fmp4_hls(
                remote_url, auth_header, ffmpeg_path, content_length, duration
            )
        return self._ctx_piped_mkv(
            remote_url, auth_header, ffmpeg_path, content_length, duration, threshold
        )

    @staticmethod
    def _ctx_fmp4_hls(remote_url, auth_header, ffmpeg_path, content_length, duration):
        """fMP4 HLS remux context (experimental, full random seek)."""
        ctx = {
            "remote_url": remote_url,
            "auth_header": auth_header,
            "content_type": "application/vnd.apple.mpegurl",
            "mode": "hls",
            "remux": True,
            "faststart": False,
            "ffmpeg_path": ffmpeg_path,
            "total_bytes": content_length,
            "duration_seconds": duration,
            "seekable": True,
            "hls_segment_duration": _sp._HLS_SEGMENT_SECONDS,
            "hls_segment_format": "fmp4",
        }
        _sp.xbmc.log(
            "NZB-DAV: Force-remuxing {}B file via fMP4 HLS "
            "(experimental, duration={:.1f}s)".format(content_length, duration),
            _sp.xbmc.LOGWARNING,
        )
        return ctx

    @staticmethod
    def _ctx_piped_mkv(
        remote_url, auth_header, ffmpeg_path, content_length, duration, threshold
    ):
        """Piped Matroska remux context (cache-bounded seek)."""
        ctx = {
            "remote_url": remote_url,
            "auth_header": auth_header,
            "content_type": "video/x-matroska",
            "remux": True,
            "faststart": False,
            "ffmpeg_path": ffmpeg_path,
            "total_bytes": content_length,
            "duration_seconds": duration,
            "seekable": duration is not None and content_length > 0,
        }
        _sp.xbmc.log(
            "NZB-DAV: Force-remuxing large {}B file via piped MKV "
            "(duration={}, threshold={}B)".format(
                content_length,
                "{:.1f}s".format(duration) if duration else "unknown",
                threshold,
            ),
            _sp.xbmc.LOGWARNING,
        )
        return ctx

    @staticmethod
    def _decide_force_remux(content_length, content_length_unknown, settings_snapshot):
        """Resolve (threshold, force_mode, needs_remux) for a non-MP4 source."""
        if settings_snapshot:
            threshold = _sp._force_remux_threshold_bytes_from_snapshot(
                settings_snapshot
            )
            force_mode = _sp._force_remux_mode_from_snapshot(settings_snapshot)
        else:
            threshold = _sp._get_force_remux_threshold_bytes()
            force_mode = _sp._get_force_remux_mode()
        force_remux_requested = threshold > 0 and force_mode in (
            "matroska",
            "hls_fmp4",
        )
        needs_remux = force_remux_requested and (
            content_length_unknown or content_length >= threshold
        )
        if needs_remux and content_length_unknown:
            _sp.xbmc.log(
                "NZB-DAV: Content length unknown; forcing live remux "
                "instead of zero-byte pass-through",
                _sp.xbmc.LOGWARNING,
            )
        return threshold, force_mode, needs_remux

    def _build_ctx_default(
        self,
        remote_url,
        auth_header,
        content_length_hint,
        content_type,
        settings_snapshot,
    ):
        """Non-MP4 context: force-remux decision then remux or pass-through."""
        content_length = self._get_content_length(
            remote_url, auth_header, content_length_hint=content_length_hint
        )
        content_length_unknown = content_length <= 0
        threshold, force_mode, needs_remux = self._decide_force_remux(
            content_length, content_length_unknown, settings_snapshot
        )
        ffmpeg_caps = self._get_ffmpeg_capabilities() if needs_remux else {}
        if ffmpeg_caps.get("ffmpeg_path"):
            return self._build_ctx_default_remux(
                remote_url,
                auth_header,
                content_length,
                force_mode,
                ffmpeg_caps,
                threshold,
            )
        if needs_remux:
            if content_length_unknown:
                raise OSError(
                    "Unable to determine content length for stream "
                    "and ffmpeg unavailable"
                )
            _sp.xbmc.log(
                "NZB-DAV: {}B file exceeds remux threshold but no "
                "ffmpeg found — falling back to pass-through, "
                "playback may fail on 32-bit Kodi".format(content_length),
                _sp.xbmc.LOGWARNING,
            )
        return {
            "remote_url": remote_url,
            "auth_header": auth_header,
            "content_length": content_length,
            "content_type": content_type,
            "remux": False,
        }
