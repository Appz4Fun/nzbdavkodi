# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Pure response-parsing and shape helpers for the nzbdav API client.

These functions carry no I/O and no settings access: they only normalize
and reshape the JSON SABnzbd-compatible API returns. They are split out of
``nzbdav_api`` to keep that module under the file-size budget. Every public
name here is re-exported from ``resources.lib.nzbdav_api`` so existing
``resources.lib.nzbdav_api.<name>`` callers and tests keep resolving.
"""

import re
import socket

import xbmc

from resources.lib.http_util import redact_text as _redact_text

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


def _response_slots(response, section_name):
    """Return SABnzbd ``slots`` from a response section, tolerating bad shapes."""
    section = response.get(section_name, {})
    if not isinstance(section, dict):
        return []
    slots = section.get("slots", [])
    if not isinstance(slots, list):
        return []
    return [slot for slot in slots if isinstance(slot, dict)]


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
    # ``isinstance(None, socket.timeout)`` is False, so a missing/``None``
    # ``reason`` falls through without a separate guard.
    return isinstance(getattr(exc, "reason", None), socket.timeout)


def _unique_names(names):
    """Return non-empty names in first-seen order."""
    unique = []
    seen = set()
    for name in names or []:
        if not isinstance(name, str) or not name:
            continue
        if name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique


def _history_search_term(name):
    """Return the same SABnzbd history search term as find_completed_by_name."""
    return name.split(".")[0] if "." in name else name


def _unique_search_terms(names):
    """Return non-empty SABnzbd search terms for names, in first-seen order."""
    terms = []
    seen = set()
    for name in names:
        term = _history_search_term(name)
        if term and term not in seen:
            terms.append(term)
            seen.add(term)
    return terms


def _completed_job_from_slot(slot):
    """Return the public completed-job shape from a SABnzbd history slot."""
    return {
        "status": slot.get("status", ""),
        "storage": slot.get("storage", ""),
        "name": slot.get("name", ""),
        "nzo_id": slot.get("nzo_id", ""),
        # Downloaded byte size. nzbdav history is keyed by NAME, so the picker's
        # DL/cache match disambiguates same-filename collisions (different
        # release/resolution, or a repost at a different retention) by comparing
        # this against the indexer result's advertised size (_tag_available).
        "bytes": slot.get("bytes"),
        "fail_message": slot.get("fail_message", ""),
        # SABnzbd-compatible: epoch-seconds timestamp the job moved into
        # history. nzbdav-rs reports unix epoch directly. Used by the
        # resolver's by-name fallback to suppress stale-prior-attempt
        # false positives on resubmit.
        "completed": slot.get("completed"),
    }


def _slot_completed_sort_key(slot):
    try:
        return int(slot.get("completed"))
    except (TypeError, ValueError):
        return -1


def _terminal_slot_sort_key(slot):
    """Sort terminal history rows, preferring rows with usable timestamps."""
    return _slot_completed_sort_key(slot)


def _record_completed_name_matches(slots, target_names, found):
    """Copy exact completed name matches from history slots into found."""
    remaining = target_names.difference(found)
    if not remaining:
        return
    for slot in slots:
        name = slot.get("name")
        if name in remaining and slot.get("status") == "Completed":
            found[name] = _completed_job_from_slot(slot)
            xbmc.log(
                "NZB-DAV: Found existing download '{}' in history".format(name),
                xbmc.LOGINFO,
            )


def _record_queued_matches(slots, key, target_names, found):
    """Record queue slots whose ``key`` field is a target name into found."""
    for slot in slots:
        name = slot.get(key)
        if name not in target_names or name in found:
            continue
        xbmc.log(
            "NZB-DAV: Found '{}' in queue via {} (nzo_id={})".format(
                name, key, slot.get("nzo_id")
            ),
            xbmc.LOGINFO,
        )
        found[name] = {
            "nzo_id": slot.get("nzo_id", ""),
            "name": name,
            "status": slot.get("status", ""),
        }


class _CompletedJobs(dict):
    """Completed-history mapping plus whether the history lookup succeeded."""

    def __init__(self, *args, **kwargs):
        lookup_done = kwargs.pop("lookup_done", False)
        super().__init__(*args, **kwargs)
        self._lookup_done = bool(lookup_done)


def completed_jobs_lookup_done(completed_jobs):
    """Return whether a completed-jobs mapping came from a successful lookup."""
    return getattr(completed_jobs, "_lookup_done", False) is True


def _completed_jobs_from_slots(slots):
    """Build the name->metadata map of Completed history slots."""
    jobs = _CompletedJobs(lookup_done=True)
    for slot in slots:
        if slot.get("status") == "Completed" and slot.get("name"):
            jobs[slot["name"]] = _completed_job_from_slot(slot)
    return jobs


def _job_status_from_slots(slots, nzo_id):
    """Return the status dict for nzo_id within queue slots, or None."""
    for slot in slots:
        if slot.get("nzo_id") != nzo_id:
            continue
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


def _submit_http_error_result(e):
    """Build submit_nzb's (None, error) result from an HTTPError.

    nzbdav returned a structured HTTP error (e.g. 500 on duplicate submit,
    502/503/504 from upstream issues). Capture the body so the caller can
    either surface it or classify retries based on status code. Redact
    apikey-style tokens: nzbdav's error pages sometimes echo the failing URL
    (which carried the indexer's apikey) back to the client, which then goes
    into a Kodi dialog visible to anyone reading the screen / logs.
    """
    body = ""
    try:
        body = e.read().decode("utf-8", errors="replace")
    except Exception:  # pylint: disable=broad-except
        pass
    body = _redact_text(_sanitize_server_message(body))[:500]
    xbmc.log(
        "NZB-DAV: Submit NZB got HTTP {} from nzbdav: {}".format(e.code, body),
        xbmc.LOGERROR,
    )
    return None, {"status": e.code, "message": body}


def _submit_request_error_result(e, timeout, nzb_name):
    """Build submit_nzb's (None, error) result from a generic request error.

    Distinguishes a client-side timeout (where nzbdav may have accepted the
    submit anyway, so the caller should probe queue/history before retrying)
    from any other failure (caller may retry).
    """
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
    xbmc.log(
        "NZB-DAV: Submit NZB request failed: {}".format(_redact_text(str(e))),
        xbmc.LOGERROR,
    )
    return None, None


def _submit_parse_result(response):
    """Build submit_nzb's result from a successfully-parsed response dict.

    Returns (nzo_id, None) on success, or (None, {"status": "rejected", ...})
    when nzbdav saw the request but rejected the NZB (a 200 with status=false,
    e.g. empty / truncated / password-only NZB), which is NOT retryable.
    """
    nzo_ids = response.get("nzo_ids")
    if response.get("status") and isinstance(nzo_ids, list) and nzo_ids and nzo_ids[0]:
        nzo_id = nzo_ids[0]
        xbmc.log(
            "NZB-DAV: NZB submitted successfully, nzo_id={}".format(nzo_id),
            xbmc.LOGINFO,
        )
        return nzo_id, None
    # ``response`` is always a dict here (``_coerce_response_dict`` normalized
    # non-dict JSON to ``{}``), so ``.get`` is safe.
    error_msg = response.get("error")
    # Redact: nzbdav can echo back the failing indexer URL (with apikey)
    # inside its rejection payload (e.g. "Failed to fetch <url>"), which
    # would otherwise land in the Kodi log.
    xbmc.log(
        "NZB-DAV: Submit NZB rejected by nzbdav: {}".format(
            _redact_text(str(response))
        ),
        xbmc.LOGERROR,
    )
    return None, {
        "status": "rejected",
        "message": (
            _redact_text(str(error_msg)) if error_msg else "nzbdav rejected the NZB"
        ),
    }


def _cancel_job_outcome(response, nzo_id):
    """Return whether nzbdav reported the queue DELETE succeeded."""
    # Truthy match (not `is True` identity) — submit_nzb's success branch
    # uses the same loose check, and at least one nzbdav build returns
    # status="ok" (string) instead of the documented JSON `true`. Closes
    # the §H.3 cancel/submit-asymmetric finding.
    if response.get("status"):
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
