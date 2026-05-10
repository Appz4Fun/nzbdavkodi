# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Shared Newznab search query planning."""

from dataclasses import dataclass

from resources.lib.indexer_presets import (
    DIRECT_FALLBACK_HOSTS,
    DOGNZB_TVSEARCH_FALLBACK_HOSTS,
    host_contains,
)


@dataclass(frozen=True)
class NewznabSearchPlan:
    primary: dict
    fallback: dict
    reason: str


def plan_newznab_search(
    provider_kind,
    host,
    search_type,
    title,
    year=None,
    imdb=None,
    season=None,
    episode=None,
    caps=None,
    api_key="",
    max_results=25,
):
    del year
    base = {"apikey": api_key, "o": "xml", "limit": max_results}
    if _missing_caps(caps):
        return _missing_caps_plan(base, search_type, title, imdb, season, episode)

    if search_type == "episode":
        return _episode_plan(
            base, provider_kind, host, title, imdb, season, episode, caps
        )
    return _movie_plan(base, provider_kind, host, title, imdb, caps)


def _missing_caps(caps):
    return not caps or not caps.get("search_types")


def _params(base, search_type, **items):
    params = dict(base)
    params["t"] = search_type
    for key, value in items.items():
        if value not in (None, ""):
            params[key] = value
    return params


def _missing_caps_plan(base, search_type, title, imdb, season, episode):
    if search_type == "episode":
        fallback = _generic_search(base, title) if title else None
        if imdb:
            primary = _params(base, "tvsearch", imdbid=_imdb_digits(imdb))
        else:
            primary = _params(base, "tvsearch", q=title)
        if season:
            primary["season"] = season
        if episode:
            primary["ep"] = episode
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


def _generic_search(base, title, caps=None):
    if _missing_caps(caps):
        return _params(base, "search", q=title)
    if not _supports(caps, "search"):
        return None
    if title and _supports(caps, "search", "q"):
        return _params(base, "search", q=title)
    return _params(base, "search")


def _movie_title_params(base, provider_kind, host, title, caps):
    if _direct_movie_title_fallback(provider_kind, host):
        return (
            _generic_search(base, title, caps),
            "direct_movie_title_search_fallback",
        )
    if _supports(caps, "movie", "q"):
        return _params(base, "movie", q=title), "movie_title"
    return _generic_search(base, title, caps), "movie_title_search_fallback"


def _movie_plan(base, provider_kind, host, title, imdb, caps):
    imdbid = _imdb_digits(imdb)
    fallback = _generic_search(base, title, caps) if title else None
    if imdbid and _supports(caps, "movie", "imdbid"):
        return NewznabSearchPlan(
            _params(base, "movie", imdbid=imdbid),
            fallback,
            "movie_imdb",
        )

    primary, reason = _movie_title_params(base, provider_kind, host, title, caps)
    if primary is None:
        return _no_query_plan()
    return NewznabSearchPlan(primary, fallback, reason)


def _episode_plan(base, provider_kind, host, title, imdb, season, episode, caps):
    fallback = _generic_search(base, title, caps) if title else None
    if _direct_episode_fallback(provider_kind, host) or not _supports(caps, "tvsearch"):
        if fallback is None:
            return _no_query_plan()
        return NewznabSearchPlan(fallback, fallback, "episode_search_fallback")

    params = _params(base, "tvsearch")
    if title and _supports(caps, "tvsearch", "q"):
        params["q"] = title
    imdbid = _imdb_digits(imdb)
    if imdbid and _supports(caps, "tvsearch", "imdbid"):
        params["imdbid"] = imdbid
    if season and _supports(caps, "tvsearch", "season"):
        params["season"] = season
    if episode and _supports(caps, "tvsearch", "ep"):
        params["ep"] = episode
    return NewznabSearchPlan(params, fallback, "episode_tvsearch")


def _imdb_digits(imdb):
    if not imdb:
        return ""
    value = str(imdb)
    if value.startswith("tt"):
        value = value[2:]
    return "".join(char for char in value if char.isdigit())
