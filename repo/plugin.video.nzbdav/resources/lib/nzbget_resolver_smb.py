# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""NZBGet completed-download SMB mapping + video discovery for the resolver.

Cohesive helper group split out of ``nzbget_resolver`` to keep every module
under Codacy's 500-NLOC file gate (same split idiom as
``resolver_fallback_jobs`` / ``nzbget_resolver_dupes``). Names that live in
(or are patched via) ``nzbget_resolver`` -- including the sibling helpers,
which the suite reaches as ``resources.lib.nzbget_resolver.<name>`` -- are
resolved at call time through ``import resources.lib.nzbget_resolver as
_core`` so those ``@patch`` decorators keep intercepting, with no top-level
import cycle. ``xbmcvfs``/``time`` are imported directly: the suite patches
them on the shared module objects (``patch.object(xbmcvfs, ...)``), which any
import path sees. Every moved name is re-exported from ``nzbget_resolver``.
"""

import time

import xbmcvfs

import resources.lib.nzbget_resolver as _core  # noqa: F401  pylint: disable=unused-import
from resources.lib.episode_inventory import build_video_inventory

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

    exact = _core._smb_exact_mapping(normalized, base, completed_base)
    if exact is not None:
        return exact

    return _core._smb_fallback_mapping(normalized, base, category)


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
        if not _core._is_video_name(name):
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
        if not _core._is_video_name(name):
            continue
        path = "{}/{}".format(folder, name)
        size = _core._smb_file_size(path)
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
    inventory = _core._smb_inventory(folder, depth=depth)
    if inventory is None or inventory.selected_path is None:
        return None, -1
    return inventory.selected_path, inventory.selected_size


def _smb_video_candidates_in_tree(folder, depth=_SMB_MAX_DEPTH):
    """Return playable ``(path, size)`` rows below a reachable SMB folder.

    ``None`` means the tree could not be confirmed reachable/listable and the
    caller should retry. An empty list means the folder was positively
    reachable but currently contained no playable files.
    """
    try:
        # Kodi Omega treats SMB directory exists probes as directories only
        # when their URL ends in a slash. Keep listdir and child URLs stable.
        if not xbmcvfs.exists(folder.rstrip("/") + "/"):
            return None
        dirs, files = xbmcvfs.listdir(folder)
    except Exception:  # pylint: disable=broad-except
        return None

    rows = []
    for name in sorted(files, key=str.casefold):
        if _core._is_video_name(name):
            path = "{}/{}".format(folder, name)
            rows.append((path, _core._smb_file_size(path)))
    if depth > 0:
        for subdir in sorted(dirs, key=str.casefold):
            child_rows = _core._smb_video_candidates_in_tree(
                "{}/{}".format(folder, subdir), depth - 1
            )
            if child_rows is None:
                return None
            rows.extend(child_rows)
    return rows


def _smb_inventory(folder, requested_episode=None, depth=_SMB_MAX_DEPTH):
    """Build an episode-aware inventory for one reachable SMB folder tree."""
    rows = _core._smb_video_candidates_in_tree(folder, depth=depth)
    if rows is None:
        return None
    return build_video_inventory(rows, requested=requested_episode)


def _report_smb_inventory(callback, inventory):
    """Invoke an optional inventory callback without breaking playback."""
    if callback is None:
        return
    try:
        callback(inventory)
    except Exception as error:  # pylint: disable=broad-except
        _core.xbmc.log(
            "NZB-DAV: SMB inventory callback failed: {}".format(error),
            _core.xbmc.LOGDEBUG,
        )


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
    dialog.update(percent, _core._string(30219))
    return False


def resolve_smb_video(
    smb_folder,
    monitor=None,
    dialog=None,
    interval=_SMB_LIST_RETRY_INTERVAL,
    budget=_SMB_RESOLVE_BUDGET,
    requested_episode=None,
    on_inventory=None,
):
    """List an SMB folder and return its requested playable video, or None.

    Searches the folder tree (top level plus nested subdirectories, see
    ``_largest_video_in_tree``) and keeps retrying until a video appears or
    the wall-clock ``budget`` (seconds, ``time.monotonic``) elapses — long
    enough to absorb the lag between NZBGet reporting SUCCESS and the moved
    files becoming visible over SMB. Sleeps ``interval`` seconds between
    attempts via ``Monitor.waitForAbort`` so it stays cancelable and honors
    Kodi shutdown. When a ``dialog`` (DialogProgress) is supplied, it shows a
    progress bar over the wait and its Cancel button aborts the search.
    With episode context, an exact tagged episode beats larger pack members;
    named wrong episodes fail closed, while untagged videos retain the legacy
    largest-file fallback. Returns None if no playable selection appears
    within the budget.
    """
    if monitor is None:
        monitor = _core.xbmc.Monitor()
    deadline = time.monotonic() + budget
    last_complete_inventory = None
    while True:
        inventory = _core._smb_inventory(
            smb_folder, requested_episode=requested_episode
        )
        if inventory is not None:
            last_complete_inventory = inventory
        if inventory is not None and inventory.files:
            _core._report_smb_inventory(on_inventory, inventory)
            return inventory.selected_path
        now = time.monotonic()
        if now >= deadline:
            if last_complete_inventory is not None:
                _core._report_smb_inventory(on_inventory, last_complete_inventory)
            return None
        if _core._drive_resolve_dialog(dialog, now, deadline, budget):
            return None
        if monitor.waitForAbort(interval):
            return None
