# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the ranked result card texture generator."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_texture_script():
    script_path = REPO_ROOT / "scripts" / "generate_results_card_textures.py"
    spec = importlib.util.spec_from_file_location(
        "generate_results_card_textures", script_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_results_card_texture_generator_targets_committed_media_files():
    module = _load_texture_script()

    assert module.CARD_SIZE == (1770, 120)
    assert module.CARD_RADIUS == 24
    assert module.TEXTURE_SPECS == (
        ("results-card.png", module.CARD_FILL, module.CARD_BORDER),
        ("results-card-focus.png", module.CARD_FOCUS_FILL, module.CARD_FOCUS_BORDER),
    )


def test_results_card_texture_generator_default_output_dir():
    module = _load_texture_script()

    assert module.default_output_dir() == (
        REPO_ROOT
        / "repo"
        / "plugin.video.nzbdav"
        / "resources"
        / "skins"
        / "Default"
        / "media"
    )
