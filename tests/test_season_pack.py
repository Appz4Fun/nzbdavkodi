# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for completed season-pack catalog persistence and matching."""

import json
import os
from unittest.mock import patch

from resources.lib import season_pack


def _context(episode=2, **overrides):
    context = {
        "type": "episode",
        "title": "Spider-Noir",
        "tvdb": "123",
        "tmdb_id": "456",
        "imdb": "tt789",
        "season": 1,
        "episode": episode,
    }
    context.update(overrides)
    return context


def _record(job_id="41", folder="/downloads/a", **overrides):
    record = {
        "backend": "nzbget",
        "job_id": job_id,
        "job_name": "Spider-Noir.S01E01",
        "folder": folder,
        "title": "Spider-Noir",
        "tvdb": "123",
        "tmdb_id": "456",
        "imdb": "tt789",
        "season": 1,
        "episodes": [1, 2, 3],
        "last_confirmed": 10,
    }
    record.update(overrides)
    return record


def _use_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(season_pack, "_catalog_dir", lambda: str(tmp_path))


def test_missing_and_corrupt_catalog_fail_soft_and_next_write_repairs(
    tmp_path, monkeypatch
):
    _use_catalog(tmp_path, monkeypatch)
    assert season_pack.load_records() == []

    catalog = tmp_path / season_pack._CATALOG_FILENAME
    catalog.write_text("{broken", encoding="utf-8")
    assert season_pack.load_records() == []

    assert season_pack.upsert(_record()) is True
    assert season_pack.load_records()[0]["job_id"] == "41"


def test_upsert_is_scoped_by_backend_and_exact_string_job_id(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record(41, "/downloads/a"))
    season_pack.upsert(_record("42", "/downloads/b"))
    season_pack.upsert(
        _record("41", "/webdav/a", backend="nzbdav", job_name="same name")
    )

    rows = season_pack.load_records()
    assert {(row["backend"], row["job_id"], row["folder"]) for row in rows} == {
        ("nzbget", "41", "/downloads/a"),
        ("nzbget", "42", "/downloads/b"),
        ("nzbdav", "41", "/webdav/a"),
    }
    assert season_pack.find_exact("nzbget", 41)["folder"] == "/downloads/a"
    assert season_pack.find_exact("nzbdav", "41")["folder"] == "/webdav/a"


def test_upsert_replaces_only_the_same_exact_job_key(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record("41", "/downloads/old", last_confirmed=1))
    season_pack.upsert(_record("41", "/downloads/new", last_confirmed=2))

    assert season_pack.load_records() == [season_pack.find_exact("nzbget", "41")]
    assert season_pack.find_exact("nzbget", "41")["folder"] == "/downloads/new"


def test_remove_deletes_only_exact_backend_job_key(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record("41", "/downloads/a"))
    season_pack.upsert(_record("42", "/downloads/b"))
    season_pack.upsert(_record("41", "/webdav/a", backend="nzbdav"))

    assert season_pack.remove("nzbget", 41) is True

    assert {(row["backend"], row["job_id"]) for row in season_pack.load_records()} == {
        ("nzbget", "42"),
        ("nzbdav", "41"),
    }


def test_find_requires_backend_season_available_episode_and_identity(
    tmp_path, monkeypatch
):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record())

    assert season_pack.find_for_episode(_context(2), "nzbget")["job_id"] == "41"
    assert season_pack.find_for_episode(_context(8), "nzbget") is None
    assert season_pack.find_for_episode(_context(2, season=2), "nzbget") is None
    assert season_pack.find_for_episode(_context(2), "nzbdav") is None


def test_all_common_strong_ids_must_match(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record())

    assert season_pack.find_for_episode(_context(tvdb="999"), "nzbget") is None


def test_matching_partial_common_strong_id_allows_unshared_ids(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record(tvdb="", tmdb_id="", imdb="tt789"))

    context = _context(tvdb="999", tmdb_id="456", imdb="tt789")
    assert season_pack.find_for_episode(context, "nzbget")["job_id"] == "41"


def test_title_fallback_only_when_neither_side_has_any_strong_id(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    no_ids = {"tvdb": "", "tmdb_id": "", "imdb": ""}
    season_pack.upsert(_record(**no_ids))

    assert season_pack.find_for_episode(_context(**no_ids), "nzbget") is not None
    assert (
        season_pack.find_for_episode(
            _context(tvdb="999", tmdb_id="", imdb=""), "nzbget"
        )
        is None
    )

    season_pack.remove("nzbget", "41")
    season_pack.upsert(_record("42", "/downloads/b", title="Spider Noir"))
    assert season_pack.find_for_episode(_context(**no_ids), "nzbget") is None


def test_title_fallback_is_normalized_when_both_sides_lack_ids(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record(tvdb="", tmdb_id="", imdb="", title="Spider.Noir"))

    context = _context(tvdb="", tmdb_id="", imdb="", title=" spider noir ")
    assert season_pack.find_for_episode(context, "nzbget") is not None


def test_find_returns_newest_confirmed_matching_job(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record("old", "/downloads/old", last_confirmed=10))
    season_pack.upsert(_record("new", "/downloads/new", last_confirmed=20))

    assert season_pack.find_for_episode(_context(), "nzbget")["job_id"] == "new"


def test_save_bounds_catalog_to_newest_100_records(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    rows = [
        _record(str(number), "/downloads/{}".format(number), last_confirmed=number)
        for number in range(105)
    ]

    assert season_pack.save_records(rows) is True

    loaded = season_pack.load_records()
    assert len(loaded) == 100
    assert {row["job_id"] for row in loaded} == {
        str(number) for number in range(5, 105)
    }


def test_save_uses_atomic_sibling_temp_then_replace(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    real_replace = os.replace

    with patch.object(season_pack.os, "replace", wraps=real_replace) as replace:
        assert season_pack.save_records([_record()]) is True

    source, destination = replace.call_args.args
    assert os.path.dirname(source) == str(tmp_path)
    assert source != destination
    assert destination == str(tmp_path / season_pack._CATALOG_FILENAME)
    assert not os.path.exists(source)


def test_failed_atomic_replace_preserves_existing_catalog_and_fails_soft(
    tmp_path, monkeypatch
):
    _use_catalog(tmp_path, monkeypatch)
    assert season_pack.save_records([_record("old")]) is True

    with patch.object(season_pack.os, "replace", side_effect=OSError("disk full")):
        assert season_pack.save_records([_record("new")]) is False

    assert [row["job_id"] for row in season_pack.load_records()] == ["old"]
    assert not list(tmp_path.glob("season-pack-*.json"))


def test_context_from_params_safely_converts_numeric_fields_and_overrides():
    context = season_pack.context_from_params(
        {
            "type": "episode",
            "title": "Wrong",
            "season": " 01 ",
            "episode": "bad",
            "imdb": "tt789",
            "tvdb": 123,
            "tmdb_id": "456",
        },
        title="Spider-Noir",
        episode="08",
    )

    assert context == {
        "type": "episode",
        "title": "Spider-Noir",
        "imdb": "tt789",
        "tvdb": 123,
        "tmdb_id": "456",
        "season": 1,
        "episode": 8,
    }
    assert (
        season_pack.context_from_params({"season": "", "episode": None})["season"]
        is None
    )


def test_episode_summary_is_concise_for_ranges_and_gaps():
    assert season_pack.episode_summary([8, 2, 1, 3, 4, 5, 6, 7, 7]) == "1-8"
    assert season_pack.episode_summary([5, 1, 3]) == "1, 3, 5"
    assert season_pack.episode_summary([]) == ""


def test_picker_result_retains_exact_pack_metadata():
    record = _record()
    result = season_pack.picker_result(record, "Downloaded season pack - Episodes 1-3")

    assert result["_available"] is True
    assert result["_display_title"] == "Downloaded season pack - Episodes 1-3"
    assert result["_season_pack"]["job_id"] == "41"
    assert result["_season_pack"]["folder"] == "/downloads/a"
    assert result["_season_pack"] is not record
    assert result["link"] == ""
    assert result["indexer"] == "NZBGet"


def test_catalog_payload_contains_version_and_canonical_record_fields(
    tmp_path, monkeypatch
):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record(extra="discard me"))

    payload = json.loads(
        (tmp_path / season_pack._CATALOG_FILENAME).read_text(encoding="utf-8")
    )
    assert payload["version"] == 1
    assert set(payload["records"][0]) == {
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
    }
