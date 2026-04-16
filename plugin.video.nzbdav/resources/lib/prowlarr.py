# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Prowlarr Newznab-compatible API client."""

import xml.etree.ElementTree as ET
from urllib.error import URLError
from urllib.parse import urlencode

import xbmc

from resources.lib.http_util import http_get as _http_get

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"


def _format_request_error(error):
    reason = getattr(error, "reason", None)
    if reason:
        return str(reason)
    return str(error)


def _prowlarr_unavailable_error(error):
    return "Prowlarr unavailable: {}".format(_format_request_error(error))


def _get_settings():
    """Read Prowlarr settings from Kodi addon config."""
    import xbmcaddon

    addon = xbmcaddon.Addon()
    host = addon.getSetting("prowlarr_host").rstrip("/")
    api_key = addon.getSetting("prowlarr_api_key")
    indexer_ids_raw = addon.getSetting("prowlarr_indexer_ids").strip()
    indexer_ids = [
        s.strip() for s in indexer_ids_raw.split(",") if s.strip().isdigit()
    ]
    return host, api_key, indexer_ids


def _build_search_url(host, api_key, params, indexer_ids):
    """Build Prowlarr search URL with repeated indexerIds params."""
    base_qs = urlencode(params)
    if indexer_ids:
        id_qs = "&".join("indexerIds={}".format(i) for i in indexer_ids)
        return "{}/api/v1/search?{}&{}".format(host, base_qs, id_qs)
    return "{}/api/v1/search?{}".format(host, base_qs)


def search_prowlarr(search_type, title, year="", imdb="", season="", episode=""):
    """Search Prowlarr for NZB entries.

    Returns ([], None) immediately when no indexer IDs are configured —
    the user must opt indexers in before Prowlarr returns results.

    Returns:
        A tuple of (results, error_message). results is a list of dicts with
        keys: title, link, size, indexer, pubdate, age. error_message is None
        on success or a short string describing the failure.
    """
    try:
        host, api_key, indexer_ids = _get_settings()
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Failed to read Prowlarr settings: {}".format(e), xbmc.LOGERROR
        )
        return [], "Failed to read Prowlarr settings"

    if not host:
        return [], None

    if not indexer_ids:
        xbmc.log(
            "NZB-DAV: Prowlarr: no indexer IDs configured, skipping search",
            xbmc.LOGDEBUG,
        )
        return [], None

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

    from resources.lib.http_util import redact_url

    url = _build_search_url(host, api_key, params, indexer_ids)
    xbmc.log(
        "NZB-DAV: Prowlarr search URL: {}".format(redact_url(url)), xbmc.LOGDEBUG
    )

    try:
        xml_text = _http_get(url)
    except (URLError, Exception) as e:
        xbmc.log(
            "NZB-DAV: Prowlarr search request failed: {}".format(e), xbmc.LOGERROR
        )
        return [], _prowlarr_unavailable_error(e)

    results, parse_error = _parse_results_checked(xml_text)
    if parse_error:
        return [], parse_error

    # Fallback: if IMDB search returned nothing, retry with title
    if not results and imdb and title:
        xbmc.log(
            "NZB-DAV: Prowlarr: no results with imdbid={}, retrying with title '{}'".format(
                imdb, title
            ),
            xbmc.LOGINFO,
        )
        params.pop("imdbid", None)
        params["q"] = title
        fallback_url = _build_search_url(host, api_key, params, indexer_ids)
        try:
            xml_text = _http_get(fallback_url)
            results, parse_error = _parse_results_checked(xml_text)
            if parse_error:
                return [], parse_error
        except (URLError, Exception) as e:
            xbmc.log(
                "NZB-DAV: Prowlarr title fallback failed: {}".format(e), xbmc.LOGERROR
            )
            return [], _prowlarr_unavailable_error(e)

    xbmc.log(
        "NZB-DAV: Prowlarr returned {} results for '{}'".format(len(results), title),
        xbmc.LOGINFO,
    )
    return results, None


def parse_results(xml_text):
    """Parse Newznab XML response into a list of result dicts."""
    results, _ = _parse_results_checked(xml_text)
    return results


def _parse_results_checked(xml_text):
    """Parse Newznab XML and return (results, error_message)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        xbmc.log(
            "NZB-DAV: Failed to parse Prowlarr XML response: {}".format(e),
            xbmc.LOGERROR,
        )
        return [], "Prowlarr returned an invalid response: {}".format(e)

    if root.tag != "rss":
        xbmc.log(
            "NZB-DAV: Unexpected Prowlarr XML root: {}".format(root.tag), xbmc.LOGERROR
        )
        return [], "Prowlarr returned an invalid response: expected RSS feed"

    results = []
    for item in root.iter("item"):
        title = _get_text(item, "title")
        link = _get_text(item, "link")
        pubdate = _get_text(item, "pubDate")

        size = ""
        indexer = ""
        for attr in item.iter("{%s}attr" % NEWZNAB_NS):
            name = attr.get("name", "")
            if name == "size":
                size = attr.get("value", "")
            elif name in ("indexer", "source", "prowlarrindexer"):
                if not indexer:
                    indexer = attr.get("value", "")

        if not indexer:
            indexer = _get_text(item, "source") or ""

        if not size:
            enclosure = item.find("enclosure")
            if enclosure is not None:
                size = enclosure.get("length", "")

        if not link:
            enclosure = item.find("enclosure")
            if enclosure is not None:
                link = enclosure.get("url", "")

        age = _calculate_age(pubdate) if pubdate else ""

        results.append(
            {
                "title": title or "",
                "link": link or "",
                "size": size,
                "indexer": indexer,
                "pubdate": pubdate or "",
                "age": age,
            }
        )

    return results, None


def _get_text(element, tag):
    child = element.find(tag)
    if child is not None and child.text:
        return child.text
    return ""


def _calculate_age(pubdate_str):
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
    except Exception:
        return ""
