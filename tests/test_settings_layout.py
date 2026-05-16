# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Structural assertions over the bundled settings.xml.

The settings.xml is hand-edited and Kodi has no schema validation —
silent breakage (a default flipped from ``true`` to garbage, a
required-creds block reordered behind an optional one) only surfaces
on a fresh install. These tests pin the load-bearing invariants:

    * Required defaults that the first-run UX depends on.
    * Category ordering inside the Connections panel (Fix #3 — the
      nzbdav + WebDAV blocks must appear ahead of Hydra/Prowlarr so a
      top-down user finishes with a working playback backend).
    * That ``*_enabled`` flags whose paired credentials default empty
      also default ``false`` (Fix #2 — opt-in after credentials).
    * That ``prowlarr_indexer_ids`` precedes its associated
      "Test Prowlarr" action (the action depends on the IDs).
"""

import os
import xml.etree.ElementTree as ET

import pytest

# pylint: disable=redefined-outer-name

_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "repo",
    "plugin.video.nzbdav",
    "resources",
    "settings.xml",
)


@pytest.fixture(scope="module")
def settings_root():
    tree = ET.parse(_SETTINGS_PATH)
    return tree.getroot()


def _setting_by_id(category, setting_id):
    for child in category.findall("setting"):
        if child.get("id") == setting_id:
            return child
    return None


def _connections_category(root):
    """The Connections category has label 30000 and is the FIRST category."""
    for cat in root.findall("category"):
        if cat.get("label") == "30000":
            return cat
    raise AssertionError("Connections category (label=30000) not found")


def _index_of_setting(category, predicate):
    """Return the position (within the category's children) of the first
    setting matching ``predicate``. -1 if absent. Used to assert
    relative ordering."""
    for idx, child in enumerate(category.findall("setting")):
        if predicate(child):
            return idx
    return -1


# --- Fix #1: webdav_url default is no longer empty -------------------------


def test_webdav_url_default_is_localhost_8080(settings_root):
    """First install must have a discoverable webdav starting point."""
    cat = _connections_category(settings_root)
    webdav_url = _setting_by_id(cat, "webdav_url")
    assert webdav_url is not None, "webdav_url setting missing"
    assert webdav_url.get("default") == "http://localhost:8080"


# --- Fix #2: nzbhydra_enabled defaults false (paired creds default empty) --


def test_nzbhydra_enabled_defaults_false(settings_root):
    """nzbhydra_enabled with empty hydra_api_key would always fail
    test_hydra on first launch — flipped to opt-in after creds."""
    cat = _connections_category(settings_root)
    hydra = _setting_by_id(cat, "nzbhydra_enabled")
    assert hydra is not None
    assert hydra.get("default") == "false"
    # Paired credentials still default empty (the user has to enter them).
    api_key = _setting_by_id(cat, "hydra_api_key")
    assert api_key.get("default") == ""


def test_prowlarr_enabled_remains_false(settings_root):
    """Confirm the Fix #2 audit conclusion: prowlarr_enabled was already
    correct; this test pins it so a future edit doesn't regress it."""
    cat = _connections_category(settings_root)
    prowlarr = _setting_by_id(cat, "prowlarr_enabled")
    assert prowlarr.get("default") == "false"


# --- Fix #3: Connections category order -----------------------------------


def test_connections_category_required_creds_first(settings_root):
    """nzbdav + WebDAV (required playback backend) must appear BEFORE
    Hydra / Prowlarr (optional indexers) in the Connections panel.
    First-run users completing the panel top-down should finish with a
    working playback backend even if they skip the indexer rows."""
    cat = _connections_category(settings_root)

    nzbdav_url_idx = _index_of_setting(cat, lambda s: s.get("id") == "nzbdav_url")
    webdav_url_idx = _index_of_setting(cat, lambda s: s.get("id") == "webdav_url")
    hydra_url_idx = _index_of_setting(cat, lambda s: s.get("id") == "hydra_url")
    prowlarr_host_idx = _index_of_setting(cat, lambda s: s.get("id") == "prowlarr_host")

    assert nzbdav_url_idx >= 0
    assert webdav_url_idx >= 0
    assert hydra_url_idx >= 0
    assert prowlarr_host_idx >= 0

    # nzbdav before webdav (logical pairing — nzbdav serves the WebDAV mount).
    assert nzbdav_url_idx < webdav_url_idx
    # Both required blocks before either optional indexer.
    assert webdav_url_idx < hydra_url_idx
    assert webdav_url_idx < prowlarr_host_idx
    # Hydra before Prowlarr (preserve the prior Hydra-first convention).
    assert hydra_url_idx < prowlarr_host_idx


def test_prowlarr_indexer_ids_precedes_test_action(settings_root):
    """``prowlarr_indexer_ids`` is consumed by the Test Prowlarr action;
    it must appear BEFORE that action in the panel so users finish
    selecting indexers before validating the connection."""
    cat = _connections_category(settings_root)
    settings_list = list(cat.findall("setting"))

    indexer_ids_idx = next(
        (
            i
            for i, s in enumerate(settings_list)
            if s.get("id") == "prowlarr_indexer_ids"
        ),
        -1,
    )
    test_action_idx = next(
        (
            i
            for i, s in enumerate(settings_list)
            if s.get("action", "").endswith("test_prowlarr)")
        ),
        -1,
    )
    assert indexer_ids_idx >= 0
    assert test_action_idx >= 0
    assert indexer_ids_idx < test_action_idx


# --- Sanity: every setting has a unique id (when id is present) -----------


def test_no_duplicate_setting_ids(settings_root):
    """Settings with an id attribute should be unique across the file —
    Kodi keys by id, and a dup silently shadows."""
    seen = []
    for setting in settings_root.iter("setting"):
        sid = setting.get("id")
        if sid:
            seen.append(sid)
    duplicates = {s for s in seen if seen.count(s) > 1}
    assert not duplicates, "duplicate setting ids: {}".format(sorted(duplicates))
