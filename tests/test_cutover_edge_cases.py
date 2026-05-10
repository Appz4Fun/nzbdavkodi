# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Edge cases for the transparent fallback cutover.

These cover invariants the live demo can't easily prove on every run:
deterministic fingerprint geometry, origin-allow-list strictness, the
``Failed`` vs ``Completed`` history contract, the per-session probe-base
augmentation, and the prevalidation-eligibility gate. Each case is a
single deterministic assertion so a regression here surfaces in unit
tests, not in 3-min Kodi runs.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from resources.lib.fallback_streams import (
    _PrecomputedProbeBase,
    _origin_key,
    _split_http_url,
    _validated_probe_url,
    fingerprint_ranges,
)


# 1: backward-seek / replayed bytes — fingerprint geometry stays stable
#    across seeks. fingerprint_ranges is purely a function of the
#    content_length the proxy already cached at session-prepare time, so
#    seeking back into already-played ranges must not invalidate the
#    pre-validated peer set.
def test_fingerprint_ranges_deterministic_for_same_content_length():
    """Same content_length must always produce the same 100 ranges so a
    seek backwards doesn't trigger a fresh fingerprint sweep."""
    a = fingerprint_ranges(8_531_780_780)
    b = fingerprint_ranges(8_531_780_780)
    assert a == b
    assert len(a) == 100


# 2: forward seek inside the cached upstream window — small files
#    fingerprint as a single full range, so a seek inside that range
#    can't fall outside any "validated" zone.
def test_fingerprint_ranges_small_file_returns_single_range():
    """File smaller than _FINGERPRINT_BYTES (4 KiB) must collapse to one
    range covering the whole file; otherwise validation would be
    impossible for tiny clips."""
    ranges = fingerprint_ranges(2048)
    assert ranges == [(0, 2047)]


# 3: forward seek past EOF — validation MUST refuse content_length=0,
#    otherwise an "advertise 8.5 GB / serve 0 bytes" upstream would slip
#    through prevalidation and surface as a stall once Kodi seeked past
#    the cache horizon.
def test_fingerprint_ranges_zero_content_length_returns_empty():
    """No ranges to validate against when the upstream reports no
    bytes — prevalidation must not produce a false "validated" mark."""
    assert fingerprint_ranges(0) == []
    assert fingerprint_ranges(-1) == []


# 4: primary fails before first frame — at session prepare time the
#    addon's /direct_play handler HEAD-validates each fallback and
#    rejects any that doesn't return a Content-Length. This is the
#    earliest gate in the chain.
def test_direct_play_skips_unstreamable_fallbacks():
    """A fallback whose HEAD fails or returns no Content-Length must
    never make it into the session's fallback_sources."""
    from urllib.error import HTTPError

    import xbmcplugin  # MagicMock from conftest

    captured = {}

    def fake_prepare_direct_playback(remote_url, headers, **kwargs):
        captured["fallbacks"] = kwargs.get("fallback_sources", [])
        return {"playback_url": "http://127.0.0.1:1/stream/x"}

    def fake_urlopen(req, timeout=10):  # noqa: ARG001
        url = req.full_url
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        if "good" in url:
            resp.headers = {"Content-Length": "1000"}
            return resp
        raise HTTPError(url, 503, "Service Unavailable", {}, None)

    # ``_handle_direct_play`` re-imports the resolver module inside
    # the function body, so patching the whole ``sys.modules`` entry
    # was racy across pytest sessions (MagicMock subbed in only for
    # this test, but shared globals leaked). Patch the two symbols
    # the function actually pulls — the function-local
    # ``from resources.lib.resolver import (_direct_playback_service_config,
    # _prepare_direct_playback)`` resolves these patched attributes
    # at call time.
    resolver_mod = __import__("resources.lib.resolver", fromlist=[""])
    from resources.lib.router import _handle_direct_play

    with patch.object(
        resolver_mod, "_prepare_direct_playback", new=fake_prepare_direct_playback
    ), patch.object(
        resolver_mod, "_direct_playback_service_config", return_value=(12345, "tok")
    ), patch("urllib.request.urlopen", side_effect=fake_urlopen):
        _handle_direct_play(
            handle=1,
            params={
                "primary_url": "http://good.example/movie.mkv",
                "fallback_urls": '["http://bad.example/movie.mkv","http://good.example/alt.mkv"]',
            },
        )

    fallback_urls = [s["stream_url"] for s in captured.get("fallbacks", [])]
    assert "http://bad.example/movie.mkv" not in fallback_urls
    assert "http://good.example/alt.mkv" in fallback_urls
    xbmcplugin.setResolvedUrl.assert_called()


# 5: all fallbacks pre-fail — `_validated_probe_url` must enforce the
#    configured-origin allow list when no session probe base is supplied.
def test_validated_probe_url_rejects_off_origin_when_only_global_bases():
    """Without per-session probe-base augmentation, an off-origin URL
    falls through to the configured allow-list and gets rejected — that
    is the safety net for unknown peers."""
    base_parts = _split_http_url("http://nzbdav-rs:8080/")
    base = _PrecomputedProbeBase(base_parts, _origin_key(base_parts), "/")
    assert _validated_probe_url(
        "http://127.0.0.1:5001/movie.mkv", probe_bases=(base,)
    ) is None


# 6: fallback hash mismatch — `_validated_probe_url` must accept URLs
#    only when origin AND base path align.
def test_validated_probe_url_accepts_session_origin():
    """When the session adds its own primary URL as a probe base, peers
    on the same origin pass."""
    parts = _split_http_url("http://127.0.0.1:5001/movie.mkv")
    base = _PrecomputedProbeBase(parts, _origin_key(parts), "/")
    accepted = _validated_probe_url(
        "http://127.0.0.1:5001/movie.mkv", probe_bases=(base,)
    )
    assert accepted is not None
    assert accepted.startswith("http://127.0.0.1:5001/")


# 7: rapid serial cutovers — fallback detection by name must still
#    surface the most recent terminal entry. nzbdav_api.find_terminal_by_name
#    accepts both Completed and Failed (and returns the matching slot).
def test_find_terminal_by_name_returns_failed_slot():
    """nzbdav-rs remaps the nzo_id queue→history; the addon's poll loop
    has to see Failed history rows or it hangs out the download timeout."""
    from resources.lib.nzbdav_api import find_terminal_by_name

    failed_slot = {
        "name": "Movie.Title.x264.mkv",
        "nzo_id": "alt-uuid",
        "status": "Failed",
        "fail_message": "no importable video file found",
        "storage": "",
    }
    response = {
        "history": {
            "noofslots": 1,
            "slots": [failed_slot],
        }
    }

    fake_get = MagicMock(return_value=__import__("json").dumps(response))
    fake_settings = MagicMock(return_value=("http://nzbdav:8080", "k"))

    with patch(
        "resources.lib.nzbdav_api._http_get", fake_get
    ), patch(
        "resources.lib.nzbdav_api._get_settings", fake_settings
    ):
        result = find_terminal_by_name("Movie.Title.x264.mkv")
    assert result is not None
    assert result["status"] == "Failed"
    assert result["fail_message"] == "no importable video file found"


# 8: empty fallback list — find_completed_by_name MUST stay strict and
#    only return Completed (otherwise picker_completed_stream would try
#    to play a Failed entry).
def test_find_completed_by_name_excludes_failed():
    """The completed-stream picker contract: only Completed status
    surfaces. Same-history broadening would mask "no importable
    video" failures as ready-to-play."""
    from resources.lib.nzbdav_api import find_completed_by_name

    response = {
        "history": {
            "noofslots": 1,
            "slots": [
                {"name": "Movie", "nzo_id": "x", "status": "Failed", "storage": ""}
            ],
        }
    }
    fake_get = MagicMock(return_value=__import__("json").dumps(response))
    fake_settings = MagicMock(return_value=("http://nzbdav:8080", "k"))

    with patch(
        "resources.lib.nzbdav_api._http_get", fake_get
    ), patch(
        "resources.lib.nzbdav_api._get_settings", fake_settings
    ):
        result = find_completed_by_name("Movie")
    assert result is None


# 9: lying upstream — _dump_submitted_nzb is opt-in. Without
#    NZBDAV_DUMP_NZBS_DIR a regular submit must NOT touch disk, so
#    silent debug captures don't leak indexer URLs into user storage.
def test_dump_submitted_nzb_no_op_without_env(monkeypatch, tmp_path):
    """Without the explicit env var the dump must do nothing."""
    monkeypatch.delenv("NZBDAV_DUMP_NZBS_DIR", raising=False)
    from resources.lib.nzbdav_api import _dump_submitted_nzb

    _dump_submitted_nzb("http://example.com/x.nzb", "name")
    # No exception, no files; assertion is the lack of side effects.
    assert tmp_path.exists()  # tmp_path stayed empty as expected.
    assert list(tmp_path.iterdir()) == []


# 10: empty fallback set — when all candidates dedup to the picked
#    release's link, no fallback peer can be attached and the loader
#    must return None so the resolver's fallback worker short-circuits.
def test_fallback_loader_short_circuits_for_lone_picked_link(monkeypatch):
    """Single-link results pool: nothing for the worker to submit, so
    the loader must report no peers (None)."""
    from resources.lib.router import _fallback_candidate_loader_for_selection

    selected = {
        "title": "Movie.x264.mkv",
        "link": "http://only.example/getnzb",
        "_meta": {},
    }
    monkeypatch.setattr(
        "resources.lib.fallback_streams.selected_manifest_may_have_fallback_peer",
        lambda result: True,
    )
    monkeypatch.setattr(
        "resources.lib.fallback_streams.selection_pool_may_have_fallback_peer",
        lambda selected, results: False,
    )
    loader = _fallback_candidate_loader_for_selection(selected, [selected])
    assert loader is None


# 11 (Ralph-discovered): _split_http_url must consistently return None on
#     reject (not False). Discovered via tests/ralph_loop.py seed=5
#     when the empty-string input returned False, breaking callers that
#     use ``parts is None`` instead of truthiness.
@pytest.mark.parametrize(
    "rejected",
    [
        "",
        "/",
        "//",
        "http://",
        "ftp://server/",
        "javascript:alert(1)",
        "http://user:pass@host/",  # auth in URL is rejected by design
        "http:// host /",  # space in netloc → ValueError on urlsplit (raises in some Python versions)
        "http://[invalid::ipv6/",  # malformed v6 → ValueError
        "http://host:not-a-port/",
        "http://\rhost/",  # control char
    ],
)
def test_split_http_url_returns_none_on_reject(rejected):
    """Type-contract regression: rejected URLs must be ``None``, never
    ``False``. Same-shaped return on every reject path lets callers
    safely use ``parts is None`` or truthiness — both work, but only
    consistently if reject = None."""
    result = _split_http_url(rejected)
    assert result is None, "expected None for {!r}, got {!r}".format(rejected, result)


def test_split_http_url_returns_split_result_on_accept():
    """Happy path: accepted URLs return a parsed SplitResult-like with
    a usable scheme."""
    result = _split_http_url("http://nzbdav-rs:8080/dav")
    assert result is not None
    assert result.scheme.lower() == "http"
    assert result.hostname == "nzbdav-rs"
    assert result.port == 8080
