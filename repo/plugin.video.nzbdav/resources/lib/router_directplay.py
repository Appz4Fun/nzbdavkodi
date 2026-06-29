# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Direct-play (``/direct_play``) helpers split out of ``router``.

Every reference to a name that lives in (or is patched via) ``router`` is
resolved at call time through ``import resources.lib.router as _router`` so the
test suite's ``@patch("resources.lib.router.<name>")`` decorators keep working
and no top-level import cycle is introduced. ``_handle_direct_play`` itself
stays in ``router`` (tests import it from there) and calls into here.
"""

import base64
from urllib.parse import unquote, urlsplit, urlunsplit

import xbmc


def _direct_play_parse_fallback_urls(fallback_urls_raw):
    """Parse the ``fallback_urls`` JSON param into a list (``[]`` on bad shape)."""
    import json as _json

    try:
        fallback_urls = _json.loads(fallback_urls_raw)
    except (TypeError, ValueError):
        fallback_urls = []
    if not isinstance(fallback_urls, list):
        fallback_urls = []
    return fallback_urls


def _direct_play_prepare_and_serve(
    handle, primary_url, primary_auth, fallback_urls, validate_url
):
    """Build fallback sources, prepare the proxy, and hand Kodi the proxy URL.

    Extracted verbatim from the tail of ``_handle_direct_play``; resolves the
    handle False on a missing proxy URL and True on success, unchanged.
    """
    import resources.lib.router as _router
    from resources.lib.resolver import (
        _direct_playback_service_config,
        _prepare_direct_playback,
    )

    fallback_sources = _direct_play_fallback_sources(fallback_urls, validate_url)

    xbmc.log(
        "NZB-DAV: /direct_play primary={} fallbacks={}".format(
            primary_url[:120], len(fallback_sources)
        ),
        xbmc.LOGINFO,
    )

    primary_headers = {"Authorization": primary_auth} if primary_auth else {}
    service_port, prepare_token = _direct_playback_service_config()
    prepared = _prepare_direct_playback(
        primary_url,
        primary_headers,
        fallback_sources=fallback_sources,
        service_port=service_port,
        prepare_token=prepare_token,
    )
    proxy_url = _direct_play_proxy_url(prepared)
    if not proxy_url:
        _router.xbmcplugin.setResolvedUrl(handle, False, _router.xbmcgui.ListItem())
        return
    xbmc.log(
        "NZB-DAV: /direct_play handing Kodi proxy URL: {}".format(proxy_url[:160]),
        xbmc.LOGINFO,
    )
    listitem = _router.xbmcgui.ListItem(path=proxy_url)
    listitem.setMimeType("video/x-matroska")
    listitem.setContentLookup(False)
    _router.xbmcplugin.setResolvedUrl(handle, True, listitem)


def _direct_play_split_auth(url):
    """Return (clean_url, auth_header) — Python urllib's name
    resolver mis-parses ``user:pass@host`` and raises gaierror,
    so we have to peel off the inline auth and pass it via header."""
    try:
        parsed = urlsplit(url)
    except (ValueError, TypeError):
        return url, ""
    # Empty username (``://:pass@host`` or ``://@host``) is not a
    # legitimate auth credential; emitting ``Basic OnBhc3M=`` would
    # send a malformed header that some upstreams accept and some
    # reject. Treat it as "no auth" and let the caller forward the
    # URL verbatim.
    if parsed.username in (None, ""):
        return url, ""
    userpass = "{}:{}".format(unquote(parsed.username), unquote(parsed.password or ""))
    encoded = base64.b64encode(userpass.encode()).decode()
    host = parsed.hostname or ""
    if parsed.port:
        host = "{}:{}".format(host, parsed.port)
    clean = urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )
    return clean, "Basic " + encoded


def _head_content_length(resp):
    """Return (length, error) from a HEAD response's Content-Length header."""
    resp_headers = getattr(resp, "headers", {}) or {}
    length = int(resp_headers.get("Content-Length", "1") or 1)
    if length > 0:
        return length, ""
    return 0, "missing-length"


def _direct_play_head_length(url, auth_header):
    """HEAD ``url`` and return (content_length, error). error is "" on success."""
    from urllib import request as urllib_request
    from urllib.error import HTTPError, URLError
    from urllib.request import Request

    import resources.lib.router as _router

    headers = {"Authorization": auth_header} if auth_header else {}
    try:
        req = Request(url, method="HEAD", headers=headers)
        # nosemgrep
        opener = (
            urllib_request.urlopen
            if _router.urlopen is _router._ORIGINAL_URLOPEN
            else _router.urlopen
        )
        with opener(req, timeout=10) as resp:  # nosec B310
            return _head_content_length(resp)
    except HTTPError as exc:
        return 0, "http-{}".format(exc.code)
    except URLError as exc:
        return 0, "url-{}".format(exc.reason)
    except (OSError, ValueError) as exc:
        return 0, str(exc)[:60]


def _direct_play_fallback_sources(fallback_urls, validate_url):
    """Build validated, HEAD-probed fallback source dicts for direct playback.

    Skips non-string/empty entries, non-http(s) URLs, and unstreamable peers
    (HEAD error or non-positive length), logging each skip exactly as before.
    """
    fallback_sources = []
    for idx, url_raw in enumerate(fallback_urls):
        if not isinstance(url_raw, str) or not url_raw:
            continue
        url, auth = _direct_play_split_auth(url_raw)
        try:
            validate_url(url)
        except (ValueError, TypeError):
            xbmc.log(
                "NZB-DAV: /direct_play skipping non-http(s) fallback: {}".format(
                    url_raw[:120]
                ),
                xbmc.LOGWARNING,
            )
            continue
        length, err = _direct_play_head_length(url, auth)
        if err or length <= 0:
            xbmc.log(
                "NZB-DAV: /direct_play skipping unstreamable fallback "
                "({}): {}".format(err, url[:120]),
                xbmc.LOGWARNING,
            )
            continue
        stream_headers = {"Authorization": auth} if auth else {}
        fallback_sources.append(
            {
                "title": "direct-play-fallback-{}".format(idx),
                "nzb_url": "",
                "job_name": "direct-play-fallback-{}".format(idx),
                "nzo_id": "direct-play-fallback-{}".format(idx),
                "stream_url": url,
                "stream_headers": stream_headers,
                "content_length": length,
            }
        )
    return fallback_sources


def _direct_play_proxy_url(prepared):
    """Extract the proxy URL from a prepare payload, logging failures.

    Returns the URL string, or ``""`` when the payload is missing/empty or has
    no proxy URL (caller resolves the Kodi handle as a failure).
    """
    if not prepared:
        xbmc.log("NZB-DAV: /direct_play prepare returned no payload", xbmc.LOGERROR)
        return ""
    if isinstance(prepared, str):
        return prepared
    proxy_url = prepared.get("playback_url") or prepared.get("proxy_url")
    if not proxy_url:
        xbmc.log(
            "NZB-DAV: /direct_play prepared payload missing proxy URL: keys={}".format(
                list(prepared.keys())
            ),
            xbmc.LOGERROR,
        )
        return ""
    return proxy_url
