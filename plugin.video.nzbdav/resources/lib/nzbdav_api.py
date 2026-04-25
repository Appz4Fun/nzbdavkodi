# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""nzbdav SABnzbd-compatible API client."""

import json
import re
import socket
from urllib.error import HTTPError
from urllib.parse import urlencode

import xbmc
import xbmcaddon

from resources.lib.http_util import http_get as _http_get

# nzbdav's /api?mode=addurl handler fetches the .nzb from the indexer,
# parses the XML, and enumerates segments before returning. On a big
# REMUX this can routinely exceed 30 s — the previous default caused
# client-side timeouts for submits that nzbdav had actually accepted
# and was still processing. 120 s gives nzbdav real headroom while
# remaining short enough that a truly unreachable backend still
# surfaces in a reasonable time.
_DEFAULT_SUBMIT_TIMEOUT = 120

_HTML_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")


def _coerce_response_dict(response):
    """Return ``response`` if it's a dict, else an empty dict.

    nzbdav's SABnzbd-compatible API documents object responses, but a
    misconfigured proxy / error page / truncated body can produce a JSON
    array, ``null``, or scalar. Without this normalization, every
    ``response.get(...)`` chain that follows ``json.loads`` raises
    ``AttributeError`` on those inputs and crashes the caller. Treating
    non-dict JSON as "no useful payload" lets the existing fallback
    branches (`response.get("status")`, etc.) handle it as the absence
    of the expected fields, which is what they were already designed to
    do for missing keys.
    """
    return response if isinstance(response, dict) else {}


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


# Hard min/max clamp for submit_timeout. The setting is exposed as
# free-form text in the Kodi UI, so a typo can produce wildly wrong
# values (we hit ``submit_timeout=300000`` once — 83 hours, which
# would let a hung connection block the resolver effectively forever
# before timing out). 5 s is the absolute minimum that still gives
# nzbdav time to respond on a healthy LAN; 600 s (10 min) is the
# absolute maximum that's compatible with the queue-adoption path
# being effective.
_SUBMIT_TIMEOUT_MIN = 5
_SUBMIT_TIMEOUT_MAX = 600


def _clamp_int_setting(value, lo, hi):
    """Clamp an int setting value into [lo, hi]. Used to defend
    against typo'd setting values cascading into pathological
    behavior (hour-long timeouts, sub-MB threshold, etc.). Returns
    ``value`` if already in range, otherwise the nearer bound."""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _get_submit_timeout():
    """Read the configurable submit timeout from settings, default 120s.

    Clamped to [_SUBMIT_TIMEOUT_MIN, _SUBMIT_TIMEOUT_MAX] so a typo
    in the Kodi settings UI can't produce a 83-hour timeout."""
    try:
        raw = xbmcaddon.Addon().getSetting("submit_timeout")
        value = int(raw) if raw else _DEFAULT_SUBMIT_TIMEOUT
    except (ValueError, TypeError):
        return _DEFAULT_SUBMIT_TIMEOUT
    return _clamp_int_setting(value, _SUBMIT_TIMEOUT_MIN, _SUBMIT_TIMEOUT_MAX)


def _is_timeout_error(exc):
    """True if ``exc`` is or wraps a socket/connection timeout.

    Covers both shapes that ``urllib.request.urlopen(..., timeout=N)``
    can raise: a bare ``socket.timeout`` (which is an alias for
    ``TimeoutError`` on Python 3.10+) and a ``URLError`` whose
    ``reason`` attribute is a timeout. Either counts as "client gave
    up before the server responded" — we want those routed to the
    queue-adoption path, not the generic retry path.
    """
    if isinstance(exc, socket.timeout):
        return True
    reason = getattr(exc, "reason", None)
    if reason is not None and isinstance(reason, socket.timeout):
        return True
    return False


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
        - On **client-side timeout** (socket.timeout, or URLError
          wrapping one): (None, {"status": "timeout", "message": str}).
          A timeout does NOT mean the submit failed — nzbdav may well
          have accepted the request and be processing it right now.
          The caller should check nzbdav's queue / history for a job
          matching ``nzb_name`` before retrying; a fresh submit would
          risk either a duplicate rejection or orphaning the
          in-progress job with a second nzo_id.
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
        response = _coerce_response_dict(json.loads(response_text))
    except HTTPError as e:
        # nzbdav returned a structured HTTP error (e.g. 500 on duplicate
        # submit, 502/503/504 from upstream issues). Capture the body so
        # the caller can either surface it or classify retries based on
        # status code. Redact apikey-style tokens: nzbdav's error pages
        # sometimes echo the failing URL (which carried the indexer's
        # apikey) back to the client, which then goes into a Kodi dialog
        # visible to anyone reading the screen / logs.
        from resources.lib.http_util import redact_text

        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # pylint: disable=broad-except
            pass
        body = redact_text(_sanitize_server_message(body))[:500]
        xbmc.log(
            "NZB-DAV: Submit NZB got HTTP {} from nzbdav: {}".format(e.code, body),
            xbmc.LOGERROR,
        )
        return None, {"status": e.code, "message": body}
    except Exception as e:  # pylint: disable=broad-except
        # ``Exception`` intentionally — the prior ``(socket.timeout, URLError,
        # json.JSONDecodeError, Exception)`` tuple made the three named
        # classes dead code because ``Exception`` is their base. This path
        # is the last-chance safety net for an nzbdav submit, so catching
        # the full family (including things we haven't anticipated) keeps
        # the resolver from crashing while still letting caller-level
        # queue/history probes retry.
        if _is_timeout_error(e):
            xbmc.log(
                "NZB-DAV: Submit NZB client-side timeout after {}s — nzbdav "
                "may have accepted the submit anyway; caller will check "
                "queue/history for '{}' before retrying".format(timeout, nzb_name),
                xbmc.LOGWARNING,
            )
            return None, {
                "status": "timeout",
                "message": "Timed out after {}s".format(timeout),
            }
        # Redact: HTTPError / URLError str() can echo the failing URL
        # (which embeds the indexer apikey) into the log. Same defense as
        # the prowlarr / hydra fetch paths. TODO.md §H.2-H2f / §H.3.
        from resources.lib.http_util import redact_text

        xbmc.log(
            "NZB-DAV: Submit NZB request failed: {}".format(redact_text(str(e))),
            xbmc.LOGERROR,
        )
        return None, None
    nzo_ids = response.get("nzo_ids")
    if response.get("status") and isinstance(nzo_ids, list) and nzo_ids and nzo_ids[0]:
        nzo_id = nzo_ids[0]
        xbmc.log(
            "NZB-DAV: NZB submitted successfully, nzo_id={}".format(nzo_id),
            xbmc.LOGINFO,
        )
        return nzo_id, None
    # Distinguish "nzbdav saw the request but rejected the NZB" from
    # "request never reached nzbdav". The former returns a 200 with
    # status=false (e.g. empty / truncated / password-only NZB) and is
    # NOT retryable — the caller should surface a specific error
    # immediately. The latter (network failure, timeout) is already
    # handled in the except branches above.
    error_msg = response.get("error") if isinstance(response, dict) else None
    xbmc.log(
        "NZB-DAV: Submit NZB rejected by nzbdav: {}".format(response),
        xbmc.LOGERROR,
    )
    return None, {
        "status": "rejected",
        "message": str(error_msg) if error_msg else "nzbdav rejected the NZB",
    }


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
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        # cancel_job is a "make the mess go away" path — anything that
        # prevents the cancel from reaching nzbdav should just get logged
        # and swallowed so the caller doesn't cascade into error dialogs.
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
    """Check if a job has landed in nzbdav's history.

    Returns dict with keys ``status``, ``storage``, ``name``,
    ``fail_message`` when the nzo_id is found, or ``None`` when it
    hasn't appeared yet (or on any network / settings / parse error —
    the resolver's poll loop treats None as "keep polling", so
    transient failures don't abort the resolve).
    """
    try:
        base_url, api_key = _get_settings()
    except Exception:  # pylint: disable=broad-except
        xbmc.log("NZB-DAV: Failed to read settings for job history", xbmc.LOGDEBUG)
        return None

    params = {
        "mode": "history",
        "nzo_ids": nzo_id,
        "apikey": api_key,
        "output": "json",
    }
    url = "{}/api?{}".format(base_url, urlencode(params))

    try:
        response_text = _http_get(url, timeout=10)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Job history request failed for nzo_id={}: {}".format(nzo_id, e),
            xbmc.LOGDEBUG,
        )
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
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Settings read failed in find_completed_by_name: {}".format(e),
            xbmc.LOGDEBUG,
        )
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
        response_text = _http_get(url, timeout=10)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: History search request failed for '{}': {}".format(name, e),
            xbmc.LOGDEBUG,
        )
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
            response_text = _http_get(url, timeout=10)
            response = _coerce_response_dict(json.loads(response_text))
        except Exception as e:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: History fallback request failed for '{}': {}".format(name, e),
                xbmc.LOGDEBUG,
            )
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


def find_queued_by_name(name):
    """Search nzbdav's active queue for a job matching ``name``.

    Used by the resolver's submit path to recover from a client-side
    submit timeout: when Kodi times out on ``/api?mode=addurl`` but
    nzbdav actually accepted and started processing the submit,
    re-submitting would either bounce as a duplicate or orphan the
    in-progress job with a second ``nzo_id``. Polling the queue for
    the same ``nzbname`` lets the resolver adopt the existing job
    instead.

    Args:
        name: The nzb name (matches the ``nzbname`` parameter passed
            to ``submit_nzb``). nzbdav echoes this verbatim in queue
            and history slots.

    Returns:
        Dict with ``nzo_id``, ``name``, ``status`` on a match, else
        ``None``. ``None`` also covers every error path: missing
        settings, network failure, malformed response. The caller
        should treat ``None`` as "not yet in the queue, keep waiting
        or retry the submit".

    Side effects:
        One HTTP GET to nzbdav /api?mode=queue with a short timeout
        (10 s — this is a recovery-path probe, not the main submit).
        No retries; the resolver calls this in a short loop after a
        submit timeout and handles its own pacing.
    """
    try:
        base_url, api_key = _get_settings()
    except Exception:  # pylint: disable=broad-except
        return None

    params = {
        "mode": "queue",
        "apikey": api_key,
        "output": "json",
        "limit": 200,
    }
    url = "{}/api?{}".format(base_url, urlencode(params))

    try:
        response_text = _http_get(url, timeout=10)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: find_queued_by_name request failed: {}".format(e),
            xbmc.LOGWARNING,
        )
        return None

    slots = response.get("queue", {}).get("slots", [])
    for slot in slots:
        if slot.get("filename") == name or slot.get("nzo_id_name") == name:
            xbmc.log(
                "NZB-DAV: Found '{}' already in nzbdav queue with "
                "nzo_id={}".format(name, slot.get("nzo_id")),
                xbmc.LOGINFO,
            )
            return {
                "nzo_id": slot.get("nzo_id", ""),
                "name": name,
                "status": slot.get("status", ""),
            }
    # Some nzbdav builds report the user-supplied nzbname under "filename"
    # only after the fetch/parse phase finishes, so a freshly-submitted job
    # may appear under a different slot key during the first few seconds.
    # Fall back to a broader scan across any string-valued slot field.
    for slot in slots:
        for key in ("filename", "nzo_id_name", "name"):
            if slot.get(key) == name:
                xbmc.log(
                    "NZB-DAV: Found '{}' in queue via {} (nzo_id={})".format(
                        name, key, slot.get("nzo_id")
                    ),
                    xbmc.LOGINFO,
                )
                return {
                    "nzo_id": slot.get("nzo_id", ""),
                    "name": name,
                    "status": slot.get("status", ""),
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
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Settings read failed in get_completed_names: {}".format(e),
            xbmc.LOGDEBUG,
        )
        return set()

    params = {
        "mode": "history",
        "apikey": api_key,
        "output": "json",
        "limit": 500,
    }
    url = "{}/api?{}".format(base_url, urlencode(params))

    try:
        response_text = _http_get(url, timeout=10)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: get_completed_names request failed: {}".format(e),
            xbmc.LOGDEBUG,
        )
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
    """Poll the nzbdav queue for an in-flight NZB's current status.

    Args:
        nzo_id: SABnzbd-compatible job identifier returned by submit_nzb.

    Returns:
        A dict with keys ``status`` (e.g. "Queued", "Downloading",
        "Fetching NZB", "Failed"), ``percentage`` (string, 0-100), and
        ``filename`` when the slot is known, or ``None`` on any network
        / parse / settings failure. The resolver's poll loop treats None
        as "no data this tick" and re-polls, so transient failures do
        not abort the resolve.
    """
    try:
        base_url, api_key = _get_settings()
    except Exception as e:  # pylint: disable=broad-except
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
        response_text = _http_get(url, timeout=10)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        # ``Exception`` intentionally — the prior ``(URLError, json.JSONDecodeError,
        # Exception)`` tuple was dead code (Exception subsumes the first two).
        # Resolver polls this every second while a download is active; any
        # crash here would kill the poll loop, so we log and return None
        # so the caller treats the tick as "no data, try again".
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
