"""Full-stack tests — run against docker-compose.full-test.yml.

Requires all four services to be up:
  nzbdav-rs  http://localhost:8180
  NZBHydra2  http://localhost:5076
  Kodi       http://localhost:8080/jsonrpc
  VNC        http://localhost:6901  (not checked here)

Run via:
  just functional-test-full
  # or directly:
  .venv/bin/python -m pytest tests/test_full_stack.py -v -m functional
"""

import socket
import time

import pytest

pytestmark = pytest.mark.functional

requests = pytest.importorskip("requests")

KODI_RPC = "http://localhost:8080/jsonrpc"
KODI_AUTH = ("kodi", "kodi")
NZBDAV_URL = "http://localhost:8180"
NZBDAV_API_KEY = "testkey-dev"
HYDRA_URL = "http://localhost:5076"


def _rpc(method, params=None):
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params:
        payload["params"] = params
    r = requests.post(KODI_RPC, json=payload, auth=KODI_AUTH, timeout=10)
    r.raise_for_status()
    return r.json()


def _wait_http(url, timeout=120, interval=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(url, timeout=5)
            return True
        except Exception:
            time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# nzbdav-rs
# ---------------------------------------------------------------------------


def test_nzbdav_is_up():
    assert _wait_http(f"{NZBDAV_URL}/ui", timeout=60), "nzbdav /ui did not respond"


def test_nzbdav_api_version():
    r = requests.get(
        f"{NZBDAV_URL}/api",
        params={"mode": "version", "apikey": NZBDAV_API_KEY},
        timeout=10,
    )
    assert r.status_code == 200
    data = r.json()
    assert "version" in data


def test_nzbdav_webdav_root():
    r = requests.request(
        "PROPFIND",
        f"{NZBDAV_URL}/dav",
        auth=("admin", "devpass"),
        headers={"Depth": "1"},
        timeout=10,
    )
    assert r.status_code in (200, 207), f"WebDAV PROPFIND returned {r.status_code}"


# ---------------------------------------------------------------------------
# NZBHydra2
# ---------------------------------------------------------------------------


def test_hydra_is_up():
    assert _wait_http(f"{HYDRA_URL}/", timeout=120), "NZBHydra2 did not respond"


def test_hydra_responds():
    r = requests.get(f"{HYDRA_URL}/", timeout=10, allow_redirects=True)
    assert r.status_code in (200, 302, 401)


# ---------------------------------------------------------------------------
# Kodi
# ---------------------------------------------------------------------------


def test_kodi_is_up():
    assert _wait_http(KODI_RPC, timeout=120), "Kodi JSON-RPC did not respond"


def test_kodi_version():
    result = _rpc("Application.GetProperties", {"properties": ["version"]})
    assert "result" in result
    assert result["result"]["version"]["major"] >= 18


def _kodi_major_version():
    result = _rpc("Application.GetProperties", {"properties": ["version"]})
    return result.get("result", {}).get("version", {}).get("major", 0)


def test_kodi_addon_installed():
    # linuxserver/kodi-headless only provides Kodi 18 (xbmc.python 2.26).
    # The addon requires xbmc.python 3.0.0 (Kodi 19+) so it won't load on 18.
    if _kodi_major_version() < 19:
        pytest.skip("addon requires Kodi 19+ (xbmc.python 3.0.0); container is Kodi 18")
    result = _rpc("Addons.GetAddons", {"type": "xbmc.addon.video"})
    addons = result.get("result", {}).get("addons", [])
    found = any(a.get("addonid") == "plugin.video.nzbdav" for a in addons)
    assert found, "plugin.video.nzbdav not found in Kodi addons"


def test_kodi_addon_enabled():
    if _kodi_major_version() < 19:
        pytest.skip("addon requires Kodi 19+ (xbmc.python 3.0.0); container is Kodi 18")
    result = _rpc(
        "Addons.GetAddonDetails",
        {"addonid": "plugin.video.nzbdav", "properties": ["enabled"]},
    )
    assert result.get("result", {}).get("addon", {}).get("enabled") is True


# ---------------------------------------------------------------------------
# Cross-service
# ---------------------------------------------------------------------------


def test_stream_proxy_port_reachable():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    result = sock.connect_ex(("localhost", 1995))
    sock.close()
    if result != 0:
        pytest.skip("stream proxy not running (expected unless addon service started)")
