# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Prowlarr search client.

Prowlarr's native ``/api/v1/search`` endpoint (the one this client calls,
with the Prowlarr API key + ``indexerIds``) returns a **JSON** array of
release objects — not Newznab RSS/XML. Historically this module parsed the
response as XML, which crashed with ``syntax error: line 1, column 0`` the
moment Prowlarr answered (issue #313). The parser now sniffs the payload and
routes JSON to ``_parse_json_results`` while still understanding Newznab XML
(e.g. a reverse-proxied per-indexer ``/{id}/api`` feed) via the legacy path.
"""

import json
import xml.etree.ElementTree as ET  # nosec B405 — parsing trusted Prowlarr responses from user-configured local service
from urllib.parse import urlencode, urlparse

import xbmc

from resources.lib.http_util import (
    age_string_from_days as _age_from_days,
)
from resources.lib.http_util import (
    calculate_age as _calculate_age,
)
from resources.lib.http_util import (
    clean_search_query as _clean_search_query,
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
from resources.lib.http_util import (
    iso8601_to_rfc2822 as _iso_to_rfc2822,
)

NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"


def _build_xxe_safe_parser():
    """Return an ElementTree XMLParser with external entities disabled.

    Mirrors ``hydra._build_xxe_safe_parser``. ``xml.etree.ElementTree`` does
    not expose a ``resolve_entities=False`` knob, but the underlying expat
    parser can be told to ignore DefaultHandler output and reject
    ExternalEntityRef callbacks. A hostile or compromised Prowlarr instance
    could otherwise coerce us into reading arbitrary local files via an XXE
    payload — same threat model as the NZBHydra2 path, kept on parity here.
    """
    parser = ET.XMLParser()  # nosec B314 — entities disabled below
    try:
        parser.parser.DefaultHandler = lambda _d: None
        parser.parser.ExternalEntityRefHandler = lambda *_: False
    except AttributeError:  # pragma: no cover — non-expat parser backend
        pass
    return parser


# _format_request_error, _get_text, _calculate_age imported from
# resources.lib.http_util above; definitions removed to eliminate
# hydra.py ↔ prowlarr.py duplication.


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


def _get_settings(settings_getter=None):
    """
    Load Prowlarr connection settings from the Kodi addon configuration.

    Returns:
        tuple: (host, api_key, indexer_ids)
            - host: base URL for Prowlarr with any trailing '/' removed.
            - api_key: API key string (may be empty).
            - indexer_ids: list of configured indexer ID strings; each ID is
                trimmed and empty entries are omitted.
    """
    if settings_getter is None:
        import xbmcaddon

        addon = xbmcaddon.Addon("plugin.video.nzbdav")

        def settings_getter(key, default=""):
            return addon.getSetting(key) or default

    host = settings_getter("prowlarr_host", "").rstrip("/")
    api_key = settings_getter("prowlarr_api_key", "")
    ids_raw = settings_getter("prowlarr_indexer_ids", "").strip()
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


def _digits(value):
    """Return only the decimal digits of ``value`` (ids are numeric)."""
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())


def _build_prowlarr_query(search_type, title, imdb="", tvdb="", season="", episode=""):
    """Compose Prowlarr's ``query`` value using its ``{token:value}`` syntax.

    Prowlarr's native ``/api/v1/search`` binds only Query/Type/IndexerIds/
    Categories/Limit/Offset. It extracts ids/season/episode by regex-parsing
    ``{tvdbid:..}`` / ``{imdbid:..}`` / ``{season:..}`` / ``{episode:..}``
    tokens out of the query TEXT (``NewznabRequest.QueryToParams``), and only
    when ``type`` is ``tvsearch``/``movie``. It does NOT bind ``imdbid=`` /
    ``tvdbid=`` / ``season=`` / ``ep=`` query params, so the only way to do an
    id-keyed search is to embed the tokens here (verified against Prowlarr
    source; see issue #313). For TV, tvdbid is preferred over imdbid (#318).
    """
    tokens = []
    if search_type == "episode":
        tvdbid = _digits(tvdb)
        imdbid = _digits(imdb)
        if tvdbid:
            tokens.append("{{tvdbid:{}}}".format(tvdbid))
        elif imdbid:
            tokens.append("{{imdbid:{}}}".format(imdbid))
        if season:
            tokens.append("{{season:{}}}".format(season))
        if episode:
            tokens.append("{{episode:{}}}".format(episode))
    else:
        imdbid = _digits(imdb)
        if imdbid:
            tokens.append("{{imdbid:{}}}".format(imdbid))

    token_str = "".join(tokens)
    # Strip query-breaking '&' from the keyword title (#294): Prowlarr passes
    # the query text through to the indexer, which ANDs each term against
    # release names that spell '&' as "and" or omit it. The {token:value} ids
    # are appended after, so they are never touched.
    text = _clean_search_query(title)
    if text and token_str:
        return "{} {}".format(text, token_str)
    return text or token_str


def search_prowlarr(
    search_type,
    title,
    year="",
    imdb="",
    season="",
    episode="",
    settings_getter=None,
    tvdb="",
):
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
        tvdb (str, optional): TheTVDB series id. For episode searches it is
            preferred over `imdb` (many indexers key TV on tvdbid) — issue
            #318.

    Returns:
        tuple: `(results, error_message)` where `results` is a list of dicts
            with keys `title`, `link`, `size`, `indexer`, `pubdate`, `age`;
            and `error_message` is `None` on success or a short string
            describing the failure. Returns `([], None)` when Prowlarr is
            enabled but no indexer IDs are configured.
    """
    try:
        base_url, api_key, indexer_ids = _get_settings(settings_getter)
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

    if settings_getter is None:
        import xbmcaddon

        raw_max = xbmcaddon.Addon("plugin.video.nzbdav").getSetting("max_results")
    else:
        try:
            raw_max = settings_getter("max_results", "25")
        except Exception:  # pylint: disable=broad-except
            raw_max = "25"
    try:
        max_results = int(raw_max) if raw_max not in (None, "") else 25
    except (TypeError, ValueError):
        max_results = 25
    max_results = max(1, min(max_results, 10000))
    params = {"apikey": api_key, "limit": max_results}

    # Prowlarr's native /api/v1/search binds only Query/Type/IndexerIds/
    # Categories/Limit/Offset. ids/season/episode are NOT query params here —
    # they must be embedded as {token:value} inside `query`, and Prowlarr only
    # parses them when `type` is tvsearch/movie (see _build_prowlarr_query).
    params["type"] = "tvsearch" if search_type == "episode" else "movie"
    params["query"] = _build_prowlarr_query(
        search_type, title, imdb=imdb, tvdb=tvdb, season=season, episode=episode
    )

    from resources.lib.http_util import redact_text, redact_url

    url = _build_search_url(base_url, params, indexer_ids)

    xbmc.log("NZB-DAV: Prowlarr search URL: {}".format(redact_url(url)), xbmc.LOGDEBUG)

    try:
        xml_text = _http_get(url, timeout=300)
    except Exception as e:
        # Redact: HTTPError / URLError str() can echo back the failing URL
        # (which embeds the apikey query param). Mirrors the redaction
        # already in nzbdav_api's submit error path.
        xbmc.log(
            "NZB-DAV: Prowlarr search request failed: {}".format(redact_text(str(e))),
            xbmc.LOGERROR,
        )
        return [], _prowlarr_unavailable_error(e)

    results, parse_error = _parse_results_checked(xml_text)
    if parse_error:
        return [], parse_error

    # Fallback: an id-keyed query returned nothing — the id may be wrong or the
    # indexer may not map it. Retry by title (keeping season/episode tokens for
    # TV), dropping the id token.
    if not results and (tvdb or imdb) and title:
        xbmc.log(
            "NZB-DAV: Prowlarr: no results with id (tvdb={} imdb={}), retrying "
            "by title '{}'".format(tvdb or "-", imdb or "-", title),
            xbmc.LOGINFO,
        )
        params["query"] = _build_prowlarr_query(
            search_type, title, season=season, episode=episode
        )
        fallback_url = _build_search_url(base_url, params, indexer_ids)
        try:
            xml_text = _http_get(fallback_url, timeout=300)
            results, parse_error = _parse_results_checked(xml_text)
            if parse_error:
                return [], parse_error
        except Exception as e:
            xbmc.log(
                "NZB-DAV: Prowlarr title fallback failed: {}".format(
                    redact_text(str(e))
                ),
                xbmc.LOGERROR,
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


def _parse_results_checked(text):
    """Parse a Prowlarr search response into normalized result dicts.

    Prowlarr's native ``/api/v1/search`` returns a JSON array; a
    reverse-proxied per-indexer Newznab feed returns RSS/XML. Sniff the
    first non-whitespace character and dispatch accordingly so both shapes
    work and a server that switches formats can't silently break search.

    Returns ``(results, error_message)`` — ``error_message`` is ``None`` on
    success or a short human-readable string on a malformed/unexpected body.
    """
    stripped = (text or "").lstrip()
    if stripped[:1] in ("[", "{"):
        return _parse_json_results(stripped)
    return _parse_xml_results(text)


def _parse_json_results(text):
    """Parse a Prowlarr native ``/api/v1/search`` JSON array into result dicts.

    Each element is a Prowlarr ``ReleaseResource``. Torrent releases are
    skipped — nzbdav only consumes NZB/usenet downloads — and the remaining
    fields are mapped onto the same ``{title, link, size, indexer, pubdate,
    age}`` shape the XML path produces, so downstream consumers don't care
    which transport answered.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        xbmc.log(
            "NZB-DAV: Failed to parse Prowlarr JSON response: {}".format(e),
            xbmc.LOGERROR,
        )
        return [], "Prowlarr returned an invalid response: {}".format(e)

    if not isinstance(data, list):
        # Prowlarr error bodies are JSON objects ({"error": ...}), not arrays.
        from resources.lib.http_util import redact_text

        message = ""
        if isinstance(data, dict):
            raw_message = data.get("error") or data.get("message") or ""
            message = redact_text(str(raw_message)) if raw_message else ""
        xbmc.log(
            "NZB-DAV: Unexpected Prowlarr JSON payload (not an array): {}".format(
                message or type(data).__name__
            ),
            xbmc.LOGERROR,
        )
        detail = ": {}".format(message) if message else ": expected a JSON array"
        return [], "Prowlarr returned an invalid response{}".format(detail)

    results = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        # nzbdav is usenet-only: keep strictly protocol == "usenet", dropping
        # torrents and any release with a missing/unknown protocol. Prowlarr's
        # ReleaseResource always sets protocol, so this only excludes torrent
        # indexers a user added to their Prowlarr id list (and malformed rows).
        if (entry.get("protocol") or "").lower() != "usenet":
            continue

        size_val = entry.get("size")
        if isinstance(size_val, bool):
            size = ""
        elif isinstance(size_val, int) and size_val > 0:
            size = str(size_val)
        elif isinstance(size_val, str) and size_val.strip().isdigit():
            size = size_val.strip()
        else:
            size = ""

        results.append(
            {
                "title": entry.get("title") or "",
                "link": entry.get("downloadUrl") or "",
                "size": size,
                "indexer": entry.get("indexer") or "",
                # publishDate is ISO-8601; normalize to RFC-2822 so the
                # pubdate_to_epoch / Age-sort consumers can parse it.
                "pubdate": _iso_to_rfc2822(entry.get("publishDate")),
                "age": _age_from_days(entry.get("age")),
            }
        )

    return results, None


def _parse_xml_results(xml_text):
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
        root = ET.fromstring(
            xml_text, parser=_build_xxe_safe_parser()
        )  # nosec B314 — entities disabled in _build_xxe_safe_parser
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
                    except (ValueError, AttributeError):
                        # Narrow from bare Exception — urlparse only
                        # raises these for shape-mismatch input. A
                        # broader catch would hide real bugs in
                        # callers that pass unexpected types.
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


# _get_text and _calculate_age imported from resources.lib.http_util.
