# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from resources.lib.ptt.transformers import convert_months, transform_resolution


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


def test_convert_months_basic():
    assert convert_months("20 Janu 2020") == "20 Jan 2020"
    assert convert_months("15 Febr 2021") == "15 Feb 2021"


def test_convert_months_case_insensitive():
    assert convert_months("20 jAnU 2020") == "20 Jan 2020"
    assert convert_months("15 fEbR 2021") == "15 Feb 2021"


def test_convert_months_all_months():
    months = [
        ("Janu", "Jan"),
        ("Febr", "Feb"),
        ("Marc", "Mar"),
        ("Apri", "Apr"),
        ("May", "May"),
        ("June", "Jun"),
        ("July", "Jul"),
        ("Augu", "Aug"),
        ("Sept", "Sep"),
        ("Octo", "Oct"),
        ("Nove", "Nov"),
        ("Dece", "Dec"),
    ]
    for long_month, short_month in months:
        assert convert_months("01 {} 2020".format(long_month)) == "01 {} 2020".format(
            short_month
        )


def test_convert_months_no_match():
    # Full month names and already-short names pass through unchanged.
    assert convert_months("01 January 2020") == "01 January 2020"
    assert convert_months("01 Jan 2020") == "01 Jan 2020"
    assert convert_months("random string") == "random string"
    assert convert_months("") == ""
