# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import re
from pathlib import Path


def _recipe_body(justfile_text, recipe_name):
    match = re.search(
        r"^{}(?:\s+[^:]*)?:$".format(re.escape(recipe_name)), justfile_text, re.M
    )
    assert match is not None
    start = match.start()
    lines = justfile_text[start:].splitlines()
    body = []
    for line in lines[1:]:
        if line and not line.startswith((" ", "\t")):
            break
        body.append(line)
    return "\n".join(body)


def test_make_dev_installs_dependencies_for_all_just_recipes():
    justfile_text = Path("justfile").read_text(encoding="utf-8")

    body = _recipe_body(justfile_text, "make-dev")

    # uv pre-fetches the pinned interpreters; the toolchain stays fully
    # uv-pinned with no raw pip escape hatch.
    assert "uv python install" in body
    assert "pip install" not in body
    assert "brew install" in body
    assert "brew list --formula --full-name" in body
    assert "ffmpeg" in body
    assert "x265" in body
    assert "brew reinstall" in body
    assert "ffmpeg_formula" in body
    assert "ffmpeg -version" in body


def test_functional_test_recipe_is_dev_only_and_not_in_default_test():
    justfile_text = Path("justfile").read_text(encoding="utf-8")

    test_body = _recipe_body(justfile_text, "test")
    functional_body = _recipe_body(justfile_text, "functional-test")
    top_imdb_body = _recipe_body(justfile_text, "functional-test-top-imdb")

    assert "not functional" in test_body
    assert "test_functional_fallback_playback.py" in functional_body
    assert "-m functional" in functional_body
    assert "test_functional_imdb_top50_random_sample_fallback_playback" in top_imdb_body
    assert "-m functional" in top_imdb_body


def test_justfile_has_extreme_functional_test_recipe():
    contents = Path(__file__).resolve().parents[1].joinpath("justfile").read_text()
    assert "extreme-functional-test:" in contents


def test_justfile_has_setup_extreme_functional_test_recipe():
    contents = Path(__file__).resolve().parents[1].joinpath("justfile").read_text()
    assert "setup-extreme-functional-test:" in contents


def test_setup_extreme_functional_test_shell_quotes_env_values():
    contents = Path(__file__).resolve().parents[1].joinpath("justfile").read_text()
    body = _recipe_body(contents, "setup-extreme-functional-test")

    assert "emit_env()" in body
    assert "printf '%s=%q\\n'" in body
    assert 'echo "HYDRA_API_KEY=$HYDRA_API_KEY"' not in body


def test_test_recipe_excludes_extreme_marker():
    contents = Path(__file__).resolve().parents[1].joinpath("justfile").read_text()
    test_block = re.search(r"^test:\n(?:    .+\n)+", contents, re.MULTILINE)
    assert test_block is not None
    assert "not extreme" in test_block.group(0)


def test_github_workflows_exclude_extreme_marker_from_default_pytest_runs():
    # CI and release run the default suite via `just test`, whose recipe excludes
    # the integration/functional/extreme markers and whose testpaths skip
    # tests-extensive/. Guards against a workflow inlining a raw `pytest tests/`
    # that would sweep the slow extensive suites back into a default run.
    root = Path(__file__).resolve().parents[1]
    for name in ("ci.yml", "release.yml"):
        contents = (root / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert "just test" in contents
        assert "pytest" not in contents


def test_pages_workflow_deploys_repository_metadata_on_main_push():
    root = Path(__file__).resolve().parents[1]
    contents = (root / ".github" / "workflows" / "pages.yml").read_text(
        encoding="utf-8"
    )

    assert "push:" in contents
    assert "branches: [main]" in contents
    assert "repo/repository.nzbdav/**" in contents
    assert "scripts/select_stable_release.py" in contents
    assert "repo/repository.nzbdav.releases" not in contents
    assert (
        'gh api --paginate --slurp "repos/$GITHUB_REPOSITORY/releases?per_page=100"'
        in contents
    )
    assert "selected_tag=" in contents
    assert "github.event.workflow_run.head_branch" in contents
    assert "WORKFLOW_HEAD_BRANCH:" in contents
    assert 'selected_tag="$WORKFLOW_HEAD_BRANCH"' in contents
    assert 'selected_tag="${{ github.event.workflow_run.head_branch' not in contents
    assert (
        'python3 scripts/select_stable_release.py releases.json --tag "$selected_tag"'
        in contents
    )
    assert "--clobber" in contents
    assert "rm -rf pages-dist" in contents
    assert "--output-dir pages-dist" in contents
    assert "--addon-zip release-addon.zip" in contents
    assert "--release-asset-url" not in contents
    assert "--smoke-check" in contents
    assert "--repository-addon-dir" not in contents
    assert "scripts/generate_repo.py" in contents


def test_pages_workflow_skips_prerelease_release_runs_without_deploying():
    root = Path(__file__).resolve().parents[1]
    contents = (root / ".github" / "workflows" / "pages.yml").read_text(
        encoding="utf-8"
    )
    skip_guard = "if: ${{ steps.stable_release.outputs.skip_deploy != 'true' }}"

    def assert_step_has_skip_guard(step_name):
        pattern = (r"- name: {}\n" r"(?:        [^\n]*\n)*?" r"        {}\n").format(
            re.escape(step_name), re.escape(skip_guard)
        )
        assert re.search(pattern, contents) is not None

    assert "skip_deploy=true" in contents
    assert "Skipping Pages deploy for non-stable release tag" in contents
    assert "skip_deploy=false" in contents
    assert_step_has_skip_guard("Download selected addon release")
    assert_step_has_skip_guard("Generate Pages repository")
    assert_step_has_skip_guard("Upload Pages artifact")
    assert_step_has_skip_guard("Deploy to GitHub Pages")


def test_release_workflow_does_not_generate_unpublished_repository_metadata():
    root = Path(__file__).resolve().parents[1]
    contents = (root / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "Create GitHub Release" in contents
    assert "Generate repository metadata" not in contents
    assert "scripts/generate_repo.py" not in contents


def test_extreme_functional_test_recipe_preserves_exported_env_overrides():
    contents = Path(__file__).resolve().parents[1].joinpath("justfile").read_text()
    body = _recipe_body(contents, "extreme-functional-test")

    assert "env_snapshot" in body
    assert 'source "$env_file"' in body
    assert 'source "$env_snapshot"' in body
