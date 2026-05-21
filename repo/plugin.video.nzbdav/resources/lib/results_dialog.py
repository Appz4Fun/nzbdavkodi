# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Custom full-screen dialog for NZB search results selection."""

import xbmcaddon
import xbmcgui

from resources.lib.http_util import format_size as _format_size
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string

# Color constants matching the mockup
_RES_COLORS = {
    "2160p": "FFA78BFA",
    "1080p": "FF60A5FA",
    "720p": "FF4ADE80",
    "480p": "FFFBBF24",
}

_SRC_COLORS = {
    "BluRay REMUX": "FF60A5FA",
    "REMUX": "FF60A5FA",
    "BluRay": "FF4ADE80",
    "WEB-DL": "FFC084FC",
    "WEBRip": "FFF0ABFC",
    "HDTV": "FFFDE68A",
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
    return _c(_AVAILABLE_LABEL, "FF22C55E")


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
            meta = result.get("_meta", {})
            filename = result.get("title", "")

            li = xbmcgui.ListItem(label=filename)

            # Resolution — colored inline
            res = meta.get("resolution", "")
            res_text = _plain_text(res)
            res_color = _RES_COLORS.get(res, "FFEEEEEE")
            res_colored = _c(res, res_color)
            li.setProperty("resolution", res_colored)

            # HDR — colored inline
            hdr_list = meta.get("hdr", [])
            hdr_text = _join_tokens(hdr_list) if hdr_list else "SDR"
            if hdr_list:
                hdr_colored = _c(hdr_text, "FFFBBF24")
            else:
                hdr_colored = _c("SDR", "FF6B7280")
            li.setProperty("hdr", hdr_colored)

            # Codec
            codec = meta.get("codec", "")
            codec_text = _plain_text(codec)
            codec_colored = _c(codec, "FF94A3B8")
            li.setProperty("codec", codec_colored)

            # Audio
            audio_list = meta.get("audio", [])
            audio_str = _join_tokens(audio_list)
            audio_colored = _c(audio_str, "FFE879A8")
            li.setProperty("audio", audio_colored)

            # Source / Quality — colored inline
            quality = meta.get("quality", "")
            src_display = _SRC_SHORT.get(quality, quality)
            src_text = _plain_text(src_display)
            src_color = _SRC_COLORS.get(quality, "FFAAAAAA")
            src_colored = _c(src_display, src_color)
            li.setProperty("quality", src_colored)

            # Container (MKV, MP4, etc.) — default to MKV since most
            # scene releases are MKV and only MP4 releases tag the title.
            container = (meta.get("container", "") or "MKV").upper()
            container_color = "FF34D399" if container == "MKV" else "FFEF4444"
            container_colored = _c(container, container_color)
            li.setProperty("container", container_colored)

            # Size
            size_text = _format_size(result.get("size"))
            size_colored = _c(size_text, "FFA1A1AA")
            li.setProperty("size", size_colored)

            # Age
            age_text = _plain_text(result.get("age", ""))
            age_colored = _c(age_text, "FF6B7280")
            li.setProperty("age", age_colored)

            # Indexer
            indexer_text = _plain_text(result.get("indexer", ""))
            indexer_colored = _c(indexer_text, "FF4A9EFF")
            li.setProperty("indexer", indexer_colored)

            # Group
            group_text = _plain_text(meta.get("group", ""))
            li.setProperty("group", _c(group_text, "FF34D399"))

            details_line = _join_parts(
                " · ",
                [size_text, age_text, indexer_text, group_text],
            )
            technical_summary = _join_parts(
                " · ",
                [
                    res_text,
                    hdr_text,
                    codec_text,
                    audio_str,
                    src_text,
                    container,
                    size_text,
                ],
            )
            technical_summary_colored = _join_parts(
                " · ",
                [
                    res_colored,
                    hdr_colored,
                    codec_colored,
                    audio_colored,
                    src_colored,
                    container_colored,
                    size_colored,
                ],
            )
            meta_origin_colored = _join_parts(
                " · ",
                [age_colored, indexer_colored],
            )
            if result.get("_available"):
                status_text = _DOWNLOADED_LABEL
                status_colored = _c(_DOWNLOADED_LABEL, "FF22C55E")
            else:
                status_text = ""
                status_colored = ""
            ranked_details_line = _join_parts(
                " · ",
                [status_text, age_text, indexer_text, technical_summary],
            )
            summary_line_colored = _join_parts(
                " · ",
                [status_colored, meta_origin_colored, technical_summary_colored],
            )
            li.setProperty(
                "primary_badges",
                _join_parts(
                    " · ",
                    [res_text, hdr_text, codec_text, audio_str, src_text, container],
                ),
            )
            li.setProperty("details_line", details_line)
            li.setProperty("technical_summary", technical_summary)
            li.setProperty("technical_summary_colored", technical_summary_colored)
            li.setProperty("meta_origin_colored", meta_origin_colored)
            li.setProperty("summary_line_colored", summary_line_colored)
            li.setProperty("ranked_details_line", ranked_details_line)
            li.setProperty("detail_title", filename)
            li.setProperty("detail_video", _join_parts(" ", [res_text, codec_text]))
            li.setProperty("detail_audio", audio_str)
            li.setProperty("detail_source", _join_parts(" ", [src_text, container]))
            li.setProperty("detail_origin", details_line)
            li.setProperty(
                "detail_status",
                _DOWNLOADED_LABEL if result.get("_available") else "",
            )

            # Already downloaded indicator
            if result.get("_available"):
                li.setProperty("available", _available_text())

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
