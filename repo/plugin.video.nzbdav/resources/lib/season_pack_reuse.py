# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Validate and reuse one exact completed season-pack job."""

import time
from typing import NamedTuple

import xbmcaddon

from resources.lib import nzbget_api, season_pack, webdav


class ReuseResult(NamedTuple):
    """Result of validating an identity-matched catalog record."""

    state: str
    stream_url: object = None
    stream_headers: object = None


def _stale(record):
    season_pack.remove(record.get("backend"), record.get("job_id"))
    return ReuseResult("stale", None, None)


def _confirmed(record):
    refreshed = dict(record)
    refreshed["last_confirmed"] = time.time()
    try:
        season_pack.upsert(refreshed)
    except Exception:  # pylint: disable=broad-except
        pass


def _bound_setting_getter(settings_getter):
    if settings_getter is not None:
        return settings_getter
    addon = xbmcaddon.Addon("plugin.video.nzbdav")

    def getter(key, default=""):
        value = addon.getSetting(key)
        return value if value else default

    return getter


def _nzbget_folder_for_record(record, settings_getter=None):
    from resources.lib.nzbget_resolver_smb import nzbget_smb_target

    getter = _bound_setting_getter(settings_getter)
    smb_root = getter("nzbget_smb_root", "").strip()
    _url, _user, _password, category = nzbget_api._get_settings(
        settings_getter=settings_getter
    )
    completed_base = nzbget_api.completed_base_dir(settings_getter=settings_getter)
    return nzbget_smb_target(smb_root, record.get("folder"), category, completed_base)


def _smb_inventory(folder, requested_episode=None):
    from resources.lib.nzbget_resolver_smb import _smb_inventory as build_inventory

    return build_inventory(folder, requested_episode=requested_episode)


def _webdav_folder_for_record(record):
    from resources.lib.resolver_poll import _storage_to_webdav_path

    return _storage_to_webdav_path(record.get("folder"))


def _stream_body_available(url, headers):
    from resources.lib.resolver_playback import _completed_stream_body_available

    return _completed_stream_body_available(url, headers)


def _reuse_nzbget(record, requested, settings_getter):
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
    if not inventory.files or not inventory.selected_path:
        return _stale(record)
    _confirmed(record)
    return ReuseResult("valid", inventory.selected_path, {})


def _reuse_nzbdav(record, requested, settings_getter):
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
    if not inventory.files or not inventory.selected_path:
        return _stale(record)
    try:
        stream_url, stream_headers = webdav.get_webdav_stream_url_for_path(
            inventory.selected_path, settings_getter=settings_getter
        )
        if not _stream_body_available(stream_url, stream_headers):
            return ReuseResult("transient", None, None)
    except Exception:  # pylint: disable=broad-except
        return ReuseResult("transient", None, None)
    _confirmed(record)
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
        return _reuse_nzbget(record, requested, settings_getter)
    if active_backend == "nzbdav":
        return _reuse_nzbdav(record, requested, settings_getter)
    return ReuseResult("not_applicable", None, None)
