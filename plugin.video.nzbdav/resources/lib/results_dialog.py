# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Custom full-screen dialog for NZB search results selection."""

import xbmcaddon
import xbmcgui

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

ACTION_SELECT = 7
ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92
ACTION_CONTEXT_MENU = 117

LIST_ID = 50


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
        self.setProperty("count", "{} sources found".format(self.filtered_count))
        self.setProperty("sort_info", "Sorted by relevance")
        self.setProperty(
            "filter_info",
            "Showing {} of {} sources after filters".format(
                self.filtered_count, self.total_count
            ),
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
            res_color = _RES_COLORS.get(res, "FFEEEEEE")
            li.setProperty("resolution", _c(res, res_color))

            # HDR — colored inline
            hdr_list = meta.get("hdr", [])
            if hdr_list:
                li.setProperty("hdr", _c(" ".join(hdr_list), "FFFBBF24"))
            else:
                li.setProperty("hdr", _c("SDR", "FF333333"))

            # Codec
            codec = meta.get("codec", "")
            li.setProperty("codec", _c(codec, "FF94A3B8"))

            # Audio
            audio_list = meta.get("audio", [])
            audio_str = " ".join(audio_list) if audio_list else ""
            li.setProperty("audio", _c(audio_str, "FFE879A8"))

            # Source / Quality — colored inline
            quality = meta.get("quality", "")
            src_display = _SRC_SHORT.get(quality, quality)
            src_color = _SRC_COLORS.get(quality, "FFAAAAAA")
            li.setProperty("quality", _c(src_display, src_color))

            # Size
            li.setProperty("size", _c(_format_size(result.get("size")), "FFA1A1AA"))

            # Age
            li.setProperty("age", _c(result.get("age", ""), "FF6B7280"))

            # Indexer
            li.setProperty("indexer", _c(result.get("indexer", ""), "FF4A9EFF"))

            # Group
            li.setProperty("group", _c(meta.get("group", ""), "FF34D399"))

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

    def get_selected_index(self):
        """Return the index of the selected result, or -1 if cancelled."""
        return self.selected_index


def show_results_dialog(results, title="", year="", total_count=0):
    """Show the results dialog and return the selected result dict, or None."""
    addon = xbmcaddon.Addon()
    addon_path = addon.getAddonInfo("path")

    dialog = ResultsDialog(
        "results-dialog.xml",
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


def _format_size(size_bytes):
    """Format byte size to human readable."""
    if not size_bytes:
        return ""
    size_bytes = int(size_bytes)
    if size_bytes >= 1073741824:
        return "{:.1f} GB".format(size_bytes / 1073741824)
    if size_bytes >= 1048576:
        return "{:.0f} MB".format(size_bytes / 1048576)
    return "{} B".format(size_bytes)


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
