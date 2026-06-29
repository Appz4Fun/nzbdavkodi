# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Submit-with-UI-pump, adoption and retry flow.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


def _job_nzo_id(match):
    if isinstance(match, dict) and match.get("nzo_id"):
        return match["nzo_id"]
    return None


def _submit_probe_interval(probe_started):
    """Fast cadence inside the initial window, then the slower steady interval."""
    elapsed = _resolver.time.monotonic() - probe_started
    if elapsed < _resolver._SUBMIT_QUEUE_PROBE_FAST_WINDOW_SECONDS:
        return _resolver._SUBMIT_QUEUE_PROBE_FAST_INTERVAL_SECONDS
    return _resolver._SUBMIT_QUEUE_PROBE_INTERVAL_SECONDS


def _drop_rejected_completed_match(match, rejected_ids):
    """Return ``match`` unless its nzo_id was already body-probe rejected.

    The pre-submit body probe records Completed rows whose mid-file body is
    unavailable; re-adopting one would bypass the intended re-download, so drop
    it (return None) and let the caller keep probing.
    """
    if match is not None and _job_nzo_id(match) in rejected_ids:
        return None
    return match


def _start_probe_thread_or_run(target, thread_name):
    """Start ``target`` on a daemon thread, falling back to inline on failure."""
    thread = _resolver.threading.Thread(target=target, name=thread_name, daemon=True)
    try:
        thread.start()
    except RuntimeError:
        target()


def _safe_probe_by_name(find_fn, title, settings_getter, probe_label):
    """Call a find-by-name probe, logging+swallowing any error (returns None)."""
    try:
        return find_fn(title, **_resolver._settings_getter_kwargs(settings_getter))
    except Exception as e:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: concurrent {} probe raised: {}".format(probe_label, e),
            _resolver.xbmc.LOGWARNING,
        )
        return None


def _find_adoptable_job_during_submit(
    title, settings_getter=None, rejected_completed_ids=None
):
    """Return queue/history nzo_id without serializing behind a slow queue miss."""
    rejected_ids = tuple(rejected_completed_ids or ())
    result = {"nzo_id": None, "done_count": 0}
    lock = _resolver.threading.Lock()
    progress = _resolver.threading.Event()

    def _record_match(match):
        nzo_id = _job_nzo_id(match)
        with lock:
            if nzo_id and result["nzo_id"] is None:
                result["nzo_id"] = nzo_id
            result["done_count"] += 1
        progress.set()

    def _probe_queue():
        _record_match(
            _safe_probe_by_name(
                _resolver.find_queued_by_name, title, settings_getter, "queue"
            )
        )

    def _probe_history():
        # Don't re-adopt a Completed row the pre-submit body probe already
        # rejected (mid-file body unavailable); it would bypass the intended
        # re-download. See finding #7.
        _record_match(
            _drop_rejected_completed_match(
                _safe_probe_by_name(
                    _resolver.find_completed_by_name, title, settings_getter, "history"
                ),
                rejected_ids,
            )
        )

    _start_probe_thread_or_run(_probe_queue, "nzbdav-submit-queue-probe")

    progress.wait(_resolver._SUBMIT_HISTORY_PROBE_PARALLEL_GRACE_SECONDS)
    with lock:
        nzo_id = result["nzo_id"]
        done_count = result["done_count"]
    if nzo_id:
        return nzo_id
    if done_count:
        _probe_history()
        with lock:
            return result["nzo_id"]

    _start_probe_thread_or_run(_probe_history, "nzbdav-submit-history-probe")
    expected_done = 2

    return _await_adoptable_probe_result(lock, result, progress, expected_done)


def _await_adoptable_probe_result(lock, result, progress, expected_done):
    """Wait for the concurrent queue/history probes to settle; return nzo_id."""
    while True:
        with lock:
            nzo_id = result["nzo_id"]
            done_count = result["done_count"]
        if nzo_id or done_count >= expected_done:
            return nzo_id
        progress.wait(0.01)
        progress.clear()


def _submit_nzb_with_ui_pump(
    nzb_url, title, dialog, monitor, settings_getter=None, rejected_completed_ids=None
):
    """Run ``submit_nzb`` off the plugin thread, pump the dialog, and
    race a concurrent queue probe against the submit.

    ``submit_nzb`` issues a synchronous HTTP request to ``/api?mode=addurl``
    which on a big NZB routinely takes 30-300 s. Running it on the Kodi
    plugin thread freezes the progress dialog. The fix is two-part:

    1. ``submit_nzb`` runs in a daemon worker thread; the plugin thread
       loops on ``monitor.waitForAbort`` at 250 ms cadence, advances the
       dialog progress bar, and checks ``dialog.iscanceled`` every tick.
    2. Daemon probe threads concurrently watch nzbdav's queue/history via
       ``find_queued_by_name`` / ``find_completed_by_name`` and short-circuit
       as soon as the job for ``title`` appears — usually well before
       ``addurl`` replies.

    Returns ``(nzo_id, None)`` on success (either by worker completion or
    by queue adoption), or ``(None, error_dict)`` on cancel, shutdown,
    or submit failure.
    """
    _resolver.xbmc.log(
        "NZB-DAV: _submit_nzb_with_ui_pump entered for '{}' "
        "(threaded pump + concurrent queue probe)".format(title),
        _resolver.xbmc.LOGINFO,
    )

    # Completed rows the pre-submit body probe already rejected must not be
    # re-adopted by the concurrent history probe (finding #7); a tuple keeps
    # the `in` test cheap and never None.
    rejected_ids = tuple(rejected_completed_ids or ())
    submit_result = [None, None]
    submit_done = _resolver.threading.Event()
    activity_ready = _resolver.threading.Event()
    submit_timeout_seconds = max(
        _resolver._get_submit_timeout_seconds(
            **_resolver._settings_getter_kwargs(settings_getter)
        ),
        1,
    )

    def _submit_worker():
        try:
            submit_kwargs = _resolver._settings_getter_kwargs(settings_getter)
            if settings_getter is not None:
                submit_kwargs["submit_timeout"] = submit_timeout_seconds
            submit_result[0], submit_result[1] = _resolver.submit_nzb(
                nzb_url,
                title,
                **submit_kwargs,
            )
        except Exception as e:  # pylint: disable=broad-except
            _resolver.xbmc.log(
                "NZB-DAV: submit_nzb worker raised: {}".format(e),
                _resolver.xbmc.LOGERROR,
            )
            submit_result[0], submit_result[1] = None, None
        finally:
            submit_done.set()
            activity_ready.set()

    queue_hit = [None]
    adoption_status = [""]
    queue_hit_lock = _resolver.threading.Lock()
    adopted_during_submit = [False]
    queue_stop = _resolver.threading.Event()
    first_queue_probe_done = _resolver.threading.Event()

    def _current_adoption_hit():
        with queue_hit_lock:
            return queue_hit[0]

    def _record_adoption_hit(match):
        nzo_id = _job_nzo_id(match)
        if not nzo_id or queue_stop.is_set():
            return False
        with queue_hit_lock:
            if queue_hit[0]:
                return True
            queue_hit[0] = nzo_id
            if isinstance(match, dict):
                adoption_status[0] = str(match.get("status", "") or "")
        activity_ready.set()
        return True

    def _queue_probe_worker():
        # Probe immediately for already-visible retry/duplicate jobs. A miss
        # still falls through to the fast retry cadence while nzbdav receives
        # the addurl request.
        if queue_stop.wait(_resolver._SUBMIT_QUEUE_PROBE_INITIAL_DELAY_SECONDS):
            return
        probe_started = _resolver.time.monotonic()
        first_probe = True
        while not queue_stop.is_set() and not submit_done.is_set():
            match = _safe_probe_by_name(
                _resolver.find_queued_by_name, title, settings_getter, "queue"
            )
            try:
                if _record_adoption_hit(match):
                    return
            finally:
                if first_probe:
                    first_probe = False
                    first_queue_probe_done.set()
            if queue_stop.wait(_submit_probe_interval(probe_started)):
                return

    def _wait_for_history_probe_start():
        deadline = _resolver.time.monotonic() + max(
            0, _resolver._SUBMIT_HISTORY_PROBE_PARALLEL_GRACE_SECONDS
        )
        while not queue_stop.is_set() and not _current_adoption_hit():
            if first_queue_probe_done.is_set():
                return True
            remaining = deadline - _resolver.time.monotonic()
            if remaining <= 0:
                return True
            first_queue_probe_done.wait(min(0.01, remaining))
        return False

    def _history_probe_worker():
        # Start shortly after the queue probe so an immediate queue hit wins
        # without a history request. If the first queue probe misses quickly,
        # do not make completed-history adoption wait out the full grace.
        if not _wait_for_history_probe_start():
            return
        probe_started = _resolver.time.monotonic()
        while (
            not queue_stop.is_set()
            and not submit_done.is_set()
            and not _current_adoption_hit()
        ):
            match = _drop_rejected_completed_match(
                _safe_probe_by_name(
                    _resolver.find_completed_by_name, title, settings_getter, "history"
                ),
                rejected_ids,
            )
            if _record_adoption_hit(match):
                return
            if queue_stop.wait(_submit_probe_interval(probe_started)):
                return

    submit_t = _resolver.threading.Thread(
        target=_submit_worker, name="nzbdav-submit", daemon=True
    )
    probe_t = _resolver.threading.Thread(
        target=_queue_probe_worker, name="nzbdav-submit-probe", daemon=True
    )
    history_probe_t = _resolver.threading.Thread(
        target=_history_probe_worker, name="nzbdav-submit-history-probe", daemon=True
    )
    started_threads = []

    def _start_submit_thread(thread, label):
        try:
            thread.start()
        except RuntimeError as error:
            _resolver.xbmc.log(
                "NZB-DAV: Could not start {} thread for '{}': {}".format(
                    label, title, error
                ),
                _resolver.xbmc.LOGWARNING,
            )
            return False
        started_threads.append(thread)
        return True

    if not _start_submit_thread(submit_t, "submit"):
        _resolver.xbmc.log(
            "NZB-DAV: Falling back to synchronous submit for '{}'".format(title),
            _resolver.xbmc.LOGWARNING,
        )
        _submit_worker()
    elif not submit_done.is_set():
        _start_submit_thread(probe_t, "queue probe")
        _start_submit_thread(history_probe_t, "history probe")

    # Anchor elapsed to wall-clock via time.monotonic() instead of
    # accumulating _SUBMIT_UI_PUMP_INTERVAL_SECONDS per loop; the per-loop
    # accumulation under-reports on slow skins because dialog.update()
    # itself can block for tens of milliseconds.
    loop_start = _resolver.time.monotonic()
    submit_msg = _resolver._string(30097)

    def _probe_adoption_result():
        nzo_id = _current_adoption_hit()
        if not nzo_id:
            return None
        adopted_during_submit[0] = True
        if adoption_status[0] == "Completed":
            _resolver._safe_dialog_update(
                dialog,
                100,
                "Already completed in nzbdav\nPreparing stream: {}".format(title[:60]),
            )
        else:
            _resolver._safe_dialog_update(
                dialog,
                1,
                "Found in nzbdav\nChecking download status: {}".format(title[:60]),
            )
        _resolver.xbmc.log(
            "NZB-DAV: Concurrent queue/history probe found '{}' under "
            "nzo_id={}; adopting without waiting for addurl response".format(
                title, nzo_id
            ),
            _resolver.xbmc.LOGINFO,
        )
        return nzo_id, None

    def _wait_for_submit_activity_or_abort(wait_seconds):
        deadline = _resolver.time.monotonic() + max(0, wait_seconds)
        while not submit_done.is_set() and not _current_adoption_hit():
            if _resolver._monitor_abort_requested(monitor):
                return True
            remaining = deadline - _resolver.time.monotonic()
            if remaining <= 0:
                return False
            activity_ready.wait(min(0.01, remaining))
            activity_ready.clear()
        return False

    def _pump_dialog_progress(last_update):
        """Advance the dialog progress bar; return the next ``last_update``."""
        now = _resolver.time.monotonic()
        if now - last_update < _resolver._SUBMIT_UI_PUMP_INTERVAL_SECONDS:
            return last_update
        elapsed = now - loop_start
        pct = int((elapsed * 100) / submit_timeout_seconds) % 100
        _resolver._safe_dialog_update(
            dialog,
            pct,
            "{}\n{} ({}s)".format(submit_msg, title[:60], int(elapsed)),
        )
        return now

    def _run_pump_loop():
        """Pump the dialog until submit finishes or a terminal result occurs.

        Returns the ``(nzo_id, error)`` tuple to return from the caller.
        """
        last_dialog_update = loop_start
        while not submit_done.is_set():
            probe_result = _probe_adoption_result()
            if probe_result:
                return probe_result
            if dialog.iscanceled():
                _resolver.xbmc.log(
                    "NZB-DAV: User cancelled during submit for '{}'".format(title),
                    _resolver.xbmc.LOGINFO,
                )
                return None, {"status": "cancelled", "message": ""}
            if _wait_for_submit_activity_or_abort(
                _resolver._SUBMIT_ADOPTION_CHECK_INTERVAL_SECONDS
            ):
                return None, {"status": "shutdown", "message": ""}
            probe_result = _probe_adoption_result()
            if probe_result:
                return probe_result
            last_dialog_update = _pump_dialog_progress(last_dialog_update)
        # Race window re-check: prefer adopted nzo_id over a failed submit.
        nzo_id = _current_adoption_hit()
        if nzo_id and not submit_result[0]:
            _resolver.xbmc.log(
                "NZB-DAV: Queue probe found '{}' under nzo_id={} just as "
                "submit worker finished; preferring the adopted job over "
                "the submit result".format(title, nzo_id),
                _resolver.xbmc.LOGINFO,
            )
            return nzo_id, None
        return submit_result[0], submit_result[1]

    def _skip_thread_join(t):
        """Whether the cleanup loop should leave thread ``t`` un-joined."""
        if t is submit_t and adopted_during_submit[0] and not submit_done.is_set():
            return True
        if t in (probe_t, history_probe_t) and (
            _current_adoption_hit() or submit_result[0] or submit_result[1]
        ):
            # A successful addurl response is authoritative. The adoption
            # probe may still be blocked in a read-only queue/history API
            # call, so do not keep the post-picker submit path waiting on
            # cleanup after we already have the nzo_id or submitted result.
            # A terminal submit error is just as authoritative for the
            # immediate UI path; retries/adoption happen in the caller.
            return True
        return False

    def _join_started_threads():
        # Signal the probe worker to exit its wait loop, then give cleanup a
        # brief bounded window. If we already adopted while addurl is still
        # blocked, waiting on that uninterruptible HTTP worker only adds
        # latency; it is daemon=True and will die with the plugin interpreter.
        queue_stop.set()
        for t in started_threads:
            if _skip_thread_join(t):
                continue
            try:
                t.join(timeout=1)
            except RuntimeError as e:
                # Thread.join raises RuntimeError if the thread wasn't
                # started or if join is called on the current thread.
                # Both are best-effort cleanup paths here (threads are
                # daemon=True so they die with the interpreter anyway)
                # but log at debug so a real misuse surfaces.
                _resolver.xbmc.log(
                    "NZB-DAV: Resolver worker join failed: {}".format(e),
                    _resolver.xbmc.LOGDEBUG,
                )

    try:
        return _run_pump_loop()
    finally:
        _join_started_threads()


def _get_submit_timeout_seconds(settings_getter=None):
    """Read submit_timeout setting; returns int or 300 on error."""
    try:
        if settings_getter is None:
            raw = _resolver.xbmcaddon.Addon("plugin.video.nzbdav").getSetting(
                "submit_timeout"
            )
        else:
            raw = settings_getter("submit_timeout", "")
        return int(raw) if raw else 300
    except Exception:  # pylint: disable=broad-except
        # xbmcaddon import failures, unexpected setting shapes, int() on
        # a MagicMock in tests — all funnel to the documented default.
        # ``Exception`` on its own (the previous ``(ValueError, TypeError,
        # Exception)`` tuple was dead code — Exception subsumes the other
        # two) keeps the safety net without the misleading tuple.
        return 300


def _adopt_queued_or_completed_job(
    title, monitor, settings_getter=None, rejected_completed_ids=None
):
    """Return an existing nzbdav nzo_id for ``title`` if the submit we
    just timed out on actually reached nzbdav.

    After a client-side submit timeout, nzbdav may be:
    - Still fetching/parsing the NZB (no queue entry yet)
    - Processing it (queue entry exists under ``title``)
    - Already done (history entry exists under ``title``)

    Probes queue and history a handful of times on a short interval.
    Returns the matching ``nzo_id`` on the first positive hit, ``None``
    if nothing surfaces within the poll budget (caller retries submit).
    """
    for poll in range(_resolver._SUBMIT_ADOPT_POLL_COUNT):
        nzo_id = _find_adoptable_job_during_submit(
            title,
            settings_getter=settings_getter,
            rejected_completed_ids=rejected_completed_ids,
        )
        if nzo_id:
            return nzo_id
        if poll < _resolver._SUBMIT_ADOPT_POLL_COUNT - 1:
            if monitor.waitForAbort(_resolver._SUBMIT_ADOPT_POLL_INTERVAL_SECONDS):
                return None
    return None


def _log_submit_attempt_failed(attempt, max_submit_retries, title):
    """Log a submit attempt that produced neither an nzo_id nor an error."""
    _resolver.xbmc.log(
        "NZB-DAV: Submit attempt {}/{} failed for '{}'".format(
            attempt, max_submit_retries, title
        ),
        _resolver.xbmc.LOGWARNING,
    )


def _submit_retry_backoff_aborted(attempt, max_submit_retries, monitor, title):
    """Wait the inter-attempt backoff; return True if Kodi is shutting down."""
    if attempt < max_submit_retries and monitor.waitForAbort(2):
        _resolver.xbmc.log(
            "NZB-DAV: Kodi shutdown during submit retry backoff "
            "(attempt {}/{}) for '{}'".format(attempt, max_submit_retries, title),
            _resolver.xbmc.LOGINFO,
        )
        return True
    return False


def _report_all_submit_attempts_failed(
    last_submit_error, dialog, title, max_submit_retries, selected_indexer
):
    """Surface the terminal error after every submit attempt failed."""
    if last_submit_error:
        _resolver.xbmc.log(
            "NZB-DAV: All {} submit attempts failed for '{}', "
            "last HTTP {}: {}".format(
                max_submit_retries,
                title,
                last_submit_error["status"],
                last_submit_error["message"],
            ),
            _resolver.xbmc.LOGERROR,
        )
        _resolver._close_dialog_before_submit_error(dialog)
        _resolver._show_submit_error_dialog(
            _resolver._submit_error_with_indexer(last_submit_error, selected_indexer)
        )
        return

    _resolver.xbmc.log(
        "NZB-DAV: All {} submit attempts failed for '{}'. "
        "Check nzbdav URL and API key in settings.".format(max_submit_retries, title),
        _resolver.xbmc.LOGERROR,
    )
    _resolver._close_dialog_before_submit_error(dialog)
    _resolver.xbmcgui.Dialog().ok(_resolver._addon_name(), _resolver._string(30098))


def _submit_nzb_with_retries(
    nzb_url,
    title,
    dialog,
    monitor,
    max_submit_retries=3,
    settings_getter=None,
    selected_indexer=None,
    rejected_completed_ids=None,
):
    """Submit an NZB with the existing retry and error-dialog behavior."""
    _resolver.xbmc.log(
        "NZB-DAV: Submitting NZB for '{}'".format(title), _resolver.xbmc.LOGINFO
    )
    last_submit_error = None
    error_ctx = _resolver._build_submit_error_ctx(
        title,
        dialog,
        monitor,
        settings_getter,
        selected_indexer,
        rejected_completed_ids,
    )

    for attempt in range(1, max_submit_retries + 1):
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            nzb_url,
            title,
            dialog,
            monitor,
            settings_getter=settings_getter,
            rejected_completed_ids=rejected_completed_ids,
        )
        if nzo_id:
            return nzo_id

        if submit_error:
            last_submit_error = submit_error
            error_ctx["attempt_label"] = "{}/{}".format(attempt, max_submit_retries)
            action, value = _resolver._handle_submit_attempt_error(
                submit_error, error_ctx
            )
            if action == "return":
                return value
        else:
            _log_submit_attempt_failed(attempt, max_submit_retries, title)

        if _submit_retry_backoff_aborted(attempt, max_submit_retries, monitor, title):
            return None

    _report_all_submit_attempts_failed(
        last_submit_error, dialog, title, max_submit_retries, selected_indexer
    )
    return None
