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


def _get_settings(settings_getter=None):
    if settings_getter is None:
        addon = xbmcaddon.Addon("plugin.video.nzbdav")
        url = addon.getSetting("nzbget_url").strip().rstrip("/")
        user = addon.getSetting("nzbget_username").strip()
        password = addon.getSetting("nzbget_password")
        category = addon.getSetting("nzbget_category").strip()
    else:
        url = settings_getter("nzbget_url", "").strip().rstrip("/")
        user = settings_getter("nzbget_username", "").strip()
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
    payload = {"method": method, "params": list(params), "id": 1}
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
    #        DupeKey, DupeScore, DupeMode)
    params = [filename, content_b64, category, 0, False, False, "", 0, "SCORE"]
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
        if group.get("NZBID") == nzbid:
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
        if item.get("NZBID") == nzbid:
            status = str(item.get("Status") or "")
            return {
                "present": True,
                "success": status.startswith("SUCCESS"),
                "status": status,
                "dest_dir": item.get("DestDir", ""),
            }
    return {"present": False, "success": False, "status": "", "dest_dir": ""}


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
    # editqueue(Command, Offset, Text, IDs)
    _rpc_call(
        "editqueue",
        ["GroupFinalDelete", 0, "", [nzbid]],
        settings_getter=settings_getter,
    )
    _rpc_call(
        "editqueue",
        ["HistoryFinalDelete", 0, "", [nzbid]],
        settings_getter=settings_getter,
    )
