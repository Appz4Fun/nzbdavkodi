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


def poll_nzbget_job(
    nzbid, dialog, monitor, timeout, settings_getter=None, interval=_POLL_INTERVAL
):
    """Wait for an NZBGet job to reach a terminal state.

    Returns a dict with "outcome" in {"success","failed","canceled",
    "timeout","aborted"} and, on success, "dest_dir". Drives the progress
    dialog: download % from listgroups, then a post-processing message
    once the job leaves the active queue.

    ``timeout`` is enforced against the wall clock (``time.monotonic``) so a
    slow/stalled NZBGet box whose RPCs take far longer than ``interval``
    can't stretch the configured budget — see the sibling nzbdav poll loop.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dialog.iscanceled():
            return {"outcome": "canceled"}
        group = nzbget_api.group_status(nzbid, settings_getter=settings_getter)
        if group["present"]:
            _update_active_dialog(dialog, group)
        else:
            terminal = _poll_history_outcome(nzbid, dialog, settings_getter)
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


def _poll_history_outcome(nzbid, dialog, settings_getter):
    """Resolve a job that has left the queue via the NZBGet history.

    Returns the terminal outcome dict (success/failed) once the history row
    appears, or ``None`` during the brief queue->history hand-off gap (after
    showing post-processing) so the poll loop keeps waiting.
    """
    hist = nzbget_api.history_status(nzbid, settings_getter=settings_getter)
    if hist["present"]:
        if hist["success"]:
            return {"outcome": "success", "dest_dir": hist["dest_dir"]}
        return {"outcome": "failed", "status": hist["status"]}
    # Not in queue, not yet in history — brief gap during the hand-off; show
    # post-processing and keep waiting.
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


def _handle_poll_failure(outcome, nzbid, settings_getter, on_failure):
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
        # Same-name duplicate backups (#372): the picker-computed list of
        # same-release-name results, threaded from the resolve params.
        self.dupe_backups = None


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
    dupe_backups=None,
):
    """Read the per-submit NZBGet context once for the submit flow.

    Resolves the poll interval, the category (NZBGet nests categorized
    completed output under a per-category subfolder by default, so the SMB
    target must include that segment), and the global completed base used to
    map a history DestDir onto the SMB root regardless of category/custom
    layout (None when unavailable -> nzbget_smb_target falls back). Attaches
    the flow's getter + callbacks so the submit helpers can stay low-arity, plus
    the picker-computed same-name duplicate backups (#372).

    Binds a ``None`` getter (the real Kodi handle-based ``resolve`` path passes
    no getter) to the addon-backed two-arg getter up front, so the background
    backup-submit thread carries a valid getter created on this (main) thread
    rather than a ``None`` it would have to resolve from a worker thread.
    """
    settings_getter = _bind_getter(settings_getter)
    interval = _read_poll_interval(settings_getter)
    _u, _user, _pw, category = nzbget_api._get_settings(settings_getter=settings_getter)
    completed_base = nzbget_api.completed_base_dir(settings_getter=settings_getter)
    ctx = _SubmitCtx(smb_root, category, completed_base, dialog, interval, timeout)
    ctx.settings_getter = settings_getter
    ctx.on_success = on_success
    ctx.on_failure = on_failure
    ctx.dupe_backups = dupe_backups
    return ctx


def _submit_name_backups(backups, name, settings_getter):
    """Submit same-name duplicate backups to NZBGet (#372).

    ``backups`` is the picker-computed list of ``{"link": ...}`` results that
    share the pick's release NAME. Each is appended under the pick's ``name`` so
    NZBGet's name-based duplicate check groups it with the already-queued pick:
    the pick (submitted first, so the incumbent) keeps downloading and each
    backup is parked in history as a duplicate, and if the pick finishes
    unrepairable (par2/unpack/health) NZBGet fails over to one. Best-effort: the
    pick is already queued and playing out normally, so a bad/duplicate URL or a
    failed fetch/append for one backup never aborts the rest or the playback.
    Returns the list of submitted NZBIDs (for logging/tests).
    """
    submitted = []
    seen = set()
    for backup in backups or []:
        if not isinstance(backup, dict):
            continue
        nzb_url = backup.get("link")
        if not nzb_url or nzb_url in seen:
            continue
        seen.add(nzb_url)
        try:
            nzbid, error = nzbget_api.append_nzb(
                nzb_url, name, settings_getter=settings_getter
            )
        except Exception as exc:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: NZBGet duplicate backup submit raised: {}".format(
                    _redact_text(str(exc))
                ),
                xbmc.LOGWARNING,
            )
            continue
        if nzbid:
            submitted.append(nzbid)
            xbmc.log(
                "NZB-DAV: Queued NZBGet duplicate backup for '{}'".format(name),
                xbmc.LOGINFO,
            )
        else:
            xbmc.log(
                "NZB-DAV: NZBGet duplicate backup submit failed: {}".format(error),
                xbmc.LOGINFO,
            )
    return submitted


def _spawn_name_backups(ctx, name):
    """Fire-and-forget the same-name duplicate backups in a daemon thread.

    Called only after the pick is confirmed queued, so the pick is the incumbent
    NZBGet keeps active. Each backup fetch is an indexer HTTP round-trip, so it
    runs off the resolve thread to keep it from delaying the pick's poll/progress
    ("it won't affect playback"); the daemon flag keeps it from blocking Kodi
    shutdown. All errors are swallowed -- the backups are pure insurance and must
    never break the pick's playback.

    Timing (best-effort): the backups only need to reach NZBGet before the pick
    finishes to be available as failover, which for a normal-length download
    (the target case -- a full release whose par2 repair fails) they easily do,
    landing within seconds while the pick downloads for minutes. A pick that
    completes in under the few seconds it takes to fetch+submit the backups (a
    tiny or fully-cached release) can beat them: if it SUCCEEDED the late backups
    are dupe-suppressed against its history row, which is harmless (a successful
    pick needs no failover); if it FAILED fast the backups still submit (a failed
    row does not suppress) and download, just untracked in this attempt (the
    round-2 poll limitation). See TODO.md.
    """
    backups = list(ctx.dupe_backups or [])
    if not backups:
        return None
    getter = ctx.settings_getter

    def _worker():
        try:
            _submit_name_backups(backups, name, getter)
        except Exception as exc:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: NZBGet duplicate backup worker error: {}".format(
                    _redact_text(str(exc))
                ),
                xbmc.LOGWARNING,
            )

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

    The pick is submitted exactly as before (#372 changes nothing here). Once
    it is confirmed queued -- and therefore the incumbent NZBGet keeps active --
    the picker-computed same-name backups are submitted so NZBGet can fail over
    to one if the pick turns out unrepairable.
    """
    getter = ctx.settings_getter
    nzbid, error = nzbget_api.append_nzb(nzb_url, title, settings_getter=getter)
    if not nzbid:
        # Surface the specific (already-redacted) NZBGet message — auth vs dupe
        # vs "append returned 0" — per the spec error table, else the generic.
        ctx.on_failure(error or _string(30222))
        return False

    # Pick is queued (the incumbent): submit its same-name duplicate backups so
    # NZBGet can fail over to one if the pick is unrepairable (#372). Submitted
    # AFTER the pick so a same-name backup can't become the incumbent and get
    # the pick dupe-deleted, and off-thread so it never delays the poll below.
    _spawn_name_backups(ctx, title)

    result = poll_nzbget_job(
        nzbid,
        ctx.dialog,
        xbmc.Monitor(),
        ctx.timeout,
        settings_getter=getter,
        interval=ctx.interval,
    )
    handled, leave_job = _handle_poll_failure(
        result["outcome"], nzbid, getter, ctx.on_failure
    )
    if handled:
        return leave_job

    # Download completed (now SUCCESS history): record the post-date before the
    # SMB mapping, which can still fail without un-completing it. Fail-soft.
    record_download(title, download_pubdate, download_size)

    video_url = _resolve_completed_smb(
        result["dest_dir"],
        ctx.smb_root,
        ctx.category,
        ctx.completed_base,
        ctx.dialog,
        ctx.interval,
    )
    if not video_url:
        ctx.on_failure(_string(30223))
        return leave_job

    ctx.on_success(video_url)
    return leave_job


def _run_nzbget_backend(
    nzb_url,
    title,
    settings_getter,
    on_success,
    on_failure,
    download_pubdate=None,
    download_size=None,
    completed_job=None,
    dupe_backups=None,
):
    """Shared NZBGet flow: reuse completed files, else submit -> poll -> SMB.

    Calls ``on_success(video_url)`` exactly once on success, or
    ``on_failure(message)`` exactly once on any failure (``message`` is ``None``
    for the silent cancel exit); this core guarantees a single terminal callback
    and owns the progress dialog. ``download_pubdate``/``download_size`` record
    the selected result's identity in the ledger for the picker's "DL" tag;
    ``completed_job`` is the corroborated history match played directly (nothing
    submitted) when its ``dest_dir`` still holds a playable video over SMB.
    ``dupe_backups`` is the picker-computed list of same-name results submitted
    as NZBGet duplicate backups after the pick is queued (#372).
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
            dupe_backups=dupe_backups,
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
        dupe_backups=params.get("_nzbget_dupe_backups"),
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
        dupe_backups=resolve_params.get("_nzbget_dupe_backups"),
    )
