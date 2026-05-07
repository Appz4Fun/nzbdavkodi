# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Direct Newznab-compatible indexer provider."""

from concurrent.futures import ThreadPoolExecutor, wait
from urllib.error import URLError
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree as ET

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
from resources.lib.http_util import (
    redact_text as _redact_text,
)
from resources.lib.http_util import (
    redact_url as _redact_url,
)

PRESET_INDEXERS = (
    ("nzblife", "NZB.life / NZB.su", "https://api.nzb.su/api"),
    ("nzbgeek", "NZBGeek", "https://api.nzbgeek.info/api"),
    ("nzbfinder", "NZBFinder", "https://nzbfinder.ws/api"),
    ("drunkenslug", "DrunkenSlug", "https://drunkenslug.com/api"),
    ("nzbplanet", "NZBPlanet", "https://api.nzbplanet.net/api"),
    ("dognzb", "DOGnzb", "https://api.dognzb.cr/api"),
)

_CUSTOM_SLOT_IDS = ("custom1", "custom2", "custom3")
NEWZNAB_NS = "http://www.newznab.com/DTD/2010/feeds/attributes/"
_DIRECT_REQUEST_ERRORS = (
    URLError,
    AttributeError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)
_DIRECT_FANOUT_MAX_WORKERS = 4
_DIRECT_FANOUT_TIMEOUT = 20


def _setting_enabled(addon, setting_id):
    return addon.getSetting(setting_id).lower() == "true"


def _setting_text(addon, setting_id):
    value = addon.getSetting(setting_id)
    return value.strip() if isinstance(value, str) else ""


def _configured_preset(addon, indexer_id, label, default_url):
    if not _setting_enabled(addon, "direct_indexer_{}_enabled".format(indexer_id)):
        return None
    url = (
        _setting_text(addon, "direct_indexer_{}_url".format(indexer_id)) or default_url
    )
    api_key = _setting_text(addon, "direct_indexer_{}_api_key".format(indexer_id))
    if not url or not api_key:
        xbmc.log(
            "NZB-DAV: Direct indexer {} enabled without URL/API key; skipping".format(
                label
            ),
            xbmc.LOGDEBUG,
        )
        return None
    return {"id": indexer_id, "label": label, "api_url": url, "api_key": api_key}


def _configured_custom(addon, slot_id):
    if not _setting_enabled(addon, "direct_indexer_{}_enabled".format(slot_id)):
        return None
    name = _setting_text(addon, "direct_indexer_{}_name".format(slot_id))
    url = _setting_text(addon, "direct_indexer_{}_url".format(slot_id))
    api_key = _setting_text(addon, "direct_indexer_{}_api_key".format(slot_id))
    if not name or not url or not api_key:
        xbmc.log(
            "NZB-DAV: Direct indexer {} missing name, URL, or API key; skipping".format(
                slot_id
            ),
            xbmc.LOGDEBUG,
        )
        return None
    return {"id": slot_id, "label": name, "api_url": url, "api_key": api_key}


def get_configured_indexers():
    """Return enabled direct indexers with complete URL and API key config."""
    addon = xbmcaddon.Addon()
    if not _setting_enabled(addon, "direct_indexers_enabled"):
        return []

    configured = []
    for indexer_id, label, default_url in PRESET_INDEXERS:
        item = _configured_preset(addon, indexer_id, label, default_url)
        if item:
            configured.append(item)

    for slot_id in _CUSTOM_SLOT_IDS:
        item = _configured_custom(addon, slot_id)
        if item:
            configured.append(item)

    return configured


def build_search_url(api_url, params):
    """Build a Newznab API URL from a user-configured API URL or host URL."""
    base = api_url.rstrip("/")
    if not base.endswith("/api"):
        base = base + "/api"
    return "{}?{}".format(base, urlencode(params))


def _build_xxe_safe_parser():
    return ET.XMLParser()  # nosec B314 - Python 3.8+ disables external entities


def _parse_newznab_attrs(item):
    size = ""
    indexer = ""
    for attr in item.iter():
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


def _source_hostname(item):
    source_text = _get_text(item, "source")
    if source_text:
        return source_text
    source_el = item.find("source")
    if source_el is None:
        return ""
    source_url = source_el.get("url", "")
    if not source_url:
        return ""
    if "/" not in source_url:
        return source_url
    try:
        return urlparse(source_url).hostname or ""
    except (AttributeError, TypeError, ValueError):
        return ""


def _build_result(item, fallback_indexer):
    title = _get_text(item, "title")
    link = _get_text(item, "link")
    pubdate = _get_text(item, "pubDate")
    size, attr_indexer = _parse_newznab_attrs(item)
    indexer = attr_indexer or _source_hostname(item) or fallback_indexer
    enclosure = item.find("enclosure")
    if enclosure is not None:
        if not link:
            link = enclosure.get("url", "")
        if not size:
            size = enclosure.get("length", "")
    return {
        "title": title or "",
        "link": link or "",
        "size": size,
        "indexer": indexer or "",
        "pubdate": pubdate or "",
        "age": _calculate_age(pubdate) if pubdate else "",
    }


def parse_results(xml_text, fallback_indexer):
    """Parse Newznab XML into the existing normalized result shape."""
    try:
        root = ET.fromstring(xml_text, parser=_build_xxe_safe_parser())  # nosec B314
    except ET.ParseError as error:
        xbmc.log(
            "NZB-DAV: Failed to parse direct indexer XML: {}".format(error),
            xbmc.LOGERROR,
        )
        return [], "Direct indexer returned an invalid response: {}".format(error)

    if root.tag != "rss":
        return [], "Direct indexer returned an invalid response: expected RSS feed"

    return [_build_result(item, fallback_indexer) for item in root.iter("item")], None


def _read_max_results():
    raw = xbmcaddon.Addon().getSetting("max_results")
    try:
        max_results = int(raw) if raw not in (None, "") else 25
    except (TypeError, ValueError):
        max_results = 25
    return max(1, min(max_results, 100))


def _search_params(
    search_type, title, api_key, max_results, imdb="", season="", episode=""
):
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
    return params


def _indexer_unavailable_error(indexer, error):
    return "Direct indexer {} unavailable: {}".format(
        indexer["label"], _format_request_error(error)
    )


def _format_timeout_seconds(timeout_seconds):
    if isinstance(timeout_seconds, float) and timeout_seconds.is_integer():
        return str(int(timeout_seconds))
    return str(timeout_seconds)


def _indexer_timeout_error(indexer, timeout_seconds):
    return _indexer_unavailable_error(
        indexer,
        TimeoutError(
            "timed out after {}s".format(_format_timeout_seconds(timeout_seconds))
        ),
    )


def _worker_count(total_count):
    return max(1, min(total_count, _DIRECT_FANOUT_MAX_WORKERS))


def _run_indexer_fanout(indexers, worker, timeout_seconds=None):
    timeout_seconds = (
        _DIRECT_FANOUT_TIMEOUT if timeout_seconds is None else timeout_seconds
    )
    executor = ThreadPoolExecutor(max_workers=_worker_count(len(indexers)))
    entries = []
    try:
        for indexer in indexers:
            entries.append((indexer, executor.submit(worker, indexer)))

        done, not_done = wait(
            [future for _indexer, future in entries], timeout=timeout_seconds
        )
        outcomes = []
        for indexer, future in entries:
            if future in not_done:
                future.cancel()
                outcomes.append(
                    (indexer, None, _indexer_timeout_error(indexer, timeout_seconds))
                )
                continue
            if future not in done:
                outcomes.append(
                    (indexer, None, _indexer_timeout_error(indexer, timeout_seconds))
                )
                continue
            try:
                value, error = future.result()
            except _DIRECT_REQUEST_ERRORS as error:
                outcomes.append(
                    (indexer, None, _indexer_unavailable_error(indexer, error))
                )
                continue
            outcomes.append((indexer, value, error))
        return outcomes
    finally:
        executor.shutdown(wait=False)


def _fetch_indexer(indexer, params, error_prefix):
    url = build_search_url(indexer["api_url"], params)
    xbmc.log(
        "NZB-DAV: Direct indexer {} search URL: {}".format(
            indexer["label"], _redact_url(url)
        ),
        xbmc.LOGDEBUG,
    )
    try:
        return _http_get(url, timeout=15), None
    except _DIRECT_REQUEST_ERRORS as error:
        xbmc.log(
            "NZB-DAV: {}: {}".format(error_prefix, _redact_text(str(error))),
            xbmc.LOGERROR,
        )
        return None, _indexer_unavailable_error(indexer, error)


def _search_one_indexer(
    indexer, search_type, title, max_results, imdb="", season="", episode=""
):
    params = _search_params(
        search_type,
        title,
        indexer["api_key"],
        max_results,
        imdb=imdb,
        season=season,
        episode=episode,
    )
    xml_text, request_error = _fetch_indexer(
        indexer, params, "Direct indexer {} search failed".format(indexer["label"])
    )
    if request_error:
        return [], request_error
    results, parse_error = parse_results(xml_text, indexer["label"])
    if parse_error:
        return [], parse_error

    if not results and imdb and title:
        params.pop("imdbid", None)
        params["q"] = title
        xml_text, request_error = _fetch_indexer(
            indexer,
            params,
            "Direct indexer {} title fallback failed".format(indexer["label"]),
        )
        if request_error:
            return [], request_error
        results, parse_error = parse_results(xml_text, indexer["label"])
        if parse_error:
            return [], parse_error

    return results, None


def search_direct_indexers(search_type, title, year="", imdb="", season="", episode=""):
    """Search all configured direct Newznab indexers."""
    del year
    indexers = get_configured_indexers()
    if not indexers:
        return [], None

    max_results = _read_max_results()
    all_results = []
    errors = []

    def worker(indexer):
        return _search_one_indexer(
            indexer,
            search_type,
            title,
            max_results,
            imdb=imdb,
            season=season,
            episode=episode,
        )

    for _indexer, results, error in _run_indexer_fanout(indexers, worker):
        if error:
            errors.append(error)
            xbmc.log("NZB-DAV: {}".format(error), xbmc.LOGWARNING)
            continue
        all_results.extend(results)

    if not all_results and errors:
        return [], errors[0]
    return all_results, None


def _check_one_indexer_caps(indexer):
    params = {"apikey": indexer["api_key"], "t": "caps", "o": "xml"}
    url = build_search_url(indexer["api_url"], params)
    try:
        response = _http_get(url, timeout=15)
        if "<caps" in response or "<server" in response or "<rss" in response:
            return True, None
        return False, "Direct indexer {} unexpected response".format(indexer["label"])
    except _DIRECT_REQUEST_ERRORS as error:
        return False, _indexer_unavailable_error(indexer, error)


def test_configured_indexers():
    """Return (ok_count, total_count, errors) for configured direct indexers."""
    indexers = get_configured_indexers()
    ok_count = 0
    errors = []
    for indexer, ok, error in _run_indexer_fanout(indexers, _check_one_indexer_caps):
        if error:
            errors.append(error)
            xbmc.log(
                "NZB-DAV: Direct indexer caps error for {}: {}".format(
                    indexer["label"], _redact_text(error)
                ),
                xbmc.LOGWARNING,
            )
        elif ok:
            ok_count += 1
    return ok_count, len(indexers), errors
