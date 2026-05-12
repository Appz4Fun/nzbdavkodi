# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Unit tests for the Rust orchestrator bootstrap module.

The bootstrap module is the bridge between ``service.py`` and the
bundled per-arch orchestrator binary. The tests below pin the
contract:

  * The ``use_orchestrator`` flag gates everything (default off ⇒
    ``start()`` returns None and never spawns anything).
  * On supported architectures the bundled binary is copied to
    addon_data/bin/orchestrator and chmod +x.
  * The bound address is read back from the ``--addr-file`` written
    by the binary.
  * Hard failures raise :class:`OrchestratorUnavailable` with a
    structured ``reason`` field (so service.py can mirror it into a
    named ``orchestrator.error`` event).
"""

from __future__ import annotations

import stat
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest
from resources.lib import orchestrator_bootstrap


@pytest.fixture
def addon(tmp_path, monkeypatch):
    """Wire xbmcaddon + xbmcvfs to point at a writable tmp dir.

    The bootstrap reads three things from Kodi:
      * ``Addon.getSetting("use_orchestrator")`` — the gate.
      * ``Addon.getAddonInfo("path")`` — install dir (bundled bin/).
      * ``Addon.getAddonInfo("profile")`` — addon_data dir.

    We override all three on a per-test MagicMock instead of leaning
    on the global conftest mock so individual tests can flip the gate
    without leaking state.
    """
    install_dir = tmp_path / "install"
    profile_dir = tmp_path / "profile"
    install_dir.mkdir()
    profile_dir.mkdir()

    addon_mock = MagicMock()
    addon_mock.getSetting.return_value = "false"

    def _info(key, *args, **kwargs):
        return {
            "id": "plugin.video.nzbdav",
            "name": "NZB-DAV",
            "version": "0.0.0",
            "path": str(install_dir),
            "profile": str(profile_dir),
        }.get(key, "")

    addon_mock.getAddonInfo.side_effect = _info

    xbmcaddon = sys.modules["xbmcaddon"]
    monkeypatch.setattr(xbmcaddon, "Addon", MagicMock(return_value=addon_mock))

    xbmcvfs = sys.modules["xbmcvfs"]
    monkeypatch.setattr(xbmcvfs, "translatePath", lambda p: p)

    return addon_mock, install_dir, profile_dir


def test_disabled_returns_none(addon):
    addon_mock, _install, _profile = addon
    addon_mock.getSetting.return_value = "false"
    assert orchestrator_bootstrap.start() is None


def test_enabled_missing_binary_raises_unavailable(addon, monkeypatch):
    """When the gate is on but no per-arch binary is bundled we raise.

    The error must carry a structured ``reason`` so service.py can
    emit the §11 ``orchestrator.error`` event with a stable string.
    """
    addon_mock, _install, _profile = addon
    addon_mock.getSetting.return_value = "true"
    # Pretend we're on an unsupported arch so the lookup misses.
    monkeypatch.setattr(orchestrator_bootstrap.platform, "machine", lambda: "sparc64")

    with pytest.raises(orchestrator_bootstrap.OrchestratorUnavailable) as excinfo:
        orchestrator_bootstrap.start()
    assert excinfo.value.reason == "binary_not_bundled_for_arch"


def test_addr_file_timeout_kills_child(addon, monkeypatch):
    """A child that binds but never writes the addr file is killed."""
    addon_mock, install, _profile = addon
    addon_mock.getSetting.return_value = "true"
    monkeypatch.setattr(orchestrator_bootstrap.platform, "machine", lambda: "x86_64")

    bin_dir = install / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "orchestrator-x86_64-musl"
    binary.write_bytes(b"\x7fELF stub")
    binary.chmod(0o755)

    fake_popen = MagicMock()
    fake_popen.poll.return_value = None
    fake_popen.stdout = None
    fake_popen.terminate = MagicMock()

    monkeypatch.setattr(
        orchestrator_bootstrap.subprocess, "Popen", MagicMock(return_value=fake_popen)
    )
    monkeypatch.setattr(orchestrator_bootstrap, "_ADDR_FILE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(orchestrator_bootstrap, "_ADDR_FILE_POLL_S", 0.05)

    with pytest.raises(orchestrator_bootstrap.OrchestratorUnavailable) as excinfo:
        orchestrator_bootstrap.start()

    assert excinfo.value.reason == "addr_file_timeout"
    fake_popen.terminate.assert_called_once()


def test_successful_spawn_returns_process_with_addr(addon, monkeypatch):
    """End-to-end happy path with a faked Popen + addr-file."""
    addon_mock, install, profile = addon
    addon_mock.getSetting.return_value = "true"
    monkeypatch.setattr(orchestrator_bootstrap.platform, "machine", lambda: "x86_64")

    bin_dir = install / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "orchestrator-x86_64-musl"
    binary.write_bytes(b"\x7fELF stub")

    fake_popen = MagicMock()
    fake_popen.poll.return_value = None
    fake_popen.stdout = None

    def _spawn_writes_addr_file(*args, **kwargs):
        env = kwargs.get("env", {})
        addr_file = env.get("ORCHESTRATOR_ADDR_FILE")

        # Write asynchronously so the polling wait in _spawn exercises.
        def _writer():
            time.sleep(0.05)
            with open(addr_file, "w", encoding="utf-8") as fh:
                fh.write("127.0.0.1:54321\n")

        threading.Thread(target=_writer, daemon=True).start()
        return fake_popen

    monkeypatch.setattr(
        orchestrator_bootstrap.subprocess,
        "Popen",
        MagicMock(side_effect=_spawn_writes_addr_file),
    )

    proc = orchestrator_bootstrap.start()
    assert proc is not None
    assert proc.addr == "127.0.0.1:54321"

    # Materialised binary should be at addon_data/bin/orchestrator with +x.
    materialised = profile / "bin" / "orchestrator"
    assert materialised.is_file()
    assert materialised.stat().st_mode & stat.S_IXUSR


def test_stop_is_idempotent(addon, monkeypatch):
    """Calling .stop() twice on an already-exited child must not raise."""
    addon_mock, install, _profile = addon
    addon_mock.getSetting.return_value = "true"
    monkeypatch.setattr(orchestrator_bootstrap.platform, "machine", lambda: "x86_64")

    bin_dir = install / "bin"
    bin_dir.mkdir()
    (bin_dir / "orchestrator-x86_64-musl").write_bytes(b"\x7fELF")

    fake_popen = MagicMock()
    # poll() returns None first (alive), then a real exit code after .stop().
    fake_popen.poll.side_effect = [None, 0, 0, 0]
    fake_popen.stdout = None

    def _spawn(*args, **kwargs):
        env = kwargs.get("env", {})
        with open(env["ORCHESTRATOR_ADDR_FILE"], "w", encoding="utf-8") as fh:
            fh.write("127.0.0.1:1\n")
        return fake_popen

    monkeypatch.setattr(
        orchestrator_bootstrap.subprocess, "Popen", MagicMock(side_effect=_spawn)
    )
    proc = orchestrator_bootstrap.start()

    proc.stop()
    # Second stop should short-circuit on poll() returning a code, no
    # additional terminate/kill calls.
    proc.stop()
    assert fake_popen.terminate.call_count <= 1
