# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from resources.lib.ptt.parse import extend_options


def test_extend_options_empty():
    options = {}
    result = extend_options(options)
    assert result == {
        "skipIfAlreadyFound": True,
        "skipFromTitle": False,
        "skipIfFirst": False,
        "remove": False,
    }
    # extend_options mutates the options dictionary and returns it
    assert result is options


def test_extend_options_default_arg():
    result1 = extend_options()
    result1["mutation"] = True
    result2 = extend_options()
    assert "mutation" not in result2, "Default argument mutation detected!"

    assert result2 == {
        "skipIfAlreadyFound": True,
        "skipFromTitle": False,
        "skipIfFirst": False,
        "remove": False,
    }


def test_extend_options_override_defaults():
    options = {
        "skipIfAlreadyFound": False,
        "skipFromTitle": True,
        "skipIfFirst": True,
        "remove": True,
    }
    result = extend_options(options)
    assert result == {
        "skipIfAlreadyFound": False,
        "skipFromTitle": True,
        "skipIfFirst": True,
        "remove": True,
    }
    # extend_options mutates the options dictionary and returns it
    assert result is options


def test_extend_options_partial_override():
    options = {"remove": True}
    result = extend_options(options)
    assert result == {
        "skipIfAlreadyFound": True,
        "skipFromTitle": False,
        "skipIfFirst": False,
        "remove": True,
    }


def test_extend_options_extra_keys():
    options = {"extra_key": "value"}
    result = extend_options(options)
    assert result == {
        "skipIfAlreadyFound": True,
        "skipFromTitle": False,
        "skipIfFirst": False,
        "remove": False,
        "extra_key": "value",
    }
