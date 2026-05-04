# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

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
