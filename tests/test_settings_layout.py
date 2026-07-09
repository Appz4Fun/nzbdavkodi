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
    for child in category.iter("setting"):
        if child.get("id") == setting_id:
            return child
    return None


def _connections_category(root):
    """The Connections category has label 30000 and is the FIRST category.

    Settings live under <settings version="1"><section><category><group>
    <setting>...</setting></group></category></section></settings>.
    """
    for cat in root.find("section").findall("category"):
        if cat.get("label") == "30000":
            return cat
    raise AssertionError("Connections category (label=30000) not found")


def _index_of_setting(category, predicate):
    """Return the position (within the category, across all its groups) of
    the first setting matching ``predicate``. -1 if absent. Used to assert
    relative ordering. ``category.iter("setting")`` walks groups in document
    order, so positions still reflect on-screen order."""
    for idx, child in enumerate(category.iter("setting")):
        if predicate(child):
            return idx
    return -1


def _categories(root):
    section = root.find("section")
    assert section is not None, "new-format settings.xml missing section"
    assert section.get("id") == "plugin.video.nzbdav"
    return section.findall("category")


def _category_by_label(root, label):
    for category in _categories(root):
        if category.get("label") == label:
            return category
    raise AssertionError("category label={} not found".format(label))


def _dependency_for(setting, dep_type, dep_setting):
    for dependency in setting.findall("./dependencies/dependency"):
        if (
            dependency.get("type") == dep_type
            and dependency.get("setting") == dep_setting
        ):
            return dependency
    return None


def test_settings_xml_uses_new_format_with_category_help(settings_root):
    """Kodi's old add-on settings format ignores category help; keep the
    Matrix/Omega settings format so each tab can show a help blurb."""
    assert settings_root.get("version") == "1"
    categories = _categories(settings_root)
    assert categories, "settings.xml has no categories"
    for category in categories:
        assert category.get("id"), "category missing stable id"
        assert category.get("help"), "category {} missing help id".format(
            category.get("label")
        )


# --- Fix #1: webdav_url default is no longer empty -------------------------


def test_webdav_url_default_is_localhost_8080(settings_root):
    """First install must have a discoverable webdav starting point."""
    cat = _connections_category(settings_root)
    webdav_url = _setting_by_id(cat, "webdav_url")
    assert webdav_url is not None, "webdav_url setting missing"
    assert webdav_url.findtext("default") == "http://localhost:8080"


# --- Fix #2: nzbhydra_enabled defaults false (paired creds default empty) --


def test_nzbhydra_enabled_defaults_false(settings_root):
    """nzbhydra_enabled with empty hydra_api_key would always fail
    test_hydra on first launch — flipped to opt-in after creds."""
    cat = _connections_category(settings_root)
    hydra = _setting_by_id(cat, "nzbhydra_enabled")
    assert hydra is not None
    assert hydra.findtext("default") == "false"
    # Paired credentials still default empty (the user has to enter them).
    api_key = _setting_by_id(cat, "hydra_api_key")
    assert api_key.findtext("default") == ""


def test_prowlarr_enabled_remains_false(settings_root):
    """Confirm the Fix #2 audit conclusion: prowlarr_enabled was already
    correct; this test pins it so a future edit doesn't regress it."""
    cat = _connections_category(settings_root)
    prowlarr = _setting_by_id(cat, "prowlarr_enabled")
    assert prowlarr.findtext("default") == "false"


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
    settings_list = list(cat.iter("setting"))

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
            if s.findtext("data", "").endswith("test_prowlarr)")
        ),
        -1,
    )
    assert indexer_ids_idx >= 0
    assert test_action_idx >= 0
    assert indexer_ids_idx < test_action_idx


def test_direct_indexer_options_depend_on_master_toggle(settings_root):
    """All direct-indexer options must be tied to the in-dialog master toggle
    via an `enable` dependency, not `visible`.

    Kodi's CGUIDialogSettingsBase only creates GUI controls for a group at
    dialog-build time if the group contains at least one currently-visible
    setting (CSettingCategory::GetGroups -> ContainsVisibleSettings). The
    "Popular Indexers" / "Custom Newznab Indexers" groups have no setting
    other than these dependents, so when direct_indexers_enabled is off,
    those groups are entirely hidden and get NO controls built at all —
    meaning there is nothing for the live dependency-update mechanism to
    later reveal when the toggle flips (confirmed live on a Kodi 21.3-Omega
    device: toggling stayed stuck until switching category tabs away and
    back forced a rebuild). `enable` sidesteps this because it never hides
    the setting from ContainsVisibleSettings — the row always renders, just
    greyed out — and enabled/disabled state DOES update live through the
    same UpdateSettingControl path regardless of group composition.
    """
    cat = _category_by_label(settings_root, "30163")
    settings_list = list(cat.iter("setting"))
    master_idx = _index_of_setting(
        cat, lambda s: s.get("id") == "direct_indexers_enabled"
    )
    assert master_idx >= 0

    for setting in settings_list[master_idx + 1 :]:
        assert (
            _dependency_for(setting, "enable", "direct_indexers_enabled") is not None
        ), "setting {} is not tied to direct_indexers_enabled via enable=".format(
            setting.get("id") or setting.get("label")
        )


# --- Sanity: every setting has a unique id (when id is present) -----------


def _setting_anywhere(root, setting_id):
    for setting in root.iter("setting"):
        if setting.get("id") == setting_id:
            return setting
    return None


def test_readahead_buffer_mb_setting_present(settings_root):
    """The read-ahead prefetch cache is gated by readahead_buffer_mb; pin its
    layout (type=integer, default=256) like the sibling tuning settings."""
    setting = _setting_anywhere(settings_root, "readahead_buffer_mb")
    assert setting is not None, "readahead_buffer_mb setting missing"
    assert setting.get("type") == "integer"
    assert setting.findtext("default") == "256"
    assert setting.get("label") == "30207"


def test_passthrough_stall_wait_setting_present(settings_root):
    """Pin the passthrough_stall_wait layout (was previously unasserted)."""
    setting = _setting_anywhere(settings_root, "passthrough_stall_wait")
    assert setting is not None
    assert setting.get("type") == "integer"
    assert setting.findtext("default") == "120"


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
