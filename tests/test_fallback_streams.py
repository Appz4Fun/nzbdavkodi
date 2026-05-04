# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import patch

from resources.lib.fallback_streams import (
    _SAFE_JOB_RE,
    attach_fallback_candidates,
    build_fallback_job_name,
    build_prepare_fallback_payload,
)


def _result(title, link, size, meta=None):
    return {
        "title": title,
        "link": link,
        "size": size,
        "_meta": meta
        or {
            "resolution": "1080p",
            "quality": "WEB-DL",
            "codec": "x265/HEVC",
            "group": "GROUP",
            "container": "mkv",
        },
    }


@patch("resources.lib.fallback_streams._fallback_settings")
def test_exact_duplicate_title_attaches_distinct_fallback_candidates(mock_settings):
    mock_settings.return_value = (True, 2)
    primary = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://a/nzb",
        1000,
    )
    duplicate = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://b/nzb",
        "1001",
    )
    unrelated = _result(
        "Example Movie 2026 2160p WEB-DL x265-GROUP",
        "https://c/nzb",
        1000,
        meta={
            "resolution": "2160p",
            "quality": "WEB-DL",
            "codec": "x265/HEVC",
            "group": "GROUP",
            "container": "mkv",
        },
    )

    results = [primary, duplicate, unrelated]

    assert attach_fallback_candidates(results) is results
    assert primary["_fallback_candidates"] == [duplicate]
    assert duplicate["_fallback_candidates"] == [primary]
    assert unrelated["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams._fallback_settings")
def test_disabled_setting_adds_empty_fallback_lists(mock_settings):
    mock_settings.return_value = (False, 2)
    results = [
        _result(
            "Example Movie 2026 1080p WEB-DL x265-GROUP",
            "https://a/nzb",
            1000,
        ),
        _result(
            "Example Movie 2026 1080p WEB-DL x265-GROUP",
            "https://b/nzb",
            1000,
        ),
    ]

    attach_fallback_candidates(results)

    assert [result["_fallback_candidates"] for result in results] == [[], []]


@patch("resources.lib.fallback_streams._fallback_settings")
def test_size_mismatch_rejected(mock_settings):
    mock_settings.return_value = (True, 2)
    results = [
        _result(
            "Example Movie 2026 1080p WEB-DL x265-GROUP",
            "https://a/nzb",
            1000,
        ),
        _result(
            "Example Movie 2026 1080p WEB-DL x265-GROUP",
            "https://b/nzb",
            10_000_000,
        ),
    ]

    attach_fallback_candidates(results)

    assert [result["_fallback_candidates"] for result in results] == [[], []]


def test_build_fallback_job_name_unique_traceable_and_single_line():
    first = build_fallback_job_name(
        "Example\nMovie: 2026 / 1080p WEB-DL x265-GROUP",
        "https://hydra/getnzb?id=one",
        1,
    )
    second = build_fallback_job_name(
        "Example\nMovie: 2026 / 1080p WEB-DL x265-GROUP",
        "https://hydra/getnzb?id=two",
        2,
    )

    assert first != second
    assert "Example Movie 2026 1080p WEB-DL x265-GROUP" in first
    assert first.endswith("[fallback-1-fc7bc55a]")
    assert second.endswith("[fallback-2-322c37e6]")
    assert "\n" not in first
    assert "\r" not in first
    assert _SAFE_JOB_RE.match(first)
    assert len(first) <= 180 + len(" [fallback-1-8af769ea]")


def test_build_fallback_job_name_uses_fallback_title_when_clean_title_empty():
    job_name = build_fallback_job_name("\n\t:::////", "https://hydra/getnzb?id=one", 1)

    assert job_name.startswith("fallback ")


def test_build_prepare_fallback_payload_preserves_completed_and_standby_jobs():
    payload = build_prepare_fallback_payload(
        [
            {
                "title": "completed",
                "nzb_url": "https://hydra/getnzb?id=done",
                "job_name": "completed [fallback-1-11111111]",
                "nzo_id": "SABnzbd_nzo_done",
                "stream_url": "http://webdav/content/completed/movie.mkv",
                "stream_headers": {"Authorization": "Basic abc"},
                "content_length": 123456,
            },
            {
                "title": "standby",
                "nzb_url": "https://hydra/getnzb?id=standby",
                "job_name": "standby [fallback-2-22222222]",
                "nzo_id": "SABnzbd_nzo_standby",
            },
            {
                "title": "missing nzo",
                "nzb_url": "https://hydra/getnzb?id=missing",
                "job_name": "missing [fallback-3-33333333]",
            },
        ]
    )

    assert payload == [
        {
            "title": "completed",
            "nzb_url": "https://hydra/getnzb?id=done",
            "job_name": "completed [fallback-1-11111111]",
            "nzo_id": "SABnzbd_nzo_done",
            "stream_url": "http://webdav/content/completed/movie.mkv",
            "stream_headers": {"Authorization": "Basic abc"},
            "content_length": 123456,
        },
        {
            "title": "standby",
            "nzb_url": "https://hydra/getnzb?id=standby",
            "job_name": "standby [fallback-2-22222222]",
            "nzo_id": "SABnzbd_nzo_standby",
            "stream_url": "",
            "stream_headers": {},
            "content_length": 0,
        },
    ]
