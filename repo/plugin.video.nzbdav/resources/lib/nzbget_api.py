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


def _append_params(
    nzb_name, nzb_bytes, category, dupe_key="", dupe_score=0, dupe_mode="SCORE"
):
    """Build NZBGet's modern 11-arg ``append`` params list.

    append(NZBFilename, Content, Category, Priority, AddToTop, AddPaused,
           DupeKey, DupeScore, DupeMode, AutoCategory, PPParameters)

    The trailing AutoCategory + PPParameters args are required by NZBGet's
    modern append signature (nzbget.com v16+, verified against a live 26.1
    box): dropping them makes NZBGet reject the whole call with
    ``Invalid parameter (Parameters)`` (JSON-RPC code 2) and the NZB never
    enters the queue. AutoCategory=False keeps the explicit category we pass
    (the SMB completed-path mapping in nzbget_resolver depends on it rather
    than NZBGet auto-reassigning one); PPParameters=[] = no extra
    post-processing parameters.

    ``dupe_key``/``dupe_score``/``dupe_mode`` implement NZBGet Smart Duplicates
    (#372, per nzbget.com/documentation/rss/#duplicates). Defaults ``""``/``0``/
    ``"SCORE"`` reproduce the pre-#372 single submit exactly; a fleet submit
    passes a shared DupeKey (identifying the release) plus a per-item DupeScore
    (pick highest) so NZBGet downloads the highest score and parks the rest in
    history as backups, failing over on an unrepairable download.
    """
    content_b64 = base64.b64encode(nzb_bytes).decode("ascii")
    filename = "{}.nzb".format(nzb_name or "submission")
    return [
        filename,
        content_b64,
        category,
        0,
        False,
        False,
        dupe_key or "",
        int(dupe_score or 0),
        (dupe_mode or "SCORE").upper(),
        False,
        [],
    ]


def append_nzb(
    nzb_url,
    nzb_name,
    settings_getter=None,
    dupe_key="",
    dupe_score=0,
    dupe_mode="SCORE",
):
    """Fetch the NZB and submit it to NZBGet via append.

    Returns (nzbid, error). On success (int > 0, None); on failure
    (None, message). ``dupe_key``/``dupe_score``/``dupe_mode`` drive NZBGet
    Smart Duplicates (#372); their defaults reproduce the pre-#372 single submit.
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
    params = _append_params(
        nzb_name, nzb_bytes, category, dupe_key, dupe_score, dupe_mode
    )
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


def config_option(name, settings_getter=None):
    """Read one running-config option's value via the ``config`` RPC.

    NZBGet's ``config`` returns the live merged config as a list of
    ``{"Name","Value"}`` structs (options with fixed value sets come back
    lower-cased). Returns the matched value lower-cased, or ``None`` when the
    option is absent or the RPC fails -- callers treat it as best-effort (e.g.
    the #372 HealthCheck=pause warning, per nzbget.com/documentation/rss).
    """
    rows, error = _rpc_call("config", [], settings_getter=settings_getter)
    if error is not None or not isinstance(rows, list):
        return None
    target = str(name or "").lower()
    for row in rows:
        if isinstance(row, dict) and str(row.get("Name", "")).lower() == target:
            return str(row.get("Value", "")).strip().lower()
    return None


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
                "dest_dir": _dest_dir(item),
            }
    return {"present": False, "success": False, "status": "", "dest_dir": ""}


class _CompletedHistory(dict):
    """Name-keyed SUCCESS history mapping, marked when the lookup succeeded.

    Mirrors nzbdav_api's completed-jobs marker: ``router._completed_lookup_was_done``
    tells "lookup ran and found nothing" (skip per-selection re-lookups) apart
    from "lookup failed" via the ``_lookup_done`` attribute.
    """

    def __init__(self, *args, **kwargs):
        lookup_done = kwargs.pop("lookup_done", False)
        super().__init__(*args, **kwargs)
        self._lookup_done = bool(lookup_done)


def _dest_dir(item):
    """Return a history item's destination dir, preferring FinalDir.

    A post-processing script that moves the output sets FinalDir to the final
    location while DestDir stays the original download dir; prefer FinalDir
    when present so the SMB target maps to where the playable file landed.
    """
    return item.get("FinalDir") or item.get("DestDir") or ""


def _history_item_bytes(item):
    """Best-effort byte size of a history item, or None when unknown.

    Prefers the exact FileSizeHi/FileSizeLo 64-bit pair, falling back to the
    rounded FileSizeMB; tolerates str/None fields like the other parsers. A
    None return makes the picker's size gate fail open (name-only match)
    instead of treating the row as a zero-byte mismatch.
    """
    size = int(_as_number(item.get("FileSizeHi"))) * 4294967296 + int(
        _as_number(item.get("FileSizeLo"))
    )
    if size > 0:
        return size
    size_mb = int(_as_number(item.get("FileSizeMB")))
    if size_mb > 0:
        return size_mb * 1048576
    return None


# Picker-render RPC bound: ``completed_history`` runs synchronously right
# before the results dialog opens, so cap the wait the way the nzbdav picker
# path does (nzbdav_api._API_READ_TIMEOUT = 10, "prevent dialog freeze")
# instead of the resolver-path _RPC_TIMEOUT — a silently-unreachable NZBGet
# box must not freeze the picker for 30s.
_PICKER_RPC_TIMEOUT = 10


def completed_history(settings_getter=None):
    """Fetch NZBGet's SUCCESS/* history keyed by job name.

    Powers the picker's "DL" tag in NZBGet mode: a result whose title matches
    a SUCCESS history row already has its finished files on disk, and the
    resolver reuses that row's ``dest_dir`` over SMB on selection instead of
    re-submitting (NZBGet's duplicate check would dupe-delete a re-submission
    of a SUCCESS item, failing the resolve). Values carry the same
    ``name``/``bytes`` shape as nzbdav completed jobs so the router reuses
    its size gate. Returns a lookup_done-marked mapping on RPC success (even
    when empty) and a plain empty dict on any failure — never raises (the
    picker render path has no try/except around tagging). One ``history`` RPC
    per call — same visible-only view as ``history_status``; avoid calling in
    tight loops.
    """
    try:
        hist, error = _rpc_call(
            "history",
            [False],
            settings_getter=settings_getter,
            timeout=_PICKER_RPC_TIMEOUT,
        )
    except Exception as exc:  # pylint: disable=broad-except
        # _rpc_call reads settings before its own try block; a raising
        # injected getter (or an early-startup Addon read) must degrade to
        # "no tags", not crash the picker render.
        xbmc.log(
            "NZB-DAV: NZBGet completed_history failed: {}".format(
                _redact_text(str(exc))
            ),
            xbmc.LOGDEBUG,
        )
        return {}
    if error is not None or not isinstance(hist, list):
        return {}
    jobs = _CompletedHistory(lookup_done=True)
    for item in hist:
        entry = _completed_job_entry(item)
        if entry is None:
            continue
        # History is newest-first; for same-name SUCCESS rows keep the newest
        # one — that's the row a replay's dupe handling would land on.
        if entry["name"] not in jobs:
            jobs[entry["name"]] = entry
    return jobs


def _completed_job_entry(item):
    """Build a SUCCESS completed-job entry from a history item, or None.

    Returns ``None`` for non-dict rows, non-SUCCESS statuses, or unnamed jobs.
    ``dest_dir`` prefers FinalDir like ``history_status`` so a
    post-processing-script move is followed to the file's final location.
    """
    if not isinstance(item, dict):
        return None
    status = str(item.get("Status") or "")
    if not status.startswith("SUCCESS"):
        return None
    name = item.get("Name")
    if not name:
        return None
    return {
        "name": name,
        "status": status,
        "bytes": _history_item_bytes(item),
        "nzbid": item.get("NZBID"),
        "dest_dir": _dest_dir(item),
    }


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
            return value if _is_absolute_path(value) else None
    return None


def _is_absolute_path(value):
    """Return whether ``value`` is an absolute POSIX or Windows path.

    Only an absolute path is trusted; an unexpanded ``${MainDir}`` template
    wouldn't be a reliable prefix of the reported history DestDir.
    """
    return value.startswith("/") or ":\\" in value or value.startswith("\\\\")


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


def _dupekey_match(item, dupe_key):
    """Case-insensitive DupeKey compare (NZBGet matches keys case-insensitively)."""
    return (
        str(item.get("DupeKey") or "").strip().lower()
        == str(dupe_key or "").strip().lower()
    )


def active_group_by_dupekey(dupe_key, exclude_nzbid=None, settings_getter=None):
    """The active (non-PAUSED) queued group sharing ``dupe_key`` (#372 round 2).

    After the pick fails, NZBGet promotes a duplicate backup from history into
    the queue -- it appears in listgroups under the SAME DupeKey with its OWN new
    NZBID and an un-paused status (other backups stay PAUSED). Returns
    ``{"present","nzbid","status","percent"}`` for that promoted download, else
    ``{"present": False}``.
    """
    if not dupe_key:
        return {"present": False}
    groups, error = _rpc_call("listgroups", [0], settings_getter=settings_getter)
    if error is not None or not isinstance(groups, list):
        return {"present": False}
    for group in groups:
        if not isinstance(group, dict) or not _dupekey_match(group, dupe_key):
            continue
        if exclude_nzbid is not None and _same_nzbid(group.get("NZBID"), exclude_nzbid):
            continue
        status = str(group.get("Status") or "")
        if status.upper() == "PAUSED":
            continue  # a still-parked backup, not the promoted-active one
        downloaded = _as_number(group.get("DownloadedSizeMB"))
        total = _as_number(group.get("FileSizeMB"))
        percent = int(downloaded * 100 / total) if total else 0
        return {
            "present": True,
            "nzbid": group.get("NZBID"),
            "status": status,
            "percent": percent,
        }
    return {"present": False}


def history_success_by_dupekey(dupe_key, settings_getter=None):
    """A SUCCESS history item sharing ``dupe_key`` (a completed group member).

    Lets the poll play a duplicate backup that NZBGet already downloaded to
    success after the pick failed (#372 round 2). Returns
    ``{"present","nzbid","dest_dir"}`` or ``{"present": False}``.
    """
    if not dupe_key:
        return {"present": False}
    hist, error = _rpc_call("history", [False], settings_getter=settings_getter)
    if error is not None or not isinstance(hist, list):
        return {"present": False}
    for item in hist:
        if not isinstance(item, dict) or not _dupekey_match(item, dupe_key):
            continue
        if str(item.get("Status") or "").startswith("SUCCESS"):
            return {
                "present": True,
                "nzbid": item.get("NZBID"),
                "dest_dir": _dest_dir(item),
            }
    return {"present": False}


def cancel_dupekey_group(dupe_key, settings_getter=None):
    """Delete every member of a DupeKey group on user-cancel (#372 round 2).

    Removes the parked duplicate backups (hidden ``Kind=DUP`` history records,
    fetched with ``history(Hidden=true)``) FIRST so NZBGet has nothing to promote,
    then the queued members (the active pick / a promoted backup). Best-effort --
    a single ``editqueue`` per bucket deletes all matching NZBIDs at once.

    ``history(Hidden=true)`` also returns VISIBLE rows (a prior ``Kind=NZB``
    ``SUCCESS/*`` for a stable DupeKey), so the history delete is restricted to
    ``Kind=DUP``: only untried parked backups can be promoted, and this must never
    ``HistoryFinalDelete`` a completed success (which would wipe its files).
    """
    if not dupe_key:
        return
    hist, error = _rpc_call("history", [True], settings_getter=settings_getter)
    if error is None and isinstance(hist, list):
        hist_ids = [
            item.get("NZBID")
            for item in hist
            if isinstance(item, dict)
            and _dupekey_match(item, dupe_key)
            and str(item.get("Kind") or "").upper() == "DUP"
            and item.get("NZBID") is not None
        ]
        if hist_ids:
            _rpc_call(
                "editqueue",
                ["HistoryFinalDelete", "", hist_ids],
                settings_getter=settings_getter,
            )
    groups, error = _rpc_call("listgroups", [0], settings_getter=settings_getter)
    if error is None and isinstance(groups, list):
        group_ids = [
            group.get("NZBID")
            for group in groups
            if isinstance(group, dict)
            and _dupekey_match(group, dupe_key)
            and group.get("NZBID") is not None
        ]
        if group_ids:
            _rpc_call(
                "editqueue",
                ["GroupFinalDelete", "", group_ids],
                settings_getter=settings_getter,
            )
