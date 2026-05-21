# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Custom full-screen dialog for NZB search results selection."""

import xbmcaddon
import xbmcgui

from resources.lib.http_util import format_size as _format_size
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string

COLOR_RESOLUTION_DEFAULT = "FFEEEEEE"
COLOR_RESOLUTION_2160P = "FFA78BFA"
COLOR_RESOLUTION_1080P = "FF60A5FA"
COLOR_RESOLUTION_720P = "FF4ADE80"
COLOR_RESOLUTION_480P = "FFFBBF24"
COLOR_HDR = "FFFBBF24"
COLOR_SDR = "FF6B7280"
COLOR_CODEC = "FF94A3B8"
COLOR_AUDIO = "FFE879A8"
COLOR_SOURCE_DEFAULT = "FFAAAAAA"
COLOR_SOURCE_BLUE = "FF60A5FA"
COLOR_SOURCE_GREEN = "FF4ADE80"
COLOR_SOURCE_PURPLE = "FFC084FC"
COLOR_SOURCE_PINK = "FFF0ABFC"
COLOR_SOURCE_YELLOW = "FFFDE68A"
COLOR_CONTAINER_MKV = "FF34D399"
COLOR_CONTAINER_OTHER = "FFEF4444"
COLOR_SIZE = "FFA1A1AA"
COLOR_AGE = "FF6B7280"
COLOR_INDEXER = "FF4A9EFF"
COLOR_GROUP = "FF34D399"
COLOR_DOWNLOADED = "FF22C55E"

_RES_COLORS = {
    "2160p": COLOR_RESOLUTION_2160P,
    "1080p": COLOR_RESOLUTION_1080P,
    "720p": COLOR_RESOLUTION_720P,
    "480p": COLOR_RESOLUTION_480P,
}

_SRC_COLORS = {
    "BluRay REMUX": COLOR_SOURCE_BLUE,
    "REMUX": COLOR_SOURCE_BLUE,
    "BluRay": COLOR_SOURCE_GREEN,
    "WEB-DL": COLOR_SOURCE_PURPLE,
    "WEBRip": COLOR_SOURCE_PINK,
    "HDTV": COLOR_SOURCE_YELLOW,
}

_SRC_SHORT = {
    "BluRay REMUX": "REMUX",
    "BluRay": "BluRay",
    "WEB-DL": "WEB-DL",
    "WEBRip": "WEBRip",
    "HDTV": "HDTV",
}


def _c(text, color):
    """Wrap text in Kodi [COLOR] tags."""
    if not text:
        return ""
    return "[COLOR {}]{}[/COLOR]".format(color, text)


# Row backgrounds for alternating stripes
_BG_A = "FF0C0C10"
_BG_B = "FF141417"
_AVAILABLE_LABEL = "DL"
_DOWNLOADED_LABEL = "Downloaded"

ACTION_SELECT = 7
ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92
ACTION_CONTEXT_MENU = 117

LIST_ID = 50


def _available_text():
    return _c(_AVAILABLE_LABEL, COLOR_DOWNLOADED)


def _resolve_layout_xml(raw_value):
    """Return the results dialog XML filename for a stored layout setting."""
    if raw_value is None:
        return "results-dialog-ranked.xml"
    try:
        value = str(raw_value).strip()
    except Exception:
        value = ""
    if value == "1":
        return "results-dialog-split.xml"
    if value == "2":
        return "results-dialog.xml"
    return "results-dialog-ranked.xml"


def _plain_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _join_tokens(value):
    if not value:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(_plain_text(item) for item in value if _plain_text(item))
    return _plain_text(value)


def _join_parts(separator, parts):
    return separator.join(part for part in parts if part)


def _metadata_part(text, color):
    plain = _plain_text(text)
    return {"plain": plain, "colored": _c(plain, color)}


def _build_display_fields(result):
    """Return Kodi ListItem properties for one search result."""
    meta = result.get("_meta", {})
    if not isinstance(meta, dict):
        meta = {}

    resolution_text = meta.get("resolution", "")
    resolution = _metadata_part(
        resolution_text,
        _RES_COLORS.get(_plain_text(resolution_text), COLOR_RESOLUTION_DEFAULT),
    )

    hdr_list = meta.get("hdr", [])
    hdr_text = _join_tokens(hdr_list) if hdr_list else "SDR"
    hdr = _metadata_part(hdr_text, COLOR_HDR if hdr_list else COLOR_SDR)

    codec = _metadata_part(meta.get("codec", ""), COLOR_CODEC)

    audio_list = meta.get("audio", [])
    audio = _metadata_part(_join_tokens(audio_list), COLOR_AUDIO)

    quality = meta.get("quality", "")
    src_display = _SRC_SHORT.get(quality, quality)
    source = _metadata_part(src_display, _SRC_COLORS.get(quality, COLOR_SOURCE_DEFAULT))

    container = (meta.get("container", "") or "MKV").upper()
    container_part = _metadata_part(
        container, COLOR_CONTAINER_MKV if container == "MKV" else COLOR_CONTAINER_OTHER
    )

    size = _metadata_part(_format_size(result.get("size")), COLOR_SIZE)
    age = _metadata_part(result.get("age", ""), COLOR_AGE)
    indexer = _metadata_part(result.get("indexer", ""), COLOR_INDEXER)
    group = _metadata_part(meta.get("group", ""), COLOR_GROUP)

    details_line = _join_parts(
        " · ",
        [size["plain"], age["plain"], indexer["plain"], group["plain"]],
    )
    primary_badges = _join_parts(
        " · ",
        [
            resolution["plain"],
            hdr["plain"],
            codec["plain"],
            audio["plain"],
            source["plain"],
            container_part["plain"],
        ],
    )
    technical_summary = _join_parts(
        " · ",
        [
            resolution["plain"],
            hdr["plain"],
            codec["plain"],
            audio["plain"],
            source["plain"],
            container_part["plain"],
            size["plain"],
        ],
    )
    technical_summary_colored = _join_parts(
        " · ",
        [
            resolution["colored"],
            hdr["colored"],
            codec["colored"],
            audio["colored"],
            source["colored"],
            container_part["colored"],
            size["colored"],
        ],
    )
    meta_origin_colored = _join_parts(" · ", [age["colored"], indexer["colored"]])

    if result.get("_available"):
        status_colored = _c(_DOWNLOADED_LABEL, COLOR_DOWNLOADED)
        downloaded_badge = _available_text()
        detail_status = _DOWNLOADED_LABEL
    else:
        status_colored = ""
        downloaded_badge = ""
        detail_status = ""

    summary_line_colored = _join_parts(
        " · ",
        [status_colored, meta_origin_colored, technical_summary_colored],
    )

    return {
        "resolution": resolution["colored"],
        "hdr": hdr["colored"],
        "codec": codec["colored"],
        "audio": audio["colored"],
        "quality": source["colored"],
        "container": container_part["colored"],
        "size": size["colored"],
        "age": age["colored"],
        "indexer": indexer["colored"],
        "group": group["colored"],
        "primary_badges": primary_badges,
        "details_line": details_line,
        "technical_summary": technical_summary,
        "technical_summary_colored": technical_summary_colored,
        "meta_origin_colored": meta_origin_colored,
        "summary_line_colored": summary_line_colored,
        "detail_video": _join_parts(" ", [resolution["plain"], codec["plain"]]),
        "detail_audio": audio["plain"],
        "detail_source": _join_parts(" ", [source["plain"], container_part["plain"]]),
        "detail_origin": details_line,
        "detail_status": detail_status,
        "downloaded_badge": downloaded_badge,
        # Compatibility alias used by the classic and split skin layouts.
        "available": downloaded_badge,
    }


class ResultsDialog(xbmcgui.WindowXMLDialog):
    """Full-screen NZB results selection dialog."""

    def __init__(self, *args, **kwargs):
        self.results = kwargs.get("results", [])
        self.title = kwargs.get("title", "")
        self.year = kwargs.get("year", "")
        self.total_count = kwargs.get("total_count", 0)
        self.filtered_count = kwargs.get("filtered_count", 0)
        self.selected_index = -1
        super().__init__(*args)

    def onInit(self):
        """Populate the dialog with results data."""
        # Set header properties
        title_display = self.title
        if self.year:
            title_display = "{} ({})".format(self.title, self.year)
        self.setProperty("title", title_display)
        self.setProperty("count", _fmt(30110, self.filtered_count))
        self.setProperty("sort_info", _string(30111))
        self.setProperty(
            "filter_info",
            _fmt(30112, self.filtered_count, self.total_count),
        )

        list_control = self.getControl(LIST_ID)
        list_control.reset()

        items = []
        for i, result in enumerate(self.results):
            filename = result.get("title", "")

            li = xbmcgui.ListItem(label=filename)
            for key, value in _build_display_fields(result).items():
                li.setProperty(key, value)
            li.setProperty("detail_title", filename)

            # Alternating row background
            li.setProperty("row_bg", _BG_A if i % 2 == 0 else _BG_B)

            items.append(li)

        list_control.addItems(items)
        self.setFocusId(LIST_ID)

    def onClick(self, controlId):
        """Handle item selection."""
        if controlId == LIST_ID:
            self.selected_index = self.getControl(LIST_ID).getSelectedPosition()
            self.close()

    def onAction(self, action):
        """Handle keyboard/remote actions."""
        action_id = action.getId()
        if action_id in (ACTION_SELECT,):
            focused = self.getFocusId()
            if focused == LIST_ID:
                self.selected_index = self.getControl(LIST_ID).getSelectedPosition()
                self.close()
        elif action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self.selected_index = -1
            self.close()
        elif action_id == ACTION_CONTEXT_MENU:
            # Close the picker on right-click / "i" button; treat as cancel
            # rather than a no-op so the user isn't trapped if they don't
            # want any of the presented results.
            self.selected_index = -1
            self.close()

    def get_selected_index(self):
        """Return the index of the selected result, or -1 if cancelled."""
        return self.selected_index


def show_results_dialog(results, title="", year="", total_count=0):
    """Show the full-screen results dialog and wait for the user pick.

    Args:
        results: List of filtered result dicts (must include
            ``title``, ``size``, ``_meta`` per ``filter_results``).
        title: Movie or show title for the dialog heading.
        year: Release year (movies) or show year (episodes),
            displayed beside the title.
        total_count: Number of results before filtering, used to
            render "Showing N of M" in the dialog header.

    Returns:
        The chosen result dict (same object reference from ``results``)
        when the user picks one, or ``None`` when the user cancels the
        dialog or no results are available.
    """
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    addon_path = addon.getAddonInfo("path")
    try:
        layout_setting = addon.getSetting("results_layout")
    except Exception:
        layout_setting = None

    dialog = ResultsDialog(
        _resolve_layout_xml(layout_setting),
        addon_path,
        "Default",
        "1080i",
        results=results,
        title=title,
        year=year,
        total_count=total_count,
        filtered_count=len(results),
    )
    dialog.doModal()

    idx = dialog.get_selected_index()
    del dialog

    if 0 <= idx < len(results):
        return results[idx]
    return None


def _format_date(pubdate):
    """Extract YYYY-MM-DD from an RFC 2822 pubdate string."""
    if not pubdate:
        return ""
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(pubdate)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return pubdate[:10] if len(pubdate) >= 10 else pubdate


def _lang_short(lang):
    """Convert language name to short code."""
    _MAP = {
        "English": "EN",
        "Spanish": "ES",
        "French": "FR",
        "German": "DE",
        "Italian": "IT",
        "Portuguese": "PT",
        "Dutch": "NL",
        "Russian": "RU",
        "Japanese": "JA",
        "Korean": "KO",
        "Chinese": "ZH",
        "Arabic": "AR",
        "Hindi": "HI",
    }
    return _MAP.get(lang, lang[:2].upper() if lang else "")
