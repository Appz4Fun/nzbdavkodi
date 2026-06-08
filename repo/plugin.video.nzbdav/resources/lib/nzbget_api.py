# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZBGet JSON-RPC client: submit an NZB, poll status, resolve completion.

Mirrors nzbdav_api conventions: settings-getter injectable for tests,
``(value, error)`` tuple returns, redacted logging. RPC endpoint is
``<nzbget_url>/jsonrpc`` with HTTP Basic auth.
"""

import base64
import json

import xbmc
import xbmcaddon

from resources.lib.http_util import http_get as _http_get
from resources.lib.http_util import http_post_json as _http_post_json
from resources.lib.http_util import redact_text as _redact_text

_RPC_TIMEOUT = 30

# settings.xml schema defaults. The injected ``settings_getter``
# (``_get_script_setting`` on the RunScript/widget path) reads the raw profile
# XML, where a setting left at its displayed default is simply absent — so it
# returns the fallback we pass. Mirror the schema defaults here, or a user who
# enables NZBGet + sets the SMB root but leaves the URL/username untouched is
# sent down the NZBGet path only to fail "not configured".
_DEFAULT_URL = "http://localhost:6789"
_DEFAULT_USER = "nzbget"


def _get_settings(settings_getter=None):
    if settings_getter is None:
        addon = xbmcaddon.Addon("plugin.video.nzbdav")
        url = addon.getSetting("nzbget_url").strip().rstrip("/")
        user = addon.getSetting("nzbget_username").strip()
        password = addon.getSetting("nzbget_password")
        category = addon.getSetting("nzbget_category").strip()
    else:
        url = settings_getter("nzbget_url", _DEFAULT_URL).strip().rstrip("/")
        user = settings_getter("nzbget_username", _DEFAULT_USER).strip()
        password = settings_getter("nzbget_password", "")
        category = settings_getter("nzbget_category", "").strip()
    return url, user, password, category


def _rpc_url(base_url):
    return "{}/jsonrpc".format(base_url)


def _rpc_call(method, params, settings_getter=None, timeout=_RPC_TIMEOUT):
    """Invoke a JSON-RPC method. Returns (result, error).

    On success: (result_value, None). On any failure: (None, message_str).
    """
    base_url, user, password, _category = _get_settings(settings_getter)
    if not base_url:
        return None, "not_configured"
    # ``id`` MUST precede ``params``: NZBGet's legacy JSON-RPC parser can
    # mis-parse fields that appear after ``params`` (maintainer note,
    # forum.nzbget.net t=2209), making append/poll RPCs fail or pick up an
    # extra parameter. Order the payload id -> method -> params accordingly.
    payload = {"id": 1, "method": method, "params": list(params)}
    try:
        text = _http_post_json(
            _rpc_url(base_url),
            payload,
            timeout=timeout,
            basic_auth=(user, password),
        )
        data = json.loads(text)
    except Exception as exc:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: NZBGet {} failed: {}".format(method, _redact_text(str(exc))),
            xbmc.LOGERROR,
        )
        return None, _redact_text(str(exc))
    if isinstance(data, dict) and data.get("error"):
        message = _redact_text(str(data["error"]))
        xbmc.log(
            "NZB-DAV: NZBGet {} error: {}".format(method, message),
            xbmc.LOGERROR,
        )
        return None, message
    return data.get("result") if isinstance(data, dict) else None, None


def _fetch_nzb_bytes(nzb_url):
    """Fetch the NZB body. Returns bytes. Raises on failure."""
    body = _http_get(nzb_url, timeout=_RPC_TIMEOUT)
    if isinstance(body, str):
        body = body.encode("utf-8")
    return body


def append_nzb(nzb_url, nzb_name, settings_getter=None):
    """Fetch the NZB and submit it to NZBGet via append.

    Returns (nzbid, error). On success (int > 0, None); on failure
    (None, message).
    """
    _base_url, _user, _password, category = _get_settings(settings_getter)
    try:
        nzb_bytes = _fetch_nzb_bytes(nzb_url)
    except Exception as exc:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: NZBGet NZB fetch failed: {}".format(_redact_text(str(exc))),
            xbmc.LOGERROR,
        )
        return None, _redact_text(str(exc))
    content_b64 = base64.b64encode(nzb_bytes).decode("ascii")
    filename = "{}.nzb".format(nzb_name or "submission")
    # append(NZBFilename, Content, Category, Priority, AddToTop, AddPaused,
    #        DupeKey, DupeScore, DupeMode, AutoCategory, PPParameters)
    #
    # The trailing AutoCategory + PPParameters args are required by NZBGet's
    # modern append signature (nzbget.com v16+, verified against a live 26.1
    # box): dropping them makes NZBGet reject the whole call with
    # ``Invalid parameter (Parameters)`` (JSON-RPC code 2) and the NZB never
    # enters the queue. AutoCategory=False keeps the explicit category we pass
    # (the SMB completed-path mapping in nzbget_resolver depends on it rather
    # than NZBGet auto-reassigning one); PPParameters=[] = no extra
    # post-processing parameters.
    params = [
        filename,
        content_b64,
        category,
        0,
        False,
        False,
        "",
        0,
        "SCORE",
        False,
        [],
    ]
    result, error = _rpc_call("append", params, settings_getter=settings_getter)
    if error is not None:
        return None, error
    try:
        nzbid = int(result)
    except (TypeError, ValueError):
        nzbid = 0
    if nzbid <= 0:
        return None, "append returned {!r}".format(result)
    return nzbid, None


def _as_number(value):
    """Coerce an NZBGet size field to a float, tolerating str/None.

    NZBGet (and some proxy layers) serialize size fields as decimal
    strings; the raw ``downloaded * 100 / total`` math would raise
    ``TypeError`` on a str. Fall back to 0 so a malformed/odd field
    degrades to 0% instead of aborting the whole poll on one tick.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _same_nzbid(left, right):
    """Compare two NZBID values tolerating str/int mismatch.

    Some NZBGet builds / proxy layers serialize ``NZBID`` as a decimal string
    while callers pass an int; a strict ``==`` would then treat the job as
    absent and let the poll fall through to a bogus timeout/failure.
    """
    try:
        return int(left) == int(right)
    except (TypeError, ValueError):
        return str(left) == str(right)


def group_status(nzbid, settings_getter=None):
    """Look up an active job by NZBID via listgroups.

    Returns a dict: {"present": bool, "status": str, "percent": int}.
    "present" is False once the job leaves the active queue (moved to
    history). On RPC error, returns present=False with status="ERROR".
    """
    groups, error = _rpc_call("listgroups", [0], settings_getter=settings_getter)
    if error is not None or not isinstance(groups, list):
        return {"present": False, "status": "ERROR", "percent": 0}
    for group in groups:
        if not isinstance(group, dict):
            continue
        if _same_nzbid(group.get("NZBID"), nzbid):
            downloaded = _as_number(group.get("DownloadedSizeMB"))
            total = _as_number(group.get("FileSizeMB"))
            percent = int(downloaded * 100 / total) if total else 0
            return {
                "present": True,
                "status": str(group.get("Status") or ""),
                "percent": percent,
            }
    return {"present": False, "status": "", "percent": 0}


def history_status(nzbid, settings_getter=None):
    """Look up a terminal job by NZBID via history.

    Returns {"present": bool, "success": bool, "status": str,
    "dest_dir": str}. "success" is True ONLY for SUCCESS/* statuses, per the
    spec's completion guarantee (full post-processing = a repaired, unpacked,
    playable file). WARNING/* (incl. WARNING/REPAIRABLE / WARNING/DAMAGED,
    where par2 repair did not run) is deliberately treated as a failure
    rather than risk playing a corrupt file — the job is left in history so
    it can be retried.
    """
    hist, error = _rpc_call("history", [False], settings_getter=settings_getter)
    if error is not None or not isinstance(hist, list):
        return {"present": False, "success": False, "status": "", "dest_dir": ""}
    for item in hist:
        if not isinstance(item, dict):
            continue
        if _same_nzbid(item.get("NZBID"), nzbid):
            status = str(item.get("Status") or "")
            return {
                "present": True,
                "success": status.startswith("SUCCESS"),
                "status": status,
                # A post-processing script that moves the output sets FinalDir
                # to the final location while DestDir stays the original
                # download dir; prefer FinalDir when present so the SMB target
                # maps to where the playable file actually landed.
                "dest_dir": (item.get("FinalDir") or item.get("DestDir") or ""),
            }
    return {"present": False, "success": False, "status": "", "dest_dir": ""}


def completed_base_dir(settings_getter=None):
    """Return NZBGet's configured global completed DestDir (absolute), or None.

    Lets ``nzbget_smb_target`` map a history ``DestDir`` *relative* to NZBGet's
    completed base onto the SMB root, which is exact for any category/custom
    DestDir layout. Best-effort: any RPC failure or a value that isn't an
    absolute path (e.g. an unexpanded ``${MainDir}`` template) degrades to None
    so the caller falls back to its folder heuristic.
    """
    cfg, error = _rpc_call("config", [], settings_getter=settings_getter)
    if error is not None or not isinstance(cfg, list):
        return None
    for item in cfg:
        if not isinstance(item, dict):
            continue
        if str(item.get("Name", "")).strip().lower() == "destdir":
            value = str(item.get("Value") or "").strip()
            # Only trust an absolute path; an unexpanded template wouldn't be a
            # reliable prefix of the reported history DestDir.
            if value.startswith("/") or ":\\" in value or value.startswith("\\\\"):
                return value
            return None
    return None


def test_connection(settings_getter=None):
    """Probe NZBGet via the version method. Returns (ok, error)."""
    result, error = _rpc_call("version", [], settings_getter=settings_getter)
    if error is not None:
        return False, error
    return result is not None, None


def cancel_job(nzbid, settings_getter=None):
    """Delete a job and its files (explicit user cancel).

    Tries the active-queue delete first (GroupFinalDelete removes the job
    and downloaded files), then a history delete in case it already moved
    to history. Best-effort — errors are logged, not raised.
    """
    # editqueue(Command, Args, IDs) — NZBGet v18+ dropped the legacy int
    # ``Offset`` parameter (pre-v18 was ``Command, Offset, Text, IDs``). The
    # target boxes run nzbget.com 16+/26.x, which reject the 4-arg shape and
    # would leave a "canceled" download running; matches the modern 11-arg
    # append signature this client already sends.
    _rpc_call(
        "editqueue",
        ["GroupFinalDelete", "", [nzbid]],
        settings_getter=settings_getter,
    )
    _rpc_call(
        "editqueue",
        ["HistoryFinalDelete", "", [nzbid]],
        settings_getter=settings_getter,
    )
