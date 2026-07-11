# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for fail-soft recording of confirmed completed pack inventories."""

from unittest.mock import patch

from resources.lib.episode_inventory import build_video_inventory
from resources.lib.season_pack_recording import record_completed_inventory


def _context(**overrides):
    context = {
        "type": "episode",
        "title": "Spider-Noir",
        "year": "2026",
        "imdb": "tt123",
        "tvdb": "456",
        "tmdb_id": "789",
        "season": 1,
        "episode": 1,
    }
    context.update(overrides)
    return context


def _inventory(*names):
    return build_video_inventory(
        [("/pack/{}".format(name), 1000 + index) for index, name in enumerate(names)],
        requested=(1, 1),
    )


def test_records_exact_backend_job_and_media_identity_for_real_pack():
    inventory = _inventory("Show.S01E01.mkv", "Show.S01E02.mkv")

    with patch("resources.lib.season_pack_recording.season_pack.upsert") as upsert:
        assert (
            record_completed_inventory(
                "nzbdav",
                "SABnzbd_nzo_exact",
                "Spider-Noir.S01.2160p",
                "/content/tv/exact/",
                _context(),
                inventory,
            )
            is True
        )

    record = upsert.call_args.args[0]
    assert record == {
        "backend": "nzbdav",
        "job_id": "SABnzbd_nzo_exact",
        "job_name": "Spider-Noir.S01.2160p",
        "folder": "/content/tv/exact/",
        "title": "Spider-Noir",
        "year": 2026,
        "imdb": "tt123",
        "tvdb": "456",
        "tmdb_id": "789",
        "season": 1,
        "episodes": [1, 2],
    }


def test_same_name_different_job_ids_are_upserted_as_distinct_exact_keys():
    inventory = _inventory("Show.S01E01.mkv", "Show.S01E02.mkv")
    with patch("resources.lib.season_pack_recording.season_pack.upsert") as upsert:
        for job_id, folder in ((41, "smb://box/a"), (42, "smb://box/b")):
            record_completed_inventory(
                "nzbget", job_id, "same name", folder, _context(), inventory
            )

    records = [call.args[0] for call in upsert.call_args_list]
    assert [(record["job_id"], record["folder"]) for record in records] == [
        ("41", "smb://box/a"),
        ("42", "smb://box/b"),
    ]


def test_does_not_record_single_generic_or_mixed_season_inventory():
    inventories = (
        _inventory("Show.S01E01.mkv"),
        _inventory("video.mkv", "other.mkv"),
        _inventory("Show.S01E01.mkv", "Show.S02E02.mkv"),
    )
    with patch("resources.lib.season_pack_recording.season_pack.upsert") as upsert:
        results = [
            record_completed_inventory(
                "nzbget", "41", "pack", "smb://box/pack", _context(), inventory
            )
            for inventory in inventories
        ]

    assert results == [False, False, False]
    upsert.assert_not_called()


def test_does_not_record_when_episode_context_or_season_does_not_match():
    inventory = _inventory("Show.S01E01.mkv", "Show.S01E02.mkv")
    contexts = (
        None,
        _context(type="movie"),
        _context(title="", imdb="", tvdb="", tmdb_id=""),
        _context(season=2),
    )
    with patch("resources.lib.season_pack_recording.season_pack.upsert") as upsert:
        results = [
            record_completed_inventory(
                "nzbdav", "id", "pack", "/content/pack", context, inventory
            )
            for context in contexts
        ]

    assert results == [False, False, False, False]
    upsert.assert_not_called()


def test_catalog_write_failure_is_swallowed():
    inventory = _inventory("Show.S01E01.mkv", "Show.S01E02.mkv")
    with patch(
        "resources.lib.season_pack_recording.season_pack.upsert",
        side_effect=OSError("disk full"),
    ):
        assert (
            record_completed_inventory(
                "nzbdav", "id", "pack", "/content/pack", _context(), inventory
            )
            is False
        )
