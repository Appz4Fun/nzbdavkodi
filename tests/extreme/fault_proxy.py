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
SLOW_BPS = int(os.environ.get("FAULT_PROXY_SLOW_BPS", str(50 * 1024)))
SLOW_DURATION = float(os.environ.get("FAULT_PROXY_SLOW_DURATION", "30"))
MIN_FAIL_START = int(os.environ.get("FAULT_PROXY_MIN_FAIL_START", str(1024 * 1024)))
MAX_FAIL_START = int(
    os.environ.get("FAULT_PROXY_MAX_FAIL_START", str(1024 * 1024 * 1024))
)
LOG_PATH = os.environ.get("FAULT_PROXY_LOG", "/var/log/fault-proxy/full.log")
EVENTS_PATH = os.environ.get("FAULT_PROXY_EVENTS", "/var/log/fault-proxy/events.jsonl")

VALID_FAULT_TYPES = {
    "connection_reset",
    "http_500",
    "slow_upstream",
    "truncated_response",
    "corrupted_bytes",
}


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

    def reset_clock(self) -> None:
        with self.lock:
            self.start_t = time.monotonic()
            self.start_t_wall = time.time()
            self.fired_events.clear()

    def replace_schedule(self, events: list[ScheduledEvent]) -> None:
        """Atomically replace the event list and reset the run clock."""
        with self.lock:
            self.scheduled_events = sorted(events, key=lambda e: e.at_seconds)
            self.start_t = time.monotonic()
            self.start_t_wall = time.time()
            self.fired_events.clear()

    def next_due_fault(self) -> Optional[ScheduledEvent]:
        """Return and remove the next scheduled event whose at_seconds has elapsed."""
        with self.lock:
            now_run = time.monotonic() - self.start_t
            for i, ev in enumerate(self.scheduled_events):
                if ev.at_seconds <= now_run:
                    return self.scheduled_events.pop(i)
            return None

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


def _is_large_playback_range(value):
    bounds = _range_bounds(value)
    if bounds is None:
        return False
    start, end = bounds
    if start < MIN_FAIL_START or start > MAX_FAIL_START:
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
    for k, v in resp.getheaders():
        if k.lower() in ("connection", "transfer-encoding"):
            continue
        handler.send_header(k, v)
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
    for k, v in resp.getheaders():
        if k.lower() in ("connection", "transfer-encoding"):
            continue
        handler.send_header(k, v)
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
    for k, v in resp.getheaders():
        if k.lower() in ("connection", "transfer-encoding"):
            continue
        handler.send_header(k, v)
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
    for k, v in resp.getheaders():
        if k.lower() in ("connection", "transfer-encoding"):
            continue
        handler.send_header(k, v)
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


_FAULT_DISPATCH = {
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
            body = json.dumps({"status": "ok"}).encode("utf-8")
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
        _log("REQUEST " + fmt % args)

    def _forward(self, head_only=False):
        method = "HEAD" if head_only else self.command
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
            if (
                not head_only
                and method == "GET"
                and _is_large_playback_range(range_header)
            ):
                due = self.state.next_due_fault()
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
            self._passthrough(resp, head_only=head_only)
        finally:
            conn.close()

    def _passthrough(self, resp, head_only=False):
        self.send_response(resp.status, resp.reason)
        for k, v in resp.getheaders():
            if k.lower() in ("connection", "transfer-encoding"):
                continue
            self.send_header(k, v)
        self.send_header("Connection", "close")
        # BaseHTTPRequestHandler owns this connection flag.
        # pylint: disable-next=attribute-defined-outside-init
        self.close_connection = True
        self.end_headers()
        if head_only:
            resp.close()
            return
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
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
