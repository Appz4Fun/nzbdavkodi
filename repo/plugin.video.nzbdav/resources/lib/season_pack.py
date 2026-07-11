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
import uuid
from contextlib import contextmanager
from typing import NamedTuple

import xbmc
import xbmcvfs

try:
    import fcntl
except ImportError:  # pragma: no cover - CoreELEC and macOS provide fcntl
    fcntl = None

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
_STALE_LOCK_SECONDS = 30.0


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


def _pid_is_alive(pid):
    """Return True unless the OS conclusively reports that ``pid`` is dead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    return True


def _directory_lock_owner(lock_dir):
    try:
        owner_path = os.path.join(lock_dir, "owner.json")
        with open(owner_path, "r", encoding="utf-8") as handle:
            owner = json.load(handle)
        return int(owner["pid"]), str(owner["token"])
    except (KeyError,) + _IO_ERRORS:
        return None


def _remove_stale_directory_lock(lock_dir):
    """Remove a sufficiently old fallback lock with no live valid owner."""
    owner_path = os.path.join(lock_dir, "owner.json")
    try:
        newest_mtime = os.path.getmtime(lock_dir)
        try:
            newest_mtime = max(newest_mtime, os.path.getmtime(owner_path))
        except FileNotFoundError:
            pass
        age = time.time() - newest_mtime
    except OSError:
        return
    owner = _directory_lock_owner(lock_dir)
    if age < _STALE_LOCK_SECONDS or (owner is not None and _pid_is_alive(owner[0])):
        return

    if owner is None:
        try:
            os.rmdir(lock_dir)
            return
        except OSError:
            # A concurrent owner write or malformed owner file needs revalidation.
            pass

    # Recheck both freshness and ownership immediately before removing owner.json.
    # This preserves a creator that completed its owner write during our first read.
    try:
        newest_mtime = max(os.path.getmtime(lock_dir), os.path.getmtime(owner_path))
        owner = _directory_lock_owner(lock_dir)
        if time.time() - newest_mtime < _STALE_LOCK_SECONDS:
            return
        if owner is not None and _pid_is_alive(owner[0]):
            return
        os.remove(owner_path)
        os.rmdir(lock_dir)
    except OSError:
        # Another process may have repaired or removed the lock; cleanup is best-effort.
        pass


def _acquire_directory_lock(lock_path, deadline):
    """Portable fallback for platforms without ``fcntl``."""
    lock_dir = lock_path + ".d"
    token = uuid.uuid4().hex
    while True:
        created = False
        try:
            os.mkdir(lock_dir)
            created = True
            with open(
                os.path.join(lock_dir, "owner.json"), "w", encoding="utf-8"
            ) as handle:
                json.dump({"pid": os.getpid(), "token": token}, handle)
            return ("directory", lock_dir, token)
        except FileExistsError:
            _remove_stale_directory_lock(lock_dir)
        except _IO_ERRORS:
            if created:
                try:
                    os.remove(os.path.join(lock_dir, "owner.json"))
                except OSError:
                    # Rollback is best-effort after this process failed to
                    # publish ownership.
                    pass
                try:
                    os.rmdir(lock_dir)
                except OSError:
                    # Preserve any lock state that another process created
                    # during rollback.
                    pass
            return None
        if time.monotonic() >= deadline:
            return None
        threading.Event().wait(_LOCK_RETRY)


def _acquire_process_lock(lock_path):
    deadline = time.monotonic() + _LOCK_TIMEOUT
    if fcntl is None:
        return _acquire_directory_lock(lock_path, deadline)
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
    kind, resource, token = lock
    if kind == "fcntl":
        try:
            fcntl.flock(resource.fileno(), fcntl.LOCK_UN)
        except OSError:
            # Closing the handle below still releases the OS lock after unlock failure.
            pass
        resource.close()
        return
    owner = _directory_lock_owner(resource)
    if owner != (os.getpid(), token):
        return
    try:
        os.remove(os.path.join(resource, "owner.json"))
        os.rmdir(resource)
    except OSError:
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


def validate_job(record, settings_getter=None):
    """Validate an exact catalog job ID and backend-native folder.

    Only a conclusive successful history lookup can remove a stale record;
    transient API/auth/network/parse failures preserve it for a later retry.
    """
    if not isinstance(record, dict):
        return PackValidation(None, "transient")
    backend = record.get("backend")
    if backend == "nzbget":
        from resources.lib.nzbget_api import completed_by_id

        lookup = completed_by_id(record.get("job_id"), settings_getter=settings_getter)
        folder_key = "dest_dir"
    elif backend == "nzbdav":
        from resources.lib.nzbdav_api import completed_by_id

        lookup = completed_by_id(record.get("job_id"), settings_getter=settings_getter)
        folder_key = "storage"
    else:
        return PackValidation(None, "transient")
    if not lookup.lookup_done:
        return PackValidation(None, "transient")
    job_folder = lookup.job.get(folder_key) if lookup.job is not None else None
    if lookup.job is None or str(job_folder or "") != str(record.get("folder") or ""):
        remove(backend, record.get("job_id"))
        return PackValidation(None, "stale")
    return PackValidation(lookup.job, "valid")


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
    if not title or title != _normalize_title(context.get("title")):
        return False
    record_year = _number(record.get("year"))
    context_year = _number(context.get("year"))
    if record_year is None or context_year is None:
        return record_year is None and context_year is None
    return record_year == context_year


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
