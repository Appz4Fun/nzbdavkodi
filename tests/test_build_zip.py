# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for building the installable Kodi addon zip."""

import importlib.util
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_build_zip_module():
    script_path = REPO_ROOT / "scripts" / "build_zip.py"
    spec = importlib.util.spec_from_file_location("build_zip_script", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_zip_keeps_addon_id_as_archive_root(tmp_path, monkeypatch):
    module = _load_build_zip_module()
    monkeypatch.chdir(REPO_ROOT)

    zip_path = module.build_zip(output_dir=str(tmp_path))

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())

    assert "plugin.video.nzbdav/addon.xml" in names
    assert "plugin.video.nzbdav/addon.py" in names
    assert "repo/plugin.video.nzbdav/addon.xml" not in names
