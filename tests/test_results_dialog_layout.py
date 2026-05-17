# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Structural assertions over the custom results dialog skin."""

import os
import xml.etree.ElementTree as ET

import pytest

_RESULTS_DIALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugin.video.nzbdav",
    "resources",
    "skins",
    "Default",
    "1080i",
    "results-dialog.xml",
)


@pytest.fixture(scope="module")
def results_dialog_root():
    return ET.parse(_RESULTS_DIALOG_PATH).getroot()


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


def test_results_dialog_has_no_temporary_debug_controls(results_dialog_root):
    debug_band_colors = {
        "FFFF00FF",
        "FF2563EB",
        "FFEF4444",
        "FFF59E0B",
        "FFA855F7",
        "FF14B8A6",
    }

    for control in results_dialog_root.iter("control"):
        assert control.findtext("label") != "LAYOUT DEBUG"
        assert control.findtext("colordiffuse") not in debug_band_colors


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
