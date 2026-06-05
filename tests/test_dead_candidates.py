# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Unit tests for the per-session dead-candidate tracker."""

from resources.lib.dead_candidates import (
    DeadCandidates,
    is_provably_dead_submit_error,
)


def test_add_and_query_by_url():
    dead = DeadCandidates()
    assert not dead.has_url("http://x/a.nzb")
    dead.add(nzb_url="http://x/a.nzb")
    assert dead.has_url("http://x/a.nzb")
    assert not dead.has_url("http://x/b.nzb")


def test_add_and_query_by_nzo_id():
    dead = DeadCandidates()
    dead.add(nzo_id="SABnzbd_nzo_123")
    assert dead.has_nzo("SABnzbd_nzo_123")
    assert not dead.has_nzo("SABnzbd_nzo_999")


def test_add_ignores_empty_keys():
    dead = DeadCandidates()
    dead.add(nzb_url="", nzo_id=None)
    assert not dead.has_url("")
    assert not dead.has_nzo(None)


def test_http_error_status_is_provably_dead():
    assert is_provably_dead_submit_error({"status": 500, "message": "x"})
    assert is_provably_dead_submit_error({"status": "rejected", "message": "x"})


def test_timeout_status_is_not_provably_dead():
    assert not is_provably_dead_submit_error({"status": "timeout", "message": "x"})


def test_none_or_non_dict_is_not_provably_dead():
    assert not is_provably_dead_submit_error(None)
    assert not is_provably_dead_submit_error("oops")
