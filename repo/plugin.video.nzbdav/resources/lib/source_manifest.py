# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated fetch of btad's one-time release source manifest."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from resources.lib.http_util import http_get


def _is_http_url(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def load_source_manifest(manifest_url):
    """Return title, primary URL and ordered URLs, or None for an invalid manifest."""
    if not _is_http_url(manifest_url):
        return None
    try:
        raw = http_get(manifest_url, timeout=15, max_bytes=128 * 1024)
        data = json.loads(raw)
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    primary_url = data.get("primary_url")
    source_urls = data.get("source_urls")
    if (
        not isinstance(title, str)
        or not _is_http_url(primary_url)
        or not isinstance(source_urls, list)
    ):
        return None
    if not source_urls or not all(_is_http_url(url) for url in source_urls):
        return None
    if primary_url not in source_urls:
        return None
    return title, primary_url, source_urls
