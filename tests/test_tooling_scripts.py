# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for local developer tooling scripts."""

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_HARNESS = REPO_ROOT / "orchestrator" / "tests" / "harness" / "live"


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


def test_live_harness_runner_starts_hydra2_dependency():
    compose = (LIVE_HARNESS / "docker-compose.yml").read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^  test-runner:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:|\Z)", compose
    )

    assert match is not None
    assert re.search(
        r"(?m)^    depends_on:\n(?P<body>(?:^      .+\n?)+)", match.group("body")
    )
    assert re.search(
        r"(?m)^      hydra2:\n        condition: service_started", match.group("body")
    )


def test_live_extreme_harness_is_wired_into_compose_and_justfile():
    compose = (LIVE_HARNESS / "docker-compose.yml").read_text(encoding="utf-8")
    justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")

    assert "LIVE_EXTREME: ${LIVE_EXTREME:-0}" in compose
    assert "LIVE_EXTREME_SAMPLE_SIZE: ${LIVE_EXTREME_SAMPLE_SIZE:-3}" in compose
    assert (
        "LIVE_EXTREME_MIN_VALIDATED_PEERS: ${LIVE_EXTREME_MIN_VALIDATED_PEERS:-1}"
        in compose
    )
    assert "LIVE_EXTREME_IMDB_ID: ${LIVE_EXTREME_IMDB_ID:-}" in compose
    assert "LIVE_REPORT_ROOT: /reports" in compose
    assert "- ../../../../docs/reports:/reports" in compose
    assert "harness-live-extreme:" in justfile
    assert "LIVE_EXTREME=1 docker compose" in justfile
    assert "pytest /tests/test_live_extreme.py" in justfile


def test_live_test_runner_image_includes_extreme_modules():
    dockerfile = (LIVE_HARNESS / "test_runner" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "live_common.py" in dockerfile
    assert "test_live_extreme.py" in dockerfile


def test_live_extreme_report_records_provider_outcomes():
    live_extreme = (LIVE_HARNESS / "test_runner" / "test_live_extreme.py").read_text(
        encoding="utf-8"
    )

    assert '"provider_outcomes": search.get("providers", [])' in live_extreme
    assert '"provider_errors": provider_errors' in live_extreme
    assert "pytest.skip" in live_extreme


def test_live_extreme_uses_separate_imdb_override_from_fast_live_test():
    live_common = (LIVE_HARNESS / "test_runner" / "live_common.py").read_text(
        encoding="utf-8"
    )

    assert 'os.environ.get("LIVE_EXTREME_IMDB_ID", "").strip()' in live_common
    assert 'os.environ.get("LIVE_IMDB_ID", "").strip()' not in live_common
