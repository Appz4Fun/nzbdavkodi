# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""nzbdav SABnzbd-compatible API client."""

import json
from urllib.error import URLError
from urllib.parse import urlencode

import xbmc

from resources.lib.http_util import http_get as _http_get


def _get_settings():
    import xbmcaddon

    addon = xbmcaddon.Addon()
    url = addon.getSetting("nzbdav_url").rstrip("/")
    api_key = addon.getSetting("nzbdav_api_key")
    return url, api_key


def submit_nzb(nzb_url, nzb_name=""):
    """Submit an NZB URL to nzbdav's SABnzbd-compatible API.

    Args:
        nzb_url: Absolute URL to the NZB file as returned by NZBHydra2.
        nzb_name: Human-friendly title shown in nzbdav's queue/history.

    Returns:
        The nzo_id string assigned by nzbdav when the request succeeds, or
        None if settings are missing, the HTTP request fails, or the response
        does not include an nzo_id.

    Side effects:
        Reads nzbdav settings from Kodi via xbmcaddon.Addon().
        Performs an HTTP GET to nzbdav /api with mode=addurl.
        Logs submission URLs, successes, and errors to the Kodi log.
    """
    try:
        base_url, api_key = _get_settings()
    except Exception as e:
        xbmc.log("NZB-DAV: Failed to read nzbdav settings: {}".format(e), xbmc.LOGERROR)
        return None
    params = {
        "mode": "addurl",
        "name": nzb_url,
        "nzbname": nzb_name,
        "apikey": api_key,
        "output": "json",
    }
    url = "{}/api?{}".format(base_url, urlencode(params))
    from resources.lib.http_util import redact_url

    xbmc.log("NZB-DAV: Submit NZB URL: {}".format(redact_url(url)), xbmc.LOGDEBUG)
    try:
        response_text = _http_get(url)
        response = json.loads(response_text)
    except (URLError, json.JSONDecodeError, Exception) as e:
        xbmc.log("NZB-DAV: Submit NZB request failed: {}".format(e), xbmc.LOGERROR)
        return None
    nzo_ids = response.get("nzo_ids")
    if response.get("status") and isinstance(nzo_ids, list) and nzo_ids and nzo_ids[0]:
        nzo_id = nzo_ids[0]
        xbmc.log(
            "NZB-DAV: NZB submitted successfully, nzo_id={}".format(nzo_id),
            xbmc.LOGINFO,
        )
        return nzo_id
    xbmc.log(
        "NZB-DAV: Submit NZB response had no nzo_ids: {}".format(response),
        xbmc.LOGERROR,
    )
    return None


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
