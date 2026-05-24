# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Regression checks for repository and Kodi addon best-practice files."""

import hashlib
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ADDON_DIR = REPO_ROOT / "repo" / "plugin.video.nzbdav"
REPO_ADDON_DIR = REPO_ROOT / "repo" / "repository.nzbdav"


def _setup_wizard_skin_root():
    skin_xml = (
        ADDON_DIR / "resources" / "skins" / "Default" / "1080i" / "setup-wizard.xml"
    )
    return ET.parse(skin_xml).getroot()


def _int_text(element, child_name):
    return int(element.findtext(child_name))


def test_addon_metadata_includes_repo_links_and_disclaimer():
    addon_xml = ADDON_DIR / "addon.xml"
    root = ET.parse(addon_xml).getroot()
    metadata = root.find("./extension[@point='xbmc.addon.metadata']")

    assert metadata is not None
    assert metadata.findtext("source") == "https://github.com/Appz4Fun/nzbdavkodi"
    assert metadata.findtext("website") == "https://appz4fun.github.io/nzbdavkodi/"
    disclaimers = metadata.findall("disclaimer")
    assert len(disclaimers) >= 2


def test_repository_addon_uses_pages_sha256_metadata_urls():
    addon_xml = REPO_ADDON_DIR / "addon.xml"
    root = ET.parse(addon_xml).getroot()
    repo_dir = root.find("./extension[@point='xbmc.addon.repository']/dir")
    repo_base = "https://appz4fun.github.io/nzbdavkodi"

    assert repo_dir is not None
    info = repo_dir.find("info")
    checksum = repo_dir.find("checksum")
    datadir = repo_dir.find("datadir")
    artdir = repo_dir.find("artdir")
    assert info is not None
    assert info.text == "{}/addons.xml.gz".format(repo_base)
    assert "compressed" not in info.attrib
    assert checksum is not None
    assert checksum.text == "{}/addons.xml.gz.sha256".format(repo_base)
    assert checksum.get("verify") == "sha256"
    assert datadir is not None
    assert datadir.text == repo_base
    assert "zip" not in datadir.attrib
    assert artdir is not None
    assert artdir.text == repo_base
    assert repo_dir.findtext("hashes") == "sha256"


def test_legacy_raw_repo_zips_endpoint_migrates_to_pages_repository():
    legacy_dir = REPO_ROOT / "repo" / "zips"
    addons_xml = legacy_dir / "addons.xml"
    checksum = legacy_dir / "addons.xml.md5"
    repository_zip = legacy_dir / "repository.nzbdav-1.1.1.zip"

    assert addons_xml.exists()
    assert checksum.exists()
    assert repository_zip.exists()
    assert (
        checksum.read_text(encoding="ascii")
        == hashlib.md5(addons_xml.read_bytes()).hexdigest()
    )

    root = ET.parse(addons_xml).getroot()
    repo = root.find("./addon[@id='repository.nzbdav']")
    assert repo is not None
    assert repo.get("version") == "1.1.1"

    repo_dir = repo.find("./extension[@point='xbmc.addon.repository']/dir")
    repo_base = "https://appz4fun.github.io/nzbdavkodi"
    assert repo_dir is not None
    assert repo_dir.findtext("info") == "{}/addons.xml.gz".format(repo_base)
    assert repo_dir.findtext("checksum") == "{}/addons.xml.gz.sha256".format(repo_base)
    assert repo_dir.find("checksum").get("verify") == "sha256"
    assert repo_dir.findtext("datadir") == repo_base
    assert repo_dir.findtext("artdir") == repo_base
    assert repo_dir.findtext("hashes") == "sha256"

    with zipfile.ZipFile(repository_zip) as zf:
        assert "repository.nzbdav/addon.xml" in zf.namelist()


def test_agents_release_guidance_uses_pages_not_raw_repo_zips():
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "raw.githubusercontent.com" not in agents
    assert "regenerate it with `just repo`" not in agents
    assert "generate Kodi repo in repo/zips" not in agents
    assert "served from GitHub Pages" in agents
    assert "legacy migration endpoint" in agents


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


def test_settings_include_hidden_setup_wizard_completion_marker():
    settings_xml = ADDON_DIR / "resources" / "settings.xml"
    root = ET.parse(settings_xml).getroot()

    setting = root.find(".//setting[@id='setup_wizard_completed']")
    assert setting is not None
    assert setting.get("type") == "bool"
    assert setting.get("default") == "false"
    assert setting.get("visible") == "false"


def test_settings_include_setup_wizard_action():
    settings_xml = ADDON_DIR / "resources" / "settings.xml"
    root = ET.parse(settings_xml).getroot()

    action = root.find(
        ".//setting[@action='RunPlugin(plugin://plugin.video.nzbdav/setup_wizard)']"
    )
    assert action is not None
    assert action.get("label") == "30197"
    assert action.get("option") == "close"


def test_setup_wizard_xml_skin_exists_with_expected_controls():
    root = _setup_wizard_skin_root()

    control_ids = {
        control.get("id")
        for control in root.findall(".//control")
        if control.get("id") is not None
    }
    for control_id in ("50", "101", "102", "103", "104", "106", "107"):
        assert control_id in control_ids

    for removed_id in ("105", "108", "109", "110", "111", "112", "113"):
        assert removed_id not in control_ids


def test_setup_wizard_field_list_is_centered_single_column_layout():
    root = _setup_wizard_skin_root()
    field_list = root.find(".//control[@id='50']")

    assert field_list is not None
    left = _int_text(field_list, "left")
    width = _int_text(field_list, "width")
    center = left + (width / 2)

    assert 900 <= center <= 1020
    assert left < 700
    assert width >= 760
    assert _int_text(field_list, "top") > 250


def test_setup_wizard_rows_have_label_helper_value_columns():
    root = _setup_wizard_skin_root()
    item_layout = root.find(".//control[@id='50']/itemlayout")
    focused_layout = root.find(".//control[@id='50']/focusedlayout")

    assert item_layout is not None
    assert focused_layout is not None
    for layout in (item_layout, focused_layout):
        labels = layout.findall("control[@type='label']")
        label_infos = [label.findtext("label") for label in labels]

        assert "$INFO[ListItem.Label]" in label_infos
        assert "$INFO[ListItem.Property(helper)]" in label_infos
        assert "$INFO[ListItem.Property(value)]" in label_infos


def test_setup_wizard_focused_row_uses_results_style_left_accent():
    root = _setup_wizard_skin_root()
    focused_layout = root.find(".//control[@id='50']/focusedlayout")

    assert focused_layout is not None
    accent = None
    for image in focused_layout.findall("control[@type='image']"):
        if image.findtext("colordiffuse") == "FF6BB6FF":
            accent = image
            break

    assert accent is not None
    assert _int_text(accent, "left") == 0
    assert _int_text(accent, "width") == 6


def test_setup_wizard_skin_uses_xml_drawn_square_rows_only():
    root = _setup_wizard_skin_root()
    textures = [
        texture.text or ""
        for texture in root.findall(".//texture")
        if texture.text is not None
    ]

    assert textures
    assert set(textures) == {"white.png"}
    assert not any("round" in texture.lower() for texture in textures)


def test_setup_wizard_footer_buttons_are_keyboard_navigable():
    root = _setup_wizard_skin_root()

    expected_nav = {
        "50": {"ondown": "102"},
        "101": {"onup": "50", "onright": "104"},
        "104": {"onup": "50", "onleft": "101", "onright": "102"},
        "102": {"onup": "50", "onleft": "104", "onright": "103"},
        "103": {"onup": "50", "onleft": "102"},
    }

    for control_id, nav in expected_nav.items():
        control = root.find(".//control[@id='{}']".format(control_id))
        assert control is not None
        for direction, target_id in nav.items():
            assert control.findtext(direction) == target_id


def test_setup_wizard_footer_uses_only_one_stable_button_set():
    root = _setup_wizard_skin_root()

    previous = root.find(".//control[@id='101']")
    test = root.find(".//control[@id='104']")
    next_or_finish = root.find(".//control[@id='102']")
    cancel = root.find(".//control[@id='103']")

    assert previous is not None
    assert previous.findtext("visible") == (
        "String.IsEqual(Window.Property(wizard.previous_visible),true)"
    )
    assert test is not None
    assert test.findtext("visible") == (
        "String.IsEqual(Window.Property(wizard.test_visible),true)"
    )
    assert next_or_finish is not None
    assert next_or_finish.findtext("visible") == (
        "String.IsEqual(Window.Property(wizard.next_visible),true)"
    )
    assert cancel is not None
    assert cancel.findtext("visible") == (
        "String.IsEqual(Window.Property(wizard.cancel_visible),true)"
    )


def test_setup_wizard_buttons_are_tall_and_centered():
    skin_xml = (
        ADDON_DIR / "resources" / "skins" / "Default" / "1080i" / ("setup-wizard.xml")
    )
    root = ET.parse(skin_xml).getroot()

    for control_id in ("101", "102", "103", "104"):
        control = root.find(".//control[@id='{}']".format(control_id))
        assert control is not None
        assert int(control.findtext("height")) == 120
        assert control.findtext("align") == "center"
        assert control.findtext("aligny") == "center"


def test_setup_wizard_heading_and_welcome_text_are_centered():
    skin_xml = (
        ADDON_DIR / "resources" / "skins" / "Default" / "1080i" / ("setup-wizard.xml")
    )
    root = ET.parse(skin_xml).getroot()

    title = root.find(".//control[@id='1']")
    welcome = root.find(".//control[@id='6']")
    warning = root.find(".//control[@id='107']")

    assert title is not None
    assert title.findtext("align") == "center"
    assert welcome is not None
    assert welcome.findtext("align") == "center"
    assert welcome.findtext("visible") == (
        "String.IsEqual(Window.Property(wizard.welcome_visible),true)"
    )
    assert warning is not None
    assert warning.findtext("align") == "center"


def test_setup_wizard_action_button_has_wide_focus_area():
    skin_xml = (
        ADDON_DIR / "resources" / "skins" / "Default" / "1080i" / ("setup-wizard.xml")
    )
    root = ET.parse(skin_xml).getroot()

    action = root.find(".//control[@id='104']")

    assert action is not None
    assert int(action.findtext("width")) >= 280


def test_setup_wizard_final_page_uses_next_button_as_finish():
    skin_xml = (
        ADDON_DIR / "resources" / "skins" / "Default" / "1080i" / ("setup-wizard.xml")
    )
    root = ET.parse(skin_xml).getroot()

    test_button = root.find(".//control[@id='104']")
    previous_button = root.find(".//control[@id='101']")
    next_button = root.find(".//control[@id='102']")
    cancel_button = root.find(".//control[@id='103']")

    assert previous_button is not None
    assert previous_button.findtext("onright") == "104"
    assert test_button is not None
    assert test_button.findtext("onleft") == "101"
    assert test_button.findtext("onright") == "102"
    assert test_button.findtext("visible") == (
        "String.IsEqual(Window.Property(wizard.test_visible),true)"
    )
    assert next_button is not None
    assert next_button.findtext("onleft") == "104"
    assert next_button.findtext("onright") == "103"
    assert next_button.findtext("visible") == (
        "String.IsEqual(Window.Property(wizard.next_visible),true)"
    )
    assert cancel_button is not None
    assert cancel_button.findtext("onleft") == "102"


def test_setup_wizard_list_rows_do_not_render_secondary_kind_text():
    skin_xml = (
        ADDON_DIR / "resources" / "skins" / "Default" / "1080i" / ("setup-wizard.xml")
    )
    contents = skin_xml.read_text(encoding="utf-8")

    assert "ListItem.Property(kind)" not in contents


def test_setup_wizard_uses_modal_connection_feedback_not_inline_status_labels():
    skin_xml = (
        ADDON_DIR / "resources" / "skins" / "Default" / "1080i" / ("setup-wizard.xml")
    )
    contents = skin_xml.read_text(encoding="utf-8")

    assert "wizard.status" not in contents
    assert "wizard.status_kind" not in contents


def test_wizard_strings_exist():
    strings_po = (
        ADDON_DIR / "resources" / "language" / "resource.language.en_gb" / "strings.po"
    )
    contents = strings_po.read_text(encoding="utf-8")

    expected_ids = (
        30197,
        30198,
        30199,
        30200,
        30201,
        30202,
        30203,
        30204,
        30205,
        30206,
        30207,
        30208,
        30209,
        30210,
        30211,
        30212,
        30213,
        30215,
        30216,
        30217,
        30218,
        30219,
        30220,
        30223,
        30224,
        30225,
        30226,
        30227,
        30228,
        30230,
        30231,
        30232,
        30233,
    )
    for string_id in expected_ids:
        assert 'msgctxt "#{}"'.format(string_id) in contents


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
