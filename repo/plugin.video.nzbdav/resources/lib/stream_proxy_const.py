# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Module-level configuration constants for ``stream_proxy``.

Extracted verbatim from ``stream_proxy.py`` (god-file dissolution). These are
the immutable timeouts, byte budgets, regexes, sentinels, and tuples that the
proxy and its ``stream_proxy_*`` sibling mixins read. ``stream_proxy`` re-exports
every name here via an explicit ``from resources.lib.stream_proxy_const import
(...)`` list -- explicit, not ``import *``, so static analysers can follow the
re-export chain -- so ``stream_proxy.<NAME>`` references, the siblings'
``_sp.<NAME>`` reads, the default-arg captures in the handler/producer class
bodies, and test ``@patch`` targets all keep resolving unchanged. The mutable
module state with ``global`` mutators (the ``_proxy`` singleton and the
``_HLS_PRIVATE_TEMP_ROOT`` cache, plus their locks) stays in ``stream_proxy``;
only the true immutable constants live here.
"""

import re
import struct
import subprocess
from http.client import HTTPException

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
