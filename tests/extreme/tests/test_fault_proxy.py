"""Unit tests for tests.extreme.fault_proxy."""
import json
import socketserver
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest

from tests.extreme import fault_proxy


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
    payload = json.dumps({
        "events": [
            {"at_seconds": 60.0, "fault_type": "connection_reset"},
            {"at_seconds": 240.5, "fault_type": "http_500"},
        ],
    }).encode("utf-8")
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
    payload = json.dumps({
        "events": [{"at_seconds": 10.0, "fault_type": "kaboom"}],
    }).encode("utf-8")
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
    payload = json.dumps({
        "events": [{"fault_type": "connection_reset"}],
    }).encode("utf-8")
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
        def log_message(self, *a, **k): pass
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
    server = _Server(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def proxy_with_upstream(fake_upstream, monkeypatch, tmp_path):
    """Start the proxy + control + fake upstream; yield (proxy_url, state)."""
    monkeypatch.setattr(fault_proxy, "UPSTREAM", fake_upstream)
    monkeypatch.setattr(fault_proxy, "_upstream",
                        fault_proxy.urlsplit(fake_upstream.rstrip("/")))
    monkeypatch.setattr(fault_proxy, "LOG_PATH", str(tmp_path / "full.log"))
    monkeypatch.setattr(fault_proxy, "EVENTS_PATH", str(tmp_path / "events.jsonl"))
    state = fault_proxy.ProxyState()
    handler_class = type("BoundProxyHandler", (fault_proxy.Handler,), {"state": state})
    server = fault_proxy._ThreadedHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state, tmp_path
    server.shutdown()


def _request_large_range(proxy_url):
    req = urllib.request.Request(
        f"{proxy_url}/dav/content/movie.mkv",
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
