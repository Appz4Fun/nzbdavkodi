from unittest.mock import patch

from resources.lib.filter import (
    _fallback_parse,
    _pubdate_sort_key,
    _sort_results,
    configure_groups_dialog,
    matches_filters,
    parse_title_metadata,
)


@patch("resources.lib.ptt.parse_title")
@patch("resources.lib.filter.xbmc")
def test_parse_title_metadata_exception(mock_xbmc, mock_parse_title):
    mock_parse_title.side_effect = Exception("Test Error")
    result = parse_title_metadata("Test Title (2024)")
    assert mock_xbmc.log.called
    assert result["year"] == 2024


@patch("resources.lib.ptt.parse_title")
@patch("resources.lib.filter.xbmc")
def test_parse_title_metadata_type_error_correct(mock_xbmc, mock_parse_title):
    mock_parse_title.return_value = {
        "resolution": "1080p",
        "languages": 1,
    }
    result = parse_title_metadata("Test Title (2024)")
    assert mock_xbmc.log.called
    assert result["year"] == 2024


@patch("resources.lib.ptt.parse_title")
def test_parse_title_metadata_strings(mock_parse_title):
    mock_parse_title.return_value = {
        "hdr": "HDR10",
        "audio": "Atmos",
        "languages": "en",
        "resolution": "1080p",
        "codec": "x264",
        "group": "GRP",
        "quality": "WEB-DL",
        "channels": "5.1",
        "year": 2024,
    }
    result = parse_title_metadata("Test Title (2024)")
    assert result["hdr"] == ["HDR10"]
    assert result["audio"] == ["Atmos"]
    assert result["languages"] == ["en"]
    assert result["channels"] == "5.1"


def test_matches_filters_hdr_and_languages():
    settings = {
        "resolutions": [],
        "hdr": ["HDR10"],
        "audio": [],
        "codecs": [],
        "languages": ["en"],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
    }

    meta_no_hdr = {
        "hdr": [],
        "languages": ["en"],
        "resolution": "",
        "audio": [],
        "codec": "",
    }
    assert not matches_filters({"title": "Test"}, meta_no_hdr, settings)

    meta_wrong_hdr = {
        "hdr": ["DV"],
        "languages": ["en"],
        "resolution": "",
        "audio": [],
        "codec": "",
    }
    assert not matches_filters({"title": "Test"}, meta_wrong_hdr, settings)

    meta_wrong_lang = {
        "hdr": ["HDR10"],
        "languages": ["fr"],
        "resolution": "",
        "audio": [],
        "codec": "",
    }
    assert not matches_filters({"title": "Test"}, meta_wrong_lang, settings)


def test_pubdate_sort_key_invalid():
    assert _pubdate_sort_key({"pubdate": "invalid date"}) == 0.0
    assert _pubdate_sort_key({"pubdate": "Mon, 99 Jan 2006 15:04:05 GMT"}) == 0.0


def test_sort_results_combos():
    results = [
        {"title": "A", "_meta": {"audio": ["Atmos", "TrueHD"]}},
        {"title": "B", "_meta": {"audio": ["Atmos"]}},
        {"title": "C", "_meta": {"audio": ["TrueHD"]}},
        {"title": "D", "_meta": {"audio": ["AAC"]}},
    ]
    settings = {"release_group": [], "sort_order": 0}
    sorted_res = _sort_results(results, settings)
    assert sorted_res[0]["title"] == "A"

    results_dates = [
        {"title": "Old", "pubdate": "Mon, 02 Jan 2006 15:04:05 GMT"},
        {"title": "New", "pubdate": "Fri, 02 Jan 2026 15:04:05 GMT"},
    ]

    settings["sort_order"] = 3
    sorted_res_3 = _sort_results(results_dates, settings)
    assert sorted_res_3[0]["title"] == "New"

    settings["sort_order"] = 4
    sorted_res_4 = _sort_results(results_dates, settings)
    assert sorted_res_4[0]["title"] == "Old"


def test_fallback_parse_comprehensive():
    title = (
        "Movie (2024) Atmos TrueHD DTS-HD.MA DDP5.1 DD5.1 AAC DTS DV "
        "HDR10plus HDR10 HLG REMUX BLURAY WEBDL WEBRIP HDTV UNCUT 7.1 UPSCALE"
    )
    result = _fallback_parse(title)

    assert "Atmos" in result["audio"]
    assert "TrueHD" in result["audio"]
    assert "DTS-HD MA" in result["audio"]
    assert "DD+" in result["audio"]
    assert "DD" in result["audio"]
    assert "AAC" in result["audio"]

    assert "DV" in result["hdr"]
    assert "HDR10+" in result["hdr"]
    assert "HLG" in result["hdr"]

    assert result["quality"] == "BluRay REMUX"
    assert result["edition"] == "UNCUT"
    assert result["channels"] == "7.1"
    assert result["upscaled"] is True

    assert "DTS" in _fallback_parse("Movie (2024) DTS")["audio"]
    assert _fallback_parse("Movie (2024) BLURAY")["quality"] == "BluRay"
    assert _fallback_parse("Movie (2024) WEBDL")["quality"] == "WEB-DL"
    assert _fallback_parse("Movie (2024) WEBRIP")["quality"] == "WEBRip"
    assert _fallback_parse("Movie (2024) HDTV")["quality"] == "HDTV"
    assert _fallback_parse("Movie (2024) BDRIP")["quality"] == "BluRay"
    assert _fallback_parse("Movie (2024) DVDRIP")["quality"] == "DVDRIP"
    assert _fallback_parse("Movie (2024) HDRIP")["quality"] == "HDRIP"
    assert _fallback_parse("Movie (2024) DDP5.1")["audio"] == ["DD+"]
    assert _fallback_parse("Movie (2024) EAC3")["audio"] == ["DD+"]


@patch("resources.lib.filter.xbmcaddon.Addon")
@patch("resources.lib.filter.xbmcgui.Dialog")
@patch("resources.lib.filter.ALL_RELEASE_GROUPS", ["GRP1", "GRP2", "GRP3"])
def test_configure_groups_dialog(mock_dialog, mock_addon_class):
    mock_addon = mock_addon_class.return_value
    mock_addon.getSetting.return_value = "GRP1,GRP2"

    dialog_instance = mock_dialog.return_value
    dialog_instance.multiselect.return_value = [0, 2]

    configure_groups_dialog("test_setting", "Test Dialog", ["GRP1"])

    dialog_instance.multiselect.assert_called_once()
    mock_addon.setSetting.assert_called_with("test_setting", "GRP1,GRP3")


@patch("resources.lib.filter.xbmcaddon.Addon")
@patch("resources.lib.filter.xbmcgui.Dialog")
@patch("resources.lib.filter.ALL_RELEASE_GROUPS", ["GRP1", "GRP2", "GRP3"])
def test_configure_groups_dialog_no_current_setting(mock_dialog, mock_addon_class):
    mock_addon = mock_addon_class.return_value
    mock_addon.getSetting.return_value = ""

    dialog_instance = mock_dialog.return_value
    dialog_instance.multiselect.return_value = None

    configure_groups_dialog("test_setting", "Test Dialog", ["GRP2"])

    dialog_instance.multiselect.assert_called_once()
    mock_addon.setSetting.assert_not_called()
