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

from typing import NamedTuple

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


class PollContext(NamedTuple):
    """Optional hooks/hints threaded from resolve entry into the poll loop."""

    on_primary_submitted: object = None
    on_existing_completed: object = None
    completed_job_hint: object = None
    completed_job_lookup_done: bool = False
    settings_getter: object = None
    selected_indexer: str = ""
    rejected_completed_ids: object = None
    download_pubdate: object = None
    download_size: object = None
    dead: object = None
    requested_episode: object = None
    episode_context: object = None


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


def _poll_observation_unavailable(job_status, history, webdav_error):
    """Return whether every backend observation failed for this poll."""
    return (
        job_status is None
        and history is None
        and webdav_error in ("server_error", "connection_error")
    )


def _surface_poll_observation_timeout(nzo_id, settings_getter):
    """Surface a bounded backend-observation failure without cancelling the job."""
    message = _resolver._string(_resolver._ERROR_MESSAGES["connection_error"])
    _resolver.xbmc.log(
        "NZB-DAV: Backend state remained unavailable for {}s; "
        "stopping local poll for nzo_id={} without cancelling the remote "
        "job".format(_resolver._POLL_OBSERVABILITY_TIMEOUT_SECONDS, nzo_id),
        _resolver.xbmc.LOGERROR,
    )
    if settings_getter is None:
        _resolver.xbmcgui.Dialog().ok(_resolver._addon_name(), message)
    else:
        _resolver._notify(_resolver._addon_name(), message, 5000)


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


def _submit_and_announce(nzb_url, title, dialog, monitor, poll_ctx):
    """Submit the NZB (with retries) and fire the primary-submitted hook.

    Returns nzo_id or None. Extracted verbatim from _poll_until_ready."""
    nzo_id = _resolver._submit_nzb_with_retries(
        nzb_url,
        title,
        dialog,
        monitor,
        settings_getter=poll_ctx.settings_getter,
        selected_indexer=poll_ctx.selected_indexer,
        rejected_completed_ids=poll_ctx.rejected_completed_ids,
    )
    if not nzo_id:
        return None
    _notify_primary_submitted(poll_ctx.on_primary_submitted, nzo_id)
    return nzo_id


def _poll_until_ready(
    nzb_url, title, dialog, poll_interval, download_timeout, poll_ctx=None
):
    """Submit NZB and poll until download completes.

    Returns ``(stream_url, stream_headers)`` on success, or ``(None, None)``
    on failure (timeout, cancellation, server error, etc.).  All user
    notifications are issued inside this function; the caller only needs to
    decide what to do with the resulting stream URL. ``poll_ctx`` bundles the
    optional hooks/hints; ``None`` means ``PollContext()`` (all defaults).
    """
    poll_ctx = poll_ctx or PollContext()
    # Completed rows the body probe rejects (Completed but mid-file body
    # unavailable) are collected here so neither the submit/adoption path nor
    # the poll-loop by-name fallback re-adopts the very row we just rejected
    # and bypasses the intended re-download. The caller may pass a shared set
    # so a picker-probe rejection (recorded before this call) is honored too.
    if poll_ctx.rejected_completed_ids is None:
        poll_ctx = poll_ctx._replace(rejected_completed_ids=set())
    existing_kwargs = dict(
        on_existing_completed=poll_ctx.on_existing_completed,
        completed_job_hint=poll_ctx.completed_job_hint,
        completed_job_lookup_done=poll_ctx.completed_job_lookup_done,
        settings_getter=poll_ctx.settings_getter,
        rejected_completed_ids=poll_ctx.rejected_completed_ids,
        download_size=poll_ctx.download_size,
    )
    if poll_ctx.episode_context is not None:
        existing_kwargs["episode_context"] = poll_ctx.episode_context
    elif poll_ctx.requested_episode is not None:
        existing_kwargs["requested_episode"] = poll_ctx.requested_episode
    existing_stream = _resolver._existing_completed_stream(title, **existing_kwargs)
    if existing_stream is not None:
        return existing_stream

    monitor = _resolver.xbmc.Monitor()
    # Wall-clock submit timestamp (epoch seconds). The by-name history
    # fallback in _poll_once compares this against a slot's ``completed``
    # epoch to suppress stale prior-attempt false positives on resubmit.
    submit_started_wall = _resolver.time.time()
    nzo_id = _submit_and_announce(nzb_url, title, dialog, monitor, poll_ctx)
    if not nzo_id:
        return None, None

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
    observation_error_started = None

    def _mark_dead(nzo):
        if poll_ctx.dead is not None:
            poll_ctx.dead.add(nzb_url=nzb_url, nzo_id=nzo)

    def _run_one_poll(elapsed):
        """Run one poll iteration.

        Returns the ``(stream_url, stream_headers)`` tuple to return from
        ``_poll_until_ready``, or ``_POLL_CONTINUE`` to keep looping.
        """
        nonlocal last_status, no_video_retries, near_complete_fast_repolls
        nonlocal observation_error_started
        job_status, history, webdav_error = _resolver._poll_once(
            nzo_id,
            title,
            monitor,
            settings_getter=poll_ctx.settings_getter,
            submit_started_wall=submit_started_wall,
            rejected_completed_ids=poll_ctx.rejected_completed_ids,
        )

        should_stop, last_status = _resolver._handle_job_status(
            job_status, nzo_id, dialog, last_status
        )
        if should_stop:
            _mark_dead_on_terminal_job_status(job_status, nzo_id, _mark_dead)
            return None, None

        history_kwargs = {
            "monitor": monitor,
            "settings_getter": poll_ctx.settings_getter,
            "modal_failures": poll_ctx.settings_getter is None,
            "download_size": poll_ctx.download_size,
        }
        if poll_ctx.episode_context is not None:
            history_kwargs["episode_context"] = poll_ctx.episode_context
        elif poll_ctx.requested_episode is not None:
            history_kwargs["requested_episode"] = poll_ctx.requested_episode
        should_stop, stream_url, stream_headers, no_video_retries = (
            _resolver._handle_history_result(
                history, title, no_video_retries, max_no_video_retries, **history_kwargs
            )
        )
        if stream_url:
            _record_download_soft(
                title, poll_ctx.download_pubdate, poll_ctx.download_size
            )
            return stream_url, stream_headers
        if should_stop:
            _mark_dead_on_failed_history(history, nzo_id, _mark_dead)
            return None, None

        if _poll_observation_unavailable(job_status, history, webdav_error):
            if observation_error_started is None:
                observation_error_started = elapsed
            if (
                elapsed - observation_error_started
                >= _resolver._POLL_OBSERVABILITY_TIMEOUT_SECONDS
            ):
                _surface_poll_observation_timeout(nzo_id, poll_ctx.settings_getter)
                return None, None
        else:
            observation_error_started = None

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
        return _wait_between_polls(
            monitor, wait_seconds, nzo_id, poll_ctx.settings_getter
        )

    while True:
        iteration += 1
        elapsed = _resolver.time.monotonic() - start_time
        if _resolver._abort_poll_before_fetch(
            iteration, elapsed, download_timeout, dialog, nzo_id, title
        ):
            return None, None
        result = _run_one_poll(elapsed)
        if result is not _resolver._POLL_CONTINUE:
            return result
