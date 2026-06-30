# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

from resources.lib.resolver import resolve


def test_resolve_aborts_on_nzbdav_failed_status(resolver_mocks):
    """When nzbdav reports job Failed, resolve() should show error dialog and
    call setResolvedUrl(False)."""
    resolver_mocks.submit.return_value = ("SABnzbd_nzo_failed", None)
    resolver_mocks.status.return_value = {"status": "Downloading", "percentage": "20"}
    resolver_mocks.history.return_value = {
        "status": "Failed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Failed",
        "name": "Failed Job",
    }

    resolve(1, {"nzburl": "http://hydra/getnzb/fail", "title": "failed.mkv"})

    resolver_mocks.plugin.setResolvedUrl.assert_called_once_with(
        1, False, resolver_mocks.gui.ListItem()
    )
    resolver_mocks.gui.Dialog.return_value.ok.assert_called_once()


def test_resolve_times_out_gracefully(resolver_mocks):
    """When polling exceeds timeout, resolve() should notify and not hang
    even if status calls return None."""
    resolver_mocks.poll.return_value = (1, 5)  # override: 5s timeout
    resolver_mocks.submit.return_value = ("SABnzbd_nzo_timeout", None)
    resolver_mocks.history.return_value = None
    resolver_mocks.probe.return_value = (False, "connection_error")
    poll_started = [False]

    def no_status(_nzo_id):
        poll_started[0] = True

    resolver_mocks.status.side_effect = no_status
    resolver_mocks.time.time.side_effect = [0.0, 6.0]

    def _fake_monotonic():
        return 6.0 if poll_started[0] else 0.0

    resolver_mocks.time.monotonic.side_effect = _fake_monotonic

    with patch("resources.lib.resolver._wait_for_abort_or_timeout", return_value=False):
        resolve(1, {"nzburl": "http://hydra/getnzb/timeout", "title": "timeout.mkv"})

    resolver_mocks.plugin.setResolvedUrl.assert_called_once_with(
        1, False, resolver_mocks.gui.ListItem()
    )
    resolver_mocks.gui.Dialog.return_value.ok.assert_called_once()


def test_resolve_aborts_on_webdav_auth_failed_when_nzbdav_apis_silent(resolver_mocks):
    """Primary C3 regression test: when both nzbdav APIs return None and
    the WebDAV probe reports auth_failed, the resolver must show the
    auth dialog and call setResolvedUrl(False) within a single poll
    iteration — not spin until the download timeout."""
    resolver_mocks.submit.return_value = ("SABnzbd_nzo_silent", None)
    # Both nzbdav APIs silent — triggers the probe branch in _poll_once.
    resolver_mocks.status.return_value = None
    resolver_mocks.history.return_value = None
    # The newly-classified auth failure case that used to be "not_found".
    resolver_mocks.probe.return_value = (False, "auth_failed")

    resolve(1, {"nzburl": "http://hydra/getnzb/silent", "title": "silent.mkv"})

    # The auth dialog fired.
    resolver_mocks.gui.Dialog.return_value.ok.assert_called_once()
    # Resolve aborted.
    resolver_mocks.plugin.setResolvedUrl.assert_called_once_with(
        1, False, resolver_mocks.gui.ListItem()
    )
    # The probe was actually reached — proves the code path the test
    # claims to cover.
    assert resolver_mocks.probe.call_count >= 1


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
def test_resolve_continues_polling_when_webdav_reachable_and_apis_silent(
    mock_find_video,
    mock_stream_url,
    mock_validate,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    mock_find_completed,
    resolver_mocks,
):
    """Complementary no-false-positive test: when the nzbdav APIs are
    silent but the WebDAV probe reports (True, None), the resolver must
    NOT fire any error dialog — it must just loop back and poll again.
    On the second iteration the history API returns Completed and the
    resolve succeeds."""
    resolver_mocks.submit.return_value = ("SABnzbd_nzo_reachable", None)
    # First iteration: both APIs silent. Second iteration: history
    # returns Completed, nzbdav's queue is also empty.
    resolver_mocks.status.return_value = None
    resolver_mocks.history.side_effect = [
        None,
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Test",
            "name": "Test",
        },
    ]
    # Probe says the server is reachable — the silent APIs are not an
    # error, the job is just not queued yet.
    resolver_mocks.probe.return_value = (True, None)
    mock_find_video.return_value = "/content/uncategorized/Test/test.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/Test/test.mkv",
        {"Authorization": "Basic dGVzdDp0ZXN0"},
    )
    mock_validate.return_value = True
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}

    resolve(1, {"nzburl": "http://hydra/getnzb/reachable", "title": "reachable.mkv"})

    # No error dialog fired — the probe's (True, None) must NOT reach
    # the auth_failed branch.
    resolver_mocks.gui.Dialog.return_value.ok.assert_not_called()
    # The probe ran on the first iteration (where both APIs were
    # silent).
    assert resolver_mocks.probe.call_count >= 1
    # The resolve landed successfully (history came back Completed on
    # the second iteration and playback was finalized).
    mock_start_prepare.assert_called_once_with(
        "http://webdav:8080/content/uncategorized/Test/test.mkv",
        {"Authorization": "Basic dGVzdDp0ZXN0"},
        fallback_sources=[],
        service_config_state=None,
    )
    mock_wait_prepare.assert_called_once_with({"state": "prepare"})
    mock_finish_playback.assert_called_once_with(
        1, {"state": "prepared"}, resume_key="reachable.mkv||", resume_seconds=0.0
    )


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
def test_resolve_surfaces_http_500_body_to_user(
    mock_find_completed,
    resolver_mocks,
):
    """End-to-end: when nzbdav returns HTTP 500 on submit, the user
    sees a dialog with nzbdav's actual error message and resolve()
    aborts cleanly via setResolvedUrl(False)."""
    resolver_mocks.submit.return_value = (
        None,
        {"status": 500, "message": "duplicate nzo_id 9b7e0ea0"},
    )

    resolve(1, {"nzburl": "http://hydra/nzb", "title": "movie.mkv"})

    # Dialog fired, resolve aborted via setResolvedUrl(False)
    resolver_mocks.gui.Dialog.return_value.ok.assert_called_once()
    resolver_mocks.plugin.setResolvedUrl.assert_called_once_with(
        1, False, resolver_mocks.gui.ListItem()
    )
    # Single submit attempt — no retry on 500
    assert resolver_mocks.submit.call_count == 1


def test_poll_until_ready_records_dead_on_job_failed(resolver_mocks):
    """When the queue API reports Failed, _poll_until_ready must record the
    nzb_url and nzo_id into the dead set (so the fallback pool won't resubmit
    the same release)."""
    from resources.lib import resolver
    from resources.lib.dead_candidates import DeadCandidates

    dead = DeadCandidates()
    # Queue API returns Failed status — drives _handle_job_status to
    # should_stop=True via the failed/deleted branch.
    resolver_mocks.status.return_value = {"status": "Failed", "percentage": "0"}
    # History returns None so _handle_history_result short-circuits early and
    # does not itself trigger a stop before we reach the job-status check.
    resolver_mocks.history.return_value = None

    with patch("resources.lib.resolver._existing_completed_stream", return_value=None):
        with patch(
            "resources.lib.resolver._submit_nzb_with_retries", return_value="nzo_1"
        ):
            stream_url, _ = resolver._poll_until_ready(
                "http://x/dead.nzb",
                "Dead.Release",
                resolver_mocks.dialog,
                1,
                60,
                dead=dead,
            )

    assert stream_url is None
    assert dead.has_url("http://x/dead.nzb")
    assert dead.has_nzo("nzo_1")


def test_poll_until_ready_does_not_record_dead_on_timeout(resolver_mocks):
    """When the poll loop times out, _poll_until_ready must NOT record the
    release as dead — a timeout is a transient condition, not a Usenet failure."""
    from resources.lib import resolver
    from resources.lib.dead_candidates import DeadCandidates

    dead = DeadCandidates()
    # Drive the elapsed-time abort: monotonic() returns 0.0 for start_time,
    # then 10_000.0 for the first elapsed check inside the loop — well past the
    # 60 s download_timeout — so _abort_poll_before_fetch returns True on
    # iteration 1 before _poll_once is even called.
    resolver_mocks.time.monotonic.side_effect = [0.0, 10_000.0]
    resolver_mocks.history.return_value = None

    with patch("resources.lib.resolver._existing_completed_stream", return_value=None):
        with patch(
            "resources.lib.resolver._submit_nzb_with_retries", return_value="nzo_2"
        ):
            stream_url, _ = resolver._poll_until_ready(
                "http://x/slow.nzb",
                "Slow.Release",
                resolver_mocks.dialog,
                1,
                60,
                dead=dead,
            )

    assert stream_url is None
    assert not dead.has_url("http://x/slow.nzb")


def test_submit_fallback_skips_dead_and_primary_url():
    from resources.lib import resolver
    from resources.lib.dead_candidates import DeadCandidates

    dead = DeadCandidates()
    dead.add(nzb_url="http://x/dead.nzb")
    candidates = [
        {"link": "http://x/dead.nzb", "title": "Dead.Release"},
        {"link": "http://x/primary.nzb", "title": "Primary.Release"},
        {"link": "http://x/good.nzb", "title": "Good.Release"},
    ]
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False

    with patch(
        "resources.lib.resolver.find_completed_by_names", return_value={}
    ), patch("resources.lib.resolver.find_queued_by_names", return_value={}), patch(
        "resources.lib.resolver.submit_nzb", return_value=("nzo_good", None)
    ) as submit:
        jobs = resolver._submit_fallback_candidates(
            candidates,
            monitor,
            dead=dead,
            primary_nzb_url="http://x/primary.nzb",
        )

    submitted_urls = [call.args[0] for call in submit.call_args_list]
    assert submitted_urls == ["http://x/good.nzb"]
    assert [j["nzb_url"] for j in jobs] == ["http://x/good.nzb"]


def test_submit_fallback_records_dead_on_provable_submit_error():
    from resources.lib import resolver
    from resources.lib.dead_candidates import DeadCandidates

    dead = DeadCandidates()
    candidates = [{"link": "http://x/bad.nzb", "title": "Bad.Release"}]
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False

    with patch(
        "resources.lib.resolver.find_completed_by_names", return_value={}
    ), patch("resources.lib.resolver.find_queued_by_names", return_value={}), patch(
        "resources.lib.resolver.submit_nzb",
        return_value=(None, {"status": 500, "message": "boom"}),
    ):
        jobs = resolver._submit_fallback_candidates(candidates, monitor, dead=dead)

    assert not jobs
    assert dead.has_url("http://x/bad.nzb")


def test_submit_fallback_does_not_record_dead_on_timeout():
    from resources.lib import resolver
    from resources.lib.dead_candidates import DeadCandidates

    dead = DeadCandidates()
    candidates = [{"link": "http://x/slow.nzb", "title": "Slow.Release"}]
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False

    with patch(
        "resources.lib.resolver.find_completed_by_names", return_value={}
    ), patch("resources.lib.resolver.find_queued_by_names", return_value={}), patch(
        "resources.lib.resolver.submit_nzb",
        return_value=(None, {"status": "timeout", "message": "slow"}),
    ), patch(
        "resources.lib.resolver._adopt_queued_or_completed_job", return_value=None
    ):
        resolver._submit_fallback_candidates(candidates, monitor, dead=dead)

    assert not dead.has_url("http://x/slow.nzb")


def test_playback_fallback_sources_excludes_dead_nzo():
    from resources.lib import resolver
    from resources.lib.dead_candidates import DeadCandidates

    dead = DeadCandidates()
    dead.add(nzo_id="nzo_dead")
    jobs = [
        {"nzo_id": "nzo_dead", "title": "Dead", "nzb_url": "http://x/d.nzb"},
        {"nzo_id": "nzo_ok", "title": "Ok", "nzb_url": "http://x/o.nzb"},
    ]

    sources = resolver._playback_fallback_sources_for_stream(
        "http://primary/stream", jobs, dead=dead
    )

    assert [s["nzo_id"] for s in sources] == ["nzo_ok"]


def test_playback_fallback_sources_excludes_dead_url():
    from resources.lib import resolver
    from resources.lib.dead_candidates import DeadCandidates

    dead = DeadCandidates()
    dead.add(nzb_url="http://x/d.nzb")
    jobs = [
        {"nzo_id": "nzo_a", "title": "Dead", "nzb_url": "http://x/d.nzb"},
        {"nzo_id": "nzo_ok", "title": "Ok", "nzb_url": "http://x/o.nzb"},
    ]

    sources = resolver._playback_fallback_sources_for_stream(
        "http://primary/stream", jobs, dead=dead
    )

    assert [s["nzo_id"] for s in sources] == ["nzo_ok"]


def _dialog_close_called_when_poll_raises(helper_name, callbacks):
    """Shared body: a raise in the submit/poll helper must close the locally
    created DialogProgress before propagating (no-hang invariant). The split
    into _resolve_*_submit_and_poll returns the dialog to the caller, so a raise
    before the return would otherwise leak the modal."""
    import pytest
    from resources.lib import resolver

    dialog = MagicMock()
    # Patch the exact seam both helpers call (resolver_flow._invoke_poll_until_ready)
    # rather than the inner resolver._poll_until_ready it currently delegates to, so
    # the exception path stays forced even if that delegation ever changes.
    with patch("resources.lib.resolver.xbmcgui") as gui, patch(
        "resources.lib.resolver._get_poll_settings", return_value=(1, 10)
    ), patch("resources.lib.resolver._maybe_clear_queue_before_submit"), patch(
        "resources.lib.resolver._addon_name", return_value="NZB-DAV"
    ), patch(
        "resources.lib.resolver._string", return_value="msg"
    ), patch(
        "resources.lib.resolver_flow._invoke_poll_until_ready",
        side_effect=RuntimeError("boom"),
    ):
        gui.DialogProgress.return_value = dialog
        helper = getattr(resolver, helper_name)
        with pytest.raises(RuntimeError):
            helper("http://nzb", "Title", {}, True, "", set(), None, callbacks)
    dialog.close.assert_called_once()


def test_resolve_submit_and_poll_closes_dialog_when_poll_raises():
    _dialog_close_called_when_poll_raises(
        "_resolve_submit_and_poll", (lambda nzo: None, lambda: None)
    )


def test_resolve_and_play_submit_and_poll_closes_dialog_when_poll_raises():
    _dialog_close_called_when_poll_raises(
        "_resolve_and_play_submit_and_poll", (lambda nzo: None, lambda: None, None)
    )
