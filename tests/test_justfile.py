# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import re
from pathlib import Path


def _recipe_body(justfile_text, recipe_name):
    start = justfile_text.index("{}:".format(recipe_name))
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

    assert "pip install" in body
    assert "--break-system-packages" in body
    assert "-r requirements-test.txt" in body
    assert '"ruff>=0.15"' in body
    assert '"black>=24"' in body
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
