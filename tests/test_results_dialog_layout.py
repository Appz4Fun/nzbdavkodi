# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Structural assertions over the custom results dialog skin."""

import os
import re
import xml.etree.ElementTree as ET

import pytest
from resources.lib.results_dialog import _build_display_fields

_SKIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "repo",
    "plugin.video.nzbdav",
    "resources",
    "skins",
    "Default",
    "1080i",
)

_RESULTS_DIALOG_PATHS = [
    os.path.join(_SKIN_DIR, "results-dialog.xml"),
    os.path.join(_SKIN_DIR, "results-dialog-ranked.xml"),
    os.path.join(_SKIN_DIR, "results-dialog-ranked-compact.xml"),
    os.path.join(_SKIN_DIR, "results-dialog-split.xml"),
]

_RANKED_DIALOG_PATH = os.path.join(_SKIN_DIR, "results-dialog-ranked.xml")
_COMPACT_RANKED_DIALOG_PATH = os.path.join(
    _SKIN_DIR, "results-dialog-ranked-compact.xml"
)
_SPLIT_DIALOG_PATH = os.path.join(_SKIN_DIR, "results-dialog-split.xml")
_MEDIA_DIR = os.path.join(os.path.dirname(_SKIN_DIR), "media")


@pytest.fixture(name="results_dialog_root", scope="module")
def _results_dialog_root():
    return ET.parse(_RESULTS_DIALOG_PATHS[0]).getroot()


@pytest.fixture(name="new_results_dialog_roots", scope="module")
def _new_results_dialog_roots():
    paths = (
        _RANKED_DIALOG_PATH,
        _COMPACT_RANKED_DIALOG_PATH,
        _SPLIT_DIALOG_PATH,
    )
    return {os.path.basename(path): ET.parse(path).getroot() for path in paths}


def _property_label_control(layout, property_name):
    needle = "$INFO[ListItem.Property({})]".format(property_name)
    for control in layout.findall("control"):
        label = control.findtext("label")
        if label == needle:
            return control
    raise AssertionError("missing property label {}".format(property_name))


def _list_item_label_control(layout):
    for control in layout.findall("control"):
        if control.findtext("label") == "$INFO[ListItem.Label]":
            return control
    raise AssertionError("missing filename label")


def _all_labels(root):
    return [label.text or "" for label in root.iter("label")]


def _all_listitem_properties(root):
    names = set()
    pattern = re.compile(r"ListItem\.Property\(([^)]+)\)")
    for label in _all_labels(root):
        names.update(pattern.findall(label))
    return names


def test_results_dialog_has_no_temporary_debug_controls(
    results_dialog_root,
    new_results_dialog_roots,
):
    debug_band_colors = {
        "FFFF00FF",
        "FF2563EB",
        "FFEF4444",
        "FFF59E0B",
        "FFA855F7",
        "FF14B8A6",
    }

    roots = [results_dialog_root] + list(new_results_dialog_roots.values())
    for root in roots:
        for control in root.iter("control"):
            assert control.findtext("label") != "LAYOUT DEBUG"
            assert control.findtext("colordiffuse") not in debug_band_colors


def test_results_dialog_layout_properties_are_backed_by_display_fields():
    known_external_properties = {"detail_title"}
    produced_properties = (
        set(_build_display_fields({}).keys()) | known_external_properties
    )

    for path in _RESULTS_DIALOG_PATHS:
        root = ET.parse(path).getroot()
        assert _all_listitem_properties(root) <= produced_properties


def test_results_dialog_dl_indicator_has_room_in_both_row_layouts(
    results_dialog_root,
):
    list_control = results_dialog_root.find(".//control[@type='list'][@id='50']")
    layouts = [
        list_control.find("itemlayout"),
        list_control.find("focusedlayout"),
    ]

    for layout in layouts:
        available = _property_label_control(layout, "available")
        filename = _list_item_label_control(layout)
        age = _property_label_control(layout, "age")
        indexer = _property_label_control(layout, "indexer")

        assert int(available.findtext("width")) == 32
        assert int(filename.findtext("left")) == 40
        assert int(filename.findtext("width")) == 1140
        assert int(age.findtext("left")) == 1320
        assert int(age.findtext("width")) == 140
        assert int(indexer.findtext("left")) == 1470
        assert int(indexer.findtext("width")) == 200


def test_ranked_results_dialog_uses_shared_list_item_properties():
    root = ET.parse(_RANKED_DIALOG_PATH).getroot()
    labels = _all_labels(root)

    for property_name in ("summary_line_colored",):
        assert "$INFO[ListItem.Property({})]".format(property_name) in labels

    assert "$INFO[ListItem.Property(available)]" not in labels
    assert "$INFO[ListItem.Property(ranked_details_line)]" not in labels


def test_ranked_results_dialog_has_no_side_downloaded_indicator():
    root = ET.parse(_RANKED_DIALOG_PATH).getroot()
    list_control = root.find(".//control[@type='list'][@id='50']")

    for layout in (list_control.find("itemlayout"), list_control.find("focusedlayout")):
        with pytest.raises(AssertionError):
            _property_label_control(layout, "available")


def test_ranked_results_dialog_keeps_first_line_filename_only():
    root = ET.parse(_RANKED_DIALOG_PATH).getroot()
    list_control = root.find(".//control[@type='list'][@id='50']")
    first_line_properties = {"primary_badges", "size", "age", "indexer", "details_line"}

    for layout in (list_control.find("itemlayout"), list_control.find("focusedlayout")):
        filename = _list_item_label_control(layout)
        filename_top = int(filename.findtext("top"))
        for property_name in first_line_properties:
            try:
                control = _property_label_control(layout, property_name)
            except AssertionError:
                continue
            assert int(control.findtext("top")) != filename_top


def test_ranked_results_dialog_uses_rounded_card_textures():
    root = ET.parse(_RANKED_DIALOG_PATH).getroot()
    list_control = root.find(".//control[@type='list'][@id='50']")

    assert os.path.exists(os.path.join(_MEDIA_DIR, "results-card.png"))
    assert os.path.exists(os.path.join(_MEDIA_DIR, "results-card-focus.png"))

    layouts = (
        (list_control.find("itemlayout"), "results-card.png"),
        (list_control.find("focusedlayout"), "results-card-focus.png"),
    )
    for layout, texture_name in layouts:
        background = layout.find("./control[@type='image'][width='1770'][height='120']")
        assert background is not None
        assert background.findtext("texture") == texture_name


def test_ranked_results_dialog_has_no_left_focus_accent_bar():
    root = ET.parse(_RANKED_DIALOG_PATH).getroot()
    list_control = root.find(".//control[@type='list'][@id='50']")
    focused_layout = list_control.find("focusedlayout")

    for image_control in focused_layout.findall("./control[@type='image']"):
        assert image_control.findtext("width") != "8"


def test_ranked_results_dialog_has_no_bottom_divider_lines():
    root = ET.parse(_RANKED_DIALOG_PATH).getroot()
    list_control = root.find(".//control[@type='list'][@id='50']")

    for layout in (list_control.find("itemlayout"), list_control.find("focusedlayout")):
        for image_control in layout.findall("./control[@type='image']"):
            assert image_control.findtext("height") != "1"


def test_ranked_results_dialog_keeps_item_and_focus_geometry_symmetric():
    root = ET.parse(_RANKED_DIALOG_PATH).getroot()
    list_control = root.find(".//control[@type='list'][@id='50']")
    item_layout = list_control.find("itemlayout")
    focused_layout = list_control.find("focusedlayout")

    assert item_layout.get("width") == focused_layout.get("width")
    assert item_layout.get("height") == focused_layout.get("height")

    labels = (
        "$INFO[ListItem.Label]",
        "$INFO[ListItem.Property(summary_line_colored)]",
    )
    for label in labels:
        item_control = next(
            control
            for control in item_layout.findall("./control[@type='label']")
            if control.findtext("label") == label
        )
        focus_control = next(
            control
            for control in focused_layout.findall("./control[@type='label']")
            if control.findtext("label") == label
        )
        for dimension in ("left", "top", "width", "height", "font", "aligny"):
            assert item_control.findtext(dimension) == focus_control.findtext(dimension)


def test_compact_ranked_results_dialog_fits_more_rows():
    root = ET.parse(_COMPACT_RANKED_DIALOG_PATH).getroot()
    list_control = root.find(".//control[@type='list'][@id='50']")
    item_layout = list_control.find("itemlayout")
    focused_layout = list_control.find("focusedlayout")

    assert int(item_layout.get("height")) == 92
    assert focused_layout.get("height") == item_layout.get("height")

    normal_root = ET.parse(_RANKED_DIALOG_PATH).getroot()
    normal_list = normal_root.find(".//control[@type='list'][@id='50']")
    assert int(item_layout.get("height")) < int(
        normal_list.find("itemlayout").get("height")
    )

    for layout in (item_layout, focused_layout):
        background = layout.find("./control[@type='image'][width='1770'][height='82']")
        assert background is not None
        assert background.findtext("texture") in (
            "results-card.png",
            "results-card-focus.png",
        )
        _list_item_label_control(layout)
        _property_label_control(layout, "summary_line_colored")


def test_split_results_dialog_has_focused_detail_panel_bindings():
    root = ET.parse(_SPLIT_DIALOG_PATH).getroot()
    labels = _all_labels(root)

    for property_name in (
        "detail_title",
        "detail_video",
        "detail_audio",
        "detail_source",
        "detail_origin",
        "detail_status",
    ):
        assert (
            "$INFO[Container(50).ListItem.Property({})]".format(property_name) in labels
        )


def test_split_results_dialog_left_list_uses_shared_row_properties():
    root = ET.parse(_SPLIT_DIALOG_PATH).getroot()
    list_control = root.find(".//control[@type='list'][@id='50']")
    layouts = [
        list_control.find("itemlayout"),
        list_control.find("focusedlayout"),
    ]

    for layout in layouts:
        _property_label_control(layout, "available")
        _property_label_control(layout, "primary_badges")
        _list_item_label_control(layout)
