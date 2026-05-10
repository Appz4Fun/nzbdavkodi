# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Newznab caps parsing and fetching."""

from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import xbmc

from resources.lib.http_util import format_request_error
from resources.lib.http_util import http_get as _http_get

_REQUEST_ERRORS = (AttributeError, OSError, RuntimeError, TypeError, ValueError)
_EMPTY_CAPS = {"search_types": [], "supported_params": {}, "categories": []}
_SEARCH_TAGS = {
    "search": "search",
    "tv-search": "tvsearch",
    "movie-search": "movie",
    "audio-search": "audio",
    "book-search": "book",
}


def build_caps_url(api_url, api_key):
    base = str(api_url or "").rstrip("/")
    if not base.endswith("/api"):
        base += "/api"
    return "{}?{}".format(base, urlencode({"apikey": api_key, "t": "caps", "o": "xml"}))


def _empty_caps():
    return {
        "search_types": [],
        "supported_params": {},
        "categories": [],
    }


def _local_name(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _params(value):
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def parse_caps(xml_text):
    try:
        root = ET.fromstring(xml_text)  # nosec B314 - Python 3.8+ disables entities
    except (ET.ParseError, TypeError):
        return _empty_caps()

    search_types = []
    supported_params = {}
    categories = []

    for element in root.iter():
        local = _local_name(element.tag)
        if local in _SEARCH_TAGS and element.get("available", "").lower() == "yes":
            search_type = _SEARCH_TAGS[local]
            search_types.append(search_type)
            supported_params[search_type] = _params(element.get("supportedParams"))
        elif local in ("category", "subcat"):
            try:
                category_id = int(element.get("id", ""))
            except ValueError:
                continue
            categories.append({"id": category_id, "name": element.get("name", "")})

    return {
        "search_types": search_types,
        "supported_params": supported_params,
        "categories": categories,
    }


def fetch_caps(api_url, api_key, timeout=15):
    url = build_caps_url(api_url, api_key)
    try:
        response = _http_get(url, timeout=timeout)
    except _REQUEST_ERRORS as error:
        formatted_error = format_request_error(error)
        xbmc.log(
            "NZB-DAV: Newznab caps fetch failed: {}".format(formatted_error),
            xbmc.LOGWARNING,
        )
        return _empty_caps(), formatted_error
    return parse_caps(response), None
