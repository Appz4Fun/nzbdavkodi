# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from resources.lib.search_planner import plan_newznab_search


def _supported_caps():
    return {
        "search_types": ["search", "tvsearch", "movie"],
        "supported_params": {
            "search": ["q"],
            "tvsearch": ["q", "imdbid", "season", "ep"],
            "movie": ["q", "imdbid"],
        },
    }


def test_movie_with_supported_imdb_uses_movie_id():
    plan = plan_newznab_search(
        provider_kind="direct",
        host="https://api.example.test",
        search_type="movie",
        title="The Matrix",
        imdb="tt0133093",
        caps=_supported_caps(),
        api_key="secret",
        max_results=42,
    )

    assert plan.primary == {
        "apikey": "secret",
        "o": "xml",
        "limit": 42,
        "t": "movie",
        "imdbid": "0133093",
    }
    assert plan.fallback == {
        "apikey": "secret",
        "o": "xml",
        "limit": 42,
        "t": "search",
        "q": "The Matrix",
    }
    assert plan.reason == "movie_imdb"


def test_movie_title_on_nzbgeek_falls_back_to_search():
    plan = plan_newznab_search(
        provider_kind="direct",
        host="https://api.nzbgeek.info",
        search_type="movie",
        title="The Matrix",
        caps=_supported_caps(),
        api_key="secret",
        max_results=25,
    )

    assert plan.primary["t"] == "search"
    assert plan.primary["q"] == "The Matrix"
    assert plan.fallback == plan.primary
    assert plan.reason == "direct_movie_title_search_fallback"


def test_episode_uses_tvsearch_when_supported():
    plan = plan_newznab_search(
        provider_kind="direct",
        host="https://api.example.test",
        search_type="episode",
        title="Silo",
        imdb="tt14688458",
        season="2",
        episode="5",
        caps=_supported_caps(),
        api_key="secret",
        max_results=10,
    )

    assert plan.primary == {
        "apikey": "secret",
        "o": "xml",
        "limit": 10,
        "t": "tvsearch",
        "q": "Silo",
        "imdbid": "14688458",
        "season": "2",
        "ep": "5",
    }
    assert plan.fallback == {
        "apikey": "secret",
        "o": "xml",
        "limit": 10,
        "t": "search",
        "q": "Silo",
    }
    assert plan.reason == "episode_tvsearch"


def test_hydra_uses_provider_caps_without_direct_host_fallback():
    plan = plan_newznab_search(
        provider_kind="nzbhydra2",
        host="https://api.nzbgeek.info",
        search_type="movie",
        title="The Matrix",
        caps=_supported_caps(),
        api_key="secret",
        max_results=25,
    )

    assert plan.primary["t"] == "movie"
    assert plan.primary["q"] == "The Matrix"
    assert plan.reason == "movie_title"


def test_missing_caps_keeps_conservative_defaults():
    movie = plan_newznab_search(
        provider_kind="direct",
        host="https://api.example.test",
        search_type="movie",
        title="The Matrix",
        caps=None,
        api_key="secret",
        max_results=25,
    )
    episode = plan_newznab_search(
        provider_kind="direct",
        host="https://api.example.test",
        search_type="episode",
        title="Silo",
        imdb="tt14688458",
        season="2",
        episode="5",
        caps={},
        api_key="secret",
        max_results=25,
    )

    assert movie.primary["t"] == "movie"
    assert movie.primary["q"] == "The Matrix"
    assert movie.fallback["t"] == "search"
    assert movie.fallback["q"] == "The Matrix"
    assert movie.reason == "missing_caps_movie_default"
    assert episode.primary == {
        "apikey": "secret",
        "o": "xml",
        "limit": 25,
        "t": "tvsearch",
        "imdbid": "14688458",
        "season": "2",
        "ep": "5",
    }
    assert episode.reason == "missing_caps_episode_default"


def test_movie_title_without_supported_search_returns_no_query():
    plan = plan_newznab_search(
        provider_kind="direct",
        host="https://api.example.test",
        search_type="movie",
        title="The Matrix",
        caps={
            "search_types": ["movie"],
            "supported_params": {"movie": ["imdbid"]},
        },
        api_key="secret",
        max_results=25,
    )

    assert plan.primary == {}
    assert plan.fallback is None
    assert plan.reason == "no_supported_query"


def test_episode_without_tvsearch_or_search_returns_no_query():
    plan = plan_newznab_search(
        provider_kind="direct",
        host="https://api.example.test",
        search_type="episode",
        title="Silo",
        caps={
            "search_types": ["movie"],
            "supported_params": {"movie": ["q", "imdbid"]},
        },
        api_key="secret",
        max_results=25,
    )

    assert plan.primary == {}
    assert plan.fallback is None
    assert plan.reason == "no_supported_query"


def test_generic_search_without_q_does_not_include_unsupported_q():
    plan = plan_newznab_search(
        provider_kind="direct",
        host="https://api.nzbgeek.info",
        search_type="movie",
        title="The Matrix",
        caps={
            "search_types": ["search", "movie"],
            "supported_params": {"search": [], "movie": []},
        },
        api_key="secret",
        max_results=25,
    )

    assert plan.primary == {"apikey": "secret", "o": "xml", "limit": 25, "t": "search"}
    assert "q" not in plan.primary
    assert plan.fallback == plan.primary
    assert plan.reason == "direct_movie_title_search_fallback"
