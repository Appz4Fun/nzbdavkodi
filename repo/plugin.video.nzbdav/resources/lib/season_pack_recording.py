# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Fail-soft recording of completed, job-isolated season-pack inventories."""

import xbmc

from resources.lib import season_pack


def _number(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _has_media_identity(context):
    return any(context.get(field) for field in ("title", "imdb", "tvdb", "tmdb_id"))


def _record(backend, job_id, job_name, folder, context, inventory):
    if not isinstance(context, dict) or context.get("type") != "episode":
        return None
    if not _has_media_identity(context):
        return None
    season = _number(context.get("season"))
    episodes = sorted(set(getattr(inventory, "episodes", ()) or ()))
    pack_season = getattr(inventory, "pack_season", None)
    if season is None or pack_season != season or len(episodes) < 2:
        return None
    if not backend or job_id is None or not str(job_id).strip() or not folder:
        return None
    return {
        "backend": str(backend).strip(),
        "job_id": str(job_id).strip(),
        "job_name": str(job_name or "").strip(),
        "folder": str(folder).strip(),
        "title": str(context.get("title") or "").strip(),
        "imdb": str(context.get("imdb") or "").strip(),
        "tvdb": str(context.get("tvdb") or "").strip(),
        "tmdb_id": str(context.get("tmdb_id") or "").strip(),
        "season": season,
        "episodes": episodes,
    }


def record_completed_inventory(
    backend, job_id, job_name, folder, episode_context, inventory
):
    """Record one real pack under its exact backend job key, never raising."""
    try:
        record = _record(backend, job_id, job_name, folder, episode_context, inventory)
        if record is None:
            return False
        return bool(season_pack.upsert(record))
    except Exception as error:  # pylint: disable=broad-except
        try:
            xbmc.log(
                "NZB-DAV: season-pack record failed (non-fatal): {}".format(error),
                xbmc.LOGDEBUG,
            )
        except (AttributeError, RuntimeError):
            pass
        return False


def inventory_recorder(backend, job_id, job_name, folder, episode_context):
    """Return an inventory callback bound to one exact completed backend job."""

    def _record_inventory(inventory):
        return record_completed_inventory(
            backend, job_id, job_name, folder, episode_context, inventory
        )

    return _record_inventory
