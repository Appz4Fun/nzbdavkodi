# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Dev-box functional test for live fallback discovery and proxy failover."""

import contextlib
import hashlib
import json
import os
import random
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
import xbmcaddon
from resources.lib.fallback_streams import (
    attach_fallback_candidates_for_selection,
    build_fallback_job_name,
)
from resources.lib.filter import filter_results
from resources.lib.hydra import search_hydra
from resources.lib.nzbdav_api import (
    find_completed_by_name,
    find_queued_by_name,
    get_job_history,
    get_job_status,
    submit_nzb,
)
from resources.lib.resolver import _storage_to_webdav_path
from resources.lib.stream_proxy import StreamProxy
from resources.lib.webdav import find_video_file, get_webdav_stream_url_for_path

pytestmark = pytest.mark.functional

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

_FAILED_JOB_NAME_CACHE = {}

IMDB_TOP_50_MOVIES = (
    {
        "rank": 1,
        "title": "The Shawshank Redemption",
        "year": "1994",
        "imdb": "tt0111161",
    },
    {"rank": 2, "title": "The Godfather", "year": "1972", "imdb": "tt0068646"},
    {"rank": 3, "title": "The Dark Knight", "year": "2008", "imdb": "tt0468569"},
    {"rank": 4, "title": "The Godfather Part II", "year": "1974", "imdb": "tt0071562"},
    {"rank": 5, "title": "12 Angry Men", "year": "1957", "imdb": "tt0050083"},
    {
        "rank": 6,
        "title": "The Lord of the Rings: The Return of the King",
        "year": "2003",
        "imdb": "tt0167260",
    },
    {"rank": 7, "title": "Schindler's List", "year": "1993", "imdb": "tt0108052"},
    {
        "rank": 8,
        "title": "The Lord of the Rings: The Fellowship of the Ring",
        "year": "2001",
        "imdb": "tt0120737",
    },
    {"rank": 9, "title": "Pulp Fiction", "year": "1994", "imdb": "tt0110912"},
    {
        "rank": 10,
        "title": "The Lord of the Rings: The Two Towers",
        "year": "2002",
        "imdb": "tt0167261",
    },
    {
        "rank": 11,
        "title": "The Good the Bad and the Ugly",
        "year": "1966",
        "imdb": "tt0060196",
    },
    {"rank": 12, "title": "Forrest Gump", "year": "1994", "imdb": "tt0109830"},
    {"rank": 13, "title": "Fight Club", "year": "1999", "imdb": "tt0137523"},
    {"rank": 14, "title": "Inception", "year": "2010", "imdb": "tt1375666"},
    {
        "rank": 15,
        "title": "Star Wars: Episode V - The Empire Strikes Back",
        "year": "1980",
        "imdb": "tt0080684",
    },
    {"rank": 16, "title": "The Matrix", "year": "1999", "imdb": "tt0133093"},
    {"rank": 17, "title": "Goodfellas", "year": "1990", "imdb": "tt0099685"},
    {"rank": 18, "title": "Interstellar", "year": "2014", "imdb": "tt0816692"},
    {
        "rank": 19,
        "title": "One Flew Over the Cuckoo's Nest",
        "year": "1975",
        "imdb": "tt0073486",
    },
    {"rank": 20, "title": "Se7en", "year": "1995", "imdb": "tt0114369"},
    {"rank": 21, "title": "It's a Wonderful Life", "year": "1946", "imdb": "tt0038650"},
    {"rank": 22, "title": "Seven Samurai", "year": "1954", "imdb": "tt0047478"},
    {
        "rank": 23,
        "title": "The Silence of the Lambs",
        "year": "1991",
        "imdb": "tt0102926",
    },
    {"rank": 24, "title": "Saving Private Ryan", "year": "1998", "imdb": "tt0120815"},
    {"rank": 25, "title": "City of God", "year": "2002", "imdb": "tt0317248"},
    {"rank": 26, "title": "Life Is Beautiful", "year": "1997", "imdb": "tt0118799"},
    {"rank": 27, "title": "The Green Mile", "year": "1999", "imdb": "tt0120689"},
    {
        "rank": 28,
        "title": "Terminator 2: Judgment Day",
        "year": "1991",
        "imdb": "tt0103064",
    },
    {
        "rank": 29,
        "title": "Star Wars: Episode IV - A New Hope",
        "year": "1977",
        "imdb": "tt0076759",
    },
    {"rank": 30, "title": "Back to the Future", "year": "1985", "imdb": "tt0088763"},
    {"rank": 31, "title": "Spirited Away", "year": "2001", "imdb": "tt0245429"},
    {"rank": 32, "title": "The Pianist", "year": "2002", "imdb": "tt0253474"},
    {"rank": 33, "title": "Parasite", "year": "2019", "imdb": "tt6751668"},
    {"rank": 34, "title": "Psycho", "year": "1960", "imdb": "tt0054215"},
    {"rank": 35, "title": "Gladiator", "year": "2000", "imdb": "tt0172495"},
    {"rank": 36, "title": "The Lion King", "year": "1994", "imdb": "tt0110357"},
    {
        "rank": 37,
        "title": "Leon: The Professional",
        "year": "1994",
        "imdb": "tt0110413",
    },
    {"rank": 38, "title": "The Departed", "year": "2006", "imdb": "tt0407887"},
    {"rank": 39, "title": "American History X", "year": "1998", "imdb": "tt0120586"},
    {"rank": 40, "title": "Whiplash", "year": "2014", "imdb": "tt2582802"},
    {"rank": 41, "title": "The Prestige", "year": "2006", "imdb": "tt0482571"},
    {
        "rank": 42,
        "title": "Grave of the Fireflies",
        "year": "1988",
        "imdb": "tt0095327",
    },
    {"rank": 43, "title": "Harakiri", "year": "1962", "imdb": "tt0056058"},
    {"rank": 44, "title": "The Usual Suspects", "year": "1995", "imdb": "tt0114814"},
    {"rank": 45, "title": "Casablanca", "year": "1942", "imdb": "tt0034583"},
    {"rank": 46, "title": "The Intouchables", "year": "2011", "imdb": "tt1675434"},
    {"rank": 47, "title": "Cinema Paradiso", "year": "1988", "imdb": "tt0095765"},
    {"rank": 48, "title": "Modern Times", "year": "1936", "imdb": "tt0027977"},
    {"rank": 49, "title": "Rear Window", "year": "1954", "imdb": "tt0047396"},
    {"rank": 50, "title": "Alien", "year": "1979", "imdb": "tt0078748"},
)


class _FunctionalAttemptFailed(Exception):
    """Raised when one live candidate set cannot prove fallback playback."""


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
        "nzbdav_api_key": env["WEBDAV_API_KEY"],
        "webdav_url": env["WEBDAV_URL"],
        "webdav_username": env["WEBDAV_USERNAME"],
        "webdav_password": env["WEBDAV_PASSWORD"],
        "webdav_content_root": os.environ.get("LIVE_WEBDAV_CONTENT_ROOT", "content"),
        "fallback_streams_enabled": "true",
        "fallback_streams_max": os.environ.get("LIVE_FALLBACKS_MAX", "5"),
        "submit_timeout": os.environ.get("FUNCTIONAL_SUBMIT_TIMEOUT_SECONDS", "300"),
    }


def _patch_addon_settings(settings):
    return patch(
        "xbmcaddon.Addon.return_value.getSetting",
        side_effect=lambda key: settings.get(key, ""),
    )


def _live_search_configs():
    pool_limit = int(os.environ.get("LIVE_FALLBACK_POOL_LIMIT", "30"))
    configs = []
    framestor_title = os.environ.get(
        "LIVE_FRAMESTOR_SEARCH_TITLE", "The Matrix 1999 FraMeSToR"
    )
    if framestor_title:
        configs.append(
            {
                "label": "FrameStor/FraMeSToR",
                "title": framestor_title,
                "year": os.environ.get("LIVE_FRAMESTOR_SEARCH_YEAR", ""),
                "imdb": os.environ.get("LIVE_FRAMESTOR_SEARCH_IMDB", ""),
                "pool_limit": pool_limit,
            }
        )
    configs.append(
        {
            "label": "top Matrix result",
            "title": os.environ.get("LIVE_SEARCH_TITLE", "The Matrix"),
            "year": os.environ.get("LIVE_SEARCH_YEAR", "1999"),
            "imdb": os.environ.get("LIVE_SEARCH_IMDB", "tt0133093"),
            "pool_limit": pool_limit,
        }
    )
    return configs


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


def _looks_like_framestor_release(result):
    meta = result.get("_meta") if isinstance(result, dict) else {}
    group = meta.get("group", "") if isinstance(meta, dict) else ""
    title = result.get("title", "") if isinstance(result, dict) else ""
    return "framestor" in "{} {}".format(group, title).lower()


def _required_fallback_candidate_count():
    return int(os.environ.get("FUNCTIONAL_MIN_FALLBACK_CANDIDATES", "1"))


def _selection_candidate_pair(selected, pool, required_count):
    attach_fallback_candidates_for_selection(selected, pool)
    candidates = list(selected.get("_fallback_candidates", []) or [])
    if len(candidates) < required_count:
        return None
    return (
        dict(selected),
        [dict(candidate) for candidate in candidates],
    )


def _matrix_selections_with_fallbacks(settings):
    required_count = _required_fallback_candidate_count()
    failures = []
    for search in _live_search_configs():
        with _patch_addon_settings(settings):
            results, error = search_hydra(
                "movie",
                search["title"],
                year=search["year"],
                imdb=search["imdb"],
            )
        if error:
            failures.append("{} search failed".format(search["label"]))
            continue
        if not results:
            failures.append("{} returned no results".format(search["label"]))
            continue
        with _patch_addon_settings(settings):
            filtered, all_parsed = filter_results(results)
        results = filtered or all_parsed
        if not results:
            failures.append("{} returned no parseable results".format(search["label"]))
            continue

        pool = _candidate_pool(results, search["title"], search["pool_limit"])
        if len(pool) < 2:
            failures.append(
                "{} returned too few linked results".format(search["label"])
            )
            continue

        first_result = pool[0]
        ordered_targets = []
        ordered_targets.extend(
            result for result in pool if _looks_like_framestor_release(result)
        )
        if first_result not in ordered_targets:
            ordered_targets.append(first_result)

        selections = []
        with _patch_addon_settings(settings):
            for selected in ordered_targets:
                pair = _selection_candidate_pair(selected, pool, required_count)
                if pair is not None:
                    selections.append(pair)
        if selections:
            selections.sort(key=lambda pair: _primary_adoption_rank(pair[0]))
            return selections
        failures.append("{} produced no same-profile pair".format(search["label"]))

    return pytest.fail(
        "No FrameStor/FraMeSToR or first Matrix result produced a "
        "same-profile live fallback pair: {}".format("; ".join(failures)),
        pytrace=False,
    )


def _top_imdb_sample():
    count = int(os.environ.get("FUNCTIONAL_IMDB_SAMPLE_COUNT", "10"))
    count = max(1, min(count, len(IMDB_TOP_50_MOVIES)))
    seed = os.environ.get("FUNCTIONAL_IMDB_RANDOM_SEED")
    if not seed:
        seed = str(random.SystemRandom().randrange(1, 2**31))
    rng = random.Random(seed)
    initial = rng.sample(list(IMDB_TOP_50_MOVIES), count)
    initial_ids = {movie["imdb"] for movie in initial}
    remaining = [
        movie for movie in IMDB_TOP_50_MOVIES if movie["imdb"] not in initial_ids
    ]
    backfill = rng.sample(remaining, len(remaining))
    return seed, count, initial + backfill


def _movie_search_pool(settings, movie, query_title=None, use_imdb=True):
    title = query_title or movie["title"]
    imdb = movie["imdb"] if use_imdb else ""
    year = movie["year"] if use_imdb else ""
    with _patch_addon_settings(settings):
        results, error = search_hydra(
            "movie",
            title,
            year=year,
            imdb=imdb,
        )
    if error:
        raise _FunctionalAttemptFailed(
            "{} search failed: {}".format(movie["title"], error)
        )
    if not results:
        raise _FunctionalAttemptFailed("{} search returned no results".format(title))

    with _patch_addon_settings(settings):
        filtered, all_parsed = filter_results(results)
    parsed = filtered or all_parsed
    if not parsed:
        raise _FunctionalAttemptFailed(
            "{} search returned no parseable results".format(title)
        )

    pool_limit = int(os.environ.get("LIVE_FALLBACK_POOL_LIMIT", "50"))
    pool = _candidate_pool(parsed, movie["title"], pool_limit)
    if len(pool) < 2:
        raise _FunctionalAttemptFailed(
            "{} search returned too few linked results".format(title)
        )
    return pool


def _release_group_key(result):
    meta = result.get("_meta") if isinstance(result, dict) else {}
    group = meta.get("group", "") if isinstance(meta, dict) else ""
    if not group:
        title = result.get("title", "") if isinstance(result, dict) else ""
        match = re.search(r"-([A-Za-z0-9][A-Za-z0-9._-]{1,40})$", title)
        group = match.group(1) if match else ""
    group = re.sub(r"[^a-z0-9]+", " ", str(group).lower()).strip()
    return " ".join(group.split())


def _most_duplicated_group_pool(pool):
    keys = [_release_group_key(result) for result in pool]
    counts = Counter(key for key in keys if key)
    if not counts:
        return "", []
    best_group, best_count = max(
        counts.items(), key=lambda item: (item[1], -keys.index(item[0]))
    )
    if best_count < 2:
        return "", []
    return best_group, [
        result for result in pool if _release_group_key(result) == best_group
    ]


def _selection_pairs_for_targets(settings, targets, pool):
    required_count = _required_fallback_candidate_count()
    pairs = []
    with _patch_addon_settings(settings):
        for selected in targets:
            pair = _selection_candidate_pair(selected, pool, required_count)
            if pair is not None:
                pairs.append(pair)
    pairs.sort(key=lambda pair: _primary_adoption_rank(pair[0]))
    return pairs


def _movie_selections_with_fallbacks(settings, movie):
    failures = []
    framestor_query = "{} {} FraMeSToR".format(movie["title"], movie["year"])
    try:
        framestor_pool = _movie_search_pool(
            settings, movie, query_title=framestor_query, use_imdb=False
        )
        framestor_targets = [
            result for result in framestor_pool if _looks_like_framestor_release(result)
        ]
        pairs = _selection_pairs_for_targets(
            settings, framestor_targets, framestor_pool
        )
        if pairs:
            return "FrameStor/FraMeSToR", pairs
        failures.append("FrameStor/FraMeSToR produced no same-profile pair")
    except _FunctionalAttemptFailed as exc:
        failures.append(str(exc))

    try:
        regular_pool = _movie_search_pool(settings, movie)
        group, group_pool = _most_duplicated_group_pool(regular_pool)
        if not group_pool:
            raise _FunctionalAttemptFailed("no duplicated release group found")
        pairs = _selection_pairs_for_targets(settings, group_pool, group_pool)
        if pairs:
            return "duplicated group '{}'".format(group), pairs
        failures.append(
            "duplicated group '{}' produced no same-profile pair".format(group)
        )
    except _FunctionalAttemptFailed as exc:
        failures.append(str(exc))

    raise _FunctionalAttemptFailed("; ".join(failures))


def _functional_runtime_config():
    return {
        "download_timeout": float(
            os.environ.get("FUNCTIONAL_DOWNLOAD_TIMEOUT_SECONDS", "1800")
        ),
        "poll_interval": float(os.environ.get("FUNCTIONAL_POLL_INTERVAL_SECONDS", "5")),
        "playback_bytes": int(os.environ.get("FUNCTIONAL_PLAYBACK_BYTES", "262144")),
        "failure_after": int(
            os.environ.get("FUNCTIONAL_PRIMARY_FAILURE_AFTER_BYTES", "8192")
        ),
        "probe_safe_range": int(
            os.environ.get("FUNCTIONAL_PRIMARY_PROBE_SAFE_RANGE_BYTES", "4096")
        ),
        "prevalidate_bytes": int(
            os.environ.get("FUNCTIONAL_PREVALIDATE_BYTES", "4096")
        ),
        "playback_read_timeout": float(
            os.environ.get("FUNCTIONAL_PLAYBACK_READ_TIMEOUT_SECONDS", "300")
        ),
        "fallback_submit_limit": int(
            os.environ.get("FUNCTIONAL_FALLBACK_SUBMIT_LIMIT", "5")
        ),
        "min_completed_fallbacks": int(
            os.environ.get("FUNCTIONAL_MIN_COMPLETED_FALLBACKS", "1")
        ),
    }


def _digest8(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


def _primary_job_name(selected):
    title = "NZBDAV Functional Primary {}".format(selected.get("title", "Matrix"))
    return build_fallback_job_name(title, selected.get("link", ""), 0).replace(
        "[fallback-0-", "[primary-"
    )


def _fallback_job_name(candidate, index):
    return build_fallback_job_name(
        candidate.get("title", ""), candidate.get("link", ""), index
    )


def _primary_adoption_rank(selected):
    job_name = _primary_job_name(selected)
    if find_completed_by_name(job_name):
        return 0
    if find_queued_by_name(job_name):
        return 1
    return 2


def _stream_from_history(job, history):
    storage = history.get("storage", "")
    if not storage:
        raise _FunctionalAttemptFailed(
            "completed {} job {} has no storage path".format(
                job.get("role", "live"), _digest8(job["job_name"])
            )
        )

    webdav_folder = _storage_to_webdav_path(storage)
    video_path = find_video_file(webdav_folder)
    if not video_path:
        raise _FunctionalAttemptFailed(
            "completed {} job {} has no playable WebDAV video".format(
                job.get("role", "live"), _digest8(job["job_name"])
            )
        )

    stream_url, stream_headers = get_webdav_stream_url_for_path(video_path)
    auth_header = (stream_headers or {}).get("Authorization")
    content_length = StreamProxy._get_content_length(stream_url, auth_header)
    if content_length <= 0:
        raise _FunctionalAttemptFailed(
            "completed {} job {} has no Content-Length".format(
                job.get("role", "live"), _digest8(job["job_name"])
            )
        )

    job.update(
        {
            "stream_url": stream_url,
            "stream_headers": stream_headers or {},
            "content_length": content_length,
            "storage": storage,
            "video_path": video_path,
        }
    )
    return job


def _adopt_existing_job(job_name, include_queued=True):
    completed = find_completed_by_name(job_name)
    if completed and completed.get("nzo_id"):
        return {
            "nzo_id": completed["nzo_id"],
            "history": completed,
            "completed": True,
        }

    if not include_queued:
        return None

    queued = find_queued_by_name(job_name)
    if queued and queued.get("nzo_id"):
        return {
            "nzo_id": queued["nzo_id"],
            "history": None,
            "completed": False,
        }

    return None


def _job_failed_by_name(job_name):
    cached = _FAILED_JOB_NAME_CACHE.get(job_name)
    if cached is not None:
        return cached

    addon = xbmcaddon.Addon()
    base_url = addon.getSetting("nzbdav_url").rstrip("/")
    api_key = addon.getSetting("nzbdav_api_key")
    if not base_url or not api_key:
        _FAILED_JOB_NAME_CACHE[job_name] = False
        return False

    search_term = job_name.split(".")[0] if "." in job_name else job_name
    param_sets = [
        {
            "mode": "history",
            "apikey": api_key,
            "output": "json",
            "limit": 500,
            "search": search_term,
        },
        {
            "mode": "history",
            "apikey": api_key,
            "output": "json",
            "limit": 500,
        },
    ]
    for params in param_sets:
        try:
            with urlopen(  # nosec B310
                "{}/api?{}".format(base_url, urlencode(params)), timeout=30
            ) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:  # pylint: disable=broad-except
            continue
        history = payload.get("history", {})
        slots = history.get("slots", []) if isinstance(history, dict) else []
        if not isinstance(slots, list):
            continue
        for slot in slots:
            if slot.get("name") == job_name and slot.get("status") == "Failed":
                _FAILED_JOB_NAME_CACHE[job_name] = True
                return True

    _FAILED_JOB_NAME_CACHE[job_name] = False
    return False


def _submit_or_adopt_job(
    result, job_name, role, submit_if_missing=True, include_queued=True
):
    adopted = _adopt_existing_job(job_name, include_queued=include_queued)
    if adopted is not None:
        return {
            "role": role,
            "title": result.get("title", ""),
            "nzb_url": result.get("link", ""),
            "job_name": job_name,
            "nzo_id": adopted["nzo_id"],
            "stream_url": "",
            "stream_headers": {},
            "content_length": 0,
            "_history": adopted["history"],
            "_completed": adopted["completed"],
        }

    if not submit_if_missing:
        return None

    if _job_failed_by_name(job_name):
        raise _FunctionalAttemptFailed(
            "nzbdav {} job {} previously failed before WebDAV playback".format(
                role, _digest8(job_name)
            )
        )

    nzo_id, submit_error = submit_nzb(result.get("link", ""), job_name)
    if not nzo_id:
        adopted = _adopt_existing_job(job_name, include_queued=include_queued)
        if adopted is not None:
            nzo_id = adopted["nzo_id"]
        else:
            status = submit_error.get("status") if submit_error else "unknown"
            raise _FunctionalAttemptFailed(
                "nzbdav submit failed for {} job {}: {}".format(
                    role, _digest8(job_name), status
                ),
            )

    return {
        "role": role,
        "title": result.get("title", ""),
        "nzb_url": result.get("link", ""),
        "job_name": job_name,
        "nzo_id": nzo_id,
        "stream_url": "",
        "stream_headers": {},
        "content_length": 0,
        "_history": None,
        "_completed": False,
    }


def _resolve_live_job_stream(job, timeout_seconds, poll_interval):
    if job.get("_completed") and job.get("_history"):
        return _stream_from_history(job, job["_history"])

    deadline = time.monotonic() + timeout_seconds
    invisible_polls = 0
    invisible_limit = int(os.environ.get("FUNCTIONAL_INVISIBLE_JOB_POLLS", "6"))
    while time.monotonic() < deadline:
        history = get_job_history(job["nzo_id"])
        if history and history.get("status") == "Completed":
            return _stream_from_history(job, history)
        if history and history.get("status") == "Failed":
            raise _FunctionalAttemptFailed(
                "nzbdav {} job {} failed before WebDAV playback".format(
                    job.get("role", "live"), _digest8(job["job_name"])
                ),
            )
        status = get_job_status(job["nzo_id"])
        if status:
            invisible_polls = 0
            status_text = str(status.get("status", "")).lower()
            if status_text == "failed":
                raise _FunctionalAttemptFailed(
                    "nzbdav {} job {} failed in queue before WebDAV playback".format(
                        job.get("role", "live"), _digest8(job["job_name"])
                    ),
                )
        else:
            invisible_polls += 1
            if invisible_polls >= invisible_limit:
                raise _FunctionalAttemptFailed(
                    "nzbdav {} job {} disappeared from queue/history".format(
                        job.get("role", "live"), _digest8(job["job_name"])
                    )
                )
        time.sleep(poll_interval)

    raise _FunctionalAttemptFailed(
        "timed out waiting for nzbdav {} job {} to complete".format(
            job.get("role", "live"), _digest8(job["job_name"])
        )
    )


def _submit_and_resolve_live_jobs(selected, fallback_candidates):
    runtime = _functional_runtime_config()
    primary_job = _submit_or_adopt_job(selected, _primary_job_name(selected), "primary")
    _resolve_live_job_stream(
        primary_job,
        runtime["download_timeout"],
        runtime["poll_interval"],
    )
    primary_job.pop("_history", None)
    primary_job.pop("_completed", None)

    fallback_jobs = []
    fallback_errors = []
    rows = [
        (index, candidate, _fallback_job_name(candidate, index))
        for index, candidate in enumerate(
            fallback_candidates[: runtime["fallback_submit_limit"]], 1
        )
    ]
    attempted_job_names = set()

    def _try_fallback_candidate(index, candidate, job_name, submit_if_missing):
        fallback_job = _submit_or_adopt_job(
            candidate,
            job_name,
            "fallback-{}".format(index),
            submit_if_missing=submit_if_missing,
            include_queued=submit_if_missing,
        )
        if fallback_job is None:
            return None
        _resolve_live_job_stream(
            fallback_job,
            runtime["download_timeout"],
            runtime["poll_interval"],
        )
        fallback_job.pop("_history", None)
        fallback_job.pop("_completed", None)
        if fallback_job["content_length"] != primary_job["content_length"]:
            raise _FunctionalAttemptFailed(
                "{} job {} length {} did not match primary length {}".format(
                    fallback_job["role"],
                    _digest8(fallback_job["job_name"]),
                    fallback_job["content_length"],
                    primary_job["content_length"],
                )
            )
        return fallback_job

    for submit_if_missing in (False, True):
        for index, candidate, job_name in rows:
            if (
                submit_if_missing
                and len(fallback_jobs) >= runtime["min_completed_fallbacks"]
            ):
                break
            if submit_if_missing and job_name in attempted_job_names:
                continue
            try:
                fallback_job = _try_fallback_candidate(
                    index, candidate, job_name, submit_if_missing
                )
            except _FunctionalAttemptFailed as exc:
                fallback_errors.append(str(exc))
                attempted_job_names.add(job_name)
                continue
            if fallback_job is None:
                continue
            attempted_job_names.add(job_name)
            fallback_jobs.append(fallback_job)
        if len(fallback_jobs) >= runtime["min_completed_fallbacks"]:
            break

    if len(fallback_jobs) < runtime["min_completed_fallbacks"]:
        detail = "; ".join(fallback_errors[:5]) if fallback_errors else "none"
        raise _FunctionalAttemptFailed(
            "only {} completed same-length fallback job(s); recent failures: {}".format(
                len(fallback_jobs), detail
            )
        )

    return primary_job, fallback_jobs


def _parse_range_header(range_header, total):
    if not range_header:
        return 0, total - 1
    if not range_header.startswith("bytes="):
        raise ValueError("unsupported range")
    start_text, end_text = range_header[len("bytes=") :].split("-", 1)
    if not start_text:
        suffix = int(end_text)
        return max(0, total - suffix), total - 1
    start = int(start_text)
    end = int(end_text) if end_text else total - 1
    if start < 0 or end < start or start >= total:
        raise ValueError("invalid range")
    return start, min(end, total - 1)


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.upstream_url = ""
        self.upstream_headers = {}
        self.content_length = 0
        self.content_type = ""
        self.primary_failure_after_bytes = 0
        self.probe_safe_range_bytes = 0
        self.events = []


class _FailingPrimaryWebdavHandler(BaseHTTPRequestHandler):
    server_version = "NZBDAVFunctionalPrimary/1.0"

    def log_message(self, *_args):
        return

    def do_HEAD(self):
        self._send_primary(include_body=False)

    def do_GET(self):
        self._send_primary(include_body=True)

    def _send_primary(self, include_body):
        if self.path != "/primary.mkv":
            self.send_error(404)
            return

        try:
            start, end = _parse_range_header(
                self.headers.get("Range"), self.server.content_length
            )
        except (TypeError, ValueError):
            self.send_error(416)
            return

        requested = end - start + 1
        should_short_read = (
            include_body and requested > self.server.probe_safe_range_bytes
        )
        body = b""
        if include_body:
            upstream_request = Request(self.server.upstream_url)
            for key, value in self.server.upstream_headers.items():
                upstream_request.add_header(key, value)
            upstream_request.add_header("Range", "bytes={}-{}".format(start, end))
            read_size = (
                self.server.primary_failure_after_bytes
                if should_short_read
                else requested
            )
            with urlopen(upstream_request, timeout=60) as response:  # nosec B310
                body = response.read(read_size)

        self.server.events.append((self.path, self.command, start, end, len(body)))
        self.send_response(206 if self.headers.get("Range") else 200)
        self.send_header("Content-Type", self.server.content_type)
        self.send_header("Accept-Ranges", "bytes")
        if include_body:
            content_length = len(body)
        elif self.headers.get("Range"):
            content_length = requested
        else:
            content_length = self.server.content_length
        self.send_header("Content-Length", str(content_length))
        if self.headers.get("Range"):
            self.send_header(
                "Content-Range",
                "bytes {}-{}/{}".format(start, end, self.server.content_length),
            )
        self.send_header("Connection", "close")
        self.end_headers()
        if include_body:
            self.wfile.write(body)
            self.wfile.flush()
        if should_short_read:
            # pylint: disable-next=attribute-defined-outside-init
            self.close_connection = True


@contextlib.contextmanager
def _failing_primary_proxy(primary_job, runtime):
    server = _ThreadedHTTPServer(("127.0.0.1", 0), _FailingPrimaryWebdavHandler)
    server.upstream_url = primary_job["stream_url"]
    server.upstream_headers = dict(primary_job.get("stream_headers") or {})
    server.content_length = primary_job["content_length"]
    server.content_type = "video/x-matroska"
    server.primary_failure_after_bytes = runtime["failure_after"]
    server.probe_safe_range_bytes = runtime["probe_safe_range"]
    server.events = []
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    try:
        base_url = "http://127.0.0.1:{}".format(server.server_address[1])
        yield server, base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextlib.contextmanager
def _stream_proxy():
    proxy = StreamProxy()
    proxy.start()
    try:
        yield proxy
    finally:
        proxy.stop()


def _read_proxy_range(proxy_url, end):
    request = Request(proxy_url)
    request.add_header("Range", "bytes=0-{}".format(end))
    with urlopen(
        request, timeout=_functional_runtime_config()["playback_read_timeout"]
    ) as response:  # nosec B310
        return response.read()


def _read_webdav_range(url, headers, end, start=0):
    request = Request(url)
    request.add_header("Range", "bytes={}-{}".format(start, end))
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urlopen(request, timeout=60) as response:  # nosec B310
        return response.read()


def _prevalidate_live_fallbacks(primary_job, fallback_jobs, runtime):
    if primary_job["content_length"] <= 0:
        raise _FunctionalAttemptFailed("primary has no content length to validate")
    sample_end = min(runtime["prevalidate_bytes"], primary_job["content_length"]) - 1
    if sample_end < 0:
        raise _FunctionalAttemptFailed("primary too small to validate")
    primary_sample = _read_webdav_range(
        primary_job["stream_url"],
        primary_job["stream_headers"],
        sample_end,
    )
    validated = []
    for job in fallback_jobs:
        if job["content_length"] != primary_job["content_length"]:
            continue
        fallback_sample = _read_webdav_range(
            job["stream_url"],
            job["stream_headers"],
            sample_end,
        )
        if fallback_sample != primary_sample:
            continue
        job["validated"] = True
        validated.append(job)
    if not validated:
        raise _FunctionalAttemptFailed("no fallback passed pre-playback range test")
    return validated


def _verify_live_fallback_playback(settings, runtime, primary_job, fallback_jobs):
    playback_end = (
        min(
            runtime["playback_bytes"],
            primary_job["content_length"],
            *(job["content_length"] for job in fallback_jobs),
        )
        - 1
    )
    if playback_end < runtime["failure_after"]:
        raise _FunctionalAttemptFailed("live stream too small for failure simulation")

    with _failing_primary_proxy(primary_job, runtime) as (
        primary_proxy,
        primary_proxy_base,
    ), _stream_proxy() as proxy:
        playback_settings = dict(settings)
        playback_settings["webdav_url"] = primary_proxy_base
        playback_settings["nzbdav_url"] = settings["webdav_url"]
        with _patch_addon_settings(playback_settings):
            proxy_url, stream_info = proxy.prepare_stream(
                "{}/primary.mkv".format(primary_proxy_base),
                fallback_sources=fallback_jobs,
            )
            played_bytes = _read_proxy_range(proxy_url, playback_end)

        ctx = proxy._server.stream_context

    active_index = ctx["fallback_active_index"]
    if active_index < 0 or active_index >= len(fallback_jobs):
        raise _FunctionalAttemptFailed("stream proxy did not switch to a live fallback")

    active_fallback = fallback_jobs[active_index]
    expected_bytes = _read_webdav_range(
        active_fallback["stream_url"],
        active_fallback["stream_headers"],
        playback_end,
    )

    if played_bytes != expected_bytes:
        raise _FunctionalAttemptFailed("played bytes did not match active fallback")
    if len(stream_info["fallback_sources"]) < len(fallback_jobs):
        raise _FunctionalAttemptFailed("prepare response dropped fallback sources")
    if ctx["fallback_switch_count"] != 1:
        raise _FunctionalAttemptFailed(
            "stream proxy did not record one fallback switch"
        )
    if active_fallback["nzo_id"].startswith("functional-fallback-"):
        raise _FunctionalAttemptFailed("fallback nzo_id was synthetic")
    if ctx["fallback_sources"][active_index]["nzo_id"] != active_fallback["nzo_id"]:
        raise _FunctionalAttemptFailed("active fallback nzo_id did not match")
    if ctx["fallback_sources"][active_index]["nzb_url"] != active_fallback["nzb_url"]:
        raise _FunctionalAttemptFailed("active fallback NZB URL did not match")
    if not all(
        job["stream_url"].startswith(settings["webdav_url"]) for job in fallback_jobs
    ):
        raise _FunctionalAttemptFailed("fallback stream URL did not use live WebDAV")
    if not any(
        event[0] == "/primary.mkv" and event[4] == runtime["failure_after"]
        for event in primary_proxy.events
    ):
        raise _FunctionalAttemptFailed("primary failure proxy did not short-read")


def _exercise_live_fallback_selections(
    settings, runtime, selections, attempt_limit=None
):
    attempts = []
    limited_selections = selections
    if attempt_limit is not None:
        limited_selections = selections[: max(0, int(attempt_limit))]
    for selected, fallback_candidates in limited_selections:
        try:
            with _patch_addon_settings(settings):
                primary_job, fallback_jobs = _submit_and_resolve_live_jobs(
                    selected, fallback_candidates
                )
            fallback_jobs = _prevalidate_live_fallbacks(
                primary_job, fallback_jobs, runtime
            )
            _verify_live_fallback_playback(
                settings, runtime, primary_job, fallback_jobs
            )
            return
        except _FunctionalAttemptFailed as exc:
            attempts.append(
                "{}: {}".format(
                    _digest8(selected.get("link", selected.get("title", ""))),
                    str(exc),
                )
            )
            continue
    raise _FunctionalAttemptFailed(
        "; ".join(attempts[:10]) if attempts else "no attempts"
    )


def _looks_like_availability_failure(error):
    text = str(error).lower()
    markers = (
        "nzbdav ",
        "timed out waiting",
        "disappeared from queue/history",
        "completed same-length fallback",
        "did not match primary length",
        "no fallback passed pre-playback range test",
        "has no playable webdav video",
        "has no content-length",
        "has no storage path",
        "primary has no content length",
        "live stream too small",
    )
    return any(marker in text for marker in markers)


def test_functional_matrix_fallback_playback_submits_to_nzbdav_and_switches():
    env = _live_env()
    settings = _addon_settings(env)
    runtime = _functional_runtime_config()

    try:
        _exercise_live_fallback_selections(
            settings, runtime, _matrix_selections_with_fallbacks(settings)
        )
    except _FunctionalAttemptFailed as exc:
        pytest.fail(
            "No same-profile Matrix fallback set completed live playback: {}".format(
                exc
            )
        )


def test_functional_imdb_top50_random_sample_fallback_playback():
    env = _live_env()
    settings = _addon_settings(env)
    runtime = _functional_runtime_config()
    seed, target_count, movies = _top_imdb_sample()
    attempt_limit = int(os.environ.get("FUNCTIONAL_TOP_IMDB_SELECTION_LIMIT", "3"))
    failures = []
    unavailable = []
    successes = []

    print(
        "IMDb Top 50 fallback sample seed={} target_count={}".format(
            seed, target_count
        ),
        flush=True,
    )
    for movie in movies:
        label = "#{rank} {title} ({year})".format(**movie)
        try:
            strategy, selections = _movie_selections_with_fallbacks(settings, movie)
        except _FunctionalAttemptFailed as exc:
            unavailable.append("{}: {}".format(label, exc))
            print("SKIP {}: {}".format(label, exc), flush=True)
            continue

        try:
            _exercise_live_fallback_selections(
                settings, runtime, selections, attempt_limit=attempt_limit
            )
        except _FunctionalAttemptFailed as exc:
            if _looks_like_availability_failure(exc):
                unavailable.append("{} via {}: {}".format(label, strategy, exc))
                print("SKIP {} via {}: {}".format(label, strategy, exc), flush=True)
            else:
                failures.append("{} via {}: {}".format(label, strategy, exc))
                print("FAIL {}: {}".format(label, exc), flush=True)
            continue

        successes.append("{} via {}".format(label, strategy))
        print("PASS {} via {}".format(label, strategy), flush=True)
        if len(successes) >= target_count:
            break

    if failures:
        pytest.fail(
            "{} IMDb Top 50 fallback playback run(s) failed after "
            "{} success(es): {}".format(
                len(failures), len(successes), "; ".join(failures)
            ),
            pytrace=False,
        )

    if len(successes) < target_count:
        detail = "; ".join(unavailable[:10]) if unavailable else "none"
        pytest.fail(
            "only {} of {} requested IMDb Top 50 fallback runs were available; "
            "unavailable: {}".format(len(successes), target_count, detail),
            pytrace=False,
        )

    if unavailable:
        print(
            "Unavailable IMDb Top 50 fallback candidates: {}".format(
                "; ".join(unavailable)
            ),
            flush=True,
        )

    assert len(successes) == target_count
