# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Shared HTTP and Kodi utility functions."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

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
    netloc = parts.netloc
    if netloc and "@" in netloc:
        userinfo, _, host = netloc.rpartition("@")
        if ":" in userinfo:
            user, _, _ = userinfo.partition(":")
            netloc = "{}:REDACTED@{}".format(user, host)
        else:
            # No `:password` half — userinfo is just a username.
            netloc = "{}@{}".format(userinfo, host)
    return urlunsplit(
        (parts.scheme, netloc, parts.path, urlencode(query), parts.fragment)
    )


def redact_text(text):
    """Redact apikey-style tokens from free-form text (error bodies, logs).

    ``redact_url`` requires a parseable URL. Use this helper when the
    payload is a string that might embed credentials — upstream HTTP
    error pages, exception messages, etc. Replaces each matched
    ``<key>=<value>`` pair with ``<key>=REDACTED`` so the structure of
    the surrounding text is preserved.
    """
    if not text:
        return text
    return _EMBEDDED_CRED_RE.sub(lambda m: "{}=REDACTED".format(m.group(1)), str(text))


_ALLOWED_HTTP_SCHEMES = frozenset({"http", "https"})
HTTP_USER_AGENT = "NZB-DAV Kodi Addon"
_HTTP_USER_AGENT = HTTP_USER_AGENT


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


def http_get(url, timeout=15):
    """Perform an HTTP GET and return the response body as text.

    Invalid UTF-8 is decoded with replacement so callers receive one
    normalized request failure path instead of a raw ``UnicodeDecodeError``.
    XML/JSON parsers still reject genuinely malformed payloads downstream.

    Raises ``ValueError`` for URLs whose scheme isn't ``http`` /
    ``https``. urllib's default opener happily handles ``file://`` and
    ``ftp://`` and would otherwise return ``/etc/passwd`` if a user
    pasted that into a URL setting field.
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_HTTP_SCHEMES:
        raise ValueError("unsupported URL scheme: {!r}".format(scheme))
    req = Request(url, headers={"User-Agent": _HTTP_USER_AGENT})
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
        days = delta.days
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
        - ``""`` when ``size_bytes`` is falsy.
        - ``"X.Y GB"`` when size >= 1 GiB (binary MiB/GiB units).
        - ``"X.Y MB"`` when size >= 1 MiB.
        - ``"N B"`` for anything smaller.
    """
    if not size_bytes:
        return ""
    size_bytes = int(size_bytes)
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
