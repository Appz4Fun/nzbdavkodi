# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZBGet backend resolver: submit to NZBGet, wait, play from SMB.

Reached only when ``nzbget_enabled`` is true; ``resolver.resolve`` delegates
here. Honors the same ``setResolvedUrl``-on-failure contract as the nzbdav
path: exactly one resolution per exit, failures resolve False.
"""

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


def nzbget_smb_target(smb_root, dest_dir):
    """Map NZBGet's server-local DestDir onto the SMB root.

    The SMB root *is* NZBGet's completed dir, so we take DestDir's final
    path component (the per-release folder) and append it. Returns the
    smb:// folder URL, or None if dest_dir is empty.
    """
    if not dest_dir:
        return None
    release_folder = dest_dir.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not release_folder:
        return None
    return "{}/{}".format(smb_root.rstrip("/"), release_folder)


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


_SMB_LIST_RETRIES = 5
_SMB_LIST_RETRY_INTERVAL = 1.0


def _smb_file_size(path):
    try:
        return xbmcvfs.Stat(path).st_size()
    except Exception:  # pylint: disable=broad-except
        return 0


def resolve_smb_video(smb_folder, monitor=None):
    """List an SMB folder and return the largest video file URL, or None.

    Retries a few times (via Monitor.waitForAbort) to absorb the lag
    between NZBGet reporting SUCCESS and the files becoming visible over
    SMB. Returns None if no video file appears.
    """
    if monitor is None:
        monitor = xbmc.Monitor()
    for attempt in range(_SMB_LIST_RETRIES):
        try:
            _dirs, files = xbmcvfs.listdir(smb_folder)
        except Exception:  # pylint: disable=broad-except
            files = []
        chosen = pick_largest_video(
            files,
            lambda name: _smb_file_size("{}/{}".format(smb_folder, name)),
        )
        if chosen is not None:
            return "{}/{}".format(smb_folder, chosen)
        if attempt < _SMB_LIST_RETRIES - 1:
            if monitor.waitForAbort(_SMB_LIST_RETRY_INTERVAL):
                return None
    return None


_POLL_INTERVAL = 2.0


def poll_nzbget_job(nzbid, dialog, monitor, timeout, settings_getter=None):
    """Wait for an NZBGet job to reach a terminal state.

    Returns a dict with "outcome" in {"success","failed","canceled",
    "timeout","aborted"} and, on success, "dest_dir". Drives the progress
    dialog: download % from listgroups, then a post-processing message
    once the job leaves the active queue.
    """
    elapsed = 0.0
    while elapsed <= timeout:
        if dialog.iscanceled():
            return {"outcome": "canceled"}
        group = nzbget_api.group_status(nzbid, settings_getter=settings_getter)
        if group["present"]:
            status = group["status"] or ""
            if status.startswith("PP") or "POST" in status:
                dialog.update(group["percent"], _string(30219))
            else:
                dialog.update(group["percent"], _fmt(30105, group["percent"]))
        else:
            hist = nzbget_api.history_status(nzbid, settings_getter=settings_getter)
            if hist["present"]:
                if hist["success"]:
                    return {"outcome": "success", "dest_dir": hist["dest_dir"]}
                return {"outcome": "failed", "status": hist["status"]}
            # Not in queue, not yet in history — brief gap during the
            # hand-off; show post-processing and keep waiting.
            dialog.update(100, _string(30219))
        if monitor.waitForAbort(_POLL_INTERVAL):
            return {"outcome": "aborted"}
        elapsed += _POLL_INTERVAL
    return {"outcome": "timeout"}


_DEFAULT_TIMEOUT = 3600
_TIMEOUT_MIN = 60
_TIMEOUT_MAX = 86400


def _read_settings(settings_getter):
    if settings_getter is None:
        import xbmcaddon

        addon = xbmcaddon.Addon("plugin.video.nzbdav")

        # Kodi's ``Addon.getSetting`` takes a single positional id; the
        # injectable ``settings_getter`` contract is ``(key, default)``.
        # Bind the real getter to that two-arg shape so the call sites below
        # don't raise ``TypeError`` under real Kodi (tests inject their own
        # two-arg getter and never hit this branch).
        def getter(key, default=""):
            value = addon.getSetting(key)
            return value if value else default

    else:
        getter = settings_getter
    smb_root = getter("nzbget_smb_root", "").strip()
    url = getter("nzbget_url", "").strip()
    try:
        timeout = int(getter("download_timeout", "") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT
    if timeout < _TIMEOUT_MIN:
        timeout = _TIMEOUT_MIN
    if timeout > _TIMEOUT_MAX:
        timeout = _TIMEOUT_MAX
    return url, smb_root, timeout


def _resolve_failure(handle, message=None):
    if message:
        _notify(_addon_name(), message, 5000)
    xbmcplugin.setResolvedUrl(handle, False, xbmcgui.ListItem())


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

        dialog = xbmcgui.DialogProgress()
        dialog.create(_addon_name(), _string(30218))

        nzbid, error = nzbget_api.append_nzb(
            nzb_url, title, settings_getter=settings_getter
        )
        if not nzbid:
            # Surface the specific (already-redacted) NZBGet message —
            # auth vs dupe vs "append returned 0" — per the spec error
            # table, falling back to the generic string.
            on_failure(error or _string(30222))
            return

        result = poll_nzbget_job(
            nzbid,
            dialog,
            xbmc.Monitor(),
            timeout,
            settings_getter=settings_getter,
        )
        outcome = result["outcome"]

        if outcome == "timeout" or outcome == "aborted":
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

        smb_folder = nzbget_smb_target(smb_root, result["dest_dir"])
        if not smb_folder:
            on_failure(_string(30223))
            return
        video_url = resolve_smb_video(smb_folder)
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


def resolve_and_play_nzbget(handle, params, settings_getter=None):
    """NZBGet entry for the handle-based ``resolve`` path (``/play``).

    Delivers the finished file via ``setResolvedUrl`` — exactly one
    resolution per exit (success True, every failure False).
    """
    nzb_url = unquote(params.get("nzburl", ""))
    title = unquote(params.get("title", "")) or "submission"

    def on_success(video_url):
        listitem = xbmcgui.ListItem(path=video_url)
        xbmcplugin.setResolvedUrl(handle, True, listitem)

    def on_failure(message):
        _resolve_failure(handle, message)

    _run_nzbget_backend(nzb_url, title, settings_getter, on_success, on_failure)


def play_nzbget(nzb_url, title, params=None, settings_getter=None):
    """NZBGet entry for the handle-less ``resolve_and_play`` path.

    ``resolve_and_play`` (TMDBHelper ``/resolve``, the in-addon search
    picker, and script-play) has no plugin handle, so the finished SMB file
    is started with ``xbmc.Player().play`` and failures only notify —
    mirroring the nzbdav ``resolve_and_play`` "no setResolvedUrl" contract.
    """
    if settings_getter is None:
        settings_getter = (params or {}).get("_settings_getter")

    def on_success(video_url):
        listitem = xbmcgui.ListItem(path=video_url)
        xbmc.Player().play(video_url, listitem)

    def on_failure(message):
        if message:
            _notify(_addon_name(), message, 5000)

    _run_nzbget_backend(nzb_url, title, settings_getter, on_success, on_failure)
