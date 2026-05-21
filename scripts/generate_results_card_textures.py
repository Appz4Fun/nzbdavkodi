#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Generate ranked results card PNG textures for Kodi skins.

This script is a development helper. It imports Pillow only when generation
runs so the runtime addon stays pure Python and dependency-free.
"""

from pathlib import Path

CARD_SIZE = (1770, 120)
CARD_RADIUS = 24
CARD_FILL = (17, 20, 26, 255)
CARD_BORDER = (47, 85, 121, 255)
CARD_FOCUS_FILL = (28, 49, 77, 255)
CARD_FOCUS_BORDER = (88, 150, 207, 255)
TEXTURE_SPECS = (
    ("results-card.png", CARD_FILL, CARD_BORDER),
    ("results-card-focus.png", CARD_FOCUS_FILL, CARD_FOCUS_BORDER),
)


def repo_root():
    return Path(__file__).resolve().parents[1]


def default_output_dir():
    return (
        repo_root()
        / "repo"
        / "plugin.video.nzbdav"
        / "resources"
        / "skins"
        / "Default"
        / "media"
    )


def _draw_texture(path, fill, border):
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required to generate textures. Install with: "
            "python3 -m pip install pillow"
        ) from exc

    image = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    x1 = CARD_SIZE[0] - 1
    y1 = CARD_SIZE[1] - 1
    draw.rounded_rectangle(
        (0, 0, x1, y1),
        radius=CARD_RADIUS,
        fill=fill,
        outline=border,
        width=3,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def generate(output_dir=None):
    target_dir = Path(output_dir) if output_dir is not None else default_output_dir()
    written = []
    for filename, fill, border in TEXTURE_SPECS:
        path = target_dir / filename
        _draw_texture(path, fill, border)
        written.append(path)
    return written


def main():
    for path in generate():
        print(path)


if __name__ == "__main__":
    main()
