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


def test_addon_returns_none_when_kodi_not_registered():
    """During early service startup, ``xbmcaddon.Addon()`` can raise
    RuntimeError("unknown addon id"). The helper must swallow that and
    return None so callers fall through to their fallback instead of
    crashing the service entry point."""
    import xbmcaddon
    from resources.lib.i18n import addon

    original = xbmcaddon.Addon
    try:

        def _raise_runtime(*_a, **_kw):
            raise RuntimeError("unknown addon id")

        xbmcaddon.Addon = _raise_runtime
        assert addon() is None
    finally:
        xbmcaddon.Addon = original


def test_addon_name_returns_fallback_when_addon_none():
    """When addon() returns None (Kodi not registered), addon_name must
    return the hardcoded fallback rather than raising AttributeError
    on a None.getAddonInfo call."""
    import xbmcaddon
    from resources.lib.i18n import addon_name

    original = xbmcaddon.Addon
    try:

        def _raise_runtime(*_a, **_kw):
            raise RuntimeError("unknown addon id")

        xbmcaddon.Addon = _raise_runtime
        assert addon_name() == "NZB-DAV"
    finally:
        xbmcaddon.Addon = original


def test_string_returns_localized_value_when_kodi_provides_one():
    """When getLocalizedString returns a non-empty str, use it as-is
    rather than falling back to the bundled dict."""
    import xbmcaddon
    from resources.lib.i18n import string

    xbmcaddon.Addon.return_value.getLocalizedString.return_value = "Localized!"
    try:
        assert string(30011) == "Localized!"
    finally:
        xbmcaddon.Addon.return_value.getLocalizedString.return_value = ""


def test_string_falls_back_when_getLocalizedString_returns_non_string():
    """MagicMock / None / bytes from getLocalizedString must NOT be
    treated as a valid localization. Fall through to _FALLBACK_STRINGS."""
    import xbmcaddon
    from resources.lib.i18n import string

    original = xbmcaddon.Addon.return_value.getLocalizedString.return_value
    try:
        xbmcaddon.Addon.return_value.getLocalizedString.return_value = None
        assert string(30011) == "Install Player File"  # from _FALLBACK_STRINGS
    finally:
        xbmcaddon.Addon.return_value.getLocalizedString.return_value = original
