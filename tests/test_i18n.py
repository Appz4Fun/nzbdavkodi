# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from resources.lib.i18n import addon_name, fmt, string


def test_string_returns_value():
    """string() should return a string for a valid ID."""
    result = string(30011)
    assert isinstance(result, str)


def test_string_falls_back_for_empty_kodi_response():
    """string() should return fallback text when Kodi returns empty string."""
    import xbmcaddon

    xbmcaddon.Addon().getLocalizedString.return_value = ""
    result = string(30011)
    assert isinstance(result, str)
    # Fallback dict has 30011 = "Install Player File"
    assert result == "Install Player File"


def test_string_returns_empty_for_unknown_id():
    """string() should return '' for an ID not in fallback dict."""
    import xbmcaddon

    xbmcaddon.Addon().getLocalizedString.return_value = ""
    result = string(99999)
    assert result == ""


def test_fmt_formats_string():
    """fmt() should format a fallback string with positional arguments."""
    import xbmcaddon

    xbmcaddon.Addon().getLocalizedString.return_value = ""
    # 30083 = "Searching NZBHydra for {}..."
    result = fmt(30083, "Inception")
    assert isinstance(result, str)
    assert "Inception" in result


def test_addon_name_returns_string():
    """addon_name() should return a non-empty string."""
    result = addon_name()
    assert isinstance(result, str)
    assert len(result) > 0


def test_addon_name_falls_back_when_kodi_returns_empty():
    """addon_name() should return _FALLBACK_NAME when Kodi returns empty."""
    import xbmcaddon

    xbmcaddon.Addon().getAddonInfo.return_value = ""
    result = addon_name()
    assert result == "NZB-DAV"
