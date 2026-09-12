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


@pytest.mark.parametrize(
    "raw_title, expected_title",
    [
        # Underscores become spaces
        ("Movie_Title", "Movie Title"),
        ("A_Very_Long_Movie_Title_2023", "A Very Long Movie Title 2023"),
        # MOVIE_REGEX removals ([movie], (movie)), case insensitive
        ("The Matrix [movie]", "The Matrix"),
        ("The Matrix (Movie)", "The Matrix"),
        # NOT_ALLOWED_SYMBOLS_AT_START_AND_END removal
        ("-Movie Title-", "Movie Title"),
        ("[Movie Title]", "Movie Title"),
        ("...Movie Title...", "Movie Title"),
        # STAR_REGEX_1 strips a leading ★/【】 tag block
        ("★ Tag ★ The Dark Knight", "The Dark Knight"),
        ("【 Tag 】 The Dark Knight", "The Dark Knight"),
        ("★Tag★ The Dark Knight", "The Dark Knight"),
        ("【Tag】 The Dark Knight", "The Dark Knight"),
        # STAR_REGEX_2 strips a trailing tag block
        ("The Dark Knight ★ Tag ★", "The Dark Knight"),
        ("The Dark Knight 【 Tag 】", "The Dark Knight"),
        # EMPTY_BRACKETS_REGEX removals
        ("Movie Title []", "Movie Title"),
        ("Movie Title ()", "Movie Title"),
        ("Movie Title {}", "Movie Title"),
        ("Movie Title [  ]", "Movie Title"),
        # PARANTHESES_WITHOUT_CONTENT removals
        ("Movie Title (-)", "Movie Title"),
        ("Movie Title [.]", "Movie Title"),
        # SPECIAL_CHAR_SPACING removals
        ("Movie -- Title", "Movie Title"),
        ("Movie  ++  Title", "Movie Title"),
        # Unbalanced single bracket removal
        ("Movie Title (2020", "Movie Title 2020"),
        ("Movie Title [2020", "Movie Title 2020"),
        # Dots become spaces only when the title has no spaces already
        ("The.Matrix.1999", "The Matrix 1999"),
        ("The.Matrix 1999", "The.Matrix 1999"),
        # REDUNDANT_SYMBOLS_AT_END removals
        ("Movie Title -", "Movie Title"),
        ("Movie Title :", "Movie Title"),
        ("Movie Title .", "Movie Title"),
        ("Movie Title /", "Movie Title"),
        ("Movie Title \\", "Movie Title"),
        # Whitespace collapse and trim
        ("Movie    Title", "Movie Title"),
        ("   Movie Title   ", "Movie Title"),
        # MP3 suffix removal
        ("Movie Soundtrack mp3", "Movie Soundtrack"),
        # Combination of cleanups
        ("★ Tag ★ The.Matrix.1999[movie]( ) -", "The Matrix 1999"),
    ],
)
def test_clean_title(raw_title, expected_title):
    assert clean_title(raw_title) == expected_title


def test_translate_langs_basic():
    assert translate_langs(["en", "fr"]) == ["English", "French"]
    assert translate_langs(["de", "it"]) == ["German", "Italian"]
    assert translate_langs(["ru"]) == ["Russian"]


def test_translate_langs_empty():
    assert translate_langs([]) == []


def test_translate_langs_unknown_codes_omitted():
    assert translate_langs(["xx", "yy"]) == []
    assert translate_langs(["en", "xx", "fr"]) == ["English", "French"]


def test_translate_langs_preserves_duplicates():
    assert translate_langs(["en", "en", "xx", "fr"]) == ["English", "English", "French"]


def test_translate_langs_covers_full_table():
    keys = list(LANGUAGES_TRANSLATION_TABLE.keys())
    values = list(LANGUAGES_TRANSLATION_TABLE.values())
    assert translate_langs(keys) == values
