# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""HTTP range-contract classification, density breaker, and fault injection.

Extracted from ``stream_proxy.py`` (Stage 1 decomposition). Groups the
pass-through watchdog gate, upstream read-timeout shim, primary-failure fault
injection, range/header helpers, contract status/range/mismatch classifiers,
and the zero-fill density-breaker window math. All names are re-exported by
``stream_proxy`` so existing references and test patches keep resolving.

Plain constants are imported from ``stream_proxy``; parent helpers and any
monkeypatch target (``xbmc``) are reached at call time via
``_sp.<name>`` so patching keeps working.
"""

import os  # noqa: E402
from collections import deque  # noqa: E402
from urllib.error import HTTPError  # noqa: E402

import resources.lib.stream_proxy as _sp  # noqa: E402
from resources.lib.http_util import HTTP_USER_AGENT  # noqa: E402
from resources.lib.stream_proxy import (  # noqa: E402
    _DENSITY_BREAKER_WINDOW_BYTES,
    _DENSITY_BREAKER_ZERO_FILL_RATIO,
    _FAULT_PRIMARY_FAIL_AFTER_BYTES_ENV,
    _FAULT_TAIL_GUARD_BYTES,
    _RECOVERABLE_HTTP_RANGE_ERROR_CODES,
)


def _passthrough_watchdog_applies(ctx):
    """True if the throughput watchdog should monitor this stream.

    Only video gets the watchdog — audio bit rates legitimately fall under
    the 100 KB/s floor (a 64 kbps MP3 is 8 KB/s). Surfaced as a helper so
    the caller is short and the policy is testable in isolation.
    """
    content_type = (ctx.get("content_type") or "").lower()
    return content_type.startswith("video/")


def _set_upstream_read_timeout(resp, timeout):
    """Best-effort: arm a recv() deadline on an urlopen response's socket.

    urllib inherits the urlopen connect timeout for reads, but we set a
    tighter, explicit deadline on the body socket so a stalled upstream
    surfaces as a recoverable read error promptly (which drives live
    fallback) rather than blocking until the equal proxy->Kodi write
    timeout fires first and the stall is misattributed to a client
    disconnect. Tolerant of the CPython-internal attribute path
    (BufferedReader -> SocketIO -> socket) being absent on other
    implementations; on failure the inherited urlopen timeout still
    applies. See https://github.com/Appz4Fun/nzbdavkodi/issues/214
    """
    try:
        sock = resp.fp.raw._sock
    except AttributeError:
        return
    if sock is None:
        return
    try:
        sock.settimeout(timeout)
    except OSError:
        pass


def _fault_primary_fail_threshold():
    """Parse the fault-injection byte threshold env, or None when disabled."""
    raw = os.environ.get(_FAULT_PRIMARY_FAIL_AFTER_BYTES_ENV)
    if not raw:
        return None
    try:
        threshold = int(raw)
    except (TypeError, ValueError):
        return None
    return threshold if threshold > 0 else None


def _fault_forced_primary_failure(ctx, start):
    threshold = _sp._fault_primary_fail_threshold()
    if threshold is None:
        return False
    # Only fail the primary; once cut over to a fallback, let it stream so
    # playback can actually recover.
    if int(ctx.get("fallback_switch_count", 0) or 0) > 0:
        return False
    # Spare the tail so the demuxer initializes and playback survives long
    # enough for fallbacks to attach (see _FAULT_TAIL_GUARD_BYTES). Only when
    # the file is large enough that the tail guard and the threshold band
    # don't overlap — small test files keep the simple start>=threshold rule.
    content_length = int(ctx.get("content_length", 0) or 0)
    in_tail_guard = (
        content_length > _FAULT_TAIL_GUARD_BYTES + threshold
        and start >= content_length - _FAULT_TAIL_GUARD_BYTES
    )
    if in_tail_guard:
        return False
    return start >= threshold


def _expected_content_range(start, end, content_length):
    return "bytes {}-{}/{}".format(start, end, content_length)


def _get_header(resp, name):
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    return headers.get(name)


def _add_request_headers(req, auth_header=None):
    """Apply standard proxy outbound headers to a urllib Request."""
    req.add_header("User-Agent", HTTP_USER_AGENT)
    if auth_header:
        auth_header = _sp._validate_auth_header(auth_header)
        req.add_header("Authorization", auth_header)
    return req


def _is_terminal_http_client_error(error):
    if not isinstance(error, HTTPError):
        return False
    code = getattr(error, "code", 0) or 0
    return 400 <= code < 500 and code not in _RECOVERABLE_HTTP_RANGE_ERROR_CODES


def _strip_header_value(value):
    """Trim RFC-9110-permitted surrounding whitespace from a header value."""
    if isinstance(value, str):
        return value.strip()
    return value


def _classify_contract_status(status, is_full_object):
    """Flag a non-206 status that isn't a permitted full-object 200."""
    if status == 206:
        return None, False
    if status == 200 and is_full_object:
        return None, False
    return "status={} expected=206".format(status), True


def _classify_contract_range(status, content_range, expected_range, is_full_object):
    """Flag a missing/mismatched Content-Range against the expected value."""
    if status == 206 and content_range in (None, ""):
        # Tightening this to hard=True is the RFC-strict reading
        # but in practice it broke test fixtures and at least one
        # real upstream that returned 206 with valid Content-Length
        # but no Content-Range. Left as a soft (warn-logged) issue;
        # the chunk loop's `requested = end - start + 1` clamp
        # bounds what we actually stream, so the upside of going
        # hard is purely diagnostic. See TODO.md §H.3.
        return (
            "Content-Range missing expected={!r}".format(expected_range),
            False,
        )
    if content_range in (None, "") or content_range == expected_range:
        return None, False
    if status != 206 and status == 200 and is_full_object:
        return None, False
    return (
        "Content-Range={!r} expected={!r}".format(content_range, expected_range),
        True,
    )


def _classify_contract_mismatch(
    status, content_range, content_length, start, end, total
):
    """Classify upstream header contract issues as hard or soft mismatches."""
    expected_length = end - start + 1
    expected_range = _sp._expected_content_range(start, end, total)
    is_full_object = start == 0 and end == total - 1
    # HTTP/1.1 (RFC 9110) permits optional leading/trailing whitespace in
    # header values. Strip so an upstream that emits "Content-Length: 1024 "
    # (trailing space) doesn't get flagged as a protocol mismatch.
    content_range = _sp._strip_header_value(content_range)
    content_length = _sp._strip_header_value(content_length)
    checks = [
        _sp._classify_contract_status(status, is_full_object),
        _sp._classify_contract_range(
            status, content_range, expected_range, is_full_object
        ),
    ]
    if content_length != str(expected_length):
        detail = "Content-Length={!r} expected={!r}".format(
            content_length, str(expected_length)
        )
        checks.append((detail, False))

    problems = []
    hard = False
    for detail, is_hard in checks:
        if detail is None:
            continue
        problems.append(detail)
        if is_hard:
            hard = True

    if not problems:
        return None, False
    return "; ".join(problems), hard


def _log_contract_mismatch(start, end, status, content_range, content_length, detail):
    _sp.xbmc.log(
        "NZB-DAV: Upstream contract mismatch for {}-{} status={} "
        "Content-Range={!r} Content-Length={!r} detail={} "
        "(reason=protocol_mismatch)".format(
            start, end, status, content_range, content_length, detail
        ),
        _sp.xbmc.LOGWARNING,
    )


def _record_density_window(window, kind, count):
    """Track recent forward progress vs. zero-fill bytes in a fixed window."""
    if count <= 0:
        return
    window.append([kind, count])
    total = sum(item[1] for item in window)
    while total > _DENSITY_BREAKER_WINDOW_BYTES and window:
        overflow = total - _DENSITY_BREAKER_WINDOW_BYTES
        head = window[0]
        trim = min(head[1], overflow)
        head[1] -= trim
        total -= trim
        if head[1] == 0:
            window.popleft()


def _density_ratio(window):
    total = sum(item[1] for item in window)
    if total <= 0:
        return 0.0
    zero_fill = sum(item[1] for item in window if item[0] == "zero_fill")
    return float(zero_fill) / float(total)


def _would_trip_density_breaker(window, skip):
    if skip <= 0:
        return False
    # An empty recovery window means "no progress samples yet" — most
    # commonly, the very first range read failed before any bytes were
    # streamed. Returning True here would 100%-trip the breaker on the
    # very first recovery attempt and abort the stream before any
    # genuine recovery had a chance to land. Require at least one
    # progress sample before letting the breaker fire. Closes
    # TODO.md §H.3 ("density breaker trips on empty recovery window").
    if not window:
        return False
    trial = deque([item[:] for item in window])
    _sp._record_density_window(trial, "zero_fill", skip)
    return _sp._density_ratio(trial) > _DENSITY_BREAKER_ZERO_FILL_RATIO
