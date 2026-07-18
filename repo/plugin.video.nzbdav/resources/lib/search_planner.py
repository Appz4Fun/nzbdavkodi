# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Shared Newznab search query planning."""

from dataclasses import dataclass
from typing import NamedTuple

from resources.lib.http_util import clean_search_query
from resources.lib.indexer_presets import (
    DIRECT_FALLBACK_HOSTS,
    DOGNZB_TVSEARCH_FALLBACK_HOSTS,
    host_contains,
)


class SearchQuery(NamedTuple):
    """Identity of one search request. Keep NZBHydra2/Prowlarr symmetric:
    both providers receive the same SearchQuery."""

    search_type: str
    title: str
    year: str = ""
    imdb: str = ""
    season: str = ""
    episode: str = ""
    tvdb: str = ""
    tmdb_id: str = ""


@dataclass(frozen=True)
class NewznabSearchPlan:
    """A planned Newznab search: primary params, fallback params, and reason."""

    primary: dict
    fallback: dict
    reason: str


def plan_newznab_search(
    provider_kind,
    host,
    query,
    caps=None,
    api_key="",
    max_results=25,
):
    """Return a NewznabSearchPlan for one search request, honoring advertised caps."""
    search_type = query.search_type
    title = query.title
    year = query.year
    imdb = query.imdb
    season = query.season
    episode = query.episode
    tvdb = query.tvdb
    base = {"apikey": api_key, "o": "xml", "limit": max_results}
    # Strip query-breaking '&' from the keyword title once, so every q=title
    # branch below (and its title fallback) searches a term the indexer can
    # actually match (#294). Id-keyed searches are unaffected.
    title = clean_search_query(title)
    if _missing_caps(caps):
        return _missing_caps_plan(base, search_type, title, imdb, season, episode, tvdb)

    if search_type == "episode":
        return _episode_plan(
            base, provider_kind, host, title, (imdb, tvdb), season, episode, caps
        )
    return _movie_plan(base, provider_kind, host, title, year, imdb, caps)


def _missing_caps(caps):
    return not caps or not caps.get("search_types")


def _params(base, search_type, **items):
    params = dict(base)
    params["t"] = search_type
    for key, value in items.items():
        if value not in (None, ""):
            params[key] = value
    return params


def _missing_caps_episode_primary(base, title, imdb, season, episode, tvdb):
    """Build the default tvsearch primary params when caps are unknown."""
    tvdbid = _digits(tvdb)
    if tvdbid:
        primary = _params(base, "tvsearch", tvdbid=tvdbid)
    elif imdb:
        primary = _params(base, "tvsearch", imdbid=_imdb_digits(imdb))
    else:
        primary = _params(base, "tvsearch", q=title)
    if season:
        primary["season"] = season
    if episode:
        primary["ep"] = episode
    return primary


def _missing_caps_plan(base, search_type, title, imdb, season, episode, tvdb=None):
    if search_type == "episode":
        fallback = _generic_search(base, title) if title else None
        primary = _missing_caps_episode_primary(
            base, title, imdb, season, episode, tvdb
        )
        return NewznabSearchPlan(primary, fallback, "missing_caps_episode_default")

    if imdb:
        primary = _params(base, "movie", imdbid=_imdb_digits(imdb))
        fallback = _generic_search(base, title) if title else None
        return NewznabSearchPlan(primary, fallback, "missing_caps_movie_default")
    fallback = _generic_search(base, title) if title else None
    return NewznabSearchPlan(
        _params(base, "movie", q=title), fallback, "missing_caps_movie_default"
    )


def _supports(caps, search_type, param=None):
    search_types = set(caps.get("search_types") or [])
    if search_type not in search_types:
        return False
    if param is None:
        return True
    supported = caps.get("supported_params") or {}
    return param in set(supported.get(search_type) or [])


def _direct_provider(provider_kind):
    return provider_kind == "direct"


def _direct_movie_title_fallback(provider_kind, host):
    return _direct_provider(provider_kind) and host_contains(
        host, DIRECT_FALLBACK_HOSTS
    )


def _direct_episode_fallback(provider_kind, host):
    return _direct_provider(provider_kind) and host_contains(
        host, DOGNZB_TVSEARCH_FALLBACK_HOSTS
    )


def _no_query_plan():
    return NewznabSearchPlan({}, None, "no_supported_query")


def _generic_search(base, title, caps=None, year=None):
    if _missing_caps(caps):
        return _params(base, "search", q=title)
    if not _supports(caps, "search"):
        return None
    items = {}
    if title and _supports(caps, "search", "q"):
        items["q"] = title
    if year and _supports(caps, "search", "year"):
        items["year"] = year
    return _params(base, "search", **items)


def _movie_title_params(base, provider_kind, host, title, year, caps):
    if _direct_movie_title_fallback(provider_kind, host):
        return (
            _generic_search(base, title, caps, year=year),
            "direct_movie_title_search_fallback",
        )
    items = {}
    if _supports(caps, "movie", "q"):
        items["q"] = title
    if year and _supports(caps, "movie", "year"):
        items["year"] = year
    if items:
        return _params(base, "movie", **items), "movie_title"
    return _generic_search(base, title, caps, year=year), "movie_title_search_fallback"


def _movie_plan(base, provider_kind, host, title, year, imdb, caps):
    imdbid = _imdb_digits(imdb)
    fallback = _generic_search(base, title, caps, year=year) if title else None
    if imdbid and _supports(caps, "movie", "imdbid"):
        return NewznabSearchPlan(
            _params(base, "movie", imdbid=imdbid),
            fallback,
            "movie_imdb",
        )

    primary, reason = _movie_title_params(base, provider_kind, host, title, year, caps)
    if primary is None:
        return _no_query_plan()
    return NewznabSearchPlan(primary, fallback, reason)


def _tvsearch_id_params(params, caps, imdb, tvdb):
    """Add the preferred id token to tvsearch ``params`` in place.

    Prefer tvdbid when the indexer advertises it (issue #318); many index TV
    releases by TheTVDB id, so imdbid-keyed tvsearch misses. Send the tvdbid
    *instead of* imdbid to avoid ambiguous multi-id queries.
    """
    tvdbid = _digits(tvdb)
    imdbid = _imdb_digits(imdb)
    if tvdbid and _supports(caps, "tvsearch", "tvdbid"):
        params["tvdbid"] = tvdbid
    elif imdbid and _supports(caps, "tvsearch", "imdbid"):
        params["imdbid"] = imdbid


def _tvsearch_params(base, title, imdb, season, episode, caps, tvdb):
    """Build the tvsearch primary params honoring advertised caps."""
    params = _params(base, "tvsearch")
    if title and _supports(caps, "tvsearch", "q"):
        params["q"] = title
    _tvsearch_id_params(params, caps, imdb, tvdb)
    if season and _supports(caps, "tvsearch", "season"):
        params["season"] = season
    if episode and _supports(caps, "tvsearch", "ep"):
        params["ep"] = episode
    return params


def _episode_plan(base, provider_kind, host, title, ids, season, episode, caps):
    """Plan an episode tvsearch. ``ids`` is the ``(imdb, tvdb)`` pair."""
    imdb, tvdb = ids
    fallback = _generic_search(base, title, caps) if title else None
    if _direct_episode_fallback(provider_kind, host) or not _supports(caps, "tvsearch"):
        if fallback is None:
            return _no_query_plan()
        return NewznabSearchPlan(fallback, fallback, "episode_search_fallback")

    params = _tvsearch_params(base, title, imdb, season, episode, caps, tvdb)
    return NewznabSearchPlan(params, fallback, "episode_tvsearch")


def _imdb_digits(imdb):
    if not imdb:
        return ""
    value = str(imdb)
    if value.startswith("tt"):
        value = value[2:]
    return "".join(char for char in value if char.isdigit())


def _digits(value):
    """Return only the decimal digits of ``value`` (TVDB ids are numeric).

    Defensive against a decorated id (e.g. ``"tvdb-305288"``); returns ``""``
    for empty/None input so callers can treat it as "no id".
    """
    if not value:
        return ""
    return "".join(char for char in str(value) if char.isdigit())
