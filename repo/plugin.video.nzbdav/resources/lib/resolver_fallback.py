# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Fallback-candidate submit worker and lifecycle.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


def _collect_fallback_candidate_jobs(
    candidates, stop_event=None, dead=None, primary_nzb_url=None
):
    """Filter raw candidates into ``(candidate, nzb_url, title, job_name)`` rows.

    Drops non-dicts, entries missing a link/title, provably-dead URLs, and the
    active primary's own release.
    """
    candidate_jobs = []
    for index, candidate in enumerate(candidates or [], start=1):
        if stop_event is not None and stop_event.is_set():
            break
        row = _fallback_candidate_row(candidate, index, dead, primary_nzb_url)
        if row is not None:
            candidate_jobs.append(row)
    return candidate_jobs


def _fallback_candidate_row(candidate, index, dead, primary_nzb_url):
    """Return the ``(candidate, nzb_url, title, job_name)`` row or None to skip."""
    if not isinstance(candidate, dict):
        return None
    nzb_url = candidate.get("link")
    title = candidate.get("title")
    if not nzb_url or not title:
        return None
    # Never re-admit a provably-dead candidate, and never offer the active
    # primary as its own backup (the original primary only re-enters the
    # pool after a live cutover demotes it -- handled in stream_proxy).
    if dead is not None and dead.has_url(nzb_url):
        _resolver.xbmc.log(
            "NZB-DAV: Skipping dead fallback candidate '{}'".format(title),
            _resolver.xbmc.LOGINFO,
        )
        return None
    if primary_nzb_url and nzb_url == primary_nzb_url:
        _resolver.xbmc.log(
            "NZB-DAV: Skipping primary's own release as a fallback '{}'".format(title),
            _resolver.xbmc.LOGINFO,
        )
        return None
    job_name = _resolver.build_fallback_job_name(title, nzb_url, index)
    return (candidate, nzb_url, title, job_name)


def _lookup_existing_fallback_jobs(job_names, settings_getter=None):
    """Return ``{job_name: job}`` for fallback names already in nzbdav."""
    completed_jobs = _resolver.find_completed_by_names(
        job_names, **_resolver._settings_getter_kwargs(settings_getter)
    )
    queue_names = [name for name in job_names if name not in completed_jobs]
    queued_jobs = _resolver.find_queued_by_names(
        queue_names, **_resolver._settings_getter_kwargs(settings_getter)
    )
    existing_jobs = dict(completed_jobs)
    existing_jobs.update(queued_jobs)
    return existing_jobs


def _submit_one_fallback_candidate(
    nzb_url, job_name, monitor, settings_getter=None, dead=None
):
    """Submit a single fallback candidate; return its ``nzo_id`` or ``None``."""
    try:
        nzo_id, submit_error = _resolver.submit_nzb(
            nzb_url, job_name, **_resolver._settings_getter_kwargs(settings_getter)
        )
    except Exception as error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: Fallback submit failed for '{}': {}".format(
                job_name, _resolver._redact_log(error)
            ),
            _resolver.xbmc.LOGWARNING,
        )
        return None
    if not nzo_id and submit_error:
        nzo_id = _recover_fallback_submit_error(
            submit_error, nzb_url, job_name, monitor, settings_getter, dead
        )
    if not nzo_id:
        if submit_error is None:
            _resolver.xbmc.log(
                "NZB-DAV: Fallback submit did not create job for '{}'".format(job_name),
                _resolver.xbmc.LOGWARNING,
            )
        return None
    return nzo_id


def _recover_fallback_submit_error(
    submit_error, nzb_url, job_name, monitor, settings_getter, dead
):
    """Adopt a timed-out fallback submit, or log+mark-dead and return None."""
    status = submit_error.get("status")
    nzo_id = None
    if status == "timeout":
        _resolver.xbmc.log(
            "NZB-DAV: Fallback submit timed out for '{}'; probing "
            "queue/history in background".format(job_name),
            _resolver.xbmc.LOGWARNING,
        )
        nzo_id = _resolver._adopt_queued_or_completed_job(
            job_name, monitor, settings_getter=settings_getter
        )
    if not nzo_id:
        if dead is not None and _resolver.is_provably_dead_submit_error(submit_error):
            dead.add(nzb_url=nzb_url)
        _resolver.xbmc.log(
            "NZB-DAV: Fallback submit skipped for '{}' (status={}): {}".format(
                job_name, status, _resolver._redact_log(submit_error.get("message", ""))
            ),
            _resolver.xbmc.LOGWARNING,
        )
    return nzo_id


def _adopt_existing_fallback_job(existing_job, nzb_url, title, job_name):
    """Build the fallback-job record for an already-present nzbdav job."""
    _resolver.xbmc.log(
        "NZB-DAV: Adopting existing fallback job '{}' nzo_id={}".format(
            job_name, existing_job["nzo_id"]
        ),
        _resolver.xbmc.LOGINFO,
    )
    return {
        "title": title,
        "nzb_url": nzb_url,
        "job_name": job_name,
        "nzo_id": existing_job["nzo_id"],
        "stream_url": "",
        "stream_headers": {},
        "content_length": 0,
        "status": existing_job.get("status", ""),
    }


def _submit_fallback_candidates(
    candidates,
    monitor,
    stop_event=None,
    on_job=None,
    settings_getter=None,
    dead=None,
    primary_nzb_url=None,
    *,
    episode_context=None,
):
    """Submit duplicate fallback candidates as standby nzbdav jobs."""
    candidate_jobs = _collect_fallback_candidate_jobs(
        candidates,
        stop_event=stop_event,
        dead=dead,
        primary_nzb_url=primary_nzb_url,
    )
    existing_jobs = _lookup_existing_fallback_jobs(
        [row[3] for row in candidate_jobs], settings_getter=settings_getter
    )

    fallback_jobs = []

    def _record(job):
        if isinstance(episode_context, dict):
            job = dict(job)
            job["episode_context"] = dict(episode_context)
        fallback_jobs.append(job)
        if on_job is not None:
            on_job(dict(job))

    for _candidate, nzb_url, title, job_name in candidate_jobs:
        if stop_event is not None and stop_event.is_set():
            break
        job = _resolve_fallback_candidate_job(
            existing_jobs.get(job_name),
            nzb_url,
            title,
            job_name,
            monitor,
            settings_getter,
            dead,
        )
        if job is not None:
            _record(job)
    return fallback_jobs


def _resolve_fallback_candidate_job(
    existing_job, nzb_url, title, job_name, monitor, settings_getter, dead
):
    """Adopt an existing fallback job or submit a new one; return record or None."""
    if existing_job and existing_job.get("nzo_id"):
        return _adopt_existing_fallback_job(existing_job, nzb_url, title, job_name)
    nzo_id = _submit_one_fallback_candidate(
        nzb_url, job_name, monitor, settings_getter=settings_getter, dead=dead
    )
    if not nzo_id:
        return None
    return {
        "title": title,
        "nzb_url": nzb_url,
        "job_name": job_name,
        "nzo_id": nzo_id,
        "stream_url": "",
        "stream_headers": {},
        "content_length": 0,
    }


def _fallback_streams_enabled(settings_getter=None):
    """Return whether fallback streams are enabled in Kodi settings."""
    try:
        if settings_getter is None:
            raw = _resolver.xbmcaddon.Addon("plugin.video.nzbdav").getSetting(
                "fallback_streams_enabled"
            )
        else:
            raw = settings_getter("fallback_streams_enabled", "true")
    except (AttributeError, RuntimeError, TypeError):
        return True
    return str(raw or "").strip().lower() != "false"


def _prefetch_fallback_candidate_loader(candidate_loader):
    """Start fallback candidate discovery now and return a cached loader.

    The returned loader is still consumed by the fallback submit worker after
    primary submit/adoption, so this overlaps Hydra/NZB manifest discovery
    without submitting standby nzbdav jobs before the primary is accepted.
    """
    if candidate_loader is None:
        return None

    done = _resolver.threading.Event()
    state = {"candidates": [], "disabled": False}
    errors = []

    def _worker():
        try:
            loaded = candidate_loader()
            if loaded is _resolver.FALLBACK_CANDIDATES_DISABLED:
                state["disabled"] = True
                state["candidates"] = []
            else:
                state["candidates"] = list(loaded or [])
        except Exception as error:  # pylint: disable=broad-except
            errors.append(error)
        finally:
            done.set()

    thread = _resolver.threading.Thread(
        target=_worker, name="nzbdav-fallback-candidate-prefetch", daemon=True
    )
    try:
        thread.start()
    except RuntimeError:
        return candidate_loader

    def _load_prefetched_candidates():
        done.wait()
        if errors:
            raise errors[0]
        if state["disabled"]:
            return _resolver.FALLBACK_CANDIDATES_DISABLED
        return list(state["candidates"])

    return _load_prefetched_candidates


def _playback_active_flag():
    """Tri-state cross-process read of the live-playback liveness flag.

    Returns True when service.py is currently monitoring playback, False when
    that flag is absent/cleared, or None when the property can't be read.

    The fallback submit worker's ``state["stop"]`` event is in-process and can
    never be set by service.py's player callbacks (a different process), so the
    worker needs a cross-process signal to know playback ended. We use a
    DEDICATED ``nzbdav.playing`` window property that service.py sets while
    monitoring and clears only on stop/end -- NOT the ``nzbdav.active`` handoff
    flag, which ``service._check_active()`` consumes (clears) on the first tick
    after playback starts and so would read False mid-playback.
    """
    try:
        return _resolver.xbmcgui.Window(10000).getProperty("nzbdav.playing") == "true"
    except Exception:  # pylint: disable=broad-except
        return None


def _wait_prewarm_or_inactive(state, prewarm_delay, playback_signaled):
    """Wait up to ``prewarm_delay`` seconds, aborting early on stop/inactive.

    Returns True if the worker should ABORT: the in-process stop event is set,
    or -- only once ``playback_signaled`` is True AND we have positively
    observed the cross-process ``nzbdav.playing`` flag as live at least once --
    that flag is later cleared (playback stopped/ended during the standby
    window). Returns False once the full delay elapses with playback still live.

    The "seen live first" latch matters: if playback was never actually
    signaled (the ``_await_playback_start`` cap path, or an unusual handoff
    where service never set the flag), we never latch, so the worker degrades to
    a late submit rather than a wrongly-stranded backup -- and a brief startup
    race where the worker polls before service sets the flag can't abort it.

    Uses ``Event.wait`` for the polling sleep so a daemon worker never blocks
    Kodi shutdown. Total wait equals ``prewarm_delay``.
    """
    stop = state.get("stop")
    if not prewarm_delay:
        return bool(stop is not None and stop.is_set())
    interval = _resolver._FALLBACK_PREWARM_POLL_SECONDS
    remaining = float(prewarm_delay)
    seen_live = False
    while remaining > 0:
        wait_for = min(interval, remaining)
        if stop is not None and stop.wait(wait_for):
            return True
        remaining -= wait_for
        if playback_signaled:
            seen_live, aborted = _prewarm_playback_latch(seen_live)
            if aborted:
                return True
    return False


def _prewarm_playback_latch(seen_live):
    """Advance the seen-live latch; return ``(seen_live, should_abort)``.

    ``flag is True`` latches that playback was observed live; ``flag is False``
    once it was live means playback stopped/ended (abort). ``flag is None`` (read
    failed) leaves the latch unchanged and keeps waiting.
    """
    flag = _playback_active_flag()
    if flag is True:
        return True, False
    if flag is False and seen_live:
        return seen_live, True
    return seen_live, False


def _get_fallback_submit_delay_seconds(settings_getter=None):
    """Read the configurable fallback submit defer (seconds INTO playback).

    Backup NZBs are submitted only this many seconds AFTER playback has actually
    started (the burst is anchored to the playback-start signal, then held for
    this delay). Exposed as the ``fallback_submit_delay`` user setting so the
    defer is configurable; an empty/invalid/negative value funnels to the
    documented ``_FALLBACK_PREWARM_DELAY_SECONDS`` default. ``0`` is valid and
    means submit right at playback start.
    """
    try:
        if settings_getter is None:
            raw = _resolver.xbmcaddon.Addon("plugin.video.nzbdav").getSetting(
                "fallback_submit_delay"
            )
        else:
            raw = settings_getter("fallback_submit_delay", "")
        value = int(raw) if raw else _resolver._FALLBACK_PREWARM_DELAY_SECONDS
        return value if value >= 0 else _resolver._FALLBACK_PREWARM_DELAY_SECONDS
    except Exception:  # pylint: disable=broad-except
        # xbmcaddon import failure, unexpected setting shapes, int() on a
        # MagicMock in tests — all funnel to the documented default.
        return _resolver._FALLBACK_PREWARM_DELAY_SECONDS


def _await_playback_start(state):
    """Block until playback is signaled (return True) or the worker is stopped
    (return False).

    Anchors the fallback prewarm delay to actual playback start. Capped by
    ``_FALLBACK_PLAYBACK_WAIT_CAP_SECONDS`` so a path that never signals (a bug
    or an unusual handoff) degrades to a late submit rather than a permanently
    stranded backup.
    """
    started = state.get("playback_started")
    if started is None:
        return True
    stop = state.get("stop")
    if stop is not None and stop.is_set():
        return False
    waited = 0.0
    while not started.wait(_resolver._FALLBACK_PLAYBACK_WAIT_POLL_SECONDS):
        if stop is not None and stop.is_set():
            return False
        waited += _resolver._FALLBACK_PLAYBACK_WAIT_POLL_SECONDS
        if waited >= _resolver._FALLBACK_PLAYBACK_WAIT_CAP_SECONDS:
            return True
    return True


def _signal_fallback_playback_started(state):
    """Mark playback as started so a ``wait_for_playback`` fallback worker begins
    its prewarm-delay countdown. Best-effort no-op when there is no worker."""
    if not state:
        return
    event = state.get("playback_started")
    if event is not None:
        event.set()


def _resolve_active_fallback_candidates(candidate_list, candidate_loader):
    """Return ``(active_candidates, lookup_disabled)`` for the fallback worker.

    Uses the prefetched list when there is no loader; otherwise runs the loader,
    mapping the disabled sentinel to ``([], True)`` and any error to ``([],
    False)`` (logged) so the worker keeps its original branching unchanged.
    """
    if candidate_loader is None:
        return candidate_list, False
    try:
        loaded_candidates = candidate_loader()
    except Exception as error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: Fallback candidate lookup failed: {}".format(
                _resolver._redact_log(error)
            ),
            _resolver.xbmc.LOGWARNING,
        )
        return [], False
    if loaded_candidates is _resolver.FALLBACK_CANDIDATES_DISABLED:
        return [], True
    return list(loaded_candidates or []), False


def _notify_no_fallback_candidates(candidate_lookup_disabled, settings_getter):
    """Show the "no fallbacks" toast unless lookup was disabled or off."""
    if candidate_lookup_disabled or not _resolver._fallback_streams_enabled(
        settings_getter=settings_getter
    ):
        return
    try:
        _resolver._notify(_resolver._addon_name(), _resolver._string(30187), 4000)
    except (RuntimeError, OSError):
        # The "no fallback candidates" toast is cosmetic; a UI failure (e.g.
        # during shutdown) must not break the best-effort fallback worker's
        # clean return.
        pass


def _run_fallback_on_append_hook(state):
    """Push a late-adopted fallback into the live proxy session, if hook armed.

    The ``on_append`` hook (installed after /prepare returns a session id) is
    best-effort: a push failure must never disrupt fallback submission.
    """
    hook = state.get("on_append")
    if hook is None:
        return
    try:
        hook()
    except Exception as error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: fallback on_append hook failed: {}".format(
                _resolver._redact_log(error)
            ),
            _resolver.xbmc.LOGWARNING,
        )


def _load_and_submit_fallback_candidates(state, submit_inputs):
    """Load fallback candidates then submit them as standby nzbdav jobs.

    ``submit_inputs`` carries the worker's captured submit args (candidate list /
    loader, settings_getter, dead, primary_nzb_url, on_job). No-ops once the stop
    event is set or when there are no candidates (toasting once in that case).
    """
    active_candidates, candidate_lookup_disabled = _resolve_active_fallback_candidates(
        submit_inputs["candidate_list"], submit_inputs["candidate_loader"]
    )
    if state["stop"].is_set():
        return
    if not active_candidates:
        _notify_no_fallback_candidates(
            candidate_lookup_disabled, submit_inputs["settings_getter"]
        )
        return
    kwargs = {
        "stop_event": state["stop"],
        "on_job": submit_inputs["on_job"],
        "settings_getter": submit_inputs["settings_getter"],
        "dead": submit_inputs["dead"],
        "primary_nzb_url": submit_inputs["primary_nzb_url"],
    }
    if submit_inputs["episode_context"] is not None:
        kwargs["episode_context"] = submit_inputs["episode_context"]
    _resolver._submit_fallback_candidates(
        active_candidates, _resolver.xbmc.Monitor(), **kwargs
    )


def _start_fallback_submit_worker(
    candidates=None,
    candidate_loader=None,
    settings_getter=None,
    prewarm_delay=0,
    wait_for_playback=False,
    dead=None,
    primary_nzb_url=None,
    episode_context=None,
):
    """Start background fallback submits and return shared state.

    When ``wait_for_playback`` is set, the burst is held until playback is
    signaled (via ``_signal_fallback_playback_started``) and then for
    ``prewarm_delay`` seconds, so backups are only fetched well INTO playback,
    never during the pre-playback download. ``prewarm_delay`` then measures the
    delay from playback start rather than from worker creation (primary submit).
    The prewarm opens several concurrent connections to nzbdav (one submit + one
    prevalidation probe per source); firing them during the fragile startup
    cache-fill window competes for nzbdav's connection budget and can stall the
    live stream enough to wedge the CoreELEC audio clock (black screen). Both the
    playback wait and the delay are cancellable: a session stop aborts with no
    submission.
    """

    def _cancel_job(nzo_id):
        if settings_getter is None:
            return _resolver.cancel_job(nzo_id)
        return _resolver.cancel_job(nzo_id, settings_getter=settings_getter)

    state = {
        "lock": _resolver.threading.Lock(),
        "jobs": [],
        "stop": _resolver.threading.Event(),
        "finished": _resolver.threading.Event(),
        "playback_started": _resolver.threading.Event(),
        "wait_for_playback": wait_for_playback,
        "thread": None,
        "cancel_job": _cancel_job,
    }
    candidate_list = list(candidates or [])
    if not candidate_list and candidate_loader is None:
        state["finished"].set()
        return state

    def _append_job(job):
        should_cancel = False
        with state["lock"]:
            if state["stop"].is_set():
                should_cancel = True
            else:
                state["jobs"].append(job)
        if should_cancel:
            _resolver._cancel_fallback_job(state, job)
            return
        _run_fallback_on_append_hook(state)

    submit_inputs = {
        "candidate_list": candidate_list,
        "candidate_loader": candidate_loader,
        "settings_getter": settings_getter,
        "dead": dead,
        "primary_nzb_url": primary_nzb_url,
        "episode_context": (
            dict(episode_context) if isinstance(episode_context, dict) else None
        ),
        "on_job": _append_job,
    }

    def _worker():
        try:
            # Hold the entire prewarm/submit burst until playback has actually
            # started, then for ``prewarm_delay`` seconds, so backups are fetched
            # well INTO playback and never during the pre-playback download
            # (anchoring to primary submission could fire mid-download for a slow
            # primary). Both waits are cancellable: a stop aborts before submit.
            if wait_for_playback and not _await_playback_start(state):
                return
            # ``wait_for_playback`` worker only reaches here once playback was
            # signaled; a non-waiting worker never gets a cross-process active
            # flag to honor, so only its stop event aborts the prewarm wait.
            if _wait_prewarm_or_inactive(state, prewarm_delay, wait_for_playback):
                return
            _load_and_submit_fallback_candidates(state, submit_inputs)
        except Exception as error:  # pylint: disable=broad-except
            _resolver.xbmc.log(
                "NZB-DAV: Fallback submit worker failed: {}".format(
                    _resolver._redact_log(error)
                ),
                _resolver.xbmc.LOGWARNING,
            )
            _resolver._cancel_fallback_submitted_jobs(state)
        finally:
            state["finished"].set()

    thread = _resolver.threading.Thread(
        target=_worker, name="nzbdav-fallback-submit", daemon=True
    )
    state["thread"] = thread
    try:
        thread.start()
    except RuntimeError as error:
        # Thread creation can fail during Kodi shutdown / interpreter
        # teardown. Fallbacks are best-effort, so fail soft: mark the worker
        # finished and let the resolve path continue instead of propagating.
        state["thread"] = None
        state["finished"].set()
        _resolver.xbmc.log(
            "NZB-DAV: Fallback submit worker did not start: {}".format(error),
            _resolver.xbmc.LOGWARNING,
        )
    return state
