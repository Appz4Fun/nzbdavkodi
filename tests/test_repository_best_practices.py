# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Regression checks for repository and Kodi addon best-practice files."""

import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADDON_DIR = REPO_ROOT / "repo" / "plugin.video.nzbdav"
REPO_ADDON_DIR = REPO_ROOT / "repo" / "repository.nzbdav"


def test_addon_metadata_includes_repo_links_and_disclaimer():
    addon_xml = ADDON_DIR / "addon.xml"
    root = ET.parse(addon_xml).getroot()
    metadata = root.find("./extension[@point='xbmc.addon.metadata']")

    assert metadata is not None
    assert metadata.findtext("source") == "https://github.com/Appz4Fun/nzbdavkodi"
    assert metadata.findtext("website") == "https://appz4fun.github.io/nzbdavkodi/"
    disclaimers = metadata.findall("disclaimer")
    assert len(disclaimers) >= 2


def test_repository_addon_uses_live_pages_metadata_urls():
    addon_xml = REPO_ADDON_DIR / "addon.xml"
    root = ET.parse(addon_xml).getroot()
    repo_dir = root.find("./extension[@point='xbmc.addon.repository']/dir")

    assert repo_dir is not None
    assert (
        repo_dir.findtext("info") == "https://appz4fun.github.io/nzbdavkodi/addons.xml"
    )
    assert (
        repo_dir.findtext("checksum")
        == "https://appz4fun.github.io/nzbdavkodi/addons.xml.md5"
    )
    assert repo_dir.findtext("datadir") == "https://appz4fun.github.io/nzbdavkodi/"


def test_issue_template_contact_links_use_canonical_owner():
    issue_template = (
        REPO_ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
    ).read_text(encoding="utf-8")

    assert (
        "https://github.com/Appz4Fun/nzbdavkodi/blob/main/SUPPORT.md" in issue_template
    )
    assert (
        "https://github.com/Appz4Fun/nzbdavkodi/security/advisories/new"
        in issue_template
    )
    assert "https://github.com/xbmc4lyfe/nzbdavkodi" not in issue_template


def test_addon_news_metadata_is_tiny_current_release_summary():
    addon_xml = ADDON_DIR / "addon.xml"
    root = ET.parse(addon_xml).getroot()
    metadata = root.find("./extension[@point='xbmc.addon.metadata']")

    assert metadata is not None
    news = metadata.findtext("news") or ""
    version = root.get("version")
    summary = news.strip()
    assert summary.startswith("v{}: ".format(version))
    assert "\n" not in summary
    assert len(summary) < 80


def test_kodi_visible_changelog_is_tiny_current_release_summary():
    addon_xml = ADDON_DIR / "addon.xml"
    root = ET.parse(addon_xml).getroot()
    version = root.get("version")
    changelog = (ADDON_DIR / "changelog.txt").read_text(encoding="utf-8")
    summary = changelog.strip()

    assert summary.startswith("v{}: ".format(version))
    assert "\n" not in summary
    assert len(summary) < 80


def test_repo_changelog_keeps_full_release_history():
    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "## [1.2.1] — 2026-05-07" in changelog
    assert "## [1.2.0] — 2026-05-07" in changelog
    assert "## [0.1.0] — 2026-04-05" in changelog


def test_settings_labels_use_localized_string_ids():
    settings_xml = ADDON_DIR / "resources" / "settings.xml"
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
    settings_xml = ADDON_DIR / "resources" / "settings.xml"
    root = ET.parse(settings_xml).getroot()

    api_key_setting = root.find(".//setting[@id='prowlarr_api_key']")
    assert api_key_setting is not None
    assert api_key_setting.get("label") == "30129"

    test_action = root.find(
        ".//setting[@action='RunPlugin(plugin://plugin.video.nzbdav/test_prowlarr)']"
    )
    assert test_action is not None
    assert test_action.get("label") == "30131"


def test_settings_include_webdav_test_action():
    settings_xml = ADDON_DIR / "resources" / "settings.xml"
    root = ET.parse(settings_xml).getroot()

    test_action = root.find(
        ".//setting[@action='RunPlugin(plugin://plugin.video.nzbdav/test_webdav)']"
    )
    assert test_action is not None
    assert test_action.get("label") == "30188"


def test_language_file_exists_for_kodi_strings():
    strings_po = (
        ADDON_DIR / "resources" / "language" / "resource.language.en_gb" / "strings.po"
    )
    assert strings_po.exists()
    contents = strings_po.read_text(encoding="utf-8")
    assert 'msgctxt "#30000"' in contents
    assert 'msgctxt "#30112"' in contents


def test_settings_include_direct_indexers_category():
    settings_xml = ADDON_DIR / "resources" / "settings.xml"
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
    manage_action = indexers_category.find(
        ".//setting[@action='RunPlugin(plugin://plugin.video.nzbdav/manage_indexers)']"
    )
    assert manage_action is not None
    assert manage_action.get("label") == "30195"
    assert manage_action.get("option") == "close"
    assert (
        manage_action.get("visible")
        == "Addon.SettingBool(plugin.video.nzbdav,direct_indexers_enabled)"
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
