# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import threading
import time as _time
from unittest.mock import ANY, MagicMock, patch
from urllib.error import URLError
from urllib.parse import urlsplit
from xml.sax.saxutils import quoteattr

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
            "group": "ALT",
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
            "DV.HEVC.REMUX.FraMeSToR"
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
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT{:02d}".format(index),
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
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-SLOW{:02d}".format(index),
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

    def fetch(url, **_kwargs):
        if url == selected["link"]:
            return manifests[url]
        candidate_started.set()
        release_candidates.wait(timeout=1)
        return manifests[url]

    mock_fetch.side_effect = fetch
    release_timer = threading.Timer(0.3, release_candidates.set)
    release_timer.start()
    try:
        before = _time.monotonic()
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)
        elapsed = _time.monotonic() - before
    finally:
        release_candidates.set()
        release_timer.cancel()

    assert candidate_started.wait(0.2)
    assert elapsed < 0.2
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
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT{:02d}".format(index),
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
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-PAR{:02d}".format(index),
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
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-OVR{:02d}".format(index),
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
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-PIPE{:02d}".format(index),
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

    def fetch(url, **_kwargs):
        if url == candidates[1]["link"]:
            slow_started.set()
            release_slow.wait(timeout=1)
        return manifests[url]

    mock_fetch.side_effect = fetch
    release_timer = threading.Timer(0.25, release_slow.set)
    release_timer.start()
    try:
        before = _time.monotonic()
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)
        elapsed = _time.monotonic() - before
    finally:
        release_slow.set()
        release_timer.cancel()

    assert slow_started.is_set()
    assert elapsed < 0.2
    assert selected["_fallback_candidates"] == [candidates[0], candidates[2]]


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
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-SCALE{:02d}".format(index),
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

    def fetch(url, **_kwargs):
        if url == candidates[6]["link"]:
            slow_started.set()
            release_slow.wait(timeout=1)
        return manifests[url]

    mock_fetch.side_effect = fetch
    release_timer = threading.Timer(0.25, release_slow.set)
    release_timer.start()
    try:
        before = _time.monotonic()
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)
        elapsed = _time.monotonic() - before
    finally:
        release_slow.set()
        release_timer.cancel()

    assert slow_started.is_set()
    assert elapsed < 0.2
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

    def fetch(url, **_kwargs):
        if url == candidates[1]["link"]:
            slow_started.set()
            release_slow.wait(timeout=1)
        return manifests[url]

    mock_fetch.side_effect = fetch
    release_timer = threading.Timer(0.3, release_slow.set)
    release_timer.start()
    try:
        before = _time.monotonic()
        attach_fallback_candidates_for_selection(selected, [selected] + candidates)
        elapsed = _time.monotonic() - before
    finally:
        release_slow.set()
        release_timer.cancel()

    assert slow_started.is_set()
    assert elapsed < 0.2
    assert selected["_fallback_candidates"] == [candidates[0]]


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
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT{:02d}".format(index),
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
            "container": "mkv",
        },
    )
    candidates = [
        _result(
            "The.Matrix.1999.UHD.BluRay.2160p.DV.HEVC.REMUX-ALT{:02d}".format(index),
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


def test_fingerprint_ranges_uses_20_deterministic_4096_byte_samples_for_large_files():
    content_length = 10 * 1024 * 1024 * 1024

    ranges = fingerprint_ranges(content_length)

    assert len(ranges) == 20
    assert len(set(ranges)) == 20
    assert ranges == fingerprint_ranges(content_length)
    assert ranges[0] == (0, 4095)
    assert ranges[-1] == (content_length - 4096, content_length - 1)
    assert all((end - start + 1) == 4096 for start, end in ranges)


def test_fingerprint_ranges_reuses_large_file_sample_offsets_between_calls():
    from resources.lib import fallback_streams

    content_length = 11 * 1024 * 1024 * 1024 + 12345
    original_sha256 = fallback_streams.hashlib.sha256
    sha256_calls = []

    def counted_sha256(*args, **kwargs):
        sha256_calls.append(args)
        return original_sha256(*args, **kwargs)

    with patch(
        "resources.lib.fallback_streams.hashlib.sha256", side_effect=counted_sha256
    ):
        first = fingerprint_ranges(content_length)
        second = fingerprint_ranges(content_length)

    assert second == first
    assert len(sha256_calls) == len(first) - 2


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

    fallback_streams._cached_validated_probe_url.cache_clear()
    assert mock_urlopen.call_count == 3
    assert validation_urls == [url]


def test_precomputed_probe_bases_reuse_base_origin_checks_for_range_digest():
    from resources.lib import fallback_streams

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
def test_video_manifest_peer_match_accepts_size_within_20_percent_tolerance(
    mock_settings, mock_fetch
):
    """Different uploads of the same source MKV use different yEnc segment sizes,
    so two video manifests for the same release will report different group_bytes.
    Accept matches when the bytes are within +/-20% as long as title and profile
    gates already passed.
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
        110000000000,
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
            "video", "once upon a time alt repost.mkv", 110000000000, "articles-b"
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
