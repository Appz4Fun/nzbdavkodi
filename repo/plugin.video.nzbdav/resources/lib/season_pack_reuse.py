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
    try:
        removed = season_pack.remove(record.get("backend"), record.get("job_id"))
    except Exception:  # pylint: disable=broad-except
        return ReuseResult("transient", None, None)
    if removed is False:
        return ReuseResult("transient", None, None)
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


def _unc_native_parts(normalized):
    parts = normalized[2:].split("/")
    if len(parts) < 2:
        return None
    if not parts[0] or not parts[1]:
        return None
    root = ("unc", parts[0].casefold(), parts[1].casefold())
    return root, parts[2:], "//{}/{}".format(parts[0], parts[1])


def _native_root_parts(normalized):
    if normalized.startswith("//") and not normalized.startswith("///"):
        return _unc_native_parts(normalized)
    if _WINDOWS_DRIVE_RE.match(normalized):
        root = ("drive", normalized[:2].casefold())
        return root, normalized[3:].split("/"), normalized[:2] + "/"
    if normalized.startswith("/"):
        return ("posix",), normalized[1:].split("/"), "/"
    return None


def _join_native_path(prefix, segments):
    suffix = "/".join(segments)
    if prefix == "/":
        return "/" + suffix
    if prefix.endswith("/"):
        return prefix + suffix
    return prefix + ("/" + suffix if suffix else "")


def _canonical_native_path(value):
    """Return ``(canonical, root, segments)`` for a provable absolute path.

    Both slash styles are accepted, but any explicit ``.``/``..`` component is
    rejected rather than normalized. Cached reuse must preserve the exact
    lexical job mapping, not reinterpret a history path containing traversal.
    """
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        return None
    root_parts = _native_root_parts(normalized)
    if root_parts is None:
        return None
    root, path_parts, prefix = root_parts
    if any(part in (".", "..") for part in path_parts):
        return None
    segments = tuple(part for part in path_parts if part)
    canonical = _join_native_path(prefix, segments)
    return canonical, root, segments


def _split_smb_root(root):
    try:
        parts = urlsplit(root)
        hostname = parts.hostname
        _ = parts.port
    except (TypeError, ValueError):
        return None
    return parts, hostname


def _decoded_smb_hostname(hostname):
    decoded = unquote(hostname)
    if _ambiguous_url_component(decoded):
        return None
    if "/" in decoded or "\\" in decoded:
        return None
    return decoded


def _smb_hostname_safe(parts, hostname):
    if parts.scheme.casefold() != "smb":
        return None
    if not hostname:
        return None
    authority_host = parts.netloc.rpartition("@")[2]
    if authority_host.endswith(":"):
        return None
    if parts.query or parts.fragment:
        return None
    return _decoded_smb_hostname(hostname)


def _raw_smb_path_segments(parts):
    if not parts.path.startswith("/"):
        return None
    raw_segments = parts.path.split("/")
    if len(raw_segments) < 2:
        return None
    raw_segments = raw_segments[1:]
    if not raw_segments or any(not segment for segment in raw_segments):
        return None
    return raw_segments


def _smb_segment_safe(raw_segment):
    segment = unquote(raw_segment)
    if segment in (".", ".."):
        return False
    if _ambiguous_url_component(segment):
        return False
    return "/" not in segment and "\\" not in segment


def _safe_local_root(root):
    """Canonical absolute native path for a local/mounted completed root.

    The non-URL counterpart of the smb:// branch below: accepts the same
    POSIX / Windows-drive / UNC shapes ``_canonical_native_path`` already
    proves for ``dest_dir``/``completed_base``, rejecting relative paths and
    ``.``/``..`` traversal. Requires at least one path segment so a bare
    filesystem root can never become the join prefix.
    """
    canonical = _canonical_native_path(root)
    if canonical is None or not canonical[2]:
        return None
    return canonical[0]


def _safe_smb_root(value):
    root = str(value or "").strip().replace("\\", "/").rstrip("/")
    if not root.casefold().startswith("smb://"):
        # Not an smb:// root -- try it as a local/mounted completed path (an
        # NFS/local mount standing in for the SMB share). Native paths may
        # legitimately contain spaces, so the URL-ambiguity check below must
        # not run against this branch.
        return _safe_local_root(root)
    if _ambiguous_url_component(root):
        return None
    split_root = _split_smb_root(root)
    if split_root is None:
        return None
    parts, hostname = split_root
    if _smb_hostname_safe(parts, hostname) is None:
        return None
    raw_segments = _raw_smb_path_segments(parts)
    if raw_segments is None:
        return None
    for raw_segment in raw_segments:
        if not _smb_segment_safe(raw_segment):
            return None
    return root


def _ambiguous_url_component(value):
    return not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def _native_child_segments(target, base):
    _target_path, target_root, target_segments = target
    _base_path, base_root, base_segments = base
    if target_root != base_root:
        return None
    if len(target_segments) <= len(base_segments):
        return None
    target_prefix = target_segments[: len(base_segments)]
    base_key = base_segments
    if target_root[0] != "posix":
        target_prefix = tuple(part.casefold() for part in target_prefix)
        base_key = tuple(part.casefold() for part in base_key)
    if target_prefix != base_key:
        return None
    return list(target_segments[len(base_segments) :])


def _without_duplicate_category(relative, smb_root, category):
    category = str(category or "").strip()
    root_tail = unquote(urlsplit(smb_root).path.rstrip("/").rsplit("/", 1)[-1])
    if (
        category
        and relative[0].casefold() == category.casefold()
        and root_tail.casefold() == category.casefold()
    ):
        return relative[1:]
    return relative


def _exact_cached_smb_mapping(smb_root, native_folder, completed_base, category=""):
    """Map a canonical strict completed-base child without path traversal.

    ``smb_root`` may be an ``smb://`` URL or an absolute native path (a
    local disk path, or a local/NFS mount standing in for the SMB share).
    """
    target = _canonical_native_path(native_folder)
    base = _canonical_native_path(completed_base)
    smb_root = _safe_smb_root(smb_root)
    if target is None or base is None or smb_root is None:
        return None
    relative = _native_child_segments(target, base)
    if not relative:
        return None
    relative = _without_duplicate_category(relative, smb_root, category)
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
    _url, _user, _password, category = nzbget_api._get_settings(settings_getter)
    completed_base = nzbget_api.completed_base_dir(settings_getter=settings_getter)
    return _exact_cached_smb_mapping(
        smb_root,
        record.get("folder"),
        completed_base,
        category=category,
    )


def _smb_inventory(folder, requested_episode=None):
    # Import via nzbget_resolver, never nzbget_resolver_smb directly: the
    # split-out SMB module imports nzbget_resolver at module level, so
    # loading it first hits that cycle while partially initialized and
    # raises ImportError. nzbget_resolver re-exports every helper.
    from resources.lib.nzbget_resolver import _smb_inventory as build_inventory

    return build_inventory(folder, requested_episode=requested_episode)


# One-shot readability blips happen (a share waking up, a pack recorded from
# a completion still settling), and a pack row is an explicit selection that
# fails closed -- so probe a few times, mirroring _reuse_completed_job's
# short resolve budget, before declaring the selection unreadable.
_SMB_READ_PROBE_ATTEMPTS = 3
_SMB_READ_PROBE_RETRY_SECONDS = 1.0


def _smb_selection_readable(path, monitor=None):
    """Probe (with brief retries) that the video reads through Kodi's VFS.

    The SMB analogue of the WebDAV path's ``_stream_body_available`` gate: a
    cached reuse must not hand the player a URL that lists but cannot open.
    Waits between attempts via ``Monitor.waitForAbort`` so Kodi shutdown
    stays honored; warns (log + toast) only once the retries are exhausted.
    """
    import xbmc

    # Same cycle-safe import direction as _smb_inventory above.
    from resources.lib.nzbget_resolver import (
        _smb_video_is_readable,
        _warn_unreadable_smb_video,
    )

    if monitor is None:
        monitor = xbmc.Monitor()
    for attempt in range(_SMB_READ_PROBE_ATTEMPTS):
        if _smb_video_is_readable(path):
            return True
        if attempt + 1 < _SMB_READ_PROBE_ATTEMPTS and monitor.waitForAbort(
            _SMB_READ_PROBE_RETRY_SECONDS
        ):
            # Kodi is shutting down: bail quietly instead of diagnosing a
            # stale SMB session on the way out.
            return False
    _warn_unreadable_smb_video(path)
    return False


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


def _job_validation_failure(record, settings_getter):
    """Return a terminal preflight result, or None for a valid exact job."""
    validation = season_pack.validate_job(record, settings_getter=settings_getter)
    if validation.outcome == "transient":
        return ReuseResult("transient", None, None)
    if validation.outcome != "valid":
        return ReuseResult("stale", None, None)
    return None


def _reuse_nzbget(record, requested, episode_context, settings_getter):
    validation_failure = _job_validation_failure(record, settings_getter)
    if validation_failure is not None:
        return validation_failure
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
    if not _smb_selection_readable(inventory.selected_path):
        # Listable but not readable: the probe's own restart-Kodi warning
        # already fired, so return the distinct state the caller uses to
        # suppress the generic no-video toast (mirroring the completed-job
        # paths' SMB_UNREADABLE handling).
        return ReuseResult("unreadable", None, None)
    _refresh_inventory(record, episode_context, inventory)
    return ReuseResult("valid", inventory.selected_path, {})


def _reuse_nzbdav(record, requested, episode_context, settings_getter):
    validation_failure = _job_validation_failure(record, settings_getter)
    if validation_failure is not None:
        return validation_failure
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
