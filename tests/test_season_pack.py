# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for completed season-pack catalog persistence and matching."""

import json
import multiprocessing
import os
import time
from unittest.mock import patch

from resources.lib import season_pack

from tests.season_pack_process_helper import racing_upsert


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


def test_concurrent_process_upserts_preserve_both_exact_jobs(tmp_path, monkeypatch):
    """The load-modify-save transaction must be serialized across processes."""
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=racing_upsert,
            args=(
                str(tmp_path),
                _record(job_id, "/downloads/{}".format(job_id)),
                barrier,
                results,
            ),
        )
        for job_id in ("41", "42")
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0, 0]
    assert [results.get(timeout=1) for _process in processes] == [True, True]
    _use_catalog(tmp_path, monkeypatch)
    assert {row["job_id"] for row in season_pack.load_records()} == {"41", "42"}


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


def test_title_fallback_requires_matching_years_when_known(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    no_ids = {"tvdb": "", "tmdb_id": "", "imdb": ""}
    season_pack.upsert(_record(**no_ids, year=2026))

    assert (
        season_pack.find_for_episode(_context(**no_ids, year="2026"), "nzbget")
        is not None
    )
    assert season_pack.find_for_episode(_context(**no_ids, year=2025), "nzbget") is None
    assert season_pack.find_for_episode(_context(**no_ids), "nzbget") is None


def test_title_fallback_rejects_legacy_record_year_when_current_year_is_known(
    tmp_path, monkeypatch
):
    _use_catalog(tmp_path, monkeypatch)
    no_ids = {"tvdb": "", "tmdb_id": "", "imdb": ""}
    season_pack.upsert(_record(**no_ids))

    assert season_pack.find_for_episode(_context(**no_ids, year=2026), "nzbget") is None


def test_find_returns_newest_confirmed_matching_job(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    season_pack.upsert(_record("old", "/downloads/old", last_confirmed=10))
    season_pack.upsert(_record("new", "/downloads/new", last_confirmed=20))

    assert season_pack.find_for_episode(_context(), "nzbget")["job_id"] == "new"


def test_oversized_json_timestamp_defaults_without_breaking_load(tmp_path, monkeypatch):
    _use_catalog(tmp_path, monkeypatch)
    payload = {"version": 1, "records": [_record(last_confirmed=10**400)]}
    (tmp_path / season_pack._CATALOG_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    assert season_pack.load_records()[0]["last_confirmed"] == 0.0


def test_nonfinite_timestamps_default_and_cannot_win_newest_match(
    tmp_path, monkeypatch
):
    _use_catalog(tmp_path, monkeypatch)
    payload = {
        "version": 1,
        "records": [
            _record("nan", "/downloads/nan", last_confirmed=float("nan")),
            _record("positive", "/downloads/positive", last_confirmed=float("inf")),
            _record("negative", "/downloads/negative", last_confirmed=float("-inf")),
            _record("newest", "/downloads/newest", last_confirmed=50),
        ],
    }
    (tmp_path / season_pack._CATALOG_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )

    loaded = season_pack.load_records()
    assert {row["job_id"]: row["last_confirmed"] for row in loaded} == {
        "nan": 0.0,
        "positive": 0.0,
        "negative": 0.0,
        "newest": 50.0,
    }
    assert season_pack.find_for_episode(_context(), "nzbget")["job_id"] == "newest"


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


def test_directory_lock_owner_write_failure_cleans_created_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(season_pack, "fcntl", None)
    lock_path = str(tmp_path / "catalog.lock")

    with patch("builtins.open", side_effect=OSError("read-only filesystem")):
        assert season_pack._acquire_process_lock(lock_path) is None

    assert not os.path.exists(lock_path + ".d")


def test_directory_lock_partial_owner_write_cleans_only_new_lock_and_can_retry(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(season_pack, "fcntl", None)
    lock_path = str(tmp_path / "catalog.lock")

    def partial_write(_owner, handle):
        handle.write('{"pid":')
        handle.flush()
        raise OSError("write interrupted")

    with patch.object(season_pack.json, "dump", side_effect=partial_write):
        assert season_pack._acquire_process_lock(lock_path) is None

    assert not os.path.exists(lock_path + ".d")
    lock = season_pack._acquire_process_lock(lock_path)
    assert lock is not None
    season_pack._release_process_lock(lock)
    assert not os.path.exists(lock_path + ".d")


def test_directory_lock_preserves_old_malformed_foreign_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(season_pack, "fcntl", None)
    monkeypatch.setattr(season_pack, "_STALE_LOCK_SECONDS", 0)
    lock_path = str(tmp_path / "catalog.lock")
    lock_dir = lock_path + ".d"
    os.mkdir(lock_dir)
    owner_path = os.path.join(lock_dir, "owner.json")
    with open(owner_path, "w", encoding="utf-8") as handle:
        handle.write("{malformed")
    os.utime(lock_dir, (1, 1))

    assert season_pack._acquire_directory_lock(lock_path, time.monotonic()) is None
    assert os.path.isdir(lock_dir)
    with open(owner_path, "r", encoding="utf-8") as handle:
        assert handle.read() == "{malformed"


def test_context_from_params_safely_converts_numeric_fields_and_overrides():
    context = season_pack.context_from_params(
        {
            "type": "episode",
            "title": "Wrong",
            "season": " 01 ",
            "episode": "bad",
            "year": " 2026 ",
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
        "year": 2026,
        "season": 1,
        "episode": 8,
    }
    assert (
        season_pack.context_from_params({"season": "", "episode": None})["season"]
        is None
    )


def test_requested_episode_normalizes_context_and_rejects_incomplete_values():
    assert season_pack.requested_episode(
        {"type": "episode", "season": "1", "episode": "5"}
    ) == (1, 5)
    assert (
        season_pack.requested_episode({"type": "episode", "season": "1", "episode": ""})
        is None
    )
    assert (
        season_pack.requested_episode({"type": "episode", "season": -1, "episode": 5})
        is None
    )
    assert (
        season_pack.requested_episode({"type": "movie", "season": 1, "episode": 5})
        is None
    )
    assert season_pack.requested_episode(None) is None


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
        "year",
        "imdb",
        "tvdb",
        "tmdb_id",
        "season",
        "episodes",
        "last_confirmed",
    }
