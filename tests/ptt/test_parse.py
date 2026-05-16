# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from resources.lib.ptt.parse import LANGUAGES_TRANSLATION_TABLE, translate_langs


def test_translate_langs_basic():
    """Test standard language code translations."""
    assert translate_langs(["en", "fr"]) == ["English", "French"]
    assert translate_langs(["de", "it"]) == ["German", "Italian"]
    assert translate_langs(["ru"]) == ["Russian"]


def test_translate_langs_empty():
    """Test empty list."""
    assert translate_langs([]) == []


def test_translate_langs_unknown():
    """Test unknown language codes are omitted."""
    assert translate_langs(["xx", "yy"]) == []
    assert translate_langs(["en", "xx", "fr"]) == ["English", "French"]


def test_translate_langs_mixed():
    """Test a mix of known, unknown, and duplicated codes."""
    assert translate_langs(["en", "en", "xx", "fr"]) == ["English", "English", "French"]


def test_translate_langs_all():
    """Test all keys in translation table."""
    keys = list(LANGUAGES_TRANSLATION_TABLE.keys())
    values = list(LANGUAGES_TRANSLATION_TABLE.values())
    assert translate_langs(keys) == values
