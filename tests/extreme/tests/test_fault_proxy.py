"""Unit tests for tests.extreme.fault_proxy."""
import json
import threading
import time
import urllib.request

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
