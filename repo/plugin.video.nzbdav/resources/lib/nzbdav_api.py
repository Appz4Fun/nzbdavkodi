# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""nzbdav SABnzbd-compatible API client."""

import json
from urllib.error import HTTPError
from urllib.parse import urlencode

import xbmc
import xbmcaddon  # noqa: F401  pylint: disable=unused-import  (module-scope Kodi imports are the repo convention; tests patch <mod>.xbmcaddon and the thread-safety contract tests forbid its use on no-getter paths)

from resources.lib.exact_job import ExactJobLookup
from resources.lib.http_util import http_get as _http_get
from resources.lib.http_util import http_get_bytes as _http_get_bytes
from resources.lib.http_util import http_post as _http_post
from resources.lib.http_util import redact_text as _redact_text
from resources.lib.nzbdav_api_parsing import (
    _cancel_job_outcome,
    _clamp_int_setting,
    _coerce_response_dict,
    _completed_job_from_slot,
    _completed_jobs_from_slots,
    _CompletedJobs,
    _history_search_term,
    _is_timeout_error,
    _job_status_from_slots,
    _record_completed_name_matches,
    _record_queued_matches,
    _response_slots,
    _sanitize_server_message,
    _slot_completed_sort_key,
    _submit_http_error_result,
    _submit_parse_result,
    _submit_request_error_result,
    _terminal_slot_sort_key,
    _unique_names,
    _unique_search_terms,
    completed_jobs_lookup_done,
)

# Re-exported for backwards compatibility / static analysis: these names are
# imported above purely so ``resources.lib.nzbdav_api.<name>`` keeps resolving
# for existing callers and tests after the parsing split.
__all__ = [
    "_cancel_job_outcome",
    "_clamp_int_setting",
    "_completed_job_from_slot",
    "_completed_jobs_from_slots",
    "_CompletedJobs",
    "_coerce_response_dict",
    "_is_timeout_error",
    "_job_status_from_slots",
    "_record_completed_name_matches",
    "_record_queued_matches",
    "_response_slots",
    "_sanitize_server_message",
    "_slot_completed_sort_key",
    "_submit_http_error_result",
    "_submit_parse_result",
    "_submit_request_error_result",
    "_terminal_slot_sort_key",
    "_unique_names",
    "_history_search_term",
    "_unique_search_terms",
    "completed_jobs_lookup_done",
]

# nzbdav's submit handlers (addfile upload, addurl server-side fetch)
# parse the NZB XML and enumerate segments before returning. On a big
# REMUX this can routinely exceed 30 s. 300 s gives nzbdav real headroom
# while remaining below the 10 minute clamp for truly stuck requests.
_DEFAULT_SUBMIT_TIMEOUT = 300
# Status/history queries should be fast; 10s timeout prevents dialog freeze
# on slow/unresponsive SABnzbd. Polling loop retries every 1-2s anyway.
_API_READ_TIMEOUT = 10


def _get_settings(settings_getter=None):
    """Read nzbdav connection settings.

    With no getter, read settings.xml from disk instead of the
    ``xbmcaddon`` binding: submit/status calls run on resolver worker
    and fallback-prewarm threads, and concurrent binding reads race
    Kodi's lazy settings load (TinyXML SIGSEGV, gdb-confirmed).
    """
    if settings_getter is None:
        from resources.lib.router import _get_script_setting as settings_getter
    url = settings_getter("nzbdav_url", "").strip().rstrip("/")
    api_key = settings_getter("nzbdav_api_key", "")
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


def _get_submit_timeout(settings_getter=None):
    """Read the configurable submit timeout from settings, default 300s.

    Clamped to [_SUBMIT_TIMEOUT_MIN, _SUBMIT_TIMEOUT_MAX] so a typo
    in the Kodi settings UI can't produce a 83-hour timeout."""
    try:
        if settings_getter is None:
            # Disk read, not the xbmcaddon binding (see _get_settings).
            from resources.lib.router import _get_script_setting as settings_getter
        raw = settings_getter("submit_timeout", "")
        value = int(raw) if raw else _DEFAULT_SUBMIT_TIMEOUT
    except (ValueError, TypeError):
        return _DEFAULT_SUBMIT_TIMEOUT
    return _clamp_int_setting(value, _SUBMIT_TIMEOUT_MIN, _SUBMIT_TIMEOUT_MAX)


# NZB fetches from the indexer are quick (a few MB of XML at most), so
# they get a flat timeout independent of the submit timeout. The size cap
# bounds memory if an indexer misbehaves: even a 100 GB release's NZB is
# tens of MB of XML, so 128 MiB is generous headroom; anything larger
# falls back to the server-side addurl fetch (the server owns its own
# limits there).
_NZB_FETCH_TIMEOUT = 30
_NZB_FETCH_MAX_BYTES = 128 * 1024 * 1024


def _looks_like_nzb_document(body):
    """Return whether ``body`` parses as XML with an <nzb> document root.

    A plain "starts with <" sniff accepted HTML error pages served with
    HTTP 200; parse and check the root's local name instead (the newznab
    namespace makes the tag '{...}nzb').
    """
    from resources.lib.xml_safety import ParseError, safe_fromstring

    try:
        root = safe_fromstring(body)
    except (ParseError, ValueError):
        return False
    tag = root.tag if isinstance(root.tag, str) else ""
    return tag.rsplit("}", 1)[-1].lower() == "nzb"


def _fetch_nzb_body(nzb_url, nzb_name):
    """Fetch the NZB bytes from the indexer for an addfile submit.

    Returns the RAW body bytes (no decode: a lossy UTF-8 round-trip
    would corrupt non-UTF-8 NZBs before upload), or ``None`` on any
    failure — network error, oversized response, or a body that isn't
    an actual NZB document (e.g. an indexer HTML error page served with
    HTTP 200). ``None`` makes ``submit_nzb`` fall back to the
    server-side ``addurl`` fetch, preserving the old behavior.
    """
    if not nzb_url:
        return None
    try:
        body = _http_get_bytes(
            nzb_url, timeout=_NZB_FETCH_TIMEOUT, max_bytes=_NZB_FETCH_MAX_BYTES
        )
    except Exception as exc:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Local NZB fetch failed for '{}' ({}); falling back to "
            "server-side addurl".format(nzb_name, _redact_text(str(exc))),
            xbmc.LOGWARNING,
        )
        return None
    if body[:2] == b"\x1f\x8b":
        # Hydra serves NZBs gzip-or-plain; urllib doesn't negotiate
        # encodings, so transparently decompress a gzip body before
        # validating — otherwise every gzipped NZB would needlessly fall
        # back to the server-side addurl fetch.
        import gzip

        try:
            body = gzip.decompress(body)
        except OSError:
            body = b""
    if not body or not _looks_like_nzb_document(body):
        xbmc.log(
            "NZB-DAV: Local NZB fetch for '{}' returned non-NZB content; "
            "falling back to server-side addurl".format(nzb_name),
            xbmc.LOGWARNING,
        )
        return None
    return body


def _dump_submitted_nzb(nzb_name, body):
    """Save the NZB body that's about to be submitted to nzbdav.

    No-op unless ``NZBDAV_DUMP_NZBS_DIR`` is set in the environment. Used
    by the extreme functional test to inspect the bytes that went into
    nzbdav's deobfuscator when a job fails with "no importable video
    file found", so we can tell whether the NZB itself is malformed (or
    just unsupported by nzbdav).
    """
    import os

    dump_dir = os.environ.get("NZBDAV_DUMP_NZBS_DIR", "").strip()
    if not dump_dir or not body:
        return
    try:
        os.makedirs(dump_dir, exist_ok=True)
        safe_name = (nzb_name or "submission").replace("/", "_")[:200]
        out_path = os.path.join(dump_dir, "{}.nzb".format(safe_name))
        with open(out_path, "wb") as fh:
            fh.write(body)
        xbmc.log(
            "NZB-DAV: Dumped submitted NZB '{}' to {}".format(nzb_name, out_path),
            xbmc.LOGINFO,
        )
    except Exception as exc:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Failed to dump submitted NZB '{}': {}".format(
                nzb_name, _redact_text(str(exc))
            ),
            xbmc.LOGWARNING,
        )


def _build_submit_request(
    base_url, api_key, nzb_url, nzb_name, settings_getter, submit_timeout
):
    """Return the (url, timeout) for an addurl submit and log the request."""
    params = {
        "mode": "addurl",
        "name": nzb_url,
        "nzbname": nzb_name,
        "apikey": api_key,
        "output": "json",
    }
    url = "{}/api?{}".format(base_url, urlencode(params))
    from resources.lib.http_util import redact_url

    timeout = (
        submit_timeout
        if submit_timeout is not None
        else _get_submit_timeout(settings_getter=settings_getter)
    )
    xbmc.log(
        "NZB-DAV: Submit NZB URL (timeout={}s): {}".format(timeout, redact_url(url)),
        xbmc.LOGDEBUG,
    )
    return url, timeout


def _build_addfile_request(
    base_url, api_key, nzb_name, settings_getter, submit_timeout
):
    """Return the (url, timeout) for an addfile submit and log the request."""
    params = {
        "mode": "addfile",
        "nzbname": nzb_name,
        "apikey": api_key,
        "output": "json",
    }
    url = "{}/api?{}".format(base_url, urlencode(params))
    from resources.lib.http_util import redact_url

    timeout = (
        submit_timeout
        if submit_timeout is not None
        else _get_submit_timeout(settings_getter=settings_getter)
    )
    xbmc.log(
        "NZB-DAV: Submit NZB via addfile (timeout={}s): {}".format(
            timeout, redact_url(url)
        ),
        xbmc.LOGDEBUG,
    )
    return url, timeout


def _multipart_nzb_body(nzb_name, body):
    """Encode ``body`` as a multipart/form-data upload for mode=addfile.

    Returns ``(encoded_body, content_type)``. The file goes in the
    ``name`` field — the SABnzbd-documented one — which nzbdav (C#,
    ``Form.Files["nzbFile"] ?? Form.Files["name"]``), nzbdav-rs, and
    SAB-compatible endpoints all accept.
    """
    import uuid

    filename = (nzb_name or "download").replace('"', "_").replace("\\", "_")
    # Header-injection guard: a CR/LF in an indexer-supplied title would
    # break the part header or smuggle extra headers into the form-data.
    filename = filename.replace("\r", " ").replace("\n", " ")
    if not filename.lower().endswith(".nzb"):
        filename += ".nzb"
    boundary = uuid.uuid4().hex
    while boundary.encode("ascii") in body:
        boundary = uuid.uuid4().hex
    head = (
        (
            "--{b}\r\n"
            'Content-Disposition: form-data; name="name"; filename="{fn}"\r\n'
            "Content-Type: application/x-nzb\r\n"
            "\r\n"
        )
        .format(b=boundary, fn=filename)
        .encode("utf-8")
    )
    tail = "\r\n--{b}--\r\n".format(b=boundary).encode("ascii")
    content_type = "multipart/form-data; boundary={}".format(boundary)
    return head + body + tail, content_type


def submit_nzb(nzb_url, nzb_name="", settings_getter=None, submit_timeout=None):
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
        Reads nzbdav settings from Kodi via xbmcaddon.Addon("plugin.video.nzbdav").
        Fetches the NZB from the indexer and POSTs it to nzbdav /api with
        mode=addfile; falls back to a GET with mode=addurl when the local
        fetch fails or the addfile upload is refused with a whole-request
        4xx (nzbdav then fetches the URL server-side — newer
        nzbdav builds refuse this for private-IP indexer hosts, which is
        why addfile is the primary path).
        Logs submission URLs, successes, and errors to the Kodi log.
    """
    try:
        base_url, api_key = _get_settings(settings_getter=settings_getter)
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Failed to read nzbdav settings: {}".format(_redact_text(str(e))),
            xbmc.LOGERROR,
        )
        return None, None
    nzb_body = _fetch_nzb_body(nzb_url, nzb_name)
    # Optional NZB dump for the extreme functional test: when
    # NZBDAV_DUMP_NZBS_DIR is set, write the NZB body the addon is about
    # to submit to disk. Lets the test post-mortem inspect the exact
    # bytes that went into nzbdav's deobfuscator (e.g. for "no importable
    # video file found" failures).
    _dump_submitted_nzb(nzb_name, nzb_body)
    if nzb_body is not None:
        url, timeout = _build_addfile_request(
            base_url, api_key, nzb_name, settings_getter, submit_timeout
        )
        nzo_id, error = _execute_submit(url, timeout, nzb_name, nzb_body=nzb_body)
        if not _addfile_refused(error):
            return nzo_id, error
        # A whole-request 4xx means the endpoint refused the multipart
        # upload shape (older SAB-compatible servers without addfile) —
        # the legacy server-side addurl fetch may still work. Timeouts
        # and 5xx/status:false are NOT retried here: the server may have
        # accepted the job, and the caller's queue-adoption probes own
        # that ambiguity — a second submit would risk duplicates.
        xbmc.log(
            "NZB-DAV: addfile submit refused for '{}' (HTTP {}); retrying "
            "via server-side addurl".format(nzb_name, error.get("status")),
            xbmc.LOGWARNING,
        )
    url, timeout = _build_submit_request(
        base_url, api_key, nzb_url, nzb_name, settings_getter, submit_timeout
    )
    return _execute_submit(url, timeout, nzb_name)


# 4xx statuses that mean "this endpoint doesn't speak multipart addfile"
# (missing mode / method / media type / malformed-multipart handling).
# Auth (401/403/407), conflict (409), validation (422), and rate limiting
# (429) are about THIS submission, not the transport — retrying them via
# addurl would just repeat the same rejection or double-submit.
_ADDFILE_INCOMPATIBLE_STATUSES = frozenset({400, 404, 405, 406, 415, 501})


def _addfile_refused(error):
    """Whether an addfile error indicates the endpoint lacks addfile."""
    return (
        isinstance(error, dict)
        and error.get("status") in _ADDFILE_INCOMPATIBLE_STATUSES
    )


def _execute_submit(url, timeout, nzb_name, nzb_body=None):
    """Perform the submit request and classify the (nzo_id, error) outcome.

    With ``nzb_body`` this is a multipart addfile POST; without it, the
    legacy addurl GET. Both return the same SABnzbd-style JSON, so the
    classification below is shared.
    """
    try:
        if nzb_body is not None:
            body, content_type = _multipart_nzb_body(nzb_name, nzb_body)
            response_text = _http_post(
                url, body, timeout=timeout, headers={"Content-Type": content_type}
            )
        else:
            response_text = _http_get(url, timeout=timeout)
        response = _coerce_response_dict(json.loads(response_text))
    except HTTPError as e:
        return _submit_http_error_result(e)
    except Exception as e:  # pylint: disable=broad-except
        # ``Exception`` intentionally — the prior ``(socket.timeout, URLError,
        # json.JSONDecodeError, Exception)`` tuple made the three named
        # classes dead code because ``Exception`` is their base. This path
        # is the last-chance safety net for an nzbdav submit, so catching
        # the full family (including things we haven't anticipated) keeps
        # the resolver from crashing while still letting caller-level
        # queue/history probes retry.
        return _submit_request_error_result(e, timeout, nzb_name)
    return _submit_parse_result(response)


def _build_cancel_url(base_url, api_key, nzo_id, timeout):
    """Build the queue-delete URL for cancel_job and log the request."""
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
    return url


def cancel_job(nzo_id, timeout=30, settings_getter=None):
    """Cancel an in-flight nzbdav job by removing it from the queue.

    Issues a single SABnzbd-compatible queue DELETE
    (mode=queue&name=delete&value=<nzo_id>). This is "cancel" semantics,
    not "delete everywhere" — completed and failed jobs that have already
    moved to nzbdav's history are deliberately left intact so the user
    can still inspect failure history in nzbdav's web UI.

    Args:
        nzo_id: The nzbdav job identifier to cancel.
        timeout: HTTP timeout in seconds. Defaults to 30 for slower
            nzbdav queue cleanup on loaded boxes.
        settings_getter: Optional callable for reading addon settings.
            Required from script-mode contexts (e.g. TMDBHelper's
            tmdb_play hook) where ``xbmcaddon.Addon()`` would SIGSEGV
            because the GUI dispatcher hasn't been initialized.

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
        base_url, api_key = _get_settings(settings_getter=settings_getter)
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: cancel_job failed to read settings: {}".format(
                _redact_text(str(e))
            ),
            xbmc.LOGERROR,
        )
        return False

    url = _build_cancel_url(base_url, api_key, nzo_id, timeout)
    response = _fetch_cancel_response(url, nzo_id, timeout)
    if response is None:
        return False
    return _cancel_job_outcome(response, nzo_id)


def _fetch_cancel_response(url, nzo_id, timeout):
    """GET the cancel URL, returning the parsed dict or None on failure."""
    try:
        response_text = _http_get(url, timeout=timeout)
        return _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        # cancel_job is a "make the mess go away" path — anything that
        # prevents the cancel from reaching nzbdav should just get logged
        # and swallowed so the caller doesn't cascade into error dialogs.
        xbmc.log(
            "NZB-DAV: cancel_job network error for nzo_id={}: {}".format(
                nzo_id, _redact_text(str(e))
            ),
            xbmc.LOGWARNING,
        )
        return None


def get_queue_slots(settings_getter=None, timeout=15):
    """Return the nzbdav download-queue slots (SABnzbd ``mode=queue``).

    Read-only: one HTTP GET to ``/api?mode=queue``. Each slot is a dict with
    at least ``nzo_id`` and ``status`` (and usually ``filename``/``name`` and
    ``percentage``). The active download is the head slot (status
    ``Downloading``); waiting jobs follow. Returns an empty list on any error
    or when the queue is empty, so callers can treat it as "nothing to clear".
    """
    try:
        base_url, api_key = _get_settings(settings_getter=settings_getter)
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: get_queue_slots failed to read settings: {}".format(
                _redact_text(str(e))
            ),
            xbmc.LOGERROR,
        )
        return []
    # SAB/nzbdav paginate the queue by start/limit; without a limit a small
    # default page would hide later jobs from a "clear the whole queue". Mirror
    # find_queued_by_names's limit=200 so the probe sees the full queue.
    params = {"mode": "queue", "apikey": api_key, "output": "json", "limit": 200}
    url = "{}/api?{}".format(base_url, urlencode(params))
    try:
        response = _coerce_response_dict(json.loads(_http_get(url, timeout=timeout)))
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: get_queue_slots network error: {}".format(_redact_text(str(e))),
            xbmc.LOGWARNING,
        )
        return []
    return _response_slots(response, "queue")


def clear_queue(settings_getter=None, slots=None, timeout=None):
    """Cancel every job in the nzbdav queue and return the count cancelled.

    Removes BOTH the actively-downloading job (the queue head) and any waiting
    jobs by issuing one queue DELETE per ``nzo_id`` through ``cancel_job``.
    History (completed/failed) is left intact, matching ``cancel_job``'s
    queue-only semantics. Best-effort — a slot that fails to cancel is logged
    by ``cancel_job`` and skipped.

    ``slots`` lets the caller pass the exact queue listing it already probed
    (e.g. the one shown to the user): those jobs are cancelled rather than
    re-fetching, so a job that appeared between the probe and the clear is
    never cancelled unseen. When ``slots`` is None the current queue is
    fetched.

    ``timeout`` bounds EACH per-slot DELETE (passed to ``cancel_job``). The
    pre-submit clear path runs synchronously before the playback UI pump, so a
    short timeout keeps a stalled nzbdav from freezing the resolver for minutes
    across several deletes. When None, ``cancel_job``'s default is used.
    """
    if slots is None:
        slots = get_queue_slots(settings_getter=settings_getter)
    cancel_kwargs = {"settings_getter": settings_getter}
    if timeout is not None:
        cancel_kwargs["timeout"] = timeout
    cleared = 0
    for slot in slots:
        nzo_id = slot.get("nzo_id") if isinstance(slot, dict) else None
        if not nzo_id:
            continue
        if cancel_job(nzo_id, **cancel_kwargs):
            cleared += 1
    if cleared:
        xbmc.log(
            "NZB-DAV: clear_queue cancelled {} queued job(s)".format(cleared),
            xbmc.LOGINFO,
        )
    return cleared


def get_job_history(nzo_id, settings_getter=None):
    """Check if a job has landed in nzbdav's history.

    Returns dict with keys ``status``, ``storage``, ``name``, ``nzo_id``,
    ``fail_message`` when the nzo_id is found, or ``None`` when it
    hasn't appeared yet (or on any network / settings / parse error —
    the resolver's poll loop treats None as "keep polling", so
    transient failures don't abort the resolve).
    """
    try:
        base_url, api_key = _get_settings(settings_getter=settings_getter)
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
        response_text = _http_get(url, timeout=_API_READ_TIMEOUT)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Job history request failed for nzo_id={}: {}".format(
                nzo_id, _redact_text(str(e))
            ),
            xbmc.LOGDEBUG,
        )
        return None

    slots = _response_slots(response, "history")
    for slot in slots:
        if slot.get("nzo_id") == nzo_id:
            return {
                "status": slot.get("status", ""),
                "storage": slot.get("storage", ""),
                "name": slot.get("name", ""),
                "nzo_id": slot.get("nzo_id", ""),
                "fail_message": slot.get("fail_message", ""),
            }
    return None


def _fetch_exact_history(nzo_id, settings_getter):
    """Fetch and decode the exact-history response, or return ``None``."""
    try:
        base_url, api_key = _get_settings(settings_getter=settings_getter)
        params = {
            "mode": "history",
            "nzo_ids": nzo_id,
            "apikey": api_key,
            "output": "json",
        }
        url = "{}/api?{}".format(base_url, urlencode(params))
        decoded = json.loads(_http_get(url, timeout=_API_READ_TIMEOUT))
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: exact history lookup failed for nzo_id={}: {}".format(
                nzo_id, _redact_text(str(error))
            ),
            xbmc.LOGDEBUG,
        )
        return None
    return decoded


def _exact_history_slots(decoded):
    """Return a validated nzbdav history-slot list, or ``None``."""
    if not isinstance(decoded, dict):
        return None
    history = decoded.get("history")
    if not isinstance(history, dict):
        return None
    slots = history.get("slots")
    return slots if isinstance(slots, list) else None


def _exact_slot_identifier(slot):
    """Return a non-empty slot identifier, or ``None`` when malformed."""
    if not isinstance(slot, dict):
        return None
    identifier = slot.get("nzo_id")
    return identifier if identifier not in (None, "") else None


def _matched_exact_slot_result(slot):
    """Classify one nzbdav history slot already matched by identifier."""
    status = slot.get("status")
    if not isinstance(status, str) or not status:
        return ExactJobLookup.transient()
    if status != "Completed":
        return ExactJobLookup.stale()
    storage = slot.get("storage")
    if not isinstance(storage, str) or not storage:
        return ExactJobLookup.transient()
    return ExactJobLookup.valid(_completed_job_from_slot(slot))


def _exact_history_result(slots, nzo_id):
    """Classify validated history slots for one exact nzbdav identifier."""
    target = str(nzo_id)
    malformed = False
    for slot in slots:
        identifier = _exact_slot_identifier(slot)
        if identifier is None:
            malformed = True
            continue
        if str(identifier) == target:
            return _matched_exact_slot_result(slot)
    return ExactJobLookup.transient() if malformed else ExactJobLookup.stale()


def lookup_completed_job_exact(nzo_id, settings_getter=None):
    """Tri-state lookup of one exact nzbdav completed-history identifier.

    Unlike :func:`get_job_history`, this keeps a successful empty lookup apart
    from a network, auth, JSON, or response-shape failure. That distinction is
    required before deleting a persistent season-pack record.
    """
    decoded = _fetch_exact_history(nzo_id, settings_getter)
    slots = _exact_history_slots(decoded)
    if slots is None:
        return ExactJobLookup.transient()
    return _exact_history_result(slots, nzo_id)


def completed_by_id(nzo_id, settings_getter=None):
    """Return the shared exact-job lookup contract for one nzbdav ID."""
    return lookup_completed_job_exact(nzo_id, settings_getter=settings_getter)


def find_completed_by_name(name, settings_getter=None):
    """Search nzbdav history for a completed download matching the given name.

    Uses the SABnzbd search parameter to narrow results, then matches by name.
    Falls back to checking the full history if search returns nothing.

    Returns dict with: status, storage, name, nzo_id. None if not found.
    """
    return find_completed_by_names([name], settings_getter=settings_getter).get(name)


def find_terminal_by_name(name, settings_getter=None):
    """Return the most recent terminal (Completed or Failed) history slot by name.

    Used by the resolver's poll loop to detect that nzbdav-rs has finished
    a job — pass or fail — when its history slot carries a different
    nzo_id than the addon submitted (the queue→history nzo_id remap).
    Distinct from ``find_completed_by_name`` which intentionally matches
    only ``Completed`` rows so callers looking for a ready-to-play stream
    don't pick up a failure.

    Returns dict with: status, storage, name, nzo_id, fail_message. None
    if no matching history row is found.
    """
    if not name:
        return None
    try:
        base_url, api_key = _get_settings(settings_getter=settings_getter)
    except Exception:  # pylint: disable=broad-except
        return None
    params = {
        "mode": "history",
        "apikey": api_key,
        "output": "json",
        "limit": 200,
        "search": _history_search_term(name),
    }
    slots = _history_slots(base_url, params, "terminal lookup for '{}'".format(name))
    matches = [
        slot
        for slot in slots
        if slot.get("name") == name and slot.get("status") in ("Completed", "Failed")
    ]
    if not matches:
        return None
    return _completed_job_from_slot(max(matches, key=_terminal_slot_sort_key))


def _history_slots(base_url, params, log_context):
    """Fetch nzbdav history slots for one parameter set."""
    url = "{}/api?{}".format(base_url, urlencode(params))
    try:
        response_text = _http_get(url, timeout=_API_READ_TIMEOUT)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: History {} request failed: {}".format(
                log_context, _redact_text(str(e))
            ),
            xbmc.LOGDEBUG,
        )
        return []
    return _response_slots(response, "history")


def find_completed_by_names(names, settings_getter=None):
    """Search nzbdav history for completed downloads matching exact names.

    This is the batched equivalent of find_completed_by_name(): it groups
    identical SABnzbd search terms and performs the broad fallback search at
    most once for the remaining names.
    """
    unique_names = _unique_names(names)
    if not unique_names:
        return {}

    try:
        base_url, api_key = _get_settings(settings_getter=settings_getter)
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Settings read failed in find_completed_by_names: {}".format(
                _redact_text(str(e))
            ),
            xbmc.LOGDEBUG,
        )
        return {}

    target_names = set(unique_names)
    found = {}
    search_terms = _unique_search_terms(unique_names)

    base_params = {
        "mode": "history",
        "apikey": api_key,
        "output": "json",
        "limit": 200,
    }
    for search_term in search_terms:
        params = dict(base_params)
        params["search"] = search_term
        slots = _history_slots(
            base_url,
            params,
            "search for '{}'".format(search_term),
        )
        _record_completed_name_matches(slots, target_names, found)
        if len(found) == len(unique_names):
            return found

    # Fallback: broader search without search term filter
    if search_terms and len(found) < len(unique_names):
        slots = _history_slots(base_url, base_params, "fallback")
        _record_completed_name_matches(slots, target_names, found)
    return found


def find_queued_by_name(name, settings_getter=None):
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
        One HTTP GET to nzbdav /api?mode=queue with a bounded timeout
        (10 s — this is a recovery-path probe, not the main submit).
        No retries; the resolver calls this in a short loop after a
        submit timeout and handles its own pacing.
    """
    return find_queued_by_names([name], settings_getter=settings_getter).get(name)


def find_queued_by_names(names, settings_getter=None):
    """Search nzbdav's active queue for exact job-name matches."""
    unique_names = _unique_names(names)
    if not unique_names:
        return {}

    try:
        base_url, api_key = _get_settings(settings_getter=settings_getter)
    except Exception:  # pylint: disable=broad-except
        return {}

    params = {
        "mode": "queue",
        "apikey": api_key,
        "output": "json",
        "limit": 200,
    }
    url = "{}/api?{}".format(base_url, urlencode(params))

    try:
        response_text = _http_get(url, timeout=_API_READ_TIMEOUT)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: find_queued_by_names request failed: {}".format(
                _redact_text(str(e))
            ),
            xbmc.LOGWARNING,
        )
        return {}

    slots = _response_slots(response, "queue")
    target_names = set(unique_names)
    found = {}

    for key in ("filename", "nzo_id_name"):
        _record_queued_matches(slots, key, target_names, found)
        if len(found) == len(unique_names):
            return found

    # Some nzbdav builds report the user-supplied nzbname under "filename"
    # only after the fetch/parse phase finishes, so a freshly-submitted job
    # may appear under a different slot key during the first few seconds.
    # Fall back to the "name" slot key (the third and last key nzbdav uses
    # for the submitted name; see resolver_queueclear._queue_slot_is_title).
    _record_queued_matches(slots, "name", target_names, found)
    return found


def get_completed_jobs(settings_getter=None):
    """Fetch completed downloads from nzbdav history keyed by exact name.

    Returns:
        A mapping of completed download name to job metadata. Returns an empty
        mapping on any error or when no completed jobs exist.

    Side effects:
        Reads nzbdav settings from Kodi via xbmcaddon.Addon("plugin.video.nzbdav")
        unless ``settings_getter`` is supplied.
        Performs an HTTP GET to nzbdav /api?mode=history on every call; avoid
        calling this in tight loops.
        Logs the number of names loaded at debug level.
    """
    try:
        base_url, api_key = _get_settings(settings_getter=settings_getter)
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Settings read failed in get_completed_jobs: {}".format(
                _redact_text(str(e))
            ),
            xbmc.LOGDEBUG,
        )
        return {}

    params = {
        "mode": "history",
        "apikey": api_key,
        "output": "json",
        "limit": 500,
    }
    url = "{}/api?{}".format(base_url, urlencode(params))

    try:
        response_text = _http_get(url, timeout=_API_READ_TIMEOUT)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: get_completed_jobs request failed: {}".format(e),
            xbmc.LOGDEBUG,
        )
        return {}

    jobs = _completed_jobs_from_slots(_response_slots(response, "history"))
    xbmc.log(
        "NZB-DAV: Loaded {} completed downloads from history".format(len(jobs)),
        xbmc.LOGDEBUG,
    )
    return jobs


def get_completed_names():
    """Fetch all completed download names from nzbdav history.

    Returns:
        A set of completed download names for fast membership checks. Returns
        an empty set on any error or when no completed jobs exist.
    """
    return set(get_completed_jobs())


def get_job_status(nzo_id, settings_getter=None):
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
        base_url, api_key = _get_settings(settings_getter=settings_getter)
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
        response_text = _http_get(url, timeout=_API_READ_TIMEOUT)
        response = _coerce_response_dict(json.loads(response_text))
    except Exception as e:  # pylint: disable=broad-except
        # ``Exception`` intentionally — the prior ``(URLError, json.JSONDecodeError,
        # Exception)`` tuple was dead code (Exception subsumes the first two).
        # Resolver polls this every second while a download is active; any
        # crash here would kill the poll loop, so we log and return None
        # so the caller treats the tick as "no data, try again".
        xbmc.log(
            "NZB-DAV: Job status request failed for nzo_id={}: {}".format(
                nzo_id, _redact_text(str(e))
            ),
            xbmc.LOGERROR,
        )
        return None
    slots = _response_slots(response, "queue")
    return _job_status_from_slots(slots, nzo_id)
