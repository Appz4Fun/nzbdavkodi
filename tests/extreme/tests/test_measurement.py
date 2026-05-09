"""Unit tests for tests.extreme.measurement."""
import json
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler

import pytest

from tests.extreme import measurement


@pytest.fixture
def mock_kodi():
    """A fake JSON-RPC server that returns Player.GetActivePlayers + Player.GetProperties."""
    state = {"speed": 1, "time_sec": 0.0}
    state_lock = threading.Lock()

    class _H(BaseHTTPRequestHandler):
        def log_message(self, *a, **k): pass
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length))
            method = req.get("method", "")
            with state_lock:
                if method == "Player.GetActivePlayers":
                    body = {"jsonrpc": "2.0", "id": req["id"],
                            "result": [{"playerid": 1, "type": "video"}]}
                elif method == "Player.GetProperties":
                    t = state["time_sec"]
                    body = {"jsonrpc": "2.0", "id": req["id"], "result": {
                        "speed": state["speed"],
                        "time": {"hours": int(t // 3600), "minutes": int((t // 60) % 60),
                                 "seconds": int(t % 60), "milliseconds": int((t * 1000) % 1000)},
                        "totaltime": {"hours": 2, "minutes": 0, "seconds": 0, "milliseconds": 0},
                        "percentage": (t / 7200.0) * 100,
                        "playcount": 0,
                    }}
                else:
                    body = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -1}}
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    class _S(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
    server = _S(("127.0.0.1", 0), _H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield (server.server_address[1], state, state_lock)
    server.shutdown()


def test_player_poller_writes_timeline(mock_kodi, tmp_path):
    port, state, lock = mock_kodi
    out = tmp_path / "timeline.jsonl"
    poller = measurement.PlayerPoller(
        url=f"http://127.0.0.1:{port}/jsonrpc",
        auth=("kodi", "kodi"),
        interval=0.05,
        output_path=out,
    )
    poller.start()
    # Advance the simulated time
    for sec in [1.0, 2.0, 3.0]:
        with lock:
            state["time_sec"] = sec
        time.sleep(0.1)
    poller.stop()
    poller.join(timeout=2)
    lines = out.read_text().strip().splitlines()
    assert len(lines) >= 3
    last = json.loads(lines[-1])
    assert "t_wall" in last and "t_run" in last
    assert last["speed"] == 1
    assert last["time_sec"] >= 1.0


def test_player_poller_survives_jsonrpc_error(mock_kodi, tmp_path):
    port, state, lock = mock_kodi
    out = tmp_path / "timeline.jsonl"
    poller = measurement.PlayerPoller(
        url=f"http://127.0.0.1:{port + 9999}/jsonrpc",  # bad port -> connection refused
        auth=("kodi", "kodi"),
        interval=0.05,
        output_path=out,
    )
    poller.start()
    time.sleep(0.2)
    poller.stop()
    poller.join(timeout=2)
    # Poller should not crash; output may be empty but file should exist after stop
    # (we accept either no file or an empty file).
    assert poller.exception_count > 0
