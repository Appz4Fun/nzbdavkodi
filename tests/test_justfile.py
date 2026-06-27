# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


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

    # uv pre-fetches the pinned interpreters; pip installs are Chroma-only now
    assert "uv python install" in body
    # Every pip install must run through the pinned Chroma interpreter (the only
    # pip escape hatch); the main toolchain stays uv-pinned.
    pip_lines = [line for line in body.splitlines() if "pip install" in line]
    assert pip_lines
    assert all('"$chroma_py" -m pip install' in line for line in pip_lines)
    assert "--break-system-packages" in body
    assert "brew install" in body
    assert "brew list --formula --full-name" in body
    assert "ffmpeg" in body
    assert "x265" in body
    assert "brew reinstall" in body
    assert "ffmpeg_formula" in body
    assert "ffmpeg -version" in body


def test_make_dev_pip_flags_expansion_is_bash32_nounset_safe():
    justfile_text = Path("justfile").read_text(encoding="utf-8")

    body = _recipe_body(justfile_text, "make-dev")

    # The Chroma-specific pip flags still use the nounset-safe expansion form.
    assert 'pip install "${chroma_pip_flags[@]}"' not in body
    assert '${chroma_pip_flags+"${chroma_pip_flags[@]}"}' in body

    bash = Path("/bin/bash")
    if bash.exists():
        subprocess.run(
            [
                str(bash),
                "-uc",
                "chroma_pip_flags=(); "
                'args=(${chroma_pip_flags+"${chroma_pip_flags[@]}"}); '
                "[[ ${#args[@]} -eq 0 ]]",
            ],
            check=True,
        )


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


def test_chroma_dev_recipes_use_python314_and_dev_requirements():
    contents = Path(__file__).resolve().parents[1].joinpath("justfile").read_text()

    make_dev_body = _recipe_body(contents, "make-dev")
    install_body = _recipe_body(contents, "chroma-install")
    index_body = _recipe_body(contents, "chroma-index")
    search_body = _recipe_body(contents, "chroma-search")
    agent_check_body = _recipe_body(contents, "chroma-agent-check")

    assert "requirements-dev-chroma.txt" in make_dev_body
    assert "${CHROMA_PYTHON:-python3.14}" in make_dev_body
    assert "scripts/chroma_check_config.py --env-file .env --prompt" in make_dev_body
    assert "scripts/chroma_agent_check.py --env-file .env --soft" in make_dev_body
    assert "import chromadb" in make_dev_body
    assert "requirements-dev-chroma.txt" in install_body
    assert "--break-system-packages" in install_body
    assert '${pip_flags+"${pip_flags[@]}"}' in install_body
    assert "${CHROMA_PYTHON:-python3.14}" in install_body
    assert "scripts/chroma_index_repo.py" in index_body
    assert "scripts/chroma_search_repo.py" in search_body
    assert "scripts/chroma_agent_check.py" in agent_check_body
    assert "${CHROMA_PYTHON:-python3.14}" in index_body
    assert "${CHROMA_PYTHON:-python3.14}" in search_body
    assert "${CHROMA_PYTHON:-python3.14}" in agent_check_body
    assert "set positional-arguments" in contents
    assert '"$@"' in index_body
    assert '"$@"' in search_body
    assert '"$@"' in agent_check_body
    assert "{{args}}" not in index_body
    assert "{{args}}" not in search_body
    assert "{{args}}" not in agent_check_body


def test_chroma_search_recipe_does_not_glob_contains_literals(tmp_path):
    just = shutil.which("just")
    if just is None:
        pytest.skip("just executable is not installed")

    root = Path(__file__).resolve().parents[1]
    fake_python = tmp_path / "python"
    argv_file = tmp_path / "argv.txt"
    (tmp_path / "ab").write_text("glob match\n", encoding="utf-8")
    fake_python.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'printf \'<%s>\\n\' "$@" > "$ARGV_FILE"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = dict(os.environ)
    env["CHROMA_PYTHON"] = str(fake_python)
    env["ARGV_FILE"] = str(argv_file)

    subprocess.run(
        [
            just,
            "--justfile",
            str(root / "justfile"),
            "--working-directory",
            str(tmp_path),
            "chroma-search",
            "query",
            "--contains",
            "a[b]*",
        ],
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert argv_file.read_text(encoding="utf-8").splitlines() == [
        "<scripts/chroma_search_repo.py>",
        "<query>",
        "<--contains>",
        "<a[b]*>",
    ]


def test_chroma_search_recipe_delegates_optional_query_validation_to_python(tmp_path):
    just = shutil.which("just")
    if just is None:
        pytest.skip("just executable is not installed")

    root = Path(__file__).resolve().parents[1]
    fake_python = tmp_path / "python"
    argv_file = tmp_path / "argv.txt"
    fake_python.write_text(
        "\n".join(
            [
                "#!/usr/bin/env bash",
                'printf \'<%s>\\n\' "$@" > "$ARGV_FILE"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = dict(os.environ)
    env["CHROMA_PYTHON"] = str(fake_python)
    env["ARGV_FILE"] = str(argv_file)

    subprocess.run(
        [
            just,
            "--justfile",
            str(root / "justfile"),
            "--working-directory",
            str(tmp_path),
            "chroma-search",
        ],
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert argv_file.read_text(encoding="utf-8").splitlines() == [
        "<scripts/chroma_search_repo.py>",
    ]


def test_chroma_dev_docs_describe_shared_defaults_and_agent_check():
    docs = (
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("docs/chroma-dev-search.md")
        .read_text(encoding="utf-8")
    )

    assert "CHROMA_TENANT=eb3e5a60-028d-4f18-95fd-c9495fb8ddaa" in docs
    assert "CHROMA_COLLECTION=nzb" in docs
    assert "just chroma-agent-check" in docs
    assert "codex mcp list" in docs
    assert "Restart Codex" in docs


def test_chroma_env_example_uses_shared_collection_default():
    env_example = Path(".env.example").read_text(encoding="utf-8")
    env_lines = set(env_example.splitlines())

    assert "CHROMA_COLLECTION=nzb" in env_lines
    assert "CHROMA_COLLECTION=nzbdavkodi_code" not in env_lines
