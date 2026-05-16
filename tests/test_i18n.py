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
    # Fallback dict has 30011 = "Install TMDBHelper Player"
    assert result == "Install TMDBHelper Player"


def test_string_returns_sentinel_for_unknown_id():
    """When Kodi and _FALLBACK_STRINGS both lack the id, string() must
    return the visible sentinel ``"#<id>"`` and emit a LOGWARNING so
    missing translations surface in both the UI and the log instead of
    being silently dropped to ``""``."""
    import xbmc
    import xbmcaddon

    xbmcaddon.Addon().getLocalizedString.return_value = ""
    xbmc.log.reset_mock()
    result = string(99999)
    assert result == "#99999"
    xbmc.log.assert_called_once()
    msg, level = xbmc.log.call_args.args
    assert "missing localized string id=99999" in msg
    assert level == xbmc.LOGWARNING


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

    xbmcaddon.Addon.return_value.getLocalizedString.return_value = "Localized!"
    try:
        assert string(30011) == "Localized!"
    finally:
        xbmcaddon.Addon.return_value.getLocalizedString.return_value = ""


def test_string_falls_back_when_getLocalizedString_returns_non_string():
    """MagicMock / None / bytes from getLocalizedString must NOT be
    treated as a valid localization. Fall through to _FALLBACK_STRINGS."""
    import xbmcaddon

    original = xbmcaddon.Addon.return_value.getLocalizedString.return_value
    try:
        xbmcaddon.Addon.return_value.getLocalizedString.return_value = None
        assert string(30011) == "Install TMDBHelper Player"  # from _FALLBACK_STRINGS
    finally:
        xbmcaddon.Addon.return_value.getLocalizedString.return_value = original


def test_direct_indexer_strings_have_fallbacks():
    assert string(30163) == "Indexers"
    assert string(30165) == "Enable direct Newznab indexers"
    assert string(30176) == "No direct indexers configured"


def test_indexer_manager_strings_have_fallbacks():
    assert string(30195) == "Manage Indexers"
    assert string(30196) == "Refresh NZBHydra2 Caps"


def test_fmt_returns_id_and_args_for_missing_template():
    """When the resolved template is the ``#<id>`` sentinel, fmt() must
    surface both the missing id AND the args the caller passed —
    previously the empty-template + suffix path produced a
    leading-space gibberish like ` ('foo',)`."""
    import xbmcaddon

    xbmcaddon.Addon().getLocalizedString.return_value = ""
    result = fmt(99999, "Inception", year=2010)
    assert result.startswith("#99999")
    assert "Inception" in result
    # Must NOT start with a space (the old bug).
    assert not result.startswith(" ")


def test_strings_po_has_added_orphan_ids():
    """Settings.xml references 30160 / 30183 / 30184 / 30185 as labels
    but no msgctxt entry existed before this fix — Kodi rendered them
    as raw numbers. Verify the .po file now defines them."""
    import os

    po_path = os.path.join(
        "repo",
        "plugin.video.nzbdav",
        "resources",
        "language",
        "resource.language.en_gb",
        "strings.po",
    )
    with open(po_path, encoding="utf-8") as fh:
        content = fh.read()
    for needle in ('"#30160"', '"#30183"', '"#30184"', '"#30185"'):
        assert needle in content, "missing msgctxt {} in strings.po".format(needle)
    # Spot-check the labels match the spec.
    assert "Install Player Other" in content
    assert "Fallback Streams" in content
    assert "Enable fallback streams" in content
    assert "Maximum standby fallback streams" in content


def test_settings_xml_uses_30129_for_prowlarr_api_key():
    """The orphan #30129 ("Prowlarr API Key") msgctxt previously had
    no consumer — settings.xml reused the generic #30003 ("API Key")
    label for prowlarr_api_key. Verify the renumbering took effect so
    the existing translation actually lights up."""
    import os
    import re

    settings_path = os.path.join(
        "repo", "plugin.video.nzbdav", "resources", "settings.xml"
    )
    with open(settings_path, encoding="utf-8") as fh:
        content = fh.read()
    match = re.search(r'id="prowlarr_api_key"[^>]*label="(\d+)"', content)
    assert match is not None, "prowlarr_api_key setting not found"
    assert match.group(1) == "30129", (
        "prowlarr_api_key still references generic 30003 ('API Key') "
        "instead of orphan 30129 ('Prowlarr API Key')"
    )
