# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZBHydra2 Newznab API client."""

import xml.etree.ElementTree as ET
from urllib.error import URLError
from urllib.parse import urlencode

import xbmc

from resources.lib.http_util import http_get as _http_get

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"


def _get_settings():
    """Read NZBHydra settings from Kodi addon config."""
    import xbmcaddon

    addon = xbmcaddon.Addon()
    url = addon.getSetting("hydra_url").rstrip("/")
    api_key = addon.getSetting("hydra_api_key")
    return url, api_key


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
    except Exception as e:
        xbmc.log("NZB-DAV: Failed to read Hydra settings: {}".format(e), xbmc.LOGERROR)
        return [], "Failed to read NZBHydra settings"

    params = {"apikey": api_key, "o": "xml"}

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

    try:
        xml_text = _http_get(url)
    except (URLError, Exception) as e:
        xbmc.log("NZB-DAV: Hydra search request failed: {}".format(e), xbmc.LOGERROR)
        return [], "Search failed: {}".format(str(e)[:80])

    results = parse_results(xml_text)

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
        try:
            xml_text = _http_get(fallback_url)
            results = parse_results(xml_text)
        except (URLError, Exception) as e:
            xbmc.log(
                "NZB-DAV: Hydra title fallback failed: {}".format(e), xbmc.LOGERROR
            )
            return [], "Search failed: {}".format(str(e)[:80])

    xbmc.log(
        "NZB-DAV: Hydra returned {} results for '{}'".format(len(results), title),
        xbmc.LOGINFO,
    )
    return results, None


def parse_results(xml_text):
    """Parse Newznab XML response into a list of result dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        xbmc.log(
            "NZB-DAV: Failed to parse Hydra XML response: {}".format(e), xbmc.LOGERROR
        )
        return []

    results = []
    for item in root.iter("item"):
        title = _get_text(item, "title")
        link = _get_text(item, "link")
        pubdate = _get_text(item, "pubDate")

        # Get size and indexer from newznab attributes
        size = ""
        indexer = ""
        for attr in item.iter("{%s}attr" % NEWZNAB_NS):
            name = attr.get("name", "")
            if name == "size":
                size = attr.get("value", "")
            elif name in ("indexer", "source", "hydraIndexerName"):
                if not indexer:
                    indexer = attr.get("value", "")

        # Fallback: indexer from <source> element or category
        if not indexer:
            indexer = _get_text(item, "source") or ""
        if not indexer:
            source_el = item.find("source")
            if source_el is not None:
                indexer = source_el.get("url", "")
                # Extract domain name from URL
                if indexer and "/" in indexer:
                    try:
                        from urllib.parse import urlparse

                        indexer = urlparse(indexer).hostname or ""
                    except Exception:
                        indexer = ""

        # Fallback: get size from enclosure
        if not size:
            enclosure = item.find("enclosure")
            if enclosure is not None:
                size = enclosure.get("length", "")

        # Fallback: get link from enclosure
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

    return results


def _get_text(element, tag):
    """Get text content of a child element."""
    child = element.find(tag)
    if child is not None and child.text:
        return child.text
    return ""


def _calculate_age(pubdate_str):
    """Calculate human-readable age from an RFC 2822 date string."""
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
