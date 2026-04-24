# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for kodi_advancedsettings.has_cache_memorysize_zero()."""

from unittest.mock import patch

from resources.lib.kodi_advancedsettings import has_cache_memorysize_zero


@patch("resources.lib.kodi_advancedsettings.xbmcvfs")
def test_returns_true_when_cache_memorysize_is_zero(mock_xbmcvfs, tmp_path):
    xml = tmp_path / "advancedsettings.xml"
    xml.write_text(
        "<advancedsettings>\n"
        "  <cache>\n"
        "    <memorysize>0</memorysize>\n"
        "  </cache>\n"
        "</advancedsettings>\n"
    )
    mock_xbmcvfs.translatePath.return_value = str(xml)

    assert has_cache_memorysize_zero() is True


@patch("resources.lib.kodi_advancedsettings.xbmcvfs")
def test_returns_false_when_cache_element_missing(mock_xbmcvfs, tmp_path):
    xml = tmp_path / "advancedsettings.xml"
    xml.write_text(
        "<advancedsettings>\n"
        "  <network>\n"
        "    <buffermode>1</buffermode>\n"
        "  </network>\n"
        "</advancedsettings>\n"
    )
    mock_xbmcvfs.translatePath.return_value = str(xml)

    assert has_cache_memorysize_zero() is False


@patch("resources.lib.kodi_advancedsettings.xbmcvfs")
def test_returns_false_when_memorysize_nonzero(mock_xbmcvfs, tmp_path):
    xml = tmp_path / "advancedsettings.xml"
    xml.write_text(
        "<advancedsettings>\n"
        "  <cache>\n"
        "    <memorysize>12345</memorysize>\n"
        "  </cache>\n"
        "</advancedsettings>\n"
    )
    mock_xbmcvfs.translatePath.return_value = str(xml)

    assert has_cache_memorysize_zero() is False


@patch("resources.lib.kodi_advancedsettings.xbmcvfs")
def test_returns_false_when_file_missing(mock_xbmcvfs, tmp_path):
    mock_xbmcvfs.translatePath.return_value = str(tmp_path / "does-not-exist.xml")

    assert has_cache_memorysize_zero() is False


@patch("resources.lib.kodi_advancedsettings.xbmcvfs")
def test_returns_false_when_xml_malformed(mock_xbmcvfs, tmp_path):
    xml = tmp_path / "advancedsettings.xml"
    xml.write_text("<advancedsettings><cache><memorysize>0</memorysize>")  # unclosed
    mock_xbmcvfs.translatePath.return_value = str(xml)

    assert has_cache_memorysize_zero() is False


@patch("resources.lib.kodi_advancedsettings.xbmcvfs")
def test_returns_true_when_memorysize_has_whitespace(mock_xbmcvfs, tmp_path):
    xml = tmp_path / "advancedsettings.xml"
    xml.write_text(
        "<advancedsettings>\n"
        "  <cache>\n"
        "    <memorysize>  0  </memorysize>\n"
        "  </cache>\n"
        "</advancedsettings>\n"
    )
    mock_xbmcvfs.translatePath.return_value = str(xml)

    assert has_cache_memorysize_zero() is True
