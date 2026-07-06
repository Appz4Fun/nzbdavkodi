# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import,unused-import

"""NZBGet backend resolver: submit to NZBGet, wait, play from SMB.

Reached only when ``nzbget_enabled`` is true; ``resolver.resolve`` delegates
here. Honors the same ``setResolvedUrl``-on-failure contract as the nzbdav
path: exactly one resolution per exit, failures resolve False.
"""

import threading
import time
from urllib.parse import unquote

import xbmc
import xbmcgui
import xbmcplugin

from resources.lib import nzbget_api
from resources.lib.download_ledger import record_download
from resources.lib.http_util import notify as _notify
from resources.lib.http_util import redact_text as _redact_text
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string
from resources.lib.nzbget_resolver_dupes import (  # noqa: E402,F401
    _HEALTHCHECK_LOCK,
    _HEALTHCHECK_WARNED,
    _MAX_EXTRA_BACKUPS,
    _append_one_backup,
    _cleanup_canceled_submissions,
    _dupe_check_disabled,
    _dupe_worker_should_skip,
    _extra_backups_from_loader,
    _load_extra_candidates,
    _loader_extras_for_fleet,
    _nothing_to_submit,
    _snapshot_conn_getter,
    _spawn_dupe_backups,
    _submit_backup_fleet,
    _submit_dupe_backups,
    _usable_backup_link,
    _warn_if_healthcheck_pauses,
)
from resources.lib.nzbget_resolver_smb import (  # noqa: E402,F401
    _SMB_LIST_RETRY_INTERVAL,
    _SMB_MAX_DEPTH,
    _SMB_RESOLVE_BUDGET,
    VIDEO_EXTENSIONS,
    _drive_resolve_dialog,
    _is_video_name,
    _largest_video_in_dir,
    _largest_video_in_tree,
    _smb_exact_mapping,
    _smb_fallback_mapping,
    _smb_file_size,
    nzbget_smb_target,
    pick_largest_video,
    resolve_smb_video,
)

# Same extensions the WebDAV path uses (webdav.py VIDEO_EXTENSIONS).
_POLL_INTERVAL = 2.0

# listgroups Status values that mean the job is still actively downloading
# (as opposed to a post-processing stage like UNPACKING / REPAIRING /
# MOVING / EXECUTING_SCRIPT, which NZBGet reports as bare in-queue status
# strings while the group is still present). Anything not in this set while
# the job is present is treated as post-processing for the dialog label.
_DOWNLOAD_STATUSES = frozenset({"DOWNLOADING", "FETCHING"})


# After a tracked member fails, wait up to this long for NZBGet to promote a
# duplicate backup into the queue before declaring the whole group failed (#372
# round 2). Promotion is immediate server-side; this only absorbs the poll gap.
_PROMOTION_GRACE = 20


def poll_nzbget_job(
    nzbid,
    dialog,
    monitor,
    timeout,
    settings_getter=None,
    interval=_POLL_INTERVAL,
    dupe_key="",
    is_submitting=None,
):
    """Wait for an NZBGet job (or its duplicate group) to reach a terminal state.

    Returns a dict with "outcome" in {"success","failed","canceled",
    "timeout","aborted"} and, on success, "dest_dir". Drives the progress
    dialog: download % from listgroups, then a post-processing message once the
    job leaves the active queue.

    When ``dupe_key`` is set (#372 round 2) the poll follows NZBGet's automatic
    failover: if the tracked member fails, a promoted backup (a new active NZBID
    under the same DupeKey) is tracked instead, or an already-completed group
    member is played, before the resolve is reported failed. ``is_submitting`` (a
    predicate, the backup worker's ``Thread.is_alive``) keeps the poll from
    declaring the group exhausted while backups are still being appended -- a
    fast-failing pick can hit the promotion grace before a slow indexer's
    ``append_nzb`` has landed a backup.

    ``timeout`` is enforced against the wall clock (``time.monotonic``) so a
    slow/stalled NZBGet box whose RPCs take far longer than ``interval``
    can't stretch the configured budget — see the sibling nzbdav poll loop.
    """
    deadline = time.monotonic() + timeout
    state = {
        "current": nzbid,
        "promotion_deadline": None,
        "exclude": None,
        "paused_nzbids": (),
    }
    if dupe_key:
        state["stale_successes"] = _preexisting_success_ids(dupe_key, settings_getter)
    while time.monotonic() < deadline:
        if dialog.iscanceled():
            # Carry the CURRENTLY tracked NZBID (the promoted backup once
            # failover switched; None in group-follow mode) and any
            # paused-promoted member ids, so the cancel path can final-delete
            # exactly this resolve's downloads.
            return {
                "outcome": "canceled",
                "nzbid": state["current"],
                "paused_nzbids": state["paused_nzbids"],
            }
        terminal = _poll_tick(state, dialog, settings_getter, dupe_key, is_submitting)
        if terminal is not None:
            return terminal
        if monitor.waitForAbort(interval):
            return {"outcome": "aborted"}
    return {"outcome": "timeout"}


def _preexisting_success_ids(dupe_key, settings_getter):
    """Same-key SUCCESS rows already in history when the poll starts (#372 r4).

    Group-follow must IGNORE them: they predate this resolve (their files may
    be long gone -- the picker's reuse probe already declined them), and
    playing one would fail "No video file found" instead of waiting for this
    fleet's own member to complete. Best-effort: an RPC error yields ``()``
    (fail-open to the pre-round-4 behavior).
    """
    try:
        return tuple(
            nzbget_api.success_ids_by_dupekey(dupe_key, settings_getter=settings_getter)
        )
    except Exception:  # pylint: disable=broad-except
        return ()


def _update_active_dialog(dialog, group):
    """Update the progress dialog for an in-queue (still-present) job.

    Shows the download percent while actively downloading, else a
    "Post-processing..." message for every non-download in-queue stage
    (LOADING_PARS, REPAIRING, UNPACKING, MOVING, EXECUTING_SCRIPT,
    QUEUED/PAUSED, ...) instead of a frozen "Downloading... 100%".
    """
    status = (group["status"] or "").upper()
    if status in _DOWNLOAD_STATUSES:
        dialog.update(group["percent"], _fmt(30105, group["percent"]))
    else:
        dialog.update(group["percent"], _string(30219))


def _poll_tick(state, dialog, settings_getter, dupe_key, is_submitting=None):
    """One poll iteration. Returns a terminal outcome dict, or None to continue.

    Tracks ``state["current"]`` (the NZBID being followed). When it leaves the
    queue as a failure and ``dupe_key`` is set, drops into group-follow mode
    (``current=None``): plays an already-completed group member, or switches to a
    promoted backup, or -- if none appears within ``_PROMOTION_GRACE`` -- reports
    the group failed.
    """
    if state["current"] is not None:
        outcome = _tick_tracked_member(state, dialog, settings_getter, dupe_key)
        if outcome is not _FOLLOW_GROUP:
            return outcome
    return _tick_group_follow(state, dialog, settings_getter, dupe_key, is_submitting)


# Sentinel returned by _tick_tracked_member so "the tracked member failed but
# its DupeKey group may fail over" is distinguishable from both a terminal
# outcome dict and the plain keep-waiting None.
_FOLLOW_GROUP = object()


def _tick_tracked_member(state, dialog, settings_getter, dupe_key):
    """Poll the currently tracked NZBID (the ``state["current"]`` branch).

    Returns a terminal outcome dict, None to keep waiting, or ``_FOLLOW_GROUP``
    when the member failed under a ``dupe_key`` and the caller must drop into
    group-follow mode for this same tick (no extra poll interval is lost).
    """
    current = state["current"]
    group = nzbget_api.group_status(current, settings_getter=settings_getter)
    if group["present"]:
        _update_active_dialog(dialog, group)
        state["promotion_deadline"] = None
        return None
    hist = nzbget_api.history_status(current, settings_getter=settings_getter)
    if not hist["present"]:
        # Not in queue, not yet in history — brief hand-off gap; keep waiting.
        dialog.update(100, _string(30219))
        return None
    if hist["success"]:
        return {"outcome": "success", "dest_dir": hist["dest_dir"]}
    if not dupe_key:
        return {"outcome": "failed", "status": hist["status"]}
    # The tracked member failed but a DupeKey group may fail over. Remember
    # its id: NZBGet's queue->history transition is not atomic, so it can
    # still linger in listgroups for a tick -- exclude it below so the
    # promotion scan can't re-select the failed member as its own promotion.
    state["exclude"] = current
    state["current"] = None
    state["promotion_deadline"] = time.monotonic() + _PROMOTION_GRACE
    return _FOLLOW_GROUP


def _tick_group_follow(state, dialog, settings_getter, dupe_key, is_submitting):
    """Group-follow mode: the tracked member failed; follow the DupeKey group.

    Plays an already-completed group member, re-tracks a promoted backup, or --
    once the promotion grace expires with no backup still being appended --
    reports the group exhausted.
    """
    succeeded = nzbget_api.history_success_by_dupekey(
        dupe_key,
        exclude_nzbids=state.get("stale_successes"),
        settings_getter=settings_getter,
    )
    if succeeded["present"]:
        return {"outcome": "success", "dest_dir": succeeded["dest_dir"]}
    promoted = nzbget_api.active_group_by_dupekey(
        dupe_key, exclude_nzbid=state["exclude"], settings_getter=settings_getter
    )
    if promoted["present"]:
        state["current"] = promoted["nzbid"]
        state["promotion_deadline"] = None
        state["paused_nzbids"] = ()
        _update_active_dialog(dialog, promoted)
        return None
    state["paused_nzbids"] = tuple(promoted.get("paused_nzbids") or ())
    if (
        state["promotion_deadline"] is not None
        and time.monotonic() >= state["promotion_deadline"]
    ):
        if not _promotion_still_pending(promoted, is_submitting):
            # No backup was promoted within the grace window -> group exhausted.
            return {"outcome": "failed", "status": "FAILURE/DUPE"}
        # A promotion can still materialize: extend the grace so it keeps its
        # window (bounded by the outer poll timeout either way).
        state["promotion_deadline"] = time.monotonic() + _PROMOTION_GRACE
    dialog.update(100, _string(30219))
    return None


def _promotion_still_pending(promoted, is_submitting):
    """True while group exhaustion must NOT be declared at grace expiry.

    Two waits: the backup worker is still appending candidates (a fast-failing
    pick can beat a slow indexer's 30s NZB fetch), or a same-key member sits
    PAUSED in the queue (e.g. NZBGet globally paused when the backup was
    promoted) -- it can still resume, so the group is not exhausted. Both are
    bounded by the outer poll timeout.
    """
    if is_submitting is not None and is_submitting():
        return True
    return bool(promoted.get("paused_present"))


_DEFAULT_TIMEOUT = 3600
_TIMEOUT_MIN = 60
_TIMEOUT_MAX = 86400

# Probe budget when reusing an already-completed job's folder: the files are
# either visible now or the history row is stale — a short window absorbs a
# share waking up without delaying the fallback submit the way the
# post-download 60s settle budget would.
_SMB_REUSE_PROBE_BUDGET = 3.0

_DEFAULT_POLL_INTERVAL = 1
_POLL_INTERVAL_MIN = 1
_POLL_INTERVAL_MAX = 60


def _bind_getter(settings_getter):
    if settings_getter is not None:
        return settings_getter
    import xbmcaddon

    addon = xbmcaddon.Addon("plugin.video.nzbdav")

    # Kodi's ``Addon.getSetting`` takes a single positional id; the
    # injectable ``settings_getter`` contract is ``(key, default)``. Bind
    # the real getter to that two-arg shape so the call sites don't raise
    # ``TypeError`` under real Kodi (tests inject their own two-arg getter
    # and never hit this branch).
    def getter(key, default=""):
        value = addon.getSetting(key)
        return value if value else default

    return getter


def _read_settings(settings_getter):
    getter = _bind_getter(settings_getter)
    smb_root = getter("nzbget_smb_root", "").strip()
    # Default to the settings.xml schema default so a URL left untouched on the
    # injected-getter (RunScript/widget) path isn't read as empty -> "not
    # configured". See nzbget_api._DEFAULT_URL.
    url = getter("nzbget_url", nzbget_api._DEFAULT_URL).strip()
    try:
        timeout = int(getter("download_timeout", "") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    timeout = max(timeout, _TIMEOUT_MIN)
    timeout = min(timeout, _TIMEOUT_MAX)
    return url, smb_root, timeout


def _read_poll_interval(settings_getter):
    """Read+clamp the shared ``poll_interval`` setting (seconds).

    The NZBGet path honors the same backend-agnostic Polling setting as the
    nzbdav path (range [1..60]) instead of a hardcoded cadence.
    """
    getter = _bind_getter(settings_getter)
    try:
        interval = int(getter("poll_interval", "") or _DEFAULT_POLL_INTERVAL)
    except (TypeError, ValueError):
        interval = _DEFAULT_POLL_INTERVAL
    interval = max(interval, _POLL_INTERVAL_MIN)
    interval = min(interval, _POLL_INTERVAL_MAX)
    return interval


def _resolve_failure(handle, message=None):
    if message:
        _notify(_addon_name(), message, 5000)
    xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
    # Mirror resolver.resolve()'s failure contract: clear the video playlist
    # so Kodi doesn't advance to / retry the stale item TMDBHelper queued for
    # the resolve we just failed (the v0.6.8 retry-loop guard).
    xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()


def _reuse_completed_job(
    completed_job, smb_root, category, completed_base, dialog, interval
):
    """Probe an already-completed history match's SMB folder.

    Returns the playable video URL when the corroborated ``completed_job``
    still holds a video over SMB (the picker-reuse fast path), else ``None``
    so the caller proceeds to the normal submit flow.
    """
    if not (isinstance(completed_job, dict) and completed_job.get("dest_dir")):
        return None
    reuse_folder = nzbget_smb_target(
        smb_root, completed_job["dest_dir"], category, completed_base
    )
    if not reuse_folder:
        return None
    return resolve_smb_video(
        reuse_folder,
        dialog=dialog,
        interval=interval,
        budget=_SMB_REUSE_PROBE_BUDGET,
    )


def _handle_poll_failure(
    outcome,
    nzbid,
    settings_getter,
    on_failure,
    cancel_event=None,
    poll_result=None,
    submitted_nzbids=None,
):
    """Dispatch a non-success poll outcome to its failure callback.

    Returns ``(handled, leave_job)``: ``handled`` is True when ``outcome`` was
    a terminal failure (the caller returns), ``leave_job`` documents the
    timeout/abort policy of deliberately NOT canceling the job so it can
    finish for a later retry. The success outcome returns ``(False, False)``
    so the caller proceeds to the SMB resolve. ``poll_result`` (the poll's
    terminal dict) carries the currently tracked member and any
    paused-promoted member ids; ``submitted_nzbids`` are the backup worker's
    appends so far.
    """
    if outcome in ("timeout", "aborted"):
        on_failure(_string(30101))
        return True, True
    if outcome == "canceled":
        if cancel_event is not None:
            cancel_event.set()  # stop the backup worker first
        nzbget_api.cancel_jobs(
            _canceled_resolve_nzbids(nzbid, poll_result, submitted_nzbids),
            settings_getter=settings_getter,
        )
        on_failure(None)
        return True, False
    if outcome == "failed":
        on_failure(_string(30220))
        return True, False
    return False, False


def _canceled_resolve_nzbids(nzbid, poll_result, submitted_nzbids):
    """Every NZBID this resolve may have running at cancel (#372 round 5).

    ID-SCOPED, never a whole-DupeKey sweep: an overlapping play of the same
    release (another client, or an already-queued retry) shares the stable
    DupeKey and must survive this cancel. Covers the tracked member (the
    promoted backup once failover switched), any paused-promoted members (a
    promotion that landed while NZBGet was paused never becomes tracked), the
    worker's submitted backups (the parked hidden DUP rows -- ``cancel_jobs``
    deletes history before queue, so nothing of OURS is left to promote; a
    manual final-delete does not trigger NZBGet's failover), and the original
    pick. An append still in flight at cancel is covered by the worker's own
    drain cleanup.
    """
    result = poll_result or {}
    ids = []
    for candidate in [
        result.get("nzbid"),
        *(result.get("paused_nzbids") or ()),
        *(submitted_nzbids or []),
        nzbid,
    ]:
        if candidate is not None and candidate not in ids:
            ids.append(candidate)
    return ids


def _resolve_completed_smb(
    dest_dir, smb_root, category, completed_base, dialog, interval
):
    """Map a completed job's DestDir onto SMB and find the playable video.

    Returns the video URL, or ``None`` when the mapping yields no folder or
    no video appears within the resolve budget (both the same caller-facing
    failure, error string 30223).
    """
    smb_folder = nzbget_smb_target(smb_root, dest_dir, category, completed_base)
    if not smb_folder:
        return None
    return resolve_smb_video(smb_folder, dialog=dialog, interval=interval)


class _SubmitCtx:  # pylint: disable=too-few-public-methods
    """Resolved settings + callbacks threaded into the submit flow.

    Bundles everything ``_run_nzbget_backend`` already has (SMB root, category,
    completed base, progress dialog, poll interval, timeout) plus the flow's
    callbacks/getter, so the submit helpers stay within the parameter budget.
    ``settings_getter``, ``on_success``, ``on_failure`` are attached by the
    builder after construction.
    """

    def __init__(self, smb_root, category, completed_base, dialog, interval, timeout):
        self.smb_root = smb_root
        self.category = category
        self.completed_base = completed_base
        self.dialog = dialog
        self.interval = interval
        self.timeout = timeout
        self.settings_getter = None
        self.on_success = None
        self.on_failure = None
        # NZBGet Smart-Duplicates submission (#372): the picker-computed
        # {"key","pick_score","backups"} dict, threaded from the resolve params.
        self.dupe = None
        # Set on user-cancel so the background backup worker stops submitting
        # more duplicates (#372 round 2).
        self.cancel_event = threading.Event()
        # NZBIDs the backup worker has appended so far -- the cancel path
        # deletes exactly these (plus pick/tracked), never a whole-key sweep.
        self.submitted_nzbids = []


def _reuse_or_submit(ctx, nzb_url, title, completed_job, meta):
    """Play a corroborated completed match if present, else run the submit.

    ``meta`` is ``(download_pubdate, download_size)``. Corroborated picker
    reuse: the router tagged this selection against a SUCCESS history row
    (exact name + size/pubdate gates), so play the already-completed files
    instead of re-submitting — NZBGet's duplicate check (DupeCheck=yes by
    default) dupe-deletes a re-submission of a SUCCESS item, which would fail
    the resolve. A probe miss (files cleaned up / share moved) falls through to
    the normal submit flow. Returns ``leave_job`` for the caller's finally.
    """
    reuse_url = _reuse_completed_job(
        completed_job,
        ctx.smb_root,
        ctx.category,
        ctx.completed_base,
        ctx.dialog,
        ctx.interval,
    )
    if reuse_url:
        ctx.on_success(reuse_url)
        return False
    return _submit_poll_resolve(ctx, nzb_url, title, meta[0], meta[1])


def _close_dialog(dialog):
    """Close the progress dialog, swallowing any teardown error."""
    if dialog is None:
        return
    try:
        dialog.close()
    except Exception:  # pylint: disable=broad-except
        pass


def _build_submit_ctx(
    settings_getter,
    smb_root,
    dialog,
    timeout,
    on_success,
    on_failure,
    dupe=None,
):
    """Read the per-submit NZBGet context once for the submit flow.

    Resolves the poll interval, the category (NZBGet nests categorized
    completed output under a per-category subfolder by default, so the SMB
    target must include that segment), and the global completed base used to
    map a history DestDir onto the SMB root regardless of category/custom
    layout (None when unavailable -> nzbget_smb_target falls back). Attaches
    the flow's getter + callbacks so the submit helpers can stay low-arity, plus
    the picker-computed NZBGet Smart-Duplicates submission (#372).

    The getter is kept AS-IS (``None`` on the real Kodi handle-based path): the
    primary submit + poll must read auth via ``_get_settings``'s raw
    ``xbmcaddon`` branch so an intentionally blank ``nzbget_username`` is not
    default-substituted to ``nzbget``. The background backup thread instead reads
    from a main-thread SNAPSHOT (see ``_spawn_dupe_backups``).
    """
    interval = _read_poll_interval(settings_getter)
    _u, _user, _pw, category = nzbget_api._get_settings(settings_getter=settings_getter)
    completed_base = nzbget_api.completed_base_dir(settings_getter=settings_getter)
    ctx = _SubmitCtx(smb_root, category, completed_base, dialog, interval, timeout)
    ctx.settings_getter = settings_getter
    ctx.on_success = on_success
    ctx.on_failure = on_failure
    ctx.dupe = dupe
    return ctx


def _submit_poll_resolve(ctx, nzb_url, title, download_pubdate, download_size):
    """Submit the NZB, poll to completion, then resolve+play the SMB video.

    Returns ``leave_job`` (True only on the timeout/abort policy where the job
    is left running for a later retry) for the caller's finally. We do NOT
    reuse an existing job by name here: a bare name match has no
    size/pubdate/indexer corroboration, so a same-named repost could play the
    wrong job; NZBGet's own dupe handling covers a still-in-flight re-submit.

    When the picker computed a Smart-Duplicates submission (#372), the pick is
    submitted with the shared DupeKey at the top DupeScore so NZBGet keeps it the
    active download (the one this poll tracks); otherwise it is a plain single
    submit unchanged from pre-#372. Once queued, the release's duplicate backups
    are submitted off-thread so NZBGet can fail over to one if the pick is
    unrepairable.
    """
    getter = ctx.settings_getter
    dupe_key = (ctx.dupe or {}).get("key") or ""
    nzbid, error = _submit_pick(ctx, nzb_url, title, dupe_key)
    if not nzbid:
        # Surface the specific (already-redacted) NZBGet message — auth vs dupe
        # vs "append returned 0" — per the spec error table, else the generic.
        ctx.on_failure(error or _string(30222))
        return False

    # Pick is queued at the top DupeScore: submit the release's duplicate backups
    # so NZBGet can fail over to one if the pick is unrepairable (#372). Off-thread
    # so it never delays the poll below; scores (not order) keep the pick active.
    backups_thread = _spawn_dupe_backups(ctx) if dupe_key else None

    def _backups_still_submitting():
        # Don't exhaust the failover grace while the backup worker is still
        # appending candidates (a fast-fail pick can beat a slow indexer).
        return backups_thread is not None and backups_thread.is_alive()

    result = poll_nzbget_job(
        nzbid,
        ctx.dialog,
        xbmc.Monitor(),
        ctx.timeout,
        settings_getter=getter,
        interval=ctx.interval,
        dupe_key=dupe_key,
        is_submitting=_backups_still_submitting,
    )
    handled, leave_job = _handle_poll_failure(
        result["outcome"],
        nzbid,
        getter,
        ctx.on_failure,
        cancel_event=ctx.cancel_event,
        poll_result=result,
        submitted_nzbids=list(getattr(ctx, "submitted_nzbids", None) or []),
    )
    if handled:
        return leave_job
    _play_completed_download(
        ctx, result["dest_dir"], title, download_pubdate, download_size
    )
    return leave_job


def _submit_pick(ctx, nzb_url, title, dupe_key):
    """Append the pick, with the #372 Smart-Duplicates fields when computed.

    The pick carries the shared DupeKey at the top DupeScore so NZBGet keeps it
    the active download; without a ``dupe_key`` this is a plain single submit
    unchanged from pre-#372. Returns ``append_nzb``'s ``(nzbid, error)``.
    """
    dupe = ctx.dupe or {}
    return nzbget_api.append_nzb(
        nzb_url,
        title,
        settings_getter=ctx.settings_getter,
        dupe_key=dupe_key,
        dupe_score=int(dupe.get("pick_score") or 0) if dupe_key else 0,
        dupe_mode="SCORE",
    )


def _play_completed_download(ctx, dest_dir, title, download_pubdate, download_size):
    """Ledger-record the completed download, then resolve+play it over SMB.

    Recording happens BEFORE the SMB mapping, which can still fail without
    un-completing the download (fail-soft): the picker's "DL" tag must reflect
    the box's history even when the share is unreachable right now. The whole
    fleet's post-dates are recorded, not just the pick's: failover can complete
    under ANY same-name backup (a different upload with its own pubdate), and
    the picker's repost-guard only tags rows whose pubdate the ledger knows.
    """
    record_download(title, download_pubdate, download_size)
    _record_fleet_pubdates(getattr(ctx, "dupe", None), title)
    video_url = _resolve_completed_smb(
        dest_dir,
        ctx.smb_root,
        ctx.category,
        ctx.completed_base,
        ctx.dialog,
        ctx.interval,
    )
    if not video_url:
        ctx.on_failure(_string(30223))
        return
    ctx.on_success(video_url)


def _record_fleet_pubdates(dupe, title):
    """Ledger-record every same-name backup's post-date under ``title`` (#372).

    Any fleet member can become the SUCCESS row the next picker render reuses
    (the poll follows a promoted backup), and each is a different upload with
    its own pubdate. Recording the whole fleet keeps the repost-guard's
    purpose intact -- an unrelated same-name repost from another day is still
    rejected (its pubdate is never recorded). Loader extras need no entries:
    NZBHydra collapsed them, so no picker row carries their pubdate; their
    completion tags through the pick's own recorded row. record_download is
    best-effort and dedups epochs, so double-recording is harmless.
    """
    for backup in (dupe or {}).get("backups") or []:
        pubdate = backup.get("pubdate")
        if pubdate:
            record_download(title, pubdate)


def _run_nzbget_backend(
    nzb_url,
    title,
    settings_getter,
    on_success,
    on_failure,
    download_identity=(None, None),
    completed_job=None,
    dupe=None,
):
    """Shared NZBGet flow: reuse completed files, else submit -> poll -> SMB.

    Calls ``on_success(video_url)`` exactly once on success, or
    ``on_failure(message)`` exactly once on any failure (``message`` is ``None``
    for the silent cancel exit); this core guarantees a single terminal callback
    and owns the progress dialog. ``download_identity`` is the
    ``(download_pubdate, download_size)`` pair recording the selected result's
    identity in the ledger for the picker's "DL" tag; ``completed_job`` is the
    corroborated history match played directly (nothing submitted) when its
    ``dest_dir`` still holds a playable video over SMB. ``dupe`` is the
    picker-computed NZBGet Smart-Duplicates submission (#372).
    """
    dialog = None
    leave_job = False
    try:
        url, smb_root, timeout = _read_settings(settings_getter)
        if not all((url, smb_root, nzb_url)):
            on_failure(_string(30221))
            return

        dialog = xbmcgui.DialogProgress()
        dialog.create(_addon_name(), _string(30218))
        ctx = _build_submit_ctx(
            settings_getter,
            smb_root,
            dialog,
            timeout,
            on_success,
            on_failure,
            dupe=dupe,
        )
        leave_job = _reuse_or_submit(
            ctx, nzb_url, title, completed_job, download_identity
        )
    except Exception as exc:  # pylint: disable=broad-except
        # str(exc) can echo the indexer nzb_url (apikey=...) or the
        # smb://user:pass@host root — redact before logging.
        xbmc.log(
            "NZB-DAV: NZBGet resolve error: {}".format(_redact_text(str(exc))),
            xbmc.LOGERROR,
        )
        on_failure(None)
    finally:
        _close_dialog(dialog)
        # leave_job documents the timeout policy: on timeout/abort we
        # deliberately do NOT cancel_job so the download can finish later.
        _ = leave_job


def _apply_resume(listitem, resume_seconds):
    """Set the playback resume point on the ListItem, if any.

    The resolver scrubs the stale plugin/TMDBHelper bookmark before handing
    off to NZBGet and passes its resume position here so a replay continues
    where the user left off instead of restarting from zero.
    """
    try:
        seconds = float(resume_seconds or 0)
    except (TypeError, ValueError):
        return
    if seconds > 0:
        listitem.setProperty("StartOffset", str(seconds))


def _arm_playback_monitor(video_url, resume_seconds, resume_key):
    """Hand the SMB session to the background ``NzbdavPlayer`` monitor.

    Writes the same Home-window properties resolver's
    ``_set_playback_monitor_properties`` sets (same keys/order) so the
    monitor — gated on ``nzbdav.active="true"`` — picks up the NZBGet/SMB
    playback and persists a resume point under ``nzbdav.resume_key`` on
    stop. Without this the SMB path is never monitored, so no resume point is
    ever saved or read for it.

    ``resume_key`` is the resolver-supplied release identity; it falls back to
    ``video_url`` so a direct script/widget play (no identity threaded) still
    keys resume on the playable URL. Mirror resolver's keys but avoid importing
    it here so the two resolvers don't form an import cycle.
    """
    home = xbmcgui.Window(10000)
    home.setProperty("nzbdav.stream_url", video_url)
    home.setProperty("nzbdav.resume_key", resume_key or video_url)
    home.setProperty("nzbdav.resume_offset", str(resume_seconds))
    home.setProperty("nzbdav.stream_title", video_url.rsplit("/", 1)[-1])
    home.setProperty("nzbdav.active", "true")


def resolve_and_play_nzbget(
    handle, params, settings_getter=None, resume_seconds=0.0, resume_key=""
):
    """NZBGet entry for the handle-based ``resolve`` path (``/play``).

    Delivers the finished file via ``setResolvedUrl`` — exactly one
    resolution per exit (success True, every failure False). ``resume_seconds``
    carries the scrubbed bookmark's resume position onto the ListItem;
    ``resume_key`` is the release identity the background monitor persists the
    new resume point under (falling back to the SMB URL when absent).
    """
    nzb_url = unquote(params.get("nzburl", ""))
    title = unquote(params.get("title", "")) or "submission"

    def on_success(video_url):
        listitem = xbmcgui.ListItem(path=video_url)
        _apply_resume(listitem, resume_seconds)
        _arm_playback_monitor(video_url, resume_seconds, resume_key)
        xbmcplugin.setResolvedUrl(handle, True, listitem)

    def on_failure(message):
        _resolve_failure(handle, message)

    _run_nzbget_backend(
        nzb_url,
        title,
        settings_getter,
        on_success,
        on_failure,
        download_identity=(
            params.get("_download_pubdate"),
            params.get("_download_size"),
        ),
        completed_job=params.get("_nzbget_completed_job"),
        dupe=params.get("_nzbget_dupe"),
    )


def play_nzbget(
    nzb_url,
    title,
    params=None,
    settings_getter=None,
    resume_seconds=0.0,
    resume_key="",
):
    """NZBGet entry for the handle-less ``resolve_and_play`` path.

    ``resolve_and_play`` (TMDBHelper ``/resolve``, the in-addon search
    picker, and script-play) has no plugin handle, so the finished SMB file
    is started with ``xbmc.Player().play`` and failures only notify —
    mirroring the nzbdav ``resolve_and_play`` "no setResolvedUrl" contract.
    ``resume_seconds`` carries the scrubbed bookmark's resume position;
    ``resume_key`` is the release identity the background monitor persists the
    new resume point under (falling back to the SMB URL when absent).
    """
    resolve_params = params or {}
    if settings_getter is None:
        settings_getter = resolve_params.get("_settings_getter")

    def on_success(video_url):
        listitem = xbmcgui.ListItem(path=video_url)
        _apply_resume(listitem, resume_seconds)
        _arm_playback_monitor(video_url, resume_seconds, resume_key)
        xbmc.Player().play(video_url, listitem)

    def on_failure(message):
        if message:
            _notify(_addon_name(), message, 5000)

    _run_nzbget_backend(
        nzb_url,
        title,
        settings_getter,
        on_success,
        on_failure,
        download_identity=(
            resolve_params.get("_download_pubdate"),
            resolve_params.get("_download_size"),
        ),
        completed_job=resolve_params.get("_nzbget_completed_job"),
        dupe=resolve_params.get("_nzbget_dupe"),
    )
