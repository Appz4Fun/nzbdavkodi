# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""No-getter settings reads must never touch the xbmcaddon binding.

A gdb backtrace from the extreme harness showed Kodi SIGSEGVing inside
TinyXML (``CAddonSettings::Load`` via ``CAddon::GetSetting``) when two
threads race the lazy user-settings load — the fallback prewarm burst
plus service-side prevalidation was enough to kill the Kodi process
mid-playback. Every ``settings_getter=None`` branch reachable from a
background thread therefore reads settings.xml from disk (via
``router._get_script_setting``) instead of the binding. These tests pin
that contract: the binding constructor raising means any regression
fails loudly.
"""

from unittest.mock import patch

_DISK = {
    "nzbdav_url": "http://nzbdav:3000/",
    "nzbdav_api_key": "diskkey",
    "hydra_url": "http://hydra:5076/",
    "hydra_api_key": "hydrakey",
    "max_results": "42",
    "submit_timeout": "120",
    "fallback_streams_enabled": "false",
    "fallback_streams_max": "3",
    "fallback_submit_delay": "45",
    "webdav_url": "http://webdav:3000/",
    "webdav_username": "u",
    "webdav_password": "p",
    "webdav_content_root": "media",
    "prowlarr_host": "http://prowlarr:9696/",
    "prowlarr_api_key": "pk",
    "prowlarr_indexer_ids": "1, 2",
}


def _disk_getter(key, default=""):
    return _DISK.get(key, default)


def _forbid_binding(module_path):
    """Patch a module's xbmcaddon.Addon to explode on construction."""
    return patch(
        "{}.xbmcaddon.Addon".format(module_path),
        side_effect=AssertionError(
            "xbmcaddon binding read from a no-getter path (thread-unsafe)"
        ),
    )


def test_nzbdav_api_settings_read_from_disk():
    from resources.lib import nzbdav_api

    with patch(
        "resources.lib.router._get_script_setting", side_effect=_disk_getter
    ), _forbid_binding("resources.lib.nzbdav_api"):
        assert nzbdav_api._get_settings() == ("http://nzbdav:3000", "diskkey")
        assert nzbdav_api._get_submit_timeout() == 120


def test_hydra_settings_read_from_disk():
    from resources.lib import hydra

    with patch(
        "resources.lib.router._get_script_setting", side_effect=_disk_getter
    ), _forbid_binding("resources.lib.hydra"):
        assert hydra._get_settings() == ("http://hydra:5076", "hydrakey")


def test_resolver_fallback_flags_read_from_disk():
    from resources.lib import resolver

    with patch(
        "resources.lib.router._get_script_setting", side_effect=_disk_getter
    ), _forbid_binding("resources.lib.resolver"):
        assert resolver._fallback_streams_enabled() is False
        assert resolver._get_fallback_submit_delay_seconds() == 45


def test_fallback_settings_read_from_disk():
    from resources.lib import fallback_streams

    with patch(
        "resources.lib.router._get_script_setting", side_effect=_disk_getter
    ), _forbid_binding("resources.lib.fallback_streams"):
        assert fallback_streams._fallback_settings() == (False, 3)


def test_filter_settings_read_from_disk():
    from resources.lib import filter as filter_mod

    with patch(
        "resources.lib.router._get_script_setting", side_effect=_disk_getter
    ), _forbid_binding("resources.lib.filter"):
        settings = filter_mod._get_filter_settings()
    assert settings["max_results"] == 42


def test_probe_stream_bases_never_touch_binding():
    from resources.lib import fallback_streams

    with patch(
        "resources.lib.router._get_script_setting", side_effect=_disk_getter
    ), _forbid_binding("resources.lib.fallback_streams"):
        bases = fallback_streams._configured_stream_bases()
    hosts = {parts[1] for parts in bases}
    assert "webdav:3000" in hosts


def test_prowlarr_settings_read_from_disk():
    from resources.lib import prowlarr

    with patch(
        "resources.lib.router._get_script_setting", side_effect=_disk_getter
    ), patch(
        "resources.lib.prowlarr.xbmcaddon",
        create=True,
    ) as mock_mod:
        mock_mod.Addon.side_effect = AssertionError("binding read")
        host, api_key, indexer_ids = prowlarr._get_settings()[:3]
    assert host == "http://prowlarr:9696"
    assert api_key == "pk"
    assert indexer_ids == ["1", "2"]


def test_webdav_content_root_read_from_disk():
    from resources.lib import webdav

    with patch(
        "resources.lib.router._get_script_setting", side_effect=_disk_getter
    ), _forbid_binding("resources.lib.webdav"):
        assert webdav._probe_content_root(None) == "media"


def test_resolver_submit_timeout_read_from_disk():
    from resources.lib import resolver

    with patch(
        "resources.lib.router._get_script_setting", side_effect=_disk_getter
    ), _forbid_binding("resources.lib.resolver"):
        assert resolver._get_submit_timeout_seconds() == 120


def test_disk_getter_falls_back_to_schema_defaults(tmp_path, monkeypatch):
    """Kodi's profile XML omits settings left at their defaults; the
    binding returned the SCHEMA default for those, so the disk reader
    must too — otherwise default nzbdav URLs blank out and filter
    toggles silently flip (PR #438 review findings)."""
    from resources.lib import router

    empty_profile = tmp_path / "settings.xml"
    empty_profile.write_text('<settings version="2"></settings>')
    monkeypatch.setattr(
        "resources.lib.router._script_settings_paths",
        lambda: [str(empty_profile)],
    )
    assert router._get_script_setting("nzbdav_url", "") == "http://localhost:3000"
    assert router._get_script_setting("fallback_streams_enabled", "") == "true"
    # Unknown key: caller default wins.
    assert router._get_script_setting("no_such_setting", "zz") == "zz"


def test_disk_getter_prefers_user_value_over_schema(tmp_path, monkeypatch):
    from resources.lib import router

    profile = tmp_path / "settings.xml"
    profile.write_text(
        '<settings version="2">'
        '<setting id="nzbdav_url">http://server:9999</setting>'
        "</settings>"
    )
    monkeypatch.setattr(
        "resources.lib.router._script_settings_paths",
        lambda: [str(profile)],
    )
    assert router._get_script_setting("nzbdav_url", "") == "http://server:9999"
