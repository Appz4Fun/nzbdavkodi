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

ACTION_SELECT = 7
ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92
ACTION_CONTEXT_MENU = 117

LIST_ID = 50


def _available_text():
    return _c(_AVAILABLE_LABEL, "FF22C55E")


def _build_result_item(result, row_index):
    """Build a styled ListItem for a single search result row."""
    meta = result.get("_meta", {})
    is_pack = bool(result.get("_season_pack"))
    label = result.get("_display_title") or result.get("title", "")
    li = xbmcgui.ListItem(label=label)

    res = meta.get("resolution", "")
    li.setProperty("resolution", _c(res, _RES_COLORS.get(res, "FFEEEEEE")))

    hdr_list = meta.get("hdr", [])
    if hdr_list:
        li.setProperty("hdr", _c(" ".join(hdr_list), "FFFBBF24"))
    elif not is_pack:
        li.setProperty("hdr", _c("SDR", "FF6B7280"))
    else:
        li.setProperty("hdr", "")

    li.setProperty("codec", _c(meta.get("codec", ""), "FF94A3B8"))

    audio_list = meta.get("audio", [])
    audio_str = " ".join(audio_list) if audio_list else ""
    li.setProperty("audio", _c(audio_str, "FFE879A8"))

    quality = meta.get("quality", "")
    src_display = _SRC_SHORT.get(quality, quality)
    li.setProperty("quality", _c(src_display, _SRC_COLORS.get(quality, "FFAAAAAA")))

    # Container (MKV, MP4, etc.) — default to MKV since most scene releases
    # are MKV and only MP4 releases tag the title.
    default_container = "" if is_pack else "MKV"
    container = (meta.get("container", "") or default_container).upper()
    # MKV green; everything else (incl. MP4, which is fully supported via the
    # stream proxy) gets neutral grey rather than error-red, so a supported
    # container is not flagged as a false negative.
    container_color = "FF34D399" if container == "MKV" else "FFA1A1AA"
    li.setProperty("container", _c(container, container_color))

    li.setProperty("size", _c(_format_size(result.get("size")), "FFA1A1AA"))
    li.setProperty("age", _c(result.get("age", ""), "FF6B7280"))
    li.setProperty("indexer", _c(result.get("indexer", ""), "FF4A9EFF"))
    li.setProperty("group", _c(meta.get("group", ""), "FF34D399"))

    if result.get("_available"):
        li.setProperty("available", _available_text())

    reject = result.get("_filter_reject")
    li.setProperty(
        "filter_reason", _c(_fmt(30371, reject), "FFFBBF24") if reject else ""
    )

    # Alternating row background
    li.setProperty("row_bg", _BG_A if row_index % 2 == 0 else _BG_B)
    return li


def _provider_only(results):
    """``results`` with the synthetic local season-pack row excluded.

    ``_prepend_pack`` inserts that row into both the filtered and
    all-rows views, so it is always non-empty/truthy even when zero
    online provider results survived filtering. Callers deciding
    "did anything real pass the filters" must check this, not the raw
    list.
    """
    return [
        row
        for row in results
        if not (isinstance(row, dict) and row.get("_season_pack"))
    ]


class ResultsDialog(xbmcgui.WindowXMLDialog):
    """Full-screen NZB results selection dialog with a show-all toggle."""

    def __init__(self, *args, **kwargs):
        self.results = kwargs.get("results", [])
        self.all_results = kwargs.get("all_results") or []
        self.title = kwargs.get("title", "")
        self.year = kwargs.get("year", "")
        self.total_count = kwargs.get("total_count", 0)
        self.show_all = not _provider_only(self.results) and bool(self.all_results)
        self.selected_result = None
        super().__init__(*args)

    def _toggle_available(self):
        """Whether the context-menu press flips views instead of cancelling.

        Requires a distinct, non-empty unfiltered list AND a non-empty
        filtered list — toggling into an empty filtered view is useless,
        so a zero-survivors picker stays pinned to show-all.
        """
        return (
            bool(self.results)
            and bool(self.all_results)
            and list(self.all_results) != list(self.results)
        )

    def _active_results(self):
        return self.all_results if self.show_all else self.results

    def _footer_hints(self):
        if not self._toggle_available():
            return _string(30370)
        if self.show_all:
            return _string(30369)
        return _fmt(30368, len(self.all_results) - len(self.results))

    def _populate(self):
        """(Re)build the list control and window properties for the active view."""
        active = self._active_results()
        title_display = self.title
        if self.year:
            title_display = "{} ({})".format(self.title, self.year)
        self.setProperty("title", title_display)
        self.setProperty("count", _fmt(30110, len(active)))
        self.setProperty("sort_info", _string(30111))
        if self.show_all:
            self.setProperty("filter_info", _fmt(30367, len(active)))
            self.setProperty("empty_message", _fmt(30087, title_display))
        else:
            self.setProperty("filter_info", _fmt(30112, len(active), self.total_count))
            self.setProperty("empty_message", _fmt(30089, title_display))
        self.setProperty("footer_hints", self._footer_hints())

        list_control = self.getControl(LIST_ID)
        list_control.reset()
        list_control.addItems(
            [_build_result_item(result, i) for i, result in enumerate(active)]
        )

    def onInit(self):
        """Populate the dialog with results data."""
        self._populate()
        self.setFocusId(LIST_ID)

    def _toggle_show_all(self):
        """Flip views, repopulate, and re-focus the same row by identity."""
        previous = self._active_results()
        pos = self.getControl(LIST_ID).getSelectedPosition()
        focused = (
            previous[pos] if isinstance(pos, int) and 0 <= pos < len(previous) else None
        )
        self.show_all = not self.show_all
        self._populate()
        active = self._active_results()
        index = 0
        if focused is not None:
            for i, row in enumerate(active):
                if row is focused:
                    index = i
                    break
        self.getControl(LIST_ID).selectItem(index)
        self.setFocusId(LIST_ID)

    def _select_focused(self):
        """Record the focused row of the active view and close."""
        pos = self.getControl(LIST_ID).getSelectedPosition()
        active = self._active_results()
        if isinstance(pos, int) and 0 <= pos < len(active):
            self.selected_result = active[pos]
        else:
            self.selected_result = None
        self.close()

    def _cancel(self):
        self.selected_result = None
        self.close()

    def onClick(self, controlId):
        """Handle item selection."""
        if controlId == LIST_ID:
            self._select_focused()

    def onAction(self, action):
        """Handle keyboard/remote actions."""
        action_id = action.getId()
        if action_id in (ACTION_SELECT,):
            if self.getFocusId() == LIST_ID:
                self._select_focused()
        elif action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self._cancel()
        elif action_id == ACTION_CONTEXT_MENU:
            # Context menu toggles the show-all view when one exists;
            # otherwise it keeps its close-as-cancel behavior so the user
            # isn't trapped if they don't want any presented result.
            if self._toggle_available():
                self._toggle_show_all()
            else:
                self._cancel()

    def get_selected_result(self):
        """Return the chosen result dict, or ``None`` when cancelled."""
        return self.selected_result


def show_results_dialog(results, title="", year="", total_count=0, all_results=None):
    """Show the full-screen results dialog and wait for the user pick.

    Args:
        results: List of filtered result dicts (must include
            ``title``, ``size``, ``_meta`` per ``filter_results``).
        title: Movie or show title for the dialog heading.
        year: Release year (movies) or show year (episodes),
            displayed beside the title.
        total_count: Number of results before filtering, used to
            render "Showing N of M" in the dialog header.
        all_results: Optional unfiltered row list (same dict objects as
            ``results`` plus the filter-rejected ones). When provided and
            distinct, the context-menu button toggles the dialog between
            the filtered and show-all views; when ``results`` is empty the
            dialog opens directly in show-all mode.

    Returns:
        The chosen result dict (same object reference from whichever view
        was active) when the user picks one, or ``None`` when the user
        cancels the dialog or no results are available.
    """
    addon = xbmcaddon.Addon("plugin.video.nzbdav")
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
        all_results=all_results,
    )
    dialog.doModal()

    selected = dialog.get_selected_result()
    del dialog
    return selected


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
