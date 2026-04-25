# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZBHydra2 Newznab API client."""

from urllib.error import URLError
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree as element_tree

import xbmc

from resources.lib.http_util import (
    calculate_age as _calculate_age,
)
from resources.lib.http_util import (
    format_request_error as _format_request_error,
)
from resources.lib.http_util import (
    get_xml_text as _get_text,
)
from resources.lib.http_util import (
    http_get as _http_get,
)

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"
_HYDRA_REQUEST_ERRORS = (
    AttributeError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)
_SOURCE_URL_ERRORS = (AttributeError, TypeError, ValueError)
_PUBDATE_ERRORS = (OverflowError, TypeError, ValueError)


# _format_request_error, _get_text, _calculate_age imported from
# resources.lib.http_util above; definitions removed to eliminate
# hydra.py ↔ prowlarr.py duplication.


def _hydra_unavailable_error(error):
    return "NZBHydra unavailable: {}".format(_format_request_error(error))


def _get_settings():
    """Read NZBHydra settings from Kodi addon config."""
    import xbmcaddon

    addon = xbmcaddon.Addon()
    url = addon.getSetting("hydra_url").rstrip("/")
    api_key = addon.getSetting("hydra_api_key")
    return url, api_key


def _fetch_hydra_xml(url, error_prefix):
    """Fetch XML from Hydra and normalize network/runtime failures."""
    try:
        return _http_get(url, timeout=15), None
    except (URLError,) + _HYDRA_REQUEST_ERRORS as error:
        xbmc.log("NZB-DAV: {}: {}".format(error_prefix, error), xbmc.LOGERROR)
        return None, _hydra_unavailable_error(error)


def search_hydra(search_type, title, year="", imdb="", season="", episode=""):
    """Search NZBHydra2 for NZB entries.

    Args:
        search_type: Either "movie" or "episode" to select the Newznab query.
        title: Movie or show title used when imdb is not provided.
        year: Release year for movie searches (optional).
        imdb: IMDb ID such as "tt0133093" (preferred when available).
        season: Season number for TV searches (optional).
        episode: Episode number for TV searches (optional).

    Returns:
        A tuple of (results, error_message). results is a list of dicts with
        keys: title, link, size, indexer, pubdate, age. error_message is None
        on success or a short string describing the failure.

    Side effects:
        Reads NZBHydra settings from Kodi via xbmcaddon.Addon().
        Performs one or two HTTP GET requests to NZBHydra2 (fallback by title
        when an imdb-based search returns no results).
        Logs search URLs and errors to the Kodi log.
    """
    try:
        base_url, api_key = _get_settings()
    except _HYDRA_REQUEST_ERRORS as error:
        xbmc.log(
            "NZB-DAV: Failed to read Hydra settings: {}".format(error), xbmc.LOGERROR
        )
        return [], "Failed to read NZBHydra settings"

    import xbmcaddon

    max_results = int(xbmcaddon.Addon().getSetting("max_results") or 25)
    params = {"apikey": api_key, "o": "xml", "limit": max_results}

    if search_type == "episode":
        params["t"] = "tvsearch"
        if imdb:
            params["imdbid"] = imdb
        else:
            params["q"] = title
        if season:
            params["season"] = season
        if episode:
            params["ep"] = episode
    else:
        params["t"] = "movie"
        if imdb:
            params["imdbid"] = imdb
        else:
            params["q"] = title

    url = "{}/api?{}".format(base_url, urlencode(params))
    from resources.lib.http_util import redact_url

    xbmc.log("NZB-DAV: Hydra search URL: {}".format(redact_url(url)), xbmc.LOGDEBUG)

    xml_text, request_error = _fetch_hydra_xml(url, "Hydra search request failed")
    if request_error:
        return [], request_error

    results, parse_error = _parse_results_checked(xml_text)
    if parse_error:
        return [], parse_error

    # Fallback: if IMDB search returned nothing, retry with title
    if not results and imdb and title:
        xbmc.log(
            "NZB-DAV: No results with imdbid={}, retrying with title '{}'".format(
                imdb, title
            ),
            xbmc.LOGINFO,
        )
        params.pop("imdbid", None)
        params["q"] = title
        fallback_url = "{}/api?{}".format(base_url, urlencode(params))
        xml_text, request_error = _fetch_hydra_xml(
            fallback_url, "Hydra title fallback failed"
        )
        if request_error:
            return [], request_error
        results, parse_error = _parse_results_checked(xml_text)
        if parse_error:
            return [], parse_error

    xbmc.log(
        "NZB-DAV: Hydra returned {} results for '{}'".format(len(results), title),
        xbmc.LOGINFO,
    )
    return results, None


def parse_results(xml_text):
    """Parse Newznab XML response into a list of result dicts."""
    results, _ = _parse_results_checked(xml_text)
    return results


def _source_url_hostname(source_url):
    """Extract a hostname from a Hydra <source url=\"...\"> fallback."""
    if not source_url:
        return ""
    if "/" not in source_url:
        return source_url
    try:
        return urlparse(source_url).hostname or ""
    except _SOURCE_URL_ERRORS:
        return ""


def _parse_newznab_attrs(item):
    """Return (size, indexer) from Newznab attributes on an item.

    The hardcoded ``{NEWZNAB_NS}attr`` lookup misses RSS variants that use
    a different default namespace, no namespace at all, or a custom
    Newznab URI. Iterate every descendant and match by local-name so spec
    drift between NZBHydra2 versions doesn't silently zero out
    ``size``/``indexer`` columns.
    """
    size = ""
    indexer = ""
    for attr in item.iter():
        # `tag` is either ``"{ns}local"`` (namespaced) or ``"local"``
        # (unqualified). We only care about the local-name == "attr".
        tag = attr.tag
        if not isinstance(tag, str):
            continue
        local = tag.rsplit("}", 1)[-1]
        if local != "attr":
            continue
        name = attr.get("name", "")
        if name == "size":
            size = attr.get("value", "")
        elif name in ("indexer", "source", "hydraIndexerName") and not indexer:
            indexer = attr.get("value", "")
    return size, indexer


def _resolve_indexer(item, attr_indexer):
    """Resolve the display indexer name for a Hydra result item."""
    if attr_indexer:
        return attr_indexer
    source_text = _get_text(item, "source")
    if source_text:
        return source_text
    source_el = item.find("source")
    if source_el is None:
        return ""
    return _source_url_hostname(source_el.get("url", ""))


def _get_enclosure(item):
    """Return the enclosure element for an item, if present."""
    return item.find("enclosure")


def _build_result(item):
    """Convert one Hydra RSS item into the addon result shape."""
    title = _get_text(item, "title")
    link = _get_text(item, "link")
    pubdate = _get_text(item, "pubDate")
    size, attr_indexer = _parse_newznab_attrs(item)
    indexer = _resolve_indexer(item, attr_indexer)
    enclosure = _get_enclosure(item)

    if enclosure is not None:
        if not size:
            size = enclosure.get("length", "")
        if not link:
            link = enclosure.get("url", "")

    return {
        "title": title or "",
        "link": link or "",
        "size": size,
        "indexer": indexer,
        "pubdate": pubdate or "",
        "age": _calculate_age(pubdate) if pubdate else "",
    }


def _build_xxe_safe_parser():
    """Return an ElementTree XMLParser with external entities disabled.

    ``xml.etree.ElementTree`` doesn't expose a ``resolve_entities=False``
    knob directly, but the underlying expat parser can be told to
    ignore DefaultHandler output and reject ExternalEntityRef callbacks.
    A hostile NZBHydra2 instance (compromised, MITM'd, or simply
    misbehaving) could otherwise coerce us into reading arbitrary
    local files via an XXE payload. Mirrors webdav.py's WebDAV
    PROPFIND parser for defense in depth.
    """
    parser = element_tree.XMLParser()  # nosec B314 — entities disabled below
    try:
        parser.parser.DefaultHandler = lambda _d: None
        parser.parser.ExternalEntityRefHandler = lambda *_: False
    except AttributeError:  # pragma: no cover — non-expat parser backend
        pass
    return parser


def _parse_results_checked(xml_text):
    """Parse Newznab XML and return (results, error_message)."""
    try:
        root = element_tree.fromstring(
            xml_text, parser=_build_xxe_safe_parser()
        )  # nosec B314 — entities disabled in _build_xxe_safe_parser
    except element_tree.ParseError as error:
        xbmc.log(
            "NZB-DAV: Failed to parse Hydra XML response: {}".format(error),
            xbmc.LOGERROR,
        )
        return [], "NZBHydra returned an invalid response: {}".format(error)

    if root.tag != "rss":
        xbmc.log(
            "NZB-DAV: Unexpected Hydra XML root: {}".format(root.tag), xbmc.LOGERROR
        )
        return [], "NZBHydra returned an invalid response: expected RSS feed"

    return [_build_result(item) for item in root.iter("item")], None


# _get_text and _calculate_age imported from resources.lib.http_util.
