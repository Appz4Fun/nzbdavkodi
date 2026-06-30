# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Per-iteration poll helpers (queue/history status classification).

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


def _get_poll_settings(settings_getter=None):
    try:
        if settings_getter is None:
            addon = _resolver.xbmcaddon.Addon("plugin.video.nzbdav")
            interval_raw = addon.getSetting("poll_interval")
            timeout_raw = addon.getSetting("download_timeout")
        else:
            interval_raw = settings_getter("poll_interval", "1")
            timeout_raw = settings_getter("download_timeout", "3600")
        interval = int(interval_raw or "1")
        timeout = int(timeout_raw or "3600")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        interval = 1
        timeout = 3600
    interval = _resolver._clamp_int_setting(
        "poll_interval",
        interval,
        _resolver._POLL_INTERVAL_MIN,
        _resolver._POLL_INTERVAL_MAX,
    )
    timeout = _resolver._clamp_int_setting(
        "download_timeout",
        timeout,
        _resolver._DOWNLOAD_TIMEOUT_MIN,
        _resolver._DOWNLOAD_TIMEOUT_MAX,
    )
    return interval, timeout


def _storage_to_webdav_path(storage):
    """Convert nzbdav storage path to WebDAV content path.

    Handles server flavours that return different ``storage`` values
    in their SABnzbd history:

    * Upstream nzbdav (Node): returns a filesystem path like
      ``/mnt/nzbdav/completed-symlinks/uncategorized/Name`` or
      ``/mnt/data/completed-symlinks/uncategorized/Name``. Strip the
      mount prefix and re-root under ``/content/``.
    * nzbdav-rs (Rust port): returns the WebDAV path directly, e.g.
      ``/content/uncategorized/Name/`` or (no-category submit) just
      ``/content/Name/``. Pass through as-is with trailing slash.

    Fallback (unknown shape): take the last two path components as
    ``{category}/{name}`` under ``/content/``. Good enough for
    SABnzbd-style layouts we haven't seen yet.
    """
    # nzbdav-rs already returns a /content/... path.
    if storage.startswith("/content/"):
        return storage.rstrip("/") + "/"

    # Upstream nzbdav's completed-symlinks layout.
    for prefix in (
        "/mnt/nzbdav/completed-symlinks/",
        "/mnt/data/completed-symlinks/",
    ):
        if storage.startswith(prefix):
            relative = storage[len(prefix) :]
            return "/content/{}/".format(relative)

    # Fallback: use the last two path components (category/name).
    parts = storage.rstrip("/").split("/")
    relative = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return "/content/{}/".format(relative)


def _history_status_is_terminal(history_status):
    """Return whether a history row is enough to stop waiting on queue state."""
    if not isinstance(history_status, dict):
        return False
    return history_status.get("status") in ("Completed", "Failed")


def _queue_status_is_clearly_active(job_status):
    """Return whether queue status is enough to defer history to the next poll."""
    if not _queue_status_has_active_status(job_status):
        return False
    try:
        return float(job_status.get("percentage", 0) or 0) < 100
    except (TypeError, ValueError):
        return True


def _queue_status_has_active_status(job_status):
    """Return whether the queue row describes an active nzbdav job."""
    if not isinstance(job_status, dict):
        return False
    status = str(job_status.get("status", "") or "").strip().lower()
    return status in _resolver._ACTIVE_QUEUE_STATUSES


def _queue_status_is_nearly_complete(job_status):
    """Return whether queue progress is close enough to briefly await history."""
    if not isinstance(job_status, dict):
        return False
    try:
        percentage = float(job_status.get("percentage", 0) or 0)
    except (TypeError, ValueError):
        return False
    return percentage >= _resolver._POLL_NEAR_COMPLETE_PERCENTAGE


def _queue_status_is_late_active(job_status):
    """Return whether an active queue row is late enough to catch history."""
    if not _queue_status_has_active_status(job_status):
        return False
    try:
        percentage = float(job_status.get("percentage", 0) or 0)
    except (TypeError, ValueError):
        return False
    return percentage >= _resolver._POLL_LATE_ACTIVE_HISTORY_GRACE_PERCENTAGE


def _queue_status_history_grace_seconds(job_status):
    if not isinstance(job_status, dict):
        return _resolver._POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS
    try:
        percentage = float(job_status.get("percentage", 0) or 0)
    except (TypeError, ValueError):
        return _resolver._POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS
    if percentage >= 100.0:
        return _resolver._POLL_FULL_PROGRESS_HISTORY_GRACE_SECONDS
    return _resolver._POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS


def _poll_wait_after_status(job_status, poll_interval, fast_repolls_used):
    """Return the next poll wait and updated near-complete fast-repoll count."""
    if _queue_status_is_nearly_complete(job_status):
        if fast_repolls_used < _resolver._POLL_NEAR_COMPLETE_FAST_REPOLL_COUNT:
            return (
                min(poll_interval, _resolver._POLL_NEAR_COMPLETE_FAST_REPOLL_SECONDS),
                fast_repolls_used + 1,
            )
        return poll_interval, fast_repolls_used
    return poll_interval, 0


def _wait_for_nearly_complete_history(
    history_ready, history_done, deadline, grace_seconds=None
):
    """Give completed history a small chance to beat the next poll interval."""
    if grace_seconds is None:
        grace_seconds = _resolver._POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS
    grace_deadline = min(
        deadline,
        _resolver.time.monotonic() + max(0, grace_seconds),
    )
    while True:
        if history_ready.is_set() or history_done.is_set():
            return
        remaining = grace_deadline - _resolver.time.monotonic()
        if remaining <= 0:
            return
        history_ready.wait(min(0.01, remaining))


def _poll_clearly_active_grace_seconds(job_status):
    """Pick the history grace window for a clearly-active queue row."""
    if _queue_status_is_nearly_complete(job_status):
        return _queue_status_history_grace_seconds(job_status)
    if _queue_status_is_late_active(job_status):
        return _resolver._POLL_LATE_ACTIVE_HISTORY_GRACE_SECONDS
    return _resolver._POLL_ACTIVE_HISTORY_GRACE_SECONDS


def _poll_active_queue_grace_seconds(job_status):
    """Return the active-queue grace window, or None when no active wait applies.

    Mirrors the original two-branch decision: a "clearly active" row (active and
    below 100%) always waits a grace window; an active row at/over 100% waits
    only when it is also near-complete. Any other state returns None so the
    caller keeps looping instead of waiting.
    """
    if _queue_status_is_clearly_active(job_status):
        return _poll_clearly_active_grace_seconds(job_status)
    if _queue_status_has_active_status(job_status) and _queue_status_is_nearly_complete(
        job_status
    ):
        return _queue_status_history_grace_seconds(job_status)
    return None


def _poll_once_await_apis(
    job_status, history_ready, queue_done, history_done, deadline
):
    """Wait (up to ``deadline``) for the queue/history threads to settle.

    ``job_status`` is the single-element holder mutated by the queue thread.
    Breaks as soon as terminal history is ready, an active queue row decides
    the outcome, both APIs have returned, or the deadline elapses — preserving
    the original per-state grace-wait timing exactly.
    """
    while True:
        if history_ready.is_set():
            return
        if queue_done.is_set():
            grace = _poll_active_queue_grace_seconds(job_status[0])
            if grace is not None:
                _wait_for_nearly_complete_history(
                    history_ready, history_done, deadline, grace
                )
                return
            if history_done.is_set():
                return
        remaining = deadline - _resolver.time.monotonic()
        if remaining <= 0:
            return
        history_ready.wait(min(0.05, remaining))


def _by_name_completed_after_submit(by_name, submit_started_wall):
    """Whether a by-name slot's ``completed`` epoch is at/after this submit.

    Requires a parseable ``completed`` timestamp >= the submit start (with a 5 s
    clock-skew tolerance). A slot lacking ``completed`` entirely (older
    nzbdav-rs builds) returns False so the by-name path never fires a
    false-failure on a stale prior-attempt row.
    """
    completed = by_name.get("completed")
    try:
        completed_ts = int(completed) if completed is not None else None
    except (ValueError, TypeError):
        return False
    return (
        completed_ts is not None
        and submit_started_wall is not None
        and completed_ts >= int(submit_started_wall) - 5
    )


def _by_name_terminal_history(
    title, settings_getter, submit_started_wall, rejected_terminal_ids
):
    """Return a synthesized terminal history row from a by-name slot, or None.

    nzbdav-rs remaps the nzo_id when a job is moved from queue to history
    (keyed by name, not nzo_id). This timestamp-gated by-name fallback finds a
    Failed/Completed slot, suppressing stale prior-attempt false positives on
    resubmit (see ``_by_name_completed_after_submit``) and skipping rows already
    rejected by the body probe.
    """
    from resources.lib.nzbdav_api import find_terminal_by_name

    by_name = find_terminal_by_name(
        title, **_resolver._settings_getter_kwargs(settings_getter)
    )
    if not (
        by_name
        and by_name.get("status")
        and by_name.get("nzo_id") not in rejected_terminal_ids
    ):
        return None
    if not _by_name_completed_after_submit(by_name, submit_started_wall):
        return None
    return {
        "status": by_name.get("status", ""),
        "storage": by_name.get("storage", ""),
        "name": by_name.get("name", ""),
        "fail_message": by_name.get("fail_message", ""),
        # Thread the validated terminal timestamp through so the synthesized
        # row carries the same ``completed`` contract as a real history slot
        # (downstream consumers can re-apply the stale-row guard without it
        # being silently absent). The gate above guarantees it parses to int.
        "completed": int(by_name.get("completed")),
    }


def _poll_once(
    nzo_id,
    title,
    monitor,
    settings_getter=None,
    submit_started_wall=None,
    rejected_completed_ids=None,
):
    """Poll nzbdav queue API and history API in parallel.

    Args:
        nzo_id: nzbdav job identifier to poll.
        title: Human-readable title used for log messages.
        monitor: xbmc.Monitor instance passed through to
            probe_webdav_reachable so the probe's retry wait
            cooperates with Kodi shutdown.

    Returns:
        A tuple of (job_status, history_status, error_type):
        - job_status: Dict from the queue API when the job is active, or None
          when the job is missing from the queue.
        - history_status: Dict from the history API when the job completed, or
          None when not present.
        - error_type: None when polling succeeds; otherwise the error string
          returned by probe_webdav_reachable() when both APIs return None.
          One of "auth_failed", "server_error", or "connection_error".

    Side effects:
        Spawns two threads to call get_job_status() and get_job_history().
        Performs HTTP requests to nzbdav queue/history endpoints and, when
        neither returns data, a WebDAV reachability probe.
        Logs poll results to the Kodi log.
    """
    # Terminal rows the body probe already rejected (Completed but mid-file
    # body unavailable). The by-name fallback must not surface them, or the
    # poll loop would abort on the stale row instead of waiting for the fresh
    # re-download. Snapshot to an immutable tuple before spawning threads.
    rejected_terminal_ids = tuple(rejected_completed_ids or ())
    job_status = [None]
    history_status = [None]
    error_type = [None]
    history_ready = _resolver.threading.Event()
    queue_done = _resolver.threading.Event()
    history_done = _resolver.threading.Event()

    def check_queue():
        try:
            job_status[0] = _resolver.get_job_status(
                nzo_id, **_resolver._settings_getter_kwargs(settings_getter)
            )
        finally:
            queue_done.set()

    def check_history():
        try:
            history_status[0] = _resolver.get_job_history(
                nzo_id, **_resolver._settings_getter_kwargs(settings_getter)
            )
            # nzbdav-rs remaps the nzo_id when a job is moved from queue to
            # history (keyed by name, not nzo_id); without a name-based
            # fallback the addon would poll indefinitely for the original
            # nzo_id even though the job has landed in history.
            if history_status[0] is None and title:
                history_status[0] = _by_name_terminal_history(
                    title,
                    settings_getter,
                    submit_started_wall,
                    rejected_terminal_ids,
                )
            if _history_status_is_terminal(history_status[0]):
                history_ready.set()
        finally:
            history_done.set()

    # daemon=True so a stalled worker thread doesn't block the plugin
    # interpreter from exiting on Kodi shutdown.
    t1 = _resolver.threading.Thread(target=check_queue, daemon=True)
    t2 = _resolver.threading.Thread(target=check_history, daemon=True)
    t1.start()
    t2.start()
    # Deadline must allow for API timeout (10s) + processing overhead
    deadline = _resolver.time.monotonic() + 12
    _poll_once_await_apis(job_status, history_ready, queue_done, history_done, deadline)

    # Only probe WebDAV for errors after both APIs returned no data within the
    # bounded wait, so we don't falsely conclude the job is missing.
    if history_status[0] is None and job_status[0] is None:
        _, error = _resolver.probe_webdav_reachable(
            monitor=monitor,
            max_retries=1,
            retry_delay=1,
            settings_getter=settings_getter,
        )
        error_type[0] = error

    _resolver.xbmc.log(
        "NZB-DAV: Poll result - job_status={} history_status={} error_type={}".format(
            job_status[0], history_status[0], error_type[0]
        ),
        _resolver.xbmc.LOGDEBUG,
    )
    return job_status[0], history_status[0], error_type[0]
