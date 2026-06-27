# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Mock Kodi modules for testing outside of Kodi."""

import contextlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.kodi_mocks import install_kodi_mocks

install_kodi_mocks()


@pytest.fixture(autouse=True)
def _reap_readahead_threads():
    """Stop any read-ahead prefetch daemon a test left running.

    ``prepare_stream()`` spawns a per-session ``nzbdav-readahead`` daemon
    thread (default on via ``readahead_buffer_mb``). In production
    ``_cleanup_session`` stops it and ``waitForAbort`` really blocks; in the
    test harness neither happens, so without this reaper one such daemon
    leaks per ``prepare_stream`` test and they accumulate across the suite,
    adding timer/GIL load that flakes timing-sensitive tests (e.g. the
    byte-0 prefetch 0.08s deadline). After each test, signal abort (so the
    loop's ``waitForAbort`` returns True and it exits) and join briefly,
    then restore the monitor defaults. Cheap no-op when none were spawned.
    """
    yield
    import threading

    leftover = [
        t
        for t in threading.enumerate()
        if t.name == "nzbdav-readahead" and t.is_alive()
    ]
    if not leftover:
        return
    monitor = sys.modules["xbmc"].Monitor.return_value
    saved_side = monitor.waitForAbort.side_effect
    saved_ret = monitor.waitForAbort.return_value
    monitor.waitForAbort.side_effect = lambda timeout=0.0: True
    try:
        for thread in leftover:
            thread.join(timeout=2)
    finally:
        monitor.waitForAbort.side_effect = saved_side
        monitor.waitForAbort.return_value = saved_ret


@pytest.fixture(autouse=True)
def _suppress_readahead_daemon(request):
    """Skip spawning the read-ahead prefetch daemon in tests that don't target it.

    ``prepare_stream`` spawns a per-session ``nzbdav-readahead`` daemon (read-ahead
    is on by default). Because ``Monitor.waitForAbort`` REALLY sleeps in this
    harness, that daemon backs off on real 0.25 s / 1.0 s sleeps: the autouse reaper
    above then pays ~1.5 s per ``prepare_stream`` test joining it (≈30 s across the
    suite), and any daemon that outlives the reaper's join races sibling tests'
    patched ``urlopen`` — the root of the nondeterministic
    ``test_prevalidated_fallback_reuses_current_probe`` full-suite flake. No general
    test asserts the daemon spawned (``_serve_proxy`` never spawns it; only
    ``prepare_stream`` does), so no-op the spawn by default.

    Exemptions keep the REAL daemon running:
      * ``real_readahead`` — unit tests that exercise the spawn directly.
      * ``functional`` / ``integration`` / ``extreme`` — the live/dev-box suites
        (``just functional-test`` etc.) exist to catch real prefetch/cutover races
        against actual playback; users get read-ahead by default, so suppressing it
        there would hide exactly what those suites validate. They are excluded from
        the default ``just test`` run, so this does not affect the fast unit suite's
        speed-up or de-flaking.
    """
    keep_real_daemon = ("real_readahead", "functional", "integration", "extreme")
    if any(request.node.get_closest_marker(marker) for marker in keep_real_daemon):
        yield
        return
    from resources.lib import stream_proxy

    with patch.object(
        stream_proxy.StreamProxy,
        "_start_readahead_prefetch",
        lambda self, ctx: None,
    ):
        yield


@pytest.fixture
def resolver_mocks():
    """Patch the dependencies that nearly every resolver test needs.

    Before this fixture, ``tests/test_resolver_errors.py`` stacked
    6-13 ``@patch`` decorators per test function (plus the fiddly
    argument-order that comes with decorator patching). Every test
    also re-built the same ``DialogProgress`` / ``Monitor`` / time
    scaffolding. This fixture consolidates the common set and
    exposes the mocks as a namespace so tests can customize return
    values without re-threading decorator arguments.

    Defaults mirror the v0.6.20 lesson (pin ``time.time()`` to 0.0
    so elapsed stays well under the download timeout) and the
    1s-poll / 60s-timeout values used by almost every test.
    """
    with contextlib.ExitStack() as stack:
        xbmc_mock = stack.enter_context(patch("resources.lib.resolver.xbmc"))
        gui_mock = stack.enter_context(patch("resources.lib.resolver.xbmcgui"))
        plugin_mock = stack.enter_context(patch("resources.lib.resolver.xbmcplugin"))
        submit_mock = stack.enter_context(patch("resources.lib.resolver.submit_nzb"))
        poll_mock = stack.enter_context(
            patch("resources.lib.resolver._get_poll_settings")
        )
        status_mock = stack.enter_context(
            patch("resources.lib.resolver.get_job_status")
        )
        history_mock = stack.enter_context(
            patch("resources.lib.resolver.get_job_history")
        )
        time_mock = stack.enter_context(patch("resources.lib.resolver.time"))
        probe_mock = stack.enter_context(
            patch("resources.lib.resolver.probe_webdav_reachable")
        )

        dialog = MagicMock()
        dialog.iscanceled.return_value = False
        gui_mock.DialogProgress.return_value = dialog

        monitor = MagicMock()
        monitor.waitForAbort.return_value = False
        xbmc_mock.Monitor.return_value = monitor

        poll_mock.return_value = (1, 60)
        time_mock.time.return_value = 0.0
        # resolver._poll_until_ready uses time.monotonic for elapsed-time
        # tracking. Default to 0.0 so tests that don't override it stay
        # below the timeout; tests that need a stale clock override
        # `resolver_mocks.time.monotonic.side_effect`.
        time_mock.monotonic.return_value = 0.0

        yield SimpleNamespace(
            xbmc=xbmc_mock,
            gui=gui_mock,
            plugin=plugin_mock,
            submit=submit_mock,
            poll=poll_mock,
            status=status_mock,
            history=history_mock,
            time=time_mock,
            probe=probe_mock,
            dialog=dialog,
            monitor=monitor,
        )
