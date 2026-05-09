"""Pytest fixtures for the extreme functional test.

All fixtures here are session-scoped because the 20-minute test runs once.
The compose_up fixture's finalizer always runs `docker compose down -v`,
so even on test failure we end with a clean machine.
"""

from __future__ import annotations

import datetime as _dt
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTREME_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = EXTREME_DIR / "compose" / "docker-compose.yml"
PROJECT_NAME = "nzbdav-extreme"

KODI_HOST_PORT = "8082"
NZBDAV_HOST_PORT = "8180"
FAULT_PROXY_DAV_HOST_PORT = "8280"
FAULT_PROXY_CONTROL_HOST_PORT = "8281"

KODI_AUTH = ("kodi", "kodi")


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kw)


def _compose(*args: str, **kw) -> subprocess.CompletedProcess:
    return _run(
        ["docker", "compose", "-p", PROJECT_NAME, "-f", str(COMPOSE_FILE), *args],
        **kw,
    )


def _existing_containers() -> list[str]:
    out = subprocess.run(
        ["docker", "compose", "-p", PROJECT_NAME, "-f", str(COMPOSE_FILE),
         "ps", "--quiet"],
        check=False, capture_output=True, text=True,
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


@pytest.fixture(scope="session")
def run_dir() -> Path:
    """Per-run report directory under docs/reports/."""
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    d = REPO_ROOT / "docs" / "reports" / f"run-{ts}"
    d.mkdir(parents=True, exist_ok=True)
    os.environ["EXTREME_RUN_DIR"] = f"run-{ts}"
    return d


@pytest.fixture(scope="session")
def env_loaded(run_dir):
    """Loads .env from EXTREME_ENV_FILE (default: ./.env)."""
    env_file = Path(os.environ.get("EXTREME_ENV_FILE", REPO_ROOT / ".env"))
    if not env_file.exists():
        pytest.fail(f"EXTREME_ENV_FILE not found: {env_file}")
    for line in env_file.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
    for required in ("HYDRA_URL", "HYDRA_API_KEY", "NNTP_USER", "NNTP_PASS",
                     "TMDB_API_KEY", "NZBDAV_API_KEY", "WEBDAV_USERNAME",
                     "WEBDAV_PASSWORD"):
        if not os.environ.get(required):
            pytest.fail(f"missing required env var: {required}")
    return env_file


@pytest.fixture(scope="session")
def compose_up(env_loaded, run_dir):
    """Build and bring up the stack. Always tears down."""
    if _existing_containers():
        pytest.fail(
            "previous nzbdav-extreme containers still up. Run "
            "`docker compose -p nzbdav-extreme down -v` and retry."
        )
    _compose("up", "-d", "--build")
    yield
    _compose("down", "-v")


@pytest.fixture(scope="session")
def nzbdav_seeded(compose_up):
    seed = EXTREME_DIR / "scripts" / "seed_nzbdav.sh"
    env = os.environ.copy()
    env["NZBDAV_URL"] = f"http://localhost:{NZBDAV_HOST_PORT}"
    _run(["bash", str(seed)], env=env)


@pytest.fixture(scope="session")
def kodi_ready(compose_up, nzbdav_seeded):
    """Poll Kodi JSON-RPC until version returns. Asserts version major == 21."""
    url = f"http://localhost:{KODI_HOST_PORT}/jsonrpc"
    body = (
        b'{"jsonrpc":"2.0","method":"Application.GetProperties",'
        b'"params":{"properties":["version"]},"id":1}'
    )
    auth = "Basic " + __import__("base64").b64encode(b"kodi:kodi").decode()
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json", "Authorization": auth},
            )
            with urllib.request.urlopen(req, timeout=2) as r:
                payload = r.read().decode()
                if '"major":21' in payload:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    pytest.fail("Kodi 21 did not become ready within 60s")


@pytest.fixture(scope="session")
def jurialmunkey_repo_added(kodi_ready):
    script = EXTREME_DIR / "scripts" / "install_jurialmunkey_repo.sh"
    _run(["bash", str(script), "nzbdav-extreme-kodi"])


@pytest.fixture(scope="session")
def tmdbhelper_installed(jurialmunkey_repo_added):
    script = EXTREME_DIR / "scripts" / "install_tmdbhelper.sh"
    _run(["bash", str(script), f"http://localhost:{KODI_HOST_PORT}", "kodi:kodi"])


@pytest.fixture(scope="session")
def nzbdav_addon_installed(tmdbhelper_installed):
    script = EXTREME_DIR / "scripts" / "install_nzbdav_addon.sh"
    template = EXTREME_DIR / "fixtures" / "addon-settings-template.xml"
    _run(["bash", str(script), "nzbdav-extreme-kodi",
          f"http://localhost:{KODI_HOST_PORT}", "kodi:kodi", str(template)])


@pytest.fixture(scope="session")
def tmdbhelper_player_added(nzbdav_addon_installed):
    src = EXTREME_DIR / "fixtures" / "nzbdav-player.json"
    _run(["docker", "exec", "nzbdav-extreme-kodi",
          "mkdir", "-p",
          "/root/.kodi/userdata/addon_data/plugin.video.themoviedb.helper/players"])
    _run(["docker", "cp", str(src),
          "nzbdav-extreme-kodi:"
          "/root/.kodi/userdata/addon_data/plugin.video.themoviedb.helper/players/nzbdav.json"])


@pytest.fixture(scope="session")
def stack_ready(tmdbhelper_player_added):
    """Convenience marker fixture; depend on this from the test body."""
    return True
