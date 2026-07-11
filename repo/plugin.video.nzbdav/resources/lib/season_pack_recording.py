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


def _record_context_valid(context):
    if not isinstance(context, dict):
        return False
    if context.get("type") != "episode":
        return False
    return _has_media_identity(context)


def _pack_details(context, inventory):
    season = _number(context.get("season"))
    episodes = sorted(set(getattr(inventory, "episodes", ()) or ()))
    if season is None:
        return None
    if getattr(inventory, "pack_season", None) != season:
        return None
    if len(episodes) < 2:
        return None
    return season, episodes


def _job_details(backend, job_id, job_name, folder):
    normalized_id = str(job_id).strip() if job_id is not None else ""
    if not backend or not normalized_id or not folder:
        return None
    return (
        str(backend).strip(),
        normalized_id,
        str(job_name or "").strip(),
        str(folder).strip(),
    )


def _record_values(job, context, pack):
    backend, job_id, job_name, folder = job
    season, episodes = pack
    return {
        "backend": backend,
        "job_id": job_id,
        "job_name": job_name,
        "folder": folder,
        "title": str(context.get("title") or "").strip(),
        "year": _number(context.get("year")),
        "imdb": str(context.get("imdb") or "").strip(),
        "tvdb": str(context.get("tvdb") or "").strip(),
        "tmdb_id": str(context.get("tmdb_id") or "").strip(),
        "season": season,
        "episodes": episodes,
    }


def _record(backend, job_id, job_name, folder, context, inventory):
    if not _record_context_valid(context):
        return None
    pack = _pack_details(context, inventory)
    if pack is None:
        return None
    job = _job_details(backend, job_id, job_name, folder)
    if job is None:
        return None
    return _record_values(job, context, pack)


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
            # Recording remains non-fatal when Kodi logging is unavailable.
            pass
        return False


def inventory_recorder(backend, job_id, job_name, folder, episode_context):
    """Return an inventory callback bound to one exact completed backend job."""

    def _record_inventory(inventory):
        return record_completed_inventory(
            backend, job_id, job_name, folder, episode_context, inventory
        )

    return _record_inventory
