# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Persistent record of the Usenet post-dates of NZBs we have downloaded.

nzbdav history is keyed by NAME and stores only the *download-completion*
timestamp, never the release's Usenet post date. So a same-name, same-size
repost is indistinguishable from the file we actually fetched using history
alone, and the picker's "DL"/cached-stream tag would land on both.

We capture the selected result's ``pubdate`` at submit time here and consult
it from ``router._tag_available`` so the tag stays on the release we really
downloaded (or another we also downloaded), not on a look-alike repost posted
on a different day. The stored value is the absolute UTC epoch (see
``http_util.pubdate_to_epoch``) rather than the drifting human "age" label.

Fails soft throughout: any read/parse/write error degrades to "no record",
which makes the picker fall back to its prior name+size behavior rather than
hide a real cache hit.
"""

import json
import os

import xbmc
import xbmcvfs

from resources.lib.http_util import pubdate_to_epoch

# Resolve the profile dir via the special:// path rather than
# ``xbmcaddon.Addon().getAddonInfo("profile")``: the file-path RunScript
# context must never touch ``xbmcaddon.Addon`` (it can crash CoreELEC inside
# the profile lookup), and ``translatePath`` of a special:// path needs no
# Addon handle. Both forms resolve to the same addon_data directory.
_PROFILE_SPECIAL_PATH = "special://profile/addon_data/plugin.video.nzbdav"
_LEDGER_FILENAME = "download_pubdates.json"
# Bound growth: most names get one or two entries; a pathological case
# (a generic filename re-uploaded constantly) is capped so the file and
# the per-lookup scan stay small. Oldest entries are dropped first.
_MAX_NAMES = 500
_MAX_EPOCHS_PER_NAME = 20


def _ledger_dir():
    profile = xbmcvfs.translatePath(_PROFILE_SPECIAL_PATH)
    os.makedirs(profile, exist_ok=True)
    return profile


def _ledger_path():
    return os.path.join(_ledger_dir(), _LEDGER_FILENAME)


def _load():
    """Return the ledger dict, or an empty dict on any read/parse error."""
    try:
        with open(_ledger_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save(data):
    try:
        with open(_ledger_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError as error:
        xbmc.log(
            "NZB-DAV: Could not persist download ledger: {}".format(error),
            xbmc.LOGWARNING,
        )


def _coerce_epoch_list(value):
    """Return value as a list of int epochs, dropping anything non-numeric."""
    if not isinstance(value, list):
        return []
    epochs = []
    for item in value:
        try:
            epochs.append(int(item))
        except (TypeError, ValueError):
            continue
    return epochs


def record_download(name, pubdate, size=None):
    """Record that the NZB ``name`` posted at ``pubdate`` was downloaded.

    ``size`` is accepted for call-site symmetry with the picker's size
    gate but is not currently stored — the post-date alone disambiguates
    same-name reposts, and size is already gated separately against the
    completed job's bytes. No-op when ``name`` is empty or ``pubdate``
    cannot be parsed (we simply have nothing to disambiguate by).
    """
    del size  # reserved for future use; see docstring
    if not name:
        return
    epoch = pubdate_to_epoch(pubdate)
    if epoch is None:
        return
    try:
        data = _load()
        existing = _coerce_epoch_list(data.get(name))
        if epoch in existing:
            return
        existing.append(epoch)
        if len(existing) > _MAX_EPOCHS_PER_NAME:
            existing = existing[-_MAX_EPOCHS_PER_NAME:]
        data[name] = existing
        if len(data) > _MAX_NAMES:
            # Drop arbitrary oldest-inserted names; dict preserves insertion
            # order, so slicing the tail keeps the most recently touched.
            data = dict(list(data.items())[-_MAX_NAMES:])
        _save(data)
    except Exception as error:  # pylint: disable=broad-except
        # Best-effort bookkeeping must never break a download. Any storage
        # surprise (unwritable profile, odd path) degrades to "not recorded".
        xbmc.log(
            "NZB-DAV: download-ledger record skipped: {}".format(error),
            xbmc.LOGDEBUG,
        )


def downloaded_pubdate_epochs(name):
    """Return the recorded pubdate epochs (ints) for ``name``, or ``[]``.

    Fails soft to ``[]`` on any error so the picker's pubdate gate degrades
    to its prior name+size behavior rather than raising into the lookup.
    """
    if not name:
        return []
    try:
        return _coerce_epoch_list(_load().get(name))
    except Exception as error:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: download-ledger read failed: {}".format(error),
            xbmc.LOGDEBUG,
        )
        return []
