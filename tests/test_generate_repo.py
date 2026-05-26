# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for repository metadata generation."""

import importlib.util
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADDON_DIR = REPO_ROOT / "repo" / "plugin.video.nzbdav"
REPO_ADDON_DIR = REPO_ROOT / "repo" / "repository.nzbdav"


def _load_generate_repo_module():
    script_path = REPO_ROOT / "scripts" / "generate_repo.py"
    spec = importlib.util.spec_from_file_location("generate_repo_script", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_generate_repo_writes_pages_root_files(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    module.generate_repo(output_dir=str(tmp_path / "repo" / "zips"))

    index_path = tmp_path / "index.html"
    assert index_path.exists()
    contents = index_path.read_text(encoding="utf-8")
    assert "repository.nzbdav-" in contents
    assert ".zip" in contents
    assert (tmp_path / ".nojekyll").exists()
    assert (tmp_path / "repo" / "zips" / "addons.xml").exists()


def test_generate_repo_root_index_links_current_addon_zip(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    release_zip_dir = tmp_path / "release-input"
    release_zip_dir.mkdir()
    release_zip = release_zip_dir / "plugin.video.nzbdav-1.2.1.zip"
    release_addon_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="plugin.video.nzbdav" name="NZB-DAV" version="1.2.1" />
"""
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.video.nzbdav/addon.xml", release_addon_xml)

    output_dir = tmp_path / "repo" / "zips"
    module.generate_repo(output_dir=str(output_dir), addon_zip=str(release_zip))

    contents = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "plugin.video.nzbdav-1.2.1.zip" in contents
    assert (tmp_path / "plugin.video.nzbdav-1.2.1.zip").exists()
    assert (
        output_dir / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.2.1.zip"
    ).exists()


def test_generate_repo_can_publish_legacy_root_metadata(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "repo" / "zips"
    module.generate_repo(output_dir=str(output_dir), legacy_root_metadata=True)

    assert (tmp_path / "addons.xml").read_bytes() == (
        output_dir / "addons.xml"
    ).read_bytes()
    assert (tmp_path / "addons.xml.md5").read_bytes() == (
        output_dir / "addons.xml.md5"
    ).read_bytes()


def test_generate_repo_legacy_root_metadata_mirrors_addon_directories(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    release_zip = tmp_path / "plugin.video.nzbdav-1.2.1.zip"
    legacy_zip_dir = tmp_path / "legacy-zips"
    legacy_zip_dir.mkdir()
    legacy_zip = legacy_zip_dir / "plugin.video.nzbdav-1.0.5.zip"

    for zip_path, version in ((release_zip, "1.2.1"), (legacy_zip, "1.0.5")):
        addon_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<addon id="plugin.video.nzbdav" name="NZB-DAV" version="{}" />'
        ).format(version)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("plugin.video.nzbdav/addon.xml", addon_xml)

    output_dir = tmp_path / "repo" / "zips"
    module.generate_repo(
        output_dir=str(output_dir),
        addon_zip=str(release_zip),
        legacy_addon_zip_dir=str(legacy_zip_dir),
        repo_zip_alias_versions=("1.0.6",),
        legacy_root_metadata=True,
    )

    assert (tmp_path / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.2.1.zip").exists()
    assert (tmp_path / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.0.5.zip").exists()
    assert (tmp_path / "repository.nzbdav" / "repository.nzbdav-1.1.0.zip").exists()
    assert (tmp_path / "repository.nzbdav" / "repository.nzbdav-1.0.6.zip").exists()


def test_generate_repo_html_indexes_use_standards_doctype(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    release_zip = tmp_path / "plugin.video.nzbdav-1.2.1.zip"
    release_addon_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="plugin.video.nzbdav" name="NZB-DAV" version="1.2.1" />
"""
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.video.nzbdav/addon.xml", release_addon_xml)

    output_dir = tmp_path / "repo" / "zips"
    module.generate_repo(output_dir=str(output_dir), addon_zip=str(release_zip))

    index_paths = [
        tmp_path / "index.html",
        output_dir / "plugin.video.nzbdav" / "index.html",
        output_dir / "repository.nzbdav" / "index.html",
    ]
    for index_path in index_paths:
        contents = index_path.read_text(encoding="utf-8").lower()
        assert contents.startswith("<!doctype html>\n")
        assert "<head>" in contents
        assert "<body>" in contents


def test_generate_repo_omits_full_changelog_from_repo_index(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "repo" / "zips"
    module.generate_repo(output_dir=str(output_dir))

    tree = ET.parse(output_dir / "addons.xml")
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    assert addon is not None
    metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
    assert metadata is not None
    assert metadata.find("news") is None


def test_generate_repo_includes_repository_checksum_url(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "repo" / "zips"
    module.generate_repo(output_dir=str(output_dir))

    tree = ET.parse(output_dir / "addons.xml")
    repo = tree.find("./addon[@id='repository.nzbdav']")
    assert repo is not None
    repo_dir = repo.find("./extension[@point='xbmc.addon.repository']/dir")
    repo_base = "https://raw.githubusercontent.com/xbmc4lyfe/nzbdavkodi/main/repo/zips"
    assert repo_dir is not None
    assert repo_dir.findtext("info") == "{}/addons.xml".format(repo_base)
    assert repo_dir.findtext("checksum") == "{}/addons.xml.md5".format(repo_base)
    assert repo_dir.findtext("datadir") == "{}/".format(repo_base)


def test_generate_repo_writes_strict_md5_payload(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "repo" / "zips"
    module.generate_repo(output_dir=str(output_dir))

    md5_payload = (output_dir / "addons.xml.md5").read_bytes()
    assert len(md5_payload) == 32
    assert md5_payload.decode("ascii").isalnum()


def test_generate_repo_can_publish_release_zip_instead_of_worktree_addon(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    release_zip = tmp_path / "release-addon.zip"
    release_addon_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="plugin.video.nzbdav" name="NZB-DAV" version="1.0.3">
    <extension point="xbmc.addon.metadata">
        <summary lang="en">Release addon</summary>
        <news>release notes are too large for repository metadata</news>
        <assets>
            <icon>resources/icon.png</icon>
            <fanart>resources/fanart.jpg</fanart>
        </assets>
    </extension>
</addon>
"""
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.video.nzbdav/addon.xml", release_addon_xml)
        zf.writestr("plugin.video.nzbdav/resources/icon.png", b"icon")
        zf.writestr("plugin.video.nzbdav/resources/fanart.jpg", b"fanart")

    output_dir = tmp_path / "repo" / "zips"
    module.generate_repo(output_dir=str(output_dir), addon_zip=str(release_zip))

    tree = ET.parse(output_dir / "addons.xml")
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    assert addon is not None
    assert addon.attrib["version"] == "1.0.3"
    metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
    assert metadata is not None
    assert metadata.find("news") is None
    assert (
        output_dir / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.0.3.zip"
    ).exists()
    assert (tmp_path / "plugin.video.nzbdav-1.0.3.zip").exists()
    assert not (output_dir / "plugin.video.nzbdav" / "release-addon.zip").exists()
    assert (
        output_dir / "plugin.video.nzbdav" / "resources" / "icon.png"
    ).read_bytes() == b"icon"


def test_generate_repo_preserves_legacy_addon_zips_for_cached_kodi_metadata(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    release_zip = tmp_path / "plugin.video.nzbdav-1.0.8.zip"
    legacy_zip_dir = tmp_path / "legacy-zips"
    legacy_zip_dir.mkdir()
    legacy_zip = legacy_zip_dir / "plugin.video.nzbdav-1.0.5.zip"

    for zip_path, version in ((release_zip, "1.0.8"), (legacy_zip, "1.0.5")):
        addon_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<addon id="plugin.video.nzbdav" name="NZB-DAV" version="{}" />'
        ).format(version)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("plugin.video.nzbdav/addon.xml", addon_xml)

    output_dir = tmp_path / "repo" / "zips"
    module.generate_repo(
        output_dir=str(output_dir),
        addon_zip=str(release_zip),
        legacy_addon_zip_dir=str(legacy_zip_dir),
    )

    tree = ET.parse(output_dir / "addons.xml")
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    assert addon is not None
    assert addon.attrib["version"] == "1.0.8"
    assert (
        output_dir / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.0.8.zip"
    ).exists()
    assert (
        output_dir / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.0.5.zip"
    ).exists()
    assert not (tmp_path / "plugin.video.nzbdav-1.0.5.zip").exists()


def test_generate_repo_writes_repository_zip_aliases_for_cached_kodi_metadata(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    module.generate_repo(
        output_dir=str(tmp_path),
        repo_zip_alias_versions=("1.0.6",),
    )

    current_repo_xml = ET.parse(REPO_ADDON_DIR / "addon.xml")
    current_repo_version = current_repo_xml.getroot().attrib["version"]
    alias_zip = tmp_path / "repository.nzbdav" / "repository.nzbdav-1.0.6.zip"
    assert alias_zip.exists()
    with zipfile.ZipFile(alias_zip) as zf:
        root = ET.fromstring(zf.read("repository.nzbdav/addon.xml"))
    assert root.attrib["version"] == current_repo_version


def test_parse_local_xml_rejects_doctype(tmp_path):
    module = _load_generate_repo_module()
    addon_xml = tmp_path / "addon.xml"
    addon_xml.write_text(
        '<!DOCTYPE addon [<!ENTITY secret "x">]>\n<addon id="x" version="1.0.0" />',
        encoding="utf-8",
    )

    with pytest.raises(module.ET.ParseError):
        module._parse_local_xml(str(addon_xml))
