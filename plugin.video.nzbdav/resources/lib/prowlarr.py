# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Prowlarr Newznab API client."""

import xml.etree.ElementTree as ET  # nosec B405 — parsing trusted Prowlarr responses from user-configured local service
from urllib.parse import urlencode, urlparse

import xbmc

from resources.lib.http_util import http_get as _http_get

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"


def _format_request_error(error):
    """
    Extract a concise message from an exception or error-like object.

    Parameters:
        error: An exception or object that may have a `reason` attribute.

    Returns:
        str: The extracted message — `str(error.reason)` if `error.reason`
            is present and truthy, otherwise `str(error)`.
    """
    reason = getattr(error, "reason", None)
    if reason:
        return str(reason)
    return str(error)


def _prowlarr_unavailable_error(error):
    """
    Format an error into a standardized "Prowlarr unavailable" message.

    Parameters:
        error (Exception|object): The error or response failure to report;
            its message or reason will be included.

    Returns:
        str: A message starting with "Prowlarr unavailable: " followed by
            the extracted error reason.
    """
    return "Prowlarr unavailable: {}".format(_format_request_error(error))


def _get_settings():
    """
    Load Prowlarr connection settings from the Kodi addon configuration.

    Returns:
        tuple: (host, api_key, indexer_ids)
            - host: base URL for Prowlarr with any trailing '/' removed.
            - api_key: API key string (may be empty).
            - indexer_ids: list of configured indexer ID strings; each ID is
                trimmed and empty entries are omitted.
    """
    import xbmcaddon

    addon = xbmcaddon.Addon()
    host = addon.getSetting("prowlarr_host").rstrip("/")
    api_key = addon.getSetting("prowlarr_api_key")
    ids_raw = addon.getSetting("prowlarr_indexer_ids").strip()
    indexer_ids = [i.strip() for i in ids_raw.split(",") if i.strip()]
    return host, api_key, indexer_ids


def _build_search_url(base_url, params, indexer_ids):
    """Build a Prowlarr /api/v1/search URL with encoded params and indexer IDs.

    All values — including each repeated ``indexerIds`` — go through
    ``urlencode(doseq=True)`` so indexer IDs with URL-special characters
    (``&``, ``=``, ``%``, space) can't corrupt the query string.
    """
    combined = list(params.items())
    for idx_id in indexer_ids:
        combined.append(("indexerIds", idx_id))
    query = urlencode(combined, doseq=True)
    return "{}/api/v1/search?{}".format(base_url, query)


def search_prowlarr(search_type, title, year="", imdb="", season="", episode=""):
    """
    Search Prowlarr for NZB results matching a movie or TV episode.

    Parameters:
        search_type (str): "movie" or "episode".
        title (str): Movie or show title used when `imdb` is not provided.
        year (str, optional): Release year; kept for API symmetry and not
            used by Prowlarr.
        imdb (str, optional): IMDb ID (e.g., "tt0133093"); used in preference
            to `title` when present.
        season (str, optional): Season number for TV searches.
        episode (str, optional): Episode number for TV searches.

    Returns:
        tuple: `(results, error_message)` where `results` is a list of dicts
            with keys `title`, `link`, `size`, `indexer`, `pubdate`, `age`;
            and `error_message` is `None` on success or a short string
            describing the failure. Returns `([], None)` when Prowlarr is
            enabled but no indexer IDs are configured.
    """
    try:
        base_url, api_key, indexer_ids = _get_settings()
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Failed to read Prowlarr settings: {}".format(e), xbmc.LOGERROR
        )
        return [], "Failed to read Prowlarr settings"

    if not indexer_ids:
        xbmc.log(
            "NZB-DAV: Prowlarr: no indexer IDs configured, skipping search",
            xbmc.LOGINFO,
        )
        return [], None

    import xbmcaddon

    max_results = int(xbmcaddon.Addon().getSetting("max_results") or 25)
    params = {"apikey": api_key, "limit": max_results}

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

    url = _build_search_url(base_url, params, indexer_ids)

    xbmc.log("NZB-DAV: Prowlarr search URL: {}".format(redact_url(url)), xbmc.LOGDEBUG)

    try:
        xml_text = _http_get(url)
    except Exception as e:
        xbmc.log("NZB-DAV: Prowlarr search request failed: {}".format(e), xbmc.LOGERROR)
        return [], _prowlarr_unavailable_error(e)

    results, parse_error = _parse_results_checked(xml_text)
    if parse_error:
        return [], parse_error

    # Fallback: if IMDB search returned nothing, retry with title
    if not results and imdb and title:
        xbmc.log(
            "NZB-DAV: Prowlarr: no results with imdbid={}, retrying with "
            "title '{}'".format(imdb, title),
            xbmc.LOGINFO,
        )
        params.pop("imdbid", None)
        params["q"] = title
        fallback_url = _build_search_url(base_url, params, indexer_ids)
        try:
            xml_text = _http_get(fallback_url)
            results, parse_error = _parse_results_checked(xml_text)
            if parse_error:
                return [], parse_error
        except Exception as e:
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
    """
    Convert Newznab RSS/XML into a list of normalized result dictionaries.

    Parameters:
        xml_text (str): The raw XML/RSS response from a Newznab-compatible indexer.

    Returns:
        list[dict]: A list of result dictionaries. Each dictionary contains:
            - title (str): Item title or empty string.
            - link (str): Download/link URL or empty string.
            - size (str): Size in bytes as a string or empty string.
            - indexer (str): Name of the indexer/source or empty string.
            - pubdate (str): Original pubDate string or empty string.
            - age (str): Human-readable age (e.g., "today", "1 day",
                "3 months") or empty string.
    """
    results, _ = _parse_results_checked(xml_text)
    return results


def _parse_results_checked(xml_text):
    """
    Parse a Prowlarr Newznab RSS XML response into a list of normalized
    result dictionaries.

    Parses the provided RSS/XML text and extracts each <item> into a dict with keys:
    `title`, `link`, `size`, `indexer`, `pubdate`, and `age`. Attempts to read size
    and indexer information from Newznab `<attr>` elements, falls back to
    `<enclosure>` and `<source>` elements when available, and computes a human-
    readable `age` from `pubDate`.

    Returns:
        results (list): List of dicts for each item. Each dict contains:
            - title (str): Item title (empty string if missing).
            - link (str): Download/link URL (empty string if missing).
            - size (str): Size in bytes as reported or empty string.
            - indexer (str): Indexer/source name or hostname, or empty string.
            - pubdate (str): Original pubDate text or empty string.
            - age (str): Human-readable age (e.g., "today", "3 days",
                "2 months") or empty string.
        error_message (str or None): Error description when the XML is
            invalid or not an RSS feed; `None` on success.
    """
    try:
        root = ET.fromstring(xml_text)  # nosec B314 — trusted Prowlarr response
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
            elif name in ("indexer", "source", "hydraIndexerName"):
                if not indexer:
                    indexer = attr.get("value", "")

        if not indexer:
            indexer = _get_text(item, "source")
        if not indexer:
            source_el = item.find("source")
            if source_el is not None:
                indexer = source_el.get("url", "")
                if indexer and "/" in indexer:
                    try:
                        indexer = urlparse(indexer).hostname or ""
                    except Exception:
                        indexer = ""

        enclosure = item.find("enclosure")
        if enclosure is not None:
            if not size:
                size = enclosure.get("length", "")
            if not link:
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
    """
    Return the text content of the first matching child element or an empty string.

    Parameters:
        element (xml.etree.ElementTree.Element): Parent XML element to search within.
        tag (str): Tag name of the child element to find.

    Returns:
        str: The child element's text if present and non-empty, otherwise
            an empty string.
    """
    child = element.find(tag)
    if child is not None and child.text:
        return child.text
    return ""


def _calculate_age(pubdate_str):
    """
    Return a human-readable age string computed from an RFC 2822 date-time.

    Parameters:
        pubdate_str (str): RFC 2822 formatted date-time string
            (e.g., 'Mon, 02 Jan 2006 15:04:05 -0700').

    Returns:
        str: Age as 'today', '1 day', '<n> days', '1 month', '<n> months',
            or an empty string if the input cannot be parsed.
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
    except (OverflowError, TypeError, ValueError):
        return ""
