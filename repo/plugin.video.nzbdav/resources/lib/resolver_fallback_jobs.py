# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Fallback submit-job bookkeeping: pending checks, cancel, snapshot, stop.

Cohesive helper group split out of ``resolver_fallback`` to keep every module
under Codacy's 500-NLOC file gate. References to names that live in (or are
patched via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


def _fallback_job_value(job, key, default=None):
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


def _fallback_job_pending(job):
    status = _fallback_job_value(job, "status")
    if status is None:
        status = _fallback_job_value(job, "state")
    if status is None:
        return True
    return str(status).strip().lower() not in _resolver._FALLBACK_TERMINAL_STATUSES


def _invoke_fallback_job_cancel(job, nzo_id, cancel_callable):
    """Run the first applicable cancel strategy; True if one fired."""
    if nzo_id and cancel_callable:
        cancel_callable(nzo_id)
        return True
    if hasattr(job, "cancel"):
        job.cancel()
        return True
    if hasattr(job, "abort"):
        job.abort()
        return True
    return False


def _cancel_fallback_job(state, job):
    cancel_callable = state.get("cancel_job") or _resolver.cancel_job
    if not _fallback_job_pending(job):
        return False
    nzo_id = _fallback_job_value(job, "nzo_id")
    try:
        return _invoke_fallback_job_cancel(job, nzo_id, cancel_callable)
    except Exception as error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: Failed to cancel fallback submit job {}: {}".format(
                _resolver._redact_log(nzo_id or job), _resolver._redact_log(error)
            ),
            _resolver.xbmc.LOGWARNING,
        )
    return False


def _cancel_fallback_submitted_jobs(state):
    """Cancel submitted fallback jobs that are still pending or running."""
    if not state:
        return []
    with state["lock"]:
        jobs_to_cancel = list(state["jobs"])

    cancelled = []
    for job in jobs_to_cancel:
        if _cancel_fallback_job(state, job):
            cancelled.append(job)
    return cancelled


def _await_fallback_worker_finish(thread, finished, wait_seconds):
    """Briefly wait for the fallback worker to finish, bounded by wait_seconds."""
    if not thread or wait_seconds <= 0:
        return
    deadline = _resolver.time.monotonic() + max(0, wait_seconds)
    while thread.is_alive():
        remaining = deadline - _resolver.time.monotonic()
        if remaining <= 0:
            break
        if finished and finished.wait(min(0.05, remaining)):
            break


def _fallback_submit_jobs_snapshot(state, wait_seconds=0.5):
    """Return fallback jobs submitted so far, waiting briefly for completion.

    A ``wait_for_playback`` worker (see ``_start_fallback_submit_worker``)
    gates ALL submission work behind ``state["playback_started"]``, which is
    only set by ``_signal_fallback_playback_started`` after this exact
    snapshot call returns (it runs from the same synchronous prepare/handoff
    sequence, strictly earlier). Waiting the full ``wait_seconds`` here can
    therefore never observe a job -- it is a guaranteed-timeout wait that
    only delays handoff. Skip it in that case; the worker still submits its
    real burst once playback is signaled, just as before.
    """
    if not state:
        return []
    playback_started = state.get("playback_started")
    if (
        state.get("wait_for_playback")
        and playback_started is not None
        and not playback_started.is_set()
    ):
        wait_seconds = 0
    _await_fallback_worker_finish(
        state.get("thread"), state.get("finished"), wait_seconds
    )
    with state["lock"]:
        return [dict(job) if isinstance(job, dict) else job for job in state["jobs"]]


def _stop_fallback_submit_worker(
    state,
    cancel_submitted=False,
    join_timeout=_resolver._FALLBACK_SHUTDOWN_JOIN_TIMEOUT,
):
    """Signal the fallback worker to stop and optionally cancel known jobs."""
    if not state:
        return []
    state["stop"].set()
    thread = state.get("thread")
    if thread:
        timeout = max(0, join_timeout)
        thread.join(timeout=timeout)
        if thread.is_alive():
            _resolver.xbmc.log(
                "NZB-DAV: Fallback submit worker still running after {:.2f}s; "
                "resolve shutdown is continuing".format(timeout),
                _resolver.xbmc.LOGWARNING,
            )
        else:
            state["thread"] = None
    if cancel_submitted:
        _cancel_fallback_submitted_jobs(state)
    return _resolver._fallback_submit_jobs_snapshot(state, wait_seconds=0)
