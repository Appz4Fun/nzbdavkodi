# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Fallback-source / session / URL / segment helpers for the stream proxy.

Extracted from ``stream_proxy.py`` (Stage 1 decomposition). Groups URL/auth
validation, fallback-source normalization + dedup + merge, content-length hint
probing, session TTL/LRU bookkeeping, proxy-URL session-id extraction, HLS
segment-resource parsing, seek detection, and per-handler lease/context touch
helpers. All names are re-exported by ``stream_proxy`` so existing references
and test patches (e.g. ``stream_proxy._validate_url``) keep resolving.

Plain constants are imported from ``stream_proxy``; parent helpers and any
monkeypatch target (``xbmc``, ``_notify``, ``urlopen``) are reached at call
time via ``_sp.<name>`` so patching keeps working.
"""

import time  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402
from urllib.request import Request  # noqa: E402

import resources.lib.stream_proxy as _sp  # noqa: E402
from resources.lib.http_util import prefer_ipv4_connections  # noqa: E402
from resources.lib.stream_proxy import (  # noqa: E402
    _CONTENT_RANGE_ZERO_RE,
    _SEEK_THRESHOLD,
    _SESSION_TTL_SECONDS,
)


def _validate_url(url):
    """Reject URLs that could inject into downstream argv / HTTP framing.

    The URL eventually lands as a ``-i`` / ``-headers`` argument to
    ffmpeg, and as the path of an outgoing HTTP request. Two guards:

    - **Scheme allow-list**: only ``http://`` / ``https://`` accepted.
      Catches ``file://``, ``ftp://``, and the junk a local process
      might POST to our loopback proxy.
    - **Control-char reject**: any byte below 0x20 (CR, LF, NUL, tab,
      etc.) in the URL string is rejected. Without this, a URL with an
      embedded ``\\r\\n`` could inject an HTTP header into ffmpeg's
      outbound request, and a URL with ``\\n`` in an ffmpeg ``-i``
      could be mis-parsed as a separate argv entry on older ffmpeg.
    """
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError("Invalid URL scheme: {}".format(repr(url)[:30]))
    if any(ord(c) < 0x20 for c in url):
        raise ValueError("URL contains control characters: {}".format(repr(url)[:60]))


def _validate_auth_header(auth_header):
    """Validate an Authorization header value before forwarding it."""
    if auth_header in (None, ""):
        return None
    if not isinstance(auth_header, str):
        raise ValueError("Authorization header must be a string")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in auth_header):
        raise ValueError("Authorization header contains control characters")
    return auth_header


def _normalize_fallback_source(source):
    """Normalize one fallback source dict, or None if it is unusable."""
    if not isinstance(source, dict):
        return None
    stream_url = source.get("stream_url") or ""
    nzo_id = source.get("nzo_id") or ""
    if not stream_url and not nzo_id:
        return None
    if stream_url:
        _sp._validate_url(stream_url)
    stream_headers = source.get("stream_headers")
    if not isinstance(stream_headers, dict):
        stream_headers = {}
    content_length = _sp._normalize_content_length_hint(source.get("content_length"))
    entry = {
        "title": source.get("title", ""),
        "nzb_url": source.get("nzb_url", ""),
        "job_name": source.get("job_name", ""),
        "nzo_id": nzo_id,
        "stream_url": stream_url,
        "stream_headers": dict(stream_headers),
        "content_length": content_length,
        "validated": bool(source.get("validated", False)),
        "failed": bool(source.get("failed", False)),
    }
    if isinstance(source.get("episode_context"), dict):
        entry["episode_context"] = dict(source["episode_context"])
    return entry


def _normalize_fallback_sources(fallback_sources):
    """Validate and normalize fallback stream metadata for session contexts."""
    normalized = []
    for source in fallback_sources or []:
        entry = _sp._normalize_fallback_source(source)
        if entry is not None:
            normalized.append(entry)
    return normalized


def _coerce_nonneg_int(value, default=0):
    """Int-coerce value, treating falsy/invalid input as the default."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _normalize_content_length_hint(content_length_hint):
    try:
        hint = int(content_length_hint or 0)
    except (TypeError, ValueError):
        return 0
    return hint if hint > 0 else 0


def _probe_content_length_hint(url, auth_header, content_length_hint):
    """Confirm a known length via a bytes=0-0 range probe; 0 if unconfirmed."""
    try:
        req = Request(url)
        _sp._add_request_headers(req, auth_header)
        req.add_header("Range", "bytes=0-0")
        with prefer_ipv4_connections():
            # nosemgrep
            with _sp.urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
                req, timeout=10
            ) as resp:
                cr = resp.headers.get("Content-Range", "")
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode()
                match = _CONTENT_RANGE_ZERO_RE.match(cr.strip())
                stream_length = int(match.group(1)) if match else 0
                if status == 206 and stream_length == content_length_hint:
                    return content_length_hint
    except (OSError, ValueError):
        pass
    return 0


def _probe_content_length_tail(url, auth_header):
    """Read total size from the Content-Range of a bytes=-1 tail probe."""
    try:
        req = Request(url)
        _sp._add_request_headers(req, auth_header)
        req.add_header("Range", "bytes=-1")
        with prefer_ipv4_connections():
            # nosemgrep
            with _sp.urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
                req, timeout=10
            ) as resp:
                cr = resp.headers.get("Content-Range", "")
                return int(cr.split("/")[1]) if "/" in cr else 0
    except (OSError, ValueError):
        return 0


def _session_last_activity(ctx, default):
    """Best-known activity timestamp for a session ctx."""
    return ctx.get("last_access", ctx.get("created_at", default))


def _expired_session_ids(sessions, keep_session, now):
    """Session ids older than the TTL, excluding the kept session."""
    return [
        session_id
        for session_id, ctx in sessions.items()
        if session_id != keep_session
        and now - _sp._session_last_activity(ctx, now) > _SESSION_TTL_SECONDS
    ]


def _least_recently_used_session(sessions, keep_session):
    """Id of the least-recently-active evictable session, or None."""
    removable = sorted(
        (_sp._session_last_activity(ctx, 0), session_id)
        for session_id, ctx in sessions.items()
        if session_id != keep_session
    )
    if not removable:
        return None
    return removable[0][1]


def _thread_is_alive(thread):
    """True when thread exists and is still running."""
    return thread is not None and thread.is_alive()


def _fallback_source_needs_prevalidation(source):
    """Whether a fallback source still has prevalidation work pending."""
    if source.get("failed") or source.get("validated"):
        return False
    # Either a resolved URL ready to fingerprint, or an nzo-only standby we
    # can resolve into one.
    return bool(source.get("stream_url") or source.get("nzo_id"))


def _fallback_dedup_key(source):
    # Dedup by nzo_id when present: a pushed source always carries
    # stream_url="" (jobs only know their nzo_id), but the live cutover
    # resolves stream_url in place — so a (nzo_id, stream_url) tuple key
    # would treat a re-push of the same nzo as new once it's resolved,
    # re-adding a duplicate that un-fails the source. Fall back to the
    # URL only for url-only sources that have no nzo_id.
    nzo_id = source.get("nzo_id", "")
    if nzo_id:
        return ("nzo", nzo_id)
    return ("url", source.get("stream_url", ""))


def _merge_new_fallback_sources(existing, normalized):
    """Append not-yet-seen normalized sources into existing; return added count."""
    seen = {_sp._fallback_dedup_key(s) for s in existing if isinstance(s, dict)}
    added = 0
    for src in normalized:
        key = _sp._fallback_dedup_key(src)
        if key in seen:
            continue
        seen.add(key)
        existing.append(src)
        added += 1
    return added


def _attach_fallback_context_fields(ctx, fallback_sources):
    """Attach fallback tracking fields to a stream context or stream info."""
    ctx["fallback_sources"] = list(fallback_sources)
    ctx["fallback_active_index"] = -1
    ctx["fallback_switch_count"] = 0
    return ctx


def _storage_to_webdav_path(storage):
    """Convert nzbdav history storage path to a WebDAV /content path."""
    if storage.startswith("/content/"):
        return storage.rstrip("/") + "/"

    for prefix in (
        "/mnt/nzbdav/completed-symlinks/",
        "/mnt/data/completed-symlinks/",
    ):
        if storage.startswith(prefix):
            relative = storage[len(prefix) :]
            return "/content/{}/".format(relative)

    parts = storage.rstrip("/").split("/")
    relative = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return "/content/{}/".format(relative)


def _extract_session_id_from_proxy_url(proxy_url):
    """Pull the session id back out of a `/stream/<id>` or `/hls/<id>/...` URL.

    Used by the orphan-session cleanup path on /prepare write-failure.
    Returns the session id string or None if the URL doesn't match the
    expected proxy-URL shapes.
    """
    if not proxy_url:
        return None
    try:
        path = urlsplit(proxy_url).path
    except (TypeError, ValueError):
        return None
    if path.startswith("/stream/"):
        rest = path[len("/stream/") :]
        return rest.split("/", 1)[0] or None
    if path.startswith("/hls/"):
        rest = path[len("/hls/") :]
        return rest.split("/", 1)[0] or None
    return None


def _notify_error(message):
    """Best-effort notification helper safe to call from proxy threads."""
    try:
        _sp._notify("NZB-DAV", str(message)[:80])
    except (RuntimeError, OSError):
        pass


def _is_seek_request(current_byte_pos, requested_byte_pos):
    """Determine if a range request is a genuine seek or a continuation.

    Returns True if the request is far from the current position (>10MB
    gap or backward), meaning ffmpeg should be restarted with -ss.
    """
    delta = requested_byte_pos - current_byte_pos
    if delta < 0:
        return True  # backward seek
    return delta > _SEEK_THRESHOLD


def _is_segment_resource(resource):
    """True if a parsed HLS resource is a ("segment", seg_n, ext) tuple."""
    return isinstance(resource, tuple) and resource[0] == "segment"


def _touch_stream_context(ctx, acquire):
    """Mark a stream context accessed, optionally leasing a handler slot.

    Returns the context, or None when it is missing or already tearing down.
    """
    if ctx is None or ctx.get("_cleanup_started"):
        return None
    ctx["last_access"] = time.time()
    if acquire:
        ctx["_active_handlers"] = int(ctx.get("_active_handlers", 0) or 0) + 1
    return ctx


def _parse_hls_segment_resource(resource):
    """Parse a ``seg_<N>.ts`` / ``seg_<N>.m4s`` resource into a segment tuple.

    Returns ``("segment", N, ext)`` for a well-formed non-negative segment,
    or None for any non-segment or malformed resource.
    """
    if not resource.startswith("seg_"):
        return None
    for ext in ("ts", "m4s"):
        suffix = "." + ext
        if not resource.endswith(suffix):
            continue
        try:
            seg_n = int(resource[len("seg_") : -len(suffix)])
        except ValueError:
            return None
        if seg_n < 0:
            return None
        return ("segment", seg_n, ext)
    return None


def _release_handler_lease(ctx):
    """Decrement the active-handler lease and claim deferred cleanup if due.

    Returns True (caller holds the context lock) when this release drops the
    last handler on a context that has pending cleanup not yet started.
    """
    active = max(0, int(ctx.get("_active_handlers", 0) or 0) - 1)
    ctx["_active_handlers"] = active
    if active == 0 and ctx.get("_cleanup_pending") and not ctx.get("_cleanup_started"):
        ctx["_cleanup_started"] = True
        return True
    return False


def _stream_context_session_id(path):
    """Extract a session id from a /stream/<id> or /hls/<id>/... path.

    Returns the session id string, or None when the path is malformed or
    not a session-scoped stream/HLS path.
    """
    if path.startswith("/stream/"):
        session_id = path[len("/stream/") :]
        if not session_id or "/" in session_id:
            return None
        return session_id
    if path.startswith("/hls/"):
        parts = path[len("/hls/") :].split("/", 1)
        if len(parts) != 2 or not parts[0]:
            return None
        return parts[0]
    return None
