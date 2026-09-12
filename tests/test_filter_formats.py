# SPDX-License-Identifier: GPL-3.0-or-later
"""Release-name regressions and filter choices for uncommon media formats."""

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest
from resources.lib import filter as filters


def _title(tags):
    return "Example.Movie.2024.{}.BluRay.REMUX-TEST".format(tags)


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("FLAC.1.0", ["FLAC"]),
        ("LPCM.2.0", ["PCM"]),
        ("Opus.5.1", ["OPUS"]),
        ("MP3", ["MP3"]),
        ("ALAC", ["ALAC"]),
        ("Vorbis", ["Vorbis"]),
        ("MP2", ["MP2"]),
        ("WMA", ["WMA"]),
        ("AC4", ["AC-4"]),
        ("AC-4", ["AC-4"]),
        ("DTS.5.1", ["DTS"]),
        ("DTS-HD.HR.5.1", ["DTS-HD HR"]),
        ("DTS-HD.MA.7.1", ["DTS-HD MA"]),
        ("DTS:X.7.1", ["DTS:X"]),
        ("DTS-X.7.1", ["DTS:X"]),
        ("TrueHD.Atmos", ["Atmos", "TrueHD"]),
        ("E-AC-3", ["DD+"]),
    ],
)
def test_audio_tags_keep_their_own_format(tag, expected):
    assert (
        filters.parse_title_metadata(_title("1080p.HEVC." + tag))["audio"] == expected
    )


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("HDR", ["HDR10"]),
        ("HDR10", ["HDR10"]),
        ("HDR+", ["HDR10+"]),
        ("HDRPlus", ["HDR10+"]),
        ("HDR10P", ["HDR10+"]),
        ("HDR10+", ["HDR10+"]),
        ("HDR10.Plus", ["HDR10+"]),
        ("HLG", ["HLG"]),
        ("SDR", ["SDR"]),
        ("DV.HDR10P", ["Dolby Vision", "HDR10+"]),
        ("Dolby.Vision.HDR", ["Dolby Vision", "HDR10"]),
        ("DoVi", ["Dolby Vision"]),
        ("10bit", []),
        ("", []),
    ],
)
def test_hdr_aliases_do_not_invent_dolby_vision_or_sdr(tag, expected):
    assert filters.parse_title_metadata(_title("2160p.HEVC." + tag))["hdr"] == expected


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("2560x1440", "1440p"),
        ("1440p", "1440p"),
        ("8K", "4320p"),
        ("7680x4320", "4320p"),
        ("1080i", "1080p"),
        ("720i", "720p"),
        ("576i", "576p"),
    ],
)
def test_resolution_aliases_share_the_correct_filter(tag, expected):
    assert filters.parse_title_metadata(_title(tag + ".HEVC"))["resolution"] == expected


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("XviD", "MPEG-4 ASP"),
        ("DivX", "MPEG-4 ASP"),
        ("MPEG4", "MPEG-4 ASP"),
        ("MPEG2", "MPEG-2"),
        ("MPEG-1", "MPEG-1"),
        ("VC-1", "VC-1"),
        ("WVC1", "VC-1"),
        ("VP8", "VP8"),
        ("WMV3", "WMV"),
        ("H.263", "H.263"),
        ("H.266", "VVC"),
        ("VVC", "VVC"),
        ("Theora", "Theora"),
        ("MJPEG", "MJPEG"),
    ],
)
def test_extra_video_codecs_have_stable_filter_values(tag, expected):
    assert filters.parse_title_metadata(_title("1080p." + tag))["codec"] == expected


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("French", ["fr"]),
        ("fr", ["fr"]),
        ("fReNcH", ["fr"]),
        ("Latino", ["es"]),
        ("latino", ["es"]),
        ("Spanish.Latin", ["es"]),
        ("Cantonese", ["yue"]),
        ("Urdu", ["ur"]),
        ("Urdo", ["ur"]),
        ("Malay", ["ms"]),
        ("Malayalam", ["ml"]),
        ("Swedish", ["sv"]),
        ("EN.FR", ["en", "fr"]),
    ],
)
def test_languages_are_case_insensitive_and_preserve_identity(tag, expected):
    assert (
        filters.parse_title_metadata(_title("1080p.HEVC." + tag))["languages"]
        == expected
    )


@pytest.mark.parametrize(
    "title",
    [
        _title("1080p.HEVC.HE-AAC"),
        _title("1080p.HEVC.DTS-HD.HR"),
        _title("1080p.HEVC.DTS-HD.HRA"),
        "Movie.2024.1080p.WEB-DL.HE-AAC",
        "No.Good.Deed.S01E01.1080p.WEB-DL.x264.AAC-TEST",
        "La.Brea.S01E01.1080p.WEB-DL.x264.AAC-TEST",
        "It.1080p.BluRay.x264.AAC-TEST",
        "Movie.2024.1080p.WEB-DL.x264.AAC-Vi.mkv",
        "Movie.2024.1080p.WEB-DL.x264.AAC-Vi-TEST.mkv",
    ],
)
def test_languages_do_not_come_from_audio_tags_titles_or_release_groups(title):
    meta = filters.parse_title_metadata(title)
    assert meta["languages"] == []
    settings = filters._get_filter_settings(lambda key, default="": default)
    settings["languages"] = ["en"]
    assert filters._language_filter_passes(meta, settings)


@pytest.mark.parametrize("tag", ["HE-AAC", "DTS-HD.HR", "DTS-HD.HRA"])
def test_language_after_ambiguous_audio_tag_still_matches(tag):
    meta = filters.parse_title_metadata(_title("1080p.HEVC." + tag + ".fr"))
    assert meta["languages"] == ["fr"]


def test_yearless_episode_keeps_explicit_language_after_episode_marker():
    meta = filters.parse_title_metadata(
        "No.Good.Deed.S01E01.EN.FR.1080p.WEB-DL.x264.AAC-TEST"
    )
    assert meta["languages"] == ["en", "fr"]


@pytest.mark.parametrize("tag,language", [("HE", "he"), ("HR", "hr"), ("FR", "fr")])
def test_language_after_hyphenated_source_is_not_a_group_extension(tag, language):
    meta = filters.parse_title_metadata("Movie.2024.1080p.WEB-DL." + tag)
    assert meta["languages"] == [language]


@pytest.mark.parametrize(
    "tag,option", [("Cantonese", "filter_cantonese"), ("Urdu", "filter_urdu")]
)
def test_chinese_selection_also_accepts_requested_language_aliases(tag, option):
    for enabled in [option, "filter_chinese"]:
        settings = filters._get_filter_settings(
            lambda key, default="", selected=enabled: (
                "true" if key == selected else "false"
            )
        )
        meta = filters.parse_title_metadata(_title("1080p.HEVC." + tag))
        assert filters._language_filter_passes(meta, settings)
    settings["languages"] = ["fr"]
    assert not filters._language_filter_passes(meta, settings)


@pytest.mark.parametrize(
    "field,setting,predicate,missing,unlisted,known",
    [
        ("audio", "audio", filters._audio_filter_passes, [], ["FUTURE_AUDIO"], ["AAC"]),
        ("hdr", "hdr", filters._hdr_filter_passes, [], ["FUTURE_HDR"], ["SDR"]),
        (
            "resolution",
            "resolution",
            filters._resolution_filter_passes,
            "",
            "9999p",
            "1080p",
        ),
        (
            "codec",
            "codec",
            filters._codec_filter_passes,
            "",
            "FUTURE_CODEC",
            "x264/AVC",
        ),
        ("languages", "language", filters._language_filter_passes, [], ["zz"], ["en"]),
    ],
)
def test_unknown_toggle_controls_only_missing_and_unlisted_values(
    field, setting, predicate, missing, unlisted, known
):
    settings = filters._get_filter_settings(lambda key, default="": default)
    assert predicate({field: missing}, settings)
    assert predicate({field: unlisted}, settings)
    settings["unknown_" + setting] = False
    assert not predicate({field: missing}, settings)
    assert not predicate({field: unlisted}, settings)
    assert predicate({field: known}, settings)


def test_unknown_hdr_is_independent_of_explicit_sdr():
    settings = filters._get_filter_settings(lambda key, default="": default)
    settings["hdr"] = ["HDR10"]
    assert filters._hdr_filter_passes({"hdr": []}, settings)
    assert not filters._hdr_filter_passes({"hdr": ["SDR"]}, settings)


def test_other_audio_does_not_override_a_disabled_known_audio_format():
    settings = filters._get_filter_settings(lambda key, default="": default)
    settings["audio"] = ["AAC"]
    assert settings["unknown_audio"] is True
    assert not filters._audio_filter_passes({"audio": ["FLAC"]}, settings)
    assert filters._audio_filter_passes({"audio": []}, settings)


def test_custom_remux_tiers_override_defaults_and_allow_an_empty_tier():
    settings = filters._get_filter_settings(
        lambda key, default="": {
            "filter_remux_tier_1": "MyGroup",
            "filter_remux_tier_2": "",
            "filter_remux_tier_3": "NCmt",
        }.get(key, default)
    )
    titles = [
        "Movie.2024.2160p.REMUX.HEVC.FLAC-" + group
        for group in ["CiNEPHiLES", "NCmt", "MyGroup"]
    ]
    assert _sorted_titles(titles, settings) == list(reversed(titles))


def test_chinatown_hybrid_survives_defaults_with_legacy_profile():
    title = (
        "Chinatown.1974.Hybrid.2160p.UHD.Blu-ray.Remux."
        "DV.HDR10P.HEVC.FLAC.1.0-CiNEPHiLES"
    )
    row = {"title": title, "size": "82796794060"}
    filtered, _ = filters.filter_results(
        [row], settings_getter=lambda key, default="": default
    )
    assert filtered == [row]
    assert row["_meta"]["hdr"] == ["Dolby Vision", "HDR10+"]
    assert row["_meta"]["audio"] == ["FLAC"]


def test_parser_failure_keeps_new_format_detection():
    with patch("resources.lib.ptt.parse_title", side_effect=ValueError("bad parser")):
        meta = filters.parse_title_metadata(_title("2560x1440.VC-1.HDR10P.LPCM.fr"))
    assert meta["resolution"] == "1440p"
    assert meta["codec"] == "VC-1"
    assert meta["audio"] == ["PCM"]
    assert meta["hdr"] == ["HDR10+"]
    assert meta["languages"] == ["fr"]


def test_settings_expose_unknowns_and_a_separate_languages_tab_after_quality():
    root = ET.parse(
        Path(__file__).resolve().parents[1]
        / "repo/plugin.video.nzbdav/resources/settings.xml"
    ).getroot()
    for kind in ["resolution", "hdr", "audio", "codec", "language"]:
        setting = root.find(".//setting[@id='filter_unknown_{}']".format(kind))
        assert setting is not None
        assert setting.findtext("default") == "true"
    categories = [category.get("id") for category in root.findall(".//category")]
    assert categories[categories.index("quality_filters") + 1] == "language_filters"


def _sorted_titles(tags, overrides=None):
    settings = filters._get_filter_settings(lambda key, default="": default)
    settings.update(overrides or {})
    rows = [
        {
            "title": title,
            "size": "5000000000",
            "_meta": filters.parse_title_metadata(title),
        }
        for title in tags
    ]
    return [row["title"] for row in filters._sort_results(rows, settings)]


def test_resolution_precedes_hdr_remux_and_group_preferences():
    hybrid = "Movie.2024.1080p.HYBRID.REMUX.AVC.FLAC-CiNEPHiLES"
    remux = "Movie.2024.2160p.REMUX.DV.HEVC.TrueHD.Atmos-FraMeSToR"
    encode = "Movie.2024.4320p.WEB-DL.SDR.HEVC.AAC-TEST"
    assert _sorted_titles([hybrid, remux, encode]) == [encode, remux, hybrid]


@pytest.mark.parametrize("resolution", ["4320p", "2160p", "1440p"])
def test_higher_resolution_sdr_beats_1080p_dv_hybrid_remux(resolution):
    high = "Movie.2024.{}.WEB-DL.SDR.HEVC.AAC-TEST".format(resolution)
    low = "Movie.2024.1080p.HYBRID.REMUX.DV.HEVC.TrueHD.Atmos-CiNEPHiLES"
    assert _sorted_titles([low, high]) == [high, low]


def test_hdr_precedes_remux_and_group_preferences_at_equal_resolution():
    titles = [
        "Movie.2024.2160p.HYBRID.REMUX.SDR.HEVC.TrueHD.Atmos-CiNEPHiLES",
        "Movie.2024.2160p.REMUX.HLG.HEVC.TrueHD.Atmos-CiNEPHiLES",
        "Movie.2024.2160p.HYBRID.REMUX.HDR.HEVC.TrueHD.Atmos-CiNEPHiLES",
        "Movie.2024.2160p.REMUX.HDR10Plus.HEVC.FLAC-NCmt",
        "Movie.2024.2160p.WEB-DL.DV.HEVC.AAC-TEST",
    ]
    assert _sorted_titles(titles) == list(reversed(titles))


def test_hybrid_remux_then_remux_win_when_resolution_and_hdr_match():
    titles = [
        "Movie.2024.2160p.WEB-DL.DV.HEVC.TrueHD.Atmos-CiNEPHiLES",
        "Movie.2024.2160p.REMUX.DV.HEVC.TrueHD.Atmos-CiNEPHiLES",
        "Movie.2024.2160p.HYBRID.REMUX.DV.HEVC.FLAC-TEST",
    ]
    assert _sorted_titles(titles) == list(reversed(titles))


def test_default_trash_remux_tiers_break_matching_quality_ties():
    titles = [
        "Movie.2024.2160p.REMUX.HEVC.FLAC-" + group
        for group in ["UNKNOWN", "EPSiLON", "NCmt", "CiNEPHiLES"]
    ]
    assert _sorted_titles(titles) == list(reversed(titles))


def test_remux_tiers_preserve_resolution_and_hdr_priority_within_remux():
    high = "Movie.2024.2160p.REMUX.HEVC.DV.FLAC-EPSiLON"
    low = "Movie.2024.1080p.REMUX.HEVC.FLAC-CiNEPHiLES"
    assert _sorted_titles([low, high]) == [high, low]


def test_preferred_tiers_apply_to_all_releases_and_match_exact_groups():
    titles = [
        "Movie.2024.2160p.WEB-DL.HEVC.FLAC-" + group
        for group in ["UNKNOWN", "EPSiLON", "NCmt", "CiNEPHiLES"]
    ]
    assert _sorted_titles(titles) == list(reversed(titles))
    fake = "Movie.2024.2160p.REMUX.HEVC.FLAC-NotCiNEPHiLES"
    real = "Movie.2024.2160p.REMUX.HEVC.FLAC-NCmt"
    assert _sorted_titles([fake, real]) == [real, fake]


def test_old_preferred_group_setting_cannot_override_tiers():
    titles = [
        "Movie.2024.2160p.WEB-DL.HEVC.FLAC-" + group
        for group in ["Legacy", "EPSiLON", "NCmt", "CiNEPHiLES"]
    ]
    assert _sorted_titles(titles, {"release_group": ["legacy"]}) == list(
        reversed(titles)
    )


def test_settings_remove_legacy_preferred_control_and_use_sixty_second_cache():
    from resources.lib.cache import DEFAULT_CACHE_TTL_SECONDS

    root = ET.parse(
        Path(__file__).resolve().parents[1]
        / "repo/plugin.video.nzbdav/resources/settings.xml"
    ).getroot()
    assert root.find(".//setting[@id='action_configure_preferred_groups']") is None
    assert root.find(".//setting[@id='filter_release_group']") is None
    assert root.find(".//setting[@id='action_configure_excluded_groups']") is not None
    assert root.findtext(".//setting[@id='cache_ttl']/default") == "60"
    assert DEFAULT_CACHE_TTL_SECONDS == 60


def test_explicit_size_sort_does_not_apply_remux_priority():
    titles = [
        "Movie.2024.2160p.WEB-DL.HEVC-TEST",
        "Movie.2024.1080p.HYBRID.REMUX.HEVC-CiNEPHiLES",
    ]
    assert _sorted_titles(titles, {"sort_order": 1}) == titles
