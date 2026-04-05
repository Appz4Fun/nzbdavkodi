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
    xbmc.log("NZB-DAV: Submit NZB URL: {}".format(url), xbmc.LOGDEBUG)
    try:
        response_text = _http_get(url)
        response = json.loads(response_text)
    except (URLError, json.JSONDecodeError, Exception) as e:
        xbmc.log("NZB-DAV: Submit NZB request failed: {}".format(e), xbmc.LOGERROR)
        return None
    if response.get("status") and response.get("nzo_ids"):
        nzo_id = response["nzo_ids"][0]
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
            }
    return None


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
    xbmc.log("NZB-DAV: Job status URL: {}".format(url), xbmc.LOGDEBUG)
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
