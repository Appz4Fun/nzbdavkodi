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
    _MAX_VETO_REPLACEMENTS,
    _append_one_backup,
    _canceled_resolve_nzbids,
    _cleanup_canceled_submissions,
    _copy_vetoed_after_append,
    _dupe_check_disabled,
    _dupe_worker_should_skip,
    _extra_backups_from_loader,
    _is_copy_failure,
    _is_copy_veto_status,
    _load_extra_candidates,
    _loader_extras_for_fleet,
    _nothing_to_submit,
    _pick_rescue_callable,
    _preexisting_success_ids,
    _read_poll_interval,
    _rescue_or_exhausted,
    _rescue_plain_pick,
    _snapshot_conn_getter,
    _spawn_dupe_backups,
    _submit_backup_fleet,
    _submit_dupe_backups,
    _submit_extras_until_filled,
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
    _report_smb_inventory,
    _smb_exact_mapping,
    _smb_fallback_mapping,
    _smb_file_size,
    _smb_inventory,
    _smb_video_candidates_in_tree,
    nzbget_smb_target,
    pick_largest_video,
    resolve_smb_video,
)
from resources.lib.season_pack import requested_episode as _requested_episode

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

# Follow-mode grace (seconds) when the pick died DELETED/COPY (#372 r6): the
# pick never entered the queue, so no server-side failover is pending for it --
# only the worker's own appends can surface a sibling (and ``is_submitting``
# already extends the wait for that). A short grace turns a wasted ~20s stall
# into a prompt rescue/exhaustion decision. Defined here (not in
# nzbget_resolver_dupes.py) because this is its only consumer.
_COPY_VETO_GRACE = 5


def poll_nzbget_job(
    nzbid,
    dialog,
    monitor,
    timeout,
    settings_getter=None,
    interval=_POLL_INTERVAL,
    dupe_key="",
    fleet=None,
):
    """Wait for an NZBGet job (or its duplicate group) to reach a terminal state.

    Returns a dict with "outcome" in {"success","failed","canceled",
    "timeout","aborted"} and, on success, the exact terminal ``nzbid`` plus
    ``job_name`` and ``dest_dir``. Drives the progress dialog: download % from
    listgroups, then a post-processing message once the job leaves the active
    queue.

    When ``dupe_key`` is set (#372 round 2) the poll follows NZBGet's automatic
    failover: if the tracked member fails, a promoted backup (a new active NZBID
    under the same DupeKey) is tracked instead, or an already-completed group
    member is played, before the resolve is reported failed. ``fleet`` carries
    two callables: ``is_submitting`` (the backup worker's ``Thread.is_alive``)
    keeps the poll from declaring the group exhausted while backups are still
    being appended, and ``owned_nzbids`` (the pick + the worker's appends so
    far) scopes failover tracking to THIS resolve -- an overlapping play of the
    same release shares the stable DupeKey, and its active download must never
    be adopted (or later canceled). NZBGet preserves NZBIDs across
    history<->queue moves, so our promoted backup always surfaces under an id
    we submitted.

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
        # #372 r6 COPY-veto rescue: the original pick id, a sticky flag set when
        # THAT pick died DELETED/COPY (never entered the queue), and a one-shot
        # guard so the FORCE re-submit is attempted at most once per resolve.
        "pick": nzbid,
        "copy_vetoed": False,
        "rescued": False,
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
        terminal = _poll_tick(state, dialog, settings_getter, dupe_key, fleet)
        if terminal is not None:
            return terminal
        if monitor.waitForAbort(interval):
            return {"outcome": "aborted"}
    return {"outcome": "timeout"}


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


def _poll_tick(state, dialog, settings_getter, dupe_key, fleet=None):
    """One poll iteration. Returns a terminal outcome dict, or None to continue.

    Tracks ``state["current"]`` (the NZBID being followed). When it leaves the
    queue as a failure and ``dupe_key`` is set, drops into group-follow mode
    (``current=None``): plays an already-completed group member, or switches to a
    promoted backup, or -- if none appears within ``_PROMOTION_GRACE`` -- reports
    the group failed.
    """
    if state["current"] is not None:
        outcome = _tick_tracked_member(state, dialog, settings_getter, dupe_key, fleet)
        if outcome is not _FOLLOW_GROUP:
            return outcome
    return _tick_group_follow(state, dialog, settings_getter, dupe_key, fleet)


# Sentinel returned by _tick_tracked_member so "the tracked member failed but
# its DupeKey group may fail over" is distinguishable from both a terminal
# outcome dict and the plain keep-waiting None.
_FOLLOW_GROUP = object()


def _tick_tracked_member(state, dialog, settings_getter, dupe_key, fleet=None):
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
    return _tick_tracked_member_terminal(state, hist, current, dupe_key, fleet)


def _tick_tracked_member_terminal(state, hist, current, dupe_key, fleet):
    """Handle the tracked member's terminal history row (#372 r6 split).

    Extracted from ``_tick_tracked_member`` (Codacy complexity feedback on
    PR #406): the group-absent/history-absent "keep waiting" checks stay in
    the caller, this handles every terminal outcome once ``hist`` is present
    -- success, a plain-submit COPY-veto rescue, or arming DupeKey
    group-follow. Same return contract as ``_tick_tracked_member``.
    """
    if hist["success"]:
        return {
            "outcome": "success",
            "dest_dir": hist["dest_dir"],
            "nzbid": hist.get("nzbid", current),
            "job_name": hist.get("job_name", ""),
        }
    if not dupe_key:
        # Plain submit: a DELETED/COPY veto (content already in history, never
        # queued) is recoverable by a one-shot FORCE re-submit (#372 r6);
        # anything else ends the poll as before.
        if _is_copy_veto_status(hist["status"]) and _rescue_plain_pick(state, fleet):
            return None
        return {"outcome": "failed", "status": hist["status"]}
    # The tracked member failed but a DupeKey group may fail over. Remember
    # its id: NZBGet's queue->history transition is not atomic, so it can
    # still linger in listgroups for a tick -- exclude it below so the
    # promotion scan can't re-select the failed member as its own promotion.
    state["exclude"] = current
    state["current"] = None
    # Only the ORIGINAL pick's COPY death arms the FORCE rescue: it never got a
    # real attempt, so no server-side promotion is coming from it -- a short
    # grace instead of the full 20s (a later-adopted backup's genuine failure
    # neither sets nor clears the sticky flag; the pick still earns its rescue).
    grace = _PROMOTION_GRACE
    if current == state.get("pick") and _is_copy_veto_status(hist["status"]):
        state["copy_vetoed"] = True
        grace = _COPY_VETO_GRACE
    state["promotion_deadline"] = time.monotonic() + grace
    return _FOLLOW_GROUP


def _tick_group_follow(state, dialog, settings_getter, dupe_key, fleet):
    """Group-follow mode: the tracked member failed; follow the DupeKey group.

    Plays an already-completed group member, re-tracks a promoted backup THIS
    resolve owns, or -- once the promotion grace expires with nothing pending
    -- reports the group exhausted. A foreign same-key active (an overlapping
    play of the same release) is never adopted: the poll holds instead, its
    SUCCESS is played via the history lookup, and its failure frees the key
    for OUR parked backups.
    """
    succeeded = nzbget_api.history_success_by_dupekey(
        dupe_key,
        exclude_nzbids=state.get("stale_successes"),
        settings_getter=settings_getter,
    )
    if succeeded["present"]:
        return {
            "outcome": "success",
            "dest_dir": succeeded["dest_dir"],
            "nzbid": succeeded.get("nzbid"),
            "job_name": succeeded.get("job_name", ""),
        }
    promoted = nzbget_api.active_group_by_dupekey(
        dupe_key, exclude_nzbid=state["exclude"], settings_getter=settings_getter
    )
    if _adopt_owned_promotion(state, dialog, promoted, fleet):
        return None
    foreign_active = promoted["present"]
    state["paused_nzbids"] = _owned_paused_ids(promoted, fleet)
    if (
        state["promotion_deadline"] is not None
        and time.monotonic() >= state["promotion_deadline"]
    ):
        if _promotion_still_pending(promoted, fleet, foreign_active):
            # A promotion can still materialize: extend the grace so it keeps
            # its window (bounded by the outer poll timeout either way). A
            # COPY-vetoed pick re-arms on its OWN short grace, not the full
            # 20s -- nothing server-side is pending for it either way, so
            # re-checking promptly after the worker drains still matters
            # (Codex review on PR #406: re-arming on _PROMOTION_GRACE here
            # defeated the whole point of the short grace).
            grace = _COPY_VETO_GRACE if state.get("copy_vetoed") else _PROMOTION_GRACE
            state["promotion_deadline"] = time.monotonic() + grace
        else:
            # No backup was promoted within the grace window. If the pick died
            # DELETED/COPY, attempt the one-shot FORCE rescue before declaring
            # the group exhausted (#372 r6); otherwise this is the unchanged
            # FAILURE/DUPE outcome.
            terminal = _rescue_or_exhausted(state, fleet)
            if terminal is not None:
                return terminal
            # Rescued: state["current"] now tracks the FORCE re-submit; fall
            # through to the dialog update and keep polling.
    dialog.update(100, _string(30219))
    return None


def _adopt_owned_promotion(state, dialog, promoted, fleet):
    """Track a promoted backup THIS resolve owns; False otherwise (#372 r5).

    A present-but-foreign active (an overlapping play of this release under
    the same stable DupeKey) is left to the caller to HOLD on -- it must never
    become ``state["current"]``, which the cancel path final-deletes.
    """
    if not promoted["present"] or not _owned_nzbid(promoted["nzbid"], fleet):
        return False
    state["current"] = promoted["nzbid"]
    state["promotion_deadline"] = None
    state["paused_nzbids"] = ()
    _update_active_dialog(dialog, promoted)
    return True


def _owned_paused_ids(promoted, fleet):
    """This resolve's paused-promoted member ids from the promotion scan.

    Foreign paused same-key rows (another play's) are dropped -- they must
    never reach the cancel set.
    """
    return tuple(
        nzbid
        for nzbid in promoted.get("paused_nzbids") or ()
        if _owned_nzbid(nzbid, fleet)
    )


def _owned_nzbid(nzbid, fleet):
    """Whether ``nzbid`` belongs to THIS resolve (#372 round 5).

    ``fleet["owned_nzbids"]`` returns the pick plus every backup the worker has
    appended so far; NZBGet preserves NZBIDs across history<->queue moves, so a
    promoted backup of OURS always matches. Without a fleet (plain polls,
    direct test calls) everything counts as owned -- the pre-round-5 behavior.
    """
    owned = (fleet or {}).get("owned_nzbids")
    if owned is None:
        return True
    return nzbid in tuple(owned())


def _promotion_still_pending(promoted, fleet, foreign_active=False):
    """True while group exhaustion must NOT be declared at grace expiry.

    Three waits: the backup worker is still appending candidates (a
    fast-failing pick can beat a slow indexer's 30s NZB fetch); a same-key
    member sits PAUSED in the queue (e.g. NZBGet globally paused when the
    backup was promoted) -- it can still resume; or a FOREIGN same-key active
    exists (an overlapping play of this release) -- its outcome will either
    hand us a playable SUCCESS or free the key for our backups. All bounded by
    the outer poll timeout.
    """
    is_submitting = (fleet or {}).get("is_submitting")
    if is_submitting is not None and is_submitting():
        return True
    return foreign_active or bool(promoted.get("paused_present"))


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


def _resolve_failure(handle, message=None):
    if message:
        _notify(_addon_name(), message, 5000)
    xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())
    # Mirror resolver.resolve()'s failure contract: clear the video playlist
    # so Kodi doesn't advance to / retry the stale item TMDBHelper queued for
    # the resolve we just failed (the v0.6.8 retry-loop guard).
    xbmc.PlayList(xbmc.PLAYLIST_VIDEO).clear()


def _reuse_completed_job(  # pylint: disable=too-many-arguments
    completed_job,
    smb_root,
    category,
    completed_base,
    dialog,
    interval,
    requested_episode=None,
    on_inventory=None,
    *,
    episode_context=None,
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
    if requested_episode is None:
        requested_episode = _requested_episode(episode_context)
    if on_inventory is None and episode_context is not None:
        from resources.lib.season_pack_recording import inventory_recorder

        on_inventory = inventory_recorder(
            "nzbget",
            completed_job.get("nzbid"),
            completed_job.get("name", ""),
            completed_job.get("dest_dir"),
            episode_context,
        )
    return resolve_smb_video(
        reuse_folder,
        dialog=dialog,
        interval=interval,
        budget=_SMB_REUSE_PROBE_BUDGET,
        requested_episode=requested_episode,
        on_inventory=on_inventory,
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
        # A COPY-shaped terminal (the FORCE rescue could not be performed or was
        # itself refused) gets the honest "already in history, re-queue failed"
        # message; every other failure keeps the generic one (#372 r6).
        on_failure(_string(30231) if _is_copy_failure(poll_result) else _string(30220))
        return True, False
    return False, False


def _resolve_completed_smb(  # pylint: disable=too-many-arguments
    dest_dir,
    smb_root,
    category,
    completed_base,
    dialog,
    interval,
    requested_episode=None,
    on_inventory=None,
    *,
    episode_context=None,
    catalog_job=None,
):
    """Map a completed job's DestDir onto SMB and find the playable video.

    Returns the video URL, or ``None`` when the mapping yields no folder or
    no video appears within the resolve budget (both the same caller-facing
    failure, error string 30223).
    """
    smb_folder = nzbget_smb_target(smb_root, dest_dir, category, completed_base)
    if not smb_folder:
        return None
    if requested_episode is None:
        requested_episode = _requested_episode(episode_context)
    if on_inventory is None and episode_context is not None and catalog_job:
        from resources.lib.season_pack_recording import inventory_recorder

        on_inventory = inventory_recorder(
            "nzbget",
            catalog_job.get("job_id"),
            catalog_job.get("job_name", ""),
            catalog_job.get("folder"),
            episode_context,
        )
    return resolve_smb_video(
        smb_folder,
        dialog=dialog,
        interval=interval,
        requested_episode=requested_episode,
        on_inventory=on_inventory,
    )


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
        self.episode_context = None
        self.season_pack_record = None
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
    the resolve. A pack-row miss fails that explicit selection without
    submitting its neighboring online rows. Returns ``leave_job`` for the
    caller's finally.
    """
    if ctx.season_pack_record is not None:
        from resources.lib.season_pack_reuse import reuse_exact_job

        pack_reuse = reuse_exact_job(
            ctx.season_pack_record,
            ctx.episode_context,
            "nzbget",
            settings_getter=ctx.settings_getter,
        )
        if pack_reuse.state == "valid":
            ctx.on_success(pack_reuse.stream_url)
            return False
        if pack_reuse.state == "stale":
            try:
                _notify(_addon_name(), _string(30365), 4000)
            except Exception:  # pylint: disable=broad-except
                # Kodi rejecting the toast must not interrupt failure handling.
                pass
        # A pack row is one explicit selection, never shorthand for an online
        # provider. Stale and transient validation both fail closed; the user
        # can choose an ordinary result separately.
        failure_message = None if pack_reuse.state == "stale" else _string(30223)
        ctx.on_failure(failure_message)
        return False

    reuse_url = _reuse_completed_job(
        completed_job,
        ctx.smb_root,
        ctx.category,
        ctx.completed_base,
        ctx.dialog,
        ctx.interval,
        episode_context=getattr(ctx, "episode_context", None),
    )
    if reuse_url:
        ctx.on_success(reuse_url)
        return False
    if not nzb_url:
        ctx.on_failure(_string(30223))
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

    def _owned_fleet_nzbids():
        # The pick plus every backup appended so far -- failover tracking and
        # cancel stay scoped to exactly this resolve's downloads.
        return [nzbid] + list(getattr(ctx, "submitted_nzbids", None) or [])

    result = poll_nzbget_job(
        nzbid,
        ctx.dialog,
        xbmc.Monitor(),
        ctx.timeout,
        settings_getter=getter,
        interval=ctx.interval,
        dupe_key=dupe_key,
        fleet={
            "is_submitting": _backups_still_submitting,
            "owned_nzbids": _owned_fleet_nzbids,
            # #372 r6: a confirmed COPY veto (pick died DELETED/COPY, group
            # otherwise exhausted) is recovered by a one-shot FORCE re-submit of
            # the pick. Built on both the fleet and plain paths (the dict is
            # always passed to the poll).
            "rescue": _pick_rescue_callable(ctx, nzb_url, title),
        },
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
        ctx,
        result["dest_dir"],
        title,
        download_pubdate,
        download_size,
        job_id=result.get("nzbid"),
        job_name=result.get("job_name") or title,
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


def _play_completed_download(
    ctx,
    dest_dir,
    title,
    download_pubdate,
    download_size,
    job_id=None,
    job_name="",
):
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
        episode_context=getattr(ctx, "episode_context", None),
        catalog_job={
            "job_id": job_id,
            "job_name": job_name or title,
            "folder": dest_dir,
        },
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


def _run_nzbget_backend(  # pylint: disable=too-many-arguments
    nzb_url,
    title,
    settings_getter,
    on_success,
    on_failure,
    download_identity=(None, None),
    completed_job=None,
    dupe=None,
    *,
    season_pack_record=None,
    episode_context=None,
):
    """Shared NZBGet flow: reuse completed files, else submit -> poll -> SMB.

    Calls ``on_success(video_url)`` exactly once on success, or
    ``on_failure(message)`` exactly once on any failure (``message`` is ``None``
    for the silent cancel exit); this core guarantees a single terminal callback
    and owns the progress dialog. ``download_identity`` is the
    ``(download_pubdate, download_size)`` pair recording the selected result's
    identity in the ledger for the picker's "DL" tag; ``episode_context``
    supplies the exact season/episode requested by the router. ``completed_job``
    is the corroborated history match played directly (nothing submitted) when
    its ``dest_dir`` still holds a playable video over SMB. ``dupe`` is the
    picker-computed NZBGet Smart-Duplicates submission (#372).
    """
    dialog = None
    leave_job = False
    try:
        url, smb_root, timeout = _read_settings(settings_getter)
        if not all((url, smb_root)):
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
        ctx.episode_context = (
            dict(episode_context) if isinstance(episode_context, dict) else None
        )
        ctx.season_pack_record = (
            dict(season_pack_record) if isinstance(season_pack_record, dict) else None
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
        season_pack_record=params.get("_season_pack"),
        episode_context=params.get("_episode_context"),
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
        season_pack_record=resolve_params.get("_season_pack"),
        episode_context=resolve_params.get("_episode_context"),
    )
