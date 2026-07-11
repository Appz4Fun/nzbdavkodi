# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Persistent, job-isolated catalog of completed season-pack inventories."""

import json
import math
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import NamedTuple

import xbmc
import xbmcvfs

try:
    import fcntl
except ImportError:  # pragma: no cover - CoreELEC and macOS provide fcntl
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - msvcrt is available only on Windows
    msvcrt = None

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
    "year",
    "imdb",
    "tvdb",
    "tmdb_id",
    "season",
    "episodes",
    "last_confirmed",
)
_TITLE_RE = re.compile(r"[^a-z0-9]+")
_IO_ERRORS = (IOError, OSError, TypeError, ValueError)
_THREAD_LOCK = threading.RLock()
_LOCK_TIMEOUT = 2.0
_LOCK_RETRY = 0.02


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


def _record_requirements(record):
    """Return validated required catalog fields, or ``None``."""
    backend = _text(record.get("backend"))
    job_id = _text(record.get("job_id"))
    folder = _text(record.get("folder"))
    season = _number(record.get("season"))
    episodes = _normalize_episodes(record.get("episodes"))
    if backend not in ("nzbget", "nzbdav"):
        return None
    if not job_id or not folder:
        return None
    if season is None or season < 0:
        return None
    if not episodes:
        return None
    return backend, job_id, folder, season, episodes


def _normalized_record_values(record, requirements, default_timestamp):
    backend, job_id, folder, season, episodes = requirements
    return {
        "backend": backend,
        "job_id": job_id,
        "job_name": _text(record.get("job_name")),
        "folder": folder,
        "title": _text(record.get("title")),
        "year": _number(record.get("year")),
        "imdb": _text(record.get("imdb")),
        "tvdb": _text(record.get("tvdb")),
        "tmdb_id": _text(record.get("tmdb_id")),
        "season": season,
        "episodes": episodes,
        "last_confirmed": _timestamp(
            record.get("last_confirmed"), default=default_timestamp
        ),
    }


def _normalize_record(record, default_timestamp=0.0):
    if not isinstance(record, dict):
        return None
    requirements = _record_requirements(record)
    if requirements is None:
        return None
    normalized = _normalized_record_values(record, requirements, default_timestamp)
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


def _load_records_unlocked(path=None):
    """Load rows while the caller owns any required transaction lock."""
    path = path or _catalog_path()
    if not path:
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except _IO_ERRORS:
        return []
    records = payload.get("records") if isinstance(payload, dict) else None
    return _canonical_records(records)


def load_records():
    """Load valid catalog rows, returning an empty list on any read failure."""
    return _load_records_unlocked()


def _discard_temp(fd, tmp_path):
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            # Cleanup is best-effort after the catalog write has already failed.
            pass
    if tmp_path:
        try:
            os.remove(tmp_path)
        except OSError:
            # A missing or busy temp file must not hide the original write failure.
            pass


def _save_records_unlocked(records, path=None):
    """Atomically save rows while the caller owns the transaction lock."""
    path = path or _catalog_path()
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
            # Catalog persistence stays fail-soft when Kodi logging is unavailable.
            pass
        return False


def _close_lock_handle(handle):
    if handle is None:
        return
    try:
        handle.close()
    except _IO_ERRORS:
        # Lock cleanup is best-effort after setup or acquisition failure.
        pass


def _initialize_msvcrt_lock(lock_path):
    """Open and initialize the byte used by the Windows locking primitive."""
    handle = None
    try:
        handle = open(lock_path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
    except _IO_ERRORS:
        _close_lock_handle(handle)
        return None
    return handle


def _acquire_msvcrt_lock(lock_path, deadline):
    """Acquire a Windows-owned nonblocking byte lock with bounded retry."""
    handle = _initialize_msvcrt_lock(lock_path)
    if handle is None:
        return None

    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return ("msvcrt", handle, None)
        except _IO_ERRORS:
            if time.monotonic() >= deadline:
                _close_lock_handle(handle)
                return None
            threading.Event().wait(_LOCK_RETRY)


def _fcntl_available():
    """Return whether the POSIX lock primitive is available."""
    return callable(getattr(fcntl, "flock", None))


def _msvcrt_available():
    """Return whether the Windows lock primitive is available."""
    return callable(getattr(msvcrt, "locking", None))


def _acquire_process_lock(lock_path):
    deadline = time.monotonic() + _LOCK_TIMEOUT
    if not _fcntl_available():
        if not _msvcrt_available():
            return None
        return _acquire_msvcrt_lock(lock_path, deadline)
    try:
        handle = open(lock_path, "a+", encoding="utf-8")
    except _IO_ERRORS:
        return None
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return ("fcntl", handle, None)
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                handle.close()
                return None
            threading.Event().wait(_LOCK_RETRY)


def _release_process_lock(lock):
    kind, resource, _token = lock
    if kind == "fcntl":
        try:
            fcntl.flock(resource.fileno(), fcntl.LOCK_UN)
        except OSError:
            # Closing the handle below still releases the OS lock after unlock failure.
            pass
        resource.close()
        return
    if kind != "msvcrt":
        return
    try:
        resource.seek(0)
        msvcrt.locking(resource.fileno(), msvcrt.LK_UNLCK, 1)
    except _IO_ERRORS:
        # Closing the handle still releases the OS-owned lock after unlock failure.
        pass
    try:
        resource.close()
    except _IO_ERRORS:
        # Catalog operations are fail-soft even if Windows reports close failure.
        pass


@contextmanager
def _catalog_transaction():
    """Serialize one catalog mutation across this process and other processes."""
    with _THREAD_LOCK:
        path = _catalog_path()
        if not path:
            yield ""
            return
        lock = _acquire_process_lock(path + ".lock")
        if lock is None:
            yield ""
            return
        try:
            yield path
        finally:
            _release_process_lock(lock)


def save_records(records):
    """Atomically save normalized rows; catalog failures never escape."""
    with _catalog_transaction() as path:
        if not path:
            return False
        return _save_records_unlocked(records, path=path)


def upsert(record):
    """Insert or refresh one exact ``(backend, job_id)`` catalog row."""
    normalized = _normalize_record(record, default_timestamp=time.time())
    if normalized is None:
        return False
    with _catalog_transaction() as path:
        if not path:
            return False
        rows = [
            row
            for row in _load_records_unlocked(path=path)
            if _job_key(row) != _job_key(normalized)
        ]
        rows.append(normalized)
        return _save_records_unlocked(rows, path=path)


def remove(backend, job_id):
    """Remove only the row with the exact backend and job identifier."""
    key = _text(backend), _text(job_id)
    with _catalog_transaction() as path:
        if not path:
            return False
        rows = _load_records_unlocked(path=path)
        remaining = [row for row in rows if _job_key(row) != key]
        if len(remaining) == len(rows):
            return True
        return _save_records_unlocked(remaining, path=path)


def find_exact(backend, job_id):
    """Return the newest row for an exact backend and job identifier."""
    key = _text(backend), _text(job_id)
    matches = [row for row in load_records() if _job_key(row) == key]
    if not matches:
        return None
    return max(matches, key=lambda row: row.get("last_confirmed", 0.0))


class PackValidation(NamedTuple):
    """Exact backend-history validation for one catalog record."""

    job: object
    outcome: str


def _completed_job_lookup(record, settings_getter):
    """Return ``(backend, lookup, folder_key)`` for a supported backend."""
    backend = record.get("backend")
    if backend == "nzbget":
        from resources.lib.nzbget_api import completed_by_id

        lookup = completed_by_id(record.get("job_id"), settings_getter=settings_getter)
        return backend, lookup, "dest_dir"
    if backend == "nzbdav":
        from resources.lib.nzbdav_api import completed_by_id

        lookup = completed_by_id(record.get("job_id"), settings_getter=settings_getter)
        return backend, lookup, "storage"
    return None


def _remove_invalid_job(backend, job_id):
    try:
        removed = remove(backend, job_id)
    except Exception:  # pylint: disable=broad-except
        return PackValidation(None, "transient")
    outcome = "stale" if removed else "transient"
    return PackValidation(None, outcome)


def _lookup_folder_matches(lookup, folder_key, record):
    if lookup.job is None:
        return False
    job_folder = lookup.job.get(folder_key)
    return str(job_folder or "") == str(record.get("folder") or "")


def validate_job(record, settings_getter=None):
    """Validate an exact catalog job ID and backend-native folder.

    Only a conclusive successful history lookup can remove a stale record;
    transient API/auth/network/parse failures preserve it for a later retry.
    """
    if not isinstance(record, dict):
        return PackValidation(None, "transient")
    lookup_details = _completed_job_lookup(record, settings_getter)
    if lookup_details is None:
        return PackValidation(None, "transient")
    backend, lookup, folder_key = lookup_details
    if not lookup.lookup_done:
        return PackValidation(None, "transient")
    if not _lookup_folder_matches(lookup, folder_key, record):
        return _remove_invalid_job(backend, record.get("job_id"))
    return PackValidation(lookup.job, "valid")


def _matching_strong_ids(record, context):
    return [field for field in _STRONG_IDS if record.get(field) and context.get(field)]


def _strong_ids_match(record, context, common):
    return all(
        _text(record.get(field)).casefold() == _text(context.get(field)).casefold()
        for field in common
    )


def _has_strong_id(values):
    return any(values.get(field) for field in _STRONG_IDS)


def _fallback_identity_matches(record, context):
    title = _normalize_title(record.get("title"))
    if not title:
        return False
    if title != _normalize_title(context.get("title")):
        return False
    record_year = _number(record.get("year"))
    context_year = _number(context.get("year"))
    if record_year is None or context_year is None:
        return record_year is None and context_year is None
    return record_year == context_year


def _content_matches(record, context):
    common = _matching_strong_ids(record, context)
    if common:
        return _strong_ids_match(record, context, common)
    if _has_strong_id(record) or _has_strong_id(context):
        return False
    return _fallback_identity_matches(record, context)


def _episode_record_matches(row, backend, season, episode, context):
    if row.get("backend") != backend:
        return False
    if row.get("season") != season:
        return False
    if episode not in row.get("episodes", []):
        return False
    return _content_matches(row, context)


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
        if _episode_record_matches(row, backend, season, episode, context)
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
        "year": _number(params.get("year")),
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
