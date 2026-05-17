# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Newznab caps parsing and fetching."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import xbmc

from resources.lib.http_util import format_request_error
from resources.lib.http_util import http_get as _http_get

_REQUEST_ERRORS = (AttributeError, OSError, RuntimeError, TypeError, ValueError)
_EMPTY_CAPS = {"search_types": [], "supported_params": {}, "categories": []}
CAPS_MAX_BYTES = 1024 * 1024
_SEARCH_TAGS = {
    "search": "search",
    "tv-search": "tvsearch",
    "movie-search": "movie",
    "audio-search": "audio",
    "book-search": "book",
}


class UnsafeNewznabXmlError(ValueError):
    """Raised when an untrusted Newznab XML response declares DTD/entities."""


def normalize_api_endpoint(api_url):
    """Return a Newznab API endpoint from either a host URL or endpoint URL."""
    parts = urlsplit(str(api_url or ""))
    path = parts.path.rstrip("/")
    if not path:
        path = "/api"
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            path,
            parts.query,
            parts.fragment,
        )
    )


def build_caps_url(api_url, api_key):
    parts = urlsplit(normalize_api_endpoint(api_url))
    query_str = parts.query
    if not query_str:
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode((("apikey", api_key), ("t", "caps"), ("o", "xml"))),
                parts.fragment,
            )
        )
    query = [
        (key, value)
        for key, value in parse_qsl(query_str, keep_blank_values=True)
        if key.lower() not in ("apikey", "t", "o")
    ]
    query.extend((("apikey", api_key), ("t", "caps"), ("o", "xml")))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


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


def _contains_xml_declaration_markup(xml_text):
    if isinstance(xml_text, bytes):
        probe = xml_text.lower()
        return b"<!doctype" in probe or b"<!entity" in probe
    probe = str(xml_text).lower()
    return "<!doctype" in probe or "<!entity" in probe


def _build_xxe_safe_parser():
    parser = ET.XMLParser()  # nosec B314 - declarations rejected below
    try:
        parser.parser.DefaultHandler = lambda _d: None
        parser.parser.ExternalEntityRefHandler = lambda *_: False
    except AttributeError:  # pragma: no cover - non-expat parser backend
        pass
    return parser


def parse_untrusted_newznab_xml(xml_text):
    if _contains_xml_declaration_markup(xml_text):
        raise UnsafeNewznabXmlError("DTD and entity declarations are not allowed")
    return ET.fromstring(xml_text, parser=_build_xxe_safe_parser())  # nosec B314


def parse_caps(xml_text):
    try:
        root = parse_untrusted_newznab_xml(xml_text)
    except (ET.ParseError, TypeError, UnsafeNewznabXmlError):
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
        response = _http_get(url, timeout=timeout, max_bytes=CAPS_MAX_BYTES)
    except _REQUEST_ERRORS as error:
        formatted_error = format_request_error(error)
        xbmc.log(
            "NZB-DAV: Newznab caps fetch failed: {}".format(formatted_error),
            xbmc.LOGWARNING,
        )
        return _empty_caps(), formatted_error
    return parse_caps(response), None
