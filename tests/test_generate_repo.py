# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for repository metadata generation."""

import importlib.util
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


def test_parse_local_xml_rejects_doctype(tmp_path):
    module = _load_generate_repo_module()
    addon_xml = tmp_path / "addon.xml"
    addon_xml.write_text(
        '<!DOCTYPE addon [<!ENTITY secret "x">]>\n<addon id="x" version="1.0.0" />',
        encoding="utf-8",
    )

    with pytest.raises(module.ET.ParseError):
        module._parse_local_xml(str(addon_xml))
