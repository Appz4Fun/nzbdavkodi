# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Validate and reuse one exact completed season-pack job."""

import re
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

import xbmcaddon

from resources.lib import nzbget_api, season_pack, webdav

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:/")


class ReuseResult(NamedTuple):
    """Result of validating an identity-matched catalog record."""

    state: str
    stream_url: object = None
    stream_headers: object = None


def _stale(record):
    season_pack.remove(record.get("backend"), record.get("job_id"))
    return ReuseResult("stale", None, None)


def _refresh_inventory(record, episode_context, inventory):
    from resources.lib.season_pack_recording import record_completed_inventory

    merged_context = dict(episode_context or {})
    for field in ("title", "year", "imdb", "tvdb", "tmdb_id"):
        if merged_context.get(field) in (None, "") and record.get(field) not in (
            None,
            "",
        ):
            merged_context[field] = record[field]
    if not merged_context.get("type"):
        merged_context["type"] = "episode"
    if merged_context.get("season") in (None, ""):
        merged_context["season"] = record.get("season")
    record_completed_inventory(
        record.get("backend"),
        record.get("job_id"),
        record.get("job_name"),
        record.get("folder"),
        merged_context,
        inventory,
    )


def _bound_setting_getter(settings_getter):
    if settings_getter is not None:
        return settings_getter
    addon = xbmcaddon.Addon("plugin.video.nzbdav")

    def getter(key, default=""):
        value = addon.getSetting(key)
        return value if value else default

    return getter


def _canonical_native_path(value):
    """Return ``(canonical, root, segments)`` for a provable absolute path.

    Both slash styles are accepted, but any explicit ``.``/``..`` component is
    rejected rather than normalized. Cached reuse must preserve the exact
    lexical job mapping, not reinterpret a history path containing traversal.
    """
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        return None
    if normalized.startswith("//") and not normalized.startswith("///"):
        parts = normalized[2:].split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return None
        root = ("unc", parts[0].casefold(), parts[1].casefold())
        path_parts = parts[2:]
        prefix = "//{}/{}".format(parts[0], parts[1])
    elif _WINDOWS_DRIVE_RE.match(normalized):
        root = ("drive", normalized[:2].casefold())
        path_parts = normalized[3:].split("/")
        prefix = normalized[:2] + "/"
    elif normalized.startswith("/"):
        root = ("posix",)
        path_parts = normalized[1:].split("/")
        prefix = "/"
    else:
        return None
    if any(part in (".", "..") for part in path_parts):
        return None
    segments = tuple(part for part in path_parts if part)
    if prefix == "/":
        canonical = "/" + "/".join(segments)
    elif prefix.endswith("/"):
        canonical = prefix + "/".join(segments)
    else:
        canonical = prefix + ("/" + "/".join(segments) if segments else "")
    return canonical, root, segments


def _safe_smb_root(value):
    root = str(value or "").strip().replace("\\", "/").rstrip("/")
    if _ambiguous_url_component(root):
        return None
    try:
        parts = urlsplit(root)
        hostname = parts.hostname
        _port = parts.port
    except (TypeError, ValueError):
        return None
    if parts.scheme.casefold() != "smb" or not hostname:
        return None
    authority_host = parts.netloc.rpartition("@")[2]
    if authority_host.endswith(":"):
        return None
    if parts.query or parts.fragment:
        return None
    decoded_host = unquote(hostname)
    if _ambiguous_url_component(decoded_host) or any(
        separator in decoded_host for separator in ("/", "\\")
    ):
        return None
    raw_segments = parts.path.split("/")
    if not parts.path.startswith("/") or len(raw_segments) < 2:
        return None
    raw_segments = raw_segments[1:]
    if not raw_segments or any(not segment for segment in raw_segments):
        return None
    for raw_segment in raw_segments:
        segment = unquote(raw_segment)
        if (
            segment in (".", "..")
            or _ambiguous_url_component(segment)
            or "/" in segment
            or "\\" in segment
        ):
            return None
    return root


def _ambiguous_url_component(value):
    return not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def _exact_cached_smb_mapping(smb_root, native_folder, completed_base):
    """Map a canonical strict completed-base child without path traversal."""
    target = _canonical_native_path(native_folder)
    base = _canonical_native_path(completed_base)
    smb_root = _safe_smb_root(smb_root)
    if target is None or base is None or smb_root is None:
        return None
    _target_path, target_root, target_segments = target
    _base_path, base_root, base_segments = base
    if target_root != base_root or len(target_segments) <= len(base_segments):
        return None
    target_prefix = target_segments[: len(base_segments)]
    base_key = base_segments
    if target_root[0] != "posix":
        target_prefix = tuple(part.casefold() for part in target_prefix)
        base_key = tuple(part.casefold() for part in base_key)
    if target_prefix != base_key:
        return None
    relative = list(target_segments[len(base_segments) :])
    if not relative:
        return None
    return "{}/{}".format(smb_root, "/".join(relative))


def _nzbget_folder_for_record(record, settings_getter=None):
    # Cached reuse must prove the server-native completed folder maps under
    # NZBGet's configured completed base.  The ordinary first-play resolver may
    # use its heuristic fallback, but applying that here could map an unrelated
    # same-tail folder to a remembered job.
    getter = _bound_setting_getter(settings_getter)
    smb_root = getter("nzbget_smb_root", "").strip()
    completed_base = nzbget_api.completed_base_dir(settings_getter=settings_getter)
    return _exact_cached_smb_mapping(
        smb_root,
        record.get("folder"),
        completed_base,
    )


def _smb_inventory(folder, requested_episode=None):
    from resources.lib.nzbget_resolver_smb import _smb_inventory as build_inventory

    return build_inventory(folder, requested_episode=requested_episode)


def _webdav_folder_for_record(record):
    from resources.lib.resolver_poll import _storage_to_webdav_path

    return _storage_to_webdav_path(record.get("folder"))


def _stream_body_available(url, headers):
    from resources.lib.resolver_playback import _completed_stream_body_available

    return _completed_stream_body_available(url, headers)


def _inventory_selected_exact(inventory, requested):
    """Whether the selected non-aux file explicitly carries the episode tag."""
    selected_path = getattr(inventory, "selected_path", None)
    if not selected_path:
        return False
    for video_file in getattr(inventory, "files", ()) or ():
        if getattr(video_file, "path", None) != selected_path:
            continue
        return not getattr(video_file, "auxiliary", True) and requested in (
            getattr(video_file, "episode_tags", ()) or ()
        )
    return False


def _reuse_nzbget(record, requested, episode_context, settings_getter):
    validation = season_pack.validate_job(record, settings_getter=settings_getter)
    if validation.outcome == "transient":
        return ReuseResult("transient", None, None)
    if validation.outcome != "valid":
        return ReuseResult("stale", None, None)
    try:
        smb_folder = _nzbget_folder_for_record(record, settings_getter)
    except Exception:  # pylint: disable=broad-except
        return ReuseResult("transient", None, None)
    if not smb_folder:
        return ReuseResult("transient", None, None)
    try:
        inventory = _smb_inventory(smb_folder, requested_episode=requested)
    except Exception:  # pylint: disable=broad-except
        return ReuseResult("transient", None, None)
    if inventory is None:
        return ReuseResult("transient", None, None)
    if not inventory.files or not _inventory_selected_exact(inventory, requested):
        return _stale(record)
    _refresh_inventory(record, episode_context, inventory)
    return ReuseResult("valid", inventory.selected_path, {})


def _reuse_nzbdav(record, requested, episode_context, settings_getter):
    validation = season_pack.validate_job(record, settings_getter=settings_getter)
    if validation.outcome == "transient":
        return ReuseResult("transient", None, None)
    if validation.outcome != "valid":
        return ReuseResult("stale", None, None)
    try:
        webdav_folder = _webdav_folder_for_record(record)
        inventory = webdav.folder_video_inventory(
            webdav_folder,
            requested=requested,
            settings_getter=settings_getter,
        )
    except Exception:  # pylint: disable=broad-except
        return ReuseResult("transient", None, None)
    if inventory is None:
        return ReuseResult("transient", None, None)
    if not inventory.files or not _inventory_selected_exact(inventory, requested):
        return _stale(record)
    try:
        stream_url, stream_headers = webdav.get_webdav_stream_url_for_path(
            inventory.selected_path, settings_getter=settings_getter
        )
        if not _stream_body_available(stream_url, stream_headers):
            return ReuseResult("transient", None, None)
    except Exception:  # pylint: disable=broad-except
        return ReuseResult("transient", None, None)
    _refresh_inventory(record, episode_context, inventory)
    return ReuseResult("valid", stream_url, stream_headers)


def reuse_exact_job(record, episode_context, active_backend, settings_getter=None):
    """Validate and resolve an exact catalog job without any NZB submission.

    ``record`` must already come from :func:`season_pack.find_for_episode`, so
    content identity matching remains catalog-owned. This layer rechecks the
    active backend, exact history identifier, backend-native folder, complete
    mapped inventory, requested episode, and (for WebDAV) playable body.
    """
    if not isinstance(record, dict) or record.get("backend") != active_backend:
        return ReuseResult("not_applicable", None, None)
    requested = season_pack.requested_episode(episode_context)
    if requested is None:
        return ReuseResult("not_applicable", None, None)
    if active_backend == "nzbget":
        return _reuse_nzbget(record, requested, episode_context, settings_getter)
    if active_backend == "nzbdav":
        return _reuse_nzbdav(record, requested, episode_context, settings_getter)
    return ReuseResult("not_applicable", None, None)
