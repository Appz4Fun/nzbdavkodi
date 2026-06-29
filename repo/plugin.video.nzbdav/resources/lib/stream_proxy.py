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
import re
import select as _select  # noqa: F401  pylint: disable=unused-import

# shutil is no longer used directly here (the ffmpeg/workdir helpers moved to
# stream_proxy_ffmpeg), but tests patch ``stream_proxy.shutil.disk_usage`` and
# the sibling reaches it through the shared module object, so keep it imported
# on this module as the patch surface.
import shutil  # noqa: F401  pylint: disable=unused-import
import socket as _socket  # noqa: F401  pylint: disable=unused-import
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, deque  # noqa: F401  pylint: disable=unused-import
from concurrent.futures import (  # noqa: F401  pylint: disable=unused-import
    ThreadPoolExecutor,
    as_completed,
)
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn as _ThreadingMixIn
from urllib.request import Request, urlopen

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
    from resources.lib.mp4_parser import (  # noqa: E402
        RangeCache,
        build_faststart_layout,
        fetch_remote_mp4_layout,
    )
except (ImportError, ModuleNotFoundError):
    RangeCache = None  # type: ignore[assignment,misc]
    build_faststart_layout = None  # type: ignore[assignment]
    fetch_remote_mp4_layout = None  # type: ignore[assignment]

from resources.lib import telemetry
from resources.lib.dv_source import probe_dolby_vision_source
from resources.lib.http_util import (  # noqa: F401  pylint: disable=unused-import
    notify as _notify,
)
from resources.lib.http_util import redact_text as _redact_text

# Singleton proxy instance
_proxy = None
_proxy_lock = threading.Lock()
_HLS_PRIVATE_TEMP_ROOT = None
_HLS_PRIVATE_TEMP_ROOT_LOCK = threading.Lock()
_MAX_STREAM_SESSIONS = 8
_SESSION_TTL_SECONDS = 6 * 3600
_PARSE_ERRORS = (
    ImportError,
    OSError,
    ValueError,
    KeyError,
    struct.error,
    HTTPException,
)
_KODI_SETTING_ERRORS = (
    AttributeError,
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)
_HLS_CLOSE_ERRORS = (
    AttributeError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    subprocess.SubprocessError,
)

# Common ffmpeg paths on CoreELEC / LibreELEC
_FFMPEG_PATHS = [
    "ffmpeg",
    "/storage/.kodi/addons.bak/tools.ffmpeg-tools/bin/ffmpeg",
    "/storage/.kodi/addons/tools.ffmpeg-tools/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    "/storage/.opt/bin/ffmpeg",
]

# ffprobe paths (same locations, swap the binary). ffprobe gives a clean
# `format=duration` response in one line and avoids parsing a wall of
# per-stream probe warnings from ffmpeg's stderr — critical for files with
# many subtitle tracks where those warnings push the `Duration:` header
# past any reasonable stderr buffer budget.
_FFPROBE_PATHS = [
    "ffprobe",
    "/storage/.kodi/addons.bak/tools.ffmpeg-tools/bin/ffprobe",
    "/storage/.kodi/addons/tools.ffmpeg-tools/bin/ffprobe",
    "/usr/bin/ffprobe",
    "/storage/.opt/bin/ffprobe",
]

# Pass-through proxy recovery constants
_UPSTREAM_OPEN_TIMEOUT = 60
# Dedicated recv() deadline for the streaming body, armed explicitly on the
# upstream socket AFTER the response headers arrive (see
# _set_upstream_read_timeout). _UPSTREAM_OPEN_TIMEOUT above is inherited by
# recv() too, but we want a tighter, explicit bound so a stalled backend (all
# Usenet providers returning article-not-found) surfaces as a RECOVERABLE read
# result that drives live fallback — and wins the race against the equal 60 s
# proxy->Kodi write timeout (_REMUX_WRITE_TIMEOUT) instead of unwinding as
# terminal_reason="client_disconnected" with recoveries=0. Kept well above a
# realistic single-article fetch so a slow-but-progressing source is not
# falsely rotated. See https://github.com/Appz4Fun/nzbdavkodi/issues/214
_UPSTREAM_READ_TIMEOUT = 45
_SKIP_PROBE_TIMEOUT = 60
# Geometric skip sizes for probing past a bad article region. 1 MB covers a
# single missing article (~700 KB). 16 MB covers a cluster of ~20 articles.
_SKIP_PROBE_SIZES = (1048576, 4194304, 16777216)
# When a probe fails fast (ConnectionRefused from docker-proxy during nzbdav
# restart, TCP RST, or immediate HTTP error) we back off and retry before
# moving to the next skip size. This gives a briefly-unavailable upstream a
# chance to recover instead of declaring the stream dead in milliseconds.
_PROBE_RETRY_DELAYS = (2, 4, 6, 8)
# Wall-clock budget for a single recovery attempt. After this the proxy
# zero-fills the remainder so the client response always completes.
_MAX_RECOVERY_SECONDS = 30
# Cap zero-filled bytes per response to prevent runaway silent playback when
# an NZB is mostly corrupt. 64 MB ≈ several seconds of 4K REMUX video.
_MAX_TOTAL_ZERO_FILL = 67108864
# Patient forward-stall wait (pass-through). When an ESTABLISHED forward stream
# (real upstream bytes already delivered this request) stalls on a RECOVERABLE
# backend condition — a still-downloading high-water short read
# (AWAITING_DOWNLOAD) or a transient 5xx/connection error (UPSTREAM_ERROR) — the
# session breaker (upstream_down_notified) has short-circuited BOTH the retry
# ladder and the skip-probe to instant give-up, so the loop would close in ms
# and Kodi reads the premature Connection: close as demuxer EOF (the live 4K
# REMUX mid-stream black screen). Instead, keep the CLIENT connection OPEN and
# re-read with abortable backoff up to this budget so a recovering backend
# resumes. A monotonic stall clock resets on ANY genuine forward byte, so a
# healthy still-downloading stream is never condemned; only a TRULY-stuck stream
# exhausts the budget and then falls through to the existing give-up paths. Bound
# it at/under Kodi's network curllowspeedtime so Kodi buffers through the wait
# rather than tearing down and stranding this handler as a zombie. 0 disables the
# wait (restores the prior instant-close behavior). Does NOT apply to
# SHORT_READ_RECOVERABLE (genuinely-missing articles must zero-fill past) nor to
# a byte-0 first read / fresh seek that never streamed (issue #214 fast-fail).
_DEFAULT_PASSTHROUGH_STALL_WAIT_SECONDS = 120
_PASSTHROUGH_STALL_WAIT_MAX_SECONDS = 600
_PASSTHROUGH_STALL_WAIT_BACKOFF_SECONDS = 2.0
# Density breaker: abort if the recent recovery window becomes mostly synthetic
# data instead of real upstream bytes.
_DENSITY_BREAKER_WINDOW_BYTES = 16 * 1024 * 1024
_DENSITY_BREAKER_ZERO_FILL_RATIO = 0.5
# Throughput stall watchdog (pass-through, video only). When the proxy→Kodi
# byte rate falls below this threshold over the rolling window, the response
# is closed so Kodi's CCurlFile reconnects with a fresh upstream fetch.
# Without this, a slow-trickle upstream — e.g. a Usenet article fetch that
# takes 60+ seconds — keeps delivering chunks under the per-read socket
# timeout (_UPSTREAM_OPEN_TIMEOUT, 60 s), so neither the urlopen-level
# timeout nor Kodi's own watchdog ever fires. Bytes drip in below playable
# rate, Kodi's CFileCache underruns, audio stalls, and the player wedges in
# a state where subsequent seeks don't trigger a fresh range request
# (CFileCache considers the source still "open"). 100 KB/s is well under
# any video bit rate that needs streaming (the slowest video is ~1 Mbps =
# 125 KB/s) but well ABOVE realistic audio rates (a 64 kbps MP3 is 8 KB/s),
# which is why the watchdog is gated on a video content type — otherwise a
# slow-but-legitimate audio stream would get rotated every 20 s.
_PASSTHROUGH_MIN_THROUGHPUT_BPS = 102400
_PASSTHROUGH_THROUGHPUT_WINDOW_SECONDS = 20.0


# Chunk size for reading from the upstream HTTP response in _serve_proxy.
# Kept small (64 KB) because on 32-bit Kodi the address space is ~3 GB and
# Kodi's CFileCache can reserve up to ~1.5 GB on its own. A 1 MB read
# buffer has been observed to hit MemoryError when a second proxy
# connection opens during Kodi's CCurlFile reconnect-on-error recovery.
_UPSTREAM_READ_CHUNK = 65536

_STRICT_CONTRACT_MODE_OFF = "off"
_STRICT_CONTRACT_MODE_WARN = "warn"
_STRICT_CONTRACT_MODE_ENFORCE = "enforce"

_UPSTREAM_RANGE_OK = "OK"
_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE = "SHORT_READ_RECOVERABLE"
# A clean short read at the download high-water mark: the upstream upload is
# still downloading and simply hasn't fetched this byte yet. Unlike a wedged
# or trickling upstream (#214), the primary is healthy — so the right response
# is to wait for the buffer to fill via the retry ladder, NOT to fail over to
# fallback sources that may themselves still be downloading. Treating this as
# fallback_exhausted closed the stream prematurely and stalled playback (the
# "Empire stalled at 1:11" regression).
_UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD = "SHORT_READ_AWAITING_DOWNLOAD"
_UPSTREAM_RANGE_PROTOCOL_MISMATCH = "PROTOCOL_MISMATCH"
_UPSTREAM_RANGE_UPSTREAM_ERROR = "UPSTREAM_ERROR"
_UPSTREAM_RANGE_CLIENT_ERROR = "CLIENT_ERROR"
_RECOVERABLE_HTTP_RANGE_ERROR_CODES = frozenset({416})

_SESSION_ZERO_FILL_RATIO_MAX = 0.05
_RECOVERY_NOTIFY_DEBOUNCE_SECONDS = 60.0
_RANGE_RETRY_DELAYS = (2, 4, 8)
# The (2, 4, 8) ladder above is the right backoff for a MID-STREAM rebuffer:
# Kodi is already playing and will wait while a still-downloading file catches
# up. Applying it to the FIRST byte, though, makes the proxy hold Kodi's initial
# open silent for up to ~14 s — longer than the player's first-read patience —
# so Kodi disconnects at byte 0 and the summary logs
# ``streamed=0 reason=client_disconnected`` (no video plays). When nothing has
# streamed yet, use a short schedule so byte 0 arrives promptly, or the
# connection closes fast enough for Kodi's CCurlFile to reconnect and retry.
_FIRST_BYTE_RANGE_RETRY_DELAYS = (0.25, 0.5, 1.0)
# Bounded exhaustion cap for the fallback cutover fall-through (F4). When
# fallback sources are attached but none validate and the primary makes no
# forward progress, _serve_proxy re-enters the retry ladder so a TRANSIENT
# trickle can recover (e3a74a1). But an indefinitely-dead primary with
# never-validating fallbacks would otherwise spin the ladder / zero-fill until
# the client gives up (looks like a hang). After this many CONSECUTIVE
# fall-throughs that streamed no new REAL upstream bytes, close cleanly with
# terminal_reason="fallback_exhausted" instead of looping. The transient
# recovery path resets the counter on any genuine streamed progress, so a
# healthy primary that briefly trickles is never penalised.
_FALLBACK_PENDING_FALLTHROUGH_MAX = 3
# F8-dropout: tri-state result for _fallback_source_matches. A definitive
# MISMATCH (provably different file) permanently fails the source; a transient
# INCONCLUSIVE (still-downloading region / probe 5xx / timeout / empty digest)
# keeps the source eligible and is reconsidered on the next cutover. ``True``
# (MATCH) means usable now. INCONCLUSIVE is a unique sentinel so it can never
# be confused with the legacy truthy/falsy contract that callers still honour
# (truthy -> select, falsy -> permanent fail).
_FALLBACK_MATCH = True
_FALLBACK_MISMATCH = False
_FALLBACK_INCONCLUSIVE = object()
# Bound on how many CONSECUTIVE transient (INCONCLUSIVE) misses a single
# fallback source may accrue before it is abandoned (failed=True). Without this
# bound a source that is permanently INCONCLUSIVE (e.g. an upstream that always
# 5xxs the probe) would be reconsidered forever on every cutover. Reset to 0
# whenever the source produces a definitive answer or validates.
_FALLBACK_SOURCE_TRANSIENT_MISS_MAX = 4
# F-route: a DEAD primary whose missing-article region reads as a CLEAN
# download-high-water short read (AWAITING_DOWNLOAD) would otherwise spin the
# retry ladder forever and never fail over (58f3d4f routed AWAITING_DOWNLOAD to
# the ladder, not fallback, to avoid premature fallback_exhausted). After this
# many CONSECUTIVE AWAITING_DOWNLOAD reads that make NO forward progress, allow
# a failover to a validated fallback by routing into the live-cutover path. A
# primary that IS still downloading and making progress resets the counter and
# keeps using the ladder, preserving 58f3d4f's intent.
_AWAITING_DOWNLOAD_NO_PROGRESS_MAX = 3
_AUTH_HEADER_NOT_PROVIDED = object()
_FALLBACK_SOURCE_STATE_NOT_PROVIDED = object()
_FALLBACK_SOURCE_STREAM_URL_HINT_KEY = "_fallback_source_stream_url_hint"

# Env-gated fault injection for verifying the live fallback cutover end to end.
# Inert unless NZBDAV_FAULT_PRIMARY_FAIL_AFTER_BYTES is set to a positive int in
# the addon process environment. When set, the PRIMARY upstream (before any
# fallback switch) is forced to fail once a range at/after that byte offset is
# requested, so the cutover path runs against a real, already-downloaded
# fallback. Off by default — safe to ship permanently.
_FAULT_PRIMARY_FAIL_AFTER_BYTES_ENV = "NZBDAV_FAULT_PRIMARY_FAIL_AFTER_BYTES"
# Spare the file tail (MKV cues/SeekHead live at EOF) from the fault so the
# demuxer can initialize and playback runs long enough for the fallback worker
# to attach+validate alternates — otherwise the very first tail read (at a
# ~file-size offset) trips the fault before any cutover target exists.
_FAULT_TAIL_GUARD_BYTES = 1073741824  # 1 GiB


_FALLBACK_PRIMARY_URL_HINT_KEY = "_fallback_primary_url_hint"
_FALLBACK_PRIMARY_AUTH_HINT_KEY = "_fallback_primary_auth_hint"
_FALLBACK_CURRENT_RANGE_CACHE_KEY = "_fallback_current_range_cache"
_FALLBACK_FINGERPRINT_WORKERS = 10
# Upper bound on concurrent pass-through handler threads. Kodi forces
# Connection: close per response, so every seek/retry opens a fresh connection
# and thus a fresh handler thread; without a cap a burst can exhaust the OS
# thread/stack budget and raise "can't start new thread", after which the
# listener stays up but cannot answer (the "background service unreachable"
# wedge). Excess connections are dropped cleanly so Kodi reconnects.
_MAX_PROXY_WORKERS = 64
_FALLBACK_PRIMARY_DIGEST_CACHE_MAX = 512
_INITIAL_RANGE_PREFETCH_WAIT_SECONDS = 0.08
# Kodi reads the MKV cues/SeekHead at the FILE TAIL before playback. nzbdav
# fetches those end-of-file usenet articles on demand, so the first tail read
# stalls 1-4s mid-startup and can wedge the CoreELEC audio clock (black screen).
# A throwaway read of the last _TAIL_PREWARM_BYTES during the prepare gap warms
# nzbdav's article cache so Kodi's real cues read is fast. 1 MiB comfortably
# covers the cues/SeekHead region Kodi reads at startup.
_TAIL_PREWARM_BYTES = 1048576
# The tail prewarm warms the MKV cues, but firing its upstream read at prepare
# time made it RACE Kodi's first-byte range request and the byte-0 prefetch for
# nzbdav's connection budget — widening the same mid-startup stall window the
# prewarm exists to close (transient black-screen). Hold the tail read back a
# short, ABORTABLE beat so the byte-0 prefetch and Kodi's initial range fetch
# win the budget first; Kodi's own cues read still arrives well after this. The
# wait uses xbmc.Monitor.waitForAbort so a Kodi shutdown / session stop during
# the defer cancels the prewarm cleanly (no wasted connection) instead of
# blocking the daemon thread.
_TAIL_PREWARM_DEFER_SECONDS = 1.5
# Per-session forward read-ahead prefetch cache. A bounded contiguous in-memory
# window is filled by a daemon thread that reads sequentially AHEAD of the
# highest-served offset using the existing upstream-fetch primitive, throttling
# when full and freeing consumed bytes behind the play head — so it keeps filling
# WHILE Kodi is paused, letting a pause build a real lead. The serve path
# consults the window FIRST; on a miss it falls through to today's untouched
# upstream-read / retry-ladder / patient-forward-stall / fallback-cutover /
# 404-awaiting path. Gated by readahead_buffer_mb (default 256, 0=off). When
# disabled or on any miss, behavior is byte-for-byte identical to today.
_READAHEAD_BUFFER_KEY = "_readahead_buffer"
_READAHEAD_THREAD_KEY = "_readahead_thread"
_DEFAULT_READAHEAD_BUFFER_MB = 256
_READAHEAD_BUFFER_MB_MAX = 4096
# Reuse the 64KB upstream chunk for heap-friendliness (the MemoryError class on
# 32-bit/CoreELEC was fixed by small chunked reads).
_READAHEAD_FETCH_CHUNK = _UPSTREAM_READ_CHUNK
# Abortable wait when the window is full (keeps it filling while paused yet
# bounded — resumes as the served offset advances and frees room).
_READAHEAD_THROTTLE_BACKOFF_SECONDS = 0.25
# Abortable wait after a best-effort upstream error (prefetch is best-effort and
# the real serve path owns recovery — this never trips the user-facing taxonomy).
_READAHEAD_ERROR_BACKOFF_SECONDS = 1.0
# Hold the read-ahead's FIRST upstream read back a short, abortable beat so the
# byte-0 prefetch and Kodi's initial range fetch win nzbdav's connection budget
# first — the read-ahead must never widen the mid-startup stall window the byte-0
# prefetch / tail prewarm exist to close (transient black-screen). The lead is
# rebuilt steadily after startup. waitForAbort lets a Kodi shutdown / session
# stop during the defer cancel the prefetch cleanly.
_READAHEAD_START_DEFER_SECONDS = 1.5
_PASSTHROUGH_RUNTIME_SETTINGS_KEY = "_passthrough_runtime_settings"
_PASSTHROUGH_RUNTIME_SETTINGS_DONE_KEY = "_passthrough_runtime_settings_done"
_PASSTHROUGH_RUNTIME_SETTINGS_ERROR_KEY = "_passthrough_runtime_settings_error"
_SETTINGS_SNAPSHOT_KEYS = (
    "force_remux_threshold_mb",
    "force_remux_mode",
    "force_remux_mode_v2_migrated",
    "strict_contract_mode",
    "density_breaker_enabled",
    "zero_fill_budget_enabled",
    "retry_ladder_enabled",
    "send_200_no_range",
    "proxy_convert_subs",
    "readahead_buffer_mb",
    # Serialized so _passthrough_runtime_settings_from_snapshot() honors a
    # user-tuned (or 0-to-disable) patient stall wait on the service /prepare
    # path; without it the snapshot consumer always fell back to the default.
    "passthrough_stall_wait",
)


# Shared zero buffer reused across all pass-through responses.
_ZERO_FILL_BUFFER = bytes(65536)

# Socket write timeout for _serve_remux.  If Kodi stops reading from the
# proxy socket without closing it (decoder stalls for too long, e.g. during
# a long DB vacuum) wfile.write() would block forever and ffmpeg would keep
# producing output into the void.  60s comfortably exceeds any normal
# buffering stall on a healthy client while still bounding zombie lifetime.
_REMUX_WRITE_TIMEOUT = 60
_REMUX_STDOUT_IDLE_TIMEOUT = 30.0
_PREPARE_TOKEN_HEADER = "X-NZBDAV-Token"  # nosec B105 — HTTP header name, not a secret
# /prepare client retry. A momentarily thread-starved proxy accepts then drops
# the loopback connection (RemoteDisconnected / reset / refused) — a FAST
# failure that clears in well under a second once a handler thread frees up. A
# single POST would otherwise surface the terminal "background service
# unreachable" dialog on that first transient hiccup, so retry the FAST
# connection failures a few times with a short backoff. A genuine timeout is
# NOT retried: it means the proxy accepted but is wedged, or a slow-but-
# reachable prepare that already had the full budget — retrying another full
# budget can't help and would multiply the wait. So the worst case stays the
# original single timeout, not a multiple of it.
_PREPARE_MAX_ATTEMPTS = 3
_PREPARE_ATTEMPT_TIMEOUT = 60
_PREPARE_RETRY_BACKOFF = 0.25
_PROP_PROXY_TOKEN = "nzbdav.proxy_token"  # nosec B105 — settings key, not a secret
# POST /stream/<session_id>/fallbacks — merge late-adopted fallback sources into
# a live session whose /prepare snapshot was taken before the fallback worker
# finished adopting them (the cutover-never-fires race).
_FALLBACK_UPDATE_PATH_RE = re.compile(r"^/stream/([^/]+)/fallbacks$")
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+)(?:\.(\d+))?")
_CONTENT_RANGE_ZERO_RE = re.compile(r"^bytes\s+0-0/(\d+)$")
# The `0*(\d+)` split intentionally strips leading zeros at match time so the
# replacement can use a plain `seg_\1.\2` string instead of an int()-casting
# callback. Do not "simplify" this to `seg_(\d+)` — that would keep the zero
# padding (seg_007 → seg_007 instead of seg_7) and silently break normalization.
_SEGMENT_NORMALIZE_RE = re.compile(r"seg_0*(\d+)\.(m4s|ts)")

# HLS segment length. Shorter segments (6 s) minimize the playlist-
# vs-actual drift that breaks seek accuracy and A/V sync on the fmp4
# path. The playlist emits fixed-duration EXTINF values based on
# this constant, but ffmpeg's `-hls_time` only places cuts at the
# next IDR after the target, so real segment durations drift ±GOP
# around the nominal. With 30 s segments and 3-5 s source GOPs that
# drift accumulates into visible A/V desync and seek misses over a
# 2-hour movie; with 6 s segments the per-segment error is the same
# but the accumulation window is shorter and a seek respawn lands
# much closer to the requested timestamp. The price is more segment
# file churn and more HTTP round-trips during linear playback, but
# HlsProducer uses ONE ffmpeg across many segments so cold-start
# amortization still holds. Also 6 s is the CMAF / Apple HLS author
# guide recommended default.
_HLS_SEGMENT_SECONDS = 6.0

# If an HLS segment request is more than this many segments ahead of the live
# ffmpeg producer, restart at the requested segment instead of waiting for
# ffmpeg to naturally catch up. With 6 s segments this keeps 5-minute and
# 15-minute skips from waiting on dozens of intermediate segments.
_HLS_FORWARD_WAIT_SEGMENTS = 2

# Disk-backed HLS session working directory. Must be on a filesystem
# with enough free space for the full remuxed output of any active
# session (~5 GB per 30 minutes at typical 4K REMUX bitrates). Each
# session gets its own subdirectory which is rm -rf'd on cleanup.
# Candidate paths in order — first one that exists + is writable wins.
# If none are available, fall back to a private mkdtemp() directory
# instead of a fixed shared /tmp path.
_HLS_WORKDIR_CANDIDATES = (
    "/var/media/CACHE_DRIVE/nzbdav-hls",
    "/var/media/STORAGE/nzbdav-hls",
    "/storage/nzbdav-hls",
)

# How long to wait for a segment file to appear on disk before
# declaring the fetch failed. Must exceed ffmpeg cold-start + a seek's
# worth of container parsing on the largest supported input.
_HLS_SEGMENT_WAIT_SECONDS = 90.0

# Segment file is considered complete when the next segment exists
# OR when its mtime has been stable for this many milliseconds.
_HLS_SEGMENT_MTIME_STABLE_MS = 500

# Hard wall-clock deadline for ffmpeg-based probes (duration, DV
# profile). These probes spawn ``ffmpeg -v info -i <url> -f null -``
# and scan stderr for a specific line. If ffmpeg hangs on the network
# read (slow upstream, auth negotiation, stalled header parse) it may
# never emit stderr output at all — without a wall-clock guard, the
# reader loop blocks forever. 30 s is very generous for a healthy
# LAN probe (typical: <2 s to Duration line on a 4K REMUX) and still
# bounded enough that a stuck probe can't wedge the prepare_stream
# path past the plugin client's 60 s /prepare timeout.
_PROBE_DEADLINE_SECONDS = 30.0

# Default threshold above which non-MP4 files are force-remuxed through
# ffmpeg instead of served as HTTP pass-through.  0 disables force-remux
# entirely.
#
# History: an earlier branch disabled force-remux by default because 12 GB
# MKV pass-through tested clean on a 32-bit Amlogic CoreELEC build. A later
# 58 GB Shawshank REMUX (and a reproduced 15.8 GB Mayor of Kingstown remux)
# both crashed with `Open - Unhandled exception` in `CVideoPlayer::
# OpenInputStream`, even though the proxy's HTTP/206 range responses are
# byte-correct under curl. The crash is deterministic at byte 0, so it isn't
# file corruption or transport — it's a 32-bit overflow somewhere in Kodi's
# cache/offset math when the advertised Content-Length is large enough.
# The existing "pass-through works for 12 GB" data point and the "58 GB
# crashes" data point put the real ceiling somewhere between those, which
# is why the default is set generously below the lowest known-bad size.
#
# ffmpeg-remux is strictly worse on files that would have passed through
# fine — seeks go through ffmpeg `-ss` instead of the source's own Cue
# index, missing Usenet articles no longer zero-fill transparently, and
# there is real CPU cost — so the threshold is kept high enough that only
# genuinely huge files get remuxed.  Users who see false positives can
# set `force_remux_threshold_mb` in the addon settings to raise the bar
# further (or to 0 to disable entirely and restore pure pass-through).
_DEFAULT_FORCE_REMUX_THRESHOLD_MB = 15000
# Clamp ceiling for force_remux_threshold_mb. Set just below 2^53 so any
# JSON-safe int the user enters survives without triggering the
# "out of range" warning every play. Realistic "I want this off" values
# (e.g. 20_000_000 MB = 20 TB) used to clamp to 1 TB and re-log on every
# play (TODO.md §D.8.2). Raising the ceiling silences that without
# changing the semantics — values above this are still real user error.
_FORCE_REMUX_THRESHOLD_MB_MAX = (1 << 53) - 1
_PREPARE_REQUEST_MAX_BYTES = 64 * 1024
_FFMPEG_CAPABILITY_PROBE_TIMEOUT = 5
_FMP4_HLS_CAPABILITY_MARKERS = (
    "-hls_segment_type",
    "-hls_fmp4_init_filename",
)


def _get_private_hls_temp_root():
    """Return a reusable private temp root for HLS work files."""
    global _HLS_PRIVATE_TEMP_ROOT  # pylint: disable=global-statement

    with _HLS_PRIVATE_TEMP_ROOT_LOCK:
        cached = _HLS_PRIVATE_TEMP_ROOT
        if cached and os.path.isdir(cached) and os.access(cached, os.W_OK):
            return cached

        temp_root = tempfile.mkdtemp(prefix="nzbdav-hls-")
        # 0o700 is restrictive (user-only); semgrep rule is a false positive.
        try:
            os.chmod(temp_root, 0o700)  # nosemgrep
        except OSError:
            pass

        _HLS_PRIVATE_TEMP_ROOT = temp_root
        return temp_root


_UPSTREAM_REACHABILITY_UNREACHABLE_NETWORK = "unreachable_network"
_UPSTREAM_REACHABILITY_HTTP_SERVER_ERROR = "http_5xx"
_UPSTREAM_REACHABILITY_HTTP_CLIENT_ERROR = "http_4xx"
_UPSTREAM_REACHABILITY_OTHER = "other"


# Pass-through terminal reasons that mean the BACKEND could not deliver the
# stream (a throughput stall, fallback exhaustion, or a zero-fill blowout) — as
# opposed to a clean finish ("complete") or a healthy user stop. Used to surface
# a clear "nzbdav can't keep up" notification instead of a silent black screen.
_STARVATION_TERMINAL_REASONS = (
    "passthrough_stall",
    "fallback_exhausted",
    "session_zero_fill_budget_exceeded",
)
# How recently (seconds) an upstream outage must have occurred, relative to the
# stream ending, for a client_disconnected end to count as backend starvation
# rather than a healthy user stop. Catches the live incident (upstream blipped
# back ~9s before Kodi gave up) without firing on a long-recovered early blip.
_STARVATION_RECENT_OUTAGE_SECONDS = 60


# Byte-offset delta used to distinguish a Kodi buffer-reconnect from a
# user-initiated seek.  When Kodi reconnects after a brief network hiccup it
# resumes very close to where it left off; a true seek jumps much further.
# 10 MB was chosen empirically: large enough to ignore normal buffering
# overlap, small enough to catch seeks that would noticeably re-position
# the stream.  Adjust if you observe unnecessary ffmpeg restarts in logs.
_SEEK_THRESHOLD = 10 * 1024 * 1024


class _ProxyStreamState:  # pylint: disable=too-few-public-methods
    """Mutable per-request state for ``_StreamHandler._serve_proxy``.

    A plain attribute holder that lets the pass-through loop body be split
    into cohesive per-phase helpers without changing behavior: every value
    that the original single-function loop kept as a local now lives here so
    the extracted step methods can read and mutate the SAME state, in the SAME
    order, exactly as the inline code did.
    """

    __slots__ = (
        "start",
        "end",
        "current",
        "total_streamed",
        "total_skipped",
        "recovery_count",
        "terminal_reason",
        "fallback_pending_fallthroughs",
        "last_fallthrough_streamed",
        "awaiting_download_no_progress",
        "density_window",
        "active_ctx",
        "fallback_pending_candidate",
        "candidate_delivered",
        "fallback_failed_to_notify",
        "streamed_real_upstream_bytes",
        "forward_stall_t0",
        "stall_wait_budget",
        "contract_mode",
        "density_breaker_enabled",
        "zero_fill_budget_enabled",
        "retry_ladder_enabled",
        "result",
        "progressed_this_iter",
    )

    def __init__(self):
        # Slot defaults; every field is overwritten by _serve_proxy_init_state /
        # _serve_proxy_unpack_runtime before it is read. Declared here so the
        # extracted step helpers don't trip attribute-defined-outside-init.
        self.start = 0
        self.end = 0
        self.current = 0
        self.total_streamed = 0
        self.total_skipped = 0
        self.recovery_count = 0
        self.terminal_reason = "unknown"
        self.fallback_pending_fallthroughs = 0
        self.last_fallthrough_streamed = -1
        self.awaiting_download_no_progress = 0
        self.density_window = None
        self.active_ctx = None
        self.fallback_pending_candidate = None
        self.candidate_delivered = False
        self.fallback_failed_to_notify = None
        self.streamed_real_upstream_bytes = 0
        self.forward_stall_t0 = None
        self.stall_wait_budget = 0
        self.contract_mode = None
        self.density_breaker_enabled = False
        self.zero_fill_budget_enabled = False
        self.retry_ladder_enabled = False
        self.result = None
        self.progressed_this_iter = False


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


class _ThreadedHTTPServer(_ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread.

    Hardened against thread/stack exhaustion: handler threads are bounded by a
    semaphore (``_MAX_PROXY_WORKERS``) and the per-connection spawn is wrapped
    so a transient ``RuntimeError: can't start new thread`` drops the accepted
    connection cleanly (Kodi reconnects) instead of escaping as an unhandled
    traceback while the listener stays up but unanswering. Stdlib
    ``ThreadingMixIn`` does neither, which is what let a playback burst wedge
    the proxy into "background service unreachable on 127.0.0.1:<port>".
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.stream_context = None
        self.stream_sessions = {}
        self.pending_stream_contexts = {}
        self.active_ffmpeg = None
        self.current_byte_pos = 0
        self.ffmpeg_lock = threading.Lock()
        self.owner_proxy = None
        self.prepare_token = ""  # nosec B105 — empty init value, not a secret
        self._worker_slots = threading.BoundedSemaphore(_MAX_PROXY_WORKERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        """Spawn a bounded, RuntimeError-tolerant handler thread.

        ``__new__``-built test doubles (and any subclass that skips __init__)
        have no ``_worker_slots`` — fall back to an unbounded-but-guarded spawn
        in that case rather than erroring.
        """
        slots = getattr(self, "_worker_slots", None)
        if slots is not None and not slots.acquire(blocking=False):
            xbmc.log(
                "NZB-DAV: Proxy at worker cap ({}); dropping connection so the "
                "client reconnects (reason=worker_cap)".format(_MAX_PROXY_WORKERS),
                xbmc.LOGWARNING,
            )
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            # Out of OS thread/stack budget. Release the slot we reserved and
            # close the accepted socket so the client sees a reset and retries,
            # instead of leaking the connection behind an unhandled traceback.
            if slots is not None:
                try:
                    slots.release()
                except ValueError:
                    pass
            xbmc.log(
                "NZB-DAV: Could not start proxy handler thread; dropping "
                "connection so the client reconnects (reason=thread_exhausted)",
                xbmc.LOGWARNING,
            )
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        """Release the worker slot once the handler thread finishes."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            slots = getattr(self, "_worker_slots", None)
            if slots is not None:
                try:
                    slots.release()
                except ValueError:
                    pass


class HlsProducer:
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

    def segment_path(self, seg_n):
        """Return the disk path for a segment index, with the extension
        determined by this producer's segment_format."""
        ext = "m4s" if self.segment_format == "fmp4" else "ts"
        return os.path.join(self.session_dir, "seg_{:06d}.{}".format(seg_n, ext))

    def playlist_path(self):
        """Return the ffmpeg-generated playlist path for fMP4 HLS."""
        return os.path.join(self.session_dir, "ffmpeg_playlist.m3u8")

    def generated_playlist_body(self):
        """Return ffmpeg's playlist with proxy-friendly segment names."""
        path = self.playlist_path()
        try:
            with open(path, "r", encoding="utf-8") as playlist_file:
                text = playlist_file.read()
        except OSError:
            return None
        if "#EXTINF:" not in text:
            return None

        text = _SEGMENT_NORMALIZE_RE.sub(r"seg_\1.\2", text)
        return text.encode("utf-8")

    def _segment_complete(self, seg_n):
        """True if seg_n.ts exists and is no longer being written.

        Completion is detected by either: the next segment file also
        exists (ffmpeg has moved on), or the file's mtime has been
        stable for more than _HLS_SEGMENT_MTIME_STABLE_MS.

        For fMP4, the "next segment exists" signal is only trusted
        if the next segment was created after the current ffmpeg
        spawn — otherwise a stale seg_n+1 from a prior generation
        can make this return True while the new seg_n is still
        being written.
        """
        # Snapshot _spawn_time under the lock so a concurrent respawn
        # can't update it between our two checks below. The atomic
        # getattr-after-lock pattern guarantees we compare every mtime
        # against a single consistent generation boundary.
        with self._lock:
            spawn_time = self._spawn_time
        path = self.segment_path(seg_n)
        if not os.path.exists(path):
            return False
        if self._next_segment_signals_complete(seg_n, spawn_time):
            return True
        # Final segment (or ffmpeg briefly mid-transition) — fall back
        # to mtime stability.
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return False
        if self.segment_format == "fmp4":
            return self._fmp4_segment_complete(seg_n, mtime, spawn_time)
        if (time.time() - mtime) * 1000.0 > _HLS_SEGMENT_MTIME_STABLE_MS:
            return True
        # If this is the terminal segment (no N+1 will ever exist),
        # ffmpeg should have exited by now.
        if seg_n >= self.total_segments - 1:
            return self._terminal_ffmpeg_exited()
        return False

    def _next_segment_signals_complete(self, seg_n, spawn_time):
        """True if seg_n+1's existence proves seg_n is complete.

        In fMP4 mode the next segment is only trusted if it was created
        after the latest spawn (a stale seg_n+1 from a prior generation
        must not mark the freshly-written seg_n complete).
        """
        next_path = self.segment_path(seg_n + 1)
        if not os.path.exists(next_path):
            return False
        if self.segment_format != "fmp4":
            return True
        try:
            next_mtime = os.path.getmtime(next_path)
        except OSError:
            return False
        return next_mtime >= spawn_time

    def _fmp4_segment_complete(self, seg_n, mtime, spawn_time):
        """fMP4 mtime-path completeness check for seg_n.

        Requires THIS segment to be from the current ffmpeg generation
        (mtime >= spawn_time). Without this guard a backward seek can
        read a stale ``seg_n.m4s`` from a prior generation whose mtime
        is far in the past — the mtime-stability check is trivially
        true for such a file. The bytes are valid but were produced
        against a different edit list / timestamp base, so Kodi's HLS
        demuxer glitches or stalls when splicing them.
        """
        if mtime < spawn_time:
            return False
        if seg_n >= self.total_segments - 1:
            return self._terminal_ffmpeg_exited()
        return False

    def _terminal_ffmpeg_exited(self):
        """True if the current ffmpeg process has exited (terminal seg)."""
        with self._lock:
            proc = self._proc
        return proc is not None and proc.poll() is not None

    def _init_file_complete(self):
        """True iff init.mp4 was written by the current ffmpeg
        generation AND ffmpeg has moved on to segment output.

        Generation boundary: _ensure_ffmpeg_headed_for unlinks
        BOTH init.mp4 AND seg_<new_target>.m4s before every
        spawn. So any init.mp4 on disk post-spawn is from the
        current generation, and any seg_<start_segment>.m4s on
        disk post-spawn was written by the current ffmpeg too
        (a prior generation cannot have produced a file we just
        unlinked).

        The "seg_<start_segment>.m4s exists" signal proves ffmpeg
        has finished the init box — the fMP4 HLS muxer writes
        init.mp4 fully before opening any segment file.
        """
        if self.segment_format != "fmp4":
            return False
        init_path = os.path.join(self.session_dir, "init.mp4")
        if not os.path.exists(init_path):
            return False
        # Deliberately reading self._start_segment WITHOUT self._lock.
        #
        # Why it's safe today:
        #   * CPython stores Python ints as PyObject*; assignment is a
        #     single pointer store and reads of that pointer are atomic
        #     under the GIL. A reader never sees a half-written int.
        #   * The caller (``wait_for_init`` / poll loop) tolerates a
        #     stale read: if ``_start_segment`` has just advanced, the
        #     stale value points at a segment path that already exists
        #     on disk (the previous target) — returning True early is
        #     correct because init.mp4 is complete in both generations.
        #     If we read the stale value and return False, the next
        #     poll cycle (~50 ms later) reads the fresh value.
        #   * Holding self._lock here would serialize the polling reader
        #     against the respawn writer and defeat the purpose of the
        #     fast-path existence check.
        #
        # Why future refactors should revisit this:
        #   * If this module ever runs under a no-GIL interpreter (PEP
        #     703) or switches to asyncio with thread-pool executors,
        #     the "atomic int read" assumption weakens.
        #   * If ``_start_segment`` ever grows into a tuple / object
        #     (e.g. (generation_id, seg_n)), the read is no longer
        #     atomic and a reader can see a torn value.
        #   * Drop-in mitigation when that day comes: replace the bare
        #     int with a ``threading.Event`` that the respawn path
        #     sets() after publishing the new ``_start_segment``, and
        #     have this method wait() on the event before reading.
        first_seg_path = os.path.join(
            self.session_dir,
            "seg_{:06d}.m4s".format(self._start_segment),
        )
        return os.path.exists(first_seg_path)

    def _cache_canonical_init_bytes(self, init_path):
        """Cache the first init.mp4 we see so later requests (and respawn
        generations with different edit lists) serve byte-identical data.
        See the docstring on self._canonical_init_bytes for the full
        rationale. No-op once the cache is populated.
        """
        if self._canonical_init_bytes is not None:
            return
        try:
            with open(init_path, "rb") as f:
                self._canonical_init_bytes = f.read()
            xbmc.log(
                "NZB-DAV: Cached canonical init.mp4 "
                "({} bytes) for session".format(len(self._canonical_init_bytes)),
                xbmc.LOGINFO,
            )
        except OSError as e:
            xbmc.log(
                "NZB-DAV: Failed to cache canonical init.mp4: {}".format(e),
                xbmc.LOGWARNING,
            )

    def _spawn_for_init_if_dead(self):
        """If no ffmpeg is alive, (re)spawn it at its current target.

        Bootstrap (fresh session: target defaults to 0) or respawn at
        whatever target the last generation had. DO NOT hardcode 0 — a
        crashed mid-seek producer still has the right start_segment to
        resume at. If ffmpeg is alive, leave it alone (CRITICAL B).
        """
        with self._lock:
            proc = self._proc
            alive = proc is not None and proc.poll() is None
            current_target = self._start_segment
        if not alive:
            self._ensure_ffmpeg_headed_for(current_target)

    def wait_for_init(self, timeout=_HLS_SEGMENT_WAIT_SECONDS):
        """Block until init.mp4 for the current producer generation
        exists and seg_<start_segment>.m4s proves ffmpeg moved past
        the init write phase. Returns the init path on success or
        None on timeout.

        CRITICAL A: this method must actively spawn ffmpeg if none
        is running. Kodi typically fetches #EXT-X-MAP BEFORE any
        segment, so a poll-only implementation would deadlock on
        the very first request.

        CRITICAL B: if ffmpeg IS running (e.g. Kodi re-fetches the
        init after a forward seek to seg 40), this method must NOT
        rewind the producer back to seg 0. Any running ffmpeg is
        left at its current _start_segment target.
        """
        if self.segment_format != "fmp4":
            return None
        init_path = os.path.join(self.session_dir, "init.mp4")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._closed:
                return None
            # Fast path: files already on disk for the current
            # generation. The on-disk check IS the truth-source —
            # _init_ready is just a redundant cached flag we set
            # below for any downstream consumer that wants to skip
            # the file syscall on subsequent calls.
            if self._init_file_complete():
                self._init_ready = True
                self._cache_canonical_init_bytes(init_path)
                return init_path
            self._spawn_for_init_if_dead()
            # If ffmpeg is alive, leave it alone — it's either
            # already headed toward the right segment, or the init
            # re-fetch is racing a valid seek that's already
            # produced init.mp4 once and will produce it again
            # after the seek-restart cleans up.
            if self._init_file_complete():
                self._init_ready = True
                return init_path
            # Use Monitor.waitForAbort instead of bare time.sleep so a
            # Kodi shutdown during HLS warmup unblocks immediately.
            # waitForAbort returns True iff Kodi is shutting down — bail
            # out early in that case. TODO.md §H.3.
            if xbmc.Monitor().waitForAbort(0.25):
                return None
        return None

    def wait_for_segment(self, seg_n, timeout=_HLS_SEGMENT_WAIT_SECONDS):
        """Block until seg_n is complete on disk, or timeout expires.

        If ffmpeg is either not running or running in a position that
        will never produce seg_n, kicks off a restart aimed at seg_n.
        Returns the segment file path on success, or None on timeout.

        For fmp4 producers, the loop additionally gates on
        _init_file_complete so a seg_n read can't race a
        still-being-written init.mp4.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._closed:
                return None
            gate = self._wait_for_segment_init_gate(seg_n)
            if gate == "abort":
                return None
            if gate == "retry":
                continue
            if self._segment_complete(seg_n):
                return self.segment_path(seg_n)
            # Do we need to (re)start ffmpeg to eventually reach seg_n?
            self._ensure_ffmpeg_headed_for(seg_n)
            # Monitor.waitForAbort instead of time.sleep so a Kodi shutdown
            # during HLS segment wait unblocks immediately. TODO.md §H.3.
            if xbmc.Monitor().waitForAbort(0.25):
                return None
        return None

    def _wait_for_segment_init_gate(self, seg_n):
        """fmp4 init gate for wait_for_segment.

        seg_n cannot be served until the current generation's init is
        on disk AND ffmpeg has moved past the init write phase. For
        segment requests we DO want to head toward seg_n specifically —
        the caller asks for a specific segment, so the "seg_n <
        start_segment" restart in _ensure_ffmpeg_headed_for is the right
        call (unlike wait_for_init, which preserves the generation).

        Returns "ok" to proceed to the segment check, "retry" to
        re-enter the wait loop, or "abort" on Kodi shutdown.
        """
        if self.segment_format != "fmp4" or self._init_ready:
            return "ok"
        if self._init_file_complete():
            self._init_ready = True
            return "ok"
        self._ensure_ffmpeg_headed_for(seg_n)
        if xbmc.Monitor().waitForAbort(0.25):
            return "abort"
        return "retry"

    def _ensure_ffmpeg_headed_for(self, seg_n):
        """Start or restart ffmpeg so that it will produce seg_n.

        If ffmpeg is already running and its start segment is <= seg_n
        (i.e. the live process will eventually reach this segment as
        it streams forward), do nothing.

        Otherwise — ffmpeg is dead, or started at a segment index
        greater than seg_n (seek backward), or far before seg_n (seek
        far forward) — kill the current ffmpeg and start a new one
        whose ``-ss`` matches seg_n.
        """
        with self._lock:
            if self._closed:
                return
            if not self._needs_ffmpeg_restart(seg_n):
                return
            self._stop_old_ffmpeg()
            self._fmp4_generation_boundary(seg_n)
            self._spawn_ffmpeg_at(seg_n)

    def _needs_ffmpeg_restart(self, seg_n):
        """Decide whether ffmpeg must be (re)started to reach seg_n.

        MUST be called with self._lock held. ffmpeg only produces
        segments >= start_segment in sequence; a request before that
        means a backward seek, and a request far ahead means a forward
        seek beyond the near-future buffer window — both restart.
        """
        proc = self._proc
        proc_alive = proc is not None and proc.poll() is None
        if not proc_alive:
            return True
        if seg_n < self._start_segment:
            return True
        if seg_n - self._start_segment > _HLS_FORWARD_WAIT_SEGMENTS:
            return True
        return False

    def _stop_old_ffmpeg(self):
        """Kill + reap the current ffmpeg, then clear self._proc.

        MUST be called with self._lock held. 2s wait (was 5s):
        concurrency audit flagged this as the worst-case hold time on
        the HlsProducer lock, which blocks every concurrent
        wait_for_segment / wait_for_init / close() call. 2s is enough
        for SIGKILL to land on a healthy child; on a genuinely stuck
        one we log + let the OS reap rather than stalling the session.
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                xbmc.log(
                    "NZB-DAV: HLS ffmpeg pid={} did not exit 2 s after kill; "
                    "leaking for the OS to reap".format(getattr(proc, "pid", "?")),
                    xbmc.LOGWARNING,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        self._proc = None

    def _fmp4_generation_boundary(self, seg_n):
        """Mark the fmp4 generation boundary before a respawn.

        MUST be called with self._lock held. Unlink the new target
        segment file so the "seg_<start_segment>.m4s exists"
        completeness signal in _init_file_complete is unambiguously
        bound to the NEW ffmpeg. Do NOT blanket-sweep other segments —
        leaving prior-generation files in place preserves the
        backward-seek cache optimization in _segment_complete. Do NOT
        unlink init.mp4 either: the canonical bytes cache already
        committed to serving the first generation's init to every Kodi
        request, so whatever new ffmpeg writes to the on-disk init.mp4
        is irrelevant. Unlinking would just race the on-disk overwrite
        and momentarily fail _init_file_complete for no gain.
        """
        if self.segment_format != "fmp4":
            return
        first_seg_path = os.path.join(self.session_dir, "seg_{:06d}.m4s".format(seg_n))
        try:
            os.unlink(first_seg_path)
        except FileNotFoundError:
            pass
        # Reset _init_ready so wait_for_init/wait_for_segment re-verify
        # the generation boundary (checks that seg_<new_target>.m4s
        # exists post-spawn) — but the canonical init bytes persist.
        self._init_ready = False

    def _spawn_ffmpeg_at(self, seg_n):
        """Spawn a new ffmpeg aimed at seg_n. Lock MUST be held."""
        start_time = seg_n * self.segment_seconds
        cmd = self._build_cmd(start_time, seg_n)
        xbmc.log(
            "NZB-DAV: HLS producer starting ffmpeg at seg {} (t={:.1f}s)".format(
                seg_n, start_time
            ),
            xbmc.LOGINFO,
        )
        try:
            # Set _spawn_time + _start_segment BEFORE Popen so a
            # concurrent _segment_complete() can't observe a stale
            # _spawn_time of 0 (which would accept a freshly-unlinked
            # prior-generation segment as complete). The tiny skew
            # before the actual spawn is harmless for that guard.
            self._start_segment = seg_n
            self._spawn_time = time.time()
            # Reopen the log if close() (or any caller) closed it, so the
            # new ffmpeg doesn't inherit a closed fd and swallow stderr.
            if self._ffmpeg_log.closed:
                self._ffmpeg_log = open(  # noqa: SIM115 — closed in close()
                    self._ffmpeg_log_path, "ab", buffering=0
                )
            # cwd=session_dir is REQUIRED for fmp4: ffmpeg 6.0.1 on
            # CoreELEC rejects absolute paths for -hls_fmp4_init_filename,
            # so _build_cmd passes relative filenames and relies on cwd.
            # mpegts passes absolute paths and tolerates either cwd, so
            # setting cwd unconditionally is safe. stdin=DEVNULL keeps the
            # child off the parent stdin (TODO.md §H.3 Low).
            self._proc = subprocess.Popen(  # nosec B603 — argv list, shell=False
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=self._ffmpeg_log,
                shell=False,
                cwd=self.session_dir,
            )
        except OSError as e:
            xbmc.log(
                "NZB-DAV: HLS producer ffmpeg spawn failed: {}".format(e),
                xbmc.LOGERROR,
            )
            self._proc = None

    def _build_cmd(self, start_time, start_segment):
        """Build the persistent-ffmpeg command.

        Two output shapes, driven by self.segment_format:

        - "mpegts" (default, legacy): ``-f segment -segment_format mpegts``
          writes ``seg_%06d.ts`` directly via ffmpeg's segment muxer.
        - "fmp4" (new): ``-f hls -hls_segment_type fmp4`` writes
          ``init.mp4`` (once per process start) plus ``seg_%06d.m4s``
          fragments. This is the DV-capable branch — DV RPU SEI NALs
          survive fmp4 fragment boundaries (vs mpegts PES packetization,
          which breaks them).

        Filename padding: both branches use ``seg_%06d.<ext>`` so the
        existing producer tests that construct segment files by name
        (``seg_000005.ts``, etc.) continue to work, and the URL parser's
        ``int()`` coercion absorbs leading zeros either way.

        Timestamp handling: ``-copyts`` is set so each output frame
        keeps the source PTS. No ``-reset_timestamps`` — an earlier
        attempt used ``-reset_timestamps 1`` to normalize each
        segment's PTS to near-zero, but Kodi's Amlogic HW decoder
        interpreted the repeated near-zero PTS values as
        non-monotonic, flagged ``messy timestamps``, and eventually
        emitted a continuous stream of ``CAMLCodec::GetPicture:
        decoder timeout - elf:[5021ms]`` errors until playback froze
        (seen on the 2026-04-13 Shawshank test run). With ``-copyts``
        and default timestamp continuity, a single running ffmpeg
        emits seg 0 at PTS 0-30, seg 1 at PTS 30-60, ... — perfectly
        monotonic. On seek-restart, the new ffmpeg's ``-ss T`` gives
        first-frame PTS near T, matching Kodi's EXTINF-based global
        time at ``seg_T/segment_seconds``. The per-segment keyframe-
        snap overlap that bit us with the earlier fresh-ffmpeg-per-
        segment design doesn't apply here: adjacent segments come
        from the SAME ffmpeg process in the persistent model, so
        only the seek boundary has any chance of overlap — and at a
        seek Kodi expects a discontinuity anyway.
        """
        cmd = self._build_base_input_args(start_time)
        if self.segment_format == "fmp4":
            self._append_fmp4_output_args(cmd, start_segment)
        else:
            self._append_mpegts_output_args(cmd, start_segment)
        return cmd

    def _append_mpegts_output_args(self, cmd, start_segment):
        """Append the legacy mpegts segment-muxer output args (unchanged)."""
        # mpegts branch — unchanged filename pattern.
        seg_pattern = os.path.join(self.session_dir, "seg_%06d.ts")
        cmd.extend(
            [
                "-f",
                "segment",
                "-segment_format",
                "mpegts",
                "-segment_time",
                "{:.3f}".format(self.segment_seconds),
                "-segment_start_number",
                str(start_segment),
                seg_pattern,
            ]
        )

    def _build_base_input_args(self, start_time):
        """Build the shared ffmpeg input args (auth + map + copy) for _build_cmd."""
        _validate_url(self.remote_url)
        # Pass auth via -headers (not URL-embedded) so credentials
        # don't leak into argv / ffmpeg.log / error messages. See
        # _ffmpeg_auth_args for the rationale.
        input_url = self.remote_url
        auth_args = _ffmpeg_auth_args(self.auth_header)

        # -probesize / -analyzeduration: ffmpeg needs to read enough
        # input bytes AND enough media duration to determine codec
        # parameters before muxing starts. The original (1 MB / 0)
        # skipped analysis entirely, which broke audio frame-size
        # detection: ffmpeg logged "track N: codec frame size is
        # not set" and the mp4 muxer fell back to a default
        # per-packet duration that didn't match reality, producing
        # AV desync on DTS/TrueHD AND outright "no audio" on
        # E-AC-3 (DDP) sources.
        #
        # The first bump to 5 MB / 2 s helped DTS slightly but
        # didn't catch E-AC-3 in a sparsely-interleaved MKV — 2 s
        # of media time covers only a handful of audio packets in
        # a 4K REMUX where audio is interleaved between large
        # video keyframes. Bumping to 50 MB / 15 s gives ffmpeg a
        # comfortable margin to read dozens of audio packets and
        # determine the codec frame size for any practical source.
        # Costs ~3-5 s of extra startup latency on first spawn
        # (and on every seek respawn) — the playback-never-started
        # watchdog in service.py was raised to 30 s for exactly
        # this reason.
        cmd = [
            self.ffmpeg_path,
            "-v",
            "warning",
            "-probesize",
            "52428800",
            "-analyzeduration",
            "15000000",
            "-fflags",
            "+fastseek",
            "-ss",
            "{:.3f}".format(start_time),
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
        ]
        self._append_input_map_args(cmd, auth_args, input_url)
        return cmd

    @staticmethod
    def _append_input_map_args(cmd, auth_args, input_url):
        """Append auth headers + ``-i`` input and the v/a copy mapping."""
        # Auth headers MUST come before -i so they apply to the input.
        cmd.extend(auth_args)
        cmd.extend(
            [
                "-i",
                input_url,
                "-map",
                "0:v:0",
                "-map",
                "0:a",
                "-c:v",
                "copy",
                "-c:a",
                "copy",
                "-sn",
                "-copyts",
            ]
        )

    def _append_fmp4_output_args(self, cmd, start_segment):
        """Append the fmp4 HLS output flags to ``cmd`` in their exact order."""
        # IMPORTANT: fmp4 arguments must be RELATIVE filenames, not
        # absolute paths. ffmpeg 6.0.1 on CoreELEC fails on absolute
        # paths for ``-hls_fmp4_init_filename`` with "Failed to open
        # segment <path>: No such file or directory", even when the
        # parent directory exists and is writable. Relative names
        # work reliably when ffmpeg is spawned with cwd set to the
        # session dir (see ``_ensure_ffmpeg_headed_for``'s ``Popen``
        # call). Reproduced 2026-04-14 on a 48 GB DV HEVC REMUX
        # and a 27 GB AVC REMUX; both failed with absolute paths,
        # both succeeded with relative.
        init_path = "init.mp4"
        seg_pattern = "seg_%06d.m4s"
        playlist_path = "ffmpeg_playlist.m3u8"
        self._append_fmp4_stability_args(cmd)
        cmd.extend(
            [
                "-f",
                "hls",
                "-hls_time",
                "{:.3f}".format(self.segment_seconds),
                "-hls_segment_type",
                "fmp4",
                "-hls_fmp4_init_filename",
                init_path,
                "-hls_segment_filename",
                seg_pattern,
                "-hls_playlist_type",
                "vod",
                "-hls_flags",
                "independent_segments+omit_endlist",
                "-start_number",
                str(start_segment),
                playlist_path,
            ]
        )

    @staticmethod
    def _append_fmp4_strict_args(cmd):
        """Append ``-strict -2`` so the fMP4 muxer accepts TrueHD/DTS-HD MA."""
        # -strict -2 (== -strict experimental) unlocks TrueHD and
        # DTS-HD MA output in the MP4/fMP4 muxer. ffmpeg 6.0.1
        # otherwise refuses with "truehd in MP4 support is
        # experimental, add '-strict -2' if you want to use it"
        # / "dts in MP4 support is experimental, ..." and fails
        # to write the init header at all. Virtually every UHD
        # REMUX uses one of those codecs, so without this flag
        # the fmp4 HLS path never produces a playable output
        # on real content. Verified 2026-04-14 against The
        # Machinist (TrueHD) — failed without -strict, succeeded
        # with it.
        cmd.extend(["-strict", "-2"])

    @staticmethod
    def _append_fmp4_stability_args(cmd):
        """Append the fmp4 codec/timestamp/movflags/tag flags in exact order."""
        HlsProducer._append_fmp4_strict_args(cmd)
        # Timestamp and fragment flags for seek-respawn stability:
        # -start_at_zero pairs with -copyts so seeked output starts from
        # a deterministic timeline, while avoid_negative_ts prevents
        # pre-roll from surfacing as negative fragment timestamps.
        # bitexact strips volatile muxer metadata, and the CMAF-style
        # movflags keep fragments self-relative across respawns. Do not
        # enable hls delete_segments here; this proxy owns segment
        # retention and may serve recently-produced files during a
        # reconnect or backward seek.
        cmd.extend(
            [
                "-start_at_zero",
                "-avoid_negative_ts",
                "make_zero",
                "-fflags",
                "+bitexact+flush_packets",
                "-flags",
                "+bitexact",
            ]
        )
        cmd.extend(
            [
                "-movflags",
                "+frag_custom+dash+delay_moov+separate_moof"
                "+default_base_moof+omit_tfhd_offset",
            ]
        )
        # Force the HLS-spec sample entry tag on the video track.
        # fMP4 HLS mandates ``hvc1`` for HEVC (parameter sets in the
        # sample description box, not inband), and Amlogic's HLS
        # demuxer looks at ``hvc1``/``hev1`` to decide whether to
        # inspect the ``dvcC``/``dvvC`` DV configuration records in
        # the init segment. ``-tag:v hvc1`` is a metadata swap,
        # not a re-encode; ffmpeg pulls SPS/PPS/VPS into ``hvcC``
        # at the muxer and leaves the bitstream otherwise
        # untouched.
        cmd.extend(["-tag:v", "hvc1"])

    # How long prepare() will wait for ffmpeg to actually produce
    # init.mp4 + the first segment before declaring the fmp4 path
    # broken and falling back to matroska. Has to comfortably exceed
    # ffmpeg's analyzeduration (15 s) plus header write time, plus a
    # safety margin for slow upstream reads. 30 s is the smallest
    # value that doesn't false-trip on a healthy 50 Mbps WEB-DL.
    _PREPARE_PRODUCTION_TIMEOUT_SECONDS = 30.0

    def prepare(self):
        """Eagerly spawn ffmpeg AND wait for it to actually produce
        init.mp4 + first segment before returning.

        Called from _register_session right after construction. For
        mpegts producers (the legacy lazy path) this is a no-op.
        For fmp4 producers this is the spawn-time validation that
        keeps the matroska late-binding fallback working — without
        it, ffmpeg's first spawn happens inside wait_for_init AFTER
        the HLS URL has already been returned to Kodi.

        Two failure-detection windows in sequence:

        1. **Argument rejection (~500 ms).** Catches "ffmpeg argv
           is wrong" failures: missing muxer, bad option, refused
           experimental codec, build mismatch, etc. ffmpeg exits
           with non-zero rc within ~10-100 ms in practice.

        2. **Production failure (up to _PREPARE_PRODUCTION_TIMEOUT
           _SECONDS).** Catches "ffmpeg started but never produced
           anything" failures: absolute path bug (a547a2d), -strict
           -2 missing (b8f09d6), analysis hang (1a56c36), and any
           future ffmpeg/source combo where output stalls after
           launch. Polls for init.mp4 + seg_000000.m4s on disk.
           If neither is on disk by the deadline, OR if ffmpeg has
           exited with non-zero rc in the meantime, raises so
           _register_session rewrites ctx to the matroska shape.

        Both checks must pass before prepare() returns successfully.
        Costs up to 30 s of latency on the first spawn for healthy
        sessions (typical: 2-5 s). That's the right tradeoff vs
        handing Kodi a URL that will never play — and the
        playback-never-started watchdog in service.py was raised
        to 30 s for exactly this latency budget.

        Raises:
            RuntimeError: ffmpeg failed to spawn, exited early, or
                produced no output within the production timeout.
        """
        if self.segment_format != "fmp4":
            return  # mpegts is lazy-spawned, no eager validation
        self._ensure_ffmpeg_headed_for(0)
        init_path = os.path.join(self.session_dir, "init.mp4")
        first_seg_path = os.path.join(self.session_dir, "seg_000000.m4s")

        ready, early_exit = self._prepare_argv_window(init_path, first_seg_path)
        if ready:
            return  # healthy — both files are on disk
        self._prepare_production_window(init_path, first_seg_path, early_exit)

    @staticmethod
    def _prepare_outputs_present(init_path, first_seg_path):
        """True if both prepare() output files are on disk."""
        return os.path.exists(init_path) and os.path.exists(first_seg_path)

    def _prepare_argv_window(self, init_path, first_seg_path):
        """Window 1: argument-rejection poll (500 ms).

        An early exit with rc != 0 is a hard failure (bad argv, missing
        muxer, refused experimental codec). An early exit with rc == 0
        is a SUCCESSFUL completion — possible when the source is shorter
        than 500 ms of stream-copy work (the synthetic test MKV). Either
        way, on early exit we drop straight to the production check.

        Returns (ready, early_exit): ready=True if both output files
        are already on disk (caller returns); early_exit tracks whether
        ffmpeg has already exited cleanly.
        """
        argv_deadline = time.monotonic() + 0.5
        while time.monotonic() < argv_deadline:
            with self._lock:
                proc = self._proc
            if proc is None:
                raise RuntimeError("ffmpeg failed to spawn — check ffmpeg.log")
            rc = proc.poll()
            if rc is not None:
                if rc != 0:
                    raise RuntimeError(
                        "ffmpeg exited immediately with code {} — fmp4 "
                        "HLS likely unsupported by this build".format(rc)
                    )
                return False, True
            if self._prepare_outputs_present(init_path, first_seg_path):
                xbmc.log(
                    "NZB-DAV: HlsProducer.prepare confirmed init.mp4 "
                    "and seg_000000.m4s on disk during argv window",
                    xbmc.LOGINFO,
                )
                return True, False
            # Monitor.waitForAbort instead of bare time.sleep so a Kodi
            # shutdown during HLS warmup unblocks the prepare argv-loop
            # immediately. TODO.md §H.3.
            if xbmc.Monitor().waitForAbort(0.05):
                raise RuntimeError("Kodi abort requested during HLS prepare")
        return False, False

    def _prepare_production_window(self, init_path, first_seg_path, early_exit):
        """Window 2: wait for actual output production.

        Polls the file system for init.mp4 + the first segment, AND
        watches ffmpeg liveness so a late crash surfaces immediately.
        If ffmpeg already exited cleanly in window 1 (early_exit), the
        output files should already exist; verify once instead of
        waiting. Raises RuntimeError on timeout or failure.
        """
        prod_deadline = time.monotonic() + self._PREPARE_PRODUCTION_TIMEOUT_SECONDS
        while time.monotonic() < prod_deadline:
            if self._prepare_outputs_present(init_path, first_seg_path):
                xbmc.log(
                    "NZB-DAV: HlsProducer.prepare confirmed init.mp4 "
                    "and seg_000000.m4s on disk",
                    xbmc.LOGINFO,
                )
                return  # healthy — both files are on disk
            if early_exit:
                # ffmpeg already finished; if the files aren't here,
                # they're never going to be. Fail immediately
                # instead of waiting for the full deadline.
                raise RuntimeError(
                    "ffmpeg exited cleanly but produced no init.mp4 / "
                    "seg_000000.m4s — check ffmpeg.log"
                )
            if self._prepare_ffmpeg_exited_clean():
                # ffmpeg exited mid-window with rc==0 — the source was
                # short enough to finish during the production wait.
                # Give the file-existence check one more iteration
                # before declaring failure.
                early_exit = True
                continue
            if xbmc.Monitor().waitForAbort(0.25):
                raise RuntimeError("Kodi abort requested during HLS prepare")
        raise RuntimeError(
            "ffmpeg did not produce init.mp4 + seg_000000.m4s within "
            "{:.0f}s — check ffmpeg.log".format(
                self._PREPARE_PRODUCTION_TIMEOUT_SECONDS
            )
        )

    def _prepare_ffmpeg_exited_clean(self):
        """Inspect ffmpeg liveness during the production window.

        Returns True iff ffmpeg has exited with rc==0 (caller should
        re-verify output files). Raises RuntimeError if ffmpeg
        disappeared or exited with a non-zero code. Returns False while
        ffmpeg is still running.
        """
        with self._lock:
            proc = self._proc
        if proc is None:
            raise RuntimeError("ffmpeg disappeared during prepare — check ffmpeg.log")
        rc = proc.poll()
        if rc is None:
            return False
        if rc != 0:
            raise RuntimeError(
                "ffmpeg exited with code {} before producing output "
                "— check ffmpeg.log".format(rc)
            )
        return True

    def _finish_close_after_kill(self, proc, wait_for_proc):
        """Finish HLS cleanup after close() has signaled ffmpeg to stop."""
        if wait_for_proc:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                xbmc.log(
                    "NZB-DAV: HlsProducer.close: ffmpeg pid={} did not exit "
                    "5 s after kill; leaking for the OS to reap".format(
                        getattr(proc, "pid", "?")
                    ),
                    xbmc.LOGWARNING,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            self._ffmpeg_log.close()
        except OSError:
            pass
        # Persist the session's ffmpeg.log to a stable rolling
        # location BEFORE the session dir is deleted. Otherwise
        # every "playback failed" debug session has to chase a
        # log that no longer exists — which has bitten us several
        # times already on the fmp4 spike. Keep the most recent
        # 10 logs, named by session_id so they're easy to
        # cross-reference with the kodi.log "session_id=..." lines.
        try:
            self._archive_ffmpeg_log()
        except Exception as e:  # pylint: disable=broad-except
            # _archive_ffmpeg_log's whole purpose is preserving the
            # session log for post-mortem debugging. Swallowing its
            # own failure silently defeats that goal — log at debug
            # so the user can diagnose "why isn't my ffmpeg.log
            # archived?" when it matters.
            xbmc.log(
                "NZB-DAV: Failed to archive ffmpeg.log for session {}: {}".format(
                    getattr(self, "session_dir", "?"), e
                ),
                xbmc.LOGDEBUG,
            )
        try:
            import shutil as _shutil

            _shutil.rmtree(self.session_dir, ignore_errors=True)
        except OSError:
            pass

    def _finish_close_after_kill_in_background(self, proc, wait_for_proc):
        thread = threading.Thread(
            target=self._finish_close_after_kill,
            args=(proc, wait_for_proc),
            name="nzbdav-old-hls-close",
        )
        thread.daemon = True
        try:
            thread.start()
        except RuntimeError:
            self._finish_close_after_kill(proc, wait_for_proc)

    def close(self, wait_for_process=True):
        """Kill ffmpeg and delete the session directory."""
        with self._lock:
            self._closed = True
            proc = self._proc
            self._proc = None
        wait_for_proc = False
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except (OSError, subprocess.SubprocessError):
                pass
            else:
                wait_for_proc = True
        if wait_for_process:
            self._finish_close_after_kill(proc, wait_for_proc)
        else:
            self._finish_close_after_kill_in_background(proc, wait_for_proc)

    @staticmethod
    def _resolve_ffmpeg_log_archive_dir():
        """Pick the archive directory for session ffmpeg logs.

        Prefers Kodi's ``special://temp/nzbdav-hls-logs/`` but only when
        translatePath yields a genuine string (in tests xbmcvfs is mocked
        and returns a MagicMock, which would leak a "MagicMock" dir in
        cwd). Falls back to the system temp dir. Returns the created
        directory path, or None if it could not be created.
        """
        archive_dir = None
        try:
            import xbmcvfs

            candidate = xbmcvfs.translatePath("special://temp/nzbdav-hls-logs/")
            if isinstance(candidate, str):
                archive_dir = candidate
        except Exception:  # pylint: disable=broad-except
            pass
        if not archive_dir:
            archive_dir = os.path.join(tempfile.gettempdir(), "nzbdav-hls-logs")
        try:
            os.makedirs(archive_dir, exist_ok=True)
        except OSError:
            return None
        return archive_dir

    @staticmethod
    def _trim_archived_ffmpeg_logs(archive_dir):
        """Keep only the 10 most recent ``ffmpeg-*.log`` files in dir."""
        try:
            entries = []
            for name in os.listdir(archive_dir):
                if not name.startswith("ffmpeg-") or not name.endswith(".log"):
                    continue
                full = os.path.join(archive_dir, name)
                try:
                    entries.append((os.path.getmtime(full), full))
                except OSError:
                    continue
            entries.sort(reverse=True)
            for _, path in entries[10:]:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        except OSError:
            pass

    def _archive_ffmpeg_log(self):
        """Copy the session's ffmpeg.log to /storage/.kodi/temp/
        nzbdav-hls-logs/ and trim to the most recent 10."""
        import shutil as _shutil

        src = self._ffmpeg_log_path
        if not os.path.exists(src):
            return
        try:
            size = os.path.getsize(src)
        except OSError:
            return
        if size == 0:
            return  # empty log — nothing useful to preserve

        archive_dir = self._resolve_ffmpeg_log_archive_dir()
        if not archive_dir:
            return

        session_id = os.path.basename(self.session_dir)
        dst = os.path.join(archive_dir, "ffmpeg-{}.log".format(session_id))
        try:
            _shutil.copy2(src, dst)
        except OSError:
            return

        self._trim_archived_ffmpeg_logs(archive_dir)

        xbmc.log(
            "NZB-DAV: Archived session ffmpeg.log to {}".format(dst),
            xbmc.LOGINFO,
        )


class StreamProxy:
    """Local HTTP proxy server for nzbdav streams."""

    def __init__(self):
        self._server = None
        self._thread = None
        self.port = 0
        self._context_lock = threading.RLock()
        self._prepare_lock = threading.RLock()
        self.prepare_token = uuid.uuid4().hex
        self._ffmpeg_capabilities = None

    def start(self):
        """Start the proxy server on a random port."""
        self._server = _ThreadedHTTPServer(("127.0.0.1", 0), _StreamHandler)
        self._server.owner_proxy = self
        self._server.prepare_token = self.prepare_token
        self.port = self._server.server_address[1]
        self._refresh_ffmpeg_capabilities()
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.daemon = True
        self._thread.start()
        xbmc.log(
            "NZB-DAV: Stream proxy started on port {}".format(self.port),
            xbmc.LOGINFO,
        )

    def is_alive(self):
        """True iff the proxy's HTTP server thread is still serving.

        Returns False when either:
        - The proxy hasn't been started yet (``_thread`` is None).
        - The serve_forever thread has exited for any reason —
          normal stop(), or an unhandled exception in the socket
          accept loop (rare but has happened historically on
          memory-pressure paths).

        The service loop polls this once a second and restarts the
        proxy when it drops to False so a crashed listener doesn't
        silently wedge every subsequent /prepare call.
        """
        thread = self._thread
        if thread is None:
            return False
        return thread.is_alive()

    def stop(self):
        """Stop the proxy server.

        ``TCPServer.shutdown()`` only signals ``serve_forever`` to exit;
        it does NOT close the listening socket. Pair it with
        ``server_close()`` so the socket file descriptor is released
        immediately. Otherwise the listener lingers until Python's GC
        runs, which surfaces as ``ResourceWarning: unclosed socket`` in
        the test suite and (more importantly) holds the port across a
        rapid stop/start cycle on the real service.
        """
        self.clear_sessions()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def clear_sessions(self, wait_for_process=True):
        """Tear down every registered session and kill its ffmpeg process.

        Called from:
        - stop() on service shutdown
        - prepare_stream() on each new play, so a zombie ffmpeg from a
          previous stream that Kodi abandoned without firing onPlayBackStopped
          (e.g. DB-vacuum stall that freezes the decoder) doesn't keep
          writing into a half-dead TCP socket forever
        - NzbdavPlayer stop/end hooks for clean-stop cases
        """
        if not self._server:
            return
        with self._context_lock:
            sessions = list(getattr(self._server, "stream_sessions", {}).values())
            pending = list(
                getattr(self._server, "pending_stream_contexts", {}).values()
            )
            self._server.stream_sessions = {}
            self._server.pending_stream_contexts = {}
            self._server.stream_context = None
            self._server.active_ffmpeg = None
        seen = set()
        for ctx in sessions + pending:
            marker = id(ctx)
            if marker in seen:
                continue
            seen.add(marker)
            self._cleanup_session_or_defer(ctx, wait_for_process=wait_for_process)

    def cleanup_session_by_id(self, session_id):
        """Tear down a single session by id, cleanup outside the lock.

        Used by the /prepare write-failure path when the plugin client
        disconnects before the response is delivered (e.g. 60 s urlopen
        timeout firing during a slow tempfile-faststart remux). Without
        this, the session lingers until the next ``prepare_stream`` call
        runs ``clear_sessions``, or the 6-hour TTL prune fires —
        meaning a tempfile from a give-up'd play could occupy disk for
        hours. Closes TODO.md §H.2-H12.
        """
        if not self._server or not session_id:
            return
        with self._context_lock:
            sessions = getattr(self._server, "stream_sessions", {})
            ctx = sessions.pop(session_id, None)
            if ctx is not None and getattr(self._server, "stream_context", None) is ctx:
                self._server.stream_context = None
        if ctx is not None:
            self._cleanup_session_or_defer(ctx)

    def _cleanup_session_or_defer(self, ctx, wait_for_process=True):
        """Cleanup now, or wait until in-flight handlers release the ctx."""
        with self._context_lock:
            if ctx.get("_cleanup_started"):
                return
            if int(ctx.get("_active_handlers", 0) or 0) > 0:
                ctx["_cleanup_pending"] = True
                return
            ctx["_cleanup_started"] = True
        self._cleanup_session(ctx, wait_for_process=wait_for_process)

    @staticmethod
    def _try_faststart_layout(remote_url, content_length, auth_header):
        """Attempt virtual moov-relocation for an MP4.

        Returns the faststart dict, or None on failure.
        """
        try:
            if fetch_remote_mp4_layout is None:
                raise ImportError("mp4_parser not available")
            layout_info = fetch_remote_mp4_layout(
                remote_url, content_length, auth_header
            )
            if layout_info:
                xbmc.log(
                    "NZB-DAV: MP4 layout: moov_before_mdat={}, moov={}B".format(
                        layout_info.get("moov_before_mdat"),
                        len(layout_info.get("moov_data", b"")),
                    ),
                    xbmc.LOGINFO,
                )
                faststart = build_faststart_layout(layout_info)
                if faststart is None:
                    xbmc.log(
                        "NZB-DAV: stco overflow — moov relocation failed "
                        "(file >4GB with 32-bit chunk offsets)",
                        xbmc.LOGWARNING,
                    )
                return faststart
            xbmc.log("NZB-DAV: MP4 layout fetch returned None", xbmc.LOGWARNING)
            return None
        except _PARSE_ERRORS as e:
            xbmc.log(
                "NZB-DAV: MP4 faststart parse failed: {}".format(e), xbmc.LOGWARNING
            )
            return None

    @staticmethod
    def _wait_for_active_ffmpeg(active_ffmpeg):
        try:
            active_ffmpeg.wait(timeout=5)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    @staticmethod
    def _wait_for_active_ffmpeg_in_background(active_ffmpeg):
        thread = threading.Thread(
            target=StreamProxy._wait_for_active_ffmpeg,
            args=(active_ffmpeg,),
            name="nzbdav-old-ffmpeg-reap",
        )
        thread.daemon = True
        thread.start()

    @staticmethod
    def _cleanup_session(ctx, wait_for_process=True):
        """Release resources associated with a stream session."""
        # Signal the read-ahead prefetch daemon to stop (per-session close; the
        # thread is daemon and also aborts on waitForAbort for global shutdown).
        # This is the single sink reached by every close route, so all sessions
        # are covered. Never block teardown on the thread — daemon is the
        # backstop.
        readahead_buffer = ctx.get(_READAHEAD_BUFFER_KEY)
        if readahead_buffer is not None:
            readahead_buffer.stop()

        StreamProxy._kill_session_ffmpeg(ctx, wait_for_process)
        StreamProxy._remove_session_tempfile(ctx)

        hls_producer = ctx.get("hls_producer")
        if hls_producer is not None:
            try:
                hls_producer.close(wait_for_process=wait_for_process)
            except _HLS_CLOSE_ERRORS as e:
                xbmc.log(
                    "NZB-DAV: HLS producer close failed: {}".format(e),
                    xbmc.LOGWARNING,
                )

    @staticmethod
    def _kill_session_ffmpeg(ctx, wait_for_process):
        """Kill the session's ffmpeg process and reap it (sync or background)."""
        active_ffmpeg = ctx.get("active_ffmpeg")
        if not active_ffmpeg:
            return
        try:
            active_ffmpeg.kill()
        except (OSError, subprocess.SubprocessError, ValueError):
            return
        if wait_for_process:
            StreamProxy._wait_for_active_ffmpeg(active_ffmpeg)
        else:
            StreamProxy._wait_for_active_ffmpeg_in_background(active_ffmpeg)

    @staticmethod
    def _remove_session_tempfile(ctx):
        """Best-effort removal of the session's temp faststart file."""
        temp_path = ctx.get("temp_path")
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    @staticmethod
    def _probe_hls_fmp4_capability(ffmpeg_path):
        """Return True when ffmpeg exposes the HLS fMP4 muxer flags we use."""
        if not ffmpeg_path:
            return False
        output = _run_ffmpeg_hls_muxer_probe(ffmpeg_path)
        if output is None:
            return False
        supported = all(marker in output for marker in _FMP4_HLS_CAPABILITY_MARKERS)
        xbmc.log(
            "NZB-DAV: ffmpeg fmp4 HLS capability {} ({})".format(
                "present" if supported else "absent", ffmpeg_path
            ),
            xbmc.LOGINFO if supported else xbmc.LOGWARNING,
        )
        return supported

    def _refresh_ffmpeg_capabilities(self):
        """Discover ffmpeg once so service-start logs show the active muxers."""
        ffmpeg_path = _find_ffmpeg()
        capabilities = {
            "ffmpeg_path": ffmpeg_path,
            "hls_fmp4": self._probe_hls_fmp4_capability(ffmpeg_path),
        }
        self._ffmpeg_capabilities = capabilities
        return capabilities

    def _get_ffmpeg_capabilities(self):
        """Return cached ffmpeg capabilities, probing lazily if needed."""
        capabilities = getattr(self, "_ffmpeg_capabilities", None)
        if isinstance(capabilities, dict):
            return capabilities
        ffmpeg_path = _find_ffmpeg()
        return {
            "ffmpeg_path": ffmpeg_path,
            "hls_fmp4": bool(ffmpeg_path),
        }

    def _assert_context_lock_owned(self):
        """Best-effort debug guard for helpers that require _context_lock."""
        if not __debug__:
            return
        context_lock = getattr(self, "_context_lock", None)
        is_owned = getattr(context_lock, "_is_owned", None)
        if callable(is_owned) and not is_owned():
            raise AssertionError("_prune_sessions_locked requires _context_lock")

    def _add_pending_context(self, session_id, ctx):
        """Register ctx in the server's pending-context map (locked)."""
        with self._context_lock:
            pending = getattr(self._server, "pending_stream_contexts", None)
            if not isinstance(pending, dict):
                pending = {}
                self._server.pending_stream_contexts = pending
            pending[session_id] = ctx

    def _drop_pending_context(self, session_id):
        """Remove session_id from the server's pending-context map (locked)."""
        with self._context_lock:
            pending = getattr(self._server, "pending_stream_contexts", {})
            if isinstance(pending, dict):
                pending.pop(session_id, None)

    @staticmethod
    def _rewrite_ctx_to_matroska(ctx):
        """Rewrite an HLS ctx in-place to the known-good matroska shape.

        ctx already has ffmpeg_path / total_bytes / duration_seconds from
        prepare_stream's fmp4 branch, so _serve_remux has everything it
        needs once the HLS-specific keys are dropped.
        """
        ctx.pop("mode", None)
        ctx.pop("hls_segment_format", None)
        ctx.pop("hls_segment_duration", None)
        ctx.pop("hls_producer", None)
        ctx["content_type"] = "video/x-matroska"
        ctx["seekable"] = (
            ctx.get("duration_seconds") is not None and ctx.get("total_bytes", 0) > 0
        )

    def _register_hls_session(self, ctx, session_id):
        """Spawn the HLS producer for ctx, falling back to matroska.

        Eager spawn-time validation catches ffmpeg builds that reject
        ``-hls_segment_type fmp4`` BEFORE the HLS URL is returned to Kodi,
        so the matroska rewrite fires for the most likely failure mode
        (no-op for mpegts, which spawns lazily). Raises RuntimeError if
        the prepare was cancelled mid-flight.
        """
        self._add_pending_context(session_id, ctx)
        workdir = _choose_hls_workdir(ctx.get("total_bytes", 0) or 0)
        producer = None
        try:
            producer = HlsProducer(ctx, workdir)
            ctx["hls_producer"] = producer
            producer.prepare()
        except Exception as e:  # noqa: BLE001 — fall back either way
            if ctx.get("_cleanup_started"):
                self._drop_pending_context(session_id)
                raise RuntimeError("HLS prepare was cancelled") from e
            xbmc.log(
                "NZB-DAV: HLS producer setup failed ({}), "
                "rewriting session to matroska fallback".format(e),
                xbmc.LOGWARNING,
            )
            # Best-effort cleanup of the partially initialized producer.
            # HlsProducer.__init__ owns disk resources (session_dir,
            # ffmpeg.log) that need close()'ing on the prepare()-failure
            # path; the `producer = None` sentinel protects against
            # AttributeError when the constructor itself raised.
            if producer is not None:
                try:
                    producer.close()
                except Exception:  # noqa: BLE001
                    pass
            self._rewrite_ctx_to_matroska(ctx)
        finally:
            self._drop_pending_context(session_id)
        if ctx.get("_cleanup_started"):
            raise RuntimeError("HLS prepare was cancelled")

    def _register_session(self, ctx):
        """Store a per-stream context and return its unique proxy URL.

        The returned URL shape depends on ``ctx["mode"]``:

        - ``"hls"`` → ``/hls/<session>/playlist.m3u8`` so Kodi's HLS
          demuxer takes over and drives segment fetches.
        - default → ``/stream/<session>`` for the existing faststart /
          temp-faststart / remux / pass-through handlers.

        For HLS sessions, an ``HlsProducer`` is attached to the ctx
        (``ctx["hls_producer"]``) which owns the persistent ffmpeg
        process and the on-disk segment directory.
        """
        session_id = uuid.uuid4().hex
        now = time.time()
        ctx["session_id"] = session_id
        ctx["created_at"] = now
        ctx["last_access"] = now
        ctx["ffmpeg_lock"] = threading.Lock()
        ctx["active_ffmpeg"] = None
        ctx["current_byte_pos"] = 0
        ctx["_active_handlers"] = 0
        ctx["_cleanup_pending"] = False
        ctx["_cleanup_started"] = False

        if ctx.get("mode") == "hls":
            self._register_hls_session(ctx, session_id)

        with self._context_lock:
            if not isinstance(getattr(self._server, "stream_sessions", None), dict):
                self._server.stream_sessions = {}
            self._server.stream_context = ctx
            self._server.stream_sessions[session_id] = ctx
            evicted = self._prune_sessions_locked(keep_session=session_id)
        # Cleanup outside the lock — `_cleanup_session` does proc.kill +
        # proc.wait, which on a stuck ffmpeg can block other lock waiters.
        for evicted_ctx in evicted:
            self._cleanup_session_or_defer(evicted_ctx)

        if ctx.get("mode") == "hls":
            return "http://127.0.0.1:{}/hls/{}/playlist.m3u8".format(
                self.port, session_id
            )
        return "http://127.0.0.1:{}/stream/{}".format(self.port, session_id)

    def _prune_sessions_locked(self, keep_session=None):
        """Drop expired sessions and cap the total number retained.

        Returns the list of evicted ctx dicts so the *caller* can do the
        actual `_cleanup_session(ctx)` work OUTSIDE `_context_lock`. The
        cleanup path runs `proc.kill()` + `proc.wait()` and on a stuck
        ffmpeg can block long enough to wedge any other thread waiting
        on `_context_lock`. Closes TODO.md §H.2-H1e.
        """
        self._assert_context_lock_owned()
        sessions = getattr(self._server, "stream_sessions", {})
        now = time.time()
        evicted = []

        expired = _expired_session_ids(sessions, keep_session, now)
        for session_id in expired:
            ctx = sessions.pop(session_id, None)
            if ctx is not None:
                evicted.append(ctx)

        while len(sessions) > _MAX_STREAM_SESSIONS:
            session_id = _least_recently_used_session(sessions, keep_session)
            if session_id is None:
                break
            ctx = sessions.pop(session_id, None)
            if ctx is not None:
                evicted.append(ctx)

        return evicted

    def _refresh_session_standby_fallbacks(self, ctx):
        """Resolve nzo-only standby fallbacks into usable WebDAV stream URLs.

        Runs the same resolution the failure-time cutover would, but AHEAD of
        the failure, so a primary stall becomes an instant source swap instead
        of a multi-second cold resolve under Kodi's read timeout.
        """
        handler = _StreamHandler.__new__(_StreamHandler)
        handler.server = self._server
        try:
            handler._refresh_standby_fallback_sources(ctx)
        except Exception as exc:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: standby fallback resolve failed: {}".format(exc),
                xbmc.LOGWARNING,
            )

    def _prevalidate_fallback_sources(self, ctx):
        handler = _StreamHandler.__new__(_StreamHandler)
        handler.server = self._server
        try:
            validated = handler._prevalidate_ready_fallback_sources(ctx)
        except Exception as exc:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: Fallback prevalidation failed: {}".format(exc),
                xbmc.LOGWARNING,
            )
            return
        if validated:
            xbmc.log(
                "NZB-DAV: Prevalidated {} fallback stream(s)".format(validated),
                xbmc.LOGINFO,
            )

    def _start_fallback_prevalidation(self, ctx):
        expected_length = _StreamHandler._fallback_expected_content_length(ctx)
        if expected_length <= 0:
            xbmc.log(
                "NZB-DAV: prevalidation skipped (expected_length={})".format(
                    expected_length
                ),
                xbmc.LOGINFO,
            )
            return
        sources = ctx.get("fallback_sources") or []
        pending = [s for s in sources if _fallback_source_needs_prevalidation(s)]
        xbmc.log(
            "NZB-DAV: prevalidation pending sources={}/{} expected_length={}".format(
                len(pending), len(sources), expected_length
            ),
            xbmc.LOGINFO,
        )
        if not pending:
            return

        # Coalesce bursts: fallbacks are pushed one-per-adopted-job, so mark
        # the session dirty and let a single running warmer re-loop to pick up
        # late arrivals, instead of spawning a thread per push.
        ctx["_fallback_prevalidation_dirty"] = True
        if _thread_is_alive(ctx.get("_fallback_prevalidation_thread")):
            return

        def _warm():
            self._run_fallback_prevalidation_warmer(ctx)

        thread = threading.Thread(target=_warm, name="nzbdav-fallback-prevalidate")
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
        if prefetch_thread and prefetch_thread is not threading.current_thread():
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
        end = min(content_length - 1, _UPSTREAM_READ_CHUNK - 1)
        try:
            body = _StreamHandler._fetch_primary_range_bytes(
                ctx["remote_url"],
                ctx.get("auth_header"),
                0,
                end,
                content_length,
            )
        except Exception as exc:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: Initial range prefetch failed: {}".format(exc),
                xbmc.LOGDEBUG,
            )
            return
        if body:
            _StreamHandler._cache_fallback_range(
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
        thread = threading.Thread(
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
                xbmc.log(
                    "NZB-DAV: Read-ahead prefetch loop error: {}".format(exc),
                    xbmc.LOGDEBUG,
                )
                if monitor.waitForAbort(_READAHEAD_ERROR_BACKOFF_SECONDS):
                    return

    @staticmethod
    def _readahead_prefetch_setup(ctx):
        """Validate ctx and build the read-ahead loop inputs.

        Returns ``(buf, monitor, content_length)`` when prefetch should run,
        or None when the ctx is missing a buffer / remote_url / positive
        content_length (the daemon then exits without looping).
        """
        buf = ctx.get(_READAHEAD_BUFFER_KEY) if isinstance(ctx, dict) else None
        if buf is None:
            return None
        try:
            content_length = int(ctx.get("content_length", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not ctx.get("remote_url") or content_length <= 0:
            return None
        return buf, xbmc.Monitor(), content_length

    @staticmethod
    def _readahead_defer_start(buf, monitor):
        """Yield to startup before the first read-ahead fetch; True to abort.

        Lets the byte-0 prefetch and Kodi's first range fetch win nzbdav's
        connection budget before the read-ahead issues its first upstream
        read. Abortable so a shutdown during the defer exits cleanly. Returns
        True when the loop should not start (stop requested or abort).
        """
        return buf.should_stop() or monitor.waitForAbort(_READAHEAD_START_DEFER_SECONDS)

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
            return monitor.waitForAbort(_READAHEAD_THROTTLE_BACKOFF_SECONDS)
        want = min(
            _READAHEAD_FETCH_CHUNK,
            buf.space_remaining(),
            content_length - fetch_offset,
        )
        if want <= 0:
            return monitor.waitForAbort(_READAHEAD_THROTTLE_BACKOFF_SECONDS)
        end = fetch_offset + want - 1
        # Re-read the source each iteration: a live fallback cutover mutates
        # ctx["remote_url"]/["auth_header"] mid-stream, so a once-hoisted local
        # would keep hammering the dead primary and the lead would stop growing
        # from the live source. The buffer is offset-addressed, so bytes from
        # either source are interchangeable for a given offset.
        remote_url = ctx.get("remote_url")
        auth_header = ctx.get("auth_header")
        if not remote_url:
            return monitor.waitForAbort(_READAHEAD_ERROR_BACKOFF_SECONDS)
        body = _StreamHandler._fetch_primary_range_bytes(
            remote_url, auth_header, fetch_offset, end, content_length
        )
        if not body:
            # Best-effort upstream error or awaiting-download: back off and
            # retry. The real serve path owns recovery; do not touch the
            # recovery taxonomy / notifications / counters here.
            return monitor.waitForAbort(_READAHEAD_ERROR_BACKOFF_SECONDS)
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
        settings = _passthrough_runtime_settings(ctx)
        mb = _coerce_nonneg_int(settings.get("readahead_buffer_mb", 0))
        if mb <= 0:
            return
        content_length = _coerce_nonneg_int(ctx.get("content_length", 0))
        if content_length <= 0:
            return
        cap_bytes = mb * 1024 * 1024
        buf = ReadAheadBuffer(cap_bytes, content_length)
        ctx[_READAHEAD_BUFFER_KEY] = buf
        thread = threading.Thread(
            target=self._run_readahead_prefetch,
            args=(ctx,),
            name="nzbdav-readahead",
        )
        thread.daemon = True
        ctx[_READAHEAD_THREAD_KEY] = thread
        try:
            thread.start()
        except RuntimeError:
            ctx.pop(_READAHEAD_THREAD_KEY, None)
            ctx.pop(_READAHEAD_BUFFER_KEY, None)

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
        if content_length <= _TAIL_PREWARM_BYTES + _UPSTREAM_READ_CHUNK:
            return
        # Yield to startup playback: hold the tail read back a short, abortable
        # beat so the byte-0 prefetch and Kodi's first-byte range request win
        # nzbdav's connection budget first. Without this the tail read RACED
        # them at prepare time and widened the very mid-startup stall window the
        # prewarm exists to close (transient black-screen). waitForAbort lets a
        # Kodi shutdown / session stop cancel the prewarm cleanly during the
        # defer instead of blocking the daemon thread or wasting a connection.
        if xbmc.Monitor().waitForAbort(_TAIL_PREWARM_DEFER_SECONDS):
            return
        tail_start = content_length - _TAIL_PREWARM_BYTES
        try:
            _StreamHandler._fetch_primary_range_bytes(
                ctx["remote_url"],
                ctx.get("auth_header"),
                tail_start,
                content_length - 1,
                content_length,
            )
        except Exception as exc:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: Tail prewarm failed: {}".format(exc),
                xbmc.LOGDEBUG,
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
        thread = threading.Thread(
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
        if isinstance(ctx.get(_PASSTHROUGH_RUNTIME_SETTINGS_KEY), dict):
            return
        if not self._initial_range_prefetchable(ctx):
            return
        done = threading.Event()
        ctx[_PASSTHROUGH_RUNTIME_SETTINGS_DONE_KEY] = done

        def _worker():
            try:
                ctx[_PASSTHROUGH_RUNTIME_SETTINGS_KEY] = (
                    _read_passthrough_runtime_settings()
                )
            except Exception as exc:  # pylint: disable=broad-except
                ctx[_PASSTHROUGH_RUNTIME_SETTINGS_ERROR_KEY] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=_worker,
            name="nzbdav-passthrough-settings-prefetch",
        )
        thread.daemon = True
        ctx["_passthrough_runtime_settings_thread"] = thread
        try:
            thread.start()
        except RuntimeError:
            ctx.pop("_passthrough_runtime_settings_thread", None)
            ctx.pop(_PASSTHROUGH_RUNTIME_SETTINGS_DONE_KEY, None)

    def merge_session_fallbacks(self, session_id, fallback_sources):
        """Merge late-adopted fallback sources into a live session context.

        Returns the count of NEW sources added, or None when the session is
        unknown (torn down / never existed). Dedups by (nzo_id, stream_url).
        Existing source dicts are preserved BY IDENTITY so in-place
        failed/validated marks the live cutover writes survive a merge, and
        the list is swapped atomically so a concurrent _serve_proxy reader
        never observes a half-mutated list.
        """
        normalized = _normalize_fallback_sources(fallback_sources)

        with self._context_lock:
            sessions = getattr(self._server, "stream_sessions", None)
            if not isinstance(sessions, dict):
                return None
            ctx = sessions.get(session_id)
            if not isinstance(ctx, dict):
                return None
            existing = list(ctx.get("fallback_sources") or [])
            added = _merge_new_fallback_sources(existing, normalized)
            if added:
                ctx["fallback_sources"] = existing
        if added:
            # Warm the freshly-pushed fallbacks in the background (resolve
            # nzo-only standbys + fingerprint) so a later primary failure cuts
            # over instantly instead of cold-resolving under Kodi's timeout.
            self._start_fallback_prevalidation(ctx)
        return added

    @staticmethod
    def _probe_dv_source(remote_url, auth_header, content_length):
        """Run the pure-Python DV source probe, failing safe on crash."""
        try:
            dv_result = probe_dolby_vision_source(
                remote_url,
                auth_header,
                file_size=content_length if content_length else None,
            )
        except Exception as exc:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: DV probe crashed -- failing safe to "
                "matroska: {!r}".format(exc),
                xbmc.LOGWARNING,
            )
            from resources.lib.dv_source import DolbyVisionSourceResult

            dv_result = DolbyVisionSourceResult("dv_unknown", "probe_crashed")
        xbmc.log(
            "NZB-DAV: dv_probe classification={} reason={} "
            "profile={} el_type={}".format(
                dv_result.classification,
                dv_result.reason,
                dv_result.profile,
                dv_result.el_type,
            ),
            xbmc.LOGDEBUG,
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
            xbmc.log(
                "NZB-DAV: dv_route=matroska reason={} "
                "profile={} el_type={}".format(
                    dv_result.reason,
                    dv_result.profile,
                    dv_result.el_type,
                ),
                xbmc.LOGWARNING,
            )
            return False
        if (
            dv_result.classification == "dv_allowed_for_fmp4"
            and dv_result.profile == 7
            and dv_result.el_type == "MEL"
        ):
            xbmc.log(
                "NZB-DAV: dv_route=fmp4 reason={} profile=7 "
                "el_type=MEL (experimental -- metadata-only EL "
                "does not exercise CAMLCodec dual-layer init)".format(dv_result.reason),
                xbmc.LOGINFO,
            )
            return True
        return self._dv_route_tail(dv_result)

    @staticmethod
    def _dv_route_tail(dv_result):
        """Tail of the DV routing matrix: non-P7 fmp4, non_dv, unknown."""
        if dv_result.classification == "dv_allowed_for_fmp4":
            xbmc.log(
                "NZB-DAV: dv_route=matroska reason={} "
                "profile={} (non-P7 DV hangs CAMLCodec "
                "onAVStarted on fmp4 per 2026-04-15 "
                "testing)".format(dv_result.reason, dv_result.profile),
                xbmc.LOGWARNING,
            )
            return False
        if dv_result.classification == "non_dv":
            xbmc.log(
                "NZB-DAV: dv_route=fmp4 reason={}".format(dv_result.reason),
                xbmc.LOGDEBUG,
            )
            return True
        xbmc.log(
            "NZB-DAV: dv_route=matroska reason={} "
            "profile={} el_type={} (unknown DV state -- "
            "failing safe)".format(
                dv_result.reason,
                dv_result.profile,
                dv_result.el_type,
            ),
            xbmc.LOGWARNING,
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
        xbmc.log(
            "NZB-DAV: Fallback streams attached; using pass-through proxy "
            "before MP4 repair or remux rescue tiers",
            xbmc.LOGINFO,
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
            xbmc.log(
                "NZB-DAV: MP4 content length unknown; skipping temp-file faststart",
                xbmc.LOGWARNING,
            )
            return None
        if content_length > _TEMP_FASTSTART_MAX:
            xbmc.log(
                "NZB-DAV: File too large for temp-file faststart "
                "({}B > {}B), skipping to MKV remux".format(
                    content_length, _TEMP_FASTSTART_MAX
                ),
                xbmc.LOGINFO,
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
            temp_size = os.path.getsize(temp_path)
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
            xbmc.log(
                "NZB-DAV: MP4 temp-file faststart ({}B)".format(temp_size),
                xbmc.LOGINFO,
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
        xbmc.log("NZB-DAV: MP4 fallback to MKV remux", xbmc.LOGWARNING)
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
                "range_cache": RangeCache(),
            }
            xbmc.log(
                "NZB-DAV: MP4 faststart proxy (virtual={}B, header={}B)".format(
                    faststart["virtual_size"], len(faststart["header_data"])
                ),
                xbmc.LOGINFO,
            )
            return ctx
        if faststart is not None and faststart.get("already_faststart"):
            xbmc.log(
                "NZB-DAV: MP4 already faststart; using pass-through proxy",
                xbmc.LOGINFO,
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
            xbmc.log(
                "NZB-DAV: ffmpeg lacks required fmp4 HLS flags; "
                "falling back to piped Matroska",
                xbmc.LOGWARNING,
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
            "hls_segment_duration": _HLS_SEGMENT_SECONDS,
            "hls_segment_format": "fmp4",
        }
        xbmc.log(
            "NZB-DAV: Force-remuxing {}B file via fMP4 HLS "
            "(experimental, duration={:.1f}s)".format(content_length, duration),
            xbmc.LOGWARNING,
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
        xbmc.log(
            "NZB-DAV: Force-remuxing large {}B file via piped MKV "
            "(duration={}, threshold={}B)".format(
                content_length,
                "{:.1f}s".format(duration) if duration else "unknown",
                threshold,
            ),
            xbmc.LOGWARNING,
        )
        return ctx

    @staticmethod
    def _decide_force_remux(content_length, content_length_unknown, settings_snapshot):
        """Resolve (threshold, force_mode, needs_remux) for a non-MP4 source."""
        if settings_snapshot:
            threshold = _force_remux_threshold_bytes_from_snapshot(settings_snapshot)
            force_mode = _force_remux_mode_from_snapshot(settings_snapshot)
        else:
            threshold = _get_force_remux_threshold_bytes()
            force_mode = _get_force_remux_mode()
        force_remux_requested = threshold > 0 and force_mode in (
            "matroska",
            "hls_fmp4",
        )
        needs_remux = force_remux_requested and (
            content_length_unknown or content_length >= threshold
        )
        if needs_remux and content_length_unknown:
            xbmc.log(
                "NZB-DAV: Content length unknown; forcing live remux "
                "instead of zero-byte pass-through",
                xbmc.LOGWARNING,
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
            xbmc.log(
                "NZB-DAV: {}B file exceeds remux threshold but no "
                "ffmpeg found — falling back to pass-through, "
                "playback may fail on 32-bit Kodi".format(content_length),
                xbmc.LOGWARNING,
            )
        return {
            "remote_url": remote_url,
            "auth_header": auth_header,
            "content_length": content_length,
            "content_type": content_type,
            "remux": False,
        }

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
        _validate_url(remote_url)
        auth_header = _validate_auth_header(auth_header)
        fallback_sources = _normalize_fallback_sources(fallback_sources)
        content_length_hint = _normalize_content_length_hint(content_length_hint)
        settings_snapshot = normalize_settings_snapshot(settings_snapshot)
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
        _attach_fallback_context_fields(ctx, fallback_sources)
        if settings_snapshot:
            ctx[_PASSTHROUGH_RUNTIME_SETTINGS_KEY] = (
                _passthrough_runtime_settings_from_snapshot(settings_snapshot)
            )
        self._start_passthrough_runtime_settings_prefetch(ctx)
        self._start_initial_range_prefetch(ctx)
        self._start_readahead_prefetch(ctx)
        self._start_tail_prewarm(ctx)
        self._start_fallback_prevalidation(ctx)

        local_url = self._register_session(ctx)
        xbmc.log(
            "NZB-DAV: Proxy ready (remux={}, faststart={}): {}".format(
                ctx.get("remux", False), ctx.get("faststart", False), local_url
            ),
            xbmc.LOGINFO,
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
        _attach_fallback_context_fields(
            stream_info, ctx.get("fallback_sources", fallback_sources)
        )
        telemetry.log_timing(
            "prepare_stream",
            (time.monotonic() - started) * 1000.0,
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
        started = time.monotonic()
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

    @staticmethod
    def _probe_duration(ffmpeg_path, url, auth_header):
        """Probe file duration. Returns seconds or None.

        Two strategies, tried in order:

        1. ``ffprobe -show_entries format=duration`` — the clean path. One
           number on stdout, no stream-probe warnings. This is the only
           reliable approach for files with many subtitle streams: a 30-
           subtitle Blu-ray remux produces a wall of ``Could not find
           codec parameters for stream N (Subtitle: hdmv_pgs_subtitle)``
           warnings from ffmpeg that can trivially push ``Duration:`` past
           any stderr buffer budget before it gets a chance to print.
        2. ``ffmpeg -i <url> -f null -`` parsed out of stderr — the
           fallback path when ffprobe isn't installed. Budget is 64 KB
           (up from the original 8 KB) so the subtitle-warning wall
           doesn't evict the Duration line on pathological inputs.

        Args:
            ffmpeg_path: Path to the ffmpeg binary. Used by the fallback
                path and as the starting point for ffprobe discovery.
            url: Remote HTTP URL to probe.
            auth_header: Optional Basic auth header; passed to the child
                process via ``-headers`` so the input URL stays clean.
        """
        _validate_url(url)
        auth_args = _ffmpeg_auth_args(auth_header)

        ffprobe_path = _find_ffprobe()
        if ffprobe_path:
            result = StreamProxy._probe_duration_ffprobe(
                ffprobe_path, url, auth_args=auth_args
            )
            if result is not None:
                return result

        return StreamProxy._probe_duration_ffmpeg(ffmpeg_path, url, auth_args=auth_args)

    @staticmethod
    def _probe_duration_ffprobe(ffprobe_path, input_url, auth_args=None):
        """Run ffprobe to get duration. Returns seconds or None."""
        try:
            cmd = [
                ffprobe_path,
                "-v",
                "error",
            ]
            if auth_args:
                cmd.extend(auth_args)
            cmd.extend(
                [
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nokey=1:noprint_wrappers=1",
                    input_url,
                ]
            )
            proc = subprocess.Popen(  # nosec B603 — argv list, shell=False
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            try:
                stdout_bytes, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
                xbmc.log("NZB-DAV: ffprobe duration timed out", xbmc.LOGWARNING)
                return None
            if proc.returncode != 0:
                return None
            text = stdout_bytes.decode("utf-8", "replace").strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return None
        except (OSError, subprocess.SubprocessError) as e:
            xbmc.log("NZB-DAV: ffprobe failed: {}".format(e), xbmc.LOGWARNING)
            return None

    @staticmethod
    def _probe_duration_ffmpeg(ffmpeg_path, input_url, auth_args=None):
        """Parse Duration out of ``ffmpeg -i`` stderr. Returns seconds or None.

        Uses the bounded-reader-thread pattern: a daemon thread reads
        stderr line-by-line into a shared buffer and signals an Event
        as soon as ``Duration:`` is matched or the 64 KB byte budget
        is exhausted. The main thread waits on the Event with a
        hard wall-clock deadline of ``_PROBE_DEADLINE_SECONDS`` so a
        stuck ffmpeg (slow upstream, stalled header parse, auth hang)
        can't wedge the probe forever. Either way — match, budget,
        deadline — the ffmpeg process is killed before returning.
        """
        return StreamProxy._probe_ffmpeg_stderr(
            ffmpeg_path,
            input_url,
            _parse_ffmpeg_duration,
            "Duration",
            auth_args=auth_args,
        )

    @staticmethod
    def _probe_ffmpeg_stderr(ffmpeg_path, input_url, parser, label, auth_args=None):
        """Shared body of ``_probe_duration_ffmpeg`` (DV probing now uses
        the pure-Python ``dv_source.probe_dolby_vision_source``, which
        parses real RPU data instead of relying on ffmpeg's stderr).
        Spawns ``ffmpeg -v info -i <url> -f null -`` and runs the parser
        against collected stderr under a bounded reader thread +
        wall-clock deadline.

        ``auth_args`` is an optional list of ffmpeg argv pieces (from
        ``_ffmpeg_auth_args``) that gets inserted before ``-i`` so the
        Authorization header is passed via ``-headers`` instead of
        being spliced into the input URL. Callers that build the URL
        with ``_embed_auth_in_url`` should leave this None; new
        callers should prefer the ``-headers`` form.
        """
        cmd = [ffmpeg_path, "-v", "info"]
        if auth_args:
            cmd.extend(auth_args)
        cmd.extend(["-i", input_url, "-f", "null", "-"])

        proc = StreamProxy._spawn_probe_proc(cmd, label)
        if proc is None:
            return None

        collected = [""]
        done = threading.Event()
        # 64 KB budget: large enough that a 30-subtitle Blu-ray remux's
        # wall of per-stream probe warnings can't push the match line out.
        budget = 65536

        reader = threading.Thread(
            target=StreamProxy._probe_stderr_reader,
            args=(proc, parser, collected, budget, done, label),
            name="nzbdav-probe-reader",
            daemon=True,
        )
        reader.start()

        if not done.wait(timeout=_PROBE_DEADLINE_SECONDS):
            xbmc.log(
                "NZB-DAV: {} probe wall-clock deadline ({}s) exceeded, "
                "killing ffmpeg".format(label, _PROBE_DEADLINE_SECONDS),
                xbmc.LOGWARNING,
            )

        return StreamProxy._finish_probe(proc, reader, parser, collected, budget, label)

    @staticmethod
    def _spawn_probe_proc(cmd, label):
        """Spawn an ffmpeg probe process; log + return None on spawn failure."""
        try:
            return subprocess.Popen(  # nosec B603 — argv list, shell=False
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            xbmc.log(
                "NZB-DAV: {} probe spawn failed: {}".format(label, e),
                xbmc.LOGWARNING,
            )
            return None

    @staticmethod
    def _probe_stderr_reader(proc, parser, collected, budget, done, label):
        """Read ffmpeg stderr into ``collected`` until match/budget/EOF."""
        try:
            for line in proc.stderr:
                collected[0] += line.decode(errors="replace")
                if parser(collected[0]) is not None:
                    return
                if len(collected[0]) > budget:
                    return
        except Exception as exc:  # pylint: disable=broad-except
            # Log the failure mode rather than swallowing silently —
            # a stderr decode error or pipe close that hides a real
            # probe failure used to surface as duration=None with no
            # diagnostic in kodi.log. Closes TODO.md §H.3 silent
            # stderr-reader failure.
            xbmc.log(
                "NZB-DAV: {} probe stderr-reader failed: {}".format(label, exc),
                xbmc.LOGWARNING,
            )
        finally:
            done.set()

    @staticmethod
    def _finish_probe(proc, reader, parser, collected, budget, label):
        """Kill the probe proc, join its reader, and return the parsed result."""
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass

        # Join the reader thread now that the proc is dead — its EOF on
        # stderr is the loop-exit condition, so once kill() takes effect
        # the thread should terminate within milliseconds. Joining here
        # (with a small timeout safety net) prevents probe threads from
        # accumulating in long-running services that probe a lot, e.g. a
        # search session that prepares many candidate streams.
        # daemon=True still covers the pathological case where stderr
        # never EOFs. Closes TODO.md §H.3 probe-reader thread leak.
        reader.join(timeout=2)

        result = parser(collected[0])
        if result is None and len(collected[0]) > budget:
            xbmc.log(
                "NZB-DAV: {} not found in first {}B of ffmpeg output".format(
                    label, budget
                ),
                xbmc.LOGWARNING,
            )
        return result

    @staticmethod
    def _prepare_tempfile_faststart(ffmpeg_path, url, auth_header):
        """Remux MP4 with faststart to a temp file. Returns path or None."""
        if not ffmpeg_path:
            return None

        _validate_url(url)
        fd, temp_path = tempfile.mkstemp(
            prefix="nzbdav_faststart_",
            suffix=".mp4",
        )
        os.close(fd)

        cmd = StreamProxy._build_faststart_cmd(ffmpeg_path, url, auth_header, temp_path)
        return StreamProxy._run_faststart_remux(cmd, temp_path)

    @staticmethod
    def _build_faststart_cmd(ffmpeg_path, url, auth_header, temp_path):
        """Build the temp-file faststart remux ffmpeg argv."""
        auth_args = _ffmpeg_auth_args(auth_header)
        cmd = [
            ffmpeg_path,
            "-v",
            "warning",
            "-y",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
        ]
        if auth_args:
            cmd.extend(auth_args)
        cmd.extend(
            [
                "-i",
                url,
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                temp_path,
            ]
        )
        return cmd

    @staticmethod
    def _run_faststart_remux(cmd, temp_path):
        """Run the faststart remux, returning temp_path on success.

        On any failure (non-zero exit, timeout, subprocess error) the
        partial output is removed and None is returned.
        """
        proc = None
        try:
            xbmc.log("NZB-DAV: Temp-file faststart remux starting", xbmc.LOGINFO)
            proc = subprocess.Popen(  # nosec B603 — argv list, shell=False
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            _, stderr = proc.communicate(timeout=600)  # 10 min timeout
            result = StreamProxy._faststart_remux_result(proc, stderr, temp_path)
            if result is not None:
                return result
            # Fall through to the cleanup block below instead of
            # returning early — the partial output that ffmpeg
            # leaves on a failed remux otherwise leaks until the OS
            # next clears tempdir. Closes TODO.md §H.3 (mkstemp
            # leak on TimeoutExpired / SubprocessError) for the
            # ffmpeg-non-zero-exit case in particular.
        except subprocess.TimeoutExpired as e:
            # communicate() timing out does NOT kill the child. Without
            # an explicit kill + reap, the ffmpeg process orphans and
            # holds the output fd + the inbound HTTP socket, potentially
            # for hours. Kill + drain the pipe before the exception
            # propagates; .communicate() on the killed proc reaps it.
            xbmc.log(
                "NZB-DAV: Temp faststart timed out after 600s; killing ffmpeg "
                "(reason=temp_faststart_timeout)",
                xbmc.LOGWARNING,
            )
            StreamProxy._kill_and_reap_faststart(proc)
            _ = e  # keep linters quiet; exception detail already logged
        except (OSError, subprocess.SubprocessError) as e:
            xbmc.log("NZB-DAV: Temp faststart error: {}".format(e), xbmc.LOGWARNING)
            # Non-timeout subprocess errors usually mean Popen itself
            # failed or communicate() hit a pipe error. Still try to
            # reap the child defensively — it's cheap when the proc
            # already exited and essential when it didn't.
            if proc is not None and proc.poll() is None:
                StreamProxy._kill_and_reap_faststart(proc)
        StreamProxy._remove_temp_file(temp_path)
        return None

    @staticmethod
    def _faststart_remux_result(proc, stderr, temp_path):
        """Evaluate a completed faststart proc.

        Returns temp_path on success (rc==0 and non-empty output), or
        None on failure (after logging the redacted ffmpeg stderr).
        """
        if proc.returncode != 0:
            # ffmpeg error messages routinely echo the full input URL,
            # including embedded basic-auth (legacy callers) and
            # apikey=... query strings. Strip those before they land in
            # kodi.log. Closes TODO.md §H.2-H2b.
            xbmc.log(
                "NZB-DAV: Temp faststart failed: {}".format(
                    _redact_text(stderr.decode(errors="replace")[:300])
                ),
                xbmc.LOGWARNING,
            )
            return None
        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            return temp_path
        return None

    @staticmethod
    def _kill_and_reap_faststart(proc):
        """Kill + drain a faststart ffmpeg child (documented reap idiom)."""
        try:
            proc.kill()
            proc.communicate(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def _remove_temp_file(temp_path):
        """Best-effort removal of a temp file if it exists."""
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass

    @staticmethod
    def _get_content_length(url, auth_header, content_length_hint=None):
        """Get file size via HEAD or range probe."""
        content_length_hint = _normalize_content_length_hint(content_length_hint)
        if content_length_hint > 0:
            confirmed = _probe_content_length_hint(
                url, auth_header, content_length_hint
            )
            if confirmed > 0:
                return confirmed

        req = Request(url, method="HEAD")
        _add_request_headers(req, auth_header)
        try:
            # nosemgrep
            with urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
                req, timeout=10
            ) as resp:
                return int(resp.headers.get("Content-Length", 0))
        except (OSError, ValueError):
            pass
        return _probe_content_length_tail(url, auth_header)

    @staticmethod
    def _detect_content_type(url):
        """Detect content type from URL extension."""
        lower = url.lower()
        if lower.endswith(".mkv"):
            return "video/x-matroska"
        if lower.endswith((".mp4", ".m4v")):
            return "video/mp4"
        if lower.endswith(".avi"):
            return "video/x-msvideo"
        return "video/mp4"


def get_service_proxy_port():
    """Get the proxy port from the background service, or 0 if not running."""
    try:
        import xbmcgui

        home = xbmcgui.Window(10000)
        port_str = home.getProperty("nzbdav.proxy_port")
        return int(port_str) if port_str else 0
    except _KODI_SETTING_ERRORS:
        return 0


def get_service_proxy_token():
    """Get the loopback /prepare token from the background service."""
    try:
        import xbmcgui

        home = xbmcgui.Window(10000)
        return home.getProperty(_PROP_PROXY_TOKEN) or ""
    except _KODI_SETTING_ERRORS:
        return ""


def get_service_proxy_config():
    """Get proxy port and token with a single Kodi Home window read."""
    try:
        import xbmcgui

        home = xbmcgui.Window(10000)
        port_str = home.getProperty("nzbdav.proxy_port")
        service_port = int(port_str) if port_str else 0
        prepare_token = home.getProperty(_PROP_PROXY_TOKEN) if service_port else ""
        return service_port, prepare_token or ""
    except _KODI_SETTING_ERRORS:
        return 0, ""


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
    _parse_ffmpeg_duration,
    _reap_process_async,
    _run_ffmpeg_hls_muxer_probe,
    _workdir_has_free_space,
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
