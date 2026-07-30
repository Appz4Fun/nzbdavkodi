# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Shared HTTP and Kodi utility functions."""

import contextlib
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


@contextlib.contextmanager
def prefer_ipv4_connections():
    """Reorder DNS results so IPv4 addresses are tried before IPv6 ones.

    Docker Desktop / WSL2 port-forwarding for NZB-DAV's own API and WebDAV
    endpoints is IPv4-only, but "localhost" resolves to both ``::1`` and
    ``127.0.0.1``, and the standard library tries ``getaddrinfo`` results in
    the order returned -- IPv6 first on this platform. Every request then
    pays a multi-second wait for the IPv6 attempt to be refused before
    falling back to the IPv4 address that actually works: measured at
    ~2.0s per request, three times in a single resolve (PROPFIND, HEAD, and
    the mid-file body probe), accounting for the entire observed ~10s
    "resolving" stall before the resume/restart prompt even appears.

    This only REORDERS results -- it never removes IPv6 -- so a host that
    is genuinely reachable only over IPv6 still works, just tried second.
    Scoped to the enclosing ``with`` block and restored immediately after,
    so it cannot affect unrelated connections outside that block. Safe to
    nest / use from multiple threads concurrently: at worst two calls
    briefly agree on the same (harmless) reordering.
    """
    original_getaddrinfo = socket.getaddrinfo

    def _ipv4_first(*args, **kwargs):
        results = original_getaddrinfo(*args, **kwargs)
        return sorted(results, key=lambda info: info[0] != socket.AF_INET)

    socket.getaddrinfo = _ipv4_first
    try:
        yield
    finally:
        socket.getaddrinfo = original_getaddrinfo


# Match common credential-style query parameter names. Covers the usual
# suspects: ``apikey``, ``api_key``, ``auth``, ``token``, ``password``,
# ``secret``. Matched case-insensitively against the param name itself,
# not the value.
_REDACT_PARAM_NAMES = frozenset(
    {
        "apikey",
        "api_key",
        "auth",
        "token",
        "password",
        "passwd",
        "secret",
        # Extended set per TODO.md §H.2-H2c. `key` (without prefix) is
        # used by some Newznab-style indexers; `access_token` covers
        # OAuth-style callbacks; `bearer` covers Authorization header
        # values that get spliced into URLs by mistake.
        "key",
        "access_token",
        "bearer",
        "session",
        "sessionid",
    }
)

# Pattern to catch apikey=... embedded in free-form strings (HTTP error
# bodies, exception messages). Used by redact_text() for the cases where
# a full URL parse isn't practical.
_EMBEDDED_CRED_RE = re.compile(
    r"(apikey|api_key|access_token|token|bearer|auth|password|passwd|secret"
    r"|sessionid|session|key)=([^&\s\"'<>]+)",
    re.IGNORECASE,
)

# Catch ``scheme://user:password@host`` userinfo embedded in free-form text.
# urllib / socket / xbmcvfs errors sometimes echo the failing URL — e.g. the
# NZBGet JSON-RPC URL or the ``smb://user:pass@host/...`` completed-folder
# root — which carries the password. ``redact_url`` only strips userinfo from
# a parseable URL; this handles the embedded-in-an-error-string case so the
# password does not leak into logs. TODO.md §H.2 (NZBGet path).
#
# We match a whole URL span and reuse ``redact_url``'s ``rpartition('@')``
# netloc logic rather than a single password-class regex. A naive
# ``user:pass@`` pattern leaks whenever the password itself contains an
# ``@`` (common in SMB/NZBGet credentials — the match stops at the FIRST
# ``@`` and the tail survives) and fails entirely for an empty username
# (``smb://:pass@host``). Splitting on the LAST ``@`` of the authority — the
# same way ``redact_url`` does — handles both.
_EMBEDDED_URL_RE = re.compile(r"(?:smb|https?|ftp)://[^\s\"'<>]+", re.IGNORECASE)


def redact_url(url):
    """Redact API keys and other credential-style params from URLs for safe logging.

    Handles two shapes callers pass:
    - Plain URLs where the key is a direct query parameter.
    - Embedded URLs: a query value that is itself a URL containing a
      credential query (e.g. ``/api?mode=addurl&name=http://hydra/getnzb/
      abc?apikey=SECRET``). The outer ``name=`` value gets recursively
      redacted so the inner ``apikey=`` doesn't leak.

    Unknown / malformed URLs round-trip unchanged.
    """
    try:
        parts = urlsplit(url)
    except (ValueError, TypeError):
        return url
    query = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if k.lower() in _REDACT_PARAM_NAMES:
            query.append((k, "REDACTED"))
            continue
        # Redact recursively if the value itself looks like a URL with
        # credentials. Guards against the common "submit this URL to
        # nzbdav" shape where the embedded URL carries an indexer apikey.
        if v and "://" in v and "=" in v:
            query.append((k, redact_url(v)))
        else:
            query.append((k, v))
    # Redact `user:password@host` userinfo in the netloc — Basic-auth-in-URL
    # is a real shape some users hand-paste into settings (and that the
    # WebDAV stack used to accept). Strip the password half before
    # logging. TODO.md §H.2-H2d.
    netloc = _redact_netloc_userinfo(parts.netloc)
    return urlunsplit(
        (parts.scheme, netloc, parts.path, urlencode(query), parts.fragment)
    )


def _redact_netloc_userinfo(netloc):
    """Strip the password half of a ``user:pass@host`` netloc for logging.

    An ``@`` inside the password or an empty username can't leak, since
    ``rpartition`` splits on the last ``@`` and only the username survives.
    Netlocs with no userinfo round-trip unchanged.
    """
    if not netloc or "@" not in netloc:
        return netloc
    userinfo, _, host = netloc.rpartition("@")
    if ":" in userinfo:
        user, _, _password = userinfo.partition(":")
        return "{}:REDACTED@{}".format(user, host)
    # No `:password` half — userinfo is just a username.
    return "{}@{}".format(userinfo, host)


def _redact_url_userinfo_span(match):
    """Strip the password from a URL span's ``user:pass@host`` userinfo.

    Reuses the same ``rpartition('@')`` / ``partition(':')`` logic as
    ``redact_url`` so an ``@`` *inside* the password (or an empty username)
    can't leak. Spans with no userinfo round-trip unchanged.
    """
    span = match.group(0)
    try:
        parts = urlsplit(span)
    except (ValueError, TypeError):
        return span
    if not parts.netloc or "@" not in parts.netloc:
        return span
    netloc = _redact_netloc_userinfo(parts.netloc)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def redact_text(text):
    """Redact apikey-style tokens from free-form text (error bodies, logs).

    ``redact_url`` requires a parseable URL. Use this helper when the
    payload is a string that might embed credentials — upstream HTTP
    error pages, exception messages, etc. Replaces each matched
    ``<key>=<value>`` pair with ``<key>=REDACTED`` so the structure of
    the surrounding text is preserved, and scrubs the password half of any
    embedded ``scheme://user:pass@host`` URL.
    """
    if not text:
        return text
    # ``\1`` is the key group; a backreference replacement avoids a per-match
    # Python callback on this hot logging/error path.
    redacted = _EMBEDDED_CRED_RE.sub(r"\1=REDACTED", str(text))
    return _EMBEDDED_URL_RE.sub(_redact_url_userinfo_span, redacted)


_WHITESPACE_RE = re.compile(r"\s+")


def clean_search_query(title):
    """Normalize a title for use as a Newznab/Prowlarr keyword query.

    Indexers tokenize the ``q``/``query`` text and AND each term against
    release names. A literal ``&`` (e.g. "Your Friends & Neighbors") becomes a
    term that no release name carries — releases spell it "and" or drop it
    entirely — so the search matches nothing and returns zero results (#294).
    Replace ``&`` with a space and collapse the surrounding whitespace so the
    remaining words still match; ``&``-free titles are returned unchanged.
    """
    if not title:
        return ""
    return _WHITESPACE_RE.sub(" ", str(title).replace("&", " ")).strip()


_ALLOWED_HTTP_SCHEMES = frozenset({"http", "https"})
HTTP_USER_AGENT = "NZB-DAV Kodi Addon"
_HTTP_USER_AGENT = HTTP_USER_AGENT


class HttpResponseTooLarge(ValueError):
    """Raised when a response body exceeds the caller's byte limit."""


def _response_status(resp):
    """Return an integer HTTP status from urllib-like responses, if exposed."""
    for attr in ("status", "code"):
        status = getattr(resp, attr, None)
        if isinstance(status, int):
            return status
    getcode = getattr(resp, "getcode", None)
    if callable(getcode):
        status = getcode()
        if isinstance(status, int):
            return status
    return None


def _read_size_for_limit(resp, max_bytes):
    """Validate a non-negative ``max_bytes`` and return the capped read size.

    Raises ``HttpResponseTooLarge`` when the response's Content-Length
    already exceeds the limit. Callers pass a normalized ``int``.
    """
    try:
        content_length = int(resp.headers.get("Content-Length", "0") or 0)
    except (AttributeError, TypeError, ValueError):
        content_length = 0
    if content_length > max_bytes:
        raise HttpResponseTooLarge("HTTP response exceeds {} bytes".format(max_bytes))
    return max_bytes + 1


def _read_capped_body(resp, max_bytes):
    """Read the response body, enforcing ``max_bytes`` when not None.

    Raises ``ValueError`` for a negative limit and ``HttpResponseTooLarge``
    when the body (by Content-Length or actual read) exceeds the limit.
    """
    if max_bytes is None:
        return resp.read()
    max_bytes = int(max_bytes)
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    body = resp.read(_read_size_for_limit(resp, max_bytes))
    if len(body) > max_bytes:
        raise HttpResponseTooLarge("HTTP response exceeds {} bytes".format(max_bytes))
    return body


def http_get(url, timeout=15, headers=None, max_bytes=None):
    """Perform an HTTP GET and return the response body as text.

    Invalid UTF-8 is decoded with replacement so callers receive one
    normalized request failure path instead of a raw ``UnicodeDecodeError``.
    XML/JSON parsers still reject genuinely malformed payloads downstream.

    Raises ``ValueError`` for URLs whose scheme isn't ``http`` /
    ``https``. urllib's default opener happily handles ``file://`` and
    ``ftp://`` and would otherwise return ``/etc/passwd`` if a user
    pasted that into a URL setting field.

    Optional ``headers`` are merged with the shared User-Agent. ``max_bytes``
    caps the response body and raises ``HttpResponseTooLarge`` when exceeded.
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_HTTP_SCHEMES:
        raise ValueError("unsupported URL scheme: {!r}".format(scheme))
    request_headers = {"User-Agent": _HTTP_USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = Request(url, headers=request_headers)
    with prefer_ipv4_connections():
        # nosemgrep
        with urlopen(  # nosec B310 — scheme allowlist enforced above
            req, timeout=timeout
        ) as resp:
            status = _response_status(resp)
            if status is not None and not 200 <= status < 300:
                raise OSError("HTTP status {}".format(status))
            body = _read_capped_body(resp, max_bytes)
            return body.decode("utf-8", errors="replace")


def http_post_json(url, payload, timeout=15, headers=None, basic_auth=None):
    """POST ``payload`` as a JSON body and return the response text.

    Mirrors ``http_get``'s scheme allowlist (urllib would otherwise honor
    ``file://``/``ftp://``). ``basic_auth`` is an optional ``(user, pass)``
    tuple sent as an HTTP Basic ``Authorization`` header — used by the
    NZBGet JSON-RPC client.
    """
    import base64
    import json as _json

    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_HTTP_SCHEMES:
        raise ValueError("unsupported URL scheme: {!r}".format(scheme))
    body = _json.dumps(payload).encode("utf-8")
    request_headers = {
        "User-Agent": _HTTP_USER_AGENT,
        "Content-Type": "application/json",
    }
    if headers:
        request_headers.update(headers)
    if basic_auth is not None:
        user, password = basic_auth
        token = base64.b64encode("{}:{}".format(user, password).encode("utf-8")).decode(
            "ascii"
        )
        request_headers["Authorization"] = "Basic " + token
    req = Request(url, data=body, headers=request_headers)
    with prefer_ipv4_connections():
        # nosemgrep
        with urlopen(  # nosec B310 — scheme allowlist enforced above
            req, timeout=timeout
        ) as resp:
            status = _response_status(resp)
            if status is not None and not 200 <= status < 300:
                raise OSError("HTTP status {}".format(status))
            return resp.read().decode("utf-8", errors="replace")


_PUBDATE_ERRORS = (OverflowError, TypeError, ValueError)


def format_request_error(error):
    """Return a user-facing HTTP request error without urllib wrapper noise.

    Shared between hydra.py and prowlarr.py so both indexer clients surface
    the same error text for the same underlying failure. Output is run
    through ``redact_text`` because some urllib error shapes (notably
    ``URLError`` wrapping a socket error and the rare ``HTTPError`` with
    a URL-bearing reason) can echo the failing URL — which embeds the
    indexer's ``apikey=...`` query — into a string that then surfaces
    to the user via ``Dialog().notification()``. TODO.md §H.2-H2e/H2f.
    """
    reason = getattr(error, "reason", None)
    if reason:
        return redact_text(str(reason))
    return redact_text(str(error))


def get_xml_text(element, tag):
    """Return the stripped text of a child element, or ``""`` when missing.

    Small wrapper used by the XML-parsing paths in hydra.py and
    prowlarr.py. Returning `""` instead of raising keeps the per-item
    parse loops simple: missing fields land as empty strings rather than
    exceptions.
    """
    child = element.find(tag)
    if child is not None and child.text:
        return child.text
    return ""


def pubdate_to_epoch(pubdate_str):
    """Return absolute UTC epoch seconds for an RFC-2822 pubdate, or None.

    Unlike :func:`calculate_age` (a *relative*, drifting "N days ago"
    label that changes every day for the same post), the epoch is a
    stable identity key: one Usenet post always maps to the same value.
    The picker uses it to tell apart same-name reposts that share a size
    but were posted on different days. A pubdate without a timezone is
    assumed UTC so the result is deterministic across hosts.
    """
    from datetime import timezone
    from email.utils import parsedate_to_datetime

    try:
        pub = parsedate_to_datetime(pubdate_str)
    except _PUBDATE_ERRORS:
        return None
    if pub is None:
        return None
    if pub.tzinfo is None:
        pub = pub.replace(tzinfo=timezone.utc)
    try:
        return int(pub.timestamp())
    except _PUBDATE_ERRORS:
        return None


# Trailing fractional seconds in an ISO-8601 timestamp (e.g. ``.1234567``).
# Compiled once at import; see :func:`iso8601_to_rfc2822`.
_ISO_FRACTIONAL_SECONDS_RE = re.compile(r"\.\d+")


def iso8601_to_rfc2822(value):
    """Convert an ISO-8601 datetime string to an RFC-2822 string.

    Prowlarr's native JSON API reports ``publishDate`` in ISO-8601
    (e.g. ``"2026-06-25T11:00:00Z"``), but every ``pubdate`` consumer in
    this addon parses RFC-2822 via ``email.utils.parsedate_to_datetime``:
    :func:`pubdate_to_epoch` (the stable identity key behind the picker's
    DL/repost gate and the fallback same-window dedup) and
    ``filter._pubdate_sort_key`` (the "Age" sort). ISO-8601 makes both
    silently fail — epoch ``None`` / sort key ``0`` — so we normalize at
    the source and keep the ``pubdate`` field format uniform rather than
    teaching every consumer a second grammar.

    Returns ``""`` when the input is empty or unparseable, matching the
    "missing field becomes empty string" contract of the parse loops.
    """
    from datetime import datetime, timezone
    from email.utils import format_datetime

    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    # .NET (Prowlarr's stack) can emit up to 7 fractional-second digits,
    # which pre-3.11 ``datetime.fromisoformat`` rejects; sub-second
    # precision is irrelevant for identity/sort/age, so drop it. The pattern
    # is module-level (``_ISO_FRACTIONAL_SECONDS_RE``) so it is compiled once
    # rather than on every result parsed during a search.
    text = _ISO_FRACTIONAL_SECONDS_RE.sub("", text)
    # ``fromisoformat`` only accepts a trailing 'Z' from Python 3.11 on;
    # map it to an explicit UTC offset for older Kodi runtimes (3.8–3.9).
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return format_datetime(dt)
    except (TypeError, ValueError, OverflowError):
        return ""


def age_string_from_days(days):
    """Format an integer day-count as a human-readable age string.

    Shared by the RFC-2822 ``pubDate`` path (``calculate_age``) and the
    Prowlarr native-JSON path, which reports an integer ``age`` (in days)
    directly rather than a parseable date string. Returns ``"today"``,
    ``"1 day"``, ``"<n> days"``, ``"1 month"``, ``"<n> months"``, or an
    empty string when ``days`` is missing, non-numeric, or negative.
    """
    try:
        days = int(days)
    except (TypeError, ValueError):
        return ""
    if days < 0:
        return ""
    if days == 0:
        return "today"
    if days == 1:
        return "1 day"
    if days < 30:
        return "{} days".format(days)
    months = days // 30
    if months == 1:
        return "1 month"
    return "{} months".format(months)


def calculate_age(pubdate_str):
    """Return a human-readable age string computed from an RFC 2822 date.

    Returns values like ``"today"``, ``"1 day"``, ``"<n> days"``,
    ``"1 month"``, ``"<n> months"``, or an empty string if the input
    cannot be parsed.
    """
    from datetime import datetime, timezone
    from email.utils import parsedate_to_datetime

    try:
        pub = parsedate_to_datetime(pubdate_str)
        now = datetime.now(timezone.utc)
        delta = now - pub
        return age_string_from_days(delta.days)
    except _PUBDATE_ERRORS:
        return ""


def format_size(size_bytes):
    """Return a human-readable byte-size string.

    Args:
        size_bytes: int or str representation of a byte count. Strings
            are coerced via ``int()`` so Newznab-style size="1234567"
            fields work without explicit conversion at every call
            site. ``None`` / ``0`` / ``""`` all map to an empty
            string — the caller renders "unknown size" in that slot.

    Returns:
        One of:
        - ``""`` when ``size_bytes`` is falsy or malformed.
        - ``"X.Y GB"`` when size >= 1 GiB (binary MiB/GiB units).
        - ``"X.Y MB"`` when size >= 1 MiB.
        - ``"N B"`` for anything smaller.
    """
    if not size_bytes:
        return ""
    try:
        size_bytes = int(size_bytes)
    except (TypeError, ValueError):
        return ""
    if size_bytes < 0:
        return ""
    if size_bytes >= 1073741824:
        return "{:.1f} GB".format(size_bytes / 1073741824)
    if size_bytes >= 1048576:
        return "{:.1f} MB".format(size_bytes / 1048576)
    return "{} B".format(size_bytes)


def _escape_builtin_arg(text):
    """Sanitize a string for inclusion in an `xbmc.executebuiltin` argument.

    Kodi's builtin parser splits arguments on top-level commas and treats
    parentheses as call-grouping; an unredacted `,` or `)` in `heading`
    or `message` would let an upstream-controlled string break out of the
    Notification call and inject arbitrary builtin invocations. The
    reduction below maps the two structural metacharacters to visually-
    similar Unicode lookalikes so the user-visible text stays legible
    while the parser sees only inert characters. Newlines are also
    flattened to spaces because some Kodi builds let an embedded newline
    terminate the builtin and run the next line as code.

    See TODO.md §H.2-H15 / §H.3 for the original audit finding.
    """
    if text is None:
        return ""
    return (
        str(text)
        .replace(",", "،")  # Arabic comma U+060C — visually similar, parser-inert
        .replace(")", "❩")  # medium right parenthesis ornament U+2769
        .replace("\n", " ")
        .replace("\r", " ")
    )


def notify(heading, message, duration=5000):
    """Show a Kodi notification."""
    import xbmc

    xbmc.executebuiltin(
        "Notification({}, {}, {})".format(
            _escape_builtin_arg(heading),
            _escape_builtin_arg(message),
            int(duration) if isinstance(duration, (int, float)) else 5000,
        )
    )
