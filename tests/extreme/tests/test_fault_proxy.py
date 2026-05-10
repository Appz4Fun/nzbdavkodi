"""Unit tests for tests.extreme.fault_proxy."""

# pylint: disable=no-name-in-module,redefined-outer-name

import http.client
import json
import socketserver
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest

from tests.extreme import fault_proxy

_IncompleteRead = http.client.IncompleteRead


@pytest.fixture
def control_server():
    """Start the proxy's control endpoint on an ephemeral port; yield base URL."""
    state = fault_proxy.ProxyState()
    server = fault_proxy.start_control_server(state, host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, state
    server.shutdown()
    server.server_close()


def test_control_health_returns_ok(control_server):
    base, _ = control_server
    with urllib.request.urlopen(f"{base}/control/health", timeout=2) as r:
        assert r.status == 200
        body = json.loads(r.read())
        assert body["status"] == "ok"


def test_control_schedule_accepts_events(control_server):
    base, state = control_server
    payload = json.dumps(
        {
            "events": [
                {"at_seconds": 60.0, "fault_type": "connection_reset"},
                {"at_seconds": 240.5, "fault_type": "http_500"},
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/control/schedule",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as r:
        assert r.status == 200
    assert len(state.scheduled_events) == 2
    assert state.scheduled_events[0].fault_type == "connection_reset"
    assert state.scheduled_events[1].at_seconds == pytest.approx(240.5)


def test_control_schedule_rejects_unknown_type(control_server):
    base, _ = control_server
    payload = json.dumps(
        {
            "events": [{"at_seconds": 10.0, "fault_type": "kaboom"}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/control/schedule",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=2)
    assert exc.value.code == 400


def test_control_schedule_rejects_missing_at_seconds(control_server):
    base, _ = control_server
    payload = json.dumps(
        {
            "events": [{"fault_type": "connection_reset"}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/control/schedule",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=2)
    assert exc.value.code == 400


def test_proxy_state_replace_schedule_resets_clock(control_server):
    _, state = control_server
    state.fired_events.append({"sentinel": True})
    events = [
        fault_proxy.ScheduledEvent(at_seconds=10.0, fault_type="connection_reset"),
        fault_proxy.ScheduledEvent(at_seconds=5.0, fault_type="http_500"),
    ]
    state.replace_schedule(events)
    # Sorted ascending by at_seconds
    assert state.scheduled_events[0].at_seconds == 5.0
    assert state.scheduled_events[1].at_seconds == 10.0
    # Clock reset; fired events cleared
    assert state.fired_events == []


@pytest.fixture
def fake_upstream():
    """A tiny upstream that streams a 100MB body for any GET."""

    class _Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a, **k):
            pass

        def do_GET(self):
            self.send_response(206)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(100 * 1024 * 1024))
            end = 2097152 + 100 * 1024 * 1024 - 1
            self.send_header("Content-Range", f"bytes 2097152-{end}/999999999")
            self.end_headers()
            chunk = b"X" * 65536
            sent = 0
            while sent < 100 * 1024 * 1024:
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                sent += len(chunk)

    class _Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    server = _Server(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy_with_upstream(fake_upstream, monkeypatch, tmp_path):
    """Start the proxy + control + fake upstream; yield (proxy_url, state)."""
    monkeypatch.setattr(fault_proxy, "UPSTREAM", fake_upstream)
    monkeypatch.setattr(
        fault_proxy, "_upstream", fault_proxy.urlsplit(fake_upstream.rstrip("/"))
    )
    monkeypatch.setattr(fault_proxy, "LOG_PATH", str(tmp_path / "full.log"))
    monkeypatch.setattr(fault_proxy, "EVENTS_PATH", str(tmp_path / "events.jsonl"))
    state = fault_proxy.ProxyState()
    handler_class = type("BoundProxyHandler", (fault_proxy.Handler,), {"state": state})
    server = fault_proxy._ThreadedHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state, tmp_path
    server.shutdown()
    server.server_close()


def _request_large_range(proxy_url):
    req = urllib.request.Request(
        f"{proxy_url}/content/movie.mkv",
        headers={"Range": "bytes=2097152-"},
    )
    return urllib.request.urlopen(req, timeout=10)


def test_http_500_fault_returns_500(proxy_with_upstream):
    proxy_url, state, _ = proxy_with_upstream
    state.scheduled_events = [fault_proxy.ScheduledEvent(0.0, "http_500")]
    state.start_t = time.monotonic() - 1.0  # already due
    with pytest.raises(urllib.error.HTTPError) as exc:
        _request_large_range(proxy_url)
    assert exc.value.code == 500
    assert state.fired_events[0]["fault_type"] == "http_500"


def test_connection_reset_fault_records_state_before_close(proxy_with_upstream):
    """State is recorded before the connection is torn down.

    Because record_fired now runs before send_response, the fired_events entry
    is visible to test threads immediately after the client raises — no sleep
    required.
    """
    proxy_url, state, _ = proxy_with_upstream
    state.scheduled_events = [fault_proxy.ScheduledEvent(0.0, "connection_reset")]
    state.start_t = time.monotonic() - 1.0  # already due
    with pytest.raises(
        (urllib.error.URLError, ConnectionResetError, http.client.IncompleteRead)
    ):
        resp = _request_large_range(proxy_url)
        resp.read()
    assert state.fired_events[0]["fault_type"] == "connection_reset"


def test_slow_upstream_fault_throttles(proxy_with_upstream, monkeypatch):
    proxy_url, state, _ = proxy_with_upstream
    # Use a 1-second slow window with 64 KiB/s so the test stays fast.
    monkeypatch.setattr(fault_proxy, "SLOW_BPS", 64 * 1024)
    monkeypatch.setattr(fault_proxy, "SLOW_DURATION", 1.0)
    state.scheduled_events = [fault_proxy.ScheduledEvent(0.0, "slow_upstream")]
    state.start_t = time.monotonic() - 1.0
    t0 = time.monotonic()
    resp = _request_large_range(proxy_url)
    resp.read(96 * 1024)  # 96 KiB > one second of throttled budget
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.9, f"throttling did not slow request, elapsed={elapsed:.2f}s"
    assert state.fired_events[0]["fault_type"] == "slow_upstream"


def test_truncated_response_short_eof(proxy_with_upstream):
    proxy_url, state, _ = proxy_with_upstream
    state.scheduled_events = [fault_proxy.ScheduledEvent(0.0, "truncated_response")]
    state.start_t = time.monotonic() - 1.0
    resp = _request_large_range(proxy_url)
    declared = int(resp.headers["Content-Length"])
    # Python's http.client raises IncompleteRead when the peer closes before
    # Content-Length bytes arrive — that IS the truncation signal.  Extract the
    # partial payload from the exception so we can assert on its length.
    try:
        body = resp.read()
    except _IncompleteRead as exc:
        body = exc.partial
    assert len(body) == fault_proxy.FAIL_BYTES
    assert len(body) < declared, "body was not truncated relative to Content-Length"
    assert state.fired_events[0]["fault_type"] == "truncated_response"


def test_corrupted_bytes_modifies_payload(proxy_with_upstream):
    proxy_url, state, _ = proxy_with_upstream
    state.scheduled_events = [fault_proxy.ScheduledEvent(0.0, "corrupted_bytes")]
    state.start_t = time.monotonic() - 1.0
    resp = _request_large_range(proxy_url)
    head = resp.read(fault_proxy.FAIL_BYTES)
    # The fake upstream sent only 'X' bytes; corrupted region should contain non-'X'.
    assert head.count(b"X") < len(head), "expected at least one corrupted byte"
    diff_count = sum(1 for b in head if b != ord("X"))
    assert 1 <= diff_count <= 64, f"unexpected number of corruptions: {diff_count}"
    assert state.fired_events[0]["fault_type"] == "corrupted_bytes"


def test_passthrough_drops_unsafe_upstream_headers(proxy_with_upstream):
    handler = type(
        "HeaderRecorder",
        (),
        {
            "headers": [],
            "send_header": lambda self, name, value: self.headers.append((name, value)),
        },
    )()
    resp = type(
        "Response",
        (),
        {
            "getheaders": lambda self: [
                ("Content-Type", "video/x-matroska"),
                ("X-Bad", "good\r\nInjected: yes"),
                ("Transfer-Encoding", "chunked"),
            ]
        },
    )()

    fault_proxy._forward_upstream_headers(handler, resp)

    assert handler.headers == [("Content-Type", "video/x-matroska")]


def test_truncated_response_logs_scheduled_bytes(proxy_with_upstream, tmp_path):
    proxy_url, state, run_dir = proxy_with_upstream
    state.scheduled_events = [fault_proxy.ScheduledEvent(0.0, "truncated_response")]
    state.start_t = time.monotonic() - 1.0
    try:
        resp = _request_large_range(proxy_url)
        try:
            resp.read()
        except _IncompleteRead:
            pass
    except Exception:
        pass
    # Inspect events.jsonl
    events_path = run_dir / "events.jsonl"
    assert events_path.exists(), "events.jsonl was not written"
    lines = [
        json.loads(ln) for ln in events_path.read_text().splitlines() if ln.strip()
    ]
    matched = [e for e in lines if e.get("fault_type") == "truncated_response"]
    assert len(matched) >= 1
    assert matched[0]["scheduled_bytes"] == fault_proxy.FAIL_BYTES
