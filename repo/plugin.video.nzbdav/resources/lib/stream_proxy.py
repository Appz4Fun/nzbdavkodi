# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Local HTTP proxy for nzbdav WebDAV streams.

For MP4 files, remuxes on the fly to MKV using ffmpeg (-c copy, no
re-encoding).  This bypasses a Kodi CFileCache bug where parsing large
MP4 moov atoms over HTTP fails with 'corrupted STCO atom'.

For MKV and other files, proxies range requests directly to the remote
WebDAV server with proper 206 responses.
"""

# hashlib/hmac/_select/_socket/OrderedDict/deque/ThreadPoolExecutor/as_completed
# and the http_util ``notify`` alias are no longer referenced directly here:
# the handler methods that used them moved into the stream_proxy_handler_*
# mixins, which reach them via ``_sp.<name>`` against this module's namespace.
# They are kept imported on this module so that indirection (and any test patch
# of e.g. ``stream_proxy.urlopen``) keeps resolving.
import hashlib  # noqa: F401  pylint: disable=unused-import
import hmac  # noqa: F401  pylint: disable=unused-import
import math
import os
import select as _select  # noqa: F401  pylint: disable=unused-import

# shutil is no longer used directly here (the ffmpeg/workdir helpers moved to
# stream_proxy_ffmpeg), but tests patch ``stream_proxy.shutil.disk_usage`` and
# the sibling reaches it through the shared module object, so keep it imported
# on this module as the patch surface.
import shutil  # noqa: F401  pylint: disable=unused-import
import socket as _socket  # noqa: F401  pylint: disable=unused-import

# ``struct`` and a direct ``subprocess`` use are gone from this module: the
# exception-tuple constants that named ``struct.error`` /
# ``subprocess.SubprocessError`` moved to ``stream_proxy_const``. ``subprocess``
# stays imported because the ffmpeg/probe siblings reach it via
# ``_sp.subprocess`` against this module's namespace; ``struct`` had no such
# consumer and was dropped.
import subprocess  # noqa: F401  pylint: disable=unused-import

# tempfile is no longer used directly here (``_get_private_hls_temp_root`` moved
# to stream_proxy_ffmpeg), but that sibling reaches it via ``_sp.tempfile`` to
# build the private HLS temp root, so keep it imported on this module as the
# shared namespace surface.
import tempfile  # noqa: F401  pylint: disable=unused-import
import threading

# time is no longer used directly here (the HLS producer methods that used it
# moved into the stream_proxy_hls_* mixins, which reach it via ``_sp.time``),
# but tests patch ``stream_proxy.time.monotonic`` / ``stream_proxy.time.sleep``
# and the siblings resolve through this module object, so keep it imported here
# as the patch surface.
import time  # noqa: F401  pylint: disable=unused-import
import uuid
from collections import OrderedDict, deque  # noqa: F401  pylint: disable=unused-import
from concurrent.futures import (  # noqa: F401  pylint: disable=unused-import
    ThreadPoolExecutor,
    as_completed,
)

# ``HTTPException`` is no longer referenced directly here (it moved into the
# ``_PARSE_ERRORS`` tuple now in ``stream_proxy_const``), but the serve sibling
# reaches it via ``_sp.HTTPException``, so keep it imported as that surface.
from http.client import HTTPException  # noqa: F401  pylint: disable=unused-import
from http.server import BaseHTTPRequestHandler
from urllib.request import (  # noqa: F401  pylint: disable=unused-import
    Request,
    urlopen,
)

import xbmc

try:
    # Imported for the Stage 1 sibling modules, which reach it via
    # ``_sp.xbmcaddon`` (kept here so the try/except availability fallback and
    # any test patch of ``stream_proxy.xbmcaddon`` stay on this module).
    import xbmcaddon  # pylint: disable=unused-import
except ImportError:
    xbmcaddon = None

# mp4_parser functions are imported here so tests can patch them at this
# module's namespace.  They have no Kodi dependencies, so the import is safe
# at module load time.  If mp4_parser is unavailable (e.g. during a partial
# install) we fall back gracefully to None, which prepare_stream treats as a
# failed faststart parse.
try:
    from resources.lib.mp4_parser import (  # noqa: E402,F401  pylint: disable=unused-import
        RangeCache,
        build_faststart_layout,
        fetch_remote_mp4_layout,
    )
except (ImportError, ModuleNotFoundError):
    RangeCache = None  # type: ignore[assignment,misc]
    build_faststart_layout = None  # type: ignore[assignment]
    fetch_remote_mp4_layout = None  # type: ignore[assignment]

from resources.lib import (  # noqa: F401  pylint: disable=unused-import
    telemetry,
)
from resources.lib.dv_source import (  # noqa: F401  pylint: disable=unused-import
    probe_dolby_vision_source,
)
from resources.lib.http_util import (  # noqa: F401  pylint: disable=unused-import
    notify as _notify,
)
from resources.lib.http_util import (  # noqa: F401  pylint: disable=unused-import
    redact_text as _redact_text,
)

# Singleton proxy instance
_proxy = None
_proxy_lock = threading.Lock()
_HLS_PRIVATE_TEMP_ROOT = None
_HLS_PRIVATE_TEMP_ROOT_LOCK = threading.Lock()

# ``_get_private_hls_temp_root`` (the cached private HLS temp-root singleton)
# now lives in ``stream_proxy_ffmpeg`` next to its only caller and is
# re-exported at the bottom of this module; it mutates this module's
# ``_HLS_PRIVATE_TEMP_ROOT`` global through ``_sp`` so the test patch surface is
# unchanged.

# stream_proxy's module-level configuration constants live in
# ``stream_proxy_const`` and are re-exported here so every ``stream_proxy.<NAME>``
# reference keeps resolving -- the sibling mixins read them as ``_sp.<NAME>`` at
# import time (including as default-argument values in their method signatures),
# and tests ``@patch`` them on this module. The names are imported
# EXPLICITLY (not ``import *``) so static analysers can follow the re-export chain
# from ``from resources.lib.stream_proxy import _NAME`` in tests. This import MUST
# stay above ``class _StreamHandler`` and the sibling mixin imports so load order holds.
from resources.lib.stream_proxy_const import (  # noqa: F401,E402  pylint: disable=cyclic-import,unused-import
    _AUTH_HEADER_NOT_PROVIDED,
    _AWAITING_DOWNLOAD_NO_PROGRESS_MAX,
    _CONTENT_RANGE_ZERO_RE,
    _DEFAULT_FORCE_REMUX_THRESHOLD_MB,
    _DEFAULT_PASSTHROUGH_STALL_WAIT_SECONDS,
    _DEFAULT_READAHEAD_BUFFER_MB,
    _DENSITY_BREAKER_WINDOW_BYTES,
    _DENSITY_BREAKER_ZERO_FILL_RATIO,
    _DURATION_RE,
    _FALLBACK_CURRENT_RANGE_CACHE_KEY,
    _FALLBACK_FINGERPRINT_WORKERS,
    _FALLBACK_INCONCLUSIVE,
    _FALLBACK_MATCH,
    _FALLBACK_MISMATCH,
    _FALLBACK_PENDING_FALLTHROUGH_MAX,
    _FALLBACK_PRIMARY_AUTH_HINT_KEY,
    _FALLBACK_PRIMARY_DIGEST_CACHE_MAX,
    _FALLBACK_PRIMARY_URL_HINT_KEY,
    _FALLBACK_SOURCE_STATE_NOT_PROVIDED,
    _FALLBACK_SOURCE_STREAM_URL_HINT_KEY,
    _FALLBACK_SOURCE_TRANSIENT_MISS_MAX,
    _FALLBACK_UPDATE_PATH_RE,
    _FAULT_PRIMARY_FAIL_AFTER_BYTES_ENV,
    _FAULT_TAIL_GUARD_BYTES,
    _FFMPEG_CAPABILITY_PROBE_TIMEOUT,
    _FFMPEG_PATHS,
    _FFPROBE_PATHS,
    _FIRST_BYTE_RANGE_RETRY_DELAYS,
    _FMP4_HLS_CAPABILITY_MARKERS,
    _FORCE_REMUX_THRESHOLD_MB_MAX,
    _HLS_CLOSE_ERRORS,
    _HLS_FORWARD_WAIT_SEGMENTS,
    _HLS_SEGMENT_MTIME_STABLE_MS,
    _HLS_SEGMENT_SECONDS,
    _HLS_SEGMENT_WAIT_SECONDS,
    _HLS_WORKDIR_CANDIDATES,
    _INITIAL_RANGE_PREFETCH_WAIT_SECONDS,
    _KODI_SETTING_ERRORS,
    _MAX_PROXY_WORKERS,
    _MAX_RECOVERY_SECONDS,
    _MAX_STREAM_SESSIONS,
    _MAX_TOTAL_ZERO_FILL,
    _PARSE_ERRORS,
    _PASSTHROUGH_MIN_THROUGHPUT_BPS,
    _PASSTHROUGH_RUNTIME_SETTINGS_DONE_KEY,
    _PASSTHROUGH_RUNTIME_SETTINGS_ERROR_KEY,
    _PASSTHROUGH_RUNTIME_SETTINGS_KEY,
    _PASSTHROUGH_STALL_WAIT_BACKOFF_SECONDS,
    _PASSTHROUGH_STALL_WAIT_MAX_SECONDS,
    _PASSTHROUGH_THROUGHPUT_WINDOW_SECONDS,
    _PREPARE_ATTEMPT_TIMEOUT,
    _PREPARE_MAX_ATTEMPTS,
    _PREPARE_REQUEST_MAX_BYTES,
    _PREPARE_RETRY_BACKOFF,
    _PREPARE_TOKEN_HEADER,
    _PROBE_DEADLINE_SECONDS,
    _PROBE_RETRY_DELAYS,
    _PROP_PROXY_TOKEN,
    _RANGE_RETRY_DELAYS,
    _READAHEAD_BUFFER_KEY,
    _READAHEAD_BUFFER_MB_MAX,
    _READAHEAD_ERROR_BACKOFF_SECONDS,
    _READAHEAD_FETCH_CHUNK,
    _READAHEAD_START_DEFER_SECONDS,
    _READAHEAD_THREAD_KEY,
    _READAHEAD_THROTTLE_BACKOFF_SECONDS,
    _RECOVERABLE_HTTP_RANGE_ERROR_CODES,
    _RECOVERY_NOTIFY_DEBOUNCE_SECONDS,
    _REMUX_STDOUT_IDLE_TIMEOUT,
    _REMUX_WRITE_TIMEOUT,
    _SEEK_THRESHOLD,
    _SEGMENT_NORMALIZE_RE,
    _SESSION_TTL_SECONDS,
    _SESSION_ZERO_FILL_RATIO_MAX,
    _SETTINGS_SNAPSHOT_KEYS,
    _SKIP_PROBE_SIZES,
    _SKIP_PROBE_TIMEOUT,
    _STARVATION_RECENT_OUTAGE_SECONDS,
    _STARVATION_TERMINAL_REASONS,
    _STRICT_CONTRACT_MODE_ENFORCE,
    _STRICT_CONTRACT_MODE_OFF,
    _STRICT_CONTRACT_MODE_WARN,
    _TAIL_PREWARM_BYTES,
    _TAIL_PREWARM_DEFER_SECONDS,
    _UPSTREAM_OPEN_TIMEOUT,
    _UPSTREAM_RANGE_CLIENT_ERROR,
    _UPSTREAM_RANGE_OK,
    _UPSTREAM_RANGE_PROTOCOL_MISMATCH,
    _UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD,
    _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
    _UPSTREAM_RANGE_UPSTREAM_ERROR,
    _UPSTREAM_REACHABILITY_HTTP_CLIENT_ERROR,
    _UPSTREAM_REACHABILITY_HTTP_SERVER_ERROR,
    _UPSTREAM_REACHABILITY_OTHER,
    _UPSTREAM_REACHABILITY_UNREACHABLE_NETWORK,
    _UPSTREAM_READ_CHUNK,
    _UPSTREAM_READ_TIMEOUT,
    _ZERO_FILL_BUFFER,
)

# ``_ProxyStreamState`` (the per-request __slots__ pass-through state holder)
# now lives in ``stream_proxy_state`` and is re-exported at the bottom of this
# module so ``stream_proxy._ProxyStreamState`` keeps resolving.
# Stage-2 mixin split: _StreamHandler's request-handling methods live in the
# stream_proxy_handler_<area> sibling modules and are composed back here via
# MRO. These imports sit after the module-level constants the mixins capture
# in default arguments (e.g. _AUTH_HEADER_NOT_PROVIDED) so the partially
# initialized module already exposes them when each mixin class body runs.
from resources.lib.stream_proxy_handler_cutover import (  # noqa: E402
    _FallbackCutoverMixin,
)
from resources.lib.stream_proxy_handler_dispatch import _DispatchMixin  # noqa: E402
from resources.lib.stream_proxy_handler_fingerprint import (  # noqa: E402
    _FingerprintMixin,
)
from resources.lib.stream_proxy_handler_hlsserve import _HlsServeMixin  # noqa: E402
from resources.lib.stream_proxy_handler_probe import (  # noqa: E402
    _FallbackProbeMixin,
)
from resources.lib.stream_proxy_handler_proxyserve import (  # noqa: E402
    _ProxyServeMixin,
)
from resources.lib.stream_proxy_handler_proxyserve2 import (  # noqa: E402
    _ProxyServeStallMixin,
)
from resources.lib.stream_proxy_handler_rangecache import (  # noqa: E402
    _RangeCacheMixin,
)
from resources.lib.stream_proxy_handler_rangeparse import (  # noqa: E402
    _RangeParseMixin,
)
from resources.lib.stream_proxy_handler_remux import _RemuxMixin  # noqa: E402
from resources.lib.stream_proxy_handler_serve import _ServeMixin  # noqa: E402
from resources.lib.stream_proxy_handler_standby import (  # noqa: E402
    _FallbackStandbyMixin,
)
from resources.lib.stream_proxy_handler_upstream import (  # noqa: E402
    _UpstreamRelayMixin,
)


class _StreamHandler(  # pylint: disable=too-many-ancestors
    BaseHTTPRequestHandler,
    _RemuxMixin,
    _DispatchMixin,
    _ServeMixin,
    _HlsServeMixin,
    _FallbackCutoverMixin,
    _FallbackStandbyMixin,
    _FallbackProbeMixin,
    _FingerprintMixin,
    _RangeCacheMixin,
    _ProxyServeMixin,
    _ProxyServeStallMixin,
    _UpstreamRelayMixin,
    _RangeParseMixin,
):
    """HTTP handler that remuxes MP4 to MKV or proxies other formats."""

    protocol_version = "HTTP/1.1"
    close_connection = False

    # Defined directly on the class (not a mixin) so it wins MRO over
    # BaseHTTPRequestHandler.log_message, which precedes the mixins in the
    # base list.
    def log_message(self, fmt, *args):  # pylint: disable=arguments-differ
        xbmc.log("NZB-DAV: Proxy: {}".format(fmt % args), xbmc.LOGDEBUG)


# ``_ThreadedHTTPServer`` (the worker-bounded threaded server) now lives in
# ``stream_proxy_httpserver`` and is re-exported at the bottom of this module
# so ``stream_proxy._ThreadedHTTPServer`` keeps resolving.


# Stage-final mixin split: HlsProducer's ffmpeg/segment methods live in the
# stream_proxy_hls_* sibling modules and are composed back here via MRO. The
# imports sit after the module constants the mixins reach via ``_sp.<name>``.
from resources.lib.stream_proxy_hls_ffmpeg import (  # noqa: E402
    _HlsProduceMixin,
)
from resources.lib.stream_proxy_hls_segment import (  # noqa: E402
    _HlsSegmentMixin,
)


class HlsProducer(_HlsProduceMixin, _HlsSegmentMixin):
    """Persistent ffmpeg + disk-backed HLS segment producer for a
    single session.

    The original per-segment approach (one ffmpeg cold start per
    segment request) made Kodi cache constantly: each segment paid
    ~10-15 s of container parsing against a remote 58 GB MKV, which
    is longer than the 30 s segment duration, so Kodi's HLS demuxer
    ran out of buffered data every time. The fix is to keep one
    ffmpeg running using the ``segment`` muxer, writing
    ``seg_000000.ts`` files directly to a session directory on disk.
    Kodi's segment requests become simple file reads — no cold start
    between consecutive segments, just once per seek.

    Seeks are handled by killing the current ffmpeg and restarting
    with ``-ss <target>`` and ``-segment_start_number <seg_n>`` so
    the new ffmpeg writes ``seg_%06d.ts`` files at the right index.
    Backward seeks to an already-produced segment just read the
    existing file without restarting ffmpeg at all.

    Thread safety: mutation of the ffmpeg process pointer and
    ``start_segment`` is guarded by ``_lock``. Segment file reads
    are stateless and don't need locking.
    """

    def __init__(self, ctx, base_workdir):
        self.ctx = ctx
        self.remote_url = ctx["remote_url"]
        self.auth_header = ctx.get("auth_header")
        self.ffmpeg_path = ctx["ffmpeg_path"]
        self.duration_seconds = float(ctx["duration_seconds"])
        self.segment_seconds = float(
            ctx.get("hls_segment_duration", _HLS_SEGMENT_SECONDS)
        )
        self.total_segments = int(
            math.ceil(self.duration_seconds / self.segment_seconds)
        )
        self.segment_format = ctx.get("hls_segment_format", "mpegts")
        self.session_dir = os.path.join(base_workdir, ctx["session_id"])
        os.makedirs(self.session_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._proc = None
        self._start_segment = 0  # -segment_start_number of the live ffmpeg
        self._closed = False
        self._spawn_time = 0.0  # time.time() of the most recent ffmpeg spawn
        # _init_ready MUST be set here, not only in the spawn path:
        # wait_for_init reads it before the first spawn and would
        # AttributeError on a fresh session otherwise.
        self._init_ready = False
        # Canonical init segment bytes. Populated the first time
        # wait_for_init observes a complete init.mp4 on disk. After
        # that, ``_serve_hls_init`` returns these bytes for every
        # Kodi request, ignoring whatever ffmpeg writes to the disk
        # file on subsequent generations. Rationale: on a seek
        # respawn, ffmpeg produces a new init.mp4 with a different
        # edit list (``elst`` box) — the codec config (``hvcC``,
        # ``mp4a``) is byte-identical, so from a decoder
        # compatibility standpoint the first init works for every
        # generation. But HLS fmp4 clients only load ``EXT-X-MAP``
        # once per playlist, so Kodi has already cached the first
        # init's bytes. Serving a different init on a later request
        # — or worse, letting Kodi re-parse a half-written disk
        # file mid-respawn — would be either a no-op (if Kodi
        # ignores the second fetch) or a decoder stall (if it
        # accepts it). Caching the bytes here makes the behavior
        # deterministic regardless of what Kodi does.
        self._canonical_init_bytes = None
        # Session-wide stderr log. Opened once at session construction,
        # reused across every ffmpeg spawn (fixing the stderr=PIPE
        # deadlock from the persistent-producer era), closed in close().
        # Binary append + unbuffered so a caller can tail the file live
        # during a stall.
        self._ffmpeg_log_path = os.path.join(self.session_dir, "ffmpeg.log")
        self._ffmpeg_log = open(  # noqa: SIM115 — closed in close()
            self._ffmpeg_log_path, "ab", buffering=0
        )

    # How long prepare() will wait for ffmpeg to actually produce
    # init.mp4 + the first segment before declaring the fmp4 path
    # broken and falling back to matroska. Has to comfortably exceed
    # ffmpeg's analyzeduration (15 s) plus header write time, plus a
    # safety margin for slow upstream reads. 30 s is the smallest
    # value that doesn't false-trip on a healthy 50 Mbps WEB-DL.
    _PREPARE_PRODUCTION_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Stage-3 StreamProxy mixin split. StreamProxy's methods now live in cohesive
# ``stream_proxy_mgr_*`` mixins, composed back onto the class below via MRO.
# Imported here (after _StreamHandler / HlsProducer are defined) so the class
# declaration can reference the mixin names. The mixins reach this module's
# globals at call time via ``import resources.lib.stream_proxy as _sp`` so test
# monkeypatches keep resolving. __init__, the singleton plumbing, and the
# module-level get_proxy()/reset helpers stay in this file.
# ---------------------------------------------------------------------------
from resources.lib.stream_proxy_mgr_context import (  # noqa: E402
    _MgrContextBuildMixin,
)
from resources.lib.stream_proxy_mgr_faststart import (  # noqa: E402
    _MgrFaststartMixin,
)
from resources.lib.stream_proxy_mgr_handoff import _MgrPrepareMixin  # noqa: E402
from resources.lib.stream_proxy_mgr_lifecycle import (  # noqa: E402
    _MgrLifecycleMixin,
)
from resources.lib.stream_proxy_mgr_prefetch import _MgrPrefetchMixin  # noqa: E402
from resources.lib.stream_proxy_mgr_probe import _MgrProbeMixin  # noqa: E402
from resources.lib.stream_proxy_mgr_sessions import _MgrSessionsMixin  # noqa: E402


class StreamProxy(  # pylint: disable=too-many-ancestors
    _MgrLifecycleMixin,
    _MgrSessionsMixin,
    _MgrPrefetchMixin,
    _MgrContextBuildMixin,
    _MgrPrepareMixin,
    _MgrProbeMixin,
    _MgrFaststartMixin,
):
    """Local HTTP proxy server for nzbdav streams.

    The implementation is composed from cohesive ``stream_proxy_mgr_*``
    mixins (lifecycle, sessions, prefetch, context build, prepare/handoff,
    probing, faststart); ``__init__`` and the singleton plumbing stay here.
    """

    def __init__(self):
        self._server = None
        self._thread = None
        self.port = 0
        self._context_lock = threading.RLock()
        self._prepare_lock = threading.RLock()
        self.prepare_token = uuid.uuid4().hex
        self._ffmpeg_capabilities = None


# The ``get_service_proxy_*`` Kodi-Home-window readers were moved to
# ``stream_proxy_serviceprops``; re-export them here so callers and test patch
# targets (``resources.lib.stream_proxy.get_service_proxy_port``) keep resolving
# and the ``_ORIGINAL_*`` aliases below bind to the SAME function objects (the
# identity check in ``resolver_prepare`` depends on that).
from resources.lib.stream_proxy_serviceprops import (  # noqa: E402,F401  pylint: disable=unused-import
    get_service_proxy_config,
    get_service_proxy_port,
    get_service_proxy_token,
)

_ORIGINAL_GET_SERVICE_PROXY_PORT = get_service_proxy_port
_ORIGINAL_GET_SERVICE_PROXY_TOKEN = get_service_proxy_token


def get_proxy():
    """Get or create the singleton stream proxy."""
    global _proxy
    with _proxy_lock:
        if _proxy is None or not _proxy.is_alive():
            # Reset the singleton if a previous instance died (e.g. service
            # was restarted, the prior thread crashed). Without this, every
            # subsequent get_proxy() returns a stale handle whose
            # serve_forever loop has already exited and clients get
            # connection-refused errors with no diagnostic. TODO.md §H.3.
            _proxy = StreamProxy()
            _proxy.start()
        return _proxy


def reset_proxy_singleton():
    """Drop the module-level proxy reference (safe to call after stop()).

    Used by service shutdown / restart paths so the next ``get_proxy()``
    call constructs a fresh instance instead of returning the stopped
    singleton. Safe under the proxy lock so no concurrent
    ``get_proxy()`` can observe the half-cleared state.
    """
    global _proxy
    with _proxy_lock:
        _proxy = None


# ---------------------------------------------------------------------------
# Stage 1 decomposition re-exports.
#
# Cohesive units that used to live inline in this module now live in sibling
# ``stream_proxy_*`` modules. They are imported here, at the END of module
# load, so every name stays resolvable as ``stream_proxy.<name>`` for callers
# and for test ``@patch`` targets (including ``_StreamHandler``, which is still
# defined above and calls these helpers as bare module globals). The siblings
# import the constants they need back from this module (a deliberate, documented
# import cycle: the constants are all defined above, before these imports
# execute) and reach this module's helpers / patched names at call time via
# ``import resources.lib.stream_proxy as _sp`` so monkeypatching keeps working.
#
# These are re-exports for external callers / test patches, so pylint's
# unused-import is expected and disabled for the block.
# ---------------------------------------------------------------------------
# pylint: disable=unused-import
from resources.lib.stream_proxy_buffer import ReadAheadBuffer  # noqa: E402,F401
from resources.lib.stream_proxy_contract import (  # noqa: E402,F401
    _add_request_headers,
    _classify_contract_mismatch,
    _classify_contract_range,
    _classify_contract_status,
    _density_ratio,
    _expected_content_range,
    _fault_forced_primary_failure,
    _fault_primary_fail_threshold,
    _get_header,
    _is_terminal_http_client_error,
    _log_contract_mismatch,
    _passthrough_watchdog_applies,
    _record_density_window,
    _set_upstream_read_timeout,
    _strip_header_value,
    _would_trip_density_breaker,
)
from resources.lib.stream_proxy_fallback import (  # noqa: E402,F401
    _attach_fallback_context_fields,
    _coerce_nonneg_int,
    _expired_session_ids,
    _extract_session_id_from_proxy_url,
    _fallback_dedup_key,
    _fallback_source_needs_prevalidation,
    _is_seek_request,
    _is_segment_resource,
    _least_recently_used_session,
    _merge_new_fallback_sources,
    _normalize_content_length_hint,
    _normalize_fallback_source,
    _normalize_fallback_sources,
    _notify_error,
    _parse_hls_segment_resource,
    _probe_content_length_hint,
    _probe_content_length_tail,
    _release_handler_lease,
    _session_last_activity,
    _storage_to_webdav_path,
    _stream_context_session_id,
    _thread_is_alive,
    _touch_stream_context,
    _validate_auth_header,
    _validate_url,
)
from resources.lib.stream_proxy_ffmpeg import (  # noqa: E402,F401
    _choose_hls_workdir,
    _disk_free_bytes,
    _drain_killed_ffmpeg_probe,
    _embed_auth_in_url,
    _ffmpeg_auth_args,
    _find_ffmpeg,
    _find_ffprobe,
    _get_private_hls_temp_root,
    _parse_ffmpeg_duration,
    _reap_process_async,
    _run_ffmpeg_hls_muxer_probe,
    _workdir_has_free_space,
)

# Stage-final decomposition re-exports. The per-request state holder and the
# threaded HTTP server were moved to dedicated sibling modules; importing them
# here at the end of module load keeps ``stream_proxy._ProxyStreamState`` and
# ``stream_proxy._ThreadedHTTPServer`` resolvable for callers and test patches.
from resources.lib.stream_proxy_httpserver import (  # noqa: E402,F401
    _ThreadedHTTPServer,
)
from resources.lib.stream_proxy_recovery import (  # noqa: E402,F401
    _claim_one_shot_flag,
    _classify_upstream_error,
    _clear_upstream_unreachable_flag,
    _maybe_notify_recovery_summary,
    _maybe_notify_stream_starvation,
    _notify_fallback_outcome,
    _prepare_recovery_summary,
    _project_session_zero_fill_ratio,
    _read_session_recovery_state,
    _record_upstream_recovered,
    _record_upstream_unreachable,
    _stream_starvation_evident,
    _update_session_recovery_state,
)
from resources.lib.stream_proxy_service import (  # noqa: E402,F401
    ServiceProxyUnavailableError,
    prepare_stream_via_service,
    update_stream_fallbacks_via_service,
)
from resources.lib.stream_proxy_settings import (  # noqa: E402,F401
    _bool_from_snapshot,
    _clamp_int_setting,
    _density_breaker_enabled,
    _force_remux_mode_from_snapshot,
    _force_remux_threshold_bytes_from_snapshot,
    _get_addon_setting,
    _get_bool_setting,
    _get_force_remux_mode,
    _get_force_remux_threshold_bytes,
    _get_passthrough_stall_wait_seconds,
    _get_readahead_buffer_mb,
    _get_server_context_lock,
    _get_strict_contract_mode,
    _int_from_snapshot,
    _passthrough_runtime_settings,
    _passthrough_runtime_settings_from_snapshot,
    _read_passthrough_runtime_settings,
    _retry_ladder_enabled,
    _send_200_no_range_enabled,
    _set_addon_setting,
    _strict_contract_mode_from_snapshot,
    _zero_fill_budget_enabled,
    build_settings_snapshot,
    normalize_settings_snapshot,
)
from resources.lib.stream_proxy_state import _ProxyStreamState  # noqa: E402,F401
