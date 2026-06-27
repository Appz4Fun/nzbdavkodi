# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Integration checks for a real Hydra/nzbdav/WebDAV setup.

These tests intentionally stay read-only. They verify that configured live
services respond and that fallback NZB candidates can be gathered from real
Hydra search results without submitting or downloading a release.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

import pytest
from resources.lib.fallback_streams import attach_fallback_candidates_for_selection
from resources.lib.http_util import http_get
from resources.lib.hydra import search_hydra
from resources.lib.webdav import probe_webdav_reachable

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_LIVE_ENV = (
    "HYDRA_URL",
    "HYDRA_API_KEY",
    "WEBDAV_URL",
    "NZBDAV_URL",
    "WEBDAV_API_KEY",
    "WEBDAV_USERNAME",
    "WEBDAV_PASSWORD",
)


def _load_dotenv(path):
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _live_env():
    values = _load_dotenv(REPO_ROOT / ".env")
    for key in REQUIRED_LIVE_ENV:
        if os.environ.get(key):
            values[key] = os.environ[key]
    missing = [key for key in REQUIRED_LIVE_ENV if not values.get(key)]
    if missing:
        pytest.skip("missing live-service env vars: {}".format(", ".join(missing)))
    return values


def _addon_settings(env):
    return {
        "hydra_url": env["HYDRA_URL"],
        "hydra_api_key": env["HYDRA_API_KEY"],
        "max_results": os.environ.get("LIVE_HYDRA_MAX_RESULTS", "100"),
        "nzbdav_url": env["NZBDAV_URL"],
        # The live-test env keeps the API key under the requested name.
        "nzbdav_api_key": env["WEBDAV_API_KEY"],
        "webdav_url": env["WEBDAV_URL"],
        "webdav_username": env["WEBDAV_USERNAME"],
        "webdav_password": env["WEBDAV_PASSWORD"],
        "webdav_content_root": os.environ.get("LIVE_WEBDAV_CONTENT_ROOT", "content"),
        "fallback_streams_enabled": "true",
        "fallback_streams_max": os.environ.get("LIVE_FALLBACKS_MAX", "5"),
    }


def _patch_addon_settings(settings):
    return patch(
        "xbmcaddon.Addon.return_value.getSetting",
        side_effect=lambda key: settings.get(key, ""),
    )


def _live_search_config():
    return {
        "title": os.environ.get("LIVE_SEARCH_TITLE", "The Matrix"),
        "year": os.environ.get("LIVE_SEARCH_YEAR", "1999"),
        "imdb": os.environ.get("LIVE_SEARCH_IMDB", "tt0133093"),
        "pool_limit": int(os.environ.get("LIVE_FALLBACK_POOL_LIMIT", "20")),
    }


def _candidate_pool(results, title, pool_limit):
    title_tokens = [token.lower() for token in title.split() if token.strip()]
    candidates = [
        result
        for result in results
        if result.get("link")
        and all(token in result.get("title", "").lower() for token in title_tokens)
    ]
    if len(candidates) < 2:
        candidates = [result for result in results if result.get("link")]
    return candidates[:pool_limit]


def test_live_hydra_matrix_search_produces_fallback_candidates():
    env = _live_env()
    search = _live_search_config()
    settings = _addon_settings(env)

    with _patch_addon_settings(settings):
        results, error = search_hydra(
            "movie",
            search["title"],
            year=search["year"],
            imdb=search["imdb"],
        )

    if error:
        pytest.fail("Hydra live search failed: {}".format(error), pytrace=False)
    assert results, "Hydra live search returned no results"

    pool = _candidate_pool(results, search["title"], search["pool_limit"])
    assert len(pool) >= 2, "Hydra live search did not return enough linked results"

    selected_with_fallback = None
    with _patch_addon_settings(settings):
        for selected in pool:
            attach_fallback_candidates_for_selection(selected, pool)
            if selected.get("_fallback_candidates"):
                selected_with_fallback = selected
                break

    assert (
        selected_with_fallback is not None
    ), "No live fallback candidates were found for the configured search"
    assert selected_with_fallback.get("_fallback_candidates")
    for fallback in selected_with_fallback["_fallback_candidates"]:
        assert fallback.get("link")
        assert fallback.get("link") != selected_with_fallback.get("link")


def test_live_nzbdav_api_and_webdav_are_reachable():
    env = _live_env()
    settings = _addon_settings(env)
    params = {
        "mode": "queue",
        "apikey": env["WEBDAV_API_KEY"],
        "output": "json",
        "limit": 1,
    }
    url = "{}/api?{}".format(env["NZBDAV_URL"].rstrip("/"), urlencode(params))

    try:
        response = json.loads(http_get(url, timeout=30))
    except Exception as exc:  # pylint: disable=broad-except
        pytest.fail(
            "nzbdav live queue API request failed: {}".format(type(exc).__name__),
            pytrace=False,
        )
    assert isinstance(response, dict)
    assert isinstance(response.get("queue"), dict)

    with _patch_addon_settings(settings):
        reachable, error = probe_webdav_reachable(max_retries=0)

    assert reachable, "WebDAV live probe failed: {}".format(error)
