# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for repository metadata generation."""

import importlib.util
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


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

    module.generate_repo(output_dir=str(tmp_path))

    index_path = tmp_path / "index.html"
    assert index_path.exists()
    contents = index_path.read_text(encoding="utf-8")
    assert "repository.nzbdav-" in contents
    assert ".zip" in contents
    assert (tmp_path / ".nojekyll").exists()


def test_generate_repo_omits_full_changelog_from_repo_index(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    module.generate_repo(output_dir=str(tmp_path))

    tree = ET.parse(tmp_path / "addons.xml")
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    assert addon is not None
    metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
    assert metadata is not None
    assert metadata.find("news") is None


def test_generate_repo_includes_repository_checksum_url(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    module.generate_repo(output_dir=str(tmp_path))

    tree = ET.parse(tmp_path / "addons.xml")
    repo = tree.find("./addon[@id='repository.nzbdav']")
    assert repo is not None
    repo_dir = repo.find("./extension[@point='xbmc.addon.repository']/dir")
    assert repo_dir is not None
    assert (
        repo_dir.findtext("checksum")
        == "https://xbmc4lyfe.github.io/nzbdavkodi/addons.xml.md5"
    )


def test_generate_repo_writes_crlf_terminated_md5_payload(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    module.generate_repo(output_dir=str(tmp_path))

    md5_payload = (tmp_path / "addons.xml.md5").read_bytes()
    assert len(md5_payload) == 34
    assert md5_payload.endswith(b"\r\n")
    assert md5_payload[:32].decode("ascii").isalnum()


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

    module.generate_repo(output_dir=str(tmp_path / "dist"), addon_zip=str(release_zip))

    tree = ET.parse(tmp_path / "dist" / "addons.xml")
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    assert addon is not None
    assert addon.attrib["version"] == "1.0.3"
    metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
    assert metadata is not None
    assert metadata.find("news") is None
    assert (
        tmp_path / "dist" / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.0.3.zip"
    ).exists()
    assert (tmp_path / "dist" / "plugin.video.nzbdav-1.0.3.zip").exists()
    assert not (
        tmp_path / "dist" / "plugin.video.nzbdav" / "release-addon.zip"
    ).exists()
    assert (
        tmp_path / "dist" / "plugin.video.nzbdav" / "resources" / "icon.png"
    ).read_bytes() == b"icon"


def test_parse_local_xml_rejects_doctype(tmp_path):
    module = _load_generate_repo_module()
    addon_xml = tmp_path / "addon.xml"
    addon_xml.write_text(
        '<!DOCTYPE addon [<!ENTITY secret "x">]>\n<addon id="x" version="1.0.0" />',
        encoding="utf-8",
    )

    with pytest.raises(module.ET.ParseError):
        module._parse_local_xml(str(addon_xml))
