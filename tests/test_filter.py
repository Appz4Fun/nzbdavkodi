# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import ast
from pathlib import Path
from unittest.mock import patch

from resources.lib.filter import (
    _sort_results,
    filter_results,
    matches_filters,
    parse_title_metadata,
    release_is_pack,
)

FILTER_MODULE = (
    Path(__file__).resolve().parents[1]
    / "repo"
    / "plugin.video.nzbdav"
    / "resources"
    / "lib"
    / "filter.py"
)


def _make_result(title, size="5000000000", pubdate="", link="http://example.com/nzb"):
    return {
        "title": title,
        "link": link,
        "size": size,
        "indexer": "test",
        "pubdate": pubdate,
        "age": "1 day",
    }


def test_filter_uses_module_scope_kodi_imports():
    tree = ast.parse(FILTER_MODULE.read_text())
    kodi_modules = {"xbmc", "xbmcaddon", "xbmcgui"}
    module_imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    local_imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import) and node not in tree.body
        for alias in node.names
        if alias.name in kodi_modules
    ]

    assert kodi_modules <= module_imports
    assert local_imports == []


# --- parse_title_metadata with real PTT (not mocked) ---


def test_parse_title_metadata_movie():
    meta = parse_title_metadata(
        "The.Matrix.1999.2160p.BluRay.REMUX.HEVC.DTS-HD.MA.7.1-GROUP"
    )
    assert meta["resolution"] == "2160p"
    # Codec is a PTT-derived string; accept any normalized form that
    # identifies HEVC/h265 so a PTT upgrade that renames the token doesn't
    # break this test.
    codec_lower = meta["codec"].lower()
    assert (
        "hevc" in codec_lower or "265" in codec_lower
    ), "expected HEVC/x265 codec, got {!r}".format(meta["codec"])
    assert meta["group"] == "GROUP"


def test_parse_title_metadata_no_resolution():
    meta = parse_title_metadata("Some.Random.Title-GROUP")
    assert meta["resolution"] == ""


def test_parse_title_metadata_exposes_proper_and_repack_flags():
    meta = parse_title_metadata("Movie.2024.PROPER.REPACK.1080p.BluRay.x264-GROUP")

    assert meta["proper"] is True
    assert meta["repack"] is True


def test_parse_title_metadata_1080p_x264():
    """Real PTT parsing of a typical 1080p x264 release."""
    meta = parse_title_metadata("Inception.2010.1080p.BluRay.x264-FGT")
    assert meta["resolution"] == "1080p"
    assert meta["codec"] == "x264/AVC"
    assert meta["group"] == "FGT"


def test_parse_title_metadata_720p_web():
    meta = parse_title_metadata("The.Office.S09E23.720p.WEB-DL.AAC2.0.H.264-NTb")
    assert meta["resolution"] == "720p"


def test_parse_title_metadata_4k_hdr():
    meta = parse_title_metadata(
        "Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-FLUX"
    )
    assert meta["resolution"] == "2160p"


def test_parse_title_metadata_empty_title():
    """Empty title should return empty metadata without crashing."""
    meta = parse_title_metadata("")
    assert meta["resolution"] == ""
    assert meta["codec"] == ""
    assert meta["group"] == ""
    assert meta["hdr"] == []
    assert meta["audio"] == []
    assert meta["languages"] == []


def test_parse_title_metadata_special_characters():
    """Title with special characters should not crash."""
    meta = parse_title_metadata("Spider-Man.No.Way.Home.2021.1080p.BluRay.x264-SPARKS")
    assert meta["resolution"] == "1080p"
    assert meta["group"] == "SPARKS"


def test_parse_title_metadata_dots_and_dashes():
    """Complex title with many dots and dashes."""
    meta = parse_title_metadata(
        "Mr.Robot.S04E13.Series.Finale.Part.2.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTG"
    )
    assert meta["resolution"] == "1080p"


def test_parse_title_metadata_fallback_preserves_hyphenated_release_group():
    with patch("resources.lib.ptt.parse_title", return_value={}):
        meta = parse_title_metadata("Movie.2024.1080p.WEB-DL.x264-GROUP-NAME")

    assert meta["group"] == "GROUP-NAME"


def test_parse_title_metadata_fallback_preserves_underscored_release_group():
    with patch("resources.lib.ptt.parse_title", return_value={}):
        meta = parse_title_metadata("Movie.2024.1080p.WEB-DL.x264-GROUP_NAME")

    assert meta["group"] == "GROUP_NAME"


# --- Full search->filter pipeline with real PTT ---


def _all_pass_settings():
    """Settings that accept everything."""
    return {
        "resolutions": ["2160p", "1080p", "720p", "480p"],
        "hdr": ["HDR10", "HDR10+", "Dolby Vision", "HLG", "SDR"],
        "audio": ["Atmos", "TrueHD", "DTS-HD MA", "DTS:X", "DD+", "DD", "AAC"],
        "codecs": ["x265/HEVC", "x264/AVC", "AV1", "VP9", "MPEG-2"],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }


@patch("resources.lib.filter._get_filter_settings")
def test_filter_pipeline_realistic_titles(mock_settings):
    """Full pipeline: search results with realistic NZB titles, parsed by PTT."""
    mock_settings.return_value = {
        "resolutions": ["1080p"],
        "hdr": ["SDR"],
        "audio": ["Atmos", "TrueHD", "DTS-HD MA", "DTS:X", "DD+", "DD", "AAC"],
        "codecs": ["x265/HEVC", "x264/AVC"],
        "languages": [],
        "exclude_keywords": ["cam"],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": ["yify"],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result(
            "The.Matrix.1999.2160p.UHD.BluRay.REMUX.HDR.HEVC.DTS-HD.MA.7.1-FraMeSToR"
        ),
        _make_result("The.Matrix.1999.1080p.BluRay.x264.DTS-FGT"),
        _make_result("The.Matrix.1999.1080p.BluRay.x264-YIFY"),
        _make_result("The.Matrix.1999.CAM.x264-JUNK"),
        _make_result("The.Matrix.1999.720p.WEB-DL.x264-GRP"),
    ]
    filtered, _ = filter_results(results)
    # Should keep only the 1080p x264 FGT release (YIFY excluded, CAM excluded,
    # 2160p excluded by resolution, 720p excluded by resolution)
    assert len(filtered) == 1
    assert "FGT" in filtered[0]["title"]


@patch("resources.lib.filter._get_filter_settings")
def test_filter_pipeline_empty_results(mock_settings):
    mock_settings.return_value = _all_pass_settings()
    filtered, _ = filter_results([])
    assert filtered == []


@patch("resources.lib.filter._get_filter_settings")
def test_filter_pipeline_all_filtered_out(mock_settings):
    mock_settings.return_value = {
        "resolutions": ["480p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Movie.2024.2160p.BluRay.HEVC-GRP"),
        _make_result("Movie.2024.1080p.BluRay.x264-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 0


# --- Existing filter tests ---


@patch("resources.lib.filter._get_filter_settings")
def test_filter_excludes_resolution(mock_settings):
    mock_settings.return_value = {
        "resolutions": ["1080p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Movie.2024.2160p.BluRay.HEVC-GRP"),
        _make_result("Movie.2024.1080p.BluRay.x264-GRP"),
        _make_result("Movie.2024.720p.WEB-DL.x264-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1
    assert "1080p" in filtered[0]["title"]


@patch("resources.lib.filter._get_filter_settings")
def test_filter_excludes_keywords(mock_settings):
    mock_settings.return_value = {
        "resolutions": ["2160p", "1080p", "720p", "480p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": ["cam", "ts"],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Movie.2024.CAM.x264-GRP"),
        _make_result("Movie.2024.1080p.BluRay.x264-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1
    assert "BluRay" in filtered[0]["title"]


@patch("resources.lib.filter._get_filter_settings")
def test_filter_size_range(mock_settings):
    mock_settings.return_value = {
        "resolutions": ["2160p", "1080p", "720p", "480p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 1000,
        "max_size": 10000,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Small.Movie-GRP", size="500000000"),
        _make_result("Good.Movie-GRP", size="5000000000"),
        _make_result("Huge.Movie-GRP", size="50000000000"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1
    assert "Good" in filtered[0]["title"]


@patch("resources.lib.filter._get_filter_settings")
def test_filter_max_results(mock_settings):
    mock_settings.return_value = {
        "resolutions": ["2160p", "1080p", "720p", "480p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 2,
    }
    results = [_make_result("Movie.{}.1080p-GRP".format(i)) for i in range(5)]
    filtered, _ = filter_results(results)
    assert len(filtered) == 2


@patch("resources.lib.filter._get_filter_settings")
def test_filter_preferred_release_group_boosted(mock_settings):
    mock_settings.return_value = {
        "resolutions": ["2160p", "1080p", "720p", "480p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": ["SPARKS"],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Movie.2024.1080p.BluRay.x264-OTHER"),
        _make_result("Movie.2024.1080p.BluRay.x264-SPARKS"),
    ]
    filtered, _ = filter_results(results)
    assert filtered[0]["title"].endswith("-SPARKS")


@patch("resources.lib.filter._get_filter_settings")
def test_filter_exclude_release_group(mock_settings):
    mock_settings.return_value = {
        "resolutions": ["2160p", "1080p", "720p", "480p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": ["yify"],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Movie.2024.1080p.BluRay.x264-YIFY"),
        _make_result("Movie.2024.1080p.BluRay.x264-SPARKS"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1
    assert "SPARKS" in filtered[0]["title"]


# --- Edge case tests ---


@patch("resources.lib.filter._get_filter_settings")
def test_filter_very_large_size(mock_settings):
    """100GB+ files should not overflow or crash."""
    mock_settings.return_value = _all_pass_settings()
    results = [_make_result("Movie.2024.2160p.REMUX-GRP", size="107374182400")]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1


@patch("resources.lib.filter._get_filter_settings")
def test_filter_zero_size(mock_settings):
    """Zero-size results should pass if no min_size set."""
    mock_settings.return_value = _all_pass_settings()
    results = [_make_result("Movie.2024.1080p-GRP", size="0")]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1


@patch("resources.lib.filter._get_filter_settings")
def test_filter_empty_size(mock_settings):
    """Empty size string should not crash."""
    mock_settings.return_value = _all_pass_settings()
    results = [_make_result("Movie.2024.1080p-GRP", size="")]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1


@patch("resources.lib.filter._get_filter_settings")
def test_filter_require_keywords(mock_settings):
    mock_settings.return_value = {
        "resolutions": [],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": ["remux"],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Movie.2024.1080p.BluRay.REMUX.HEVC-GRP"),
        _make_result("Movie.2024.1080p.BluRay.x264-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1
    assert "REMUX" in filtered[0]["title"]


# --- Sort order tests ---


def test_sort_by_size_largest_first():
    results = [
        _make_result("Small", size="1000000000"),
        _make_result("Large", size="9000000000"),
    ]
    for r in results:
        r["_meta"] = parse_title_metadata(r["title"])
    settings = _all_pass_settings()
    settings["sort_order"] = 1
    sorted_r = _sort_results(results, settings)
    assert sorted_r[0]["title"] == "Large"


def test_sort_by_size_largest_first_tolerates_malformed_size():
    results = [
        _make_result("Bad", size="unknown"),
        _make_result("Large", size="9000000000"),
    ]
    for r in results:
        r["_meta"] = parse_title_metadata(r["title"])
    settings = _all_pass_settings()
    settings["sort_order"] = 1

    sorted_r = _sort_results(results, settings)

    assert [r["title"] for r in sorted_r] == ["Large", "Bad"]


def test_sort_by_size_smallest_first():
    results = [
        _make_result("Large", size="9000000000"),
        _make_result("Small", size="1000000000"),
    ]
    for r in results:
        r["_meta"] = parse_title_metadata(r["title"])
    settings = _all_pass_settings()
    settings["sort_order"] = 2
    sorted_r = _sort_results(results, settings)
    assert sorted_r[0]["title"] == "Small"


def test_sort_relevance_tolerates_malformed_size():
    results = [
        _make_result("Movie.2024.1080p.H264-GRP", size="not-a-number"),
        _make_result("Movie.2024.1080p.H264-GRP", size="1000000000"),
    ]
    for r in results:
        r["_meta"] = parse_title_metadata(r["title"])
    settings = _all_pass_settings()
    settings["sort_order"] = 0

    sorted_r = _sort_results(results, settings)

    assert len(sorted_r) == 2


def test_sort_relevance_preserves_order():
    results = [
        _make_result("First"),
        _make_result("Second"),
        _make_result("Third"),
    ]
    for r in results:
        r["_meta"] = parse_title_metadata(r["title"])
    settings = _all_pass_settings()
    settings["sort_order"] = 0
    sorted_r = _sort_results(results, settings)
    assert sorted_r[0]["title"] == "First"
    assert sorted_r[1]["title"] == "Second"
    assert sorted_r[2]["title"] == "Third"


# --- New tests ---


def test_parse_title_metadata_multiple_audio_codecs():
    """Real PTT parsing of a TrueHD Atmos title should detect both audio codecs."""
    meta = parse_title_metadata(
        "The.Dark.Knight.2008.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.Atmos.7.1-GROUP"
    )
    audio = meta["audio"]
    assert len(audio) >= 1, "Should detect at least one audio codec"
    # TrueHD and Atmos are both present; at least one of them should be recognized
    assert any(
        a in ("TrueHD", "Atmos") for a in audio
    ), "TrueHD.Atmos title should have TrueHD or Atmos in audio list"


@patch("resources.lib.filter._get_filter_settings")
def test_filter_tv_episode_title_with_season_episode(mock_settings):
    """TV episode titles in SxxExx format should pass resolution and codec filters."""
    mock_settings.return_value = {
        "resolutions": ["1080p"],
        "hdr": [],
        "audio": [],
        "codecs": ["x265/HEVC", "x264/AVC"],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Breaking.Bad.S05E14.Ozymandias.1080p.BluRay.x265.DTS-HD.MA-NTb"),
        _make_result("Breaking.Bad.S05E14.Ozymandias.720p.WEB-DL.x264-GRP"),
        _make_result("Breaking.Bad.S05E14.Ozymandias.2160p.BluRay.HEVC-SPARKS"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1, "Only the 1080p result should pass the resolution filter"
    assert "S05E14" in filtered[0]["title"], "Filtered result should be the TV episode"
    assert "1080p" in filtered[0]["title"]


@patch("resources.lib.filter._get_filter_settings")
def test_filter_no_resolution_detected_passes_when_all_enabled(mock_settings):
    """Results with no detected resolution should pass when all resolutions enabled."""
    mock_settings.return_value = _all_pass_settings()
    results = [
        _make_result("Some.Old.Movie.DVDRip.x264-GRP"),
        _make_result("Another.Release.HDTV.x264-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert (
        len(filtered) == 2
    ), "Results with no detected resolution should pass when all resolutions enabled"


@patch("resources.lib.filter._get_filter_settings")
def test_filter_combined_resolution_audio_codec(mock_settings):
    """Combined resolution + audio + codec filters should all apply simultaneously."""
    mock_settings.return_value = {
        "resolutions": ["1080p"],
        "hdr": [],
        "audio": ["DTS-HD MA"],
        "codecs": ["x265/HEVC"],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        # matches all three filters
        _make_result("Movie.2024.1080p.BluRay.HEVC.DTS-HD.MA.7.1-GRP"),
        # wrong codec (x264 instead of HEVC)
        _make_result("Movie.2024.1080p.BluRay.x264.DTS-HD.MA-GRP"),
        # wrong resolution
        _make_result("Movie.2024.720p.BluRay.HEVC.DTS-HD.MA-GRP"),
        # wrong audio
        _make_result("Movie.2024.1080p.BluRay.HEVC.AAC-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1, "Only the result matching all three filters should pass"
    assert "HEVC" in filtered[0]["title"], "Filtered result should contain HEVC"
    assert "DTS-HD" in filtered[0]["title"], "Filtered result should contain DTS-HD"


@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_attaches_meta_key(mock_settings):
    """filter_results should attach a _meta key to each result that passes."""
    mock_settings.return_value = _all_pass_settings()
    results = [
        _make_result("Movie.2024.1080p.BluRay.x264-GRP"),
        _make_result("Another.2023.2160p.UHD.BluRay.HEVC-SPARKS"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 2
    for item in filtered:
        assert "_meta" in item, "Each filtered result must have a _meta key"
        meta = item["_meta"]
        assert "resolution" in meta, "_meta must contain resolution"
        assert "codec" in meta, "_meta must contain codec"
        assert "audio" in meta, "_meta must contain audio list"
        assert "hdr" in meta, "_meta must contain hdr list"
        assert "group" in meta, "_meta must contain group"


@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_reuses_prefilled_meta(mock_settings):
    """filter_results should not reparse results that already have metadata."""
    mock_settings.return_value = _all_pass_settings()
    meta = {
        "resolution": "1080p",
        "hdr": [],
        "audio": [],
        "codec": "x264/AVC",
        "languages": [],
        "group": "GRP",
    }
    result = _make_result("Movie.2024.1080p.BluRay.x264-GRP")
    result["_meta"] = meta

    with patch(
        "resources.lib.filter.parse_title_metadata",
        side_effect=AssertionError("should reuse _meta"),
    ):
        filtered, all_parsed = filter_results([result])

    assert filtered == [result]
    assert all_parsed == [result]
    assert result["_meta"] is meta


@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_reparses_partial_prefilled_meta(mock_settings):
    """filter_results should only reuse parse-shaped metadata dicts."""
    mock_settings.return_value = _all_pass_settings()
    result = _make_result("Movie.2024.1080p.BluRay.x264-GRP")
    result["_meta"] = {}
    meta = {
        "resolution": "1080p",
        "hdr": [],
        "audio": [],
        "codec": "x264/AVC",
        "languages": [],
        "group": "GRP",
    }

    with patch("resources.lib.filter.parse_title_metadata", return_value=meta) as parse:
        filtered, all_parsed = filter_results([result])

    assert filtered == [result]
    assert all_parsed == [result]
    parse.assert_called_once_with(result["title"])
    assert result["_meta"] is meta


@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_reparses_malformed_prefilled_meta(mock_settings):
    """filter_results should reject fully-keyed metadata with unsafe value types."""
    mock_settings.return_value = _all_pass_settings()
    result = _make_result("Movie.2024.1080p.BluRay.x264-GRP")
    result["_meta"] = {
        "resolution": "1080p",
        "hdr": "HDR10",
        "audio": ["DTS"],
        "codec": "x264/AVC",
        "languages": "en",
        "group": object(),
    }
    meta = {
        "resolution": "1080p",
        "hdr": [],
        "audio": [],
        "codec": "x264/AVC",
        "languages": [],
        "group": "GRP",
    }

    with patch("resources.lib.filter.parse_title_metadata", return_value=meta) as parse:
        filtered, all_parsed = filter_results([result])

    assert filtered == [result]
    assert all_parsed == [result]
    parse.assert_called_once_with(result["title"])
    assert result["_meta"] is meta


@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_reparses_prefilled_meta_with_non_string_list_items(
    mock_settings,
):
    """filter_results should reject cached metadata with unsafe list contents."""
    mock_settings.return_value = _all_pass_settings()
    result = _make_result("Movie.2024.1080p.BluRay.x264-GRP")
    result["_meta"] = {
        "resolution": "1080p",
        "hdr": [{}],
        "audio": ["DTS"],
        "codec": "x264/AVC",
        "languages": ["en"],
        "group": "GRP",
    }
    meta = {
        "resolution": "1080p",
        "hdr": [],
        "audio": [],
        "codec": "x264/AVC",
        "languages": [],
        "group": "GRP",
    }

    with patch("resources.lib.filter.parse_title_metadata", return_value=meta) as parse:
        filtered, all_parsed = filter_results([result])

    assert filtered == [result]
    assert all_parsed == [result]
    parse.assert_called_once_with(result["title"])
    assert result["_meta"] is meta


@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_caches_duplicate_title_metadata(mock_settings):
    """filter_results should parse identical titles once per filtering pass."""
    mock_settings.return_value = _all_pass_settings()
    title = "Movie.2024.1080p.BluRay.x264-GRP"
    results = [
        _make_result(title, link="http://example.com/one.nzb"),
        _make_result(title, link="http://example.com/two.nzb"),
    ]
    meta = {
        "resolution": "1080p",
        "hdr": [],
        "audio": [],
        "codec": "x264/AVC",
        "languages": [],
        "group": "GRP",
    }

    with patch("resources.lib.filter.parse_title_metadata", return_value=meta) as parse:
        filtered, all_parsed = filter_results(results)

    assert filtered == results
    assert all_parsed == results
    assert parse.call_count == 1
    assert results[0]["_meta"] is meta
    assert results[1]["_meta"] == meta
    assert results[1]["_meta"] is not meta
    assert results[1]["_meta"]["hdr"] is not meta["hdr"]


@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_seeds_duplicate_title_cache_from_prefilled_meta(mock_settings):
    """A valid prefilled _meta should satisfy later duplicate titles."""
    mock_settings.return_value = _all_pass_settings()
    title = "Movie.2024.1080p.BluRay.x264-GRP"
    meta = {
        "resolution": "1080p",
        "hdr": [],
        "audio": [],
        "codec": "x264/AVC",
        "languages": [],
        "group": "GRP",
    }
    first = _make_result(title, link="http://example.com/one.nzb")
    first["_meta"] = meta
    second = _make_result(title, link="http://example.com/two.nzb")

    with patch(
        "resources.lib.filter.parse_title_metadata",
        side_effect=AssertionError("should reuse cached _meta"),
    ):
        filtered, all_parsed = filter_results([first, second])

    assert filtered == [first, second]
    assert all_parsed == [first, second]
    assert first["_meta"] is meta
    assert second["_meta"] == meta
    assert second["_meta"] is not meta


# --- Size parsing robustness tests ---


def test_matches_filters_non_numeric_size():
    """matches_filters should not crash on non-numeric size values."""
    result = {
        "title": "Movie.2024.1080p.BluRay.x264-GRP",
        "size": "not-a-number",
    }
    meta = parse_title_metadata(result["title"])
    settings = {
        "resolutions": [],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 100,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    # Should not raise, should return False (can't meet min_size)
    assert matches_filters(result, meta, settings) is False


def test_matches_filters_empty_size():
    """matches_filters should handle empty string size gracefully."""
    result = {
        "title": "Movie.2024.1080p.BluRay.x264-GRP",
        "size": "",
    }
    meta = parse_title_metadata(result["title"])
    settings = {
        "resolutions": [],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    assert matches_filters(result, meta, settings) is True


def test_matches_filters_none_size():
    """matches_filters should handle None size gracefully."""
    result = {
        "title": "Movie.2024.1080p.BluRay.x264-GRP",
        "size": None,
    }
    meta = parse_title_metadata(result["title"])
    settings = {
        "resolutions": [],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    assert matches_filters(result, meta, settings) is True


@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_returns_all_parsed(mock_settings):
    """filter_results should return (filtered, all_parsed) tuple."""
    mock_settings.return_value = {
        "resolutions": ["1080p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        {"title": "Movie.2024.1080p.BluRay.x264-GRP", "size": "5000000000"},
        {"title": "Movie.2024.720p.BluRay.x264-GRP", "size": "3000000000"},
    ]
    filtered, all_parsed = filter_results(results)
    assert len(filtered) == 1  # Only 1080p passes
    assert len(all_parsed) == 2  # Both have _meta attached


@patch("resources.lib.filter.xbmc")
@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_log_counts_before_max_results_truncation(
    mock_settings, mock_xbmc
):
    mock_settings.return_value = {
        "resolutions": [],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "sort_order": 0,
        "max_results": 1,
    }
    results = [
        {"title": "Movie.2024.1080p.BluRay.x264-GRP", "size": "5000000000"},
        {"title": "Other.2024.1080p.BluRay.x264-GRP", "size": "3000000000"},
    ]

    filtered, all_parsed = filter_results(results)

    assert len(filtered) == 1
    assert len(all_parsed) == 2
    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "Filtered 2 -> 2 results (showing 1)" in logged


@patch("resources.lib.filter.telemetry.log_timing")
@patch("resources.lib.filter._get_filter_settings")
def test_filter_results_logs_timing(
    mock_settings,
    mock_log_timing,
):
    mock_settings.return_value = _all_pass_settings()
    results = [
        {"title": "Movie.2024.1080p.BluRay.x264-GRP", "size": "5000000000"},
        {"title": "Other.2024.720p.BluRay.x264-GRP", "size": "3000000000"},
    ]

    filtered, all_parsed = filter_results(results)

    assert len(filtered) == 2
    assert len(all_parsed) == 2
    assert mock_log_timing.call_count == 1
    label, elapsed_ms = mock_log_timing.call_args.args
    assert label == "filter_results"
    assert elapsed_ms >= 0
    assert mock_log_timing.call_args.kwargs == {
        "input": 2,
        "matched": 2,
        "shown": 2,
    }


# --- _get_filter_settings tests (direct coverage of the Kodi-settings reader) ---


@patch("xbmcaddon.Addon")
def test_get_filter_settings_collects_enabled_resolutions_and_codecs(mock_addon):
    """When specific resolution / codec toggles are "true", the
    corresponding labels show up in the returned lists; disabled
    toggles don't leak through."""
    from resources.lib.filter import _get_filter_settings

    enabled = {
        "filter_1080p": "true",
        "filter_2160p": "true",
        "filter_hevc": "true",
        "filter_av1": "true",
        "filter_dolby_vision": "true",
        "filter_atmos": "true",
        "filter_english": "true",
    }
    mock_addon.return_value.getSetting.side_effect = lambda k: enabled.get(k, "false")

    settings = _get_filter_settings()

    assert "1080p" in settings["resolutions"]
    assert "2160p" in settings["resolutions"]
    assert "720p" not in settings["resolutions"]
    assert "x265/HEVC" in settings["codecs"]
    assert "AV1" in settings["codecs"]
    assert "x264/AVC" not in settings["codecs"]
    assert "Dolby Vision" in settings["hdr"]
    assert "HDR10" not in settings["hdr"]
    assert "Atmos" in settings["audio"]
    assert "DD" not in settings["audio"]
    # Languages are stored as ISO 639-1 codes (matching PTT's output)
    # rather than UI labels — see TODO.md §H.2-H11.
    assert "en" in settings["languages"]
    assert "es" not in settings["languages"]


@patch("xbmcaddon.Addon")
def test_get_filter_settings_csv_fields_split_and_stripped(mock_addon):
    """Comma-separated settings (exclude_keywords, release_group, etc.)
    must be split on commas, whitespace trimmed, and empty entries
    dropped."""
    from resources.lib.filter import _get_filter_settings

    raw = {
        "filter_exclude_keywords": "CAM, HDCAM ,  ,TS",
        "filter_require_keywords": "",
        "filter_release_group": "GRP1,GRP2",
        "filter_exclude_release_group": "  NUKED  , ",
    }
    mock_addon.return_value.getSetting.side_effect = lambda k: raw.get(k, "")

    settings = _get_filter_settings()

    assert settings["exclude_keywords"] == ["cam", "hdcam", "ts"]
    assert settings["require_keywords"] == []
    assert settings["release_group"] == ["grp1", "grp2"]
    assert settings["exclude_release_group"] == ["nuked"]


@patch("xbmcaddon.Addon")
def test_get_filter_settings_int_fields_fall_back_on_non_numeric(mock_addon):
    """Non-numeric strings for int-valued settings must fall back to the
    documented defaults rather than raising ValueError."""
    from resources.lib.filter import _get_filter_settings

    raw = {
        "filter_min_size": "not a number",
        "filter_max_size": "",
        "max_results": "",
    }
    mock_addon.return_value.getSetting.side_effect = lambda k: raw.get(k, "")

    settings = _get_filter_settings()

    assert settings["min_size"] == 0
    assert settings["max_size"] == 0
    # max_results default is 25 per _get_filter_settings
    assert settings["max_results"] == 25


@patch("xbmcaddon.Addon")
def test_get_filter_settings_returns_empty_lists_when_nothing_enabled(mock_addon):
    """All toggles "false" / unset must produce empty lists rather than
    partial junk — this is the fresh-install shape."""
    from resources.lib.filter import _get_filter_settings

    mock_addon.return_value.getSetting.side_effect = lambda k: ""

    settings = _get_filter_settings()

    assert settings["resolutions"] == []
    assert settings["hdr"] == []
    assert settings["audio"] == []
    assert settings["codecs"] == []
    assert settings["languages"] == []
    assert settings["exclude_keywords"] == []
    assert settings["require_keywords"] == []
    assert settings["release_group"] == []
    assert settings["exclude_release_group"] == []


@patch("xbmcaddon.Addon", side_effect=RuntimeError("Kodi settings unavailable"))
def test_filter_results_uses_script_settings_getter_without_kodi_addon(mock_addon):
    """RunScript playback can enter without a safe Kodi addon settings
    context, so filtering must be able to read settings from the script
    settings adapter instead."""
    settings = {
        "filter_1080p": "true",
        "filter_hevc": "true",
        "max_results": "5",
    }

    def script_setting(key, default=""):
        return settings.get(key, default)

    results = [
        _make_result(
            "The.Odyssey.2026.1080p.WEB-DL.DDP5.1.H.265-GROUP.mkv",
            size=str(8 * 1024**3),
        )
    ]

    filtered, all_parsed = filter_results(results, settings_getter=script_setting)

    mock_addon.assert_not_called()
    assert filtered == results
    assert all_parsed == results


# --- Fix #5: cross-validate min_size / max_size + decimal parse ----------


def _build_size_settings(min_raw, max_raw):
    """Lookup table for ``settings_getter`` style filter resolution.

    Only the size keys are populated; everything else defaults to "" so
    every other filter is disabled and we isolate the size handling.
    """
    overrides = {
        "filter_min_size": str(min_raw),
        "filter_max_size": str(max_raw),
    }

    def _getter(key, default=""):
        return overrides.get(key, default)

    return _getter


def test_get_filter_settings_inverted_range_zeros_both_bounds():
    """min_size > max_size silently rejected everything before Fix #5.
    Now both bounds zero out (filter disabled) and we log a warning."""
    from resources.lib.filter import _get_filter_settings

    getter = _build_size_settings(min_raw=10000, max_raw=5000)

    with patch("resources.lib.filter.xbmc") as mock_xbmc:
        settings = _get_filter_settings(settings_getter=getter)

    assert settings["min_size"] == 0
    assert settings["max_size"] == 0
    log_lines = [c.args[0] for c in mock_xbmc.log.call_args_list]
    assert any("filter_min_size=10000" in line for line in log_lines)
    assert any("filter_max_size=5000" in line for line in log_lines)


def test_get_filter_settings_open_ended_floor_preserved():
    """min>0 with max=0 (no upper bound) is a valid configuration —
    the inverted-range check must NOT zero it out."""
    from resources.lib.filter import _get_filter_settings

    getter = _build_size_settings(min_raw=1000, max_raw=0)

    settings = _get_filter_settings(settings_getter=getter)

    assert settings["min_size"] == 1000
    assert settings["max_size"] == 0


def test_get_filter_settings_decimal_input_truncates_to_int():
    """A user typing "1.5" into a number field must parse as 1, not
    silently fall back to 0 (the old behavior). Fix #5."""
    from resources.lib.filter import _get_filter_settings

    getter = _build_size_settings(min_raw="1.5", max_raw="100.9")

    settings = _get_filter_settings(settings_getter=getter)

    assert settings["min_size"] == 1
    assert settings["max_size"] == 100


def test_get_filter_settings_unparseable_input_falls_back_to_default():
    """Garbage input (still) falls back to the documented default
    rather than crashing."""
    from resources.lib.filter import _get_filter_settings

    overrides = {"filter_min_size": "abc", "filter_max_size": "xyz"}

    def getter(key, default=""):
        return overrides.get(key, default)

    settings = _get_filter_settings(settings_getter=getter)

    assert settings["min_size"] == 0
    assert settings["max_size"] == 0


def test_get_filter_settings_non_finite_float_input_falls_back_to_default():
    from resources.lib.filter import _get_filter_settings

    overrides = {"filter_min_size": "1e309", "filter_max_size": "nan"}

    def getter(key, default=""):
        return overrides.get(key, default)

    settings = _get_filter_settings(settings_getter=getter)

    assert settings["min_size"] == 0
    assert settings["max_size"] == 0


def test_get_filter_settings_valid_range_unchanged():
    """A normal min<max configuration must pass through cleanly."""
    from resources.lib.filter import _get_filter_settings

    getter = _build_size_settings(min_raw=1000, max_raw=10000)

    settings = _get_filter_settings(settings_getter=getter)

    assert settings["min_size"] == 1000
    assert settings["max_size"] == 10000


# ---------------------------------------------------------------------------
# release_is_pack: distinguish multi-episode / season packs from single-file
# releases so the #282 stub size-guard can skip packs (a pack legitimately
# serves one sub-pack-sized episode).
# ---------------------------------------------------------------------------


def test_release_is_pack_false_for_movie():
    assert release_is_pack("The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP") is False


def test_release_is_pack_false_for_single_episode():
    assert release_is_pack("Some.Show.S01E05.1080p.WEB-DL.x264-GROUP") is False


def test_release_is_pack_true_for_full_season():
    assert release_is_pack("Some.Show.S01.1080p.WEB-DL.x264-GROUP") is True


def test_release_is_pack_true_for_multi_episode_range():
    assert release_is_pack("Some.Show.S01E01-E10.1080p.WEB-DL.x264-GROUP") is True


def test_release_is_pack_true_for_complete_series():
    assert release_is_pack("Some.Show.S01-S05.COMPLETE.1080p.WEB-DL.x264-GROUP") is True


def test_release_is_pack_true_for_season_tagless_complete_collection():
    """A 'Complete Collection/Series' release carries no Sxx tag (PTT parses
    seasons=[] episodes=[] complete=True). It is still a pack — picking one
    episode out of it must not trip the #282 single-file stub guard. The
    ``complete`` flag only counts WITH a collection/series/box-set token (see
    test_release_is_pack_false_for_complete_titled_movie)."""
    assert release_is_pack("Friends.Complete.Collection.1080p.BluRay-GROUP") is True
    assert release_is_pack("The.Office.US.Complete.Series.1080p.WEB-DL-GROUP") is True
    assert release_is_pack("Friends.Complete.Box.Set.1080p.BluRay-GRP") is True


def test_release_is_pack_false_for_complete_titled_movie():
    """A single-file movie whose title contains a standalone 'Complete' token
    (PTT sets complete=True, seasons=[], episodes=[]) must NOT be treated as a
    pack — otherwise it skips the #282 stub guard and the tiny nzbdav job-start
    stub can be streamed for those titles (e.g. 'Complete Unknown')."""
    assert release_is_pack("Complete.Unknown.2024.1080p.BluRay.x264-GROUP") is False
    assert release_is_pack("Some.Movie.Complete.2024.1080p.BluRay-GRP") is False


def test_release_is_pack_false_for_single_episode_tagged_complete():
    """#340 review: a single SxxExx that also carries a 'COMPLETE' token (PTT:
    one season, one episode, complete=True) is one episode, not a pack — its
    advertised size is for that episode, so the stub guard must stay active."""
    assert release_is_pack("Some.Show.S01E05.COMPLETE.1080p.WEB-DL-GRP") is False


def test_release_is_pack_false_for_collection_word_in_movie_title():
    """#340 review: a movie whose own title contains a collection/series word
    (with 'Complete' separated by the year, or no 'Complete' at all) must NOT be
    classified as a pack — only adjacent 'Complete Collection'-style phrasing is
    a pack signal."""
    assert release_is_pack("The.Collection.2012.COMPLETE.1080p.BluRay-GRP") is False
    assert release_is_pack("Marvel.Collection.2024.1080p.BluRay-GRP") is False


def test_release_is_pack_false_for_single_episode_with_pack_phrase():
    """#340 Codex review: a SINGLE-episode release whose NAME contains a pack
    phrase ("Miniseries", "Box Set") still advertises a one-episode size, so it
    must NOT be classified as a pack — the phrase branch must not short-circuit
    the single-episode-tag check, or the #282 stub guard would be skipped and a
    job-start stub could stream. The episode tag overrides the phrase."""
    assert release_is_pack("Chernobyl.Miniseries.S01E01.1080p.WEB.x264-GRP") is False
    assert release_is_pack("Some.Show.Box.Set.S01E05.1080p.WEB-DL-GRP") is False
    assert release_is_pack("Some.Show.Boxset.S01E05.1080p.WEB-DL-GRP") is False


def test_release_is_pack_true_for_season_tagless_miniseries():
    """A whole-season miniseries with NO single-episode tag stays a pack: the
    season-tagless phrase ("Miniseries") classifies it (and the bare-season PTT
    check backs it up), so the stub guard correctly skips it."""
    assert release_is_pack("Chernobyl.Miniseries.1080p.WEB-DL-GRP") is True
    assert release_is_pack("Chernobyl.Miniseries.S01.1080p.WEB-DL-GRP") is True


def test_release_is_pack_true_for_standalone_movie_collection_keywords():
    """#340 Codex review: movie-collection packs named with only the collection
    keyword ("Trilogy", "Quadrilogy", "Anthology") carry no season/episode tag,
    so PTT yields nothing and the phrase's "Complete"-adjacency requirement
    misses them -- the resolver would then apply the single-file floor and reject
    a real 20 GB movie out of a 60 GB trilogy as a stub. These unambiguous
    collection words are pack signals on their own."""
    assert release_is_pack("The.Matrix.Trilogy.1080p.BluRay.x264-GRP") is True
    assert release_is_pack("Alien.Quadrilogy.1080p.BluRay-GRP") is True
    assert release_is_pack("The.Twilight.Zone.Anthology.1080p.BluRay-GRP") is True
    assert release_is_pack("Nolan.Filmography.2160p.UHD.BluRay-GRP") is True


def test_release_is_pack_true_for_limited_series():
    """#340 Codex review: season-tag-less "Limited Series" packs (e.g.
    "Chernobyl.Limited.Series") parse to no seasons/episodes in PTT, so they are
    recognized via the phrase like "Mini Series"."""
    assert release_is_pack("Chernobyl.Limited.Series.1080p.WEB-DL-GRP") is True
    assert release_is_pack("The.Queens.Gambit.LimitedSeries.1080p.WEB-GRP") is True


def test_release_is_pack_false_for_single_episode_collection_keyword():
    """A standalone collection keyword must still defer to a single-episode tag:
    a one-episode release is not a pack even if its name contains a collection
    word, so the stub guard stays active."""
    assert release_is_pack("Some.Anthology.Show.S01E03.1080p.WEB-DL-GRP") is False


def test_release_is_pack_true_for_reversed_ordinal_season_complete():
    """#340 Codex review: a reversed-phrase full-season pack with an ordinal or
    spelled cardinal between the keyword and "Complete" ("Season One Complete",
    "Season Two Complete", "Season First Complete") parses to no seasons/episodes
    in PTT (the spelled number is not recognized), so the reversed phrase branch
    must allow the ordinal/cardinal slot or the single-file floor would reject a
    real episode of the season pack."""
    assert release_is_pack("Some.Show.Season.One.Complete.1080p.WEB-DL-GRP") is True
    assert release_is_pack("Some.Show.Season.Two.Complete.720p.HDTV-GRP") is True
    assert release_is_pack("Some.Show.Season.First.Complete.1080p.WEB-GRP") is True


def test_release_is_pack_true_for_reversed_complete_series_phrasing():
    """'<Show>.Series.Complete' (words adjacent, reversed order) is still a
    whole-series pack."""
    assert release_is_pack("Breaking.Bad.Series.Complete.1080p.BluRay-GRP") is True


def test_release_is_pack_true_for_ordinal_complete_season():
    """#340 review: ordinal complete-season packs ("The Complete First Season",
    "Complete 2nd Season", "Complete Final Season") carry an ordinal between
    'complete' and 'season' and leave PTT seasons/episodes empty. They are still
    whole-season packs and must skip the single-file stub guard."""
    assert release_is_pack("Some.Show.The.Complete.First.Season.1080p-GRP") is True
    assert release_is_pack("Some.Show.Complete.2nd.Season.720p-GRP") is True
    assert release_is_pack("Some.Show.Complete.Final.Season.1080p-GRP") is True
    # A movie's stray "Complete" + non-keyword word stays NOT-a-pack.
    assert release_is_pack("Complete.Unknown.2024.1080p.BluRay-GROUP") is False


def test_release_is_pack_true_for_nxn_episode_range():
    """PTT collapses '1x01-1x10' to seasons=[1] episodes=[1], so the
    episode/season-count checks miss it. _episode_tags expands the NxN range,
    so the whole-season pack is recognized and the #282 stub guard is skipped."""
    assert release_is_pack("Some.Show.1x01-1x10.1080p.WEB-DL-GRP") is True
    assert release_is_pack("Some.Show.01x01-01x10.1080p-GRP") is True


def test_release_is_pack_false_for_single_nxn_episode():
    assert release_is_pack("Some.Show.1x05.1080p-GRP") is False


def test_release_is_pack_false_for_empty_or_nonstring():
    assert release_is_pack("") is False
    assert release_is_pack(None) is False


def test_release_is_pack_true_for_spelled_ordinal_season():
    """#340 Codex review: a whole-season pack named with a spelled
    ordinal/cardinal and "Season" but no explicit "Complete" ("First Season",
    "The Second Season", "3rd Season", "Season Two") parses to no
    seasons/episodes in PTT (the spelled number is not recognized), so the
    phrase gate must treat an ordinal/cardinal adjacent to "season(s)" as a pack
    or the single-file floor would reject a real episode of the season pack."""
    assert release_is_pack("Some.Show.First.Season.1080p.WEB-DL-GRP") is True
    assert release_is_pack("Some.Show.The.Second.Season.1080p.WEB-DL-GRP") is True
    assert release_is_pack("Some.Show.Third.Season.720p.HDTV-GRP") is True
    assert release_is_pack("Some.Show.3rd.Season.1080p.WEB-GRP") is True
    assert release_is_pack("Some.Show.Season.Two.1080p.WEB-DL-GRP") is True


def test_release_is_pack_false_for_ordinal_in_movie_title():
    """The ordinal/cardinal season signal requires "season" ADJACENT to the
    ordinal, so a movie whose title merely begins with an ordinal word
    ("First Blood", "First Man", "Second Act") does NOT match and keeps the #282
    stub guard active (#340 Codex review)."""
    assert release_is_pack("First.Blood.1982.1080p.BluRay.x264-GRP") is False
    assert release_is_pack("First.Man.2018.1080p.BluRay.x264-GRP") is False
    assert release_is_pack("Second.Act.2018.1080p.WEB-DL-GRP") is False


def test_release_is_pack_false_for_single_episode_ordinal_season():
    """A single-episode release whose name contains a spelled-ordinal season
    phrase ("First Season" + S01E05) keeps the stub guard: the episode tag
    overrides the phrase (#340 Codex review)."""
    assert release_is_pack("Some.Show.First.Season.S01E05.1080p.WEB-DL-GRP") is False


def test_release_is_pack_false_for_last_final_season_movie_titles():
    """#340 Codex review: 'The Last Season' / 'Final Season' are real single-movie
    titles. The bare ordinal+season branch must NOT treat 'last'/'final' season as
    a pack (only numeric/positional ordinals), or the stub guard is skipped for
    those movies. With an adjacent 'Complete' they stay packs."""
    assert release_is_pack("The.Last.Season.2007.1080p.BluRay.x264-GRP") is False
    assert release_is_pack("Final.Season.2024.1080p.WEB-DL-GRP") is False
    # "Complete Final Season" is unambiguously a whole-season pack -> still True.
    assert release_is_pack("Some.Show.Complete.Final.Season.1080p-GRP") is True
    # Numeric/positional season packs are unaffected.
    assert release_is_pack("Some.Show.First.Season.1080p-GRP") is True
    assert release_is_pack("Some.Show.Season.Two.1080p-GRP") is True


def test_release_is_pack_true_for_complete_batch():
    """#282 follow-up: anime/TV 'Complete Batch' releases are whole-season packs
    (PTT leaves seasons/episodes empty). 'batch(es)' is a complete-adjacent pack
    keyword, so the resolver skips the single-file floor for them."""
    assert release_is_pack("Some.Show.Complete.Batch.1080p-GRP") is True
    assert release_is_pack("Some.Show.Batch.Complete.1080p-GRP") is True
    assert release_is_pack("Some.Show.Complete.Batches.1080p-GRP") is True


def test_release_is_pack_false_for_bare_batch_and_batch_movie():
    """'batch' is ONLY a pack signal adjacent to 'Complete'. A bare 'Batch', a
    movie titled 'The.Batch', and the substring 'Batchelor' stay non-pack (keep
    the #282 stub guard); a single episode of a batch keeps it too."""
    assert release_is_pack("Some.Show.Batch.1080p-GRP") is False
    assert release_is_pack("The.Batch.2024.1080p.BluRay-GRP") is False
    assert release_is_pack("Some.Movie.Batchelor.2024-GRP") is False
    assert release_is_pack("Some.Show.Complete.Batch.S01E05.1080p-GRP") is False


def test_release_is_pack_true_for_spelled_series():
    """#282 follow-up: UK-style spelled 'Series One' / 'First Series' whole-series
    packs (PTT leaves seasons/episodes empty) must be recognized, mirroring the
    spelled-season handling, or the single-file floor rejects real episodes."""
    assert release_is_pack("Doctor.Who.Series.One.1080p-GRP") is True
    assert release_is_pack("Some.Show.First.Series.1080p-GRP") is True
    assert release_is_pack("Some.Show.Series.Two.1080p-GRP") is True
    assert release_is_pack("Some.Show.Second.Series.720p-GRP") is True
    assert release_is_pack("Some.Show.3rd.Series.1080p-GRP") is True


def test_release_is_pack_false_for_series_in_movie_title():
    """A bare 'Series' in a movie title (no adjacent ordinal/cardinal) stays
    non-pack; a single episode of a spelled-series pack keeps the guard too."""
    assert release_is_pack("The.Series.2024.1080p-GRP") is False
    assert release_is_pack("Series.2024.1080p-GRP") is False
    assert release_is_pack("Doctor.Who.Series.One.S01E05.1080p-GRP") is False
