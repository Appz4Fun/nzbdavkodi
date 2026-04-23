# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Shared HTTP and Kodi utility functions."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


def redact_url(url):
    """Redact API keys from URLs for safe logging."""
    parts = urlsplit(url)
    query = [
        (k, "REDACTED" if k.lower() == "apikey" else v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def http_get(url, timeout=15):
    """Perform an HTTP GET and return the response body as text."""
    req = Request(url)
    with urlopen(
        req, timeout=timeout
    ) as resp:  # nosec B310 nosemgrep — URL validated by caller (nzbdav/hydra/prowlarr config)
        return resp.read().decode("utf-8")


_PUBDATE_ERRORS = (OverflowError, TypeError, ValueError)


def format_request_error(error):
    """Return a user-facing HTTP request error without urllib wrapper noise.

    Shared between hydra.py and prowlarr.py so both indexer clients surface
    the same error text for the same underlying failure.
    """
    reason = getattr(error, "reason", None)
    if reason:
        return str(reason)
    return str(error)


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
    """Format byte size to human readable."""
    if not size_bytes:
        return ""
    size_bytes = int(size_bytes)
    if size_bytes >= 1073741824:
        return "{:.1f} GB".format(size_bytes / 1073741824)
    if size_bytes >= 1048576:
        return "{:.1f} MB".format(size_bytes / 1048576)
    return "{} B".format(size_bytes)


def notify(heading, message, duration=5000):
    """Show a Kodi notification."""
    import xbmc

    xbmc.executebuiltin("Notification({}, {}, {})".format(heading, message, duration))
