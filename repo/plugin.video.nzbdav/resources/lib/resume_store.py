# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Stable playback resume state for streams hidden behind proxy URLs."""

import base64
import json
import os
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

import xbmc
import xbmcvfs

_STORE_PATH = "special://profile/addon_data/plugin.video.nzbdav/resume.json"
_MIN_RESUME_SECONDS = 5.0
_NEAR_END_SECONDS = 120.0
_MAX_ITEMS = 256


def _store_path(path=None):
    if path:
        return path
    try:
        return xbmcvfs.translatePath(_STORE_PATH)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return _STORE_PATH


def _identity_netloc(parts):
    """Return host[:port] for a URL, bracketing IPv6 hosts."""
    hostname = parts.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        netloc = "[{}]".format(hostname)
    else:
        netloc = hostname
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        netloc = "{}:{}".format(netloc, port)
    return netloc


def _resume_identity(key):
    """Return a stable stream identity without credentials or transient parts."""
    if not key:
        return ""
    key = str(key)
    try:
        parts = urlsplit(key)
    except ValueError:
        return key
    if not parts.scheme or not parts.netloc:
        return key
    netloc = _identity_netloc(parts)
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _resume_id(key):
    identity = _resume_identity(key)
    if not identity:
        return ""
    encoded = base64.urlsafe_b64encode(identity.encode("utf-8")).decode("ascii")
    return "url:" + encoded.rstrip("=")


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (IOError, OSError, TypeError, ValueError):
        return {"version": 1, "items": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "items": {}}
    items = payload.get("items")
    if not isinstance(items, dict):
        payload["items"] = {}
    else:
        payload["items"] = {
            resume_id: item
            for resume_id, item in items.items()
            if isinstance(item, dict)
        }
    return payload


def _discard_temp(fd, tmp_path):
    """Best-effort cleanup of a failed atomic write's leftovers.

    Closing the dangling descriptor and unlinking the orphaned temp file can
    themselves fail (perms/race/already-gone); those are non-fatal since the
    write already failed and was logged by the caller, so swallow them rather
    than masking the original error.
    """
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            # Best-effort close of the dangling fd; non-fatal if it fails.
            pass
    if tmp_path:
        try:
            os.unlink(tmp_path)
        except OSError:
            # Best-effort unlink of the orphaned temp file; non-fatal if it fails.
            pass


def _write(path, payload):
    directory = os.path.dirname(path)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="resume-", suffix=".json", dir=directory)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
        os.replace(tmp_path, path)
    except (IOError, OSError, TypeError, ValueError) as error:
        xbmc.log(
            "NZB-DAV: Failed to write resume state: {}".format(error),
            xbmc.LOGWARNING,
        )
        _discard_temp(fd, tmp_path)


def _coerce_position(position):
    try:
        value = float(position)
    except (TypeError, ValueError):
        return 0.0
    if value < _MIN_RESUME_SECONDS:
        return 0.0
    return value


def _near_end(position, duration):
    try:
        duration = float(duration)
    except (TypeError, ValueError):
        return False
    return duration > 0.0 and max(0.0, duration - position) <= _NEAR_END_SECONDS


def is_useful_resume(position, duration=None):
    """Return whether a resume offset is worth persisting or replaying."""
    position = _coerce_position(position)
    return position > 0.0 and not _near_end(position, duration)


def _coerce_updated_at(item):
    try:
        return float(item.get("updated_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _trim_items(items):
    if len(items) <= _MAX_ITEMS:
        return items
    ordered = sorted(
        items.items(),
        key=lambda item: _coerce_updated_at(item[1]),
        reverse=True,
    )
    return dict(ordered[:_MAX_ITEMS])


def get_resume(key, path=None):
    """Return the saved resume offset for a stable stream key."""
    resume_id = _resume_id(key)
    if not resume_id:
        return 0.0
    payload = _read(_store_path(path))
    item = payload.get("items", {}).get(resume_id)
    if not isinstance(item, dict):
        return 0.0
    return _coerce_position(item.get("position", 0.0))


def save_resume(key, position, duration=None, path=None, now=None):
    """Persist a useful resume offset for a stable stream key."""
    resume_id = _resume_id(key)
    position = _coerce_position(position)
    if not resume_id:
        return
    if not is_useful_resume(position, duration):
        clear_resume(key, path=path)
        return

    store_path = _store_path(path)
    payload = _read(store_path)
    items = payload.setdefault("items", {})
    now_fn = now or time.time
    items[resume_id] = {
        "position": position,
        "updated_at": float(now_fn()),
    }
    payload["items"] = _trim_items(items)
    payload["version"] = 1
    _write(store_path, payload)


def clear_resume(key, path=None):
    """Remove the saved resume offset for a stable stream key."""
    resume_id = _resume_id(key)
    if not resume_id:
        return
    store_path = _store_path(path)
    payload = _read(store_path)
    items = payload.setdefault("items", {})
    if resume_id not in items:
        return
    del items[resume_id]
    payload["version"] = 1
    _write(store_path, payload)
