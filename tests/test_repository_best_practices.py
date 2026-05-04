# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Regression checks for repository and Kodi addon best-practice files."""

import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_addon_metadata_includes_repo_links_and_disclaimer():
    addon_xml = REPO_ROOT / "plugin.video.nzbdav" / "addon.xml"
    root = ET.parse(addon_xml).getroot()
    metadata = root.find("./extension[@point='xbmc.addon.metadata']")

    assert metadata is not None
    assert metadata.findtext("source") == "https://github.com/xbmc4lyfe/nzbdavkodi"
    assert metadata.findtext("website") == "https://xbmc4lyfe.github.io/nzbdavkodi/"
    disclaimers = metadata.findall("disclaimer")
    assert len(disclaimers) >= 2


def test_settings_labels_use_localized_string_ids():
    settings_xml = REPO_ROOT / "plugin.video.nzbdav" / "resources" / "settings.xml"
    root = ET.parse(settings_xml).getroot()

    for category in root.findall("category"):
        assert category.get("label", "").isdigit()
        for setting in category.findall("setting"):
            label = setting.get("label")
            if label is not None:
                assert label.isdigit()

    sort_setting = root.find(".//setting[@id='sort_order']")
    assert sort_setting is not None
    assert sort_setting.get("lvalues") == "30077|30078|30079|30080|30081"


def test_prowlarr_api_key_label_is_not_reused_for_test_action():
    settings_xml = REPO_ROOT / "plugin.video.nzbdav" / "resources" / "settings.xml"
    root = ET.parse(settings_xml).getroot()

    api_key_setting = root.find(".//setting[@id='prowlarr_api_key']")
    assert api_key_setting is not None
    assert api_key_setting.get("label") == "30003"

    test_action = root.find(
        ".//setting[@action='RunPlugin(plugin://plugin.video.nzbdav/test_prowlarr)']"
    )
    assert test_action is not None
    assert test_action.get("label") == "30131"


def test_language_file_exists_for_kodi_strings():
    strings_po = (
        REPO_ROOT
        / "plugin.video.nzbdav"
        / "resources"
        / "language"
        / "resource.language.en_gb"
        / "strings.po"
    )
    assert strings_po.exists()
    contents = strings_po.read_text(encoding="utf-8")
    assert 'msgctxt "#30000"' in contents
    assert 'msgctxt "#30112"' in contents


def test_settings_include_direct_indexers_category():
    settings_xml = REPO_ROOT / "plugin.video.nzbdav" / "resources" / "settings.xml"
    root = ET.parse(settings_xml).getroot()

    indexers_category = root.find("./category[@label='30163']")
    assert indexers_category is not None
    assert (
        indexers_category.find(".//setting[@id='direct_indexers_enabled']") is not None
    )
    assert (
        indexers_category.find(".//setting[@id='direct_indexer_nzbgeek_api_key']")
        is not None
    )
    assert (
        indexers_category.find(
            ".//setting[@action='RunPlugin(plugin://plugin.video.nzbdav/test_direct_indexers)']"
        )
        is not None
    )


def test_community_health_files_exist():
    expected = [
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / "CODE_OF_CONDUCT.md",
        REPO_ROOT / "SUPPORT.md",
        REPO_ROOT / ".github" / "CODEOWNERS",
        REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml",
    ]
    for path in expected:
        assert path.exists(), "{} is missing".format(path)
