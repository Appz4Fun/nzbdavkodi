# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

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
import xbmcvfs

from resources.lib import nzbget_api
from resources.lib.download_ledger import record_download
from resources.lib.http_util import notify as _notify
from resources.lib.http_util import redact_text as _redact_text
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string

# Same extensions the WebDAV path uses (webdav.py VIDEO_EXTENSIONS).
VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")


def nzbget_smb_target(smb_root, dest_dir, category="", completed_base=""):
    """Map NZBGet's server-local DestDir onto the SMB root.

    ``smb_root`` points at NZBGet's *completed* base dir, exposed over SMB.

    Preferred mapping (exact): when ``completed_base`` (NZBGet's configured
    global completed DestDir) is known and is a prefix of ``dest_dir``, map the
    *relative* remainder onto ``smb_root``. This mirrors whatever subfolder
    layout NZBGet actually used — ``AppendCategoryDir`` on/off, a category that
    matches or a category-specific/custom ``DestDir`` whose folder name differs
    from the category setting (e.g. ``<completed>/films/<release>`` for category
    ``movies``) — without guessing.

    Fallback heuristic (``completed_base`` unknown / ``dest_dir`` outside it):
    derive the category segment from ``dest_dir``'s own parent. Nest under it
    only when a category is configured and the parent isn't already the SMB
    root's own trailing segment (the AppendCategoryDir=no case, where the
    parent is the completed base itself). Returns the smb:// folder URL, or
    None if dest_dir is empty.
    """
    if not dest_dir:
        return None
    normalized = dest_dir.replace("\\", "/").rstrip("/")
    base = smb_root.rstrip("/")

    exact = _smb_exact_mapping(normalized, base, completed_base)
    if exact is not None:
        return exact

    return _smb_fallback_mapping(normalized, base, category)


def _smb_exact_mapping(normalized, base, completed_base):
    """Map ``dest_dir`` onto ``base`` via the configured completed base.

    Returns the smb:// URL when ``completed_base`` is known and a strict
    prefix of ``dest_dir``, else ``None`` so the caller falls back to the
    heuristic.
    """
    cb = (completed_base or "").replace("\\", "/").rstrip("/")
    if not cb or normalized.casefold() == cb.casefold():
        return None
    if not (normalized + "/").casefold().startswith(cb.casefold() + "/"):
        return None
    rel = normalized[len(cb) :].strip("/")
    # Guard against a doubled tail when smb_root was pointed at a
    # subfolder of the completed base (e.g. root .../movies + rel
    # movies/Release): drop rel's leading segment if base already ends
    # with it.
    rel_head = rel.split("/", 1)[0]
    if rel_head and base.casefold().endswith("/" + rel_head.casefold()):
        rel = rel[len(rel_head) :].strip("/")
    if rel:
        return "{}/{}".format(base, rel)
    return None


def _smb_fallback_mapping(normalized, base, category):
    """Derive the SMB target from ``dest_dir``'s own tail segments.

    Used when the completed base is unknown / outside ``dest_dir``. Returns
    the smb:// URL, or ``None`` when ``dest_dir`` has no usable segments.
    """
    segments = list(filter(None, normalized.split("/")))
    if not segments:
        return None
    release_folder = segments[-1]
    parent_folder = segments[-2] if len(segments) >= 2 else ""
    category = (category or "").strip().strip("/")
    # Without the completed base we can't reliably tell a real category
    # subfolder apart from the completed base's own last segment (the
    # server-side folder name may differ from the SMB share alias). So only
    # nest when DestDir's parent is *exactly* the configured category — the
    # one case we can be sure about. Otherwise map the release folder directly
    # under the SMB root (correct for AppendCategoryDir=no, and the safe
    # default for a custom DestDir whose layout we can't confirm).
    if (
        category
        and parent_folder.casefold() == category.casefold()
        and not base.casefold().endswith("/" + category.casefold())
    ):
        return "{}/{}/{}".format(base, parent_folder, release_folder)
    return "{}/{}".format(base, release_folder)


def pick_largest_video(filenames, size_of):
    """Return the largest video file from a list, or None.

    ``size_of`` is a callable mapping filename -> size (bytes). Mirrors the
    addon's "largest video wins" rule.
    """
    best = None
    best_size = -1
    for name in filenames:
        lower = name.lower()
        if not any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS):
            continue
        size = size_of(name)
        if size > best_size:
            best = name
            best_size = size
    return best


_SMB_LIST_RETRY_INTERVAL = 1.0
# Total wall-clock budget to keep re-listing the SMB share after NZBGet
# reports SUCCESS. NZBGet only enters history once its move is marked
# complete, but the moved files can take a while longer to become listable
# over the Samba export (observed: the containing folder's mtime landing at
# the exact second we start looking). The old ~4s (5×1s) window lost that
# race and failed with "No video file found on SMB share" even though the
# download succeeded. 60s absorbs the visibility lag with wide margin.
_SMB_RESOLVE_BUDGET = 60.0
# Releases whose archive unpacks into a nested ``<release>/<inner>/video``
# layout are common; descend a few levels (like the WebDAV resolver) so they
# still resolve. Bounded to keep a pathological tree from stalling playback.
_SMB_MAX_DEPTH = 3


def _smb_file_size(path):
    try:
        return xbmcvfs.Stat(path).st_size()
    except Exception:  # pylint: disable=broad-except
        return 0


def _is_video_name(name):
    """True when ``name`` ends in one of the playable video extensions."""
    lower = name.lower()
    return any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS)


def _largest_video_in_dir(folder, files):
    """Return ``(url, size)`` for the largest video file directly in ``folder``.

    Considers only the given ``files`` (no descent). Returns ``(None, -1)``
    when none are playable videos.
    """
    best, best_size = None, -1
    for name in files:
        if not _is_video_name(name):
            continue
        path = "{}/{}".format(folder, name)
        size = _smb_file_size(path)
        if size > best_size:
            best, best_size = path, size
    return best, best_size


def _largest_video_in_tree(folder, depth=_SMB_MAX_DEPTH):
    """Return ``(url, size)`` for the largest video at or below ``folder``.

    Descends up to ``depth`` levels of subdirectories so a video tucked
    inside a top-level folder (a common archive layout) still resolves
    instead of failing with "No video file found on SMB share". Returns
    ``(None, -1)`` when nothing playable is found.
    """
    try:
        dirs, files = xbmcvfs.listdir(folder)
    except Exception:  # pylint: disable=broad-except
        return None, -1
    best, best_size = _largest_video_in_dir(folder, files)
    if depth > 0:
        for sub in dirs:
            url, size = _largest_video_in_tree("{}/{}".format(folder, sub), depth - 1)
            if url is not None and size > best_size:
                best, best_size = url, size
    return best, best_size


def _drive_resolve_dialog(dialog, now, deadline, budget):
    """Advance the resolve progress bar; return True if the user canceled.

    No-op (returns False) when no ``dialog`` is supplied. Otherwise drives the
    progress bar over the resolve window so the user sees a "finishing up"
    indicator instead of being dropped back to the home screen while the file
    settles onto the share.
    """
    if dialog is None:
        return False
    if dialog.iscanceled():
        return True
    elapsed = budget - max(0.0, deadline - now)
    percent = int(min(100.0, elapsed * 100.0 / budget)) if budget else 100
    dialog.update(percent, _string(30219))
    return False


def resolve_smb_video(
    smb_folder,
    monitor=None,
    dialog=None,
    interval=_SMB_LIST_RETRY_INTERVAL,
    budget=_SMB_RESOLVE_BUDGET,
):
    """List an SMB folder and return the largest video file URL, or None.

    Searches the folder tree (top level plus nested subdirectories, see
    ``_largest_video_in_tree``) and keeps retrying until a video appears or
    the wall-clock ``budget`` (seconds, ``time.monotonic``) elapses — long
    enough to absorb the lag between NZBGet reporting SUCCESS and the moved
    files becoming visible over SMB. Sleeps ``interval`` seconds between
    attempts via ``Monitor.waitForAbort`` so it stays cancelable and honors
    Kodi shutdown. When a ``dialog`` (DialogProgress) is supplied, it shows a
    progress bar over the wait and its Cancel button aborts the search.
    Returns None if no video file appears within the budget.
    """
    if monitor is None:
        monitor = xbmc.Monitor()
    deadline = time.monotonic() + budget
    while True:
        url, _size = _largest_video_in_tree(smb_folder)
        if url is not None:
            return url
        now = time.monotonic()
        if now >= deadline:
            return None
        if _drive_resolve_dialog(dialog, now, deadline, budget):
            return None
        if monitor.waitForAbort(interval):
            return None


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
    state = {"current": nzbid, "promotion_deadline": None, "exclude": None}
    while time.monotonic() < deadline:
        if dialog.iscanceled():
            return {"outcome": "canceled"}
        terminal = _poll_tick(state, dialog, settings_getter, dupe_key, is_submitting)
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
        dupe_key, settings_getter=settings_getter
    )
    if succeeded["present"]:
        return {"outcome": "success", "dest_dir": succeeded["dest_dir"]}
    promoted = nzbget_api.active_group_by_dupekey(
        dupe_key, exclude_nzbid=state["exclude"], settings_getter=settings_getter
    )
    if promoted["present"]:
        state["current"] = promoted["nzbid"]
        state["promotion_deadline"] = None
        _update_active_dialog(dialog, promoted)
        return None
    if (
        state["promotion_deadline"] is not None
        and time.monotonic() >= state["promotion_deadline"]
    ):
        if is_submitting is not None and is_submitting():
            # Backups are still being appended (each NZB fetch can take up to the
            # 30s timeout); a fast-failing pick must not be reported failed before
            # its backups can reach NZBGet. Extend the grace so a backup that lands
            # after this point still gets a promotion window.
            state["promotion_deadline"] = time.monotonic() + _PROMOTION_GRACE
            dialog.update(100, _string(30219))
            return None
        # No backup was promoted within the grace window -> group exhausted.
        return {"outcome": "failed", "status": "FAILURE/DUPE"}
    dialog.update(100, _string(30219))
    return None


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
    outcome, nzbid, settings_getter, on_failure, dupe_key="", cancel_event=None
):
    """Dispatch a non-success poll outcome to its failure callback.

    Returns ``(handled, leave_job)``: ``handled`` is True when ``outcome`` was
    a terminal failure (the caller returns), ``leave_job`` documents the
    timeout/abort policy of deliberately NOT canceling the job so it can
    finish for a later retry. The success outcome returns ``(False, False)``
    so the caller proceeds to the SMB resolve.
    """
    if outcome in ("timeout", "aborted"):
        on_failure(_string(30101))
        return True, True
    if outcome == "canceled":
        # Stop the backup worker, then delete the whole DupeKey group so NZBGet
        # can't promote a parked backup after the pick is deleted (#372 round 2);
        # fall back to canceling just the pick when there is no group.
        if cancel_event is not None:
            cancel_event.set()
        if dupe_key:
            nzbget_api.cancel_dupekey_group(dupe_key, settings_getter=settings_getter)
        else:
            nzbget_api.cancel_job(nzbid, settings_getter=settings_getter)
        on_failure(None)
        return True, False
    if outcome == "failed":
        on_failure(_string(30220))
        return True, False
    return False, False


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
        nzb_url = _usable_backup_link(backup, seen)
        if not nzb_url:
            continue
        seen.add(nzb_url)
        index += 1
        nzbid = _append_one_backup(nzb_url, backup, dupe_key, index, settings_getter)
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
        nzbid, error = nzbget_api.append_nzb(
            nzb_url,
            job_name,
            settings_getter=settings_getter,
            dupe_key=dupe_key,
            dupe_score=score,
            dupe_mode="SCORE",
        )
    except Exception as exc:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: NZBGet duplicate backup submit raised: {}".format(
                _redact_text(str(exc))
            ),
            xbmc.LOGWARNING,
        )
        return None
    if nzbid:
        xbmc.log(
            "NZB-DAV: Queued NZBGet duplicate backup '{}' (score {})".format(
                job_name, score
            ),
            xbmc.LOGINFO,
        )
        return nzbid
    xbmc.log(
        "NZB-DAV: NZBGet duplicate backup submit failed: {}".format(error),
        xbmc.LOGINFO,
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
        value = nzbget_api.config_option("HealthCheck", settings_getter=settings_getter)
    except Exception:  # pylint: disable=broad-except
        return
    if value != "pause":
        return
    xbmc.log(
        "NZB-DAV: NZBGet HealthCheck=Pause disables automatic duplicate failover; "
        "set it to Delete or None to enable it (#372).",
        xbmc.LOGWARNING,
    )
    with _HEALTHCHECK_LOCK:
        if _HEALTHCHECK_WARNED[0]:
            return
        _HEALTHCHECK_WARNED[0] = True
    _notify(_addon_name(), _string(30230), 6000)


def _snapshot_conn_getter(settings_getter):
    """A thread-safe getter over a main-thread snapshot of NZBGet connection
    settings (#372).

    Read the connection settings once on the calling (main/resolve) thread so the
    background backup worker performs NO off-thread Kodi ``getSetting`` (unsafe on
    CoreELEC/Kodi builds), and preserves a blank ``nzbget_username``/password
    verbatim (``dict.get`` returns the stored ``""`` rather than the auth default
    the addon getter substitutes).
    """
    url, user, password, category = nzbget_api._get_settings(settings_getter)
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
            nzbget_api.config_option("DupeCheck", settings_getter=settings_getter)
            == "no"
        )
    except Exception:  # pylint: disable=broad-except
        return False


_MAX_EXTRA_BACKUPS = 5


def _extra_backups_from_loader(loader, seen_links, limit=_MAX_EXTRA_BACKUPS):
    """Same-content / NZBHydra-deferred candidates from the fallback loader.

    #372 round 2 widening: beyond the picker's exact same-name rows, the fallback
    loader (an indexer search, already threaded for the nzbdav path) surfaces the
    same-content mirrors and NZBHydra duplicate uploads that were collapsed into a
    single picker row. Returns ``[{"link","title","score"}]`` deduped against
    ``seen_links``, scored DESCENDING from 0 so they sit BELOW every same-name
    backup (a last-resort failover, keyed under the pick's DupeKey). Bounded by
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
    score = 0
    for candidate in _load_extra_candidates(loader):
        if len(extras) >= cap:
            break
        link = _usable_backup_link(candidate, seen)
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
    if _dupe_check_disabled(getter):
        xbmc.log(
            "NZB-DAV: NZBGet DupeCheck=no -- skipping #372 duplicate "
            "backups (they would download in parallel).",
            xbmc.LOGINFO,
        )
        return True
    return False


def _submit_backup_fleet(getter, cancel_event, dupe_key, backups, loader, extras_limit):
    """Submit the same-name backups, then the loader-widened extras (#372).

    Widens with same-content / Hydra-deferred candidates (#372 r2) as
    lowest-priority backups keyed under the same (pick's) DupeKey. Bounds them
    by the standby cap's REMAINING slots so same-name backups + extras never
    exceed "Maximum standby fallback streams". Reads ONLY the snapshot
    ``getter`` -- this runs on the worker thread, which must never touch Kodi.
    """
    _submit_dupe_backups(backups, dupe_key, getter, cancel_event=cancel_event)
    if cancel_event.is_set():
        return
    remaining = (
        _MAX_EXTRA_BACKUPS
        if extras_limit is None
        else max(0, extras_limit - len(backups))
    )
    extras = _extra_backups_from_loader(
        loader, [b.get("link") for b in backups], limit=remaining
    )
    if extras:
        _submit_dupe_backups(extras, dupe_key, getter, cancel_event=cancel_event)


def _resweep_after_cancel(getter, dupe_key):
    """One extra canceled-group sweep after the backup worker drains (#372 r2).

    Covers the append that was already in flight when the user canceled and so
    landed after _handle_poll_failure's one-shot sweep. Best-effort like every
    other backup step: a failed sweep is swallowed, never raised off-thread.
    """
    try:
        nzbget_api.cancel_dupekey_group(dupe_key, settings_getter=getter)
    except Exception:  # pylint: disable=broad-except
        pass


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
    backups = list(dupe.get("backups") or [])
    if not dupe_key or not backups:
        return None
    try:
        getter = _snapshot_conn_getter(ctx.settings_getter)
    except Exception as exc:  # pylint: disable=broad-except
        # The snapshot reads Kodi/injected settings and runs AFTER the primary is
        # already accepted. Backups are pure insurance -- a settings-read failure
        # here must skip them, never propagate out and fail the primary's playback.
        xbmc.log(
            "NZB-DAV: NZBGet duplicate backup snapshot failed: {}".format(
                _redact_text(str(exc))
            ),
            xbmc.LOGWARNING,
        )
        return None
    cancel_event = ctx.cancel_event
    loader = dupe.get("loader")
    extras_limit = dupe.get("max_backups")

    def _worker():
        reached_submit = False
        try:
            if _dupe_worker_should_skip(getter, cancel_event):
                return
            _warn_if_healthcheck_pauses(getter)
            reached_submit = True
            _submit_backup_fleet(
                getter, cancel_event, dupe_key, backups, loader, extras_limit
            )
        except Exception as exc:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: NZBGet duplicate backup worker error: {}".format(
                    _redact_text(str(exc))
                ),
                xbmc.LOGWARNING,
            )
        finally:
            # If a cancel arrived while a backup's append was already in flight, that
            # backup can land in NZBGet AFTER _handle_poll_failure's one-shot
            # cancel_dupekey_group sweep -- and NZBGet would then promote the orphan
            # as the group's new active download. Re-sweep once the worker has
            # drained so nothing the user canceled survives (#372 r2 cancel-race).
            if reached_submit and cancel_event.is_set():
                _resweep_after_cancel(getter, dupe_key)

    try:
        thread = threading.Thread(
            target=_worker, name="nzbdav-nzbget-dupe-backups", daemon=True
        )
        thread.start()
    except Exception as exc:  # pylint: disable=broad-except
        # e.g. RuntimeError "can't start new thread" under thread exhaustion.
        # The backups are pure insurance -- never let them break the already-
        # queued pick's playback.
        xbmc.log(
            "NZB-DAV: NZBGet duplicate backup spawn failed: {}".format(
                _redact_text(str(exc))
            ),
            xbmc.LOGWARNING,
        )
        return None
    return thread


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
        dupe_key=dupe_key,
        cancel_event=ctx.cancel_event,
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
    the box's history even when the share is unreachable right now.
    """
    record_download(title, download_pubdate, download_size)
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


def _run_nzbget_backend(
    nzb_url,
    title,
    settings_getter,
    on_success,
    on_failure,
    download_pubdate=None,
    download_size=None,
    completed_job=None,
    dupe=None,
):
    """Shared NZBGet flow: reuse completed files, else submit -> poll -> SMB.

    Calls ``on_success(video_url)`` exactly once on success, or
    ``on_failure(message)`` exactly once on any failure (``message`` is ``None``
    for the silent cancel exit); this core guarantees a single terminal callback
    and owns the progress dialog. ``download_pubdate``/``download_size`` record
    the selected result's identity in the ledger for the picker's "DL" tag;
    ``completed_job`` is the corroborated history match played directly (nothing
    submitted) when its ``dest_dir`` still holds a playable video over SMB.
    ``dupe`` is the picker-computed NZBGet Smart-Duplicates submission (#372).
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
            ctx, nzb_url, title, completed_job, (download_pubdate, download_size)
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
        download_pubdate=params.get("_download_pubdate"),
        download_size=params.get("_download_size"),
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
        download_pubdate=resolve_params.get("_download_pubdate"),
        download_size=resolve_params.get("_download_size"),
        completed_job=resolve_params.get("_nzbget_completed_job"),
        dupe=resolve_params.get("_nzbget_dupe"),
    )
