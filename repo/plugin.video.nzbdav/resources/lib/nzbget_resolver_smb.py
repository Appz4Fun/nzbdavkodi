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

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".wmv", ".mov")


def nzbget_smb_target(smb_root, dest_dir, category="", completed_base=""):
    """Map NZBGet's server-local DestDir onto the SMB root.

    ``smb_root`` points at NZBGet's *completed* base dir, exposed over SMB --
    or, equally, a plain local/mounted absolute path standing in for it (an
    NFS or local mount onto the same completed-downloads directory).

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
# Bytes to read when probing that a selected video actually opens for
# reading. Listable is not readable: a file still settling after NZBGet's
# move — or a poisoned cached Kodi SMB session — can list and stat fine
# while open() fails with "Permission denied", which otherwise surfaces
# only after the player handoff as a silent playback failure. The probe
# goes through xbmcvfs, i.e. the exact same cached libsmbclient session
# VideoPlayer will use.
_SMB_READ_PROBE_BYTES = 8192


class _UnreadableSelection:  # pylint: disable=too-few-public-methods
    """Falsy sentinel: a video was selected but never became readable."""

    def __bool__(self):
        return False


# Distinct deadline result for "selected but never readable". Falsy, so a
# caller that only truth-tests keeps its ordinary miss behavior; the
# completed-reuse callers test ``is SMB_UNREADABLE`` to fail closed instead
# of falling through to a re-submit -- NZBGet would just dupe-delete a
# re-submission of the already-SUCCESS row, burying the restart-Kodi hint
# under an unrelated failure.
SMB_UNREADABLE = _UnreadableSelection()


def _smb_file_size(path):
    try:
        return xbmcvfs.Stat(path).st_size()
    except Exception:  # pylint: disable=broad-except
        return 0


def _is_video_name(name):
    """True when ``name`` ends in one of the playable video extensions."""
    lower = name.lower()
    # ⚡ Bolt: str.endswith natively takes a tuple (faster than any() generator loop)
    return lower.endswith(VIDEO_EXTENSIONS)


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


def _smb_video_scan_in_tree(folder, depth=_SMB_MAX_DEPTH):
    """Return ``(visible video rows, complete)`` for one SMB tree scan."""
    try:
        # Kodi Omega treats SMB directory exists probes as directories only
        # when their URL ends in a slash. Keep listdir and child URLs stable.
        if not xbmcvfs.exists(folder.rstrip("/") + "/"):
            return [], False
        dirs, files = xbmcvfs.listdir(folder)
    except Exception:  # pylint: disable=broad-except
        return [], False

    rows = []
    complete = True
    for name in sorted(files, key=str.casefold):
        if _core._is_video_name(name):
            path = "{}/{}".format(folder, name)
            rows.append((path, _core._smb_file_size(path)))
    if depth > 0:
        for subdir in sorted(dirs, key=str.casefold):
            child_rows, child_complete = _smb_video_scan_in_tree(
                "{}/{}".format(folder, subdir), depth - 1
            )
            rows.extend(child_rows)
            if not child_complete:
                complete = False
    return rows, complete


def _smb_video_candidates_in_tree(folder, depth=_SMB_MAX_DEPTH):
    """Return complete playable rows, or ``None`` for an incomplete tree."""
    rows, complete = _smb_video_scan_in_tree(folder, depth=depth)
    return rows if complete else None


def _smb_inventory(folder, requested_episode=None, depth=_SMB_MAX_DEPTH):
    """Build an episode-aware inventory for one reachable SMB folder tree."""
    rows, complete = _smb_video_scan_in_tree(folder, depth=depth)
    if not complete:
        return None
    return build_video_inventory(rows, requested=requested_episode)


def _partial_smb_selection_is_safe(inventory, requested_episode):
    """Whether visible partial rows prove a safe playable selection."""
    if inventory.selected_path is None:
        return False
    if requested_episode is None:
        return True
    for video_file in inventory.files:
        if video_file.path != inventory.selected_path:
            continue
        return not video_file.auxiliary and requested_episode in video_file.episode_tags
    return False


def _smb_video_is_readable(path):
    """True when ``path`` opens and yields data through Kodi's VFS.

    Exercises the same cached SMB session VideoPlayer will use, so a
    listable-but-unreadable selection is caught before the player handoff
    instead of failing playback with no user-visible explanation.
    """
    try:
        handle = xbmcvfs.File(path)
        try:
            return bool(handle.readBytes(_SMB_READ_PROBE_BYTES))
        finally:
            handle.close()
    except Exception:  # pylint: disable=broad-except
        return False


def _warn_unreadable_smb_video(path):
    """Log + toast that a visible video never became readable.

    A selection that stays listable-but-unreadable past the resolve budget
    is often the SMB stuck-session signature (Kodi's cached SMB session gets
    "Permission denied" while fresh sessions read the same file fine), so
    name that fix instead of leaving only the generic no-video message --
    ``path`` may equally be a local/mounted root, where the advice to
    restart Kodi is still harmless even though there is no SMB session to
    reset.
    """
    _core.xbmc.log(
        "NZB-DAV: video is listable but not readable through Kodi's VFS: {} "
        "-- if this persists, restart Kodi (for smb:// roots this resets "
        "its cached SMB session)".format(_core._redact_text(path)),
        _core.xbmc.LOGERROR,
    )
    try:
        _core._notify(_core._addon_name(), _core._string(30366), 7000)
    except Exception:  # pylint: disable=broad-except
        # A rejected toast must not break the resolve failure path.
        pass


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
    named wrong episodes and multiple untagged videos fail closed, while one
    untagged video retains the ordinary single-file fallback. A selection is
    returned only once it also *reads* through Kodi's VFS (see
    ``_smb_video_is_readable``); until then the same retry budget keeps
    absorbing files that are listed before they are readable. Returns None if
    no playable selection appears within the budget, or the falsy
    ``SMB_UNREADABLE`` sentinel when a selection stayed visible but never
    became readable -- in that case the inventory callback is deliberately
    NOT invoked, so an unplayable completion cannot enter the season-pack
    catalog and shadow future picks.
    """
    if monitor is None:
        monitor = _core.xbmc.Monitor()
    deadline = time.monotonic() + budget
    last_complete_inventory = None
    unreadable_path = None
    while True:
        rows, complete = _smb_video_scan_in_tree(smb_folder)
        inventory = build_video_inventory(rows, requested=requested_episode)
        if complete:
            last_complete_inventory = inventory
        selected = None
        if complete and inventory.selected_path:
            selected = inventory.selected_path
        elif not complete and _partial_smb_selection_is_safe(
            inventory, requested_episode
        ):
            selected = inventory.selected_path
        if selected is not None:
            if _core._smb_video_is_readable(selected):
                if complete:
                    _core._report_smb_inventory(on_inventory, inventory)
                return selected
            if selected != unreadable_path:
                _core.xbmc.log(
                    "NZB-DAV: selected video listed but not readable yet, "
                    "waiting: {}".format(_core._redact_text(selected)),
                    _core.xbmc.LOGINFO,
                )
            unreadable_path = selected
        elif (
            complete
            and unreadable_path is not None
            and all(
                video_file.path != unreadable_path for video_file in inventory.files
            )
        ):
            # Only a COMPLETE scan that no longer lists the unreadable file
            # proves it is really gone (cleanup): forget it, so the deadline
            # reports an ordinary miss and completed-reuse callers keep
            # their submit fallback. Everything else -- an incomplete scan
            # (share blip), or a complete scan where the file persists but
            # selection went ambiguous (e.g. a second untagged video
            # appeared) -- keeps the unreadable state, so the deadline still
            # fails closed and never reports the stale
            # last_complete_inventory into the season-pack catalog as if
            # the pack were playable.
            unreadable_path = None
        now = time.monotonic()
        if now >= deadline:
            if unreadable_path is not None:
                _core._warn_unreadable_smb_video(unreadable_path)
                return SMB_UNREADABLE
            if last_complete_inventory is not None:
                _core._report_smb_inventory(on_inventory, last_complete_inventory)
            return None
        if _core._drive_resolve_dialog(dialog, now, deadline, budget):
            return None
        if monitor.waitForAbort(interval):
            return None
