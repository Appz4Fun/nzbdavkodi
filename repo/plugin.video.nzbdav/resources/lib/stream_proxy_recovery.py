# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Upstream-reachability, starvation, and recovery-summary notifications.

Extracted from ``stream_proxy.py`` (Stage 1 decomposition). Groups upstream
error classification, the per-server unreachable/recovered flag transitions,
one-shot starvation detection + user notification, the per-session recovery
state accounting, and the end-of-stream recovery-summary notification. All
names are re-exported by ``stream_proxy`` so existing references and test
patches keep resolving.

Plain constants are imported from ``stream_proxy``; parent helpers and any
monkeypatch target (``xbmc``, ``_notify``, ``urlopen``) are reached at call
time via ``_sp.<name>`` so patching keeps working.
"""

import socket as _socket  # noqa: E402
import time  # noqa: E402
from urllib.error import HTTPError, URLError  # noqa: E402

import resources.lib.stream_proxy as _sp  # noqa: E402
from resources.lib.stream_proxy import (  # noqa: E402
    _RECOVERY_NOTIFY_DEBOUNCE_SECONDS,
    _STARVATION_RECENT_OUTAGE_SECONDS,
    _STARVATION_TERMINAL_REASONS,
    _UPSTREAM_REACHABILITY_HTTP_CLIENT_ERROR,
    _UPSTREAM_REACHABILITY_HTTP_SERVER_ERROR,
    _UPSTREAM_REACHABILITY_OTHER,
    _UPSTREAM_REACHABILITY_UNREACHABLE_NETWORK,
)


def _classify_upstream_error(error):
    """Bucket a urlopen exception into a reachability category.

    Returns one of:
      * ``_UPSTREAM_REACHABILITY_UNREACHABLE_NETWORK`` — DNS / TCP-refused /
        socket timeout / connection reset. Strong signal that nzbdav (or
        the network path to it) is down, not that the stream itself is
        bad. Worth surfacing to the user.
      * ``_UPSTREAM_REACHABILITY_HTTP_SERVER_ERROR`` — HTTPError with
        status 5xx. nzbdav is up but stressed/erroring.
      * ``_UPSTREAM_REACHABILITY_HTTP_CLIENT_ERROR`` — HTTPError with
        status 4xx. Auth or path issue, not an outage.
      * ``_UPSTREAM_REACHABILITY_OTHER`` — any other OSError / ValueError
        that doesn't fit the above.

    The distinction matters because the stream-proxy's zero-fill /
    skip-probe recovery can disguise a total upstream outage as a
    "bad stream" to the user. Classifying lets us emit an actionable
    notification instead of silently zero-filling the rest of the file.
    """
    if isinstance(error, HTTPError):
        code = getattr(error, "code", 0) or 0
        if 500 <= code < 600:
            return _UPSTREAM_REACHABILITY_HTTP_SERVER_ERROR
        if 400 <= code < 500:
            return _UPSTREAM_REACHABILITY_HTTP_CLIENT_ERROR
        return _UPSTREAM_REACHABILITY_OTHER
    # URLError wraps the underlying reason (socket.gaierror, timeout, etc.)
    if isinstance(error, URLError):
        reason = getattr(error, "reason", None)
        if isinstance(
            reason, (ConnectionError, _socket.timeout, TimeoutError, OSError)
        ):
            return _UPSTREAM_REACHABILITY_UNREACHABLE_NETWORK
    if isinstance(error, (ConnectionError, _socket.timeout, TimeoutError)):
        return _UPSTREAM_REACHABILITY_UNREACHABLE_NETWORK
    return _UPSTREAM_REACHABILITY_OTHER


def _clear_upstream_unreachable_flag(ctx, observed_at):
    """Reset the one-shot notify gate when ``observed_at`` is the newest event.

    Returns True when the flag was cleared (caller holds the context lock),
    False when nothing was notified or a newer failure supersedes this
    success observation.
    """
    if not ctx.get("upstream_down_notified"):
        return False
    last_down = float(ctx.get("last_upstream_unreachable_at", 0) or 0)
    # Drop stale success observations — a newer failure takes
    # precedence. When timestamps tie (same millisecond), prefer
    # success since that's the happy-path default.
    if last_down > observed_at:
        return False
    ctx["upstream_down_notified"] = False
    ctx["upstream_last_recovered_at"] = observed_at
    return True


def _record_upstream_recovered(server, ctx, observed_at=None):
    """Clear the session's unreachable flag once upstream bytes flow again.

    Paired with ``_record_upstream_unreachable``: after a prolonged
    outage notification has fired, a subsequent successful upstream
    read means nzbdav is back. Clearing the flag lets a LATER outage
    in the same session fire a fresh notification instead of staying
    silent under the "already notified" guard.

    **Timestamp ordering** guards a concurrency race: with the
    ThreadingHTTPServer, Thread A can be handling a successful
    urlopen while Thread B is mid-failure on a different range
    request. Without ordering, A's "cleared" update could stomp B's
    "marked down" update (or vice versa), producing a silently-
    latched or silently-cleared flag that didn't reflect the most
    recent observation. Callers pass ``observed_at`` (the wall-clock
    time at which they opened the socket); the helper only clears
    the flag when that observation is NEWER than the most recent
    recorded unreachable event. An older observation is dropped as
    stale.

    Preserves ``upstream_unreachable_count`` as a running total for
    diagnostics — we only reset the one-shot notification gate.
    """
    if observed_at is None:
        observed_at = time.time()
    context_lock = _sp._get_server_context_lock(server)

    def _update():
        return _sp._clear_upstream_unreachable_flag(ctx, observed_at)

    if context_lock is None:
        cleared = _update()
    else:
        with context_lock:
            cleared = _update()

    if cleared:
        _sp.xbmc.log(
            "NZB-DAV: Upstream reachable again after outage "
            "(reason=upstream_recovered)",
            _sp.xbmc.LOGINFO,
        )


def _record_upstream_unreachable(server, ctx, error):
    """Track an upstream-unreachable event on the session and fire a
    one-shot user notification the first time it happens.

    The handler that decides to zero-fill / retry / abort is unchanged —
    this is purely a visibility layer so the user learns "nzbdav is
    unreachable" instead of watching silent playback glitch through to
    the end. Subsequent failures in the same session bump the counter
    but stay silent to avoid spamming the UI during a prolonged outage.
    Pair with ``_record_upstream_recovered`` so a later outage in the
    same session can fire a fresh notification instead of remaining
    silent behind the "already notified" guard.
    """
    context_lock = _sp._get_server_context_lock(server)

    def _update():
        already_notified = bool(ctx.get("upstream_down_notified"))
        count = int(ctx.get("upstream_unreachable_count", 0) or 0) + 1
        ctx["upstream_unreachable_count"] = count
        ctx["last_upstream_unreachable_at"] = time.time()
        if already_notified:
            return False
        ctx["upstream_down_notified"] = True
        return True

    if context_lock is None:
        should_notify = _update()
    else:
        with context_lock:
            should_notify = _update()

    if not should_notify:
        return

    _sp.xbmc.log(
        "NZB-DAV: Upstream appears unreachable ({}); notifying user once "
        "(reason=upstream_unreachable)".format(type(error).__name__),
        _sp.xbmc.LOGERROR,
    )
    try:
        _sp._notify("NZB-DAV", "nzbdav unreachable — playback may glitch")
    except (RuntimeError, OSError):
        pass


def _claim_one_shot_flag(ctx, flag):
    """Set a one-shot context flag, returning True only on the first claim.

    Caller holds the context lock; subsequent calls with the flag already
    set return False so a debounced notification fires at most once.
    """
    if ctx.get(flag):
        return False
    ctx[flag] = True
    return True


def _stream_starvation_evident(ctx, terminal_reason):
    """Whether a non-clean stream end shows evidence of backend starvation.

    The backend (not a healthy stop) ended this stream when an explicit stall
    terminal reason fired, OR the upstream is still flagged down, OR an outage
    happened very recently, OR the patient forward-stall wait exhausted. The
    forward-stall signal covers the pure-SLOW case (a still-downloading region
    that never caught up: AWAITING with no 5xx, so no outage is recorded) —
    without it that give-up would be silent, the exact thing this guard exists
    to prevent. The RECENCY window matters because ``upstream_unreachable_count``
    is a sticky running total and ``last_upstream_unreachable_at`` can predate a
    long healthy stretch the user then stopped — that must NOT fire. It also
    catches the live incident, where the upstream momentarily "recovered" a few
    seconds before Kodi gave up and disconnected (so a recovered-vs-unreachable
    ordering check alone would wrongly stay silent).

    ``complete`` and ``density_breaker_tripped`` (which emits its own toast)
    never count.
    """
    if terminal_reason in ("complete", "density_breaker_tripped"):
        return False
    if terminal_reason in _STARVATION_TERMINAL_REASONS:
        return True
    if ctx.get("upstream_down_notified") or ctx.get("forward_stall_exhausted"):
        return True
    last_down = float(ctx.get("last_upstream_unreachable_at", 0) or 0)
    return (
        last_down > 0 and (time.time() - last_down) <= _STARVATION_RECENT_OUTAGE_SECONDS
    )


def _maybe_notify_stream_starvation(
    server, ctx, terminal_reason, total_streamed, requested_bytes
):
    """Fire ONE clear notification when a pass-through stream ends because the
    backend could not keep up, so the user gets an explanation instead of a
    silent black screen. This is the graceful-starvation guard for the live
    failure mode where nzbdav delivers far below the release's bitrate and/or
    returns HTTP 5xx for a sustained period: playback exhausts the head-start,
    the demuxer EOFs, and Kodi stops with no reason. The user should learn WHY.

    Distinct from the early one-shot ``_record_upstream_unreachable`` toast
    (which warns when an outage STARTS): this explains why playback STOPPED. It
    fires only for an abnormal end that shows backend trouble (see
    ``_stream_starvation_evident``) and never for a clean finish or a healthy
    stop of a fully-delivered stream. Debounced once per session via
    ``starvation_notified``; returns whether the notification fired.
    """
    if not _sp._stream_starvation_evident(ctx, terminal_reason):
        return False
    # Only when genuinely starved — a fully-delivered range the user happened
    # to stop is not starvation.
    if 0 < requested_bytes <= total_streamed:
        return False

    context_lock = _sp._get_server_context_lock(server)

    def _update():
        return _sp._claim_one_shot_flag(ctx, "starvation_notified")

    if context_lock is None:
        fire = _update()
    else:
        with context_lock:
            fire = _update()
    if not fire:
        return False

    _sp.xbmc.log(
        "NZB-DAV: Stream stalled — backend could not keep up "
        "(terminal={} streamed={} requested={} reason=stream_starvation)".format(
            terminal_reason, total_streamed, requested_bytes
        ),
        _sp.xbmc.LOGWARNING,
    )
    try:
        _sp._notify("NZB-DAV", "nzbdav can't keep up — playback stalled")
    except (RuntimeError, OSError):
        pass
    return True


def _read_session_recovery_state(ctx):
    return {
        "streamed": int(ctx.get("session_streamed_bytes", 0) or 0),
        "zero_fill": int(ctx.get("session_zero_fill_bytes", 0) or 0),
        "recoveries": int(ctx.get("session_recovery_count", 0) or 0),
        "last_notify": float(ctx.get("last_recovery_notify_at", 0) or 0),
    }


def _update_session_recovery_state(server, ctx, streamed=0, zero_fill=0, recoveries=0):
    """Apply session-level recovery counters under the proxy context lock."""
    context_lock = _sp._get_server_context_lock(server)

    def _update():
        state = _sp._read_session_recovery_state(ctx)
        state["streamed"] += streamed
        state["zero_fill"] += zero_fill
        state["recoveries"] += recoveries
        ctx["session_streamed_bytes"] = state["streamed"]
        ctx["session_zero_fill_bytes"] = state["zero_fill"]
        ctx["session_recovery_count"] = state["recoveries"]
        return state

    if context_lock is None:
        return _update()
    with context_lock:
        return _update()


def _project_session_zero_fill_ratio(
    server, ctx, extra_zero_fill=0, extra_recoveries=0
):
    """Return the projected session zero-fill ratio if another gap is skipped."""
    context_lock = _sp._get_server_context_lock(server)

    def _project():
        state = _sp._read_session_recovery_state(ctx)
        projected_zero_fill = state["zero_fill"] + extra_zero_fill
        projected_recoveries = state["recoveries"] + extra_recoveries
        denominator = max(
            int(ctx.get("content_length", 0) or 0),
            state["streamed"] + projected_zero_fill,
        )
        ratio = float(projected_zero_fill) / float(denominator or 1)
        return projected_zero_fill, projected_recoveries, ratio

    if context_lock is None:
        return _project()
    with context_lock:
        return _project()


def _prepare_recovery_summary(ctx, now, zero_fill_bytes, recovery_count):
    """Compute the (skipped, recoveries) summary payload, or None.

    Returns None when there is nothing to report or the toast is still
    inside the debounce window. Stamps the last-notify time when a payload
    is returned (caller holds the context lock).
    """
    state = _sp._read_session_recovery_state(ctx)
    skipped = state["zero_fill"] if zero_fill_bytes is None else zero_fill_bytes
    recoveries = state["recoveries"] if recovery_count is None else recovery_count
    if recoveries <= 0:
        return None
    if state["last_notify"] and (
        now - state["last_notify"] < _RECOVERY_NOTIFY_DEBOUNCE_SECONDS
    ):
        return None
    ctx["last_recovery_notify_at"] = now
    return skipped, recoveries


def _maybe_notify_recovery_summary(
    server, ctx, zero_fill_bytes=None, recovery_count=None
):
    """Emit a debounced toast summarizing skipped bytes and recovery count.

    ``zero_fill_bytes`` and ``recovery_count``, when provided, override the
    stored per-session counters used in the summary. Notifications are
    rate-limited by ``_RECOVERY_NOTIFY_DEBOUNCE_SECONDS`` to avoid frequent
    toasts.

    Parameters:
        server: Server instance owning the session context (used for optional
            context locking).
        ctx (dict): Session context with recovery counters and the last-notify
            timestamp.
        zero_fill_bytes (int | None): Optional override for the total
            skipped/zero-filled bytes to report.
        recovery_count (int | None): Optional override for the recovery count.

    Returns:
        bool: ``True`` if a notification was emitted, ``False`` otherwise
        (debounced, nothing to report, or an internal notification error).
    """
    context_lock = _sp._get_server_context_lock(server)
    now = time.time()

    def _prepare():
        return _sp._prepare_recovery_summary(ctx, now, zero_fill_bytes, recovery_count)

    if context_lock is None:
        payload = _prepare()
    else:
        with context_lock:
            payload = _prepare()

    if payload is None:
        return False
    skipped, recoveries = payload
    try:
        _sp._notify(
            "NZB-DAV",
            "Skipped {} bytes across {} recoveries".format(skipped, recoveries),
        )
    except (RuntimeError, OSError):
        return False
    return True


def _notify_fallback_outcome(candidate_number, success):
    """Toast the outcome of switching to a live fallback candidate.

    Parameters:
        candidate_number (int): 1-based position of the fallback candidate in
            the session's list.
        success (bool): ``True`` if the candidate began delivering bytes (the
            cutover succeeded), ``False`` if it was abandoned before any bytes
            arrived.

    Returns:
        bool: ``True`` if the toast was posted, ``False`` on a runtime/OS error.
    """
    outcome = "successful" if success else "was a failure"
    try:
        _sp._notify(
            "NZB-DAV",
            "fall back to candidate #{} {}".format(candidate_number, outcome),
        )
    except (RuntimeError, OSError):
        return False
    return True
