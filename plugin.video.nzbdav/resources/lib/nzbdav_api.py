# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""nzbdav SABnzbd-compatible API client."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

import xbmc
import xbmcaddon

from resources.lib.http_util import http_get as _http_get

_DEFAULT_SUBMIT_TIMEOUT = 30

_HTML_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_server_message(raw):
    """Sanitize a raw HTTP response body for display in a Kodi dialog.

    Strips HTML tags (some servers return styled error pages), collapses
    runs of whitespace to single spaces, and trims. Returns an empty
    string if nothing meaningful remains. Caller is responsible for
    truncation and the empty-fallback ("(no error message)").
    """
    if not raw:
        return ""
    cleaned = _HTML_TAG_RE.sub("", raw)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned


def _get_settings():
    addon = xbmcaddon.Addon()
    url = addon.getSetting("nzbdav_url").rstrip("/")
    api_key = addon.getSetting("nzbdav_api_key")
    return url, api_key


def _get_submit_timeout():
    """Read the configurable submit timeout from settings, default 30s."""
    try:
        raw = xbmcaddon.Addon().getSetting("submit_timeout")
        return int(raw) if raw else _DEFAULT_SUBMIT_TIMEOUT
    except (ValueError, TypeError):
        return _DEFAULT_SUBMIT_TIMEOUT


def submit_nzb(nzb_url, nzb_name=""):
    """Submit an NZB URL to nzbdav's SABnzbd-compatible API.

    Args:
        nzb_url: Absolute URL to the NZB file as returned by NZBHydra2.
        nzb_name: Human-friendly title shown in nzbdav's queue/history.

    Returns:
        Tuple of (nzo_id, error). At most one of the two is non-None:
        - On success: (nzo_id_string, None)
        - On structured HTTP error from nzbdav (any 4xx/5xx that comes
          back as urllib.error.HTTPError): (None, {"status": int,
          "message": str}). The caller classifies by status code to
          decide retry vs surface.
        - On non-HTTP errors (network unreachable, JSON decode failure,
          truthy-but-empty response, anything else): (None, None) —
          caller may retry.

    Side effects:
        Reads nzbdav settings from Kodi via xbmcaddon.Addon().
        Performs an HTTP GET to nzbdav /api with mode=addurl.
        Logs submission URLs, successes, and errors to the Kodi log.
    """
    try:
        base_url, api_key = _get_settings()
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log("NZB-DAV: Failed to read nzbdav settings: {}".format(e), xbmc.LOGERROR)
        return None, None
    params = {
        "mode": "addurl",
        "name": nzb_url,
        "nzbname": nzb_name,
        "apikey": api_key,
        "output": "json",
    }
    url = "{}/api?{}".format(base_url, urlencode(params))
    from resources.lib.http_util import redact_url

    timeout = _get_submit_timeout()
    xbmc.log(
        "NZB-DAV: Submit NZB URL (timeout={}s): {}".format(timeout, redact_url(url)),
        xbmc.LOGDEBUG,
    )
    try:
        response_text = _http_get(url, timeout=timeout)
        response = json.loads(response_text)
    except HTTPError as e:
        # nzbdav returned a structured HTTP error (e.g. 500 on duplicate
        # submit, 502/503/504 from upstream issues). Capture the body so
        # the caller can either surface it or classify retries based on
        # status code.
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # pylint: disable=broad-except
            pass
        body = _sanitize_server_message(body)[:500]
        xbmc.log(
            "NZB-DAV: Submit NZB got HTTP {} from nzbdav: {}".format(e.code, body),
            xbmc.LOGERROR,
        )
        return None, {"status": e.code, "message": body}
    except (
        URLError,
        json.JSONDecodeError,
        Exception,
    ) as e:  # pylint: disable=broad-except
        xbmc.log("NZB-DAV: Submit NZB request failed: {}".format(e), xbmc.LOGERROR)
        return None, None
    nzo_ids = response.get("nzo_ids")
    if response.get("status") and isinstance(nzo_ids, list) and nzo_ids and nzo_ids[0]:
        nzo_id = nzo_ids[0]
        xbmc.log(
            "NZB-DAV: NZB submitted successfully, nzo_id={}".format(nzo_id),
            xbmc.LOGINFO,
        )
        return nzo_id, None
    xbmc.log(
        "NZB-DAV: Submit NZB response had no nzo_ids: {}".format(response),
        xbmc.LOGERROR,
    )
    return None, None


def cancel_job(nzo_id, timeout=3):
    """Cancel an in-flight nzbdav job by removing it from the queue.

    Issues a single SABnzbd-compatible queue DELETE
    (mode=queue&name=delete&value=<nzo_id>). This is "cancel" semantics,
    not "delete everywhere" — completed and failed jobs that have already
    moved to nzbdav's history are deliberately left intact so the user
    can still inspect failure history in nzbdav's web UI.

    Args:
        nzo_id: The nzbdav job identifier to cancel.
        timeout: HTTP timeout in seconds. Defaults to 3 because this is
            called from user-facing abort paths (cancel button, Kodi
            shutdown) where waiting longer feels broken. A healthy
            nzbdav responds in ~50ms; the 3s cap protects against
            unreachable backends without blocking the UI thread for the
            full network timeout.

    Returns:
        True if nzbdav reported the queue DELETE succeeded (job was
        found in the active queue and removed). False otherwise — which
        includes the legitimate "job not in queue anymore" case (it
        either completed, failed, or was already manually cancelled).
        Callers should treat False as a non-error: the next play
        attempt's find_completed_by_name() check will pick up any job
        that genuinely raced into history.

    Side effects:
        One HTTP GET to nzbdav /api with a bounded timeout. Logs
        outcome at LOGINFO on success, LOGDEBUG on "not in queue"
        (a normal race), LOGWARNING on network error.
    """
    try:
        base_url, api_key = _get_settings()
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: cancel_job failed to read settings: {}".format(e),
            xbmc.LOGERROR,
        )
        return False

    params = {
        "mode": "queue",
        "name": "delete",
        "value": nzo_id,
        "apikey": api_key,
        "output": "json",
    }
    url = "{}/api?{}".format(base_url, urlencode(params))
    from resources.lib.http_util import redact_url

    xbmc.log(
        "NZB-DAV: cancel_job URL (timeout={}s): {}".format(timeout, redact_url(url)),
        xbmc.LOGDEBUG,
    )
    try:
        response_text = _http_get(url, timeout=timeout)
        response = json.loads(response_text)
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: cancel_job network error for nzo_id={}: {}".format(nzo_id, e),
            xbmc.LOGWARNING,
        )
        return False
    if response.get("status") is True:
        xbmc.log(
            "NZB-DAV: cancel_job removed nzo_id={} from queue".format(nzo_id),
            xbmc.LOGINFO,
        )
        return True
    err = response.get("error", "unknown")
    xbmc.log(
        "NZB-DAV: cancel_job got status=false for nzo_id={} (job is no longer "
        "in the active queue, may have completed/failed): {}".format(nzo_id, err),
        xbmc.LOGDEBUG,
    )
    return False


def get_job_history(nzo_id):
    """Check if a job is completed in nzbdav's history.

    Returns dict with: status, storage, name. None if not found.
    """
    try:
        base_url, api_key = _get_settings()
    except Exception:
        return None

    params = {
        "mode": "history",
        "nzo_ids": nzo_id,
        "apikey": api_key,
        "output": "json",
    }
    url = "{}/api?{}".format(base_url, urlencode(params))

    try:
        response_text = _http_get(url)
        response = json.loads(response_text)
    except Exception:
        return None

    slots = response.get("history", {}).get("slots", [])
    for slot in slots:
        if slot.get("nzo_id") == nzo_id:
            return {
                "status": slot.get("status", ""),
                "storage": slot.get("storage", ""),
                "name": slot.get("name", ""),
                "fail_message": slot.get("fail_message", ""),
            }
    return None


def find_completed_by_name(name):
    """Search nzbdav history for a completed download matching the given name.

    Uses the SABnzbd search parameter to narrow results, then matches by name.
    Falls back to checking the full history if search returns nothing.

    Returns dict with: status, storage, name, nzo_id. None if not found.
    """
    try:
        base_url, api_key = _get_settings()
    except Exception:
        return None

    # Extract a short search term from the name (first few words)
    search_term = name.split(".")[0] if "." in name else name

    params = {
        "mode": "history",
        "apikey": api_key,
        "output": "json",
        "limit": 200,
        "search": search_term,
    }
    url = "{}/api?{}".format(base_url, urlencode(params))

    try:
        response_text = _http_get(url)
        response = json.loads(response_text)
    except Exception:
        return None

    slots = response.get("history", {}).get("slots", [])
    for slot in slots:
        if slot.get("name") == name and slot.get("status") == "Completed":
            xbmc.log(
                "NZB-DAV: Found existing download '{}' in history".format(name),
                xbmc.LOGINFO,
            )
            return {
                "status": slot.get("status", ""),
                "storage": slot.get("storage", ""),
                "name": slot.get("name", ""),
                "nzo_id": slot.get("nzo_id", ""),
            }

    # Fallback: broader search without search term filter
    if search_term:
        params.pop("search")
        url = "{}/api?{}".format(base_url, urlencode(params))
        try:
            response_text = _http_get(url)
            response = json.loads(response_text)
        except Exception:
            return None

        slots = response.get("history", {}).get("slots", [])
        for slot in slots:
            if slot.get("name") == name and slot.get("status") == "Completed":
                xbmc.log(
                    "NZB-DAV: Found '{}' in history (broad search)".format(name),
                    xbmc.LOGINFO,
                )
                return {
                    "status": slot.get("status", ""),
                    "storage": slot.get("storage", ""),
                    "name": slot.get("name", ""),
                    "nzo_id": slot.get("nzo_id", ""),
                }
    return None


def get_completed_names():
    """Fetch all completed download names from nzbdav history.

    Returns:
        A set of completed download names for fast membership checks. Returns
        an empty set on any error or when no completed jobs exist.

    Side effects:
        Reads nzbdav settings from Kodi via xbmcaddon.Addon().
        Performs an HTTP GET to nzbdav /api?mode=history on every call; avoid
        calling this in tight loops.
        Logs the number of names loaded at debug level.
    """
    try:
        base_url, api_key = _get_settings()
    except Exception:
        return set()

    params = {
        "mode": "history",
        "apikey": api_key,
        "output": "json",
        "limit": 500,
    }
    url = "{}/api?{}".format(base_url, urlencode(params))

    try:
        response_text = _http_get(url)
        response = json.loads(response_text)
    except Exception:
        return set()

    slots = response.get("history", {}).get("slots", [])
    names = set()
    for slot in slots:
        if slot.get("status") == "Completed" and slot.get("name"):
            names.add(slot["name"])
    xbmc.log(
        "NZB-DAV: Loaded {} completed download names from history".format(len(names)),
        xbmc.LOGDEBUG,
    )
    return names


def get_job_status(nzo_id):
    try:
        base_url, api_key = _get_settings()
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Failed to read nzbdav settings for status check: {}".format(e),
            xbmc.LOGERROR,
        )
        return None
    params = {
        "mode": "queue",
        "nzo_ids": nzo_id,
        "apikey": api_key,
        "output": "json",
    }
    url = "{}/api?{}".format(base_url, urlencode(params))
    from resources.lib.http_util import redact_url

    xbmc.log("NZB-DAV: Job status URL: {}".format(redact_url(url)), xbmc.LOGDEBUG)
    try:
        response_text = _http_get(url)
        response = json.loads(response_text)
    except (URLError, json.JSONDecodeError, Exception) as e:
        xbmc.log(
            "NZB-DAV: Job status request failed for nzo_id={}: {}".format(nzo_id, e),
            xbmc.LOGERROR,
        )
        return None
    slots = response.get("queue", {}).get("slots", [])
    for slot in slots:
        if slot.get("nzo_id") == nzo_id:
            status = slot.get("status", "Unknown")
            percentage = slot.get("percentage", "0")
            xbmc.log(
                "NZB-DAV: Job {} status={} percentage={}".format(
                    nzo_id, status, percentage
                ),
                xbmc.LOGDEBUG,
            )
            return {
                "status": status,
                "percentage": percentage,
                "filename": slot.get("filename", ""),
            }
    xbmc.log(
        "NZB-DAV: Job {} not found in queue (may be complete)".format(nzo_id),
        xbmc.LOGDEBUG,
    )
    return None
