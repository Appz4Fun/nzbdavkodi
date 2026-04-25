# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import patch

from resources.lib.filter import (
    _sort_results,
    filter_results,
    matches_filters,
    parse_title_metadata,
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
