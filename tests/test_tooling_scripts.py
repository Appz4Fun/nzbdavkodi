# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for local developer tooling scripts."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name):
    script_path = REPO_ROOT / "scripts" / "{}.py".format(name)
    spec = importlib.util.spec_from_file_location(name, script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_python_runtime_import_check_collects_shipped_modules():
    module = _load_script("python_runtime_import_check")

    modules = module.collect_import_names(REPO_ROOT / "plugin.video.nzbdav")

    assert "addon" in modules
    assert "service" in modules
    assert "resources.lib.resolver" in modules
    assert "resources.lib.stream_proxy" in modules
    assert "resources.lib.ptt.parse" in modules


def test_repo_smoke_builds_pages_parity_artifacts(tmp_path, monkeypatch):
    module = _load_script("repo_smoke")
    monkeypatch.chdir(REPO_ROOT)

    result = module.run_smoke(tmp_path)

    assert result.addons_xml.exists()
    assert result.addons_md5.exists()
    assert result.root_index.exists()
    assert result.addon_zip.exists()
    assert result.repo_zip.exists()
    assert result.addons_md5.read_text(encoding="utf-8").isalnum()
    assert len(result.addons_md5.read_text(encoding="utf-8")) == 32


def test_default_ci_pytest_expressions_exclude_extreme_tests():
    expected = "not integration and not functional and not extreme"
    paths = [
        REPO_ROOT / "justfile",
        REPO_ROOT / ".github" / "workflows" / "ci.yml",
        REPO_ROOT / ".github" / "workflows" / "release.yml",
    ]

    for path in paths:
        assert expected in path.read_text(encoding="utf-8"), str(path)
