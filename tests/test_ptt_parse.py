# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from resources.lib.ptt.parse import extend_options

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
