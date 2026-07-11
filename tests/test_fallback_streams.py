# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import hashlib
import threading
import time as _time
from unittest.mock import ANY, MagicMock, patch
from urllib.error import URLError
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

import pytest
from resources.lib.fallback_streams import (
    _SAFE_JOB_RE,
    _fallback_settings,
    attach_fallback_candidates,
    attach_fallback_candidates_for_selection,
    build_fallback_job_name,
    build_prepare_fallback_payload,
    fetch_content_length,
    fetch_range_digest,
    fingerprint_ranges,
    first_prefetchable_fallback_peer,
)
from resources.lib.nzb_manifest import make_empty_manifest


@pytest.fixture(autouse=True)
def _clear_fallback_manifest_cache():
    from resources.lib import fallback_streams

    fallback_streams.clear_fallback_manifest_cache()
    yield
    fallback_streams.clear_fallback_manifest_cache()


def _mock_range_response(body, status=206, headers=None):
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=body)
    resp.status = status
    resp.getcode = MagicMock(return_value=status)
    header_map = {str(key).lower(): value for key, value in (headers or {}).items()}
    resp.headers.get = MagicMock(
        side_effect=lambda key, default=None: header_map.get(str(key).lower(), default)
    )
    return resp


def _nzb_xml(files):
    body = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">',
    ]
    body.extend(files)
    body.append("</nzb>")
    return "\n".join(body).encode("utf-8")


def _nzb_file(subject, segments):
    segment_xml = "\n".join(
        '<segment bytes="{}" number="{}">{}</segment>'.format(size, number, msgid)
        for number, size, msgid in segments
    )
    return """
    <file poster="poster" date="1777937305" subject={}>
      <groups><group>alt.binaries.test</group></groups>
      <segments>{}</segments>
    </file>
    """.format(quoteattr(subject), segment_xml)


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


def _fallback_setting(key):
    return {
        "webdav_url": "http://webdav/content",
        "nzbdav_url": "http://nzbdav:3000",
    }.get(key, "")


def test_configured_stream_bases_use_schema_defaults_without_kodi_fallback():
    from resources.lib import fallback_streams

    with patch(
        "resources.lib.router._get_script_setting",
        side_effect=lambda _key, default="": default,
    ), patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon",
        side_effect=AssertionError("script probe setup should not call Kodi settings"),
    ):
        bases = fallback_streams._configured_stream_bases()

    rendered = [(item.scheme, item.netloc, item.path) for item in bases]
    assert ("http", "localhost:8080", "") in rendered
    assert ("http", "localhost:3000", "") in rendered


def test_schema_default_reader_supports_old_and_new_settings_format():
    from xml.etree import ElementTree as ET

    from resources.lib import fallback_streams

    root = ET.fromstring("""
        <settings version="1">
          <section id="plugin.video.nzbdav">
            <category id="connection" label="30000" help="30300">
              <group id="webdav" label="30006">
                <setting id="webdav_url" type="string" label="30007">
                  <default>http://localhost:8080</default>
                </setting>
                <setting
                  id="legacy_url"
                  type="text"
                  label="30005"
                  default="http://localhost:3000"
                />
              </group>
            </category>
          </section>
        </settings>
        """)

    assert (
        fallback_streams._setting_default_from_root(root, "webdav_url")
        == "http://localhost:8080"
    )
    assert (
        fallback_streams._setting_default_from_root(root, "legacy_url")
        == "http://localhost:3000"
    )


def test_schema_setting_default_parses_real_file_via_safe_fromstring(tmp_path):
    from resources.lib import fallback_streams

    schema_file = tmp_path / "settings.xml"
    schema_file.write_text(
        """
        <settings version="1">
          <section id="plugin.video.nzbdav">
            <category id="connection">
              <group id="webdav">
                <setting id="webdav_url" type="string">
                  <default>http://example-schema:9999</default>
                </setting>
              </group>
            </category>
          </section>
        </settings>
        """,
        encoding="utf-8",
    )

    with patch("xbmcvfs.translatePath", return_value=str(schema_file)):
        assert (
            fallback_streams._schema_setting_default("webdav_url")
            == "http://example-schema:9999"
        )


def test_configured_stream_bases_tolerates_trailing_space_in_url():
    """A stray trailing space in nzbdav_url must not empty the probe-base
    allow-list. _split_http_url rejects whitespace in the netloc (an SSRF/
    homograph guard), so an un-stripped config value silently drops the only
    allowed origin — which makes fallback content-length probes return 0 and
    every byte-identical fallback get rejected at cutover.
    """
    from resources.lib import fallback_streams

    def _spaced(key):
        return {
            "webdav_url": "",
            "nzbdav_url": "http://192.168.1.93:3000 ",
        }.get(key, "")

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_spaced,
    ):
        bases = fallback_streams.configured_stream_probe_bases()

    assert len(bases) == 1
    assert bases[0].origin == ("http", "192.168.1.93", 3000)


def _manifest(kind, name, size, digest, article_count=2):
    manifest = {
        "payload_kind": kind,
        "group_name": name,
        "group_bytes": size,
        "video_name": name if kind == "video" else "",
        "normalized_video_name": name if kind == "video" else "",
        "video_bytes": size if kind == "video" else 0,
        "archive_base_name": name if kind == "archive" else "",
        "article_digest": digest,
        "article_count": article_count,
        "skipped_candidate_count": 0,
        "skipped_candidates": [],
        "unsupported_reason": "",
    }
    return manifest


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_manifest_grouping_uses_video_name_and_bytes_not_result_size(
    mock_settings, mock_fetch
):
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
    manifests = {
        "https://a/nzb": _manifest("video", "example movie 2026 group.mkv", 8000, "a"),
        "https://b/nzb": _manifest("video", "example movie 2026 group.mkv", 8000, "b"),
        "https://c/nzb": _manifest("video", "example movie 2026 group.mkv", 9000, "c"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    results = [primary, duplicate, unrelated]

    assert attach_fallback_candidates(results) is results
    assert primary["_fallback_candidates"] == [duplicate]
    assert duplicate["_fallback_candidates"] == [primary]
    assert unrelated["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.telemetry.log_timing")
@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_attach_fallback_candidates_logs_manifest_timing(
    mock_settings, mock_fetch, mock_log_timing
):
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
    manifests = {
        "https://a/nzb": _manifest("video", "example movie 2026 group.mkv", 8000, "a"),
        "https://b/nzb": _manifest("video", "example movie 2026 group.mkv", 8000, "b"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, duplicate])

    assert mock_log_timing.call_count == 1
    label, elapsed_ms = mock_log_timing.call_args.args
    assert label == "fallback_manifests"
    assert elapsed_ms >= 0
    assert mock_log_timing.call_args.kwargs == {
        "input": 2,
        "fetched": 2,
    }


@patch("resources.lib.fallback_streams._fallback_settings")
def test_malformed_manifest_group_bytes_fails_closed_without_aborting(mock_settings):
    mock_settings.return_value = (True, 5)
    malformed = _result("Movie bad manifest", "https://idx/a.nzb", 1)
    malformed["_fallback_manifest"] = _manifest("video", "movie.mkv", 1000, "a")
    malformed["_fallback_manifest"]["group_bytes"] = "not-a-number"
    valid = _result("Movie valid manifest", "https://idx/b.nzb", 2)
    valid["_fallback_manifest"] = _manifest("video", "movie.mkv", 1000, "b")

    attach_fallback_candidates([malformed, valid])

    assert malformed["_fallback_candidates"] == []
    assert valid["_fallback_candidates"] == []


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
def test_attach_fallback_candidates_skips_settings_for_duplicate_only_pool(
    mock_settings,
):
    mock_settings.side_effect = AssertionError("settings should not be read")
    selected = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://idx/same.nzb",
        1000,
    )
    duplicate = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP repost",
        "https://idx/same.nzb",
        1000,
    )
    missing_link = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP missing",
        "",
        1000,
    )

    attach_fallback_candidates([selected, duplicate, missing_link])

    assert selected["_fallback_candidates"] == []
    assert duplicate["_fallback_candidates"] == []
    assert missing_link["_fallback_candidates"] == []
    mock_settings.assert_not_called()


@patch("resources.lib.fallback_streams._title_tokens")
def test_first_prefetchable_peer_skips_title_tokens_for_single_selected_result(
    mock_title_tokens,
):
    selected = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://idx/selected.nzb",
        1000,
    )

    peer = first_prefetchable_fallback_peer(selected, [selected])

    assert peer is None
    mock_title_tokens.assert_not_called()


@patch("resources.lib.fallback_streams._title_tokens")
def test_first_prefetchable_peer_skips_selected_title_tokens_for_profile_mismatches(
    mock_title_tokens,
):
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-profile-mismatch.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "container": "mkv",
        },
    )
    lower_profile = _result(
        "The.Matrix.1999.1080p.WEB-DL.x264-GROUP",
        "https://idx/lower-profile.nzb",
        12000000000,
        meta={
            "resolution": "1080p",
            "quality": "WEB-DL",
            "codec": "x264/AVC",
            "hdr": [],
            "audio": ["DDP5.1"],
            "container": "mp4",
        },
    )

    peer = first_prefetchable_fallback_peer(selected, [selected, lower_profile])

    assert peer is None
    mock_title_tokens.assert_not_called()


@patch("resources.lib.fallback_streams._title_tokens")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_attach_skips_title_tokens_for_single_selected_result(
    mock_settings, mock_title_tokens
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://idx/selected-only.nzb",
        1000,
    )

    attach_fallback_candidates_for_selection(selected, [selected])

    assert selected["_fallback_candidates"] == []
    mock_title_tokens.assert_not_called()


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._title_tokens")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_attach_skips_selected_title_tokens_for_cached_profile_mismatches(
    mock_settings, mock_title_tokens, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected_meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "container": "mkv",
    }
    mismatch_meta = dict(selected_meta)
    mismatch_meta["resolution"] = "1080p"
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-profile-mismatch-selection.nzb",
        60000000000,
        meta=selected_meta,
    )
    profile_mismatches = [
        _result(
            "The.Matrix.1999.ProfileMismatch{:02d}.1080p.BluRay.REMUX."
            "DV.HEVC-GROUP".format(index),
            "https://idx/selection-profile-mismatch-{}.nzb".format(index),
            60000000000,
            meta=mismatch_meta,
        )
        for index in range(5)
    ]

    attach_fallback_candidates_for_selection(selected, [selected] + profile_mismatches)

    assert selected["_fallback_candidates"] == []
    mock_title_tokens.assert_not_called()
    mock_fetch.assert_not_called()


@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_attach_skips_settings_for_single_selected_result(mock_settings):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://idx/selected-settings-skip.nzb",
        1000,
    )

    attach_fallback_candidates_for_selection(selected, [selected])

    assert selected["_fallback_candidates"] == []
    mock_settings.assert_not_called()


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._prefetch_gate_proof")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_attach_skips_prefetch_proof_for_duplicate_links(
    mock_settings, mock_prefetch_proof, mock_fetch
):
    mock_settings.return_value = (True, 5)
    mock_prefetch_proof.return_value = None
    selected = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://idx/selected-duplicate-link.nzb",
        1000,
    )
    duplicate = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP mirror",
        selected["link"],
        1000,
    )
    duplicate["_fallback_prefetch_gate_proof"] = ("stale-proof",)

    attach_fallback_candidates_for_selection(selected, [selected, duplicate])

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()
    mock_prefetch_proof.assert_not_called()


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_prefetch_title_tokens_are_reused_for_selection_attach(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 1)
    selected_title = "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP"
    related_title = "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT"
    meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "group": "GROUP",
        "container": "mkv",
    }
    selected = _result(
        selected_title,
        "https://idx/selected.nzb",
        60000000000,
        meta=meta,
    )
    related = _result(
        related_title,
        "https://idx/related.nzb",
        60000000000,
        meta=meta,
    )
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        related["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "related"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]
    release_title_normalizations = []
    original_normalize_title = fallback_streams._normalize_title

    def counted_normalize_title(value):
        if value in (selected_title, related_title):
            release_title_normalizations.append(value)
        return original_normalize_title(value)

    with patch(
        "resources.lib.fallback_streams._normalize_title",
        side_effect=counted_normalize_title,
    ):
        peer = first_prefetchable_fallback_peer(selected, [selected, related])
        attach_fallback_candidates_for_selection(selected, [selected, related])

    assert peer is related
    assert selected["_fallback_candidates"] == [related]
    assert release_title_normalizations == [selected_title, related_title]


def test_fallback_settings_default_to_enabled_with_five_candidates():
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        return_value="",
    ):
        assert _fallback_settings() == (True, 5)


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_same_article_mirrors_are_not_attached_as_fallbacks(mock_settings, mock_fetch):
    mock_settings.return_value = (True, 5)
    primary = _result("Movie", "https://idx/a.nzb", 1)
    mirror = _result("Movie mirror", "https://idx/b.nzb", 2)
    repost = _result("Movie repost", "https://idx/c.nzb", 3)
    manifests = {
        "https://idx/a.nzb": _manifest("video", "movie.mkv", 1000, "articles-a"),
        "https://idx/b.nzb": _manifest("video", "movie.mkv", 1000, "articles-a"),
        "https://idx/c.nzb": _manifest("video", "movie.mkv", 1000, "articles-c"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, mirror, repost])

    assert primary["_fallback_candidates"] == [repost]
    assert mirror["_fallback_candidates"] == [repost]
    assert repost["_fallback_candidates"] == [primary]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_similar_reposts_with_different_manifest_names_are_fallbacks(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    primary = _result(
        "The.Bourne.Identity.2002.2160p.UHD.BluRay.REMUX.DV.HEVC-FraMeSToR",
        "https://idx/primary.nzb",
        51085890006,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["DTS:X"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    repost_months_later = _result(
        "The.Bourne.Identity.2002.UHD.BluRay.2160p.DTS-X.7.1.DV.HEVC.REMUX-ALT",
        "https://idx/repost.nzb",
        51085890006,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["DTS:X"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    lower_quality = _result(
        "The.Bourne.Identity.2002.1080p.BluRay.x264-GRP",
        "https://idx/1080p.nzb",
        12000000000,
        meta={
            "resolution": "1080p",
            "quality": "BluRay",
            "codec": "x264/AVC",
            "hdr": [],
            "audio": ["DTS-HD MA"],
            "group": "GRP",
            "container": "mkv",
        },
    )
    manifests = {
        "https://idx/primary.nzb": _manifest(
            "video", "bourne identity framestor.mkv", 51085890006, "articles-a"
        ),
        "https://idx/repost.nzb": _manifest(
            "video", "the bourne identity alternate post.mkv", 51085890006, "articles-b"
        ),
        "https://idx/1080p.nzb": _manifest(
            "video", "the bourne identity 1080p.mkv", 12000000000, "articles-c"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, repost_months_later, lower_quality])

    assert primary["_fallback_candidates"] == [repost_months_later]
    assert repost_months_later["_fallback_candidates"] == [primary]
    assert lower_quality["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_lenient_manifest_match_still_rejects_unrelated_titles(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    primary = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/matrix.nzb",
        50000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    unrelated_same_profile = _result(
        "The.Bourne.Identity.2002.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/bourne.nzb",
        50000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    manifests = {
        "https://idx/matrix.nzb": _manifest(
            "video", "matrix release.mkv", 50000000000, "articles-a"
        ),
        "https://idx/bourne.nzb": _manifest(
            "video", "bourne release.mkv", 50000000000, "articles-b"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, unrelated_same_profile])

    assert primary["_fallback_candidates"] == []
    assert unrelated_same_profile["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_attach_fallbacks_prefilters_unrelated_results_before_manifest_fetch(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    primary = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/primary-prefilter.nzb",
        50000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    repost = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/repost-prefilter.nzb",
        50000000000,
        meta=primary["_meta"],
    )
    unrelated = [
        _result(
            "Zq{0:02d}Yp{0:02d}.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP".format(index),
            "https://idx/unrelated-prefilter-{}.nzb".format(index),
            50000000000,
            meta=primary["_meta"],
        )
        for index in range(6)
    ]
    manifests = {
        primary["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 50000000000, "primary"
        ),
        repost["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 50000000000, "repost"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, repost] + unrelated)

    assert primary["_fallback_candidates"] == [repost]
    assert repost["_fallback_candidates"] == [primary]
    assert [result["_fallback_candidates"] for result in unrelated] == [
        [],
        [],
        [],
        [],
        [],
        [],
    ]
    assert [call.args[0] for call in mock_fetch.call_args_list] == [
        "https://idx/primary-prefilter.nzb",
        "https://idx/repost-prefilter.nzb",
    ]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_fallback_matching_rejects_different_matrix_encodes_without_prefilled_meta(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    primary = {
        "title": (
            "The.Matrix.1999.2160p.BDRip.TrueHD.7.1.Atmos.DV.HDR10." "x265.10bit-MarkII"
        ),
        "link": "https://idx/primary.nzb",
        "size": 68000000000,
    }
    webdl = {
        "title": "The.Matrix.1999.1080p.AMZN.WEB-DL.DDP5.1.H.264-GPRS",
        "link": "https://idx/webdl.nzb",
        "size": 18000000000,
    }
    bluray = {
        "title": "The.Matrix.1999.1080p.BluRay.DTS.x264.D.Z0N3",
        "link": "https://idx/bluray.nzb",
        "size": 16000000000,
    }
    manifests = {
        "https://idx/primary.nzb": _manifest("archive", "matrix", 0, "articles-a"),
        "https://idx/webdl.nzb": _manifest("archive", "matrix", 0, "articles-b"),
        "https://idx/bluray.nzb": _manifest("archive", "matrix", 0, "articles-c"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, webdl, bluray])

    assert primary["_fallback_candidates"] == []
    assert webdl["_fallback_candidates"] == []
    assert bluray["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_too_large_manifest_can_attach_same_profile_metadata_fallback(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    primary = {
        "title": (
            "The.Matrix.1999.UHD.BluRay.2160p.TrueHD.Atmos.7.1."
            "DV.HEVC.REMUX-FraMeSToR"
        ),
        "link": "https://idx/primary.nzb",
        "size": 61554618879,
    }
    repost = {
        "title": (
            "The.Matrix.1999.UHD.BluRay.2160p.TrueHD.Atmos.7.1."
            "DV.HEVC.REMUX-FraMeSToR"
        ),
        "link": "https://idx/repost.nzb",
        "size": 61538207424,
    }
    different_encode = {
        "title": "The.Matrix.1999.1080p.AMZN.WEB-DL.DDP5.1.H.264-GPRS",
        "link": "https://idx/webdl.nzb",
        "size": 18000000000,
    }
    manifests = {
        "https://idx/primary.nzb": make_empty_manifest("too_large"),
        "https://idx/repost.nzb": _manifest(
            "video",
            (
                "the matrix 1999 uhd bluray 2160p truehd atmos 7 1 "
                "dv hevc remux framestor.mkv"
            ),
            58598943755,
            "articles-b",
        ),
        "https://idx/webdl.nzb": make_empty_manifest("too_large"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, repost, different_encode])

    assert primary["_fallback_candidates"] == [repost]
    assert repost["_fallback_candidates"] == [primary]
    assert different_encode["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_unsupported_manifest_uses_indexer_size_as_synthetic_video_manifest(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "group": "GROUP",
        "container": "mkv",
    }
    primary = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/unsupported-primary.nzb",
        "60000000000",
        meta=meta,
    )
    fallback = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/unsupported-fallback.nzb",
        61000000000,
        meta=meta,
    )
    mock_fetch.return_value = make_empty_manifest("no_video_file")

    attach_fallback_candidates([primary, fallback])

    assert primary["_fallback_candidates"] == [fallback]
    assert fallback["_fallback_candidates"] == [primary]
    assert primary["_fallback_manifest"]["payload_kind"] == "video"
    assert primary["_fallback_manifest"]["group_bytes"] == 60000000000
    assert primary["_fallback_manifest"]["video_bytes"] == 60000000000
    assert (
        primary["_fallback_manifest"]["article_digest"]
        == hashlib.sha256(primary["link"].encode("utf-8")).hexdigest()
    )
    assert primary["_fallback_manifest"]["unsupported_reason"] == ""


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_tiny_indexer_size_does_not_synthesize_unsupported_manifest(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Movie 2026 1080p WEB-DL x265-GROUP",
        "https://idx/tiny-a.nzb",
        90 * 1024 * 1024,
    )
    fallback = _result(
        "Movie 2026 1080p WEB-DL x265-ALT",
        "https://idx/tiny-b.nzb",
        99 * 1024 * 1024,
    )
    mock_fetch.return_value = make_empty_manifest("no_video_file")

    attach_fallback_candidates([primary, fallback])

    assert primary["_fallback_candidates"] == []
    assert fallback["_fallback_candidates"] == []
    assert primary["_fallback_manifest"]["unsupported_reason"] == "no_video_file"


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_metadata_only_fallback_rejects_proper_repack_and_edition_mismatches(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    primary = {
        "title": (
            "Movie.2024.PROPER.REPACK.Extended.Cut.2160p.UHD.BluRay."
            "REMUX.DV.HEVC-GROUP"
        ),
        "link": "https://idx/primary.nzb",
        "size": 60000000000,
    }
    plain = {
        "title": "Movie.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "link": "https://idx/plain.nzb",
        "size": 60000000000,
    }
    theatrical = {
        "title": "Movie.2024.Theatrical.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "link": "https://idx/theatrical.nzb",
        "size": 60000000000,
    }
    manifests = {
        "https://idx/primary.nzb": make_empty_manifest("too_large"),
        "https://idx/plain.nzb": make_empty_manifest("too_large"),
        "https://idx/theatrical.nzb": make_empty_manifest("too_large"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, plain, theatrical])

    assert primary["_fallback_candidates"] == []
    assert plain["_fallback_candidates"] == []
    assert theatrical["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_prefilter_skips_manifest_fetch_for_unrelated_candidates(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    related = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/related.nzb",
        60000000000,
        meta=selected["_meta"],
    )
    unrelated = [
        _result(
            "Bourne.Identity.{:02d}.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP".format(index),
            "https://idx/unrelated-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(10)
    ]
    manifests = {
        "https://idx/selected.nzb": _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        "https://idx/related.nzb": _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "related"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates_for_selection(selected, [selected, related] + unrelated)

    assert selected["_fallback_candidates"] == [related]
    assert [call.args[0] for call in mock_fetch.call_args_list] == [
        "https://idx/selected.nzb",
        "https://idx/related.nzb",
    ]


@patch("resources.lib.fallback_streams.telemetry.log_timing")
@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_logs_manifest_timing(
    mock_settings, mock_fetch, mock_log_timing
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-timing.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    related = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/related-timing.nzb",
        60000000000,
        meta=selected["_meta"],
    )
    manifests = {
        "https://idx/selected-timing.nzb": _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        "https://idx/related-timing.nzb": _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "related"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates_for_selection(selected, [selected, related])

    assert selected["_fallback_candidates"] == [related]
    assert mock_log_timing.call_count == 1
    label, elapsed_ms = mock_log_timing.call_args.args
    assert label == "fallback_selection_manifests"
    assert elapsed_ms >= 0
    assert mock_log_timing.call_args.kwargs == {
        "attached": 1,
        "pool": 2,
        "selected_manifest_fetch": True,
    }


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_prefetch_rejects_indexer_size_outside_twenty_five_percent(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-size-gate.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "container": "mkv",
        },
    )
    oversized = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/oversized-size-gate.nzb",
        76000000000,
        meta=selected["_meta"],
    )

    attach_fallback_candidates_for_selection(selected, [selected, oversized])

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_prefilled_unsupported_manifest_can_synthesize_indexer_manifest(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "group": "GROUP",
        "container": "mkv",
    }
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/prefilled-unsupported.nzb",
        60000000000,
        meta=meta,
    )
    selected["_fallback_manifest"] = make_empty_manifest("no_video_file")
    fallback = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/prefilled-fallback.nzb",
        61000000000,
        meta=meta,
    )
    mock_fetch.return_value = _manifest(
        "video", "the matrix 1999 remux.mkv", 61000000000, "fallback"
    )

    attach_fallback_candidates_for_selection(selected, [selected, fallback])

    assert selected["_fallback_candidates"] == [fallback]
    assert selected["_fallback_manifest"]["payload_kind"] == "video"
    assert selected["_fallback_manifest"]["group_bytes"] == 60000000000
    assert [call.args[0] for call in mock_fetch.call_args_list] == [
        "https://idx/prefilled-fallback.nzb",
    ]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_cached_prefetch_proof_still_respects_indexer_size_gate(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 5)
    meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "container": "mkv",
    }
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/proof-selected.nzb",
        60000000000,
        meta=meta,
    )
    oversized = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/proof-oversized.nzb",
        76000000000,
        meta=meta,
    )
    fallback_streams._remember_prefetch_gate_match(
        selected, oversized, selected["_meta"], oversized["_meta"]
    )

    attach_fallback_candidates_for_selection(selected, [selected, oversized])

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_does_not_reset_unselected_candidate_lists(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 1)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    related = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/related-untouched.nzb",
        60000000000,
        meta=selected["_meta"],
    )
    unrelated = _result(
        "Bourne.Identity.2002.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/unrelated-untouched.nzb",
        60000000000,
        meta=selected["_meta"],
    )
    related_stale = ["stale-related"]
    unrelated_stale = ["stale-unrelated"]
    dict.__setitem__(related, "_fallback_candidates", related_stale)
    dict.__setitem__(unrelated, "_fallback_candidates", unrelated_stale)
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        related["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "related"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates_for_selection(selected, [selected, related, unrelated])

    assert selected["_fallback_candidates"] == [related]
    assert related["_fallback_candidates"] is related_stale
    assert unrelated["_fallback_candidates"] is unrelated_stale


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_skips_manifest_fetch_when_no_plausible_peers(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "container": "mkv",
        },
    )
    unrelated = [
        _result(
            "Bourne.Identity.{:02d}.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP".format(index),
            "https://idx/unrelated-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(10)
    ]

    attach_fallback_candidates_for_selection(selected, [selected] + unrelated)

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
def test_selection_fallback_skips_prefilter_for_unusable_prefetched_manifest(
    mock_fetch,
):
    from resources.lib import fallback_streams

    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-prefetched-fetch-error.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "container": "mkv",
        },
    )
    selected["_fallback_manifest"] = make_empty_manifest("fetch_error")
    unrelated = [
        _result(
            "Bourne.Identity.{:02d}.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP".format(index),
            "https://idx/unusable-selected-unrelated-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(10)
    ]

    with patch(
        "resources.lib.fallback_streams._fallback_settings", return_value=(True, 5)
    ) as mock_settings, patch(
        "resources.lib.fallback_streams._title_tokens",
        wraps=fallback_streams._title_tokens,
    ) as mock_title_tokens:
        attach_fallback_candidates_for_selection(selected, [selected] + unrelated)

    assert selected["_fallback_candidates"] == []
    mock_settings.assert_not_called()
    mock_title_tokens.assert_not_called()
    mock_fetch.assert_not_called()


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_skips_metadata_parse_for_unrelated_raw_titles(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected_meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "container": "mkv",
    }
    selected = {
        "title": "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "link": "https://idx/selected-raw.nzb",
        "size": 60000000000,
    }
    unrelated = [
        {
            "title": "Bourne.Identity.Raw{:02d}.2160p.UHD.BluRay.REMUX."
            "DV.HEVC-GROUP".format(index),
            "link": "https://idx/unrelated-raw-{}.nzb".format(index),
            "size": 60000000000,
        }
        for index in range(5)
    ]
    parsed_titles = []

    def parse_title_metadata(title):
        parsed_titles.append(title)
        return dict(selected_meta)

    with patch(
        "resources.lib.filter.parse_title_metadata", side_effect=parse_title_metadata
    ):
        attach_fallback_candidates_for_selection(selected, [selected] + unrelated)

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()
    assert not parsed_titles


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_rejects_batch_after_unusable_selected_manifest(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALTx{:02d}".format(index),
            "https://idx/fallback-unusable-selected-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(5)
    ]
    manifests = {selected["link"]: make_empty_manifest("fetch_error")}
    for index, candidate in enumerate(candidates):
        manifests[candidate["link"]] = _manifest(
            "video",
            "the matrix 1999 remux.mkv",
            60000000000,
            "fallback-{}".format(index),
        )
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates_for_selection(selected, [selected] + candidates)

    assert selected["_fallback_candidates"] == []
    assert selected["_fallback_manifest_error"] == "fetch_error"
    assert set(call.args[0] for call in mock_fetch.call_args_list) == {
        "https://idx/selected.nzb",
        "https://idx/fallback-unusable-selected-0.nzb",
        "https://idx/fallback-unusable-selected-1.nzb",
        "https://idx/fallback-unusable-selected-2.nzb",
        "https://idx/fallback-unusable-selected-3.nzb",
        "https://idx/fallback-unusable-selected-4.nzb",
    }


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_skips_candidate_wait_after_unusable_selected_manifest(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-fast-fail.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-SLOWx{:02d}".format(index),
            "https://idx/fallback-slow-unusable-selected-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 6)
    ]
    manifests = {selected["link"]: make_empty_manifest("fetch_error")}
    for candidate in candidates:
        manifests[candidate["link"]] = _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, candidate["link"]
        )
    candidate_started = threading.Event()
    release_candidates = threading.Event()

    candidate_completions = [0]
    completions_lock = threading.Lock()

    def fetch(url, **_kwargs):
        if url == selected["link"]:
            # Load-independent gate: the scan aborts on the unusable selected
            # manifest only once selected_ready, so block the selected manifest
            # until a candidate fetch has actually started. Candidates are in the
            # initial fetch window (submitted without needing selected_ready), so
            # this cannot deadlock -- candidate_started is set before the scan
            # aborts at realistic load. The 30s is a hang-safety bound only (a
            # normally scheduled daemon thread starts in ms).
            candidate_started.wait(30)
            return manifests[url]
        candidate_started.set()
        # No self-timeout: a candidate fetch stays in flight until the finally
        # below releases it (after the snapshot), so it can never complete before
        # we record whether the scan consumed it. The generous 3s only bounds a
        # regression where the scan wrongly waits for this fetch (then it completes
        # -> candidate_completions -> caught below).
        release_candidates.wait(timeout=3)
        with completions_lock:
            candidate_completions[0] += 1
        return manifests[url]

    mock_fetch.side_effect = fetch
    try:
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)
        with completions_lock:
            candidate_completions_at_return = candidate_completions[0]
    finally:
        release_candidates.set()

    # Deterministic via the selected-manifest gate above: candidate_started is
    # guaranteed set before the scan aborts, so this assert is load-independent.
    assert candidate_started.is_set()
    # Structural proof (load-independent): the unusable selected manifest must
    # abort the scan before any blocked candidate fetch is released (only the
    # finally above releases it, after the snapshot). If the scan wrongly kept
    # waiting, a candidate fetch would complete and be consumed -> count > 0.
    assert candidate_completions_at_return == 0
    assert selected["_fallback_candidates"] == []
    assert selected["_fallback_manifest_error"] == "fetch_error"


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_reuses_selected_title_tokens_while_prefiltering(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    unrelated = [
        _result(
            "Bourne.Identity.AltTitle{:02d}.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP".format(
                index
            ),
            "https://idx/unrelated-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(10)
    ]
    selected_title_token_calls = []
    original_title_tokens = fallback_streams._title_tokens

    def counted_title_tokens(result):
        if result is selected:
            selected_title_token_calls.append(result)
        return original_title_tokens(result)

    with patch(
        "resources.lib.fallback_streams._title_tokens", side_effect=counted_title_tokens
    ):
        attach_fallback_candidates_for_selection(selected, [selected] + unrelated)

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()
    assert len(selected_title_token_calls) == 1


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_skips_title_tokens_for_profile_mismatches(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "container": "mkv",
        },
    )
    profile_mismatches = [
        _result(
            "The.Matrix.1999.ProfileMismatch{:02d}.2160p.UHD.BluRay."
            "REMUX.DV.HEVC-GROUP".format(index),
            "https://idx/mismatch-{}.nzb".format(index),
            60000000000,
            meta={
                "resolution": "1080p",
                "quality": "REMUX",
                "codec": "x265/HEVC",
                "hdr": ["Dolby Vision"],
                "audio": ["TrueHD", "Atmos"],
                "container": "mkv",
            },
        )
        for index in range(10)
    ]
    candidate_title_token_calls = []
    original_title_tokens = fallback_streams._title_tokens

    def counted_title_tokens(result):
        if result is not selected:
            candidate_title_token_calls.append(result)
        return original_title_tokens(result)

    with patch(
        "resources.lib.fallback_streams._title_tokens", side_effect=counted_title_tokens
    ):
        attach_fallback_candidates_for_selection(
            selected, [selected] + profile_mismatches
        )

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()
    assert not candidate_title_token_calls


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_reuses_metadata_during_profile_prefilter(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 5)
    selected_meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "channels": "7.1",
        "container": "mkv",
    }
    candidate_meta = dict(selected_meta)
    candidate_meta["channels"] = "5.1"
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta=selected_meta,
    )
    profile_mismatches = [
        _result(
            "The.Matrix.1999.AudioMismatch{:02d}.2160p.UHD.BluRay."
            "REMUX.DV.HEVC-GROUP".format(index),
            "https://idx/audio-mismatch-{}.nzb".format(index),
            60000000000,
            meta=candidate_meta,
        )
        for index in range(5)
    ]
    meta_calls = []
    original_result_meta = fallback_streams._result_meta

    def counted_result_meta(result):
        meta_calls.append(result)
        return original_result_meta(result)

    with patch(
        "resources.lib.fallback_streams._result_meta", side_effect=counted_result_meta
    ):
        attach_fallback_candidates_for_selection(
            selected, [selected] + profile_mismatches
        )

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()
    assert not meta_calls


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_reuses_selected_metadata_across_profile_prefilter(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 5)
    selected_meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "channels": "7.1",
        "container": "mkv",
    }
    candidate_meta = dict(selected_meta)
    candidate_meta["channels"] = "5.1"
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta=selected_meta,
    )
    profile_mismatches = [
        _result(
            "The.Matrix.1999.AudioMismatch{:02d}.2160p.UHD.BluRay."
            "REMUX.DV.HEVC-GROUP".format(index),
            "https://idx/audio-mismatch-extra-{}.nzb".format(index),
            60000000000,
            meta=candidate_meta,
        )
        for index in range(5)
    ]
    meta_calls = []
    original_result_meta = fallback_streams._result_meta

    def counted_result_meta(result):
        meta_calls.append(result)
        return original_result_meta(result)

    with patch(
        "resources.lib.fallback_streams._result_meta", side_effect=counted_result_meta
    ):
        attach_fallback_candidates_for_selection(
            selected, [selected] + profile_mismatches
        )

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()
    assert not meta_calls


def test_prefetchable_peer_skips_title_tokens_for_cached_profile_mismatches():
    from resources.lib import fallback_streams

    selected_meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "container": "mkv",
    }
    mismatch_meta = dict(selected_meta)
    mismatch_meta["resolution"] = "1080p"
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta=selected_meta,
    )
    profile_mismatches = [
        _result(
            "The.Matrix.1999.ProfileMismatch{:02d}.1080p.BluRay.REMUX."
            "DV.HEVC-GROUP".format(index),
            "https://idx/profile-mismatch-{}.nzb".format(index),
            60000000000,
            meta=mismatch_meta,
        )
        for index in range(5)
    ]
    candidate_title_token_calls = []
    original_title_tokens = fallback_streams._title_tokens

    def counted_title_tokens(result):
        if result is not selected:
            candidate_title_token_calls.append(result)
        return original_title_tokens(result)

    with patch(
        "resources.lib.fallback_streams._title_tokens", side_effect=counted_title_tokens
    ):
        peer = fallback_streams.first_prefetchable_fallback_peer(
            selected, [selected] + profile_mismatches
        )

    assert peer is None
    assert not candidate_title_token_calls


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_skips_selected_metadata_parse_when_titles_unrelated(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 3)
    selected = {
        "title": "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "link": "https://idx/selected.nzb",
        "size": 60000000000,
    }
    unrelated = [
        {
            "title": "Totally.Other.Movie.{:02d}.2160p.UHD.BluRay.REMUX."
            "DV.HEVC-GROUP".format(index),
            "link": "https://idx/unrelated-{}.nzb".format(index),
            "size": 60000000000,
        }
        for index in range(5)
    ]
    meta_calls = []
    original_result_meta = fallback_streams._result_meta

    def counted_result_meta(result):
        meta_calls.append(result)
        return original_result_meta(result)

    with patch(
        "resources.lib.fallback_streams._result_meta", side_effect=counted_result_meta
    ):
        attach_fallback_candidates_for_selection(selected, [selected] + unrelated)

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()
    assert [call for call in meta_calls if call is selected] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_reuses_lazy_selected_meta_for_related_raw_peers(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 5)
    selected_meta = {
        "resolution": "2160p",
        "quality": "REMUX",
        "codec": "x265/HEVC",
        "hdr": ["Dolby Vision"],
        "audio": ["TrueHD", "Atmos"],
        "container": "mkv",
    }
    mismatch_meta = dict(selected_meta)
    mismatch_meta["resolution"] = "1080p"
    selected = {
        "title": "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "link": "https://idx/selected-raw-related.nzb",
        "size": 60000000000,
    }
    related_mismatches = [
        {
            "title": "The.Matrix.1999.RelatedRaw{:02d}.2160p.UHD.BluRay."
            "REMUX.DV.HEVC-GROUP".format(index),
            "link": "https://idx/related-raw-mismatch-{}.nzb".format(index),
            "size": 60000000000,
        }
        for index in range(5)
    ]
    selected_meta_reads = []
    original_result_meta = fallback_streams._result_meta

    def parse_title_metadata(title):
        if "RelatedRaw" in title:
            return dict(mismatch_meta)
        return dict(selected_meta)

    def counted_result_meta(result):
        if result is selected:
            selected_meta_reads.append(result)
        return original_result_meta(result)

    with patch(
        "resources.lib.filter.parse_title_metadata", side_effect=parse_title_metadata
    ), patch(
        "resources.lib.fallback_streams._result_meta", side_effect=counted_result_meta
    ):
        attach_fallback_candidates_for_selection(
            selected, [selected] + related_mismatches
        )

    assert selected["_fallback_candidates"] == []
    mock_fetch.assert_not_called()
    assert len(selected_meta_reads) == 1


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_stops_manifest_fetch_after_max_candidates(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 3)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALTx{:02d}".format(index),
            "https://idx/fallback-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 9)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        )
    }
    for index, candidate in enumerate(candidates, start=1):
        manifests[candidate["link"]] = _manifest(
            "video",
            "the matrix 1999 remux.mkv",
            60000000000,
            "fallback-{}".format(index),
        )
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates_for_selection(selected, [selected] + candidates)

    assert selected["_fallback_candidates"] == candidates[:3]
    fetched_urls = [call.args[0] for call in mock_fetch.call_args_list]
    assert fetched_urls[0] == "https://idx/selected.nzb"
    assert set(fetched_urls[1:]) == {
        "https://idx/fallback-1.nzb",
        "https://idx/fallback-2.nzb",
        "https://idx/fallback-3.nzb",
    }


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_fetches_candidate_manifests_in_parallel(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 2)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-parallel.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-PARx{:02d}".format(index),
            "https://idx/fallback-parallel-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 3)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        )
    }
    for index, candidate in enumerate(candidates, start=1):
        manifests[candidate["link"]] = _manifest(
            "video",
            "the matrix 1999 remux.mkv",
            60000000000,
            "parallel-{}".format(index),
        )

    started = []
    started_lock = threading.Lock()
    second_candidate_started = threading.Event()
    first_candidate_saw_second = [False]

    def fetch(url, **_kwargs):
        if url == selected["link"]:
            return manifests[url]
        with started_lock:
            started.append(url)
            if len(started) == 2:
                second_candidate_started.set()
        if url == candidates[0]["link"]:
            first_candidate_saw_second[0] = second_candidate_started.wait(0.2)
        return manifests[url]

    mock_fetch.side_effect = fetch

    attach_fallback_candidates_for_selection(selected, [selected] + candidates)

    assert selected["_fallback_candidates"] == candidates
    assert first_candidate_saw_second[0]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_overlaps_selected_manifest_with_candidate_batch(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 2)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-overlap.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-OVRx{:02d}".format(index),
            "https://idx/fallback-overlap-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 3)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        )
    }
    for index, candidate in enumerate(candidates, start=1):
        manifests[candidate["link"]] = _manifest(
            "video",
            "the matrix 1999 remux.mkv",
            60000000000,
            "overlap-{}".format(index),
        )

    candidate_started = threading.Event()
    selected_saw_candidate = [False]

    def fetch(url, **_kwargs):
        if url == selected["link"]:
            selected_saw_candidate[0] = candidate_started.wait(0.2)
        else:
            candidate_started.set()
        return manifests[url]

    mock_fetch.side_effect = fetch

    attach_fallback_candidates_for_selection(selected, [selected] + candidates)

    assert selected["_fallback_candidates"] == candidates
    assert selected_saw_candidate[0]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_pipelines_second_manifest_wave_after_underfill(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 3)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-pipeline.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-PIPEx{:02d}".format(index),
            "https://idx/fallback-pipeline-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 10)
    ]
    matching_digests = {
        "https://idx/fallback-pipeline-1.nzb": "match-1",
        "https://idx/fallback-pipeline-7.nzb": "match-7",
        "https://idx/fallback-pipeline-8.nzb": "match-8",
    }
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        )
    }
    for candidate in candidates:
        digest = matching_digests.get(candidate["link"])
        if digest is None:
            manifests[candidate["link"]] = _manifest(
                "video",
                "different {}.mkv".format(candidate["link"].rsplit("-", 1)[-1]),
                90000000000,
                "miss-{}".format(candidate["link"].rsplit("-", 1)[-1]),
            )
        else:
            manifests[candidate["link"]] = _manifest(
                "video", "the matrix 1999 remux.mkv", 60000000000, digest
            )

    seventh_started = threading.Event()
    fourth_saw_seventh = [False]

    def fetch(url, **_kwargs):
        if url == candidates[6]["link"]:
            seventh_started.set()
        if url == candidates[3]["link"]:
            fourth_saw_seventh[0] = seventh_started.wait(0.2)
        return manifests[url]

    mock_fetch.side_effect = fetch

    attach_fallback_candidates_for_selection(selected, [selected] + candidates)

    assert selected["_fallback_candidates"] == [
        candidates[0],
        candidates[6],
        candidates[7],
    ]
    assert fourth_saw_seventh[0]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_uses_later_completed_candidate_instead_of_slow_gap(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 2)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-slow-gap.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-GAP{:02d}".format(index),
            "https://idx/fallback-slow-gap-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 4)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        candidates[0]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "match-1"
        ),
        candidates[1]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "slow-match-2"
        ),
        candidates[2]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "match-3"
        ),
    }
    slow_started = threading.Event()
    release_slow = threading.Event()
    slow_completed = threading.Event()

    def fetch(url, **_kwargs):
        if url == candidates[1]["link"]:
            slow_started.set()
            # No self-timeout: the slow gap candidate stays in flight until the
            # finally below releases it (after the snapshot), so it can never
            # complete before we record whether the scan waited for it. The
            # generous 3s only bounds a regression where the scan blocks on this
            # fetch (then it completes -> slow_completed -> caught below).
            release_slow.wait(timeout=3)
            slow_completed.set()
        elif url == selected["link"]:
            # Load-independent gate: no candidate is attached, and the scan cannot
            # return, until selected_ready. Block the selected manifest until the
            # slow gap candidate's daemon thread has actually started, so
            # slow_started is provably set before the scan returns -- removing the
            # thread-start race at realistic load. The 30s is a hang-safety bound
            # only (a normally scheduled daemon thread starts in ms; the slow
            # candidate is in the initial window, so it is always submitted).
            slow_started.wait(30)
        return manifests[url]

    mock_fetch.side_effect = fetch
    try:
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)
        slow_completed_at_return = slow_completed.is_set()
    finally:
        release_slow.set()

    # Deterministic via the selected-manifest gate above: slow_started is
    # guaranteed set before the scan returns, so this assert is load-independent.
    assert slow_started.is_set()
    # Structural proof (load-independent): the slow gap candidate (GAP02) is left
    # in flight and never released here, so a healthy early-return that uses the
    # faster candidate[2] (GAP03) must NOT have waited for it. A
    # "waits-but-same-result" regression (drain the slow in-flight fetch, then
    # discard it) would block until it completed -> slow_completed set.
    assert not slow_completed_at_return
    # The list assertion then confirms the right (faster) candidate was attached.
    assert selected["_fallback_candidates"] == [candidates[0], candidates[2]]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_prefers_earlier_candidate_completing_within_settle_window(
    mock_settings, mock_fetch
):
    """An earlier candidate (index 1) that finishes slightly out of order but
    WITHIN the settle window must be preferred over a later index that completed
    first, so the cap-fill shortcut never skips an earlier exact-name/tier-0
    peer. Regression for the out-of-order candidate-cap settle-window fix."""
    mock_settings.return_value = (True, 2)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-settle.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-GAP{:02d}".format(index),
            "https://idx/fallback-settle-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 4)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        candidates[0]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "match-1"
        ),
        candidates[1]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "earlier-2"
        ),
        candidates[2]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "later-3"
        ),
    }

    def fetch(url, **_kwargs):
        # Index 1 lands a hair after index 2 but far inside the settle window,
        # so it must still win the second slot ahead of the later index 2.
        if url == candidates[1]["link"]:
            _time.sleep(0.02)
        return manifests[url]

    mock_fetch.side_effect = fetch
    with patch(
        "resources.lib.fallback_streams._FALLBACK_MANIFEST_SETTLE_WINDOW_SECONDS",
        0.3,
    ):
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)

    assert selected["_fallback_candidates"] == [candidates[0], candidates[1]]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_starts_followup_fetch_before_first_wave_tail_finishes(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 2)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-rolling-window.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ROLL{:02d}".format(index),
            "https://idx/fallback-rolling-window-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 5)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        candidates[0]["link"]: _manifest(
            "video", "different first miss.mkv", 90000000000, "miss-1"
        ),
        candidates[1]["link"]: _manifest(
            "video", "different slow miss.mkv", 90000000000, "miss-2"
        ),
        candidates[2]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "match-3"
        ),
        candidates[3]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "match-4"
        ),
    }
    third_started = threading.Event()
    release_slow_second = threading.Event()
    slow_second_saw_third = [False]

    def fetch(url, **_kwargs):
        if url == candidates[2]["link"]:
            third_started.set()
        if url == candidates[1]["link"]:
            slow_second_saw_third[0] = third_started.wait(timeout=0.2)
            release_slow_second.wait(timeout=1)
        return manifests[url]

    mock_fetch.side_effect = fetch

    try:
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)
    finally:
        release_slow_second.set()

    assert slow_second_saw_third[0]
    assert selected["_fallback_candidates"] == [candidates[2], candidates[3]]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_scales_second_wave_to_remaining_slots(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-scaled-wave.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-SCALEx{:02d}".format(index),
            "https://idx/fallback-scaled-wave-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 13)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        )
    }
    for index, candidate in enumerate(candidates, start=1):
        if index == 5:
            manifests[candidate["link"]] = _manifest(
                "video", "different matrix remux.mkv", 90000000000, "miss-5"
            )
        else:
            manifests[candidate["link"]] = _manifest(
                "video",
                "the matrix 1999 remux.mkv",
                60000000000,
                "scaled-{}".format(index),
            )

    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates_for_selection(selected, [selected] + candidates)

    assert selected["_fallback_candidates"] == candidates[:4] + [candidates[5]]
    fetched_urls = [call.args[0] for call in mock_fetch.call_args_list]
    expected_urls = [selected["link"]] + [
        candidate["link"] for candidate in candidates[:7]
    ]
    assert len(fetched_urls) == len(expected_urls)
    assert set(fetched_urls) == set(expected_urls)


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_does_not_wait_for_optional_tail_after_max_filled(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-optional-tail.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-TAIL{:02d}".format(index),
            "https://idx/fallback-optional-tail-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 8)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        )
    }
    for index, candidate in enumerate(candidates, start=1):
        if index == 5:
            manifests[candidate["link"]] = _manifest(
                "video", "different matrix remux.mkv", 90000000000, "miss-5"
            )
        else:
            manifests[candidate["link"]] = _manifest(
                "video",
                "the matrix 1999 remux.mkv",
                60000000000,
                "tail-{}".format(index),
            )

    slow_started = threading.Event()
    release_slow = threading.Event()

    tail_completed = [False]

    def fetch(url, **_kwargs):
        if url == candidates[6]["link"]:
            slow_started.set()
            # No self-timeout: the optional-tail fetch stays in flight until the
            # finally below releases it (after the snapshot), so it cannot complete
            # before we record whether the scan waited for it. The generous 3s only
            # bounds a regression where the scan blocks on this fetch (then it
            # completes -> tail_completed -> caught below).
            release_slow.wait(timeout=3)
            tail_completed[0] = True
        return manifests[url]

    mock_fetch.side_effect = fetch
    try:
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)
        tail_completed_at_return = tail_completed[0]
    finally:
        release_slow.set()

    # Unlike the other optional-tail tests, the in-flight tail here
    # (candidates[6]) is beyond max_candidates, so the rolling window submits it
    # only AFTER the selected manifest is ready -- it cannot be gated on the
    # selected fetch without deadlocking the scan (Codex P2). Confirm it started
    # with a generous 10s bound (a normally scheduled daemon thread starts in
    # ms); the not-completed-at-return snapshot below is the real structural
    # proof and does not depend on this.
    assert slow_started.wait(10)
    # Structural proof (load-independent): with max_candidates already filled,
    # the scan must return before the optional-tail fetch (candidates[6]) is
    # released (only the finally above releases it, after the snapshot). If it
    # wrongly waited, that fetch would complete -> tail_completed_at_return True.
    assert tail_completed_at_return is False
    assert selected["_fallback_candidates"] == candidates[:4] + [candidates[5]]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_does_not_wait_for_optional_tail_after_partial_match(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-partial-tail.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-PARTIAL{:02d}".format(
                index
            ),
            "https://idx/fallback-partial-tail-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 4)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        candidates[0]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "match-1"
        ),
        candidates[1]["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "slow-match-2"
        ),
        candidates[2]["link"]: _manifest(
            "video", "different matrix remux.mkv", 90000000000, "miss-3"
        ),
    }
    slow_started = threading.Event()
    release_slow = threading.Event()
    slow_completed = threading.Event()

    def fetch(url, **_kwargs):
        if url == candidates[1]["link"]:
            slow_started.set()
            # No self-timeout: the slow optional-tail candidate stays in flight
            # until the finally below releases it (after the snapshot), so it can
            # never complete before we record whether the scan waited for it. The
            # generous 3s only bounds a regression where the scan blocks on this
            # fetch (then it completes -> slow_completed -> caught below).
            release_slow.wait(timeout=3)
            slow_completed.set()
        elif url == selected["link"]:
            # Load-independent gate: no candidate is attached, and the scan cannot
            # return, until selected_ready. Block the selected manifest until the
            # slow optional-tail candidate's daemon thread has actually started, so
            # slow_started is provably set before the scan returns -- removing the
            # thread-start race at realistic load. The 30s is a hang-safety bound
            # only (a normally scheduled daemon thread starts in ms; the slow
            # candidate is in the initial window, so it is always submitted).
            slow_started.wait(30)
        return manifests[url]

    mock_fetch.side_effect = fetch
    try:
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)
        slow_completed_at_return = slow_completed.is_set()
    finally:
        release_slow.set()

    # Deterministic via the selected-manifest gate above: slow_started is
    # guaranteed set before the scan returns, so this assert is load-independent.
    assert slow_started.is_set()
    # Structural proof (load-independent): the slow optional-tail candidate is
    # left in flight and never released here, so a healthy bounded tail-wait must
    # give up and return WITHOUT it completing. A "waits-but-same-result"
    # regression (drain the slow in-flight fetch, then discard it) would block
    # until it completed -> slow_completed set.
    assert not slow_completed_at_return
    # The list assertion confirms the slow optional tail was not attached.
    assert selected["_fallback_candidates"] == [candidates[0]]
    # Premise pin (load-independent): the structural slow_completed guard proves
    # the scan does not BLOCK on the optional tail, but it cannot see drift in the
    # bounded wait the scan grants before giving up. Widening that wait (e.g. to
    # 0.2s) would keep this test green yet slow every real partial-match path.
    # Pin the documented optional-tail wait so such a drift goes red here.
    from resources.lib import fallback_streams

    assert fallback_streams._FALLBACK_MANIFEST_OPTIONAL_TAIL_WAIT_SECONDS == 0.1


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_stops_prefilter_scan_after_max_attached_candidates(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 3)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALTx{:02d}".format(index),
            "https://idx/fallback-extra-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 9)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        )
    }
    for index, candidate in enumerate(candidates, start=1):
        manifests[candidate["link"]] = _manifest(
            "video",
            "the matrix 1999 remux.mkv",
            60000000000,
            "fallback-extra-{}".format(index),
        )
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]
    candidate_title_token_calls = []
    original_title_tokens = fallback_streams._title_tokens

    def counted_title_tokens(result):
        if result is not selected:
            candidate_title_token_calls.append(result)
        return original_title_tokens(result)

    with patch(
        "resources.lib.fallback_streams._title_tokens", side_effect=counted_title_tokens
    ):
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)

    assert selected["_fallback_candidates"] == candidates[:3]
    assert candidate_title_token_calls == candidates[:3]
    fetched_urls = [call.args[0] for call in mock_fetch.call_args_list]
    assert fetched_urls[0] == "https://idx/selected.nzb"
    assert set(fetched_urls[1:]) == {
        "https://idx/fallback-extra-1.nzb",
        "https://idx/fallback-extra-2.nzb",
        "https://idx/fallback-extra-3.nzb",
    }


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_reuses_prefilter_match_for_manifest_gate(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 3)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALTx{:02d}".format(index),
            "https://idx/fallback-{}.nzb".format(index),
            60000000000,
            meta=selected["_meta"],
        )
        for index in range(1, 7)
    ]
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        )
    }
    for index, candidate in enumerate(candidates, start=1):
        manifests[candidate["link"]] = _manifest(
            "video",
            "the matrix 1999 remux.mkv",
            60000000000,
            "fallback-{}".format(index),
        )
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]
    title_token_calls = []
    original_title_tokens = fallback_streams._title_tokens

    def counted_title_tokens(result):
        title_token_calls.append(result)
        return original_title_tokens(result)

    with patch(
        "resources.lib.fallback_streams._title_tokens", side_effect=counted_title_tokens
    ):
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)

    assert selected["_fallback_candidates"] == candidates[:3]
    assert len(title_token_calls) == 1 + 3


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_reuses_known_prefetch_peer_profile_match(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 1)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    related = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/related.nzb",
        60000000000,
        meta=selected["_meta"],
    )
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        related["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "related"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]
    original_profiles_match = fallback_streams._metadata_profiles_match
    profile_match_calls = []

    def counted_profiles_match(*args, **kwargs):
        profile_match_calls.append(args[1])
        return original_profiles_match(*args, **kwargs)

    with patch(
        "resources.lib.fallback_streams._metadata_profiles_match",
        side_effect=counted_profiles_match,
    ):
        peer = first_prefetchable_fallback_peer(selected, [selected, related])
        assert peer is related
        attach_fallback_candidates_for_selection(selected, [selected, related])

    assert selected["_fallback_candidates"] == [related]
    assert profile_match_calls == [related]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_rar_only_manifests_are_grouped_provisionally_for_runtime_validation(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    primary = _result("Movie", "https://idx/a.nzb", 1)
    archive_fallback = _result("Movie", "https://idx/b.nzb", 2)
    manifests = {
        "https://idx/a.nzb": _manifest("archive", "movie", 0, "articles-a"),
        "https://idx/b.nzb": _manifest("archive", "movie", 0, "articles-b"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, archive_fallback])

    assert primary["_fallback_candidates"] == [archive_fallback]
    assert archive_fallback["_fallback_candidates"] == [primary]


@patch("resources.lib.nzb_manifest.http_get")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_attach_fallbacks_skips_unhealthy_manifest_file_candidates(
    mock_settings, mock_http_get
):
    mock_settings.return_value = (True, 5)
    primary = _result("Movie primary", "https://idx/a.nzb", 1)
    fallback = _result("Movie fallback", "https://idx/b.nzb", 2)
    bodies = {
        "https://idx/a.nzb": _nzb_xml(
            [
                _nzb_file(
                    '"Broken.Primary.mkv" yEnc (1/1)',
                    [(1, 10000, "malformed id")],
                ),
                _nzb_file('"Movie.mkv" yEnc (1/1)', [(1, 8000, "good-a@id")]),
            ]
        ),
        "https://idx/b.nzb": _nzb_xml(
            [
                _nzb_file(
                    '"Broken.Fallback.mkv" yEnc (1/1)',
                    [(1, 10000, "also malformed")],
                ),
                _nzb_file('"Movie.mkv" yEnc (1/1)', [(1, 8000, "good-b@id")]),
            ]
        ),
    }
    mock_http_get.side_effect = lambda url, **_kwargs: bodies[url].decode("utf-8")

    attach_fallback_candidates([primary, fallback])

    assert primary["_fallback_manifest"]["video_name"] == "Movie.mkv"
    assert fallback["_fallback_manifest"]["video_name"] == "Movie.mkv"
    assert primary["_fallback_manifest"]["skipped_candidate_count"] == 1
    assert fallback["_fallback_manifest"]["skipped_candidate_count"] == 1
    assert primary["_fallback_candidates"] == [fallback]
    assert fallback["_fallback_candidates"] == [primary]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_manifest_fetches_are_cached_per_attach_call(mock_settings, mock_fetch):
    mock_settings.return_value = (True, 5)
    first = _result("Movie", "https://idx/same.nzb", 1)
    second = _result("Movie repost", "https://idx/repost.nzb", 2)
    duplicate = _result("Movie duplicate", "https://idx/same.nzb", 3)
    manifests = {
        "https://idx/same.nzb": _manifest("video", "movie.mkv", 1000, "same-articles"),
        "https://idx/repost.nzb": _manifest(
            "video", "movie.mkv", 1000, "repost-articles"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([first, second, duplicate])

    assert [(call.args[0], call.kwargs) for call in mock_fetch.call_args_list] == [
        ("https://idx/same.nzb", {"health_check": ANY}),
        ("https://idx/repost.nzb", {"health_check": ANY}),
    ]
    assert first["_fallback_manifest_error"] == ""
    assert second["_fallback_manifest_error"] == ""
    assert duplicate["_fallback_manifest_error"] == ""


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_manifest_fetches_are_cached_across_short_lived_selection_calls(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    fallback_streams.clear_fallback_manifest_cache()
    mock_settings.return_value = (True, 1)
    now = [100.0]
    manifests = {
        "https://idx/selected-cache.nzb": _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        "https://idx/related-cache.nzb": _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "related"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    def result_pair():
        selected = _result(
            "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
            "https://idx/selected-cache.nzb",
            60000000000,
            meta={
                "resolution": "2160p",
                "quality": "REMUX",
                "codec": "x265/HEVC",
                "hdr": ["Dolby Vision"],
                "audio": ["TrueHD", "Atmos"],
                "group": "GROUP",
                "container": "mkv",
            },
        )
        related = _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
            "https://idx/related-cache.nzb",
            60000000000,
            meta=selected["_meta"],
        )
        return selected, related

    with patch(
        "resources.lib.fallback_streams._fallback_manifest_cache_now",
        side_effect=lambda: now[0],
    ):
        first_selected, first_related = result_pair()
        attach_fallback_candidates_for_selection(
            first_selected, [first_selected, first_related]
        )
        second_selected, second_related = result_pair()
        attach_fallback_candidates_for_selection(
            second_selected, [second_selected, second_related]
        )

    assert first_selected["_fallback_candidates"] == [first_related]
    assert second_selected["_fallback_candidates"] == [second_related]
    first_manifest = first_selected["_fallback_manifest"]
    second_manifest = second_selected["_fallback_manifest"]
    first_related_manifest = first_related["_fallback_manifest"]
    second_related_manifest = second_related["_fallback_manifest"]
    assert first_manifest == second_manifest
    assert first_manifest is not second_manifest
    assert first_related_manifest == second_related_manifest
    assert first_related_manifest is not second_related_manifest
    first_manifest["skipped_candidates"].append({"subject": "mutated"})
    first_related_manifest["skipped_candidates"].append({"subject": "mutated"})
    assert second_manifest["skipped_candidates"] == []
    assert second_related_manifest["skipped_candidates"] == []
    assert sorted(call.args[0] for call in mock_fetch.call_args_list) == [
        "https://idx/related-cache.nzb",
        "https://idx/selected-cache.nzb",
    ]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_short_lived_manifest_cache_expires_between_selection_calls(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    fallback_streams.clear_fallback_manifest_cache()
    mock_settings.return_value = (True, 1)
    now = [100.0]
    manifests = {
        "https://idx/selected-expire.nzb": _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        "https://idx/related-expire.nzb": _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "related"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    def result_pair():
        selected = _result(
            "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
            "https://idx/selected-expire.nzb",
            60000000000,
            meta={
                "resolution": "2160p",
                "quality": "REMUX",
                "codec": "x265/HEVC",
                "hdr": ["Dolby Vision"],
                "audio": ["TrueHD", "Atmos"],
                "group": "GROUP",
                "container": "mkv",
            },
        )
        related = _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
            "https://idx/related-expire.nzb",
            60000000000,
            meta=selected["_meta"],
        )
        return selected, related

    with patch(
        "resources.lib.fallback_streams._fallback_manifest_cache_now",
        side_effect=lambda: now[0],
    ):
        first_selected, first_related = result_pair()
        attach_fallback_candidates_for_selection(
            first_selected, [first_selected, first_related]
        )
        now[0] += fallback_streams._FALLBACK_MANIFEST_CACHE_TTL_SECONDS + 0.01
        second_selected, second_related = result_pair()
        attach_fallback_candidates_for_selection(
            second_selected, [second_selected, second_related]
        )

    assert first_selected["_fallback_candidates"] == [first_related]
    assert second_selected["_fallback_candidates"] == [second_related]
    assert len(mock_fetch.call_args_list) == 4


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
def test_manifest_cache_ttl_starts_after_fetch_completes(mock_fetch):
    from resources.lib import fallback_streams

    fallback_streams.clear_fallback_manifest_cache()
    now = [100.0]
    manifest = _manifest("video", "movie.mkv", 1000, "articles")

    def fetch_manifest(_url, **_kwargs):
        now[0] += fallback_streams._FALLBACK_MANIFEST_CACHE_TTL_SECONDS - 1
        return manifest

    mock_fetch.side_effect = fetch_manifest

    with patch(
        "resources.lib.fallback_streams._fallback_manifest_cache_now",
        side_effect=lambda: now[0],
    ):
        first = fallback_streams._fetch_fallback_manifest("https://idx/slow.nzb")
        now[0] += fallback_streams._FALLBACK_MANIFEST_CACHE_TTL_SECONDS - 0.5
        second = fallback_streams._fetch_fallback_manifest("https://idx/slow.nzb")

    assert first == second
    assert mock_fetch.call_count == 1


@pytest.mark.parametrize(
    "fetch_result",
    [
        RuntimeError("fetch failed"),
        None,
    ],
)
@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
def test_manifest_cache_reuses_normalized_fetch_errors(mock_fetch, fetch_result):
    from resources.lib import fallback_streams

    fallback_streams.clear_fallback_manifest_cache()
    if isinstance(fetch_result, Exception):
        mock_fetch.side_effect = fetch_result
    else:
        mock_fetch.return_value = fetch_result

    first = fallback_streams._fetch_fallback_manifest("https://idx/error.nzb")
    second = fallback_streams._fetch_fallback_manifest("https://idx/error.nzb")

    assert first == make_empty_manifest("fetch_error")
    assert second == first
    assert second is not first
    assert mock_fetch.call_count == 1


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_attach_fallbacks_reuses_known_prefetch_peer_profile_match(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 1)
    selected = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/selected-full-list.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "audio": ["TrueHD", "Atmos"],
            "group": "GROUP",
            "container": "mkv",
        },
    )
    related = _result(
        "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT",
        "https://idx/related-full-list.nzb",
        60000000000,
        meta=selected["_meta"],
    )
    manifests = {
        selected["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "selected"
        ),
        related["link"]: _manifest(
            "video", "the matrix 1999 remux.mkv", 60000000000, "related"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]
    original_profiles_match = fallback_streams._metadata_profiles_match
    profile_match_calls = []

    def counted_profiles_match(*args, **kwargs):
        profile_match_calls.append(args[1])
        return original_profiles_match(*args, **kwargs)

    with patch(
        "resources.lib.fallback_streams._metadata_profiles_match",
        side_effect=counted_profiles_match,
    ):
        attach_fallback_candidates([selected, related])

    assert selected["_fallback_candidates"] == [related]
    assert related["_fallback_candidates"] == [selected]
    assert profile_match_calls == [related, selected]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_selection_fallback_rejects_stale_prefetch_proof_before_signature_work(
    mock_settings, mock_fetch
):
    from resources.lib import fallback_streams

    mock_settings.return_value = (True, 5)
    previous = _result(
        "Previous.Movie.2026.2160p.UHD.BluRay.REMUX-GROUP",
        "https://idx/previous.nzb",
        60000000000,
        meta={
            "resolution": "2160p",
            "quality": "REMUX",
            "codec": "x265/HEVC",
            "container": "mkv",
        },
    )
    selected = _result(
        "Current.Movie.2026.2160p.UHD.BluRay.REMUX-GROUP",
        "https://idx/current.nzb",
        60000000000,
        meta=previous["_meta"],
    )
    stale_candidate = _result(
        "Different.Movie.2026.1080p.WEB-DL-GROUP",
        "https://idx/stale-candidate.nzb",
        5000000000,
        meta={
            "resolution": "1080p",
            "quality": "WEB-DL",
            "codec": "x265/HEVC",
            "container": "mkv",
        },
    )
    fallback_streams._remember_prefetch_gate_match(
        previous,
        stale_candidate,
        previous["_meta"],
        stale_candidate["_meta"],
    )
    signature_calls = []
    original_signature = fallback_streams._metadata_profile_signature

    def counted_signature(meta):
        signature_calls.append(meta)
        return original_signature(meta)

    with patch(
        "resources.lib.fallback_streams._metadata_profile_signature",
        side_effect=counted_signature,
    ):
        attach_fallback_candidates_for_selection(selected, [selected, stale_candidate])

    assert selected["_fallback_candidates"] == []
    assert not signature_calls
    mock_fetch.assert_not_called()


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_manifest_fetch_exception_marks_one_result_failed_without_raising(
    mock_settings, mock_fetch
):
    mock_settings.return_value = (True, 5)
    broken = _result("Movie broken", "https://idx/broken.nzb", 1)
    working = _result("Movie working", "https://idx/working.nzb", 2)

    def fetch(url, **_kwargs):
        if url.endswith("broken.nzb"):
            raise RuntimeError("message id failure")
        return _manifest("video", "movie.mkv", 1000, "articles-working")

    mock_fetch.side_effect = fetch

    attach_fallback_candidates([broken, working])

    assert broken["_fallback_manifest_error"] == "fetch_error"
    assert broken["_fallback_candidates"] == []
    assert working["_fallback_manifest_error"] == ""
    assert working["_fallback_candidates"] == []


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
    assert first.endswith("[fallback-1-91fffc91]")
    assert second.endswith("[fallback-2-db3a3f35]")
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


def test_build_prepare_fallback_payload_preserves_episode_context():
    context = {
        "type": "episode",
        "title": "Spider-Noir",
        "imdb": "tt1234567",
        "tvdb": "451234",
        "tmdb_id": "987",
        "season": 1,
        "episode": 1,
    }
    payload = build_prepare_fallback_payload(
        [{"title": "pack", "nzo_id": "nzo-pack", "episode_context": context}]
    )

    assert payload[0]["episode_context"] == context


def test_fingerprint_ranges_uses_100_deterministic_4096_byte_samples_for_large_files():
    """Fingerprint count was raised 20 → 100 to give the cutover a denser
    byte-equivalence proof per fallback (still 4096 bytes per range)."""
    content_length = 10 * 1024 * 1024 * 1024

    ranges = fingerprint_ranges(content_length)

    assert len(ranges) == 100
    assert len(set(ranges)) == 100
    assert ranges == fingerprint_ranges(content_length)
    assert ranges[0] == (0, 4095)
    assert ranges[-1] == (content_length - 4096, content_length - 1)
    assert all((end - start + 1) == 4096 for start, end in ranges)


def test_fingerprint_ranges_handles_small_files():
    assert fingerprint_ranges(1024) == [(0, 1023)]


def test_fingerprint_ranges_chunks_whole_file_when_smaller_than_sample_budget():
    assert fingerprint_ranges(5000) == [(0, 4095), (4096, 4999)]


@patch("resources.lib.fallback_streams.urlopen", side_effect=URLError("timeout"))
def test_fetch_range_digest_returns_none_on_probe_error(_mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert (
            fetch_range_digest("http://webdav/content/movie.mkv", None, 0, 1023) is None
        )


@patch("resources.lib.fallback_streams.urlopen", side_effect=URLError("out-of-bounds"))
def test_fetch_range_digest_rejects_out_of_bounds_range_before_probe(mock_urlopen):
    probe_bases = (urlsplit("http://webdav/content"),)

    assert (
        fetch_range_digest(
            "http://webdav/content/movie.mkv",
            None,
            1000,
            1005,
            content_length=1000,
            probe_bases=probe_bases,
        )
        is None
    )

    mock_urlopen.assert_not_called()


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_rejects_non_http_urls(mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_range_digest("file:///etc/passwd", None, 0, 3) is None
    mock_urlopen.assert_not_called()


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_rejects_off_origin_urls(mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert (
            fetch_range_digest("http://evil.test/content/movie.mkv", None, 0, 3) is None
        )
    mock_urlopen.assert_not_called()


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_content_length_rejects_off_origin_urls(mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_content_length("http://evil.test/content/movie.mkv", None) == 0
    mock_urlopen.assert_not_called()


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_rejects_configured_host_outside_content_root(mock_urlopen):
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_range_digest("http://webdav/private/movie.mkv", None, 0, 3) is None
    mock_urlopen.assert_not_called()


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_content_length_accepts_configured_stream_url(mock_urlopen):
    mock_urlopen.return_value = _mock_range_response(
        b"",
        headers={"Content-Length": "1234"},
    )

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_content_length("http://webdav/content/movie.mkv", None) == 1234

    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://webdav/content/movie.mkv"
    assert mock_urlopen.call_args.kwargs["timeout"] == 10


def test_fetch_content_length_reuses_validated_probe_url_for_precomputed_bases():
    from resources.lib import fallback_streams

    url = "http://webdav/content/movie.mkv"
    response = _mock_range_response(
        b"",
        status=200,
        headers={"Content-Length": "1234"},
    )
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        probe_bases = fallback_streams.configured_stream_probe_bases()

    validation_urls = []
    original_validate = fallback_streams._validated_probe_url
    if hasattr(fallback_streams._cached_validated_probe_url, "cache_clear"):
        fallback_streams._cached_validated_probe_url.cache_clear()

    def counted_validate(url, probe_bases=None):
        validation_urls.append(url)
        return original_validate(url, probe_bases=probe_bases)

    with patch("resources.lib.fallback_streams.urlopen", return_value=response) as (
        mock_urlopen
    ), patch(
        "resources.lib.fallback_streams._validated_probe_url",
        side_effect=counted_validate,
    ):
        assert [
            fetch_content_length(url, None, probe_bases=probe_bases) for _ in range(3)
        ] == [1234, 1234, 1234]

    if hasattr(fallback_streams._cached_validated_probe_url, "cache_clear"):
        fallback_streams._cached_validated_probe_url.cache_clear()
    assert mock_urlopen.call_count == 3
    assert validation_urls == [url]


def test_precomputed_probe_bases_reuse_base_origin_checks_for_range_digest():
    from resources.lib import fallback_streams

    # The module-level @lru_cache on _cached_validated_probe_url leaks across
    # tests; clear it so this assertion is independent of test execution order.
    if hasattr(fallback_streams._cached_validated_probe_url, "cache_clear"):
        fallback_streams._cached_validated_probe_url.cache_clear()

    body = b"A" * 4
    response = _mock_range_response(
        body,
        status=206,
        headers={"Content-Range": "bytes 0-3/10"},
    )
    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        probe_bases = fallback_streams.configured_stream_probe_bases()

    origin_calls = []
    original_origin_key = fallback_streams._origin_key

    def counted_origin_key(parts):
        origin_calls.append(parts.geturl())
        return original_origin_key(parts)

    with patch("resources.lib.fallback_streams.urlopen", return_value=response), patch(
        "resources.lib.fallback_streams._origin_key", side_effect=counted_origin_key
    ):
        assert fetch_range_digest(
            "http://webdav/content/movie.mkv",
            None,
            0,
            3,
            content_length=10,
            probe_bases=probe_bases,
        )

    assert origin_calls == ["http://webdav/content/movie.mkv"]


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_rejects_server_that_ignores_range(mock_urlopen):
    mock_urlopen.return_value = _mock_range_response(b"A" * 4, status=200)

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_range_digest("http://webdav/content/movie.mkv", None, 0, 3) is None


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_requires_matching_content_range(mock_urlopen):
    mock_urlopen.return_value = _mock_range_response(
        b"A" * 4,
        status=206,
        headers={"Content-Range": "bytes 4-7/10"},
    )

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert fetch_range_digest("http://webdav/content/movie.mkv", None, 0, 3) is None


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_requires_matching_content_range_total(mock_urlopen):
    mock_urlopen.return_value = _mock_range_response(
        b"A" * 4,
        status=206,
        headers={"Content-Range": "bytes 0-3/11"},
    )

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert (
            fetch_range_digest(
                "http://webdav/content/movie.mkv", None, 0, 3, content_length=10
            )
            is None
        )


@patch("resources.lib.fallback_streams.urlopen")
def test_fetch_range_digest_accepts_matching_partial_content(mock_urlopen):
    body = b"A" * 4
    mock_urlopen.return_value = _mock_range_response(
        body,
        status=206,
        headers={"Content-Range": "bytes 0-3/10"},
    )

    with patch(
        "resources.lib.fallback_streams.xbmcaddon.Addon.return_value.getSetting",
        side_effect=_fallback_setting,
    ):
        assert (
            fetch_range_digest(
                "http://webdav/content/movie.mkv", None, 0, 3, content_length=10
            )
            == "63c1dd951ffedf6f7fd968ad4efa39b8ed584f162f46e715114ee184f8de9201"
        )
    assert mock_urlopen.call_args.kwargs["timeout"] == 10


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_video_manifest_peer_match_accepts_size_within_10_percent_tolerance(
    mock_settings, mock_fetch
):
    """Different uploads of the same source MKV use different yEnc segment sizes,
    so two video manifests for the same release will report different group_bytes.
    Accept matches when the bytes are within the +/-10% Tier-1 band as long as the
    content-identity gate already passed. (Tightened from the old +/-20% band so a
    differently-encoded release can no longer slip through on size alone.)
    """
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/primary.nzb",
        95000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    repost_within_tolerance = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/repost.nzb",
        103000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    manifests = {
        "https://idx/primary.nzb": _manifest(
            "video", "once upon a time framestor.mkv", 95000000000, "articles-a"
        ),
        "https://idx/repost.nzb": _manifest(
            "video", "once upon a time alt repost.mkv", 103000000000, "articles-b"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, repost_within_tolerance])

    assert primary["_fallback_candidates"] == [repost_within_tolerance]
    assert repost_within_tolerance["_fallback_candidates"] == [primary]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_video_manifest_peer_match_rejects_size_outside_20_percent_tolerance(
    mock_settings, mock_fetch
):
    """A larger gap probably reflects different audio/video tracks, not just
    different yEnc segmentation. Stay conservative outside the tolerance band.
    """
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/primary.nzb",
        95000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    repost_outside_tolerance = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/different.nzb",
        130000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    manifests = {
        "https://idx/primary.nzb": _manifest(
            "video", "once upon a time framestor.mkv", 95000000000, "articles-a"
        ),
        "https://idx/different.nzb": _manifest(
            "video", "once upon a time bigger.mkv", 130000000000, "articles-b"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, repost_outside_tolerance])

    assert primary["_fallback_candidates"] == []
    assert repost_outside_tolerance["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_archive_peer_matches_video_peer_within_20_percent_tolerance(
    mock_settings, mock_fetch
):
    """A direct-MKV upload and a RAR upload of the same release should peer
    when the manifest group_bytes are within +/-20%, even though their kinds
    differ (video vs archive). Title and profile gates upstream still bound
    the candidate set.
    """
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/primary.nzb",
        87000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    video_repost_within_tolerance = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/repost.nzb",
        87500000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    manifests = {
        "https://idx/primary.nzb": _manifest(
            "archive", "once upon a time framestor", 87000000000, "articles-a"
        ),
        "https://idx/repost.nzb": _manifest(
            "video", "once upon a time framestor.mkv", 87500000000, "articles-b"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, video_repost_within_tolerance])

    assert primary["_fallback_candidates"] == [video_repost_within_tolerance]
    assert video_repost_within_tolerance["_fallback_candidates"] == [primary]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_archive_peer_does_not_match_video_peer_outside_20_percent(
    mock_settings, mock_fetch
):
    """An archive RAR for one release should not peer with a video MKV whose
    group_bytes are more than 20% off, even when titles and profiles agree.
    A 67% gap (e.g., Theatrical-UHD vs Extended-UHD) reflects different
    runtime, not yEnc segmentation noise.
    """
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/primary.nzb",
        87000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    video_outside_tolerance = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/different.nzb",
        137000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    manifests = {
        "https://idx/primary.nzb": _manifest(
            "archive", "once upon a time framestor", 87000000000, "articles-a"
        ),
        "https://idx/different.nzb": _manifest(
            "video",
            "once upon a time framestor extended.mkv",
            137000000000,
            "articles-b",
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, video_outside_tolerance])

    assert primary["_fallback_candidates"] == []
    assert video_outside_tolerance["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_archive_peer_matches_archive_peer_within_20_percent_tolerance(
    mock_settings, mock_fetch
):
    """Two archive RAR uploads of the same release should peer when their
    manifest group_bytes are within +/-20%. yEnc segmentation noise across
    different uploads still produces small variance even for the same source.
    """
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/primary.nzb",
        82000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    archive_repost_within_tolerance = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/repost.nzb",
        90000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    manifests = {
        "https://idx/primary.nzb": _manifest(
            "archive", "once upon a time framestor", 82000000000, "articles-a"
        ),
        "https://idx/repost.nzb": _manifest(
            "archive", "once upon a time framestor alt", 90000000000, "articles-b"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, archive_repost_within_tolerance])

    assert primary["_fallback_candidates"] == [archive_repost_within_tolerance]
    assert archive_repost_within_tolerance["_fallback_candidates"] == [primary]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_archive_peer_rejects_archive_peer_outside_20_percent_tolerance(
    mock_settings, mock_fetch
):
    """Two archive RAR uploads with very different group_bytes should not
    peer. Previously archive-vs-archive returned True unconditionally, so a
    Theatrical-UHD RAR (~82G) could peer with an Extended-UHD RAR (~137G)
    despite the 67% gap. Apply the same +/-20% tolerance as video peers.
    """
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/primary.nzb",
        82000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    archive_outside_tolerance = _result(
        "Once.Upon.a.Time.in.the.West.1968.PROPER.UHD.BluRay.2160p.DTS-HD.MA.5.1.DV.HEVC.HYBRID.REMUX-FraMeSToR",
        "https://idx/extended.nzb",
        137000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    manifests = {
        "https://idx/primary.nzb": _manifest(
            "archive", "once upon a time framestor", 82000000000, "articles-a"
        ),
        "https://idx/extended.nzb": _manifest(
            "archive", "once upon a time framestor extended", 137000000000, "articles-b"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, archive_outside_tolerance])

    assert primary["_fallback_candidates"] == []
    assert archive_outside_tolerance["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_archive_peers_with_shared_archive_base_match_via_group_key_short_circuit(
    mock_settings, mock_fetch
):
    """Archive manifests that share a non-empty archive_base produce identical
    group keys (archive group keys exclude bytes by design), so they peer via
    the early-return short-circuit even when their group_bytes diverge widely.
    Pin that contract so a future change to the size gate does not silently
    drop legitimate same-archive-base peers.
    """
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Once.Upon.a.Time.in.the.West.1968.UHD.BluRay.2160p.DV.HEVC.REMUX-FraMeSToR",
        "https://idx/primary-shared-base.nzb",
        95000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    repost_shared_base = _result(
        "Once.Upon.a.Time.in.the.West.1968.UHD.BluRay.2160p.DV.HEVC.REMUX-FraMeSToR",
        "https://idx/shared-base-repost.nzb",
        140000000000,
        meta={
            "resolution": "2160p",
            "quality": "BluRay REMUX",
            "codec": "x265/HEVC",
            "hdr": ["Dolby Vision"],
            "group": "FraMeSToR",
            "container": "mkv",
        },
    )
    manifests = {
        "https://idx/primary-shared-base.nzb": _manifest(
            "archive", "shared archive base", 80000000000, "articles-a"
        ),
        "https://idx/shared-base-repost.nzb": _manifest(
            "archive", "shared archive base", 140000000000, "articles-b"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, repost_shared_base])

    assert primary["_fallback_candidates"] == [repost_shared_base]
    assert repost_shared_base["_fallback_candidates"] == [primary]


# ---------------------------------------------------------------------------
# Tiered content-identity fallback (F1/F2/F3)
# ---------------------------------------------------------------------------


def _movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"):
    return {
        "resolution": resolution,
        "quality": "BluRay REMUX",
        "codec": codec,
        "hdr": ["Dolby Vision"],
        "group": group,
        "container": "mkv",
    }


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_content_identity_gate_rejects_different_movie_part(mock_settings, mock_fetch):
    """Dune Part Two must never fall back to Dune Part One even though the
    release tokens (dune, part, year, profile) overlap heavily."""
    mock_settings.return_value = (True, 5)
    part_two = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/part-two.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    part_one = _result(
        "Dune.Part.One.2021.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/part-one.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    manifests = {
        "https://idx/part-two.nzb": _manifest(
            "video", "dune two.mkv", 60000000000, "a"
        ),
        "https://idx/part-one.nzb": _manifest(
            "video", "dune one.mkv", 60000000000, "b"
        ),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([part_two, part_one])

    assert part_two["_fallback_candidates"] == []
    assert part_one["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_content_identity_gate_rejects_different_movie_year(mock_settings, mock_fetch):
    """Same title, different year (Avatar 2009 vs 2022) is different content."""
    mock_settings.return_value = (True, 5)
    old = _result(
        "Avatar.2009.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/avatar-2009.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    new = _result(
        "Avatar.2022.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/avatar-2022.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    manifests = {
        "https://idx/avatar-2009.nzb": _manifest("video", "a.mkv", 60000000000, "a"),
        "https://idx/avatar-2022.nzb": _manifest("video", "b.mkv", 60000000000, "b"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([old, new])

    assert old["_fallback_candidates"] == []
    assert new["_fallback_candidates"] == []


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_content_identity_gate_rejects_different_episode(mock_settings, mock_fetch):
    """S02E05 must not fall back to S02E06 or S01E05."""
    mock_settings.return_value = (True, 5)
    e05 = _result(
        "Show.Name.S02E05.2160p.WEB-DL.x265-GROUP",
        "https://idx/e05.nzb",
        6000000000,
        meta=_movie_meta(),
    )
    e06 = _result(
        "Show.Name.S02E06.2160p.WEB-DL.x265-GROUP",
        "https://idx/e06.nzb",
        6000000000,
        meta=_movie_meta(),
    )
    s01 = _result(
        "Show.Name.S01E05.2160p.WEB-DL.x265-GROUP",
        "https://idx/s01e05.nzb",
        6000000000,
        meta=_movie_meta(),
    )
    manifests = {
        "https://idx/e05.nzb": _manifest("video", "e05.mkv", 6000000000, "a"),
        "https://idx/e06.nzb": _manifest("video", "e06.mkv", 6000000000, "b"),
        "https://idx/s01e05.nzb": _manifest("video", "s1e5.mkv", 6000000000, "c"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([e05, e06, s01])

    assert e05["_fallback_candidates"] == []
    assert e06["_fallback_candidates"] == []
    assert s01["_fallback_candidates"] == []


def test_same_content_rejects_part_vs_bare_movie():
    """FS-1: an explicit-part release is different content from the bare title.

    PTT keeps the part word in the title, so "dune part two" != "dune" and the
    old `primary_title == candidate_title` guard never fired. A sequel must not
    peer with the bare original even when the sequel omits its year.
    """
    from resources.lib import fallback_streams as fs

    part_two = _result(
        "Dune.Part.Two.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/part-two.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    bare = _result(
        "Dune.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/bare.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    assert fs._same_content(part_two, bare) is False
    assert fs._same_content(bare, part_two) is False


def test_same_content_rejects_part_one_vs_part_two():
    """FS-1: differing explicit parts stay different content."""
    from resources.lib import fallback_streams as fs

    part_two = _result(
        "Dune.Part.Two.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/part-two.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    part_one = _result(
        "Dune.Part.One.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/part-one.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    assert fs._same_content(part_two, part_one) is False


def test_normalize_title_collapses_conjunction_spellings():
    """ "&", the literal word "and", and an omitted conjunction are one identity.

    "Your Friends & Neighbors", "Your.Friends.and.Neighbors", and
    "Your.Friends.Neighbors" all name the same work; normalization must
    collapse all three spellings to a single token sequence so they peer.
    """
    from resources.lib import fallback_streams as fs

    amp = fs._normalize_title("Your Friends & Neighbors")
    andd = fs._normalize_title("Your Friends and Neighbors")
    omitted = fs._normalize_title("Your Friends Neighbors")

    assert amp == andd == omitted == "your friends neighbors"


def test_normalize_title_preserves_part_ordinals():
    """REGRESSION GUARD: dropping "and" must not weaken part/chapter discrimination.

    The conjunction collapse strips only a standalone "and"; ordinal words that
    distinguish "Part One" from "Part Two" must survive intact, and a substring
    like "and" inside a real word (e.g. "Andromeda") must not be touched.
    """
    from resources.lib import fallback_streams as fs

    assert fs._normalize_title("Dune Part One") == "dune part one"
    assert fs._normalize_title("Dune Part Two") == "dune part two"
    assert fs._normalize_title("Dune Part One") != fs._normalize_title("Dune Part Two")
    assert fs._normalize_title("Andromeda") == "andromeda"


def test_same_content_peers_conjunction_variants():
    """A yearless/episode-less title peers across "&", "and", and omitted forms.

    Without a year or episode to corroborate identity, the title comparison must
    stand on its own. Before the conjunction collapse, only "&"-vs-omitted
    peered; the surviving literal "and" token diverged, so "and"-vs-omitted and
    "and"-vs-"&" missed legitimate fallback peers.
    """
    from resources.lib import fallback_streams as fs

    amp = _result(
        "Your.Friends.&.Neighbors.1080p.WEB-DL.x264-GROUP",
        "https://idx/amp.nzb",
        6000000000,
    )
    andd = _result(
        "Your.Friends.and.Neighbors.1080p.WEB-DL.x264-GROUP",
        "https://idx/and.nzb",
        6000000000,
    )
    omitted = _result(
        "Your.Friends.Neighbors.1080p.WEB-DL.x264-GROUP",
        "https://idx/omitted.nzb",
        6000000000,
    )

    assert fs._same_content(amp, andd) is True
    assert fs._same_content(andd, amp) is True
    assert fs._same_content(andd, omitted) is True
    assert fs._same_content(omitted, andd) is True
    assert fs._same_content(amp, omitted) is True


def test_normalize_title_collapses_foreign_conjunctions():
    """French "et" and German "und" are conjunctions too, like "and"/"&".

    "Jules et Jim" / "Jules and Jim" / "Jules Jim" name the same work, as do the
    "und" spellings, so every variant must normalize to one shared identity.
    """
    from resources.lib import fallback_streams as fs

    assert (
        fs._normalize_title("Jules et Jim")
        == fs._normalize_title("Jules and Jim")
        == fs._normalize_title("Jules Jim")
        == "jules jim"
    )
    assert (
        fs._normalize_title("Dog Day und Night")
        == fs._normalize_title("Dog Day and Night")
        == fs._normalize_title("Dog Day Night")
        == "dog day night"
    )


def test_normalize_title_collapses_double_escaped_ampersand():
    """A literal "&amp;" (from a double-escaped feed) collapses like a bare "&".

    XML parsing normally decodes "&amp;" to "&", but double-escaped feeds
    ("&amp;amp;") leave the literal entity in the title. It must collapse to the
    same identity as "&", "and", and the omitted form -- not leave a stray
    "amp" token. A real "amp" WORD must be left untouched.
    """
    from resources.lib import fallback_streams as fs

    assert (
        fs._normalize_title("Cats &amp; Dogs")
        == fs._normalize_title("Cats & Dogs")
        == fs._normalize_title("Cats and Dogs")
        == fs._normalize_title("Cats Dogs")
        == "cats dogs"
    )
    # A DOUBLE-escaped entity ("&amp;amp;") must also fully decode, not leave a
    # residual "&amp;" that becomes a stray "amp" token.
    assert fs._normalize_title("Cats &amp;amp; Dogs") == "cats dogs"
    # The "&amp;" rewrite is exact: a genuine "amp" word is not a conjunction.
    assert fs._normalize_title("Marshall Amp Sessions") == "marshall amp sessions"


def test_normalize_title_keeps_leading_conjunction_word():
    """A leading "and"/"et"/"und" is a content word, not a conjunction.

    Conjunction folding must only fire INTERIOR (operand on both sides). A
    leading conjunction is content-bearing -- "And Just Like That" is a
    different work from "Just Like That" -- so it must survive normalization
    rather than collapse the two titles to one identity.
    """
    from resources.lib import fallback_streams as fs

    assert fs._normalize_title("And Just Like That") == "and just like that"
    assert fs._normalize_title("And Just Like That") != fs._normalize_title(
        "Just Like That"
    )


def test_normalize_title_keeps_lone_conjunction_token():
    """A title that is ONLY a conjunction token (e.g. "ET") is never folded away.

    Folding a lone token to an empty title is dangerous: an empty core title
    matches any release in the corroborated paths. Interior-only folding keeps
    boundary tokens, so a non-empty title never normalizes to empty.
    """
    from resources.lib import fallback_streams as fs

    assert fs._normalize_title("ET") == "et"
    assert fs._normalize_title("ET") != ""
    assert fs._normalize_title("ET 1982") == "et 1982"


def test_same_content_rejects_leading_and_vs_bare():
    """REGRESSION GUARD: a leading-"and" title is different content from the bare.

    "And Just Like That" and "Just Like That" are different shows; with no year
    or episode to corroborate, the title gate must reject the pair rather than
    peer them just because the leading "and" was folded away.
    """
    from resources.lib import fallback_streams as fs

    leading = _result(
        "And.Just.Like.That.1080p.WEB-DL.x264-GROUP",
        "https://idx/and-jlt.nzb",
        6000000000,
    )
    bare = _result(
        "Just.Like.That.1080p.WEB-DL.x264-GROUP",
        "https://idx/jlt.nzb",
        6000000000,
    )
    assert fs._same_content(leading, bare) is False
    assert fs._same_content(bare, leading) is False


def test_same_content_rejects_lone_acronym_vs_other_same_year():
    """REGRESSION GUARD: a lone-token title must not fold to empty and match by year.

    "ET" (parsed title "ET") must not peer with an unrelated movie of the same
    year. Folding "et" to an empty title would let the year-corroborated path
    accept any 1982 release.
    """
    from resources.lib import fallback_streams as fs

    et_movie = _result(
        "ET.1982.1080p.BluRay.x264-GROUP",
        "https://idx/et.nzb",
        6000000000,
    )
    other = _result(
        "Blade.Runner.1982.1080p.BluRay.x264-GROUP",
        "https://idx/blade.nzb",
        6000000000,
    )
    assert fs._same_content(et_movie, other) is False
    assert fs._same_content(other, et_movie) is False


def test_normalize_title_only_drops_whole_conjunction_words():
    """REGRESSION GUARD: only standalone conjunction WORDS are dropped.

    A conjunction spelled as a substring of a real word (e.g. "et" in "Planet",
    "und" in "Underworld", "and" in "Andromeda") must survive untouched -- the
    collapse is whole-token only.
    """
    from resources.lib import fallback_streams as fs

    assert fs._normalize_title("Planet of the Apes") == "planet of the apes"
    assert fs._normalize_title("Underworld") == "underworld"
    assert fs._normalize_title("Andromeda") == "andromeda"


def test_same_content_peers_foreign_conjunction_variants():
    """A yearless/episode-less title peers across "et"/"and"/omitted forms."""
    from resources.lib import fallback_streams as fs

    et_form = _result(
        "Jules.et.Jim.1080p.BluRay.x264-GROUP",
        "https://idx/et.nzb",
        6000000000,
    )
    and_form = _result(
        "Jules.and.Jim.1080p.BluRay.x264-GROUP",
        "https://idx/and.nzb",
        6000000000,
    )
    omitted = _result(
        "Jules.Jim.1080p.BluRay.x264-GROUP",
        "https://idx/omitted.nzb",
        6000000000,
    )

    assert fs._same_content(et_form, and_form) is True
    assert fs._same_content(and_form, et_form) is True
    assert fs._same_content(et_form, omitted) is True
    assert fs._same_content(omitted, et_form) is True


def test_same_episode_with_part_token_matches_bare_repost():
    """An episode that carries an episode-title Part/Chapter token must still
    peer with the same SxxExx posted without that token.

    PTT leaves words like "Chapter One" inside an episode title, so the
    part-vs-bare xor (the 'Dune Part Two' vs 'Dune' movie discriminator) must
    NOT fire for episodes — both sides are the same episode, one just spelled
    out its episode title.
    """
    from resources.lib import fallback_streams as fs

    titled = _result(
        "Show.Name.S01E01.Chapter.1.1080p.WEB-DL.x265-GROUP",
        "https://idx/titled.nzb",
        2000000000,
    )
    bare = _result(
        "Show.Name.S01E01.1080p.WEB-DL.x265-GROUP",
        "https://idx/bare.nzb",
        2000000000,
    )
    assert fs._same_content(titled, bare) is True
    assert fs._same_content(bare, titled) is True

    real_titled = _result(
        "Stranger.Things.S04E01.Chapter.One.The.Hellfire.Club.1080p.NF.WEB-DL.x265-GROUP",
        "https://idx/stranger-titled.nzb",
        2000000000,
    )
    real_bare = _result(
        "Stranger.Things.S04E01.1080p.NF.WEB-DL.x265-GROUP",
        "https://idx/stranger-bare.nzb",
        2000000000,
    )
    assert fs._same_content(real_titled, real_bare) is True
    assert fs._same_content(real_bare, real_titled) is True


def test_same_content_rejects_different_part_same_episode():
    """REGRESSION GUARD: same SxxExx but a differing explicit part is different
    content, so the global differing-explicit-part reject must still apply to
    episodes (the broad CodeRabbit fix would false-accept this)."""
    from resources.lib import fallback_streams as fs

    part_one = _result(
        "Some.Show.S01E01.Part.1.1080p.WEB-DL.x265-GROUP",
        "https://idx/ep-part-one.nzb",
        2000000000,
    )
    part_two = _result(
        "Some.Show.S01E01.Part.2.1080p.WEB-DL.x265-GROUP",
        "https://idx/ep-part-two.nzb",
        2000000000,
    )
    assert fs._same_content(part_one, part_two) is False
    assert fs._same_content(part_two, part_one) is False


def test_same_content_rejects_episode_with_conflicting_years():
    """REGRESSION GUARD: a same-SxxExx episode whose differing parsed year marks
    a distinct production (a reboot/remake) is DIFFERENT content. The original
    Doctor Who (2005) S01E01 must never peer with the reboot (2023) S01E01."""
    from resources.lib import fallback_streams as fs

    classic = _result(
        "Doctor.Who.2005.S01E01.1080p.WEB-DL.x265-GROUP",
        "https://idx/dw-2005.nzb",
        2000000000,
    )
    reboot = _result(
        "Doctor.Who.2023.S01E01.1080p.WEB-DL.x265-GROUP",
        "https://idx/dw-2023.nzb",
        2000000000,
    )
    assert fs._same_content(classic, reboot) is False
    assert fs._same_content(reboot, classic) is False

    # A same-year same-episode repost still peers.
    same_year = _result(
        "Doctor.Who.2005.S01E01.1080p.WEB-DL.x265-ALT",
        "https://idx/dw-2005-alt.nzb",
        2000000000,
    )
    assert fs._same_content(classic, same_year) is True
    assert fs._same_content(same_year, classic) is True

    # One side omitting the year must NOT reject (only both-parsed-and-differ
    # rejects), so a yearless repost of the same episode still peers.
    yearless = _result(
        "Doctor.Who.S01E01.1080p.WEB-DL.x265-ALT",
        "https://idx/dw-yearless.nzb",
        2000000000,
    )
    assert fs._same_content(classic, yearless) is True
    assert fs._same_content(yearless, classic) is True


def test_titles_core_related_fails_closed_on_empty_token_set():
    """FS-2: an empty core-title token set must not fail open.

    A subject that normalizes to no word tokens (all punctuation) paired with an
    empty PTT title used to return True; without corroborating identity it must
    fail closed.
    """
    from resources.lib import fallback_streams as fs

    assert fs._titles_core_related("", "dune") is False
    assert fs._titles_core_related("dune", "") is False
    # But corroborating identity (matching year/episode) rescues the empty case.
    assert fs._titles_core_related("", "dune", corroborated=True) is True


def test_same_content_rejects_subtitled_sequel_without_year():
    """FS-3: a subset title relation with no year/episode backstop is rejected.

    "Avatar" must not peer with "Avatar The Way Of Water" when neither carries a
    year, because the extra tokens are a distinguishing subtitle, not junk.
    """
    from resources.lib import fallback_streams as fs

    avatar = _result(
        "Avatar.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/avatar.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    sequel = _result(
        "Avatar.The.Way.Of.Water.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/sequel.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    assert fs._same_content(avatar, sequel) is False
    assert fs._same_content(sequel, avatar) is False


def test_same_content_keeps_junk_suffix_repost_peering():
    """Legitimate junk-SUFFIX repost of the SAME release must still peer.

    "Movie" vs "Movie mirror" (trailing noise, no distinguishing subtitle) is a
    true repost and must keep matching even with no year on either side.
    """
    from resources.lib import fallback_streams as fs

    primary = _result(
        "Movie.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/movie.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    mirror = _result(
        "Movie.mirror.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/movie-mirror.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    assert fs._same_content(primary, mirror) is True
    assert fs._same_content(mirror, primary) is True


def test_same_content_rejects_numeric_sequel_suffix_without_year():
    """FS: a lone numeric sequel tail ("Avatar 2") is a content discriminator,
    not a junk suffix, even when neither side parses a year (PTT keeps the
    sequel number in the title and _part_number_from_title only covers labeled
    parts)."""
    from resources.lib import fallback_streams as fs

    sequel = _result(
        "Avatar.2.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/avatar2.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    original = _result(
        "Avatar.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/avatar.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    # Sanity: PTT keeps the sequel number in the title, parses no year.
    assert fs._release_identity(sequel)[0] == "avatar 2"
    assert fs._release_identity(sequel)[1] == 0
    assert fs._release_identity(original)[0] == "avatar"
    assert fs._same_content(sequel, original) is False
    assert fs._same_content(original, sequel) is False
    rocky2 = _result(
        "Rocky.2.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/rocky2.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    rocky = _result(
        "Rocky.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/rocky.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    assert fs._same_content(rocky2, rocky) is False
    assert fs._same_content(rocky, rocky2) is False


def test_same_content_rejects_roman_ordinal_sequel_suffix_without_year():
    """FS-M: a lone MULTI-CHARACTER Roman-numeral or ordinal-word sequel tail
    ("Rocky IV", "Rambo III", "Iron Man Three") is a content discriminator, not
    a junk suffix, and must be rejected when neither side parses a year (PTT
    keeps the sequel discriminator inside the title)."""
    from resources.lib import fallback_streams as fs

    rocky4 = _result(
        "Rocky.IV.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/rocky4.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    rocky = _result(
        "Rocky.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/rocky.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    # Sanity: PTT keeps the Roman tail in the title, parses no year.
    assert fs._release_identity(rocky4)[0] == "rocky iv"
    assert fs._release_identity(rocky4)[1] == 0
    assert fs._release_identity(rocky)[0] == "rocky"
    assert fs._same_content(rocky4, rocky) is False
    assert fs._same_content(rocky, rocky4) is False

    rambo3 = _result(
        "Rambo.III.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/rambo3.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    rambo = _result(
        "Rambo.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/rambo.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    assert fs._same_content(rambo3, rambo) is False
    assert fs._same_content(rambo, rambo3) is False

    iron_man_three = _result(
        "Iron.Man.Three.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/iron3.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    iron_man = _result(
        "Iron.Man.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/iron.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    assert fs._same_content(iron_man_three, iron_man) is False
    assert fs._same_content(iron_man, iron_man_three) is False


def test_same_content_keeps_single_letter_and_one_eleven_tails_peering():
    """FS-M: the sequel-tail reject is MULTI-CHARACTER only.

    Single-letter Roman tails (i/v/x) collide with stray junk-suffix letters, so
    "Saw X" / "Final Destination V" must still peer; "one" is never a real
    sequel tail; and the ordinal-word ceiling stops at "ten" so "Ocean's Eleven"
    (eleven) is unaffected. All must still peer with the bare title when no year
    corroborates."""
    from resources.lib import fallback_streams as fs

    saw_x = _result(
        "Saw.X.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/sawx.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    saw = _result(
        "Saw.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/saw.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    assert fs._same_content(saw_x, saw) is True
    assert fs._same_content(saw, saw_x) is True

    fd_v = _result(
        "Final.Destination.V.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/fdv.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    fd = _result(
        "Final.Destination.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/fd.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    assert fs._same_content(fd_v, fd) is True
    assert fs._same_content(fd, fd_v) is True

    # Direct title-predicate checks for "one" exclusion and the "eleven" ceiling
    # (these PTT-parse cleanly to bare-vs-tail core titles).
    assert fs._titles_core_related("show one", "show", corroborated=False) is True
    assert fs._titles_core_related("show", "show one", corroborated=False) is True
    assert (
        fs._titles_core_related("oceans eleven", "oceans", corroborated=False) is True
    )
    assert (
        fs._titles_core_related("oceans", "oceans eleven", corroborated=False) is True
    )


def test_titles_core_related_sequel_tail_predicate_matrix():
    """FS-M: direct-predicate matrix for the sequel-tail reject."""
    from resources.lib import fallback_streams as fs

    # Multi-character Roman / ordinal tails: rejected without corroboration,
    # rescued by corroboration.
    assert fs._titles_core_related("rocky iv", "rocky", corroborated=False) is False
    assert fs._titles_core_related("rocky", "rocky iv", corroborated=False) is False
    assert fs._titles_core_related("rocky iv", "rocky", corroborated=True) is True
    assert fs._titles_core_related("iron man three", "iron man", False) is False
    # Legit junk suffix stays a repost.
    assert fs._titles_core_related("movie", "movie mirror", corroborated=False) is True
    # Single-letter ambiguity preserved.
    assert fs._titles_core_related("saw x", "saw", corroborated=False) is True
    # "one" exclusion.
    assert fs._titles_core_related("show one", "show", corroborated=False) is True
    # The new constant excludes single-letter romans and "one"/"eleven".
    assert "ii" in fs._SEQUEL_TAIL_TOKENS
    assert "three" in fs._SEQUEL_TAIL_TOKENS
    assert "ten" in fs._SEQUEL_TAIL_TOKENS
    for excluded in ("i", "v", "x", "one", "eleven"):
        assert excluded not in fs._SEQUEL_TAIL_TOKENS


def test_same_content_rescues_roman_sequel_with_matching_year():
    """FS-M: a matching parsed year on both sides corroborates and rescues an
    otherwise-rejected Roman/ordinal sequel tail (legit repost short-circuit)."""
    from resources.lib import fallback_streams as fs

    rocky4_a = _result(
        "Rocky.IV.2025.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/rocky4a.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    rocky4_b = _result(
        "Rocky.2025.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/rocky4b.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    # Sanity: both parse the same year, so identity is corroborated.
    assert fs._release_identity(rocky4_a)[1] == 2025
    assert fs._release_identity(rocky4_b)[1] == 2025
    assert fs._same_content(rocky4_a, rocky4_b) is True
    assert fs._same_content(rocky4_b, rocky4_a) is True


def test_part_number_from_title_roman_sequel_tails_are_not_parts():
    """FS-M regression guard: a bare Roman/ordinal sequel tail is NOT a
    labeled part, but an explicit Part/Chapter label still parses."""
    from resources.lib import fallback_streams as fs

    assert fs._part_number_from_title("Rocky IV") == 0
    assert fs._part_number_from_title("Saw X") == 0
    assert fs._part_number_from_title("Dune Part Two") == 2
    assert fs._part_number_from_title("John Wick Chapter 4") == 4


def test_same_content_ignores_phantom_season_for_movie_peer():
    """FS: a movie whose release-group suffix mis-parses as a season (e.g.
    REMUX-ALT01 -> seasons=[1]) must still peer with the same movie posted by a
    normal group (seasons=[]) when both carry the same parsed year.

    Without the guarded collapse the phantom season makes one side look
    episodic, and the season-presence parity check then rejects the same
    movie from a normal group.
    """
    from resources.lib import fallback_streams as fs

    phantom = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-REMUX-ALT01",
        "https://idx/matrix-alt01.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    normal = _result(
        "The.Matrix.1999.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/matrix-group.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    # Sanity: the suffix injects a phantom season on one side only, no episode,
    # and both sides parse the same year.
    phantom_identity = fs._release_identity(phantom)
    normal_identity = fs._release_identity(normal)
    assert phantom_identity[2] == (1,)
    assert phantom_identity[3] == ()
    assert normal_identity[2] == ()
    assert normal_identity[3] == ()
    assert phantom_identity[1] == normal_identity[1] == 1999
    assert fs._same_content(phantom, normal) is True
    assert fs._same_content(normal, phantom) is True


def test_same_content_keeps_distinct_seasons_apart_despite_phantom_collapse():
    """REGRESSION GUARD: the phantom-season collapse must NOT relax genuinely
    different seasons. Fargo S01 and S02 both carry a season AND the same year,
    so the season-presence parity holds and they stay subject to season
    equality — they must remain different content.
    """
    from resources.lib import fallback_streams as fs

    s01 = _result(
        "Fargo.2014.S01.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/fargo-s01.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    s02 = _result(
        "Fargo.2014.S02.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/fargo-s02.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    # Sanity: both sides carry a season and the same year (presence matches).
    assert fs._release_identity(s01)[2] == (1,)
    assert fs._release_identity(s02)[2] == (2,)
    assert fs._release_identity(s01)[1] == fs._release_identity(s02)[1] == 2014
    assert fs._same_content(s01, s02) is False
    assert fs._same_content(s02, s01) is False


def test_titles_core_related_rejects_disjoint_tail_without_corroboration():
    """FB-1: two titles that share a >=2-token prefix but diverge into DIFFERENT
    tails on both sides (neither a subset of the other) are typically distinct
    works in a franchise. A loose >=2-token overlap is not enough to call them
    the same content -- require corroborating positive identity (matching year,
    or matching season+episode). Without corroboration the disjoint-tail overlap
    must be rejected; with it, the loose overlap is allowed.
    """
    from resources.lib import fallback_streams as fs

    left = "mission impossible fallout"
    right = "mission impossible dead reckoning"
    # Sanity: neither token set is a subset of the other (this is the
    # disjoint-tail branch, not the junk-suffix subset branch).
    assert not frozenset(left.split()).issubset(frozenset(right.split()))
    assert not frozenset(right.split()).issubset(frozenset(left.split()))
    assert fs._titles_core_related(left, right, corroborated=False) is False
    assert fs._titles_core_related(right, left, corroborated=False) is False
    assert fs._titles_core_related(left, right, corroborated=True) is True
    assert fs._titles_core_related(right, left, corroborated=True) is True


def test_same_content_rejects_disjoint_tail_franchise_without_year():
    """FB-1: different films in a franchise that share a >=2-token prefix but
    diverge in their tails, with no year on either side, must NOT be treated as
    the same content (no corroborating year/episode to back the loose overlap).
    """
    from resources.lib import fallback_streams as fs

    fallout = _result(
        "Mission.Impossible.Fallout.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/fallout.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    reckoning = _result(
        "Mission.Impossible.Dead.Reckoning.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/reckoning.nzb",
        60000000000,
        meta=_movie_meta(),
    )
    # Sanity: PTT leaves the distinguishing tail in the title and parses no year.
    assert fs._release_identity(fallout)[1] == 0
    assert fs._release_identity(reckoning)[1] == 0
    assert fs._same_content(fallout, reckoning) is False
    assert fs._same_content(reckoning, fallout) is False


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_same_episode_different_group_is_rejected_fallback(mock_settings, mock_fetch):
    """A different release group is no longer a qualified fallback (user
    requirement). Even with the same show/season/episode and matching profile, a
    candidate from a different group is a different file that can never byte-match
    for a seamless cutover, so it must not be attached as a fallback."""
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Show.Name.S02E05.2160p.WEB-DL.x265-GROUP",
        "https://idx/primary.nzb",
        6000000000,
        meta=_movie_meta(group="GROUP"),
    )
    repost = _result(
        "Show.Name.S02E05.2160p.WEB-DL.x265-ALT",
        "https://idx/repost.nzb",
        6100000000,
        meta=_movie_meta(group="ALT"),
    )
    manifests = {
        "https://idx/primary.nzb": _manifest("video", "p.mkv", 6000000000, "a"),
        "https://idx/repost.nzb": _manifest("video", "r.mkv", 6100000000, "b"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, repost])

    assert primary["_fallback_candidates"] == []
    assert repost["_fallback_candidates"] == []


def test_metadata_profiles_match_requires_same_release_group():
    """User-reported scenario: a FraMeSToR release must NOT pull in an RU4HD
    encode of the same movie as a backup. Two releases with an IDENTICAL encode
    profile that differ ONLY in release group are group-agnostic peers by default
    but are rejected once require_same_group is set -- proving the group, not any
    other attribute, is the discriminator. A same-group re-post still peers.
    """
    from resources.lib import fallback_streams as fs

    # Raw results (no pre-set _meta) so the group is parsed from the release
    # title -- the real production path. Identical encode profile, group only
    # differs.
    framestor = {
        "title": "Goodfellas.1990.2160p.UHD.BluRay.REMUX.DV.HEVC.TrueHD.7.1-FraMeSToR",
        "link": "https://idx/goodfellas-framestor.nzb",
        "size": 60000000000,
    }
    ru4hd = {
        "title": "Goodfellas.1990.2160p.UHD.BluRay.REMUX.DV.HEVC.TrueHD.7.1-RU4HD",
        "link": "https://idx/goodfellas-ru4hd.nzb",
        "size": 60000000000,
    }
    same_group_repost = {
        "title": "Goodfellas.1990.2160p.UHD.BluRay.REMUX.DV.HEVC.TrueHD.7.1-FraMeSToR",
        "link": "https://idx/goodfellas-framestor-mirror.nzb",
        "size": 60000000000,
    }
    # Sanity: groups parse distinct / equal; the encode profile is otherwise
    # identical (so a False below is caused by the group, nothing else).
    assert fs._meta_value(framestor, "group") == "framestor"
    assert fs._meta_value(ru4hd, "group") == "ru4hd"
    assert fs._metadata_profiles_match(framestor, ru4hd) is True
    assert (
        fs._metadata_profiles_match(framestor, ru4hd, require_same_group=True) is False
    )
    assert (
        fs._metadata_profiles_match(
            framestor, same_group_repost, require_same_group=True
        )
        is True
    )


def test_release_similarity_tiers():
    from resources.lib import fallback_streams as fs

    primary = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/p.nzb",
        60000000000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"),
    )
    # Tier 0: same res/codec/group, ~same size.
    tier0 = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/t0.nzb",
        60500000000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"),
    )
    # Tier 1: same res/codec, different group, within ~10%.
    tier1 = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-ALT",
        "https://idx/t1.nzb",
        63000000000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="ALT"),
    )
    # Tier 2: same res, different codec.
    tier2 = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.x264-ALT",
        "https://idx/t2.nzb",
        62000000000,
        meta=_movie_meta(resolution="2160p", codec="x264/AVC", group="ALT"),
    )
    # Tier 3: same content, different resolution.
    tier3 = _result(
        "Dune.Part.Two.2024.1080p.BluRay.x264-ALT",
        "https://idx/t3.nzb",
        20000000000,
        meta=_movie_meta(resolution="1080p", codec="x264/AVC", group="ALT"),
    )
    # Different content -> None.
    other = _result(
        "Dune.Part.One.2021.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/o.nzb",
        60000000000,
        meta=_movie_meta(),
    )

    assert fs._release_similarity(primary, tier0) == 0
    assert fs._release_similarity(primary, tier1) == 1
    assert fs._release_similarity(primary, tier2) == 2
    assert fs._release_similarity(primary, tier3) == 3
    assert fs._release_similarity(primary, other) is None


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_fallback_candidates_sorted_best_tier_first(mock_settings, mock_fetch):
    """Candidates are ordered by tier (most-similar first), not pool order.

    All peers share the primary's release group (a different group is no longer a
    qualified fallback), so tiering is exercised within the same group by size:
    ranking must still place the ~identical-size repost (Tier 0, size within 3%)
    ahead of the larger same-encode repost (Tier 1, size beyond 3% but within the
    10% peer band) even though the pool lists the Tier-1 peer first.
    """
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/primary.nzb",
        60000000000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"),
    )
    # Tier 1 (same group, same res+codec, size beyond the 3% Tier-0 band but
    # within the 10% peer band), listed first to prove ranking re-orders them.
    worse = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/worse.nzb",
        62500000000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"),
    )
    # Tier 0 (same group, size within 3%).
    best = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/best.nzb",
        60100000000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"),
    )
    manifests = {
        "https://idx/primary.nzb": _manifest("video", "p.mkv", 60000000000, "a"),
        "https://idx/worse.nzb": _manifest("video", "w.mkv", 62500000000, "b"),
        "https://idx/best.nzb": _manifest("video", "x.mkv", 60100000000, "c"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, worse, best])

    assert primary["_fallback_candidates"] == [best, worse]


@patch("resources.lib.fallback_streams.fetch_nzb_video_manifest")
@patch("resources.lib.fallback_streams._fallback_settings")
def test_attach_fallback_candidates_prefers_exact_filename_over_tier(
    mock_settings, mock_fetch
):
    """Through the production attach path, an exact-same-filename repost (a
    different upload of the byte-identical file) must rank ahead of a closer
    tier/size peer — the user requirement to try exact filenames first."""
    mock_settings.return_value = (True, 5)
    primary = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/primary.nzb",
        60000000000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"),
    )
    # Tier 0 (size within 3%) but a DIFFERENT filename — listed first to prove
    # the exact-filename key re-orders ahead of the better tier.
    closer = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/closer.nzb",
        60100000000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"),
    )
    # Tier 1 (size beyond 3%, within the 10% peer band) but the EXACT same
    # filename as the primary.
    exact = _result(
        "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HEVC-GROUP",
        "https://idx/exact.nzb",
        62500000000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"),
    )
    manifests = {
        "https://idx/primary.nzb": _manifest("video", "dune.mkv", 60000000000, "a"),
        "https://idx/closer.nzb": _manifest("video", "other.mkv", 60100000000, "c"),
        "https://idx/exact.nzb": _manifest("video", "dune.mkv", 62500000000, "b"),
    }
    mock_fetch.side_effect = lambda url, **_kwargs: manifests[url]

    attach_fallback_candidates([primary, closer, exact])

    assert primary["_fallback_candidates"] == [exact, closer]


def test_metadata_profiles_match_fails_closed_on_unknown_resolution_same_group():
    """When same-group backups are required, a candidate whose resolution PTT
    could not parse must be REJECTED — the user requires the backup share the
    primary's resolution, so the gate fails closed like the group gate."""
    from resources.lib import fallback_streams

    primary = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://a/nzb",
        1000,
        meta=_movie_meta(resolution="1080p", codec="x265/HEVC", group="GROUP"),
    )
    unknown_res = _result(
        "Example Movie 2026 WEB-DL x265-GROUP",
        "https://b/nzb",
        1000,
        meta=_movie_meta(resolution="", codec="x265/HEVC", group="GROUP"),
    )
    assert not fallback_streams._metadata_profiles_match(
        primary, unknown_res, require_same_group=True
    )


def test_metadata_profiles_match_rejects_different_resolution_same_group():
    from resources.lib import fallback_streams

    primary = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://a/nzb",
        1000,
        meta=_movie_meta(resolution="1080p", codec="x265/HEVC", group="GROUP"),
    )
    other_res = _result(
        "Example Movie 2026 2160p WEB-DL x265-GROUP",
        "https://b/nzb",
        1000,
        meta=_movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP"),
    )
    assert not fallback_streams._metadata_profiles_match(
        primary, other_res, require_same_group=True
    )


def test_metadata_profiles_match_accepts_same_resolution_same_group():
    from resources.lib import fallback_streams

    primary = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://a/nzb",
        1000,
        meta=_movie_meta(resolution="1080p", codec="x265/HEVC", group="GROUP"),
    )
    same = _result(
        "Example Movie 2026 1080p WEB-DL x265-GROUP",
        "https://b/nzb",
        1000,
        meta=_movie_meta(resolution="1080p", codec="x265/HEVC", group="GROUP"),
    )
    assert fallback_streams._metadata_profiles_match(
        primary, same, require_same_group=True
    )


def test_rank_fallback_candidates_prefers_exact_same_filename():
    """An exact-same-filename repost (different upload) must be ranked ahead of a
    closer-by-tier/size peer — the user wants exact filenames preferred first."""
    from resources.lib import fallback_streams

    meta = _movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP")
    target = _result(
        "Example 2026 2160p BluRay x265-GROUP", "https://t/nzb", 60000000000, meta=meta
    )
    target["_fallback_manifest"] = _manifest(
        "video", "example.2026.2160p.group.mkv", 60000000000, "t"
    )
    # Tier 0 peer (same res+codec+group, size within 3%) but DIFFERENT filename.
    closer = _result(
        "Example 2026 2160p BluRay x265-GROUP", "https://c/nzb", 60000000000, meta=meta
    )
    closer["_fallback_manifest"] = _manifest(
        "video", "different.name.mkv", 60000000000, "c"
    )
    # Tier 1 peer (size ~10% off -> not tier 0) but EXACT same filename.
    exact = _result(
        "Example 2026 2160p BluRay x265-GROUP", "https://e/nzb", 66000000000, meta=meta
    )
    exact["_fallback_manifest"] = _manifest(
        "video", "example.2026.2160p.group.mkv", 66000000000, "e"
    )

    ranked = fallback_streams._rank_fallback_candidates(target, [closer, exact])

    assert ranked[0] is exact
    assert ranked[1] is closer


def _dated(tier, pubdate, link, exact_name=1, size_delta=0):
    """Build a (exact_name, tier, size_delta, candidate) ranking tuple."""
    return (exact_name, tier, size_delta, {"link": link, "pubdate": pubdate})


def test_dedupe_pubdate_collapses_same_hour_keeping_best_tier():
    from resources.lib import fallback_streams

    target = {"pubdate": ""}  # undated primary -> no primary suppression
    worse = _dated(2, "Mon, 01 Jan 2024 00:10:00 +0000", "https://a/nzb")
    better = _dated(0, "Mon, 01 Jan 2024 00:50:00 +0000", "https://b/nzb")

    result = fallback_streams._dedupe_candidates_by_pubdate(target, [worse, better])

    assert [item[3]["link"] for item in result] == ["https://b/nzb"]


def test_dedupe_pubdate_keeps_candidates_more_than_an_hour_apart():
    from resources.lib import fallback_streams

    target = {"pubdate": ""}
    first = _dated(1, "Mon, 01 Jan 2024 00:00:00 +0000", "https://a/nzb")
    second = _dated(1, "Mon, 01 Jan 2024 02:00:00 +0000", "https://b/nzb")

    result = fallback_streams._dedupe_candidates_by_pubdate(target, [first, second])

    assert {item[3]["link"] for item in result} == {"https://a/nzb", "https://b/nzb"}


def test_dedupe_pubdate_is_anchor_based_not_transitive():
    from resources.lib import fallback_streams

    target = {"pubdate": ""}
    a = _dated(1, "Mon, 01 Jan 2024 00:00:00 +0000", "https://a/nzb")
    b = _dated(1, "Mon, 01 Jan 2024 00:50:00 +0000", "https://b/nzb")
    c = _dated(1, "Mon, 01 Jan 2024 01:40:00 +0000", "https://c/nzb")

    result = fallback_streams._dedupe_candidates_by_pubdate(target, [a, b, c])
    links = {item[3]["link"] for item in result}

    # a & b (50 min) collapse to one; c is >1h from the a-anchor -> distinct.
    assert len(result) == 2
    assert "https://c/nzb" in links
    assert "https://a/nzb" in links  # equal tier -> order_index tie-break keeps first
    assert "https://b/nzb" not in links


def test_dedupe_pubdate_boundary_exactly_one_hour_collapses():
    from resources.lib import fallback_streams

    target = {"pubdate": ""}
    first = _dated(1, "Mon, 01 Jan 2024 00:00:00 +0000", "https://a/nzb")
    # Exactly 3600s later -> inclusive window -> same article -> collapse.
    second = _dated(1, "Mon, 01 Jan 2024 01:00:00 +0000", "https://b/nzb")

    result = fallback_streams._dedupe_candidates_by_pubdate(target, [first, second])

    assert len(result) == 1
    assert result[0][3]["link"] == "https://a/nzb"


def test_dedupe_pubdate_drops_candidates_sharing_primary_date():
    from resources.lib import fallback_streams

    target = {"pubdate": "Mon, 01 Jan 2024 00:00:00 +0000"}
    same_as_primary = _dated(0, "Mon, 01 Jan 2024 00:30:00 +0000", "https://a/nzb")
    distinct = _dated(1, "Mon, 01 Jan 2024 03:00:00 +0000", "https://b/nzb")

    result = fallback_streams._dedupe_candidates_by_pubdate(
        target, [same_as_primary, distinct]
    )

    assert [item[3]["link"] for item in result] == ["https://b/nzb"]


def test_dedupe_pubdate_keeps_all_undated_candidates():
    from resources.lib import fallback_streams

    target = {"pubdate": ""}
    one = (1, 1, 0, {"link": "https://a/nzb"})  # no pubdate key
    two = (1, 1, 0, {"link": "https://b/nzb", "pubdate": ""})  # empty pubdate
    three = (1, 1, 0, {"link": "https://c/nzb", "pubdate": "not a date"})

    result = fallback_streams._dedupe_candidates_by_pubdate(target, [one, two, three])

    assert {item[3]["link"] for item in result} == {
        "https://a/nzb",
        "https://b/nzb",
        "https://c/nzb",
    }


def test_attach_candidates_dedupes_same_postdate_keeping_best_tier():
    from resources.lib import fallback_streams

    meta = _movie_meta(resolution="1080p", codec="x265/HEVC", group="GROUP")
    target = _result(
        "Example 2026 1080p WEB-DL x265-GROUP", "https://t/nzb", 10000000000, meta=meta
    )
    target["_fallback_manifest"] = _manifest(
        "video", "example.2026.1080p.group.mkv", 10000000000, "t"
    )
    target["pubdate"] = "Wed, 01 May 2024 12:00:00 +0000"

    # Same content + group + size -> tier 0. Posted 20 min apart -> same article.
    keep = _result(
        "Example 2026 1080p WEB-DL x265-GROUP",
        "https://keep/nzb",
        10000000000,
        meta=meta,
    )
    keep["_fallback_manifest"] = _manifest(
        "video", "example.2026.1080p.group.mkv", 10000000000, "keep"
    )
    keep["pubdate"] = "Thu, 02 May 2024 00:00:00 +0000"

    drop = _result(
        "Example 2026 1080p WEB-DL x265-GROUP",
        "https://drop/nzb",
        11000000000,
        meta=meta,
    )
    drop["_fallback_manifest"] = _manifest(
        "video", "example.2026.1080p.group.mkv", 11000000000, "drop"
    )
    drop["pubdate"] = "Thu, 02 May 2024 00:20:00 +0000"

    fallback_streams._attach_candidates_for_target(target, [keep, drop], 5)

    links = [c["link"] for c in target["_fallback_candidates"]]
    assert links == ["https://keep/nzb"]


def test_rank_fallback_candidates_dedupes_same_postdate():
    from resources.lib import fallback_streams

    meta = _movie_meta(resolution="2160p", codec="x265/HEVC", group="GROUP")
    target = _result(
        "Example 2026 2160p BluRay x265-GROUP", "https://t/nzb", 60000000000, meta=meta
    )
    target["_fallback_manifest"] = _manifest(
        "video", "example.2026.2160p.group.mkv", 60000000000, "t"
    )
    target["pubdate"] = "Fri, 10 May 2024 08:00:00 +0000"

    early = _result(
        "Example 2026 2160p BluRay x265-GROUP",
        "https://early/nzb",
        60000000000,
        meta=meta,
    )
    early["_fallback_manifest"] = _manifest(
        "video", "example.2026.2160p.group.mkv", 60000000000, "early"
    )
    early["pubdate"] = "Sat, 11 May 2024 09:00:00 +0000"

    # 40 min after `early` -> same article -> must collapse.
    dupe = _result(
        "Example 2026 2160p BluRay x265-GROUP",
        "https://dupe/nzb",
        60000000000,
        meta=meta,
    )
    dupe["_fallback_manifest"] = _manifest(
        "video", "example.2026.2160p.group.mkv", 60000000000, "dupe"
    )
    dupe["pubdate"] = "Sat, 11 May 2024 09:40:00 +0000"

    # 3 hours after `early` -> distinct -> must survive.
    distinct = _result(
        "Example 2026 2160p BluRay x265-GROUP",
        "https://distinct/nzb",
        60000000000,
        meta=meta,
    )
    distinct["_fallback_manifest"] = _manifest(
        "video", "example.2026.2160p.group.mkv", 60000000000, "distinct"
    )
    distinct["pubdate"] = "Sat, 11 May 2024 12:00:00 +0000"

    ranked = fallback_streams._rank_fallback_candidates(target, [early, dupe, distinct])
    links = {c["link"] for c in ranked}

    assert "https://distinct/nzb" in links
    assert ("https://early/nzb" in links) != ("https://dupe/nzb" in links)
    assert len(ranked) == 2


def test_path_is_under_base_rejects_encoded_traversal():
    """FS: canonicalize decoded paths before the base-path allow-list check.

    A percent-encoded traversal like ``/dav/%2e%2e/admin`` passes a raw-prefix
    match against base ``/dav`` but resolves outside it once the server decodes
    it, which would leak the forwarded Authorization header to an escaped path.
    The containment check must decode and reject the escape.
    """
    from resources.lib import fallback_streams as fs

    # Legitimate in-base paths still pass (encoded and plain).
    assert fs._path_is_under_base("/dav", "/dav") is True
    assert fs._path_is_under_base("/dav/movie.mkv", "/dav") is True
    assert fs._path_is_under_base("/dav/a%20b/movie.mkv", "/dav") is True
    # An encoded slash decodes to a genuine under-base path and is accepted
    # (the WebDAV server resolves it the same way); decode-then-normalize is
    # intentionally more permissive than a raw prefix match in this direction.
    assert fs._path_is_under_base("/dav%2Fadmin", "/dav") is True
    # Encoded ".." traversal under the base must be rejected.
    assert fs._path_is_under_base("/dav/%2e%2e/admin", "/dav") is False
    assert fs._path_is_under_base("/dav/sub/%2e%2e/%2e%2e/etc", "/dav") is False
    # Raw ".." segments and backslash escapes are rejected too.
    assert fs._path_is_under_base("/dav/../admin", "/dav") is False
    assert fs._path_is_under_base("/dav\\..\\admin", "/dav") is False
    # A sibling that merely shares the base prefix string is not "under" it.
    assert fs._path_is_under_base("/davother/file", "/dav") is False


def test_validated_probe_url_rejects_encoded_traversal():
    """FS: encoded traversal must not survive probe-URL validation.

    The Authorization-forwarding probe path (fetch_content_length /
    fetch_range_digest) only runs on a URL that passes _validated_probe_url, so a
    traversal that escapes the configured base must validate to None.
    """
    from resources.lib import fallback_streams as fs
    from resources.lib.fallback_streams import _split_http_url

    base = _split_http_url("https://host/dav")
    assert base is not None

    good = fs._validated_probe_url("https://host/dav/movie.mkv", probe_bases=[base])
    assert good == "https://host/dav/movie.mkv"

    escaped = fs._validated_probe_url(
        "https://host/dav/%2e%2e/admin", probe_bases=[base]
    )
    assert escaped is None


def test_titles_core_related_strict_subset_after_equality_fast_path():
    """FS: the subset branch uses strict subsets (equality handled earlier).

    The ``left == right`` fast path returns before the subset test, so the
    subset comparison must be a strict subset on each side; a disjoint pair
    (neither a subset of the other) is rejected without corroboration.
    """
    from resources.lib import fallback_streams as fs

    # Proper-subset (junk suffix) repost still accepted in both directions.
    assert fs._titles_core_related("the matrix", "the matrix mirror") is True
    assert fs._titles_core_related("the matrix mirror", "the matrix") is True
    # Identical titles still take the equality fast path.
    assert fs._titles_core_related("the matrix", "the matrix") is True
