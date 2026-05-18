# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import pytest
from resources.lib.ptt.parse import (
    LANGUAGES_TRANSLATION_TABLE,
    clean_title,
    extend_options,
    translate_langs,
)

DEFAULT_OPTIONS = {
    "skipIfAlreadyFound": True,
    "skipFromTitle": False,
    "skipIfFirst": False,
    "remove": False,
}


def expected_options(**overrides):
    expected = DEFAULT_OPTIONS.copy()
    expected.update(overrides)
    return expected


def test_extend_options_mutates_provided_empty_dict():
    options = {}
    result = extend_options(options)
    assert result == expected_options()
    # Preserve existing behavior for callers that pass a dict: fill it in place.
    assert result is options


def test_extend_options_default_arg():
    result1 = extend_options()
    result1["mutation"] = True
    result2 = extend_options()
    assert "mutation" not in result2, "Default argument mutation detected!"

    assert result2 == expected_options()


def test_extend_options_preserves_full_overrides_in_place():
    options = {
        "skipIfAlreadyFound": False,
        "skipFromTitle": True,
        "skipIfFirst": True,
        "remove": True,
    }
    result = extend_options(options)
    assert result == expected_options(
        skipIfAlreadyFound=False,
        skipFromTitle=True,
        skipIfFirst=True,
        remove=True,
    )
    # Preserve existing behavior for callers that pass a dict: fill it in place.
    assert result is options


def test_extend_options_partial_override():
    options = {"remove": True}
    result = extend_options(options)
    assert result == expected_options(remove=True)


def test_extend_options_extra_keys():
    options = {"extra_key": "value"}
    result = extend_options(options)
    assert result == expected_options(extra_key="value")


def test_translate_langs_translates_known_codes_and_skips_unknown():
    assert translate_langs(["en", "xx", "fr", "de"]) == [
        "English",
        "French",
        "German",
    ]


def test_translate_langs_all_known_table_entries():
    codes = list(LANGUAGES_TRANSLATION_TABLE)
    assert translate_langs(codes) == [
        LANGUAGES_TRANSLATION_TABLE[code] for code in codes
    ]


@pytest.mark.parametrize(
    ("raw_title", "expected_title"),
    [
        ("Movie_Title", "Movie Title"),
        ("The Matrix [movie]", "The Matrix"),
        ("The Matrix (Movie)", "The Matrix"),
        ("-Movie Title-", "Movie Title"),
        ("[Movie Title]", "Movie Title"),
        ("...Movie Title...", "Movie Title"),
        ("Movie Title []", "Movie Title"),
        ("Movie Title ()", "Movie Title"),
        ("Movie Title (-)", "Movie Title"),
        ("Movie -- Title", "Movie Title"),
        ("Movie Title (2020", "Movie Title 2020"),
        ("The.Matrix.1999", "The Matrix 1999"),
        ("The.Matrix 1999", "The.Matrix 1999"),
        ("Movie Title -", "Movie Title"),
        ("Movie Soundtrack mp3", "Movie Soundtrack"),
    ],
)
def test_clean_title_removes_release_noise(raw_title, expected_title):
    assert clean_title(raw_title) == expected_title
