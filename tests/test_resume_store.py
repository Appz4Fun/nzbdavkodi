# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for stable addon-owned playback resume state."""

import json

from resources.lib import resume_store


def test_resume_store_round_trips_position_by_stable_key(tmp_path):
    """Resume offsets survive disposable proxy URL changes."""
    store_path = tmp_path / "resume.json"

    resume_store.save_resume(
        "http://webdav:8080/content/movie/Movie.mkv",
        1565.8,
        duration=7200.0,
        path=str(store_path),
        now=lambda: 123.0,
    )

    assert (
        resume_store.get_resume(
            "http://webdav:8080/content/movie/Movie.mkv", path=str(store_path)
        )
        == 1565.8
    )

    payload = json.loads(store_path.read_text(encoding="utf-8"))
    assert list(payload["items"].values())[0]["updated_at"] == 123.0


def test_resume_store_ignores_tiny_and_near_end_positions(tmp_path):
    store_path = tmp_path / "resume.json"
    key = "http://webdav:8080/content/movie/Movie.mkv"

    resume_store.save_resume(key, 4.0, path=str(store_path))
    assert resume_store.get_resume(key, path=str(store_path)) == 0.0

    resume_store.save_resume(key, 7195.0, duration=7200.0, path=str(store_path))
    assert resume_store.get_resume(key, path=str(store_path)) == 0.0


def test_resume_store_clear_removes_existing_offset(tmp_path):
    store_path = tmp_path / "resume.json"
    key = "http://webdav:8080/content/movie/Movie.mkv"

    resume_store.save_resume(key, 300.0, path=str(store_path))
    resume_store.clear_resume(key, path=str(store_path))

    assert resume_store.get_resume(key, path=str(store_path)) == 0.0


def test_resume_store_drops_malformed_items_before_trimming(tmp_path):
    """Corrupted over-limit stores should not block future resume saves."""
    store_path = tmp_path / "resume.json"
    payload = {
        "version": 1,
        "items": {"bad-{}".format(index): "not-an-object" for index in range(300)},
    }
    store_path.write_text(json.dumps(payload), encoding="utf-8")

    resume_store.save_resume(
        "http://webdav:8080/content/movie/Movie.mkv",
        300.0,
        path=str(store_path),
        now=lambda: 123.0,
    )

    payload = json.loads(store_path.read_text(encoding="utf-8"))
    assert len(payload["items"]) == 1
    assert list(payload["items"].values())[0]["position"] == 300.0
