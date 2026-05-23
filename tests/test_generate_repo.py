# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for repository metadata generation."""

import gzip
import hashlib
import importlib.util
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ADDON_DIR = REPO_ROOT / "repo" / "plugin.video.nzbdav"
REPO_ADDON_DIR = REPO_ROOT / "repo" / "repository.nzbdav"


def _parse_generated_addons_xml(output_dir):
    with gzip.open(output_dir / "addons.xml.gz", "rb") as fh:
        return ET.ElementTree(ET.fromstring(fh.read()))


def _rewrite_generated_addons_xml(output_dir, tree, module):
    plain_path = output_dir / "addons.xml"
    gzip_path = output_dir / "addons.xml.gz"
    sha256_path = output_dir / "addons.xml.gz.sha256"
    tree.write(plain_path, encoding="utf-8", xml_declaration=True)
    with gzip.open(gzip_path, "wb") as fh:
        fh.write(plain_path.read_bytes())
    sha256 = module.hashlib.sha256(gzip_path.read_bytes()).hexdigest()
    sha256_path.write_text("{}  addons.xml.gz".format(sha256), encoding="utf-8")


def _load_generate_repo_module():
    script_path = REPO_ROOT / "scripts" / "generate_repo.py"
    spec = importlib.util.spec_from_file_location("generate_repo_script", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_generate_repo_writes_minimal_pages_root_files(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))

    index_path = output_dir / "index.html"
    assert index_path.exists()
    contents = index_path.read_text(encoding="utf-8")
    assert "repository.nzbdav-" in contents
    assert ".zip" in contents
    assert "plugin.video.nzbdav-" not in contents
    assert (output_dir / ".nojekyll").exists()
    assert (output_dir / "addons.xml.gz").exists()
    assert (output_dir / "addons.xml.gz.sha256").exists()
    assert not (output_dir / "addons.xml.md5").exists()
    assert not list(output_dir.glob("plugin.video.nzbdav-*.zip"))


def test_generate_repo_pages_root_index_lists_repository_metadata_files(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))

    contents = (output_dir / "index.html").read_text(encoding="utf-8")
    expected_links = [
        "addons.xml",
        "addons.xml.gz",
        "addons.xml.gz.sha256",
        "repository.nzbdav-1.1.1.zip",
        "repository.nzbdav-1.1.1.zip.sha256",
    ]
    for link in expected_links:
        assert '<a href="{0}">{0}</a>'.format(link) in contents


def test_generate_repo_html_indexes_use_standards_doctype(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    release_zip = tmp_path / "plugin.video.nzbdav-1.2.1.zip"
    release_addon_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="plugin.video.nzbdav" name="NZB-DAV" version="1.2.1" />
"""
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.video.nzbdav/addon.xml", release_addon_xml)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir), addon_zip=str(release_zip))

    index_paths = [
        output_dir / "index.html",
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

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))

    tree = _parse_generated_addons_xml(output_dir)
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    assert addon is not None
    metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
    assert metadata is not None
    assert metadata.find("news") is None


def test_generate_repo_includes_pages_repository_urls(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))

    tree = _parse_generated_addons_xml(output_dir)
    repo = tree.find("./addon[@id='repository.nzbdav']")
    assert repo is not None
    repo_dir = repo.find("./extension[@point='xbmc.addon.repository']/dir")
    repo_base = "https://appz4fun.github.io/nzbdavkodi"
    assert repo_dir is not None
    info = repo_dir.find("info")
    checksum = repo_dir.find("checksum")
    assert info is not None
    assert info.text == "{}/addons.xml.gz".format(repo_base)
    assert "compressed" not in info.attrib
    assert checksum is not None
    assert checksum.text == "{}/addons.xml.gz.sha256".format(repo_base)
    assert checksum.get("verify") == "sha256"
    datadir = repo_dir.find("datadir")
    artdir = repo_dir.find("artdir")
    assert datadir is not None
    assert datadir.text == repo_base
    assert "zip" not in datadir.attrib
    assert artdir is not None
    assert artdir.text == repo_base
    assert repo_dir.findtext("hashes") == "sha256"


def test_generate_repo_fails_when_repository_addon_dir_is_missing(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    missing_repo_dir = tmp_path / "missing-repository-addon"

    with pytest.raises(SystemExit) as excinfo:
        module.generate_repo(
            output_dir=str(tmp_path / "repo-output"),
            repository_addon_dir=str(missing_repo_dir),
        )

    assert str(excinfo.value) == (
        "generate_repo: repository addon directory not found: {!r}".format(
            str(missing_repo_dir)
        )
    )
    assert not (tmp_path / "repo-output" / "addons.xml.gz").exists()


def test_generate_repo_writes_sha256_payload_for_compressed_metadata(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))

    checksum_payload = (output_dir / "addons.xml.gz.sha256").read_text(encoding="ascii")
    expected = hashlib.sha256((output_dir / "addons.xml.gz").read_bytes()).hexdigest()
    assert checksum_payload == "{}  addons.xml.gz".format(expected)


def test_generate_repo_writes_sha256_payloads_for_repository_zip_files(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))

    zip_paths = [
        output_dir / "repository.nzbdav" / "repository.nzbdav-1.1.1.zip",
        output_dir / "repository.nzbdav-1.1.1.zip",
    ]
    for zip_path in zip_paths:
        checksum_path = zip_path.with_name(zip_path.name + ".sha256")
        expected = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        assert checksum_path.read_text(encoding="ascii") == "{}  {}".format(
            expected, zip_path.name
        )


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

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir), addon_zip=str(release_zip))

    tree = _parse_generated_addons_xml(output_dir)
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    assert addon is not None
    assert addon.attrib["version"] == "1.0.3"
    metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
    assert metadata is not None
    assert metadata.find("news") is None
    assert metadata.findtext("path") == (
        "plugin.video.nzbdav/plugin.video.nzbdav-1.0.3.zip"
    )
    assert not list(output_dir.glob("plugin.video.nzbdav-*.zip"))
    assert (
        output_dir / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.0.3.zip"
    ).read_bytes() == release_zip.read_bytes()
    checksum_path = (
        output_dir / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.0.3.zip.sha256"
    )
    expected = hashlib.sha256(release_zip.read_bytes()).hexdigest()
    assert checksum_path.read_text(encoding="ascii") == "{}  {}".format(
        expected, "plugin.video.nzbdav-1.0.3.zip"
    )


def test_generate_repo_uses_kodi_relative_addon_path_with_explicit_release_asset_url(
    tmp_path, monkeypatch, capsys
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    release_zip = tmp_path / "plugin.video.nzbdav-1.0.3.zip"
    release_addon_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="plugin.video.nzbdav" name="NZB-DAV" version="1.0.3" />
"""
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.video.nzbdav/addon.xml", release_addon_xml)

    output_dir = tmp_path / "pages"
    release_asset_url = (
        "https://github.com/Appz4Fun/nzbdavkodi/releases/download/"
        "1.0.3/plugin.video.nzbdav-1.0.3.zip"
    )
    module.generate_repo(
        output_dir=str(output_dir),
        addon_zip=str(release_zip),
        release_asset_url=release_asset_url,
    )
    captured = capsys.readouterr()

    tree = _parse_generated_addons_xml(output_dir)
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    assert addon is not None
    metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
    assert metadata is not None
    assert metadata.findtext("path") == (
        "plugin.video.nzbdav/plugin.video.nzbdav-1.0.3.zip"
    )
    assert (
        output_dir / "plugin.video.nzbdav" / "plugin.video.nzbdav-1.0.3.zip"
    ).read_bytes() == release_zip.read_bytes()
    assert "release_asset_url is deprecated and ignored" in captured.err


def test_generate_repo_writes_release_path_to_all_metadata_extensions(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    release_zip = tmp_path / "plugin.video.nzbdav-1.0.4.zip"
    release_addon_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="plugin.video.nzbdav" name="NZB-DAV" version="1.0.4">
    <extension point="xbmc.addon.metadata">
        <summary lang="en">XBMC metadata</summary>
    </extension>
    <extension point="kodi.addon.metadata">
        <summary lang="en">Kodi metadata</summary>
    </extension>
</addon>
"""
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.video.nzbdav/addon.xml", release_addon_xml)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir), addon_zip=str(release_zip))

    tree = _parse_generated_addons_xml(output_dir)
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    assert addon is not None
    expected_path = "plugin.video.nzbdav/plugin.video.nzbdav-1.0.4.zip"

    for point in ("xbmc.addon.metadata", "kodi.addon.metadata"):
        metadata = addon.find("./extension[@point='{}']".format(point))
        assert metadata is not None
        assert metadata.findtext("path") == expected_path


def test_generate_repo_smoke_check_passes_for_generated_pages(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    release_zip = tmp_path / "plugin.video.nzbdav-1.2.1.zip"
    release_addon_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="plugin.video.nzbdav" name="NZB-DAV" version="1.2.1" />
"""
    with zipfile.ZipFile(release_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("plugin.video.nzbdav/addon.xml", release_addon_xml)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir), addon_zip=str(release_zip))

    module.smoke_check_pages(str(output_dir))


def test_generate_repo_smoke_check_rejects_copied_addon_zip(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))
    (output_dir / "plugin.video.nzbdav-9.9.9.zip").write_bytes(b"bad")

    with pytest.raises(SystemExit) as excinfo:
        module.smoke_check_pages(str(output_dir))

    assert str(excinfo.value) == (
        "generate_repo: Pages root must not contain plugin.video.nzbdav zip files"
    )


def test_generate_repo_smoke_check_rejects_empty_sha256_checksum(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))
    (output_dir / "addons.xml.gz.sha256").write_text("", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        module.smoke_check_pages(str(output_dir))

    assert str(excinfo.value) == (
        "generate_repo: addons.xml.gz.sha256 does not match addons.xml.gz"
    )


def test_generate_repo_smoke_check_rejects_index_missing_root_checksum_link(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))
    index_path = output_dir / "index.html"
    index_path.write_text(
        index_path.read_text(encoding="utf-8").replace(
            '<a href="addons.xml.gz.sha256">addons.xml.gz.sha256</a><br>\n', ""
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as excinfo:
        module.smoke_check_pages(str(output_dir))

    assert str(excinfo.value) == (
        "generate_repo: index.html must link addons.xml.gz.sha256"
    )


def test_generate_repo_smoke_check_rejects_missing_addon_zip_sha256(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))
    (output_dir / "repository.nzbdav" / "repository.nzbdav-1.1.1.zip.sha256").unlink(
        missing_ok=True
    )

    with pytest.raises(SystemExit) as excinfo:
        module.smoke_check_pages(str(output_dir))

    assert str(excinfo.value) == (
        "generate_repo: missing sha256 checksum for repository.nzbdav-1.1.1.zip"
    )


def test_generate_repo_smoke_check_rejects_absolute_addon_metadata_path(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))
    tree = _parse_generated_addons_xml(output_dir)
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
    metadata.find("path").text = (
        "https://github.com/Appz4Fun/nzbdavkodi/releases/download/"
        "v9.9.9/plugin.video.nzbdav-9.9.9.zip"
    )
    _rewrite_generated_addons_xml(output_dir, tree, module)

    with pytest.raises(SystemExit) as excinfo:
        module.smoke_check_pages(str(output_dir))

    assert str(excinfo.value) == (
        "generate_repo: plugin.video.nzbdav path must be relative to repository datadir"
    )


@pytest.mark.parametrize(
    "metadata_path",
    [
        "/plugin.video.nzbdav/plugin.video.nzbdav-1.0.3.zip",
        "../plugin.video.nzbdav-1.0.3.zip",
        "plugin.video.nzbdav/../plugin.video.nzbdav-1.0.3.zip",
    ],
)
def test_generate_repo_smoke_check_rejects_non_repository_relative_addon_metadata_path(
    tmp_path, monkeypatch, metadata_path
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))
    tree = _parse_generated_addons_xml(output_dir)
    addon = tree.find("./addon[@id='plugin.video.nzbdav']")
    metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
    metadata.find("path").text = metadata_path
    _rewrite_generated_addons_xml(output_dir, tree, module)

    with pytest.raises(SystemExit) as excinfo:
        module.smoke_check_pages(str(output_dir))

    assert str(excinfo.value) == (
        "generate_repo: plugin.video.nzbdav path must be relative to repository datadir"
    )


def test_generate_repo_smoke_check_rejects_stale_repository_zip(tmp_path, monkeypatch):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)

    output_dir = tmp_path / "pages"
    module.generate_repo(output_dir=str(output_dir))
    stale_zip = output_dir / "repository.nzbdav" / "repository.nzbdav-0.0.1.zip"
    stale_zip.write_bytes(b"bad")

    with pytest.raises(SystemExit) as excinfo:
        module.smoke_check_pages(str(output_dir))

    assert str(excinfo.value) == (
        "generate_repo: repository.nzbdav directory must contain one matching "
        "repository zip"
    )


def test_generate_repo_smoke_check_supports_non_default_repository_id(
    tmp_path, monkeypatch
):
    module = _load_generate_repo_module()
    monkeypatch.chdir(REPO_ROOT)
    repository_addon_dir = tmp_path / "repo" / "repository.custom"
    repository_addon_dir.mkdir(parents=True)
    (repository_addon_dir / "addon.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<addon id="repository.custom" name="Custom Repository" version="1.2.3">
    <extension point="xbmc.addon.repository" name="Custom Repository">
        <dir>
            <info>https://example.test/addons.xml.gz</info>
            <checksum verify="sha256">https://example.test/addons.xml.gz.sha256</checksum>
            <datadir>https://example.test</datadir>
            <artdir>https://example.test</artdir>
            <hashes>sha256</hashes>
        </dir>
    </extension>
</addon>
""",
        encoding="utf-8",
    )

    output_dir = tmp_path / "pages"
    module.generate_repo(
        output_dir=str(output_dir),
        repository_addon_dir=str(repository_addon_dir),
    )

    module.smoke_check_pages(
        str(output_dir), repository_addon_dir=str(repository_addon_dir)
    )


def test_parse_local_xml_rejects_doctype(tmp_path):
    module = _load_generate_repo_module()
    addon_xml = tmp_path / "addon.xml"
    addon_xml.write_text(
        '<!DOCTYPE addon [<!ENTITY secret "x">]>\n<addon id="x" version="1.0.0" />',
        encoding="utf-8",
    )

    with pytest.raises(module.ET.ParseError):
        module._parse_local_xml(str(addon_xml))
