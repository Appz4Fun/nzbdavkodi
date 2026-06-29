# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Service-proxy prepare/flush client for the nzbdav stream proxy.

Extracted from ``stream_proxy.py`` (Stage 1 decomposition). Holds the loopback
HTTP client that the resolver uses to push a prepared stream / its fallbacks
into the background proxy service, plus ``ServiceProxyUnavailableError``. All
public entry points are re-exported by ``stream_proxy`` so existing
``stream_proxy.prepare_stream_via_service`` references and test patches keep
resolving.

Plain constants are imported from ``stream_proxy``; parent helpers and any
monkeypatch target (``urlopen``, ``get_service_proxy_token``) are reached at
call time via ``_sp.<name>`` so patching keeps working.
"""

import socket as _socket  # noqa: E402
import time  # noqa: E402
from urllib.error import URLError  # noqa: E402
from urllib.request import Request  # noqa: E402

import resources.lib.stream_proxy as _sp  # noqa: E402
from resources.lib.http_util import HTTP_USER_AGENT  # noqa: E402
from resources.lib.stream_proxy import (  # noqa: E402
    _PREPARE_ATTEMPT_TIMEOUT,
    _PREPARE_MAX_ATTEMPTS,
    _PREPARE_RETRY_BACKOFF,
    _PREPARE_TOKEN_HEADER,
)


class ServiceProxyUnavailableError(OSError):
    """Raised when the NZB-DAV background service's proxy is unreachable.

    Distinct from the underlying OSError so resolver's error-handling
    layer can present the user a specific "background service not
    running" message instead of the raw ``[Errno 61] Connection
    refused`` shape. Still inherits OSError so existing broad
    ``except OSError`` clauses (resolver's _RESOLVE_RUNTIME_ERRORS)
    keep catching it without a code change.
    """


def _build_prepare_payload(
    remote_url, auth_header, fallback_sources, content_length_hint, settings_snapshot
):
    """Build the /prepare JSON payload, omitting absent optional fields."""
    payload = {
        "remote_url": remote_url,
        "auth_header": auth_header,
        "fallback_sources": list(fallback_sources or []),
    }
    content_length_hint = _sp._normalize_content_length_hint(content_length_hint)
    if content_length_hint > 0:
        payload["content_length_hint"] = content_length_hint
    settings_snapshot = _sp.normalize_settings_snapshot(settings_snapshot)
    if settings_snapshot:
        payload["settings_snapshot"] = settings_snapshot
    return payload


def _build_prepare_request(url, payload, prepare_token):
    """Build the POST Request for /prepare, attaching the auth token header."""
    import json

    req = Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("User-Agent", HTTP_USER_AGENT)
    req.add_header("Content-Type", "application/json")
    if prepare_token is None:
        prepare_token = _sp.get_service_proxy_token()
    if prepare_token:
        req.add_header(_PREPARE_TOKEN_HEADER, prepare_token)
    return req


def _prepare_attempt(req):
    """Perform one /prepare POST, returning (proxy_url, stream_info)."""
    import json

    # nosemgrep
    with _sp.urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
        req, timeout=_PREPARE_ATTEMPT_TIMEOUT
    ) as resp:
        result = json.loads(resp.read())
        proxy_url = result.pop("proxy_url")
        return proxy_url, result


def _classify_prepare_url_error(e):
    """Map a URLError to "retry", "unreachable", or "reraise".

    Retry the fast connection-reset family; treat a wrapped timeout/OSError as
    unreachable; re-raise everything else (e.g. an HTTPError from a reachable
    proxy — not a reachability problem).
    """
    reason = getattr(e, "reason", None)
    if isinstance(reason, ConnectionError):
        return "retry"
    if isinstance(reason, (_socket.timeout, TimeoutError, OSError)):
        return "unreachable"
    return "reraise"


def prepare_stream_via_service(
    port,
    remote_url,
    auth_header=None,
    prepare_token=None,
    fallback_sources=None,
    content_length_hint=None,
    settings_snapshot=None,
):
    """Ask the service's proxy to prepare a stream.

    Returns (proxy_url, stream_info) where stream_info contains
    duration_seconds, total_bytes, seekable, remux.

    Raises ServiceProxyUnavailableError when the local proxy port is
    stale / service crashed / firewall ate the loopback connection —
    the user-visible error-dialog layer uses the subclass to substitute
    an actionable message for the opaque ``Connection refused``.
    """
    url = "http://127.0.0.1:{}/prepare".format(port)
    auth_header = _sp._validate_auth_header(auth_header)
    payload = _build_prepare_payload(
        remote_url,
        auth_header,
        fallback_sources,
        content_length_hint,
        settings_snapshot,
    )
    req = _build_prepare_request(url, payload, prepare_token)
    unreachable = (
        "NZB-DAV background service unreachable on 127.0.0.1:{} — "
        "restart Kodi or toggle the addon".format(port)
    )
    last_error = None
    for index in range(_PREPARE_MAX_ATTEMPTS):
        try:
            return _prepare_attempt(req)
        except ConnectionError as e:
            # FAST transient: a momentarily thread-starved proxy accepted then
            # dropped the loopback socket (RemoteDisconnected / reset / refused).
            # It clears in well under a second once a handler thread frees up,
            # so retry before surfacing the terminal error rather than failing
            # the whole playback on the first transient hiccup.
            last_error = e
        except (_socket.timeout, TimeoutError) as e:
            # The proxy accepted but never answered within the budget: wedged,
            # not starved. Retrying another full budget won't help, so surface
            # immediately — same worst case as before this retry loop existed.
            raise ServiceProxyUnavailableError(unreachable) from e
        except URLError as e:
            # URLError wraps the same family of errors when urlopen fails. Retry
            # only the fast connection-reset family; surface a wrapped timeout/
            # OSError as unreachable; re-raise everything else (e.g. HTTPError,
            # a 4xx/5xx from a reachable proxy — not a reachability problem).
            disposition = _classify_prepare_url_error(e)
            if disposition == "retry":
                last_error = e
            elif disposition == "unreachable":
                raise ServiceProxyUnavailableError(unreachable) from e
            else:
                raise
        if index < _PREPARE_MAX_ATTEMPTS - 1:
            time.sleep(_PREPARE_RETRY_BACKOFF)
    # Every attempt failed with a fast connection reset.
    raise ServiceProxyUnavailableError(unreachable) from last_error


def update_stream_fallbacks_via_service(
    port, session_id, fallback_sources, prepare_token=None
):
    """Push late-adopted fallback sources into a live proxy session.

    Best-effort: the caller should swallow exceptions (the cutover still works
    for whatever was attached at /prepare time). Returns the parsed JSON
    response (``{"added": n}``) on success.
    """
    import json

    url = "http://127.0.0.1:{}/stream/{}/fallbacks".format(port, session_id)
    payload = {"fallback_sources": list(fallback_sources or [])}
    req = Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("User-Agent", HTTP_USER_AGENT)
    req.add_header("Content-Type", "application/json")
    if prepare_token is None:
        prepare_token = _sp.get_service_proxy_token()
    if prepare_token:
        req.add_header(_PREPARE_TOKEN_HEADER, prepare_token)
    # Short timeout: this is an in-process loopback service that answers in
    # well under a second, and the flush push runs inline on the resolver
    # thread just before playback handoff — a long timeout would stall it.
    # nosemgrep
    with _sp.urlopen(req, timeout=3) as resp:  # nosec B310 — loopback service URL
        return json.loads(resp.read())
