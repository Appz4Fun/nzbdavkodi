# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=redefined-outer-name

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


class _FakeSseResponse:
    def __init__(self, lines):
        self.status = 200
        self._lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return b""


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


def test_resolve_disabled_returns_disabled_reason(addon):
    stream_url, stream_headers, reason = orchestrator_client.resolve_via_orchestrator(
        "http://hydra/api?t=get&id=1",
        "Inception.2010.1080p.BluRay.x264-FGT",
        settings_getter=lambda k, d="": "false" if k == "use_orchestrator" else d,
    )
    assert stream_url is None
    assert stream_headers is None
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


def test_resolve_happy_path_returns_stream(addon, monkeypatch):
    _addon_mock, profile = addon
    (profile / "orchestrator.addr").write_text("127.0.0.1:9876", encoding="utf-8")

    payload = json.dumps(
        {
            "resolve_id": "01ABC",
            "primary_peer_id": "01PEER",
            "nzo_id": "nzo-1",
            "stream_url": "http://webdav/content/Movie/Movie.mkv",
            "stream_headers": {"Authorization": "Basic dXNlcjpwYXNz"},
            "peers": [
                {
                    "peer_id": "01PEER",
                    "state": "ready",
                    "validation_state": "single_peer_phase_2",
                    "nzo_id": "nzo-1",
                    "stream_url": "http://webdav/content/Movie/Movie.mkv",
                    "stream_headers": {"Authorization": "Basic dXNlcjpwYXNz"},
                    "content_length": 1234,
                }
            ],
        }
    ).encode("utf-8")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(payload)

    monkeypatch.setattr(orchestrator_client.urllib.request, "urlopen", _fake_urlopen)

    settings = {
        "use_orchestrator": "true",
        "nzbdav_url": "http://nzbdav:3000",
        "nzbdav_api_key": "nzbdav-key",
        "webdav_url": "http://webdav:3000",
        "webdav_username": "user",
        "webdav_password": "pass",
        "webdav_content_root": "content",
    }
    stream_url, stream_headers, reason = orchestrator_client.resolve_via_orchestrator(
        "http://hydra/api?t=get&id=1",
        "Inception.2010.1080p.BluRay.x264-FGT",
        poll_interval=2,
        download_timeout=120,
        settings_getter=lambda k, d="": settings.get(k, d),
    )

    assert reason is None
    assert stream_url == "http://webdav/content/Movie/Movie.mkv"
    assert stream_headers == {"Authorization": "Basic dXNlcjpwYXNz"}
    assert captured["url"] == "http://127.0.0.1:9876/v1/resolve"
    assert captured["body"]["nzb_url"] == "http://hydra/api?t=get&id=1"
    assert captured["body"]["title"] == "Inception.2010.1080p.BluRay.x264-FGT"
    assert captured["body"]["poll_interval_secs"] == 2
    assert captured["body"]["download_timeout_secs"] == 120
    assert captured["body"]["nzbdav"]["base_url"] == "http://nzbdav:3000"
    assert captured["body"]["nzbdav"]["api_key"] == "nzbdav-key"
    assert captured["body"]["nzbdav"]["webdav_url"] == "http://webdav:3000"


def test_resolve_can_return_validated_fallback_sources(addon, monkeypatch):
    _addon_mock, profile = addon
    (profile / "orchestrator.addr").write_text("127.0.0.1:9876", encoding="utf-8")

    payload = json.dumps(
        {
            "resolve_id": "01ABC",
            "primary_peer_id": "01PRIMARY",
            "nzo_id": "nzo-primary",
            "stream_url": "http://webdav/content/Movie/Primary.mkv",
            "stream_headers": {"Authorization": "Basic primary"},
            "peers": [
                {
                    "peer_id": "01PRIMARY",
                    "state": "ready",
                    "validation_state": "single_peer_phase_2",
                    "nzo_id": "nzo-primary",
                    "stream_url": "http://webdav/content/Movie/Primary.mkv",
                    "stream_headers": {"Authorization": "Basic primary"},
                    "content_length": 1234,
                },
                {
                    "peer_id": "01VALID",
                    "state": "ready",
                    "validation_state": "byte_sample_validated_phase_3",
                    "nzo_id": "nzo-valid",
                    "nzb_url": "http://hydra/api?t=get&id=valid",
                    "title": "Fallback.Valid.2026-GROUP",
                    "stream_url": "http://webdav/content/Movie/Fallback.mkv",
                    "stream_headers": {"Authorization": "Basic fallback"},
                    "content_length": 1234,
                },
                {
                    "peer_id": "01REJECTED",
                    "state": "rejected",
                    "validation_state": "byte_sample_mismatch_phase_3",
                    "stream_url": "http://webdav/content/Movie/Rejected.mkv",
                    "content_length": 1234,
                },
            ],
        }
    ).encode("utf-8")

    monkeypatch.setattr(
        orchestrator_client.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResponse(payload),
    )

    settings = {
        "use_orchestrator": "true",
        "nzbdav_url": "http://nzbdav:3000",
        "nzbdav_api_key": "nzbdav-key",
        "webdav_url": "http://webdav:3000",
    }
    stream_url, stream_headers, fallback_sources, reason = (
        orchestrator_client.resolve_via_orchestrator(
            "http://hydra/api?t=get&id=1",
            "Primary.Release.2026-GROUP",
            settings_getter=lambda k, d="": settings.get(k, d),
            return_fallback_sources=True,
        )
    )

    assert reason is None
    assert stream_url == "http://webdav/content/Movie/Primary.mkv"
    assert stream_headers == {"Authorization": "Basic primary"}
    assert fallback_sources == [
        {
            "title": "Fallback.Valid.2026-GROUP",
            "nzb_url": "http://hydra/api?t=get&id=valid",
            "job_name": "",
            "nzo_id": "nzo-valid",
            "stream_url": "http://webdav/content/Movie/Fallback.mkv",
            "stream_headers": {"Authorization": "Basic fallback"},
            "content_length": 1234,
            "validated": True,
        }
    ]


def test_resolve_posts_fallback_candidates_as_candidate_peers(addon, monkeypatch):
    _addon_mock, profile = addon
    (profile / "orchestrator.addr").write_text("127.0.0.1:9876", encoding="utf-8")

    payload = json.dumps(
        {
            "resolve_id": "01ABC",
            "primary_peer_id": "01PEER",
            "nzo_id": "nzo-1",
            "stream_url": "http://webdav/content/Movie/Movie.mkv",
            "stream_headers": {},
            "peers": [],
        }
    ).encode("utf-8")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(payload)

    monkeypatch.setattr(orchestrator_client.urllib.request, "urlopen", _fake_urlopen)

    settings = {
        "use_orchestrator": "true",
        "nzbdav_url": "http://nzbdav:3000",
        "nzbdav_api_key": "nzbdav-key",
        "webdav_url": "http://webdav:3000",
    }
    stream_url, _stream_headers, reason = orchestrator_client.resolve_via_orchestrator(
        "http://hydra/api?t=get&id=1",
        "Primary.Release.2026-GROUP",
        fallback_candidates=[
            {
                "link": "http://hydra/api?t=get&id=2",
                "title": "Fallback.Release.2026-GROUP",
                "size": 123456,
                "indexer": "Hydra",
                "newznabAttrs": [{"name": "guid", "value": "abc"}],
            },
            {"title": "missing link"},
            "not a candidate",
        ],
        settings_getter=lambda k, d="": settings.get(k, d),
    )

    assert reason is None
    assert stream_url == "http://webdav/content/Movie/Movie.mkv"
    assert captured["body"]["candidate_peers"] == [
        {
            "nzb_url": "http://hydra/api?t=get&id=2",
            "title": "Fallback.Release.2026-GROUP",
            "size": 123456,
            "indexer": "Hydra",
            "extra": {"guid": "abc"},
        }
    ]


def test_resolve_posts_peer_pool_cache_key(addon, monkeypatch):
    _addon_mock, profile = addon
    (profile / "orchestrator.addr").write_text("127.0.0.1:9876", encoding="utf-8")

    payload = json.dumps(
        {
            "resolve_id": "01ABC",
            "primary_peer_id": "01PEER",
            "nzo_id": "nzo-1",
            "stream_url": "http://webdav/content/Movie/Movie.mkv",
            "stream_headers": {},
            "peers": [],
        }
    ).encode("utf-8")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(payload)

    monkeypatch.setattr(orchestrator_client.urllib.request, "urlopen", _fake_urlopen)

    settings = {
        "use_orchestrator": "true",
        "nzbdav_url": "http://nzbdav:3000",
        "nzbdav_api_key": "nzbdav-key",
        "webdav_url": "http://webdav:3000",
    }
    _stream_url, _stream_headers, reason = orchestrator_client.resolve_via_orchestrator(
        "http://hydra/api?t=get&id=1",
        "Primary.Release.2026-GROUP",
        peer_pool_cache_key="v1:abc123",
        settings_getter=lambda k, d="": settings.get(k, d),
    )

    assert reason is None
    assert captured["body"]["peer_pool_cache_key"] == "v1:abc123"


def test_resolve_posts_caller_supplied_resolve_id(addon, monkeypatch):
    _addon_mock, profile = addon
    (profile / "orchestrator.addr").write_text("127.0.0.1:9876", encoding="utf-8")

    payload = json.dumps(
        {
            "resolve_id": "01PYPROGRESS",
            "primary_peer_id": "01PEER",
            "nzo_id": "nzo-1",
            "stream_url": "http://webdav/content/Movie/Movie.mkv",
            "stream_headers": {},
            "peers": [],
        }
    ).encode("utf-8")
    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(payload)

    monkeypatch.setattr(orchestrator_client.urllib.request, "urlopen", _fake_urlopen)

    settings = {
        "use_orchestrator": "true",
        "nzbdav_url": "http://nzbdav:3000",
        "webdav_url": "http://webdav:3000",
    }
    stream_url, _stream_headers, reason = orchestrator_client.resolve_via_orchestrator(
        "http://hydra/api?t=get&id=1",
        "Primary.Release.2026-GROUP",
        resolve_id="01PYPROGRESS",
        settings_getter=lambda k, d="": settings.get(k, d),
    )

    assert reason is None
    assert stream_url == "http://webdav/content/Movie/Movie.mkv"
    assert captured["body"]["resolve_id"] == "01PYPROGRESS"


def test_tail_resolve_events_parses_sse_events(addon, monkeypatch):
    _addon_mock, profile = addon
    (profile / "orchestrator.addr").write_text("127.0.0.1:9876", encoding="utf-8")

    captured = {}
    lines = [
        b"event: submit.accepted\n",
        (
            b'data: {"sequence":1,"resolve_id":"01PYPROGRESS",'
            b'"event":"submit.accepted","peer_id":"01PEER",'
            b'"state":"submitted","reason":null,"payload":{"nzo_id":"nzo-1"}}\n'
        ),
        b"\n",
        b"event: resolve.completed\n",
        (
            b'data: {"sequence":2,"resolve_id":"01PYPROGRESS",'
            b'"event":"resolve.completed","peer_id":"01PEER",'
            b'"state":"completed","reason":null,"payload":{"peer_count":1}}\n'
        ),
        b"\n",
    ]

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        return _FakeSseResponse(lines)

    monkeypatch.setattr(orchestrator_client.urllib.request, "urlopen", _fake_urlopen)

    events = []
    reason = orchestrator_client.tail_resolve_events(
        "01PYPROGRESS",
        events.append,
        settings_getter=lambda k, d="": "true" if k == "use_orchestrator" else d,
    )

    assert reason is None
    assert (
        captured["url"]
        == "http://127.0.0.1:9876/v1/resolve/01PYPROGRESS/events?tail=true"
    )
    assert events == [
        {
            "sequence": 1,
            "resolve_id": "01PYPROGRESS",
            "event": "submit.accepted",
            "peer_id": "01PEER",
            "state": "submitted",
            "reason": None,
            "payload": {"nzo_id": "nzo-1"},
        },
        {
            "sequence": 2,
            "resolve_id": "01PYPROGRESS",
            "event": "resolve.completed",
            "peer_id": "01PEER",
            "state": "completed",
            "reason": None,
            "payload": {"peer_count": 1},
        },
    ]
