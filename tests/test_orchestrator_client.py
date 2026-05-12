# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Unit tests for resources.lib.orchestrator_client.

Phase 1 of docs/rust-migration-plan.md. Pin the toggle-and-fallback
contract: when ``use_orchestrator`` is off the client returns
``(None, "orchestrator_disabled")`` immediately; when on the client
serialises the request, posts to ``/v1/search`` on the addr-file
host, and converts the JSON candidates back into the Python result
dict shape.
"""

from __future__ import annotations

import json
import sys
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from resources.lib import orchestrator_client


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.status = status
        self._buf = BytesIO(body)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._buf.read()


@pytest.fixture
def addon(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()

    addon_mock = MagicMock()
    addon_mock.getSetting.return_value = ""

    def _info(key, *args, **kwargs):
        return {
            "id": "plugin.video.nzbdav",
            "name": "NZB-DAV",
            "version": "0.0.0",
            "path": str(tmp_path),
            "profile": str(profile),
        }.get(key, "")

    addon_mock.getAddonInfo.side_effect = _info

    xbmcaddon = sys.modules["xbmcaddon"]
    monkeypatch.setattr(xbmcaddon, "Addon", MagicMock(return_value=addon_mock))

    xbmcvfs = sys.modules["xbmcvfs"]
    monkeypatch.setattr(xbmcvfs, "translatePath", lambda p: p)

    return addon_mock, profile


def test_disabled_returns_disabled_reason(addon):
    results, reason = orchestrator_client.search_via_orchestrator(
        "movie",
        "Inception",
        year="2010",
        settings_getter=lambda k, d="": "false" if k == "use_orchestrator" else d,
    )
    assert results is None
    assert reason == "orchestrator_disabled"


def test_missing_addr_file_falls_back(addon):
    """When use_orchestrator is on but addr-file isn't written we
    must signal addr-unavailable so the caller falls through to the
    legacy pipeline rather than hanging."""
    settings = {
        "use_orchestrator": "true",
        "nzbhydra_enabled": "true",
        "hydra_url": "http://localhost:5076",
        "hydra_api_key": "k",
    }
    results, reason = orchestrator_client.search_via_orchestrator(
        "movie",
        "Inception",
        year="2010",
        settings_getter=lambda k, d="": settings.get(k, d),
    )
    assert results is None
    assert reason == "orchestrator_addr_unavailable"


def test_happy_path_converts_candidates(addon, monkeypatch):
    _addon_mock, profile = addon
    (profile / "orchestrator.addr").write_text("127.0.0.1:9876", encoding="utf-8")

    payload = json.dumps(
        {
            "search_id": "abc",
            "total_candidates": 2,
            "candidates": [
                {
                    "nzb_url": "http://hydra/api?t=get&id=1",
                    "title": "Inception.2010.1080p.BluRay.x264-FGT",
                    "size": 12_345_678,
                    "indexer": "nzbhydra2",
                    "pubdate": "Mon, 01 Jan 2024 00:00:00 +0000",
                    "guid": "g1",
                    "categories": [2010],
                    "extra": {"category": "Movie", "imdbid": "tt1375666"},
                },
                {
                    "nzb_url": "http://hydra/api?t=get&id=2",
                    "title": "Inception.2010.2160p.UHD.BluRay.HEVC-SPARKS",
                    "size": 80_000_000_000,
                    "indexer": "nzbhydra2",
                    "pubdate": "",
                    "guid": "g2",
                    "categories": [],
                    "extra": {},
                },
            ],
            "filtered": {"filtered": [], "all": []},
            "providers": [],
        }
    ).encode("utf-8")

    monkeypatch.setattr(
        orchestrator_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(payload),
    )

    settings = {
        "use_orchestrator": "true",
        "nzbhydra_enabled": "true",
        "hydra_url": "http://localhost:5076",
        "hydra_api_key": "k",
        "max_results": "25",
    }
    results, reason = orchestrator_client.search_via_orchestrator(
        "movie",
        "Inception",
        year="2010",
        imdb="tt1375666",
        settings_getter=lambda k, d="": settings.get(k, d),
    )
    assert reason is None
    assert isinstance(results, list)
    assert len(results) == 2
    assert results[0]["link"] == "http://hydra/api?t=get&id=1"
    assert results[0]["title"].startswith("Inception")
    assert results[0]["size"] == 12_345_678
    # newznabAttrs converts the extra map into the legacy list-of-dicts.
    names = {a["name"] for a in results[0]["newznabAttrs"]}
    assert "category" in names and "imdbid" in names
