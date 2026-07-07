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


_FOCUS_ACCENT_COLOR = "FF4A9EFF"


def _accent_bars(layout):
    """Thin left-edge image bars (the high-contrast focus indicator)."""
    if layout is None:
        return []
    bars = []
    for image in layout.findall("./control[@type='image']"):
        if (
            image.findtext("colordiffuse") == _FOCUS_ACCENT_COLOR
            and image.findtext("left") == "0"
            and int(image.findtext("width") or "0") <= 8
        ):
            bars.append(image)
    return bars


def test_results_dialog_focused_row_has_high_contrast_focus_indicator():
    """The selected row must carry a bright left accent bar so focus is
    unmistakable on a TV — the recurring Palette accessibility ask. It lives
    only in the focused layout, so it does not paint every row."""
    root = ET.parse(_DIALOG_XML_PATH).getroot()
    results_list = _control(root, "list", "50")
    assert results_list is not None, "results list control id=50 missing"

    focused = results_list.find("./focusedlayout")
    unfocused = results_list.find("./itemlayout")

    assert _accent_bars(focused), "focused row is missing the accent-bar indicator"
    assert not _accent_bars(unfocused), "accent bar must be focus-only"


def _perceived_luminance(argb):
    """Rec.601 luma (0–255) of an 8-char ``AARRGGBB`` Kodi colour."""
    if argb.startswith("$INFO"):
        return 0  # Dynamic properties cannot be statically evaluated for luminance
    r, g, b = int(argb[2:4], 16), int(argb[4:6], 16), int(argb[6:8], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b


def _row_background_color(layout, layout_width="1910"):
    """The full-row background fill of a list item/focused layout."""
    for image in layout.findall("./control[@type='image']"):
        if image.findtext("width") == layout_width:
            return image.findtext("colordiffuse")
    return None


def test_focus_indicator_appearance_is_high_contrast_and_correctly_placed():
    """Verify the indicator's *appearance*, not just its presence: a single
    thin, full-height bar pinned to the left edge, in an accent colour that
    is visibly brighter than — and distinct from — both the focused and
    unfocused row fills, and painted on top of the focused background."""
    root = ET.parse(_DIALOG_XML_PATH).getroot()
    results_list = _control(root, "list", "50")
    assert results_list is not None, "results list control id=50 missing"
    focused = results_list.find("./focusedlayout")
    unfocused = results_list.find("./itemlayout")
    assert focused is not None and unfocused is not None

    bars = _accent_bars(focused)
    assert len(bars) == 1, "expected exactly one focus accent bar"
    accent = bars[0]

    # Geometry: a thin, full-height bar flush to the left edge.
    assert accent.findtext("left") == "0"
    assert accent.findtext("width") == "4"
    assert accent.findtext("height") == focused.get("height") == "66"
    assert accent.findtext("texture"), "accent bar needs a texture to render"

    # High contrast: the accent is distinct from BOTH row fills and markedly
    # brighter than the focused fill it sits on (so focus is unmistakable).
    focused_bg = _row_background_color(focused)
    unfocused_bg = _row_background_color(unfocused)
    assert _FOCUS_ACCENT_COLOR not in (focused_bg, unfocused_bg)
    accent_lum = _perceived_luminance(_FOCUS_ACCENT_COLOR)
    assert accent_lum - _perceived_luminance(focused_bg) > 80
    assert accent_lum - _perceived_luminance(unfocused_bg) > 80

    # Render order: the bar must come AFTER the focused background image so it
    # paints on top of it (Kodi draws controls in document order).
    children = list(focused)
    bg = focused.find("./control[@type='image']")
    assert bg is not None
    assert children.index(accent) > children.index(bg)
