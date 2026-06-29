# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""The poll-until-ready loop and its terminal-state helpers.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


def _record_download_soft(title, download_pubdate, download_size):
    """Record a confirmed-playable download, never letting bookkeeping raise."""
    # The stream is confirmed playable (the only success path). Record the
    # release's Usenet post-date keyed by title ONLY now -- never on a submit
    # that later times out / fails / is cancelled -- so the picker can
    # distinguish THIS completed download from a same-name repost posted on a
    # different day, and a failed attempt can't make a different repost's
    # same-name completed row look adopted here. Fail-soft: never let
    # bookkeeping break playback.
    try:
        _resolver.record_download(title, download_pubdate, download_size)
    except Exception as error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: download-ledger record failed (non-fatal): {}".format(error),
            _resolver.xbmc.LOGDEBUG,
        )


def _job_status_is_dead(job_status):
    return str((job_status or {}).get("status", "")).lower() in ("failed", "deleted")


def _mark_dead_on_terminal_job_status(job_status, nzo_id, mark_dead):
    """Mark the candidate dead when a terminal job status is failed/deleted."""
    if _job_status_is_dead(job_status):
        mark_dead(nzo_id)


def _mark_dead_on_failed_history(history, nzo_id, mark_dead):
    """Mark the candidate dead when the terminal history row reports Failed."""
    if (history or {}).get("status") == "Failed":
        mark_dead(nzo_id)


def _wait_between_polls(monitor, wait_seconds, nzo_id, settings_getter):
    """Wait the inter-poll interval; cancel + stop on shutdown, else continue.

    Returns ``(None, None)`` to stop ``_poll_until_ready`` (Kodi is shutting
    down) or the ``_POLL_CONTINUE`` sentinel to keep looping.
    """
    if _resolver._wait_for_abort_or_timeout(monitor, wait_seconds):
        _cancel_job_on_shutdown(nzo_id, settings_getter)
        return None, None
    return _resolver._POLL_CONTINUE


def _notify_primary_submitted(on_primary_submitted, nzo_id):
    """Fire the primary-submitted callback, never letting it break the poll loop."""
    if on_primary_submitted is None:
        return
    try:
        on_primary_submitted(nzo_id)
    except Exception as error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: Fallback submit worker start failed: {}".format(error),
            _resolver.xbmc.LOGWARNING,
        )


def _cancel_job_on_shutdown(nzo_id, settings_getter):
    """Cancel the job on Kodi shutdown, matching the settings-getter contract."""
    _resolver.xbmc.log(
        "NZB-DAV: Kodi shutdown detected, aborting resolve", _resolver.xbmc.LOGINFO
    )
    if settings_getter is None:
        _resolver.cancel_job(nzo_id)
    else:
        _resolver.cancel_job(nzo_id, settings_getter=settings_getter)


def _poll_until_ready(
    nzb_url,
    title,
    dialog,
    poll_interval,
    download_timeout,
    on_primary_submitted=None,
    on_existing_completed=None,
    completed_job_hint=None,
    completed_job_lookup_done=False,
    settings_getter=None,
    selected_indexer=None,
    rejected_completed_ids=None,
    download_pubdate=None,
    download_size=None,
    dead=None,
):
    """Submit NZB and poll until download completes.

    Returns ``(stream_url, stream_headers)`` on success, or ``(None, None)``
    on failure (timeout, cancellation, server error, etc.).  All user
    notifications are issued inside this function; the caller only needs to
    decide what to do with the resulting stream URL.
    """
    # Completed rows the body probe rejects (Completed but mid-file body
    # unavailable) are collected here so neither the submit/adoption path nor
    # the poll-loop by-name fallback re-adopts the very row we just rejected
    # and bypasses the intended re-download. The caller may pass a shared set
    # so a picker-probe rejection (recorded before this call) is honored too.
    if rejected_completed_ids is None:
        rejected_completed_ids = set()
    existing_stream = _resolver._existing_completed_stream(
        title,
        on_existing_completed=on_existing_completed,
        completed_job_hint=completed_job_hint,
        completed_job_lookup_done=completed_job_lookup_done,
        settings_getter=settings_getter,
        rejected_completed_ids=rejected_completed_ids,
        download_size=download_size,
    )
    if existing_stream is not None:
        return existing_stream

    monitor = _resolver.xbmc.Monitor()
    # Wall-clock submit timestamp (epoch seconds). The by-name history
    # fallback in _poll_once compares this against a slot's ``completed``
    # epoch to suppress stale prior-attempt false positives on resubmit.
    submit_started_wall = _resolver.time.time()
    nzo_id = _resolver._submit_nzb_with_retries(
        nzb_url,
        title,
        dialog,
        monitor,
        settings_getter=settings_getter,
        selected_indexer=selected_indexer,
        rejected_completed_ids=rejected_completed_ids,
    )
    if not nzo_id:
        return None, None
    _notify_primary_submitted(on_primary_submitted, nzo_id)

    _resolver.xbmc.log(
        "NZB-DAV: NZB submitted, nzo_id={}, polling every {}s (timeout={}s)".format(
            nzo_id, poll_interval, download_timeout
        ),
        _resolver.xbmc.LOGINFO,
    )
    # Monotonic clock for elapsed-time tracking — wall-clock NTP jumps
    # would otherwise either prematurely abort the poll loop (backward
    # jump) or stretch the configured download_timeout indefinitely
    # (forward jump). Initial submit timestamp stays on time.time() above
    # since it's logged for human consumption, not arithmetic.
    start_time = _resolver.time.monotonic()
    last_status = None
    iteration = 0
    no_video_retries = 0
    max_no_video_retries = 5
    near_complete_fast_repolls = 0

    def _mark_dead(nzo):
        if dead is not None:
            dead.add(nzb_url=nzb_url, nzo_id=nzo)

    def _run_one_poll():
        """Run one poll iteration.

        Returns the ``(stream_url, stream_headers)`` tuple to return from
        ``_poll_until_ready``, or ``_POLL_CONTINUE`` to keep looping.
        """
        nonlocal last_status, no_video_retries, near_complete_fast_repolls
        job_status, history, webdav_error = _resolver._poll_once(
            nzo_id,
            title,
            monitor,
            settings_getter=settings_getter,
            submit_started_wall=submit_started_wall,
            rejected_completed_ids=rejected_completed_ids,
        )

        should_stop, last_status = _resolver._handle_job_status(
            job_status, nzo_id, dialog, last_status
        )
        if should_stop:
            _mark_dead_on_terminal_job_status(job_status, nzo_id, _mark_dead)
            return None, None

        should_stop, stream_url, stream_headers, no_video_retries = (
            _resolver._handle_history_result(
                history,
                title,
                no_video_retries,
                max_no_video_retries,
                monitor=monitor,
                settings_getter=settings_getter,
                modal_failures=settings_getter is None,
                download_size=download_size,
            )
        )
        if stream_url:
            _record_download_soft(title, download_pubdate, download_size)
            return stream_url, stream_headers
        if should_stop:
            _mark_dead_on_failed_history(history, nzo_id, _mark_dead)
            return None, None

        if _resolver._handle_webdav_error(nzo_id, webdav_error):
            # Deliberately NOT calling cancel_job here. The WebDAV auth
            # failure is an addon-side observation problem (the addon
            # can't read the file the job produced), not a job-side
            # problem. The job is presumably running fine on nzbdav and
            # cancelling it would be destructive — the user's nzbdav UI
            # would show a vanished download for no apparent reason.
            return None, None

        wait_seconds, near_complete_fast_repolls = _resolver._poll_wait_after_status(
            job_status, poll_interval, near_complete_fast_repolls
        )
        return _wait_between_polls(monitor, wait_seconds, nzo_id, settings_getter)

    while True:
        iteration += 1
        elapsed = _resolver.time.monotonic() - start_time
        if _resolver._abort_poll_before_fetch(
            iteration, elapsed, download_timeout, dialog, nzo_id, title
        ):
            return None, None
        result = _run_one_poll()
        if result is not _resolver._POLL_CONTINUE:
            return result
