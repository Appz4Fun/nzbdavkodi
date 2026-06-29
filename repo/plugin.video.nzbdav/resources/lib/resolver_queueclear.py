# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Pre-submit queue-clear prompt and execution.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


def _clear_queue_on_submit_mode(settings_getter=None):
    """Return the clear-queue-on-submit policy: ``ask`` | ``always`` | ``never``.

    Reads the ``clear_queue_on_submit`` enum setting (stored as the 0-based
    index "0"/"1"/"2"). Any unset or unknown value falls back to the safe
    default, ``ask``.
    """
    try:
        if settings_getter is None:
            raw = _resolver.xbmcaddon.Addon("plugin.video.nzbdav").getSetting(
                "clear_queue_on_submit"
            )
        else:
            raw = settings_getter("clear_queue_on_submit", "0")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return "ask"
    return _resolver._CLEAR_QUEUE_ON_SUBMIT_MODES.get(str(raw or "0").strip(), "ask")


def _queue_clear_prompt_message(slots):
    """Build the localized yes/no prompt body listing the queued jobs.

    The per-item lines (name + status) are live nzbdav data, not translatable
    boilerplate; the heading and trailing question come from ``strings.po``.
    """
    lines = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        status = slot.get("status") or "?"
        name = slot.get("filename") or slot.get("name") or slot.get("nzo_id") or "?"
        lines.append("- {} ({})".format(str(name)[:60], status))
    shown = "\n".join(lines[:6])
    if len(lines) > 6:
        shown += "\n  ..."
    return "{}\n{}\n\n{}".format(
        _resolver._string(30203).format(len(slots)),
        shown,
        _resolver._string(30204),
    )


def _queue_slot_is_title(slot, title):
    """True if a queue slot is THIS playback title's own job (exact name match).

    nzbdav echoes the submitted name verbatim into the queue slot, so an exact
    match identifies the job the submit path would adopt and resume — it must be
    excluded from the clear so the user's own in-flight download is not
    cancelled and restarted. Checks the SAME slot fields, in the same order, as
    the adoption path (``find_queued_by_names``): ``filename``, ``nzo_id_name``,
    then ``name`` (nzbdav reports the name under different keys by build/phase).
    """
    if not isinstance(slot, dict):
        return False
    return title in (
        slot.get("filename"),
        slot.get("nzo_id_name"),
        slot.get("name"),
    )


def _completed_copy_blocks_clear(title, settings_getter):
    """Best-effort adopt check for the clear-queue guard, hard-bounded to the
    queue-probe budget.

    Returns True when the queue clear must be SKIPPED: either this title is
    already adoptable (a completed, body-validated copy exists) or adoption could
    not be ruled out within ``_CLEAR_QUEUE_PROBE_TIMEOUT``. Only a probe that
    completes and finds NO adoptable copy returns False (the clear may proceed).

    The probe (``_existing_completed_stream`` -> completed-history GET + WebDAV
    body probe) carries its own multi-second socket timeouts. This guard runs
    BEFORE the progress dialog, so an unbounded wait would freeze playback with
    no UI and no abort path on a slow/unreachable nzbdav. Running it on a daemon
    worker with a join deadline caps that wait. The authoritative, full-timeout
    probe inside ``_poll_until_ready`` (which runs with the dialog visible and
    abortable) still makes the real adopt-or-submit decision, so a timeout here
    never forces a wrong outcome -- only a conservative "leave the queue intact".
    A timeout/error therefore returns True (skip clear): never cancel the user's
    other downloads on an adoption we could not rule out. ``on_existing_completed``
    is left None so the worker has no side effects.
    """
    result = {}

    def _probe():
        try:
            # No download_size here on purpose: this gate only decides whether to
            # SKIP clearing OTHER queued jobs, and never serves a stream to Kodi.
            # Letting a stub look adoptable just leaves the queue intact -- the
            # conservative default this guard already documents. The #282 stub
            # guard runs on the authoritative playback probe (_poll_until_ready /
            # picker), which DOES thread download_size and rejects the stub there.
            result["stream"] = _resolver._existing_completed_stream(
                title, **_resolver._settings_getter_kwargs(settings_getter)
            )
        except Exception as error:  # pylint: disable=broad-except
            result["error"] = error

    worker = _resolver.threading.Thread(
        target=_probe, name="nzbdav-clearqueue-adopt-probe", daemon=True
    )
    worker.start()
    worker.join(_resolver._CLEAR_QUEUE_PROBE_TIMEOUT)
    return _completed_copy_blocks_clear_result(worker, result)


def _completed_copy_blocks_clear_result(worker, result):
    """Interpret the adopt-probe outcome: True means SKIP the queue clear."""
    if worker.is_alive():
        _resolver.xbmc.log(
            "NZB-DAV: completed-adopt probe exceeded {}s before clearing the "
            "queue; leaving queue intact and deferring to the in-dialog download "
            "check".format(_resolver._CLEAR_QUEUE_PROBE_TIMEOUT),
            _resolver.xbmc.LOGINFO,
        )
        return True
    if result.get("error") is not None:
        _resolver.xbmc.log(
            "NZB-DAV: completed-adopt probe failed before clearing the queue; "
            "leaving queue intact: {}".format(result["error"]),
            _resolver.xbmc.LOGWARNING,
        )
        return True
    return bool(result.get("stream"))


def _probe_clearable_queue_slots(title, settings_getter):
    """Return the queued slots eligible to clear (this title's own job removed).

    Probes the queue FIRST (bounded, best-effort), before any history lookup, so
    an empty queue -- or a slow nzbdav -- never blocks the resolver thread when
    there is nothing to clear. On timeout/error returns []. Never includes THIS
    title's own in-flight job (the submit path adopts and resumes it; clearing it
    would restart the download the user is playing).
    """
    try:
        slots = _resolver.get_queue_slots(
            timeout=_resolver._CLEAR_QUEUE_PROBE_TIMEOUT,
            **_resolver._settings_getter_kwargs(settings_getter),
        )
    except Exception as error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: queue probe before submit failed; leaving queue intact: "
            "{}".format(error),
            _resolver.xbmc.LOGWARNING,
        )
        return []
    return [s for s in slots if not _queue_slot_is_title(s, title)]


def _confirm_queue_clear(title, slots):
    """Show the ask-mode yes/no prompt; True to clear, False to keep the queue."""
    try:
        confirmed = _resolver.xbmcgui.Dialog().yesno(
            _resolver._addon_name(),
            _queue_clear_prompt_message(slots),
            nolabel=_resolver._string(30201),
            yeslabel=_resolver._string(30202),
        )
    except (RuntimeError, OSError, TypeError) as error:
        _resolver.xbmc.log(
            "NZB-DAV: clear-queue prompt failed; leaving queue intact: "
            "{}".format(error),
            _resolver.xbmc.LOGWARNING,
        )
        return False
    if not confirmed:
        _resolver.xbmc.log(
            "NZB-DAV: user kept the existing queue before submitting "
            "'{}'".format(title),
            _resolver.xbmc.LOGINFO,
        )
        return False
    return True


def _maybe_clear_queue_before_submit(
    title, settings_getter=None, completed_lookup_done=False
):
    """Optionally clear the nzbdav queue before submitting a new NZB.

    Driven by the ``clear_queue_on_submit`` setting:
      * ``never``  - no-op (does not even probe the queue);
      * ``always`` - clear the whole queue whenever it is non-empty;
      * ``ask``    - show a yes/no dialog listing the queued jobs and clear
        only on confirmation.

    "Clear" cancels the OTHER queued jobs — not this title's own in-flight job
    (the submit path adopts and resumes that one) — and leaves completed/failed
    history intact (see ``clear_queue``). Defensive: a queue-probe or dialog
    failure leaves the queue untouched and never blocks the submit.
    """
    mode = _clear_queue_on_submit_mode(settings_getter)
    if mode == "never":
        return
    slots = _probe_clearable_queue_slots(title, settings_getter)
    if not slots:
        return
    if _adoptable_copy_suppresses_clear(title, settings_getter, completed_lookup_done):
        return
    if mode == "ask" and not _confirm_queue_clear(title, slots):
        return
    _clear_queue_slots(title, slots, settings_getter)


def _adoptable_copy_suppresses_clear(title, settings_getter, completed_lookup_done):
    """True when an already-playable completed copy means the clear must be skipped.

    Don't clear when this title is already downloaded AND playable: playback
    adopts the completed copy (no new download is submitted), so cancelling other
    active jobs would be wrong. Validate with the SAME body probe the adopt path
    uses (_existing_completed_stream) so a STALE Completed row whose storage is
    missing or fails the probe does NOT suppress the clear: _poll_until_ready will
    reject that row and submit a new download, which is exactly when the clear
    should run. That probe carries multi-second socket timeouts and this guard
    runs BEFORE the progress dialog, so it is hard-bounded to the queue-probe
    budget (_completed_copy_blocks_clear): on a slow/unreachable nzbdav the guard
    returns promptly and leaves the queue intact rather than freezing playback
    with no UI. This bounded check is a best-effort gate, NOT the authoritative
    adopt decision -- _poll_until_ready re-runs the probe at full timeout with the
    dialog visible/abortable and makes the real adopt-or-submit call; trusting
    this short-timeout result to skip that probe would risk a spurious
    re-download on a slow-but-working nzbdav, so the (cheap, bounded) re-check is
    intentional. Only the no-picker-hint paths (/resolve, auto-select) need it:
    gated on completed_lookup_done, so when the picker already validated completed
    and we still reached the submit path (submit certain) it is skipped. It runs
    only after the queue probe confirmed there ARE other jobs to clear, so an
    empty queue never pays for it.
    """
    return not completed_lookup_done and _completed_copy_blocks_clear(
        title, settings_getter
    )


def _clear_queue_slots(title, slots, settings_getter):
    """Cancel exactly the probed/shown slots and log the count.

    Cancels exactly the slots we probed/showed -- not a fresh fetch -- so a job
    that appeared between the prompt and now is never cancelled unseen. Each
    delete is bound with the same short timeout as the probe so a stalled nzbdav
    can't freeze the resolver for minutes across several deletes.
    """
    cleared = _resolver.clear_queue(
        slots=slots,
        timeout=_resolver._CLEAR_QUEUE_PROBE_TIMEOUT,
        **_resolver._settings_getter_kwargs(settings_getter),
    )
    _resolver.xbmc.log(
        "NZB-DAV: cleared {} queued job(s) before submitting '{}'".format(
            cleared, title
        ),
        _resolver.xbmc.LOGINFO,
    )
