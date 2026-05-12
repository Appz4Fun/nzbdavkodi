# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Bootstrap the bundled Rust orchestrator binary.

Phase 0 of docs/rust-migration-plan.md ships the orchestrator binary
inside the addon zip under ``plugin.video.nzbdav/bin/``. This module is
responsible for:

  * Picking the right binary for the running CPU architecture.
  * Copying it to ``addon_data/bin/orchestrator`` (so it survives addon
    upgrades that wipe the install dir and so it lives on a writable
    mount on CoreELEC).
  * Setting the executable bit.
  * Spawning it as a child process under ``service.py``'s lifetime.
  * Reading the port the binary bound (via ``--addr-file``).

The whole subsystem is gated on the ``use_orchestrator`` setting; when
that flag is false the bootstrap is a no-op and the existing Python
code path runs unchanged. Phase 0 only exercises ``/v1/health``; later
phases route real traffic.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

import xbmc
import xbmcaddon
import xbmcvfs

_ADDON_ID = "plugin.video.nzbdav"
_ADDON_DIR_PROP = "path"

_BINARY_BASENAME = "orchestrator"

# Maps platform.machine() to the binary name inside plugin.video.nzbdav/bin/.
# The release workflow cross-compiles three targets; everything else
# falls through to None and the bootstrap reports the unsupported arch
# rather than crashing.
_BINARY_BY_MACHINE = {
    "aarch64": "orchestrator-aarch64-musl",
    "arm64": "orchestrator-aarch64-musl",
    "armv7l": "orchestrator-armv7-musl",
    "armv7": "orchestrator-armv7-musl",
    "armhf": "orchestrator-armv7-musl",
    "x86_64": "orchestrator-x86_64-musl",
    "amd64": "orchestrator-x86_64-musl",
}

# Bounded wait for --addr-file to appear. The binary writes the file
# immediately after binding the listener, so 5 s is generous for any
# realistic CoreELEC box. Beyond that the spawn is treated as failed.
_ADDR_FILE_TIMEOUT_S = 5.0
_ADDR_FILE_POLL_S = 0.1

# Health-probe timeout. Tight enough that a broken orchestrator
# doesn't stall the service tick; loose enough that a slow-booting
# binary on cold cache still answers.
_HEALTH_PROBE_TIMEOUT_S = 2.0


class OrchestratorUnavailable(RuntimeError):
    """Raised when the orchestrator could not be made available.

    Each subclass-style ``reason`` keyword is one of the failure modes
    enumerated in plan §11 — the value flows straight into the
    ``reason`` field of the ``orchestrator.error`` mirror event so a
    grep on kodi.log immediately tells which layer broke.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class OrchestratorProcess:
    """Lifecycle wrapper for the spawned orchestrator child process.

    Owned by :func:`service.py`'s ``main()``. Stopping the service
    terminates the child process — Phase 0 ties the two lifetimes
    together so there is exactly one orchestrator per Kodi session.
    """

    def __init__(self, popen: subprocess.Popen, addr: str, binary_path: str) -> None:
        self._popen = popen
        self.addr = addr  # "127.0.0.1:NNNN"
        self.binary_path = binary_path
        # Drain stdout in a daemon thread so the orchestrator can keep
        # writing log lines without blocking on a full pipe. We just
        # mirror them back into kodi.log under a fixed prefix; Loki
        # ingest will be wired up separately on the deploy hosts.
        self._reader_thread = threading.Thread(
            target=self._drain_stdout,
            name="orchestrator-stdout-reader",
            daemon=True,
        )
        self._reader_thread.start()

    @property
    def is_alive(self) -> bool:
        return self._popen.poll() is None

    def stop(self, timeout: float = 5.0) -> None:
        """Best-effort terminate-then-kill."""
        if self._popen.poll() is not None:
            return
        try:
            self._popen.terminate()
        except OSError:
            return
        try:
            self._popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                self._popen.kill()
            except OSError:
                pass
            try:
                self._popen.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

    def _drain_stdout(self) -> None:
        stream = self._popen.stdout
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                if not raw:
                    break
                # Each line is already a full JSON envelope from the
                # orchestrator. Tag it on the way through so it's easy
                # to filter in kodi.log.
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                except Exception:  # pragma: no cover - defensive
                    continue
                if line:
                    xbmc.log("NZB-DAV-ORCH: " + line, xbmc.LOGINFO)
        except Exception:  # pragma: no cover - defensive
            return


def _detect_binary_name() -> Optional[str]:
    machine = (platform.machine() or "").lower()
    return _BINARY_BY_MACHINE.get(machine)


def _addon() -> xbmcaddon.Addon:
    return xbmcaddon.Addon(_ADDON_ID)


def _bundled_binary_path() -> Optional[str]:
    """Resolve the bundled per-arch binary inside the addon install dir.

    Returns None if the running architecture is not in the supported set
    or the file is missing (e.g. the addon zip was built without
    bin/ — the dev install on a non-release branch).
    """
    name = _detect_binary_name()
    if name is None:
        return None
    addon = _addon()
    addon_dir = xbmcvfs.translatePath(addon.getAddonInfo(_ADDON_DIR_PROP))
    candidate = os.path.join(addon_dir, "bin", name)
    if os.path.isfile(candidate):
        return candidate
    return None


def _addon_data_dir() -> str:
    """Resolve the addon's writable profile directory."""
    addon = _addon()
    return xbmcvfs.translatePath(addon.getAddonInfo("profile"))


def _materialise_binary() -> str:
    """Copy the bundled binary to addon_data/bin/orchestrator.

    Done once per addon version (we compare mtime / size and skip the
    copy if the destination already matches). Returns the destination
    path. Raises :class:`OrchestratorUnavailable` if no binary matches.
    """
    bundled = _bundled_binary_path()
    if bundled is None:
        machine = platform.machine() or "unknown"
        raise OrchestratorUnavailable(
            "binary_not_bundled_for_arch",
            "no orchestrator binary bundled for arch '{}'".format(machine),
        )

    target_dir = os.path.join(_addon_data_dir(), "bin")
    try:
        os.makedirs(target_dir, exist_ok=True)
    except OSError as e:
        raise OrchestratorUnavailable(
            "addon_data_unwritable",
            "could not create {}: {}".format(target_dir, e),
        ) from e

    target = os.path.join(target_dir, _BINARY_BASENAME)

    src_stat = os.stat(bundled)
    needs_copy = True
    if os.path.isfile(target):
        try:
            dst_stat = os.stat(target)
            if dst_stat.st_size == src_stat.st_size and int(dst_stat.st_mtime) == int(
                src_stat.st_mtime
            ):
                needs_copy = False
        except OSError:
            needs_copy = True

    if needs_copy:
        try:
            shutil.copy2(bundled, target)
        except OSError as e:
            raise OrchestratorUnavailable(
                "binary_copy_failed",
                "could not copy {} -> {}: {}".format(bundled, target, e),
            ) from e

    # Make it executable for owner+group (the user under which Kodi
    # runs may differ from CoreELEC's `root`, so add group/other read
    # too — the addon_data dir is per-user anyway).
    try:
        mode = os.stat(target).st_mode
        os.chmod(
            target, mode | stat.S_IXUSR | stat.S_IRUSR | stat.S_IXGRP | stat.S_IRGRP
        )
    except OSError as e:
        raise OrchestratorUnavailable(
            "binary_chmod_failed",
            "could not chmod {}: {}".format(target, e),
        ) from e

    return target


def _spawn(binary_path: str) -> OrchestratorProcess:
    """Spawn the orchestrator with port=0 and read back the bound addr."""
    addon_data = _addon_data_dir()
    addr_file = os.path.join(addon_data, "orchestrator.addr")
    # Stale addr-file from a previous run would race the wait loop
    # below and let us read an old port. Clear it explicitly.
    try:
        os.remove(addr_file)
    except FileNotFoundError:
        pass
    except OSError:
        pass

    env = os.environ.copy()
    env.setdefault("ORCHESTRATOR_BIND", "127.0.0.1")
    env.setdefault("ORCHESTRATOR_PORT", "0")
    env["ORCHESTRATOR_ADDR_FILE"] = addr_file

    try:
        popen = subprocess.Popen(  # noqa: S603 - launching our own binary
            [binary_path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as e:
        raise OrchestratorUnavailable(
            "spawn_failed",
            "could not spawn {}: {}".format(binary_path, e),
        ) from e

    # Wait for the addr file to appear.
    deadline = time.monotonic() + _ADDR_FILE_TIMEOUT_S
    addr = None
    while time.monotonic() < deadline:
        if popen.poll() is not None:
            # Child died before binding. Stop waiting — the addr file
            # is never going to land.
            break
        try:
            with open(addr_file, "r", encoding="utf-8") as fh:
                content = fh.read().strip()
            if content:
                addr = content
                break
        except FileNotFoundError:
            pass
        except OSError:
            pass
        time.sleep(_ADDR_FILE_POLL_S)

    if addr is None:
        try:
            popen.terminate()
        except OSError:
            pass
        raise OrchestratorUnavailable(
            "addr_file_timeout",
            "orchestrator did not write {} within {}s".format(
                addr_file, _ADDR_FILE_TIMEOUT_S
            ),
        )

    return OrchestratorProcess(popen, addr, binary_path)


def is_enabled() -> bool:
    """Return whether the ``use_orchestrator`` setting is on."""
    try:
        return _addon().getSetting("use_orchestrator").lower() == "true"
    except RuntimeError:
        return False


def health_probe(addr: str) -> dict:
    """Hit ``GET http://<addr>/v1/health`` and return the parsed body.

    Raises :class:`OrchestratorUnavailable` with a structured reason
    on any failure (HTTP error, JSON parse error, missing fields) so
    callers can mirror it to a ``orchestrator.error`` event.
    """
    url = "http://{}/v1/health".format(addr)
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback only
            request, timeout=_HEALTH_PROBE_TIMEOUT_S
        ) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.URLError as e:
        raise OrchestratorUnavailable(
            "health_probe_unreachable",
            "GET {} failed: {}".format(url, e),
        ) from e
    except OSError as e:
        raise OrchestratorUnavailable(
            "health_probe_io_error",
            "GET {} failed: {}".format(url, e),
        ) from e

    if status != 200:
        raise OrchestratorUnavailable(
            "health_probe_non_200",
            "GET {} returned {}".format(url, status),
        )

    try:
        parsed = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise OrchestratorUnavailable(
            "health_probe_bad_json",
            "GET {} body was not valid JSON: {}".format(url, e),
        ) from e

    if parsed.get("status") != "ok":
        raise OrchestratorUnavailable(
            "health_probe_status_not_ok",
            "GET {} returned status={!r}".format(url, parsed.get("status")),
        )

    return parsed


def start() -> Optional[OrchestratorProcess]:
    """Bootstrap and spawn the orchestrator if enabled.

    Returns the running process on success or None if disabled. Raises
    :class:`OrchestratorUnavailable` on hard failures so the caller can
    log a structured ``orchestrator.error`` event and continue running
    the legacy Python pipeline.

    After a successful spawn the function hits ``/v1/health`` once and
    only returns the process once the probe passes — exit criterion
    for plan §6 phase 0 ("addon boots → spawns orchestrator → hits
    /v1/health"). The probe outcome is logged by the caller through
    the ``orchestrator.call`` mirror event.
    """
    if not is_enabled():
        return None
    binary = _materialise_binary()
    proc = _spawn(binary)
    try:
        health_probe(proc.addr)
    except OrchestratorUnavailable:
        proc.stop()
        raise
    return proc
