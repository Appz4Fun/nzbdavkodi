# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import pytest

from ptt.transformers import transform_resolution


def test_transform_resolution():
    assert transform_resolution("2160") == "2160p"
    assert transform_resolution("4k") == "2160p"
    assert transform_resolution("4K") == "2160p"
    assert transform_resolution("2160p") == "2160p"

    assert transform_resolution("1440") == "1440p"
    assert transform_resolution("2k") == "1440p"
    assert transform_resolution("2K") == "1440p"

    assert transform_resolution("1080") == "1080p"
    assert transform_resolution("1080p") == "1080p"
    assert transform_resolution("1080i") == "1080p"
    assert transform_resolution("1080P") == "1080p"

    assert transform_resolution("720") == "720p"
    assert transform_resolution("720p") == "720p"

    assert transform_resolution("480") == "480p"
    assert transform_resolution("480p") == "480p"

    assert transform_resolution("360") == "360p"
    assert transform_resolution("360p") == "360p"

    assert transform_resolution("240") == "240p"
    assert transform_resolution("240p") == "240p"

    assert transform_resolution("unknown") == "unknown"
    assert transform_resolution("") == ""
    assert transform_resolution("SD") == "sd"
