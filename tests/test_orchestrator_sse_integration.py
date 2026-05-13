# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Runtime Python ↔ Rust SSE integration coverage.

Run with: ``just test-integration``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from resources.lib import orchestrator_client

pytestmark = pytest.mark.integration


def test_python_tails_real_rust_resolve_sse_cache_hit(tmp_path, monkeypatch):
    """Python should receive events from a live Rust SSE endpoint.

    The cache-hit path keeps this test local and deterministic: the
    Rust server reads a seeded peer-pool row, ``POST /v1/resolve``
    appends a request-scoped ``resolve.cache_hit`` event, and the
    Python client tails that event over real HTTP SSE.
    """
    if shutil.which("cargo") is None:
        pytest.skip("cargo is required for the Rust orchestrator integration test")

    profile = tmp_path / "profile"
    profile.mkdir()
    peer_pool_db = tmp_path / "peer_pool.sqlite3"
    cache_key = "v1:integration-cache-key"
    _seed_cached_peer_pool(peer_pool_db, cache_key)
    proc = _start_orchestrator(tmp_path, profile, peer_pool_db)
    try:
        _install_addon_profile(profile, monkeypatch)
        settings = {
            "use_orchestrator": "true",
            "nzbdav_url": "http://127.0.0.1:9",
            "nzbdav_api_key": "unused",
            "webdav_url": "http://127.0.0.1:9",
        }

        stop = threading.Event()
        received = threading.Event()
        events = []
        tail_result = {}
        monkeypatch.setattr(orchestrator_client, "_ORCH_EVENT_TIMEOUT_S", 0.25)
        monkeypatch.setattr(orchestrator_client, "_ORCH_EVENT_RECONNECT_DELAY_S", 0.01)

        def _settings(key, default=""):
            return settings.get(key, default)

        def _on_event(event):
            events.append(event)
            if event.get("event") == "resolve.cache_hit":
                received.set()

        def _tail():
            tail_result["reason"] = orchestrator_client.tail_resolve_events(
                "01PYINTEGRATION",
                _on_event,
                settings_getter=_settings,
                stop_event=stop,
            )

        thread = threading.Thread(target=_tail, daemon=True)
        thread.start()

        stream_url, stream_headers, reason = (
            orchestrator_client.resolve_via_orchestrator(
                "http://127.0.0.1:9/nzb/primary",
                "Integration.2026.1080p-GROUP",
                peer_pool_cache_key=cache_key,
                resolve_id="01PYINTEGRATION",
                settings_getter=_settings,
            )
        )

        assert reason is None
        assert stream_url == "http://webdav/content/Integration.mkv"
        assert stream_headers == {}
        assert received.wait(5), events
        cache_events = [
            event for event in events if event.get("event") == "resolve.cache_hit"
        ]
        assert cache_events
        assert cache_events[-1]["resolve_id"] == "01PYINTEGRATION"
        assert cache_events[-1]["payload"]["cached_resolve_id"] == "01CACHEDRUST"
        assert cache_events[-1]["payload"]["cache_key"] == cache_key

        stop.set()
        thread.join(3)
        assert not thread.is_alive()
        assert tail_result.get("reason") is None
    finally:
        _stop_process(proc)


def _install_addon_profile(profile, monkeypatch):
    addon_mock = MagicMock()
    addon_mock.getSetting.return_value = ""

    def _info(key, *args, **kwargs):
        return {
            "id": "plugin.video.nzbdav",
            "name": "NZB-DAV",
            "version": "0.0.0",
            "profile": str(profile),
        }.get(key, "")

    addon_mock.getAddonInfo.side_effect = _info
    monkeypatch.setattr(
        sys.modules["xbmcaddon"], "Addon", MagicMock(return_value=addon_mock)
    )
    monkeypatch.setattr(sys.modules["xbmcvfs"], "translatePath", lambda p: p)


def _seed_cached_peer_pool(peer_pool_db, cache_key):
    response = {
        "resolve_id": "01CACHEDRUST",
        "primary_peer_id": "01PRIMARY",
        "nzo_id": "nzo-cached",
        "stream_url": "http://webdav/content/Integration.mkv",
        "stream_headers": {},
        "peer_cohort": [],
        "peers": [
            {
                "peer_id": "01PRIMARY",
                "state": "ready",
                "validation_state": "byte_sample_validated_phase_3",
                "nzo_id": "nzo-cached",
                "stream_url": "http://webdav/content/Integration.mkv",
                "stream_headers": {},
                "content_length": 1234,
            }
        ],
    }
    now_ms = int(time.time() * 1000)
    conn = sqlite3.connect(peer_pool_db)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS peer_pools (
                resolve_id TEXT PRIMARY KEY,
                cache_key TEXT,
                response_json TEXT NOT NULL,
                peer_count INTEGER NOT NULL DEFAULT 0,
                ready_peer_count INTEGER NOT NULL DEFAULT 0,
                validated_peer_count INTEGER NOT NULL DEFAULT 0,
                rejected_peer_count INTEGER NOT NULL DEFAULT 0,
                updated_at_unix_ms INTEGER NOT NULL
            )
            """)
        conn.execute(
            """
            INSERT INTO peer_pools (
                resolve_id,
                cache_key,
                response_json,
                peer_count,
                ready_peer_count,
                validated_peer_count,
                rejected_peer_count,
                updated_at_unix_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                response["resolve_id"],
                cache_key,
                json.dumps(response),
                1,
                1,
                1,
                0,
                now_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _start_orchestrator(tmp_path, profile, peer_pool_db):
    repo_root = Path(__file__).resolve().parents[1]
    orchestrator_dir = repo_root / "orchestrator"
    if not orchestrator_dir.exists():
        pytest.skip("orchestrator workspace is not present")

    env = os.environ.copy()
    env.setdefault("ORCHESTRATOR_LOG", "error")
    cmd = [
        "cargo",
        "run",
        "--quiet",
        "-p",
        "orchestrator-server",
        "--bin",
        "orchestrator",
        "--",
        "--addr-file",
        str(profile / "orchestrator.addr"),
        "--indexer-store-path",
        str(tmp_path / "indexers.json"),
        "--peer-pool-db-path",
        str(peer_pool_db),
        "--peer-pool-cache-max-age-secs",
        "3600",
    ]
    proc = subprocess.Popen(  # noqa: S603 - test starts local workspace binary
        cmd,
        cwd=str(orchestrator_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 30
    addr_file = profile / "orchestrator.addr"
    while time.time() < deadline:
        if addr_file.exists() and addr_file.read_text(encoding="utf-8").strip():
            return proc
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout is not None else ""
            pytest.fail(
                "orchestrator exited before writing addr file:\n{}".format(output)
            )
        time.sleep(0.05)
    _stop_process(proc)
    pytest.fail("orchestrator did not write addr file before timeout")


def _stop_process(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
