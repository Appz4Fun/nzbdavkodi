# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Persistent, job-isolated catalog of completed season-pack inventories."""

import json
import math
import os
import re
import tempfile
import time

import xbmc
import xbmcvfs

_PROFILE_SPECIAL_PATH = "special://profile/addon_data/plugin.video.nzbdav"
_CATALOG_FILENAME = "season_packs.json"
_MAX_RECORDS = 100
_STRONG_IDS = ("tvdb", "tmdb_id", "imdb")
_RECORD_FIELDS = (
    "backend",
    "job_id",
    "job_name",
    "folder",
    "title",
    "imdb",
    "tvdb",
    "tmdb_id",
    "season",
    "episodes",
    "last_confirmed",
)
_TITLE_RE = re.compile(r"[^a-z0-9]+")
_IO_ERRORS = (IOError, OSError, TypeError, ValueError)


def _catalog_dir():
    """Return the translated add-on profile, or empty text when unavailable."""
    try:
        path = xbmcvfs.translatePath(_PROFILE_SPECIAL_PATH)
        if not isinstance(path, str) or not path:
            return ""
        os.makedirs(path, exist_ok=True)
        return path
    except (AttributeError, RuntimeError) + _IO_ERRORS:
        return ""


def _catalog_path():
    directory = _catalog_dir()
    if not directory:
        return ""
    return os.path.join(directory, _CATALOG_FILENAME)


def _text(value):
    return str(value).strip() if value is not None else ""


def _number(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _timestamp(value, default=0.0):
    try:
        timestamp = float(value)
    except (OverflowError, TypeError, ValueError):
        timestamp = float(default)
    if math.isfinite(timestamp):
        return timestamp
    return 0.0


def _normalize_title(value):
    return _TITLE_RE.sub(" ", _text(value).casefold()).strip()


def _job_key(record):
    return _text(record.get("backend")), _text(record.get("job_id"))


def _normalize_episodes(values):
    if not isinstance(values, list):
        return []
    episodes = set()
    for value in values:
        number = _number(value)
        if number is not None and number >= 0:
            episodes.add(number)
    return sorted(episodes)


def _normalize_record(record, default_timestamp=0.0):
    if not isinstance(record, dict):
        return None
    backend = _text(record.get("backend"))
    job_id = _text(record.get("job_id"))
    folder = _text(record.get("folder"))
    season = _number(record.get("season"))
    episodes = _normalize_episodes(record.get("episodes"))
    if backend not in ("nzbget", "nzbdav") or not job_id or not folder:
        return None
    if season is None or season < 0 or not episodes:
        return None
    normalized = {
        "backend": backend,
        "job_id": job_id,
        "job_name": _text(record.get("job_name")),
        "folder": folder,
        "title": _text(record.get("title")),
        "imdb": _text(record.get("imdb")),
        "tvdb": _text(record.get("tvdb")),
        "tmdb_id": _text(record.get("tmdb_id")),
        "season": season,
        "episodes": episodes,
        "last_confirmed": _timestamp(
            record.get("last_confirmed"), default=default_timestamp
        ),
    }
    return {field: normalized[field] for field in _RECORD_FIELDS}


def _canonical_records(records):
    """Normalize rows and retain the newest row for each exact job key."""
    by_key = {}
    for record in records if isinstance(records, (list, tuple)) else []:
        normalized = _normalize_record(record)
        if normalized is None:
            continue
        key = _job_key(normalized)
        previous = by_key.get(key)
        if (
            previous is None
            or normalized["last_confirmed"] >= previous["last_confirmed"]
        ):
            by_key[key] = normalized
    return sorted(by_key.values(), key=lambda row: row["last_confirmed"])[
        -_MAX_RECORDS:
    ]


def load_records():
    """Load valid catalog rows, returning an empty list on any read failure."""
    path = _catalog_path()
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except _IO_ERRORS:
        return []
    records = payload.get("records") if isinstance(payload, dict) else None
    return _canonical_records(records)


def _discard_temp(fd, tmp_path):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    if tmp_path:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def save_records(records):
    """Atomically save normalized rows; catalog failures never escape."""
    path = _catalog_path()
    if not path:
        return False
    directory = os.path.dirname(path)
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix="season-pack-", suffix=".json", dir=directory
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(
                {"version": 1, "records": _canonical_records(records)},
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
        os.replace(tmp_path, path)
        return True
    except _IO_ERRORS as error:
        _discard_temp(fd, tmp_path)
        try:
            xbmc.log(
                "NZB-DAV: season-pack catalog write failed: {}".format(error),
                xbmc.LOGWARNING,
            )
        except (AttributeError, RuntimeError):
            pass
        return False


def upsert(record):
    """Insert or refresh one exact ``(backend, job_id)`` catalog row."""
    normalized = _normalize_record(record, default_timestamp=time.time())
    if normalized is None:
        return False
    rows = [row for row in load_records() if _job_key(row) != _job_key(normalized)]
    rows.append(normalized)
    return save_records(rows)


def remove(backend, job_id):
    """Remove only the row with the exact backend and job identifier."""
    key = _text(backend), _text(job_id)
    rows = load_records()
    remaining = [row for row in rows if _job_key(row) != key]
    if len(remaining) == len(rows):
        return True
    return save_records(remaining)


def find_exact(backend, job_id):
    """Return the newest row for an exact backend and job identifier."""
    key = _text(backend), _text(job_id)
    matches = [row for row in load_records() if _job_key(row) == key]
    if not matches:
        return None
    return max(matches, key=lambda row: row.get("last_confirmed", 0.0))


def _content_matches(record, context):
    common = [
        field for field in _STRONG_IDS if record.get(field) and context.get(field)
    ]
    if common:
        return all(
            _text(record.get(field)).casefold() == _text(context.get(field)).casefold()
            for field in common
        )
    if any(record.get(field) for field in _STRONG_IDS) or any(
        context.get(field) for field in _STRONG_IDS
    ):
        return False
    title = _normalize_title(record.get("title"))
    return bool(title) and title == _normalize_title(context.get("title"))


def find_for_episode(context, backend):
    """Find the newest matching pack containing one requested episode."""
    if not isinstance(context, dict):
        return None
    season = _number(context.get("season"))
    episode = _number(context.get("episode"))
    if season is None or episode is None:
        return None
    backend = _text(backend)
    matches = [
        row
        for row in load_records()
        if row.get("backend") == backend
        and row.get("season") == season
        and episode in row.get("episodes", [])
        and _content_matches(row, context)
    ]
    if not matches:
        return None
    return max(matches, key=lambda row: row.get("last_confirmed", 0.0))


def context_from_params(params, title=None, season=None, episode=None):
    """Build normalized episode context from router parameters and overrides."""
    params = params if isinstance(params, dict) else {}
    return {
        "type": params.get("type", "movie"),
        "title": title if title is not None else params.get("title", ""),
        "imdb": params.get("imdb", ""),
        "tvdb": params.get("tvdb", ""),
        "tmdb_id": params.get("tmdb_id", ""),
        "season": _number(season if season is not None else params.get("season")),
        "episode": _number(episode if episode is not None else params.get("episode")),
    }


def requested_episode(context):
    """Return a normalized ``(season, episode)`` tuple, or ``None``."""
    if not isinstance(context, dict) or context.get("type") != "episode":
        return None
    season = _number(context.get("season"))
    episode = _number(context.get("episode"))
    if season is None or episode is None or season < 0 or episode < 0:
        return None
    return season, episode


def episode_summary(episodes):
    """Return a compact ordered episode range or comma-separated list."""
    values = _normalize_episodes(episodes)
    if not values:
        return ""
    if values == list(range(values[0], values[-1] + 1)):
        if len(values) == 1:
            return str(values[0])
        return "{}-{}".format(values[0], values[-1])
    return ", ".join(str(value) for value in values)


def picker_result(record, label):
    """Build the internal picker row for a previously downloaded pack."""
    backend = record.get("backend") if isinstance(record, dict) else ""
    job_name = record.get("job_name", "") if isinstance(record, dict) else ""
    return {
        "title": job_name,
        "link": "",
        "size": "",
        "age": "",
        "indexer": "NZBGet" if backend == "nzbget" else "nzbdav",
        "_display_title": label,
        "_available": True,
        "_season_pack": dict(record),
        "_meta": {},
    }
