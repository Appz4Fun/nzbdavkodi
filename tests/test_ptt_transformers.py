# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from resources.lib.ptt.transformers import convert_months, transform_resolution


def test_transform_resolution_normalizes_common_release_tokens():
    assert transform_resolution("2160") == "2160p"
    assert transform_resolution("4k") == "2160p"
    assert transform_resolution("4K") == "2160p"
    assert transform_resolution("2160p") == "2160p"

    assert transform_resolution("1440") == "1440p"
    assert transform_resolution("2k") == "1440p"
    assert transform_resolution("2K") == "1440p"

    assert transform_resolution("1080") == "1080p"
    assert transform_resolution("1080i") == "1080p"
    assert transform_resolution("720") == "720p"
    assert transform_resolution("480") == "480p"
    assert transform_resolution("360") == "360p"
    assert transform_resolution("240") == "240p"


def test_transform_resolution_preserves_unknown_as_lowercase():
    assert transform_resolution("unknown") == "unknown"
    assert transform_resolution("") == ""
    assert transform_resolution("SD") == "sd"


def test_convert_months_normalizes_abbreviated_long_month_tokens():
    assert convert_months("20 Janu 2020") == "20 Jan 2020"
    assert convert_months("15 Febr 2021") == "15 Feb 2021"
    assert convert_months("31 Dece 2022") == "31 Dec 2022"


def test_convert_months_is_case_insensitive():
    assert convert_months("20 jAnU 2020") == "20 Jan 2020"
    assert convert_months("15 fEbR 2021") == "15 Feb 2021"


def test_convert_months_leaves_unmatched_text_unchanged():
    assert convert_months("01 January 2020") == "01 January 2020"
    assert convert_months("01 Jan 2020") == "01 Jan 2020"
    assert convert_months("random string") == "random string"
    assert convert_months("") == ""
