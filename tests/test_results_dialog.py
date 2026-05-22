# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import os
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

from resources.lib import results_dialog as results_dialog_module
from resources.lib.results_dialog import (
    _AVAILABLE_LABEL,
    _available_text,
    _build_display_fields,
    _format_date,
    _format_size,
    _lang_short,
    _metadata_part,
    _resolve_layout_xml,
    show_results_dialog,
)


def _make_result(**overrides):
    base = {
        "title": "Movie.2024.1080p.x264",
        "size": "5000000000",
        "indexer": "test",
        "age": "1 day",
        "_meta": {
            "resolution": "1080p",
            "codec": "x264",
            "hdr": [],
            "audio": [],
            "languages": [],
            "group": "",
            "quality": "WEB-DL",
        },
    }
    base.update(overrides)
    return base


def test_available_label_is_ascii_for_skin_compatibility():
    assert _AVAILABLE_LABEL == "DL"
    assert _AVAILABLE_LABEL.isascii()


def test_available_label_renders_green():
    assert _available_text() == "[COLOR FF22C55E]DL[/COLOR]"


def test_metadata_part_returns_plain_and_colored_text():
    part = _metadata_part("2160p", "FFA78BFA")

    assert part["plain"] == "2160p"
    assert part["colored"] == "[COLOR FFA78BFA]2160p[/COLOR]"


def test_metadata_part_omits_color_for_blank_text():
    part = _metadata_part("", "FFA78BFA")

    assert part == {"plain": "", "colored": ""}


# ---------------------------------------------------------------------------
# Layout selection
# ---------------------------------------------------------------------------


def test_resolve_layout_xml_defaults_to_classic_rows():
    assert _resolve_layout_xml("") == "results-dialog.xml"
    assert _resolve_layout_xml(None) == "results-dialog.xml"


def test_resolve_layout_xml_uses_split_detail_for_value_one():
    assert _resolve_layout_xml("1") == "results-dialog-split.xml"
    assert _resolve_layout_xml(" 1 ") == "results-dialog-split.xml"


def test_resolve_layout_xml_uses_classic_rows_for_value_two():
    assert _resolve_layout_xml("2") == "results-dialog.xml"
    assert _resolve_layout_xml(" 2 ") == "results-dialog.xml"


def test_resolve_layout_xml_uses_compact_ranked_cards_for_value_three():
    assert _resolve_layout_xml("3") == "results-dialog-ranked-compact.xml"
    assert _resolve_layout_xml(" 3 ") == "results-dialog-ranked-compact.xml"


def test_resolve_layout_xml_falls_back_to_classic_rows_for_invalid_values():
    assert _resolve_layout_xml("4") == "results-dialog.xml"
    assert _resolve_layout_xml("ranked_cards") == "results-dialog.xml"
    assert _resolve_layout_xml(0) == "results-dialog-ranked.xml"


def test_layout_options_define_setting_values_and_xml_files():
    assert hasattr(results_dialog_module, "_LAYOUT_OPTIONS")
    layout_options = results_dialog_module._LAYOUT_OPTIONS

    assert [option["value"] for option in layout_options] == ["0", "1", "2", "3"]
    assert [option["label_id"] for option in layout_options] == [
        30198,
        30199,
        30200,
        30201,
    ]
    assert [option["xml"] for option in layout_options] == [
        "results-dialog-ranked.xml",
        "results-dialog-split.xml",
        "results-dialog.xml",
        "results-dialog-ranked-compact.xml",
    ]


# ---------------------------------------------------------------------------
# show_results_dialog
# ---------------------------------------------------------------------------


def test_show_results_dialog_returns_none_on_cancel():
    """show_results_dialog returns None when user cancels (selected_index -1)."""
    results = [_make_result()]

    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = -1
        MockDialog.return_value = mock_instance

        result = show_results_dialog(
            results, title="Movie", year="2024", total_count=10
        )
        assert result is None


def test_show_results_dialog_returns_selected():
    """show_results_dialog returns selected result dict when user picks a row."""
    selected = _make_result(link="http://nzb/123")
    results = [selected]

    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = 0
        MockDialog.return_value = mock_instance

        result = show_results_dialog(results, title="Movie", year="2024", total_count=1)
        assert result == selected
        assert MockDialog.call_args.args[:4] == (
            "results-dialog.xml",
            "",
            "Default",
            "1080i",
        )


def test_show_results_dialog_uses_split_detail_layout_setting():
    selected = _make_result(link="http://nzb/123")
    results = [selected]

    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = 0
        MockDialog.return_value = mock_instance

        with patch("resources.lib.results_dialog.xbmcaddon") as mock_addon_mod:
            addon = mock_addon_mod.Addon.return_value
            addon.getAddonInfo.return_value = "/addon/path"
            addon.getSetting.return_value = "1"

            result = show_results_dialog(results, title="Movie", total_count=1)

    assert result == selected
    assert MockDialog.call_args.args[:4] == (
        "results-dialog-split.xml",
        "/addon/path",
        "Default",
        "1080i",
    )


def test_show_results_dialog_uses_classic_rows_layout_setting():
    selected = _make_result(link="http://nzb/123")
    results = [selected]

    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = 0
        MockDialog.return_value = mock_instance

        with patch("resources.lib.results_dialog.xbmcaddon") as mock_addon_mod:
            addon = mock_addon_mod.Addon.return_value
            addon.getAddonInfo.return_value = "/addon/path"
            addon.getSetting.return_value = "2"

            result = show_results_dialog(results, title="Movie", total_count=1)

    assert result == selected
    assert MockDialog.call_args.args[:4] == (
        "results-dialog.xml",
        "/addon/path",
        "Default",
        "1080i",
    )


def test_show_results_dialog_defaults_to_classic_rows_when_layout_setting_read_fails():
    selected = _make_result(link="http://nzb/123")
    results = [selected]

    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = 0
        MockDialog.return_value = mock_instance

        with patch("resources.lib.results_dialog.xbmcaddon") as mock_addon_mod:
            addon = mock_addon_mod.Addon.return_value
            addon.getAddonInfo.return_value = "/addon/path"
            addon.getSetting.side_effect = RuntimeError("settings unavailable")

            result = show_results_dialog(results, title="Movie", total_count=1)

    assert result == selected
    assert MockDialog.call_args.args[:4] == (
        "results-dialog.xml",
        "/addon/path",
        "Default",
        "1080i",
    )


def test_show_results_dialog_calls_doModal():
    """show_results_dialog must call doModal() on the dialog."""
    results = [_make_result()]

    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = -1
        MockDialog.return_value = mock_instance

        show_results_dialog(results)
        mock_instance.doModal.assert_called_once()


def test_show_results_dialog_empty_results_returns_none():
    """show_results_dialog returns None for empty results list."""
    with patch("resources.lib.results_dialog.ResultsDialog") as MockDialog:
        mock_instance = MagicMock()
        mock_instance.get_selected_index.return_value = -1
        MockDialog.return_value = mock_instance

        result = show_results_dialog([], title="Movie", year="2024", total_count=0)
        assert result is None


# ---------------------------------------------------------------------------
# Display properties
# ---------------------------------------------------------------------------


class _FakeListItem:
    def __init__(self, label=""):
        self.label = label
        self.properties = {}

    def setProperty(self, key, value):
        self.properties[key] = value

    def getProperty(self, key):
        return self.properties.get(key, "")


class _FakeListControl:
    def __init__(self):
        self.items = []

    def reset(self):
        self.items = []

    def addItems(self, items):
        self.items.extend(items)


def test_results_dialog_sets_shared_display_properties(monkeypatch):
    from resources.lib import results_dialog

    list_control = _FakeListControl()
    dialog = results_dialog.ResultsDialog(
        results=[
            _make_result(
                title="Movie.2024.2160p.UHD.BluRay.REMUX-FraMeSToR",
                size=str(72 * 1024**3),
                age="4 years",
                indexer="Hydra",
                _available=True,
                _meta={
                    "resolution": "2160p",
                    "codec": "HEVC",
                    "hdr": ["DV", "HDR10"],
                    "audio": ["TrueHD", "Atmos"],
                    "languages": ["English"],
                    "group": "FraMeSToR",
                    "quality": "BluRay REMUX",
                    "container": "mkv",
                },
            )
        ],
        title="Movie",
        year="2024",
        total_count=1,
        filtered_count=1,
    )
    monkeypatch.setattr(results_dialog.xbmcgui, "ListItem", _FakeListItem)
    monkeypatch.setattr(
        dialog, "getControl", lambda _control_id: list_control, raising=False
    )
    monkeypatch.setattr(dialog, "setFocusId", lambda _control_id: None, raising=False)
    monkeypatch.setattr(dialog, "setProperty", lambda _key, _value: None, raising=False)

    dialog.onInit()

    assert len(list_control.items) == 1
    item = list_control.items[0]
    assert "2160p" in item.getProperty("primary_badges")
    assert "DV HDR10" in item.getProperty("primary_badges")
    assert "72.0 GB" in item.getProperty("details_line")
    assert item.getProperty("technical_summary") == (
        "2160p · DV HDR10 · HEVC · TrueHD Atmos · REMUX · MKV · 72.0 GB"
    )
    assert item.getProperty("ranked_details_line") == ""
    assert item.getProperty("downloaded_badge") == "[COLOR FF22C55E]DL[/COLOR]"
    assert item.getProperty("available") == "[COLOR FF22C55E]DL[/COLOR]"
    assert item.getProperty("meta_origin_colored") == (
        "[COLOR FF6B7280]4 years[/COLOR] · [COLOR FF4A9EFF]Hydra[/COLOR]"
    )
    assert item.getProperty("technical_summary_colored") == (
        "[COLOR FFA78BFA]2160p[/COLOR] · [COLOR FFFBBF24]DV HDR10[/COLOR] · "
        "[COLOR FF94A3B8]HEVC[/COLOR] · [COLOR FFE879A8]TrueHD Atmos[/COLOR] · "
        "[COLOR FF60A5FA]REMUX[/COLOR] · [COLOR FF34D399]MKV[/COLOR] · "
        "[COLOR FFA1A1AA]72.0 GB[/COLOR]"
    )
    assert item.getProperty("summary_line_colored") == (
        "[COLOR FF22C55E]Downloaded[/COLOR] · [COLOR FF6B7280]4 years[/COLOR] · "
        "[COLOR FF4A9EFF]Hydra[/COLOR] · "
        "[COLOR FFA78BFA]2160p[/COLOR] · [COLOR FFFBBF24]DV HDR10[/COLOR] · "
        "[COLOR FF94A3B8]HEVC[/COLOR] · [COLOR FFE879A8]TrueHD Atmos[/COLOR] · "
        "[COLOR FF60A5FA]REMUX[/COLOR] · [COLOR FF34D399]MKV[/COLOR] · "
        "[COLOR FFA1A1AA]72.0 GB[/COLOR]"
    )
    assert item.getProperty("detail_title") == (
        "Movie.2024.2160p.UHD.BluRay.REMUX-FraMeSToR"
    )
    assert item.getProperty("detail_video") == "2160p HEVC"
    assert item.getProperty("detail_audio") == "TrueHD Atmos"
    assert item.getProperty("detail_source") == "REMUX MKV"
    assert item.getProperty("detail_origin") == "72.0 GB · 4 years · Hydra · FraMeSToR"
    assert item.getProperty("detail_status") == "Downloaded"


def test_results_dialog_display_properties_tolerate_missing_metadata(monkeypatch):
    from resources.lib import results_dialog

    list_control = _FakeListControl()
    dialog = results_dialog.ResultsDialog(
        results=[{"title": "Unparsed.Release", "_meta": {}}],
        title="Movie",
        total_count=1,
        filtered_count=1,
    )
    monkeypatch.setattr(results_dialog.xbmcgui, "ListItem", _FakeListItem)
    monkeypatch.setattr(
        dialog, "getControl", lambda _control_id: list_control, raising=False
    )
    monkeypatch.setattr(dialog, "setFocusId", lambda _control_id: None, raising=False)
    monkeypatch.setattr(dialog, "setProperty", lambda _key, _value: None, raising=False)

    dialog.onInit()

    item = list_control.items[0]
    assert item.getProperty("primary_badges") == "SDR · MKV"
    assert item.getProperty("details_line") == ""
    assert item.getProperty("technical_summary") == "SDR · MKV"
    assert item.getProperty("ranked_details_line") == ""
    assert item.getProperty("meta_origin_colored") == ""
    assert item.getProperty("technical_summary_colored") == (
        "[COLOR FF6B7280]SDR[/COLOR] · [COLOR FF34D399]MKV[/COLOR]"
    )
    assert item.getProperty("summary_line_colored") == (
        "[COLOR FF6B7280]SDR[/COLOR] · [COLOR FF34D399]MKV[/COLOR]"
    )
    assert item.getProperty("detail_status") == ""


def test_build_display_fields_omits_unused_ranked_details_line():
    fields = _build_display_fields(
        _make_result(
            age="1 day",
            indexer="Hydra",
            _available=True,
        )
    )

    assert "ranked_details_line" not in fields
    assert fields["downloaded_badge"] == "[COLOR FF22C55E]DL[/COLOR]"
    assert fields["available"] == fields["downloaded_badge"]
    assert fields["summary_line_colored"].startswith(
        "[COLOR FF22C55E]Downloaded[/COLOR] · [COLOR FF6B7280]1 day[/COLOR]"
    )


# ---------------------------------------------------------------------------
# Settings schema
# ---------------------------------------------------------------------------


def test_results_layout_setting_defaults_to_classic_rows():
    settings_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "repo",
        "plugin.video.nzbdav",
        "resources",
        "settings.xml",
    )
    root = ET.parse(settings_path).getroot()
    setting = root.find(".//setting[@id='results_layout']")

    assert setting is not None
    assert setting.get("type") == "enum"
    assert setting.get("default") == "2"
    assert setting.get("label") == "30197"
    assert setting.get("lvalues") == "|".join(
        str(option["label_id"]) for option in results_dialog_module._LAYOUT_OPTIONS
    )


def test_results_layout_setting_is_between_max_results_and_auto_select_separator():
    settings_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "repo",
        "plugin.video.nzbdav",
        "resources",
        "settings.xml",
    )
    root = ET.parse(settings_path).getroot()
    sorting = root.find(".//category[@label='30062']")
    ids = [setting.get("id") or setting.get("label") for setting in list(sorting)]

    assert ids.index("max_results") < ids.index("results_layout") < ids.index("30065")


def test_results_layout_language_strings_exist():
    strings_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "repo",
        "plugin.video.nzbdav",
        "resources",
        "language",
        "resource.language.en_gb",
        "strings.po",
    )
    with open(strings_path, encoding="utf-8") as strings_file:
        text = strings_file.read()

    assert 'msgctxt "#30197"' in text
    assert 'msgid "Results layout"' in text
    assert 'msgctxt "#30198"' in text
    assert 'msgid "Ranked cards"' in text
    assert 'msgctxt "#30199"' in text
    assert 'msgid "Split detail"' in text
    assert 'msgctxt "#30200"' in text
    assert 'msgid "Classic rows"' in text
    assert 'msgctxt "#30201"' in text
    assert 'msgid "Compact ranked cards"' in text


# ---------------------------------------------------------------------------
# _format_size
# ---------------------------------------------------------------------------


def test_format_size_gigabytes():
    assert _format_size(2 * 1024**3) == "2.0 GB"


def test_format_size_megabytes():
    assert _format_size(512 * 1024**2) == "512.0 MB"


def test_format_size_bytes():
    assert _format_size(1000) == "1000 B"


def test_format_size_none_returns_empty():
    assert _format_size(None) == ""


def test_format_size_zero_returns_empty():
    assert _format_size(0) == ""


def test_format_size_string_input():
    """_format_size should accept string input (as received from parsed NZB data)."""
    assert _format_size("1073741824") == "1.0 GB"


def test_format_size_malformed_string_returns_empty():
    """A malformed provider size must not crash the result picker."""
    assert _format_size("unknown") == ""


# ---------------------------------------------------------------------------
# _format_date
# ---------------------------------------------------------------------------


def test_format_date_rfc2822():
    result = _format_date("Mon, 01 Jan 2024 00:00:00 +0000")
    assert result == "2024-01-01"


def test_format_date_empty_returns_empty():
    assert _format_date("") == ""


def test_format_date_none_returns_empty():
    assert _format_date(None) == ""


def test_format_date_fallback_truncates():
    """For unparseable dates, return first 10 chars."""
    result = _format_date("2024-06-15 extra garbage")
    assert result == "2024-06-15"


# ---------------------------------------------------------------------------
# _lang_short
# ---------------------------------------------------------------------------


def test_lang_short_known_language():
    assert _lang_short("English") == "EN"
    assert _lang_short("French") == "FR"
    assert _lang_short("Japanese") == "JA"


def test_lang_short_unknown_language_uppercases_first_two():
    assert _lang_short("Klingon") == "KL"


def test_lang_short_empty_returns_empty():
    assert _lang_short("") == ""
