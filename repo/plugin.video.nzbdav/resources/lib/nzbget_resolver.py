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
        # Same-release duplicate fleet (#372): a seeded candidate list and/or a
        # lazy loader (an indexer search) threaded from the resolve params.
        self.fallback_candidates = None
        self.fallback_loader = None


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
    fallback_candidates=None,
    fallback_loader=None,
):
    """Read the per-submit NZBGet context once for the submit flow.

    Resolves the poll interval, the category (NZBGet nests categorized
    completed output under a per-category subfolder by default, so the SMB
    target must include that segment), and the global completed base used to
    map a history DestDir onto the SMB root regardless of category/custom
    layout (None when unavailable -> nzbget_smb_target falls back). Attaches
    the flow's getter + callbacks so the submit helpers can stay low-arity, plus
    the same-release duplicate-fleet inputs (#372).

    Binds a ``None`` getter (the real Kodi handle-based ``resolve`` path passes
    no getter) to the addon-backed two-arg getter up front, so every submit
    helper -- including the duplicate-fleet checks that call ``getter(key,
    default)`` directly -- receives a callable instead of ``None``.
    """
    settings_getter = _bind_getter(settings_getter)
    interval = _read_poll_interval(settings_getter)
    _u, _user, _pw, category = nzbget_api._get_settings(settings_getter=settings_getter)
    completed_base = nzbget_api.completed_base_dir(settings_getter=settings_getter)
    ctx = _SubmitCtx(smb_root, category, completed_base, dialog, interval, timeout)
    ctx.settings_getter = settings_getter
    ctx.on_success = on_success
    ctx.on_failure = on_failure
    ctx.fallback_candidates = fallback_candidates
    ctx.fallback_loader = fallback_loader
    return ctx


# Primary DupeScore for a fleet submit (#372). The user's selected release must
# stay the single ACTIVE download NZBGet keeps in the queue -- the poll tracks
# only its NZBID -- so it is submitted at this ceiling and every same-release
# backup gets a strictly-lower (negative), descending score. If a backup tied or
# beat it, NZBGet would park the primary in history as a dsDupe "duplicate" and
# the poll would read that as a failed download.
#
# The ceiling is 0 (not a large positive) on purpose: it matches the pre-#372
# single-submit score, so the primary's dupe-vs-history behavior is unchanged --
# NZBGet still dupe-deletes a re-submit of an already-SUCCESS release (also
# score 0; suppressed because new <= existing) on a reuse-miss instead of
# re-downloading gigabytes already on disk. Backups at negative scores are
# likewise suppressed against such a SUCCESS row, so no backup is re-fetched for
# an already-good release either.
_PRIMARY_DUPE_SCORE = 0


def _release_dupe_key(title):
    """Build the shared, stable NZBGet DupeKey for a release's whole fleet.

    NZBGet groups duplicates by an identical non-empty DupeKey (case-insensitive;
    the NZB name is ignored once both items carry a key). Derive it once from the
    primary title -- lowercased, whitespace-collapsed -- and namespace it so an
    unrelated same-named job from another tool on the box can't join the set. An
    empty/whitespace title yields ``""`` so the primary stays an ungrouped single
    submit (no fleet).
    """
    text = " ".join(str(title or "").split()).lower()
    return "nzbdav:" + text if text else ""


def _dupe_fleet_enabled(settings_getter):
    """Whether to submit the same-release duplicate fleet (#372).

    Reuses the ``fallback_streams_enabled`` toggle (default on): the NZBGet
    duplicate fleet is the NZBGet-backend analogue of the nzbdav fallback
    streams -- same-release backups -- so one switch governs both.
    """
    raw = settings_getter("fallback_streams_enabled", "true")
    return str(raw or "").strip().lower() != "false"


def _dupe_fleet_max(settings_getter):
    """Max number of duplicate backups to submit, from ``fallback_streams_max``."""
    try:
        return max(0, int(settings_getter("fallback_streams_max", "5") or 5))
    except (TypeError, ValueError):
        return 5


def _submit_dupe_fleet(
    candidates, dupe_key, primary_nzb_url, settings_getter, max_count
):
    """Submit same-release siblings to NZBGet as ordered duplicate backups.

    Each member carries the shared ``dupe_key`` and a strictly-descending
    DupeScore below ``_PRIMARY_DUPE_SCORE`` (best candidate first, since the
    candidate list is already ranked best-first), so NZBGet keeps them parked in
    history as backups and, on the active release failing par2/unpack/health,
    auto-redownloads the highest-scored one. Best-effort: a bad candidate or a
    failed fetch/append for one sibling never aborts the rest. Skips non-dicts,
    entries missing a link/title, and the primary's own URL. Returns the list of
    submitted NZBIDs (for logging/tests).
    """
    submitted = []
    if not dupe_key:
        return submitted
    # Import via ``fallback_streams`` (which re-exports it) rather than
    # ``fallback_streams_attach`` directly: the two modules have a mutual import
    # that only resolves cleanly when ``fallback_streams`` is imported first.
    from resources.lib.fallback_streams import build_fallback_job_name

    attempted = 0
    for candidate in candidates or []:
        if attempted >= max_count:
            break
        if not isinstance(candidate, dict):
            continue
        nzb_url = candidate.get("link")
        title = candidate.get("title")
        if not nzb_url or not title:
            continue
        if primary_nzb_url and nzb_url == primary_nzb_url:
            continue
        attempted += 1
        job_name = build_fallback_job_name(title, nzb_url, attempted)
        score = _PRIMARY_DUPE_SCORE - attempted
        try:
            nzbid, error = nzbget_api.append_nzb(
                nzb_url,
                job_name,
                settings_getter=settings_getter,
                dupe_key=dupe_key,
                dupe_score=score,
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
                "NZB-DAV: Queued NZBGet duplicate backup '{}' (score {})".format(
                    job_name, score
                ),
                xbmc.LOGINFO,
            )
        else:
            xbmc.log(
                "NZB-DAV: NZBGet duplicate backup submit failed: {}".format(error),
                xbmc.LOGINFO,
            )
    return submitted


def _spawn_dupe_fleet(ctx, primary_nzb_url, dupe_key):
    """Fire-and-forget the same-release duplicate fleet in a daemon thread.

    Loading the fleet is an indexer search (the ``_fallback_candidate_loader``),
    so it runs off the resolve thread to avoid delaying the primary's poll and
    progress dialog; the daemon flag keeps it from blocking Kodi shutdown. The
    backups only need to reach NZBGet's history before the primary finishes, so
    they land well within the download window. All errors are swallowed -- the
    fleet is pure insurance and must never break the primary playback.
    """
    if not dupe_key:
        return None
    getter = ctx.settings_getter
    max_count = _dupe_fleet_max(getter)
    if max_count <= 0:
        return None
    seeded = list(ctx.fallback_candidates or [])
    loader = ctx.fallback_loader

    def _worker():
        try:
            pool = seeded
            if not pool and loader is not None:
                pool = loader() or []
            _submit_dupe_fleet(pool, dupe_key, primary_nzb_url, getter, max_count)
        except Exception as exc:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: NZBGet duplicate fleet worker error: {}".format(
                    _redact_text(str(exc))
                ),
                xbmc.LOGWARNING,
            )

    try:
        thread = threading.Thread(
            target=_worker, name="nzbdav-nzbget-dupe-fleet", daemon=True
        )
        thread.start()
    except Exception as exc:  # pylint: disable=broad-except
        # e.g. RuntimeError "can't start new thread" under thread exhaustion.
        # The fleet is pure insurance -- never let it break the already-queued
        # primary playback.
        xbmc.log(
            "NZB-DAV: NZBGet duplicate fleet spawn failed: {}".format(
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

    When the duplicate fleet is enabled and same-release candidates are
    available (#372), the primary is submitted with the shared DupeKey at the
    top DupeScore (so it stays the active download this poll tracks) and the
    backup fleet is spawned; otherwise the primary is a plain single submit.
    """
    getter = ctx.settings_getter
    # Only the DupeKey gates the fleet; the primary always submits at
    # _PRIMARY_DUPE_SCORE (0), which is both the ceiling above every negative
    # backup score and the pre-#372 single-submit score, so a bare (no-fleet)
    # submit stays byte-for-byte unchanged.
    dupe_key = ""
    if _dupe_fleet_enabled(getter) and (ctx.fallback_candidates or ctx.fallback_loader):
        dupe_key = _release_dupe_key(title)
    nzbid, error = nzbget_api.append_nzb(
        nzb_url,
        title,
        settings_getter=getter,
        dupe_key=dupe_key,
        dupe_score=_PRIMARY_DUPE_SCORE,
    )
    if not nzbid:
        # Surface the specific (already-redacted) NZBGet message — auth vs dupe
        # vs "append returned 0" — per the spec error table, else the generic.
        ctx.on_failure(error or _string(30222))
        return False

    # Primary is queued and highest-scored: spawn the same-release backups so
    # NZBGet can fail over to one if the primary is unrepairable (#372). Spawned
    # after the primary is confirmed queued so a failed primary never triggers a
    # backup fleet, and off-thread so it doesn't delay the poll below.
    if dupe_key:
        _spawn_dupe_fleet(ctx, nzb_url, dupe_key)

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
    fallback_candidates=None,
    fallback_loader=None,
):
    """Shared NZBGet flow: reuse completed files, else submit -> poll -> SMB.

    Calls ``on_success(video_url)`` exactly once on success, or
    ``on_failure(message)`` exactly once on any failure (``message`` is ``None``
    for the silent cancel exit); this core guarantees a single terminal callback
    and owns the progress dialog. ``download_pubdate``/``download_size`` record
    the selected result's identity in the ledger for the picker's "DL" tag;
    ``completed_job`` is the corroborated history match played directly (nothing
    submitted) when its ``dest_dir`` still holds a playable video over SMB.
    ``fallback_candidates``/``fallback_loader`` seed the same-release duplicate
    fleet (#372).
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
            fallback_candidates=fallback_candidates,
            fallback_loader=fallback_loader,
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
        fallback_candidates=params.get("_fallback_candidates"),
        fallback_loader=params.get("_fallback_candidate_loader"),
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
        fallback_candidates=resolve_params.get("_fallback_candidates"),
        fallback_loader=resolve_params.get("_fallback_candidate_loader"),
    )
