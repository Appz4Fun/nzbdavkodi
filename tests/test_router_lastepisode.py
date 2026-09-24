# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Session-scoped last-picked-episode memory (router_lastepisode.py)."""

from unittest.mock import patch

from resources.lib.router_lastepisode import (
    _show_identity_key,
    recall_next_episode,
    remember_last_episode,
)


class _FakeWindow:
    """Dict-backed stand-in for ``xbmcgui.Window(10000)`` property storage."""

    _store = {}

    def getProperty(self, key):
        return self._store.get(key, "")

    def setProperty(self, key, value):
        self._store[key] = value


def _patched_window():
    _FakeWindow._store = {}
    return patch(
        "resources.lib.router_lastepisode.xbmcgui.Window",
        return_value=_FakeWindow(),
    )


def test_show_identity_key_prefers_tmdb_id_then_tvdb_then_imdb():
    params = {"tmdb_id": "1", "tvdb": "2", "imdb": "tt3"}
    assert _show_identity_key(params) == "tmdb_id:1"
    assert _show_identity_key({"tvdb": "2", "imdb": "tt3"}) == "tvdb:2"
    assert _show_identity_key({"imdb": "tt3"}) == "imdb:tt3"


def test_show_identity_key_falls_back_to_casefolded_title():
    assert _show_identity_key({"title": "From"}) == "title:from"
    assert _show_identity_key({}) == ""


def test_remember_then_recall_returns_next_episode():
    with _patched_window():
        params = {"tmdb_id": "124364", "title": "From"}
        remember_last_episode(params, "3", "5")
        assert recall_next_episode(params) == ("3", "6")


def test_recall_returns_blank_for_a_different_show():
    with _patched_window():
        remember_last_episode({"tmdb_id": "124364"}, "3", "5")
        assert recall_next_episode({"tmdb_id": "999"}) == ("", "")


def test_recall_returns_blank_when_nothing_remembered():
    with _patched_window():
        assert recall_next_episode({"tmdb_id": "124364"}) == ("", "")


def test_remember_is_a_noop_for_non_numeric_season_or_episode():
    with _patched_window():
        remember_last_episode({"tmdb_id": "124364"}, "", "")
        assert recall_next_episode({"tmdb_id": "124364"}) == ("", "")


def test_remember_is_a_noop_without_a_show_identity():
    with _patched_window():
        remember_last_episode({}, "3", "5")
        assert recall_next_episode({"title": ""}) == ("", "")
