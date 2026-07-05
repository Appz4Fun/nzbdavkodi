# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""NZBGet Smart-Duplicates backup fleet: submit, widen, cancel-cleanup (#372).

Cohesive helper group split out of ``nzbget_resolver`` to keep every module
under Codacy's 500-NLOC file gate (same split idiom as
``resolver_fallback_jobs``). References to names that live in (or are patched
via) ``nzbget_resolver`` -- including the sibling helpers themselves, which the
suite patches as ``resources.lib.nzbget_resolver.<name>`` -- are resolved at
call time through ``import resources.lib.nzbget_resolver as _core`` so those
``@patch`` decorators keep intercepting, with no top-level import cycle. Every
moved name is re-exported from ``nzbget_resolver``.
"""

import threading

import resources.lib.nzbget_resolver as _core  # noqa: F401  pylint: disable=unused-import


def _submit_dupe_backups(backups, dupe_key, settings_getter, cancel_event=None):
    """Submit the release's duplicate backups to NZBGet (#372, Smart Duplicates).

    ``backups`` is the picker-computed list of ``{"link","title","score"}`` for
    the same-release-name reposts. Each is appended with the shared ``dupe_key``,
    its own DupeScore (all below the pick's), and DupeMode=SCORE, so NZBGet keeps
    the pick (highest score) downloading and parks each backup in history as a
    duplicate -- failing over to the best remaining one if the pick is
    unrepairable. Because NZBGet decides by score, submission order does not
    matter: a backup submitted even after the pick has already succeeded is put
    into history as a backup (not deleted). Best-effort: a bad/duplicate URL or a
    failed fetch/append for one backup never aborts the rest or the pick. Stops
    early if ``cancel_event`` fires (the user canceled the resolve). Returns the
    list of submitted NZBIDs (for logging/tests).
    """
    submitted = []
    seen = set()
    index = 0
    for backup in backups or []:
        if cancel_event is not None and cancel_event.is_set():
            break
        nzb_url = _core._usable_backup_link(backup, seen)
        if not nzb_url:
            continue
        seen.add(nzb_url)
        index += 1
        nzbid = _core._append_one_backup(
            nzb_url, backup, dupe_key, index, settings_getter
        )
        if nzbid:
            submitted.append(nzbid)
    return submitted


def _usable_backup_link(candidate, seen):
    """The candidate's usable NZB link, or None to skip the row.

    Shared filter for the picker's same-name backups and the loader-widened
    extras: skip non-dict rows (defensive -- both lists are best-effort inputs)
    and links already accepted this pass or already submitted (``seen``), so one
    URL is never appended to NZBGet twice under the same DupeKey.
    """
    if not isinstance(candidate, dict):
        return None
    link = candidate.get("link")
    if not link or link in seen:
        return None
    return link


def _append_one_backup(nzb_url, backup, dupe_key, index, settings_getter):
    """Append one duplicate backup to NZBGet and log the outcome (#372).

    Returns the new NZBID, or None on a failed/raised append -- the caller keeps
    iterating either way (best-effort: one bad backup never aborts the rest).
    """
    from resources.lib.fallback_streams import build_fallback_job_name

    score = int(backup.get("score") or 0)
    job_name = build_fallback_job_name(backup.get("title") or dupe_key, nzb_url, index)
    try:
        nzbid, error = _core.nzbget_api.append_nzb(
            nzb_url,
            job_name,
            settings_getter=settings_getter,
            dupe_key=dupe_key,
            dupe_score=score,
            dupe_mode="SCORE",
        )
    except Exception as exc:  # pylint: disable=broad-except
        _core.xbmc.log(
            "NZB-DAV: NZBGet duplicate backup submit raised: {}".format(
                _core._redact_text(str(exc))
            ),
            _core.xbmc.LOGWARNING,
        )
        return None
    if nzbid:
        _core.xbmc.log(
            "NZB-DAV: Queued NZBGet duplicate backup '{}' (score {})".format(
                job_name, score
            ),
            _core.xbmc.LOGINFO,
        )
        return nzbid
    _core.xbmc.log(
        "NZB-DAV: NZBGet duplicate backup submit failed: {}".format(error),
        _core.xbmc.LOGINFO,
    )
    return None


# Warn about HealthCheck=Pause at most once per Kodi session (a list so the
# module-level flag is mutable from the worker thread; a lock so two concurrent
# resolves' background threads can't both slip through the check-then-set).
_HEALTHCHECK_WARNED = [False]
_HEALTHCHECK_LOCK = threading.Lock()


def _warn_if_healthcheck_pauses(settings_getter):
    """Warn if NZBGet's ``HealthCheck=Pause`` disables automatic dup failover.

    Per nzbget.com/documentation/rss/#duplicates automatic duplicate failover
    needs HealthCheck = Delete, None (or Park); with Pause NZBGet pauses a failed
    download instead of promoting a backup, so the picked release's backups sit
    idle until the user unpauses one. Best-effort -- an unreadable config is
    skipped. Always logs; notifies the user at most once per Kodi session.
    """
    try:
        value = _core.nzbget_api.config_option(
            "HealthCheck", settings_getter=settings_getter
        )
    except Exception:  # pylint: disable=broad-except
        return
    if value != "pause":
        return
    _core.xbmc.log(
        "NZB-DAV: NZBGet HealthCheck=Pause disables automatic duplicate failover; "
        "set it to Delete or None to enable it (#372).",
        _core.xbmc.LOGWARNING,
    )
    with _HEALTHCHECK_LOCK:
        if _HEALTHCHECK_WARNED[0]:
            return
        _HEALTHCHECK_WARNED[0] = True
    _core._notify(_core._addon_name(), _core._string(30230), 6000)


def _snapshot_conn_getter(settings_getter):
    """A thread-safe getter over a main-thread snapshot of NZBGet connection
    settings (#372).

    Read the connection settings once on the calling (main/resolve) thread so the
    background backup worker performs NO off-thread Kodi ``getSetting`` (unsafe on
    CoreELEC/Kodi builds), and preserves a blank ``nzbget_username``/password
    verbatim (``dict.get`` returns the stored ``""`` rather than the auth default
    the addon getter substitutes).
    """
    url, user, password, category = _core.nzbget_api._get_settings(settings_getter)
    snapshot = {
        "nzbget_url": url,
        "nzbget_username": user,
        "nzbget_password": password,
        "nzbget_category": category,
    }
    return lambda key, default="": snapshot.get(key, default)


def _dupe_check_disabled(settings_getter):
    """True only when NZBGet's ``DupeCheck`` option is explicitly ``no``.

    With DupeCheck off NZBGet does not park same-key items as backups -- it would
    download every one as a normal queue item (parallel full downloads). Best-
    effort: an unreadable config returns False (assume the default, on).
    """
    try:
        return (
            _core.nzbget_api.config_option("DupeCheck", settings_getter=settings_getter)
            == "no"
        )
    except Exception:  # pylint: disable=broad-except
        return False


_MAX_EXTRA_BACKUPS = 5


def _extra_backups_from_loader(
    loader, seen_links, limit=_MAX_EXTRA_BACKUPS, score_base=0
):
    """Same-content / NZBHydra-deferred candidates from the fallback loader.

    #372 round 2 widening: beyond the picker's exact same-name rows, the fallback
    loader (an indexer search, already threaded for the nzbdav path) surfaces the
    same-content mirrors and NZBHydra duplicate uploads that were collapsed into a
    single picker row. Returns ``[{"link","title","score"}]`` deduped against
    ``seen_links``, scored DESCENDING from ``score_base`` (the fleet's
    wall-clock base) so they OUTRANK any prior same-key success while sitting
    BELOW every same-name backup, which start at ``score_base + 1`` (a
    last-resort failover, keyed under the pick's DupeKey). Bounded by
    ``limit`` (the standby cap's remaining slots, hard-capped at
    ``_MAX_EXTRA_BACKUPS``) so the total backup count honors the user's
    "Maximum standby fallback streams". Best-effort: a missing/erroring loader,
    its "disabled" sentinel (a non-list), or ``limit <= 0`` yields ``[]``.
    """
    cap = min(limit, _MAX_EXTRA_BACKUPS)
    if loader is None or cap <= 0:
        return []
    extras = []
    seen = set(seen_links or [])
    score = score_base
    for candidate in _core._load_extra_candidates(loader):
        if len(extras) >= cap:
            break
        link = _core._usable_backup_link(candidate, seen)
        if not link:
            continue
        seen.add(link)
        extras.append({"link": link, "title": candidate.get("title"), "score": score})
        score -= 1
    return extras


def _load_extra_candidates(loader):
    """Run the fallback loader, absorbing every failure mode (#372 r2).

    Returns the candidate list, or ``[]`` for an erroring loader or its
    "disabled" sentinel (a non-list) -- the extras are best-effort widening
    only, so a broken indexer search must never surface past here.
    """
    try:
        candidates = loader()
    except Exception:  # pylint: disable=broad-except
        return []
    return candidates if isinstance(candidates, list) else []


def _dupe_worker_should_skip(getter, cancel_event):
    """True when the backup worker must submit nothing (#372).

    Skips silently on a pre-submit cancel (the user already gave up on the
    resolve), and skips with a log when the server has DupeCheck=no -- same-key
    items would then download in parallel instead of parking as backups.
    """
    if cancel_event.is_set():
        return True
    if _core._dupe_check_disabled(getter):
        _core.xbmc.log(
            "NZB-DAV: NZBGet DupeCheck=no -- skipping #372 duplicate "
            "backups (they would download in parallel).",
            _core.xbmc.LOGINFO,
        )
        return True
    return False


def _submit_backup_fleet(getter, cancel_event, dupe_key, dupe, submitted_ids):
    """Submit the same-name backups, then the loader-widened extras (#372).

    Widens with same-content / Hydra-deferred candidates (#372 r2) as
    lowest-priority backups keyed under the same (pick's) DupeKey. Bounds them
    by the standby cap's REMAINING slots so same-name backups + extras never
    exceed "Maximum standby fallback streams", and rides them on the fleet's
    ``score_base`` so they outrank prior same-key successes (#372 r4). A
    loader-only fleet (``backups`` empty, NZBHydra collapsed every mirror into
    one row) submits just the extras. Reads ONLY the snapshot ``getter`` --
    this runs on the worker thread, which must never touch Kodi. Every
    appended NZBID is recorded into ``submitted_ids`` AS IT LANDS so the
    post-cancel cleanup can delete exactly this resolve's submissions.
    """
    backups = list(dupe.get("backups") or [])
    submitted_ids.extend(
        _core._submit_dupe_backups(backups, dupe_key, getter, cancel_event=cancel_event)
        or []
    )
    if cancel_event.is_set():
        return
    extras = _core._loader_extras_for_fleet(dupe, backups)
    if extras:
        submitted_ids.extend(
            _core._submit_dupe_backups(
                extras, dupe_key, getter, cancel_event=cancel_event
            )
            or []
        )


def _loader_extras_for_fleet(dupe, backups):
    """The fleet's loader-widened extras, bounded and score-based (#372 r2/r4).

    Bounds the extras by the standby cap's REMAINING slots (``max_backups``
    minus the same-name backups already spent; ``None`` = the hard extras cap)
    and rides them on the fleet's ``score_base``.
    """
    extras_limit = dupe.get("max_backups")
    remaining = (
        _MAX_EXTRA_BACKUPS
        if extras_limit is None
        else max(0, extras_limit - len(backups))
    )
    return _core._extra_backups_from_loader(
        dupe.get("loader"),
        [b.get("link") for b in backups],
        limit=remaining,
        score_base=int(dupe.get("score_base") or 0),
    )


def _cleanup_canceled_submissions(getter, submitted_ids):
    """Delete exactly THIS worker's submissions after a mid-submit cancel (#372).

    Covers the append that was already in flight when the user canceled and so
    landed after _handle_poll_failure's one-shot DupeKey sweep. Scoped to the
    NZBIDs this resolve submitted -- NEVER a whole-DupeKey sweep, which could
    wipe a fresh retry of the same release (it shares the stable DupeKey) that
    started while this stale worker drained. Best-effort like every other
    backup step: a failed delete is swallowed, never raised off-thread.
    """
    try:
        _core.nzbget_api.cancel_jobs(submitted_ids, settings_getter=getter)
    except Exception:  # pylint: disable=broad-except
        pass


def _nothing_to_submit(dupe_key, dupe):
    """True when the worker has no possible submission (#372 r4).

    No key means no Smart-Duplicates fleet at all; with a key, an empty
    same-name list is still submittable when a loader exists to widen from
    (the NZBHydra collapsed-mirrors case -- a loader-only fleet).
    """
    if not dupe_key:
        return True
    return not dupe.get("backups") and dupe.get("loader") is None


def _spawn_dupe_backups(ctx):
    """Fire-and-forget the release's duplicate backups in a daemon thread (#372).

    Runs off the resolve thread (each backup is an indexer HTTP round-trip) so it
    never delays the pick's poll/progress ("it won't affect playback"); the
    daemon flag keeps it from blocking Kodi shutdown. Because every item carries
    an explicit DupeScore (the pick highest), NZBGet keeps the pick the active
    download regardless of when the backups land -- so submission order is not a
    concern and a backup arriving after the pick already succeeded is still put
    into history as a backup, not deleted. Skips entirely when the server has
    DupeCheck disabled (backups would download in parallel), and warns once if
    HealthCheck=Pause would block automatic failover. Reads settings from a
    main-thread snapshot so the worker never touches Kodi off-thread. All errors
    are swallowed -- backups are pure insurance and must never break playback.
    """
    dupe = ctx.dupe or {}
    dupe_key = dupe.get("key") or ""
    if _core._nothing_to_submit(dupe_key, dupe):
        return None
    try:
        getter = _core._snapshot_conn_getter(ctx.settings_getter)
    except Exception as exc:  # pylint: disable=broad-except
        # The snapshot reads Kodi/injected settings and runs AFTER the primary is
        # already accepted. Backups are pure insurance -- a settings-read failure
        # here must skip them, never propagate out and fail the primary's playback.
        _core.xbmc.log(
            "NZB-DAV: NZBGet duplicate backup snapshot failed: {}".format(
                _core._redact_text(str(exc))
            ),
            _core.xbmc.LOGWARNING,
        )
        return None
    cancel_event = ctx.cancel_event

    def _worker():
        reached_submit = False
        submitted_ids = []
        try:
            if _core._dupe_worker_should_skip(getter, cancel_event):
                return
            _core._warn_if_healthcheck_pauses(getter)
            reached_submit = True
            _core._submit_backup_fleet(
                getter, cancel_event, dupe_key, dupe, submitted_ids
            )
        except Exception as exc:  # pylint: disable=broad-except
            _core.xbmc.log(
                "NZB-DAV: NZBGet duplicate backup worker error: {}".format(
                    _core._redact_text(str(exc))
                ),
                _core.xbmc.LOGWARNING,
            )
        finally:
            # If a cancel arrived while a backup's append was already in flight,
            # that backup can land in NZBGet AFTER _handle_poll_failure's
            # one-shot cancel_dupekey_group sweep -- and NZBGet would then
            # promote the orphan as the group's new active download. Clean up
            # once the worker has drained, scoped to this resolve's own
            # submissions (#372 r2 cancel-race, r3 retry-race).
            if reached_submit and cancel_event.is_set() and submitted_ids:
                _core._cleanup_canceled_submissions(getter, submitted_ids)

    try:
        thread = threading.Thread(
            target=_worker, name="nzbdav-nzbget-dupe-backups", daemon=True
        )
        thread.start()
    except Exception as exc:  # pylint: disable=broad-except
        # e.g. RuntimeError "can't start new thread" under thread exhaustion.
        # The backups are pure insurance -- never let them break the already-
        # queued pick's playback.
        _core.xbmc.log(
            "NZB-DAV: NZBGet duplicate backup spawn failed: {}".format(
                _core._redact_text(str(exc))
            ),
            _core.xbmc.LOGWARNING,
        )
        return None
    return thread
