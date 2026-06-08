# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZBGet backend resolver: submit to NZBGet, wait, play from SMB.

Reached only when ``nzbget_enabled`` is true; ``resolver.resolve`` delegates
here. Honors the same ``setResolvedUrl``-on-failure contract as the nzbdav
path: exactly one resolution per exit, failures resolve False.
"""

import time
from urllib.parse import unquote

import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib import nzbget_api
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

    cb = (completed_base or "").replace("\\", "/").rstrip("/")
    if cb and normalized.casefold() != cb.casefold():
        if (normalized + "/").casefold().startswith(cb.casefold() + "/"):
            rel = normalized[len(cb) :].strip("/")
            # Guard against a doubled tail when smb_root was pointed at a
            # subfolder of the completed base (e.g. root .../movies + rel
            # movies/Release): drop rel's leading segment if base already ends
            # with it.
            rel_head = rel.split("/", 1)[0] if rel else ""
            if rel_head and base.casefold().endswith("/" + rel_head.casefold()):
                rel = rel[len(rel_head) :].strip("/")
            if rel:
                return "{}/{}".format(base, rel)

    segments = [seg for seg in normalized.split("/") if seg]
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
    best, best_size = None, -1
    for name in files:
        lower = name.lower()
        if not any(lower.endswith(ext) for ext in VIDEO_EXTENSIONS):
            continue
        path = "{}/{}".format(folder, name)
        size = _smb_file_size(path)
        if size > best_size:
            best, best_size = path, size
    if depth > 0:
        for sub in dirs:
            url, size = _largest_video_in_tree("{}/{}".format(folder, sub), depth - 1)
            if url is not None and size > best_size:
                best, best_size = url, size
    return best, best_size


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
        if dialog is not None:
            if dialog.iscanceled():
                return None
            # Drive the progress bar over the resolve window so the user sees
            # a "finishing up" indicator instead of being dropped back to the
            # home screen while the file settles onto the share.
            elapsed = budget - max(0.0, deadline - now)
            percent = int(min(100.0, elapsed * 100.0 / budget)) if budget else 100
            dialog.update(percent, _string(30219))
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
            status = (group["status"] or "").upper()
            if status in _DOWNLOAD_STATUSES:
                dialog.update(group["percent"], _fmt(30105, group["percent"]))
            else:
                # Every non-download in-queue stage (LOADING_PARS, REPAIRING,
                # UNPACKING, MOVING, EXECUTING_SCRIPT, QUEUED/PAUSED, ...) is
                # past the download, so show "Post-processing..." instead of a
                # frozen "Downloading... 100%".
                dialog.update(group["percent"], _string(30219))
        else:
            hist = nzbget_api.history_status(nzbid, settings_getter=settings_getter)
            if hist["present"]:
                if hist["success"]:
                    return {"outcome": "success", "dest_dir": hist["dest_dir"]}
                return {"outcome": "failed", "status": hist["status"]}
            # Not in queue, not yet in history — brief gap during the
            # hand-off; show post-processing and keep waiting.
            dialog.update(100, _string(30219))
        if monitor.waitForAbort(interval):
            return {"outcome": "aborted"}
    return {"outcome": "timeout"}


_DEFAULT_TIMEOUT = 3600
_TIMEOUT_MIN = 60
_TIMEOUT_MAX = 86400

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


def _run_nzbget_backend(nzb_url, title, settings_getter, on_success, on_failure):
    """Shared NZBGet flow: submit -> poll -> resolve over SMB.

    Calls ``on_success(video_url)`` exactly once on success, or
    ``on_failure(message)`` exactly once on any failure (``message`` is
    ``None`` for the silent cancel exit). Delivery — ``setResolvedUrl`` for
    the handle path vs ``xbmc.Player().play`` for the handle-less
    ``resolve_and_play`` path — is the caller's concern; this core
    guarantees a single terminal callback and owns the progress dialog.
    """
    dialog = None
    nzbid = None
    leave_job = False
    try:
        url, smb_root, timeout = _read_settings(settings_getter)
        if not url or not smb_root or not nzb_url:
            on_failure(_string(30221))
            return

        interval = _read_poll_interval(settings_getter)
        # NZBGet nests categorized completed output under a per-category
        # subfolder (default AppendCategoryDir=yes); the SMB target must
        # include that segment or it 404s. _get_settings is the canonical
        # category reader (same one append_nzb uses).
        _u, _user, _pw, category = nzbget_api._get_settings(
            settings_getter=settings_getter
        )
        # NZBGet's global completed base, used to map a history DestDir exactly
        # onto the SMB root regardless of category/custom-DestDir layout. None
        # when unavailable -> nzbget_smb_target falls back to its heuristic.
        completed_base = nzbget_api.completed_base_dir(settings_getter=settings_getter)

        dialog = xbmcgui.DialogProgress()
        dialog.create(_addon_name(), _string(30218))

        # Always submit the NZB the user actually selected. We deliberately do
        # NOT reuse an existing NZBGet job (completed history *or* in-queue) by
        # name: unlike the nzbdav completed-cache, a name match has no
        # size/pubdate/indexer corroboration (and that selected-result metadata
        # isn't available on the handle-based entry path), so a same-named
        # repost or a different indexer's result could attach to / play the
        # wrong job instead of the one the user chose. NZBGet's own dupe
        # handling deals with re-submitting a still-in-flight same-name job on a
        # quick retry.
        nzbid, error = nzbget_api.append_nzb(
            nzb_url, title, settings_getter=settings_getter
        )
        if not nzbid:
            # Surface the specific (already-redacted) NZBGet message — auth vs
            # dupe vs "append returned 0" — per the spec error table, falling
            # back to the generic string.
            on_failure(error or _string(30222))
            return

        result = poll_nzbget_job(
            nzbid,
            dialog,
            xbmc.Monitor(),
            timeout,
            settings_getter=settings_getter,
            interval=interval,
        )
        outcome = result["outcome"]

        if outcome in ("timeout", "aborted"):
            leave_job = True
            on_failure(_string(30101))
            return
        if outcome == "canceled":
            nzbget_api.cancel_job(nzbid, settings_getter=settings_getter)
            on_failure(None)
            return
        if outcome == "failed":
            on_failure(_string(30220))
            return

        smb_folder = nzbget_smb_target(
            smb_root, result["dest_dir"], category, completed_base
        )
        if not smb_folder:
            on_failure(_string(30223))
            return
        video_url = resolve_smb_video(smb_folder, dialog=dialog, interval=interval)
        if not video_url:
            on_failure(_string(30223))
            return

        on_success(video_url)
    except Exception as exc:  # pylint: disable=broad-except
        # str(exc) can echo the indexer nzb_url (apikey=...) or the
        # smb://user:pass@host root — redact both before logging.
        xbmc.log(
            "NZB-DAV: NZBGet resolve error: {}".format(_redact_text(str(exc))),
            xbmc.LOGERROR,
        )
        on_failure(None)
    finally:
        if dialog is not None:
            try:
                dialog.close()
            except Exception:  # pylint: disable=broad-except
                pass
        # leave_job documents the timeout policy: on timeout/abort we
        # deliberately do NOT cancel_job so the download can finish for a
        # later retry.
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


def resolve_and_play_nzbget(handle, params, settings_getter=None, resume_seconds=0.0):
    """NZBGet entry for the handle-based ``resolve`` path (``/play``).

    Delivers the finished file via ``setResolvedUrl`` — exactly one
    resolution per exit (success True, every failure False). ``resume_seconds``
    carries the scrubbed bookmark's resume position onto the ListItem.
    """
    nzb_url = unquote(params.get("nzburl", ""))
    title = unquote(params.get("title", "")) or "submission"

    def on_success(video_url):
        listitem = xbmcgui.ListItem(path=video_url)
        _apply_resume(listitem, resume_seconds)
        xbmcplugin.setResolvedUrl(handle, True, listitem)

    def on_failure(message):
        _resolve_failure(handle, message)

    _run_nzbget_backend(nzb_url, title, settings_getter, on_success, on_failure)


def play_nzbget(nzb_url, title, params=None, settings_getter=None, resume_seconds=0.0):
    """NZBGet entry for the handle-less ``resolve_and_play`` path.

    ``resolve_and_play`` (TMDBHelper ``/resolve``, the in-addon search
    picker, and script-play) has no plugin handle, so the finished SMB file
    is started with ``xbmc.Player().play`` and failures only notify —
    mirroring the nzbdav ``resolve_and_play`` "no setResolvedUrl" contract.
    ``resume_seconds`` carries the scrubbed bookmark's resume position.
    """
    if settings_getter is None:
        settings_getter = (params or {}).get("_settings_getter")

    def on_success(video_url):
        listitem = xbmcgui.ListItem(path=video_url)
        _apply_resume(listitem, resume_seconds)
        xbmc.Player().play(video_url, listitem)

    def on_failure(message):
        if message:
            _notify(_addon_name(), message, 5000)

    _run_nzbget_backend(nzb_url, title, settings_getter, on_success, on_failure)
