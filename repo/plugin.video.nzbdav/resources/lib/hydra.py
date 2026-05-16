# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZBHydra2 Newznab API client."""

from datetime import datetime, timezone
from urllib.error import URLError
from urllib.parse import urlencode, urlparse

try:
    from defusedxml import ElementTree as element_tree
    from defusedxml.common import DefusedXmlException as _UnsafeXmlError
except ImportError:  # pragma: no cover - Kodi installs may not bundle defusedxml
    from xml.etree import ElementTree as element_tree

    class _UnsafeXmlError(ValueError):
        """Raised when stdlib fallback rejects DTD/entity declarations."""


import xbmc
import xbmcaddon

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
from resources.lib.indexer_store import load_provider_caps, save_provider_caps
from resources.lib.newznab_caps import fetch_caps
from resources.lib.search_planner import plan_newznab_search

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
addon = xbmcaddon.Addon("plugin.video.nzbdav")


# _format_request_error, _get_text, _calculate_age imported from
# resources.lib.http_util above; definitions removed to eliminate
# hydra.py ↔ prowlarr.py duplication.


def _hydra_unavailable_error(error):
    return "NZBHydra unavailable: {}".format(_format_request_error(error))


def _get_settings(settings_getter=None):
    """Read NZBHydra settings from Kodi addon config."""
    if settings_getter is not None:
        configured_url = settings_getter("hydra_url", "").rstrip("/")
        api_key = settings_getter("hydra_api_key", "")
        return configured_url, api_key

    configured_url = addon.getSetting("hydra_url").rstrip("/")
    api_key = addon.getSetting("hydra_api_key")
    return configured_url, api_key


def refresh_hydra_caps(base_url, api_key):
    """Fetch and cache NZBHydra2's public Newznab caps."""
    caps, error = fetch_caps(base_url, api_key)
    if error:
        return caps, error

    providers = load_provider_caps()
    providers["nzbhydra2"] = {
        "base_url": str(base_url or "").rstrip("/"),
        "checked_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "caps": caps,
    }
    save_provider_caps(providers)
    return caps, None


def _hydra_provider_caps(base_url):
    """Return cached Hydra provider caps for the current base URL."""
    providers = load_provider_caps()
    if "nzbhydra2" not in providers:
        return {}, "missing"
    provider = providers.get("nzbhydra2", {})
    if not isinstance(provider, dict):
        return {}, "missing"
    stored_base_url = str(provider.get("base_url") or "").rstrip("/")
    current_base_url = str(base_url or "").rstrip("/")
    if stored_base_url != current_base_url:
        return {}, "mismatch"
    caps = provider.get("caps")
    return (caps, "current") if isinstance(caps, dict) else ({}, "missing")


def _get_hydra_caps_for_search(base_url, api_key):
    caps, cache_status = _hydra_provider_caps(base_url)
    if cache_status == "current" and caps.get("search_types"):
        return caps, True
    if cache_status == "mismatch":
        return {}, False
    refreshed_caps, error = refresh_hydra_caps(base_url, api_key)
    if error:
        xbmc.log(
            "NZB-DAV: Hydra caps refresh failed before search: {}".format(error),
            xbmc.LOGDEBUG,
        )
        return {}, False
    return refreshed_caps, bool(refreshed_caps.get("search_types"))


def _fetch_hydra_xml(request_url, error_prefix):
    """Fetch XML from Hydra and normalize network/runtime failures."""
    try:
        return _http_get(request_url, timeout=300), None
    except (URLError,) + _HYDRA_REQUEST_ERRORS as error:
        # HTTPError/URLError str() can echo the failing URL (which embeds
        # the indexer's apikey query param) back into the log. Redact
        # before logging — same defense as the prowlarr fallback path.
        # TODO.md §H.2-H2e / §H.3.
        from resources.lib.http_util import redact_text

        xbmc.log(
            "NZB-DAV: {}: {}".format(error_prefix, redact_text(str(error))),
            xbmc.LOGERROR,
        )
        return None, _hydra_unavailable_error(error)


def _search_url(base_url, params):
    return "{}/api?{}".format(base_url, urlencode(params))


def _fetch_planned_hydra_results(base_url, params, error_prefix):
    xml_text, request_error = _fetch_hydra_xml(
        _search_url(base_url, params), error_prefix
    )
    if request_error:
        return [], request_error

    results, parse_error = _parse_results_checked(xml_text)
    if parse_error:
        return [], parse_error
    return results, None


def _legacy_hydra_title_fallback(primary, title):
    if not title:
        return None

    fallback = dict(primary)
    fallback.pop("imdbid", None)
    fallback["q"] = title
    return fallback


def search_hydra(
    search_type,
    title,
    year="",
    imdb="",
    season="",
    episode="",
    settings_getter=None,
):
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
        Reads NZBHydra settings from Kodi via xbmcaddon.Addon("plugin.video.nzbdav").
        Performs one or two HTTP GET requests to NZBHydra2 (fallback by title
        when an imdb-based search returns no results).
        Logs search URLs and errors to the Kodi log.
    """
    try:
        base_url, api_key = _get_settings(settings_getter)
    except _HYDRA_REQUEST_ERRORS as error:
        xbmc.log(
            "NZB-DAV: Failed to read Hydra settings: {}".format(error), xbmc.LOGERROR
        )
        return [], "Failed to read NZBHydra settings"

    # `max_results` is exposed via Kodi's number input but the addon also
    # ships with old user profiles that may have the setting as a non-
    # numeric string (legacy text input, hand-edited XML). Guard the int
    # conversion + clamp to a sensible range — TODO.md §H.2-M20 / §H.3.
    if settings_getter is not None:
        raw_max = settings_getter("max_results", "25")
    else:
        raw_max = addon.getSetting("max_results")
    try:
        max_results = int(raw_max) if raw_max not in (None, "") else 25
    except (TypeError, ValueError):
        max_results = 25
    max_results = max(1, min(max_results, 10000))
    caps, has_provider_caps = _get_hydra_caps_for_search(base_url, api_key)
    plan = plan_newznab_search(
        provider_kind="nzbhydra2",
        host=base_url,
        search_type=search_type,
        title=title,
        year=year,
        imdb=imdb,
        season=season,
        episode=episode,
        caps=caps,
        api_key=api_key,
        max_results=max_results,
    )
    if not plan.primary:
        xbmc.log(
            "NZB-DAV: Hydra search skipped: no supported query for '{}'".format(title),
            xbmc.LOGINFO,
        )
        return [], None

    primary_url = _search_url(base_url, plan.primary)
    from resources.lib.http_util import redact_url

    xbmc.log(
        "NZB-DAV: Hydra search URL: {}".format(redact_url(primary_url)), xbmc.LOGDEBUG
    )

    results, error = _fetch_planned_hydra_results(
        base_url, plan.primary, "Hydra search request failed"
    )
    if error:
        return [], error

    fallback = (
        plan.fallback
        if has_provider_caps
        else _legacy_hydra_title_fallback(plan.primary, title)
    )
    if not results and fallback and fallback != plan.primary:
        xbmc.log(
            "NZB-DAV: No results with primary Hydra query, retrying fallback",
            xbmc.LOGINFO,
        )
        fallback_url = _search_url(base_url, fallback)
        xbmc.log(
            "NZB-DAV: Hydra fallback URL: {}".format(redact_url(fallback_url)),
            xbmc.LOGDEBUG,
        )
        results, error = _fetch_planned_hydra_results(
            base_url, fallback, "Hydra fallback search failed"
        )
        if error:
            return [], error

    xbmc.log(
        "NZB-DAV: Hydra returned {} results for '{}'".format(len(results), title),
        xbmc.LOGINFO,
    )
    return results, None


def parse_results(xml_text):
    """Parse Newznab XML response into a list of result dicts."""
    results, _ = _parse_results_checked(xml_text)
    return results


def fetch_release_duplicate_uploads(picked, settings_getter=None):
    """Return all Usenet uploads that share ``picked``'s release title.

    NZBHydra2's standard Newznab endpoint deduplicates results so the
    runtime picker only sees one row per release group. This calls the
    internal API with ``showSingleResultPerSearchResultGroup=false`` and
    keeps rows whose exact ``title`` matches ``picked``'s, so the
    resolver's fallback worker has real same-release/different-upload
    peers to feed nzbdav-rs ahead of the first article failure.
    """
    import json as _json
    from urllib.error import HTTPError as _HTTPError
    from urllib.error import URLError as _URLError
    from urllib.request import Request as _Request
    from urllib.request import urlopen as _urlopen

    try:
        base_url, _api_key = _get_settings(settings_getter)
    except _HYDRA_REQUEST_ERRORS:
        return []
    if not base_url:
        return []
    title = picked.get("title", "") if isinstance(picked, dict) else ""
    if not title:
        return []

    payload = {
        "query": title,
        "mode": "search",
        "showSingleResultPerSearchResultGroup": False,
        "loadAll": True,
    }
    body = _json.dumps(payload).encode("utf-8")
    request = _Request(
        "{}/internalapi/search".format(base_url),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # nosemgrep
        with _urlopen(request, timeout=30) as response:  # nosec B310
            data = _json.load(response)
    except (_HTTPError, _URLError, OSError, ValueError) as error:
        xbmc.log(
            "NZB-DAV: Hydra duplicate-uploads lookup failed: {}".format(
                _format_request_error(error)
            ),
            xbmc.LOGDEBUG,
        )
        return []

    raw_results = data.get("searchResults", []) if isinstance(data, dict) else []
    picked_link = picked.get("link", "") if isinstance(picked, dict) else ""
    uploads = []
    for raw in raw_results:
        if not isinstance(raw, dict):
            continue
        if raw.get("title", "") != title:
            continue
        link = raw.get("link", "") or ""
        if not link or link == picked_link:
            continue
        # Hydra's internal API uses different field names than the public
        # Newznab one. Normalize back into the addon's standard result
        # shape so downstream filter/profile/peer code is unchanged.
        uploads.append(
            {
                "title": raw.get("title", ""),
                "link": link,
                "size": raw.get("size", "") or "",
                "indexer": raw.get("indexer", "") or "",
                "pubdate": "",
                "age": "",
            }
        )
    return uploads


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


def _contains_xml_declaration_markup(xml_text):
    """Return true when XML text declares a DTD/entity block."""
    if isinstance(xml_text, bytes):
        probe = xml_text.lower()
        return b"<!doctype" in probe or b"<!entity" in probe
    probe = str(xml_text).lower()
    return "<!doctype" in probe or "<!entity" in probe


def _parse_hydra_xml(xml_text):
    """Parse Hydra XML with DTD/entity expansion disabled."""
    if getattr(element_tree, "__name__", "").startswith("defusedxml."):
        return element_tree.fromstring(xml_text)
    if _contains_xml_declaration_markup(xml_text):
        raise _UnsafeXmlError("DTD and entity declarations are not allowed")
    return element_tree.fromstring(
        xml_text, parser=_build_xxe_safe_parser()
    )  # nosec B314 — declarations rejected above and entities disabled when possible


def _parse_results_checked(xml_text):
    """Parse Newznab XML and return (results, error_message)."""
    try:
        root = _parse_hydra_xml(xml_text)
    except (element_tree.ParseError, _UnsafeXmlError) as error:
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

    # Scope to <channel><item> rather than `root.iter("item")` so a
    # nested <item> inside e.g. an <atom:link> extension element doesn't
    # get picked up as a search result. Newznab feeds put <item>s in
    # <channel> by spec; falling back to root.iter for malformed feeds
    # used to silently include junk results. TODO.md §H.2-M21.
    items = []
    for channel in root.findall("channel"):
        items.extend(channel.findall("item"))
    return [_build_result(item) for item in items], None


# _get_text and _calculate_age imported from resources.lib.http_util.
