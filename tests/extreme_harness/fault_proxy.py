"""WebDAV fault proxy with HTTP control endpoint and 5 fault types.

Spec: docs/superpowers/specs/2026-05-09-extreme-functional-test-design.md
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import random
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlsplit

UPSTREAM = os.environ.get("FAULT_PROXY_UPSTREAM", "http://nzbdav-rs:8080")
LISTEN = os.environ.get("FAULT_PROXY_LISTEN", "0.0.0.0")
PORT = int(os.environ.get("FAULT_PROXY_PORT", "19080"))
CONTROL_PORT = int(os.environ.get("FAULT_PROXY_CONTROL_PORT", "19081"))
FAIL_BYTES = int(os.environ.get("FAULT_PROXY_FAIL_BYTES", str(4 * 1024 * 1024)))
# Keep the throttle rate ABOVE the addon's minimum viable throughput
# (_PASSTHROUGH_MIN_THROUGHPUT_BPS = 100 KiB/s): sustained sub-threshold
# delivery makes the addon declare terminal stream_starvation and stop
# playback BY DESIGN ("backend could not keep up"), so a 50 KiB/s
# throttle tested an unsurvivable condition and passed only when the
# throttle happened to land across connection boundaries (fresh
# connections reset the rate window). 300 KiB/s is still ~5% of REMUX
# bitrate — the player buffer drains and freezes — but recovery is
# possible, which is what the test measures.
SLOW_BPS = int(os.environ.get("FAULT_PROXY_SLOW_BPS", str(300 * 1024)))
SLOW_DURATION = float(os.environ.get("FAULT_PROXY_SLOW_DURATION", "30"))
MIN_FAIL_START = int(os.environ.get("FAULT_PROXY_MIN_FAIL_START", str(1024 * 1024)))
# Effectively unbounded by default. The old 1 GiB default starved fault
# injection on large files: a 23 GB REMUX passes the 1 GiB playback mark
# within minutes, after which no reconnect offset ever qualified again and
# 4 of 5 scheduled faults sat unfired for the rest of a 20-minute run.
# Tail protection (Kodi's file-open cues/moov probes near EOF) is handled
# by TAIL_EXCLUDE_BYTES against the upstream Content-Range total instead.
MAX_FAIL_START = int(os.environ.get("FAULT_PROXY_MAX_FAIL_START", str(1 << 50)))
TAIL_EXCLUDE_BYTES = int(
    os.environ.get("FAULT_PROXY_TAIL_EXCLUDE", str(512 * 1024 * 1024))
)
# How often the passthrough copy loop polls for newly-due faults so they
# can be applied MID-STREAM. The addon holds movie-length upstream
# connections (a 20-minute run produced only ~16 GETs), so waiting for
# the next request starved scheduled faults for minutes; capping response
# length instead poisoned every healthy transfer (the addon's health
# model treats short reads on a completed file as upstream failures and
# killed playback within 2 minutes). Applying due faults to the in-flight
# response leaves healthy traffic completely untouched.
MIDSTREAM_CHECK_SECONDS = float(
    os.environ.get("FAULT_PROXY_MIDSTREAM_CHECK_SECONDS", "1.0")
)
LOG_PATH = os.environ.get("FAULT_PROXY_LOG", "/var/log/fault-proxy/full.log")
EVENTS_PATH = os.environ.get("FAULT_PROXY_EVENTS", "/var/log/fault-proxy/events.jsonl")

VALID_FAULT_TYPES = {
    "connection_reset",
    "http_500",
    "slow_upstream",
    "truncated_response",
    "corrupted_bytes",
    # Permanently 404s the file path being streamed when it fires,
    # forcing the addon to promote a prevalidated standby (fallback
    # cutover). Two per schedule walks the fallback chain.
    "source_dead",
}


def _send_safe_header(handler, name, value) -> None:
    if not isinstance(name, str) or not isinstance(value, str):
        return
    if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
        return
    if name.lower() in ("connection", "transfer-encoding"):
        return
    handler.send_header(name, value)


def _forward_upstream_headers(handler, resp) -> None:
    for name, value in resp.getheaders():
        _send_safe_header(handler, name, value)


@dataclasses.dataclass
class ScheduledEvent:
    at_seconds: float
    fault_type: str


class ProxyState:
    """Mutable state shared between the control endpoint and the proxy handler."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.scheduled_events: list[ScheduledEvent] = []
        self.fired_events: list[dict] = []
        self.start_t: float = time.monotonic()
        self.start_t_wall: float = time.time()
        # A fault drawn mid-stream that can only be applied to a whole
        # request (http_500): parked here and consumed by the next
        # qualifying request instead.
        self.pending_fault: Optional[ScheduledEvent] = None
        # File paths killed by source_dead faults: every later request
        # for them gets an immediate 404, so the addon must cut over to
        # a standby source to keep playing.
        self.dead_paths: set = set()
        # Total content bytes forwarded to clients — the harness
        # watchdog's ground truth for whether playback is consuming
        # real upstream data (never reset; the watchdog tracks deltas).
        self.bytes_forwarded: int = 0

    def add_bytes_forwarded(self, count: int) -> None:
        with self.lock:
            self.bytes_forwarded += count

    def reset_clock(self) -> None:
        with self.lock:
            self.start_t = time.monotonic()
            self.start_t_wall = time.time()
            self.fired_events.clear()
            # Same hygiene as replace_schedule: a parked http_500 or dead
            # path from the previous run must not leak into the next one.
            self.pending_fault = None
            self.dead_paths.clear()

    def replace_schedule(self, events: list[ScheduledEvent]) -> None:
        """Atomically replace the event list and reset the run clock."""
        with self.lock:
            self.scheduled_events = sorted(events, key=lambda e: e.at_seconds)
            self.start_t = time.monotonic()
            self.start_t_wall = time.time()
            self.fired_events.clear()
            self.dead_paths.clear()
            self.pending_fault = None

    def kill_path(self, path: str) -> None:
        with self.lock:
            self.dead_paths.add(path)

    def is_dead_path(self, path: str) -> bool:
        with self.lock:
            return path in self.dead_paths

    def next_due_fault(self) -> Optional[ScheduledEvent]:
        """Return and remove the next scheduled event whose at_seconds has elapsed."""
        with self.lock:
            now_run = time.monotonic() - self.start_t
            for i, ev in enumerate(self.scheduled_events):
                if ev.at_seconds <= now_run:
                    return self.scheduled_events.pop(i)
            return None

    def set_pending_fault(self, event: ScheduledEvent) -> None:
        with self.lock:
            self.pending_fault = event

    def take_pending_fault(self) -> Optional[ScheduledEvent]:
        with self.lock:
            event, self.pending_fault = self.pending_fault, None
            return event

    def record_fired(self, fault_type: str, range_header: str) -> None:
        with self.lock:
            self.fired_events.append(
                {
                    "t_wall": time.time(),
                    "t_run": time.monotonic() - self.start_t,
                    "fault_type": fault_type,
                    "range": range_header,
                }
            )


# --- Logging helpers ---

_log_lock = threading.Lock()


def _log(message: str) -> None:
    line = message.rstrip() + "\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    if not LOG_PATH:
        return
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with _log_lock, open(LOG_PATH, "a", encoding="utf-8") as h:
            h.write(line)
    except OSError:
        pass


def _log_event(event: dict) -> None:
    if not EVENTS_PATH:
        return
    try:
        os.makedirs(os.path.dirname(EVENTS_PATH), exist_ok=True)
        with _log_lock, open(EVENTS_PATH, "a", encoding="utf-8") as h:
            h.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        pass


# --- Range helpers (unchanged from original) ---


def _range_bounds(value):
    if not value or not value.startswith("bytes="):
        return None
    start_text, end_text = value[6:].split("-", 1)
    try:
        start = int(start_text)
    except (TypeError, ValueError):
        return None
    if not end_text:
        return start, None
    try:
        end = int(end_text)
    except (TypeError, ValueError):
        return None
    return start, end


def _content_range_total(resp):
    """Total file length from an upstream Content-Range header, or None."""
    value = resp.getheader("Content-Range") if hasattr(resp, "getheader") else None
    if not value or "/" not in value:
        return None
    total_text = value.rsplit("/", 1)[1].strip()
    try:
        return int(total_text)
    except (TypeError, ValueError):
        return None


def _is_large_playback_range(value, total_length=None):
    bounds = _range_bounds(value)
    if bounds is None:
        return False
    start, end = bounds
    if start < MIN_FAIL_START or start > MAX_FAIL_START:
        return False
    # Never fault reads near the file tail: Kodi's MKV open probes the
    # cues/index at EOF, and failing those breaks file-open rather than
    # exercising mid-playback recovery.
    if total_length and start > max(0, total_length - TAIL_EXCLUDE_BYTES):
        return False
    if end is None:
        return True
    return (end - start + 1) >= (1024 * 1024)


# --- Fault implementations (filled in by Tasks 7-9) ---


def _apply_connection_reset(handler, resp, range_header, state) -> None:
    """Forward FAIL_BYTES of the upstream body, then slam the connection closed."""
    # Record state before sending response so test threads observing
    # fired_events after the client unblocks see the entry deterministically.
    state.record_fired("connection_reset", range_header)
    _log_event(
        {
            "fault_type": "connection_reset",
            "t_wall": time.time(),
            "range": range_header,
            "fail_bytes": FAIL_BYTES,
        }
    )
    handler.send_response(resp.status, resp.reason)
    _forward_upstream_headers(handler, resp)
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()
    remaining = FAIL_BYTES
    while remaining > 0:
        chunk = resp.read(min(65536, remaining))
        if not chunk:
            break
        handler.wfile.write(chunk)
        remaining -= len(chunk)
    try:
        handler.connection.shutdown(1)
    except OSError:
        pass
    handler.connection.close()


def _apply_http_500(handler, resp, range_header, state) -> None:
    """Discard the upstream response and return a 500."""
    resp.close()
    # Record state before sending response so test threads observing
    # fired_events after the client unblocks see the entry deterministically.
    state.record_fired("http_500", range_header)
    _log_event(
        {
            "fault_type": "http_500",
            "t_wall": time.time(),
            "range": range_header,
        }
    )
    handler.send_response(500, "Internal Server Error")
    handler.send_header("Content-Length", "0")
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()


def _apply_slow_upstream(handler, resp, range_header, state) -> None:
    """Throttle the response to SLOW_BPS for SLOW_DURATION seconds, then full speed."""
    handler.send_response(resp.status, resp.reason)
    _forward_upstream_headers(handler, resp)
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()
    # Record state early (right after end_headers) so that the fired_events
    # entry is visible to test threads as soon as they unblock from read().
    # Streaming fault: state recorded before the streaming-window completes;
    # bytes are still in flight, but the test thread observes both fields
    # together via urlopen.read() because read() blocks until bytes arrive.
    state.record_fired("slow_upstream", range_header)
    _log_event(
        {
            "fault_type": "slow_upstream",
            "t_wall": time.time(),
            "range": range_header,
            "duration": SLOW_DURATION,
        }
    )
    deadline = time.monotonic() + SLOW_DURATION
    chunk_size = max(1024, SLOW_BPS // 10)  # ~10 chunks/sec
    sleep_per_chunk = chunk_size / SLOW_BPS
    while time.monotonic() < deadline:
        chunk = resp.read(chunk_size)
        if not chunk:
            resp.close()
            return
        try:
            handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            resp.close()
            return
        time.sleep(sleep_per_chunk)
    # Past throttle window — drain at full speed.
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        try:
            handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            break
    resp.close()


def _apply_truncated_response(handler, resp, range_header, state) -> None:
    """Forward upstream's headers as-is (so client sees the upstream Content-Length),
    then send only FAIL_BYTES of body and close, causing a premature EOF.
    """
    handler.send_response(resp.status, resp.reason)
    _forward_upstream_headers(handler, resp)
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()
    # Record state early (right after end_headers) so test threads observing
    # fired_events after the client unblocks on IncompleteRead see the entry
    # deterministically — same early-record convention as _apply_slow_upstream.
    state.record_fired("truncated_response", range_header)
    _log_event(
        {
            "fault_type": "truncated_response",
            "t_wall": time.time(),
            "range": range_header,
            "scheduled_bytes": FAIL_BYTES,
        }
    )
    sent = 0
    while sent < FAIL_BYTES:
        chunk = resp.read(min(65536, FAIL_BYTES - sent))
        if not chunk:
            break
        try:
            handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            resp.close()
            return
        sent += len(chunk)
    resp.close()


def _apply_corrupted_bytes(handler, resp, range_header, state) -> None:
    """Forward the response with 32 random byte positions XOR'd in the first FAIL_BYTES,
    then stream the remainder of the body unmodified.
    """
    handler.send_response(resp.status, resp.reason)
    _forward_upstream_headers(handler, resp)
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()
    # Record state early (right after end_headers) so test threads observing
    # fired_events after the client's read() returns see the entry deterministically
    # — same early-record convention as _apply_slow_upstream.
    state.record_fired("corrupted_bytes", range_header)
    _log_event(
        {
            "fault_type": "corrupted_bytes",
            "t_wall": time.time(),
            "range": range_header,
            "corruption_count": min(32, FAIL_BYTES),
        }
    )
    head = bytearray(resp.read(FAIL_BYTES))
    if head:
        positions = sorted(random.sample(range(len(head)), min(32, len(head))))
        for p in positions:
            head[p] ^= 0xFF
        try:
            handler.wfile.write(bytes(head))
        except (BrokenPipeError, ConnectionResetError):
            resp.close()
            return
    while True:
        chunk = resp.read(65536)
        if not chunk:
            break
        try:
            handler.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            break
    resp.close()


def _apply_source_dead(handler, resp, range_header, state) -> None:
    """Kill this request's file path permanently and 404 the request.

    Every subsequent request for the same path also 404s (see the
    dead-path gate in _forward), so the addon's retries fail fast and it
    must promote a prevalidated standby — a real fallback cutover.
    """
    resp.close()
    path = handler.path.split("?", 1)[0]
    state.kill_path(path)
    state.record_fired("source_dead", range_header)
    _log_event(
        {
            "fault_type": "source_dead",
            "t_wall": time.time(),
            "range": range_header,
            "path": path,
        }
    )
    handler.send_response(404, "Not Found")
    handler.send_header("Content-Length", "0")
    handler.send_header("Connection", "close")
    handler.close_connection = True
    handler.end_headers()


def _apply_midstream_fault(handler, due, range_header, state) -> Optional[str]:
    """Apply a due fault to an in-flight passthrough stream.

    Returns the action for the copy loop: "stop" (end the response now —
    the client sees a short read / reset and re-requests), "corrupt"
    (flip bytes in the next chunk), "slow" (throttle for SLOW_DURATION),
    or None (nothing to change on this stream; http_500 is parked for
    the next request because a 500 can't be sent mid-body).
    """
    if due.fault_type == "http_500":
        # Park the 500 for the next request and cut this stream short so
        # that request arrives promptly. Recorded when actually served.
        state.set_pending_fault(due)
        return "stop"
    if due.fault_type == "source_dead":
        # Kill the path being streamed and cut the stream: the addon's
        # re-request 404s via the dead-path gate and it must cut over.
        path = handler.path.split("?", 1)[0]
        state.kill_path(path)
        state.record_fired("source_dead", range_header)
        _log_event(
            {
                "fault_type": "source_dead",
                "t_wall": time.time(),
                "range": range_header,
                "path": path,
                "midstream": True,
            }
        )
        return "stop"
    payload = {
        "fault_type": due.fault_type,
        "t_wall": time.time(),
        "range": range_header,
        "midstream": True,
    }
    state.record_fired(due.fault_type, range_header)
    _log_event(payload)
    if due.fault_type == "connection_reset":
        try:
            handler.connection.shutdown(1)
        except OSError:
            # Socket may already be half-closed by the peer; the goal is
            # an abrupt teardown, so a failed shutdown is fine.
            pass
        try:
            handler.connection.close()
        except OSError:
            # Already closed — the reset the client observes is the same.
            pass
        return "stop"
    if due.fault_type == "truncated_response":
        return "stop"
    if due.fault_type == "corrupted_bytes":
        return "corrupt"
    if due.fault_type == "slow_upstream":
        return "slow"
    _log(f"WARN unimplemented midstream fault_type={due.fault_type!r}")
    return None


_FAULT_DISPATCH = {
    "source_dead": _apply_source_dead,
    "connection_reset": _apply_connection_reset,
    "http_500": _apply_http_500,
    "slow_upstream": _apply_slow_upstream,
    "truncated_response": _apply_truncated_response,
    "corrupted_bytes": _apply_corrupted_bytes,
}


# --- Control HTTP server ---


class ControlHandler(BaseHTTPRequestHandler):
    state: ProxyState  # set by start_control_server

    def log_message(self, fmt, *args):
        _log("CONTROL " + fmt % args)

    def do_GET(self):
        if self.path == "/control/health":
            # bytes_forwarded lets the harness watchdog spot BOGUS
            # playback: Kodi advancing its clock on locally-fabricated
            # data (zero-fill style) while no upstream bytes flow.
            with self.state.lock:
                forwarded = self.state.bytes_forwarded
                fired = len(self.state.fired_events)
            body = json.dumps(
                {
                    "status": "ok",
                    "bytes_forwarded": forwarded,
                    "fired": fired,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/control/schedule":
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400, "bad JSON")
                return
            events = payload.get("events", [])
            parsed = []
            for ev in events:
                fault_type = ev.get("fault_type")
                if fault_type not in VALID_FAULT_TYPES:
                    self.send_error(400, f"unknown fault_type: {fault_type}")
                    return
                at_seconds_raw = ev.get("at_seconds")
                if at_seconds_raw is None:
                    self.send_error(400, "missing at_seconds")
                    return
                try:
                    at_seconds = float(at_seconds_raw)
                except (TypeError, ValueError):
                    self.send_error(400, "at_seconds not a number")
                    return
                parsed.append(
                    ScheduledEvent(
                        at_seconds=at_seconds,
                        fault_type=fault_type,
                    )
                )
            self.state.replace_schedule(parsed)
            body = json.dumps({"scheduled": len(parsed)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_control_server(
    state: ProxyState, host: str = "0.0.0.0", port: int = CONTROL_PORT
) -> _ThreadedHTTPServer:
    handler_class = type("BoundControlHandler", (ControlHandler,), {"state": state})
    server = _ThreadedHTTPServer((host, port), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# --- Main proxy handler ---


_upstream = urlsplit(UPSTREAM.rstrip("/"))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: ProxyState  # set by main()

    def log_message(self, fmt, *args):
        # Timestamp + Range make post-mortems answerable: "which sessions
        # were alive when a scheduled fault starved" needs both.
        stamp = time.strftime("%H:%M:%S", time.gmtime())
        range_header = self.headers.get("Range", "") if self.headers else ""
        _log(f"REQUEST {stamp} [{range_header}] " + fmt % args)

    def _forward(self, head_only=False):
        # Dead-path gate: a source_dead fault killed this file — 404
        # immediately without touching the upstream so the addon's
        # retries fail fast and promotion to a standby begins.
        if self.state.is_dead_path(self.path.split("?", 1)[0]):
            self.send_response(404, "Not Found")
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            # pylint: disable-next=attribute-defined-outside-init
            self.close_connection = True
            self.end_headers()
            return
        method = "HEAD" if head_only else self.command
        if _upstream.scheme == "https":
            conn = http.client.HTTPSConnection(
                _upstream.hostname,
                _upstream.port or 443,
                timeout=120,
            )
        else:
            conn = http.client.HTTPConnection(
                _upstream.hostname,
                _upstream.port or 80,
                timeout=120,
            )
        target = self.path
        if _upstream.path:
            target = _upstream.path.rstrip("/") + self.path
        headers = {
            k: v
            for k, v in self.headers.items()
            if k.lower() not in ("host", "connection", "proxy-connection")
        }
        headers["Host"] = _upstream.netloc
        range_header = headers.get("Range") or headers.get("range") or ""
        body = None
        cl = self.headers.get("Content-Length")
        if cl:
            try:
                body = self.rfile.read(int(cl))
            except ValueError:
                body = None
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        try:
            fault_eligible = (
                not head_only
                and method == "GET"
                and _is_large_playback_range(
                    range_header, total_length=_content_range_total(resp)
                )
            )
            if fault_eligible:
                # A parked whole-request fault (http_500 drawn mid-stream)
                # takes precedence over the schedule.
                due = self.state.take_pending_fault() or self.state.next_due_fault()
                if due is not None:
                    fn = _FAULT_DISPATCH.get(due.fault_type)
                    if fn is None:
                        _log(
                            f"WARN unimplemented fault_type={due.fault_type!r}"
                            " - passthrough"
                        )
                    else:
                        fn(self, resp, range_header, self.state)
                        return
            self._passthrough(
                resp,
                head_only=head_only,
                fault_eligible=fault_eligible,
                range_header=range_header,
            )
        finally:
            conn.close()

    def _passthrough(
        self, resp, head_only=False, fault_eligible=False, range_header=""
    ):
        self.send_response(resp.status, resp.reason)
        _forward_upstream_headers(self, resp)
        self.send_header("Connection", "close")
        # BaseHTTPRequestHandler owns this connection flag.
        # pylint: disable-next=attribute-defined-outside-init
        self.close_connection = True
        self.end_headers()
        if head_only:
            resp.close()
            return
        # The addon streams movie-length responses over single upstream
        # connections, so faults that come due mid-transfer would starve
        # waiting for the next request. Poll for due faults while copying
        # (fault-eligible streams only) and apply them to the live stream.
        # An open-ended range that STARTED outside the protected tail can
        # still advance into it, so compute the byte budget after which
        # mid-stream injection must stop (EOF cues/index reads).
        midstream_fault_budget = None
        bounds = _range_bounds(range_header)
        total = _content_range_total(resp)
        if bounds is not None and total:
            midstream_fault_budget = max(0, (total - TAIL_EXCLUDE_BYTES) - bounds[0])
        # A session whose range STARTED below MIN_FAIL_START (Kodi reopens
        # from a low offset after a fault) is start-ineligible but can
        # stream hundreds of MB — freezing eligibility at request start
        # let due faults starve for that session's whole life while the
        # proxy carried the only playback traffic. Promote it once its
        # absolute position crosses MIN_FAIL_START; the budget check
        # still guards the protected tail.
        dynamic_eligible = (
            not head_only
            and not fault_eligible
            and bounds is not None
            and bool(total)
            and midstream_fault_budget is not None
            and midstream_fault_budget > 0
        )
        throttle_until = 0.0
        corrupt_next_chunk = False
        last_fault_check = 0.0
        sent = 0
        while True:
            if dynamic_eligible and bounds[0] + sent >= MIN_FAIL_START:
                fault_eligible = True
                dynamic_eligible = False
            if fault_eligible and (
                midstream_fault_budget is None or sent < midstream_fault_budget
            ):
                now = time.monotonic()
                if now - last_fault_check >= MIDSTREAM_CHECK_SECONDS:
                    last_fault_check = now
                    due = self.state.next_due_fault()
                    if due is not None:
                        action = _apply_midstream_fault(
                            self, due, range_header, self.state
                        )
                        if action == "stop":
                            resp.close()
                            return
                        if action == "corrupt":
                            corrupt_next_chunk = True
                        elif action == "slow":
                            throttle_until = time.monotonic() + SLOW_DURATION
            if throttle_until and time.monotonic() < throttle_until:
                chunk = resp.read(max(1024, SLOW_BPS // 10))
            else:
                chunk = resp.read(65536)
            if not chunk:
                break
            if corrupt_next_chunk:
                corrupt_next_chunk = False
                mutable = bytearray(chunk)
                positions = random.sample(range(len(mutable)), min(32, len(mutable)))
                for p in positions:
                    mutable[p] ^= 0xFF
                chunk = bytes(mutable)
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            sent += len(chunk)
            self.state.add_bytes_forwarded(len(chunk))
            if throttle_until and time.monotonic() < throttle_until:
                time.sleep(max(1024, SLOW_BPS // 10) / SLOW_BPS)
        resp.close()

    def do_HEAD(self):
        self._forward(head_only=True)

    def do_GET(self):
        self._forward(head_only=False)

    def do_PROPFIND(self):
        self._forward(head_only=False)


def main():
    state = ProxyState()
    handler_class = type("BoundProxyHandler", (Handler,), {"state": state})
    start_control_server(state, host=LISTEN, port=CONTROL_PORT)
    _log(f"START listen={LISTEN}:{PORT} control={CONTROL_PORT} upstream={UPSTREAM}")
    with _ThreadedHTTPServer((LISTEN, PORT), handler_class) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
