# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import pytest
from resources.lib.ptt.parse import clean_title


@pytest.mark.parametrize(
    "raw_title, expected_title",
    [
        # Replace underscores with spaces
        ("Movie_Title", "Movie Title"),
        ("A_Very_Long_Movie_Title_2023", "A Very Long Movie Title 2023"),
        # MOVIE_REGEX removals ([movie], (movie)) case insensitive
        ("The Matrix [movie]", "The Matrix"),
        ("The Matrix (Movie)", "The Matrix"),
        # NOT_ALLOWED_SYMBOLS_AT_START_AND_END removal
        ("-Movie Title-", "Movie Title"),
        ("[Movie Title]", "Movie Title"),
        ("...Movie Title...", "Movie Title"),
        # STAR_REGEX removals. The regex handles removing stars at the start
        # or end. It leaves one character if there is space.
        # e.g. '★ The Dark Knight ★' -> 'The Dark Knight ★'
        # Looking at STAR_REGEX_1 =
        # re.compile(r"^[\[【★].*[\]】★][ .]?(.+)")
        # It expects the title after the star block.
        # e.g. "★ Some tag ★ The Title" -> "The Title"
        ("★ Tag ★ The Dark Knight", "The Dark Knight"),
        ("【 Tag 】 The Dark Knight", "The Dark Knight"),
        ("★Tag★ The Dark Knight", "The Dark Knight"),
        ("【Tag】 The Dark Knight", "The Dark Knight"),
        # STAR_REGEX_2 handles tags at the end
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
        # Single bracket removals (if only 1 open/close)
        ("Movie Title (2020", "Movie Title 2020"),
        ("Movie Title [2020", "Movie Title 2020"),
        # Dots replaced with space if no space exists
        ("The.Matrix.1999", "The Matrix 1999"),
        (
            "The.Matrix 1999",
            "The.Matrix 1999",
        ),  # Has space, shouldn't replace dots
        # REDUNDANT_SYMBOLS_AT_END removals
        ("Movie Title -", "Movie Title"),
        ("Movie Title :", "Movie Title"),
        ("Movie Title .", "Movie Title"),
        ("Movie Title /", "Movie Title"),
        ("Movie Title \\", "Movie Title"),
        # Extra spacing removals
        ("Movie    Title", "Movie Title"),
        ("   Movie Title   ", "Movie Title"),
        # MP3 removal
        ("Movie Soundtrack mp3", "Movie Soundtrack"),
        # Combination of cleanups
        # The space between title and tags gets collapsed
        ("★ Tag ★ The.Matrix.1999[movie]( ) -", "The Matrix 1999"),
    ],
)
def test_clean_title(raw_title, expected_title):
    assert clean_title(raw_title) == expected_title
