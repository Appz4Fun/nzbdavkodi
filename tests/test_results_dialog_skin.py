# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Structural assertions over the bundled results dialog skin XML."""

import os
import xml.etree.ElementTree as ET

_DIALOG_XML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "repo",
    "plugin.video.nzbdav",
    "resources",
    "skins",
    "Default",
    "1080i",
    "results-dialog.xml",
)


def _control(root, control_type, control_id):
    for control in root.findall("./controls/control"):
        if control.get("type") == control_type and control.get("id") == control_id:
            return control
    return None


def test_results_dialog_scrollbar_is_linked_to_results_list():
    root = ET.parse(_DIALOG_XML_PATH).getroot()

    results_list = _control(root, "list", "50")
    scrollbar = _control(root, "scrollbar", "60")

    assert results_list is not None, "results list control id=50 missing"
    assert scrollbar is not None, "results scrollbar control id=60 missing"
    assert results_list.findtext("pagecontrol") == "60"
    assert scrollbar.findtext("orientation") == "vertical"
    assert scrollbar.findtext("showonepage") == "false"
