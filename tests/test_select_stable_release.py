# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for stable GitHub release asset selection."""

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_select_stable_release_module():
    script_path = REPO_ROOT / "scripts" / "select_stable_release.py"
    spec = importlib.util.spec_from_file_location("select_stable_release", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _release(tag, prerelease=False, draft=False, assets=None, github_names=False):
    release = {
        "assets": (
            assets
            if assets is not None
            else [
                {
                    "name": "plugin.video.nzbdav-{}.zip".format(tag.lstrip("v")),
                    "browser_download_url": "https://example.test/{}.zip".format(tag),
                }
            ]
        )
    }
    if github_names:
        release["tag_name"] = tag
        release["prerelease"] = prerelease
        release["draft"] = draft
    else:
        release["tagName"] = tag
        release["isPrerelease"] = prerelease
        release["isDraft"] = draft
    return release


def test_selects_highest_stable_semver_release():
    module = _load_select_stable_release_module()
    releases = [
        _release("v1.9.9"),
        _release("v2.0.0"),
        _release("v1.10.0"),
    ]

    selected = module.select_stable_release(releases)

    assert selected == {
        "tagName": "v2.0.0",
        "assetName": "plugin.video.nzbdav-2.0.0.zip",
        "downloadUrl": "https://example.test/v2.0.0.zip",
    }


def test_rejects_releases_marked_prerelease_by_github():
    module = _load_select_stable_release_module()
    releases = [
        _release("v2.0.0", prerelease=True),
        _release("v1.5.0"),
    ]

    selected = module.select_stable_release(releases)

    assert selected["tagName"] == "v1.5.0"


def test_rejects_draft_releases():
    module = _load_select_stable_release_module()
    releases = [
        _release("v2.0.0", draft=True),
        _release("v1.5.0"),
    ]

    selected = module.select_stable_release(releases)

    assert selected["tagName"] == "v1.5.0"


@pytest.mark.parametrize(
    "tag",
    ["v2.0.0-alpha", "v2.0.0-beta.1", "v2.0.0-rc.1", "v2.0.0-dev"],
)
def test_rejects_semver_prerelease_suffixes(tag):
    module = _load_select_stable_release_module()
    releases = [_release(tag), _release("v1.0.0")]

    selected = module.select_stable_release(releases)

    assert selected["tagName"] == "v1.0.0"


def test_allows_build_metadata_without_treating_it_as_prerelease():
    module = _load_select_stable_release_module()
    releases = [_release("v1.2.3+build.7"), _release("v1.2.2")]

    selected = module.select_stable_release(releases)

    assert selected["tagName"] == "v1.2.3+build.7"


def test_tolerates_github_snake_case_release_fields():
    module = _load_select_stable_release_module()
    releases = [_release("v1.0.0", github_names=True)]

    selected = module.select_stable_release(releases)

    assert selected["tagName"] == "v1.0.0"


def test_rejects_asset_name_that_does_not_match_release_tag():
    module = _load_select_stable_release_module()
    assets = [
        {
            "name": "plugin.video.nzbdav-1.9.0.zip",
            "browser_download_url": "https://example.test/1.9.0.zip",
        }
    ]

    with pytest.raises(SystemExit) as excinfo:
        module.select_stable_release([_release("v2.0.0", assets=assets)])

    assert str(excinfo.value) == (
        "Stable release v2.0.0 asset must be named "
        "plugin.video.nzbdav-2.0.0.zip; found plugin.video.nzbdav-1.9.0.zip"
    )


def test_rejects_matching_asset_without_download_url():
    module = _load_select_stable_release_module()
    assets = [{"name": "plugin.video.nzbdav-1.0.0.zip"}]

    with pytest.raises(SystemExit) as excinfo:
        module.select_stable_release([_release("v1.0.0", assets=assets)])

    assert str(excinfo.value) == (
        "Stable release v1.0.0 asset plugin.video.nzbdav-1.0.0.zip must include "
        "a download URL"
    )


def test_raises_when_no_stable_release_exists():
    module = _load_select_stable_release_module()

    with pytest.raises(SystemExit) as excinfo:
        module.select_stable_release([_release("v1.0.0-beta.1")])

    assert str(excinfo.value) == "No stable plugin.video.nzbdav release found"


@pytest.mark.parametrize("assets", [[], [{"name": "other.zip"}]])
def test_raises_when_selected_release_has_no_matching_asset(assets):
    module = _load_select_stable_release_module()

    with pytest.raises(SystemExit) as excinfo:
        module.select_stable_release([_release("v1.0.0", assets=assets)])

    assert str(excinfo.value) == (
        "Stable release v1.0.0 must have exactly one "
        "plugin.video.nzbdav-*.zip asset; found 0"
    )


def test_raises_when_selected_release_has_multiple_matching_assets():
    module = _load_select_stable_release_module()
    assets = [
        {
            "name": "plugin.video.nzbdav-1.0.0.zip",
            "browser_download_url": "https://example.test/1.zip",
        },
        {
            "name": "plugin.video.nzbdav-extra.zip",
            "browser_download_url": "https://example.test/2.zip",
        },
    ]

    with pytest.raises(SystemExit) as excinfo:
        module.select_stable_release([_release("v1.0.0", assets=assets)])

    assert str(excinfo.value) == (
        "Stable release v1.0.0 must have exactly one "
        "plugin.video.nzbdav-*.zip asset; found 2"
    )


def test_main_prints_github_output_lines(tmp_path, capsys):
    module = _load_select_stable_release_module()
    releases_path = tmp_path / "releases.json"
    releases_path.write_text(json.dumps([_release("v1.0.0")]), encoding="utf-8")

    exit_code = module.main([str(releases_path)])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "tag=v1.0.0\n"
        "asset_name=plugin.video.nzbdav-1.0.0.zip\n"
        "download_url=https://example.test/v1.0.0.zip\n"
    )
