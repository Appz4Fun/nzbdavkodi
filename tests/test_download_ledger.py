# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the download pubdate ledger.

The ledger records the Usenet post-date (pubdate) of NZBs we actually
download, keyed by name, so the picker can tell apart same-name reposts
that share a size but were posted on different days.
"""

import pytest
from resources.lib import download_ledger


@pytest.fixture(autouse=True)
def _tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(download_ledger, "_ledger_dir", lambda: str(tmp_path))
    return tmp_path


_DEC15 = "Wed, 15 Dec 2021 12:00:00 +0000"  # epoch 1639569600
_DEC16 = "Thu, 16 Dec 2021 12:00:00 +0000"  # epoch 1639656000


def test_record_then_retrieve_round_trips_the_epoch():
    download_ledger.record_download("Movie X", _DEC15, size=1000)
    assert download_ledger.downloaded_pubdate_epochs("Movie X") == [1639569600]


def test_unknown_name_returns_empty_list():
    download_ledger.record_download("Movie X", _DEC15)
    assert not download_ledger.downloaded_pubdate_epochs("Other Movie")


def test_unparseable_pubdate_is_not_recorded():
    download_ledger.record_download("Movie X", "not a date")
    download_ledger.record_download("Movie X", "")
    download_ledger.record_download("Movie X", None)
    assert not download_ledger.downloaded_pubdate_epochs("Movie X")


def test_duplicate_pubdate_recorded_once():
    download_ledger.record_download("Movie X", _DEC15)
    download_ledger.record_download("Movie X", _DEC15)
    assert download_ledger.downloaded_pubdate_epochs("Movie X") == [1639569600]


def test_distinct_pubdates_accumulate_under_same_name():
    download_ledger.record_download("Movie X", _DEC15)
    download_ledger.record_download("Movie X", _DEC16)
    assert sorted(download_ledger.downloaded_pubdate_epochs("Movie X")) == [
        1639569600,
        1639656000,
    ]


def test_per_name_epoch_list_is_capped_keeping_newest():
    base = 1_600_000_000
    for i in range(download_ledger._MAX_EPOCHS_PER_NAME + 5):
        # Synthesize distinct pubdates one day apart.
        epoch = base + i * 86400
        pubdate = _epoch_to_rfc2822(epoch)
        download_ledger.record_download("Spammy Name", pubdate)
    recorded = download_ledger.downloaded_pubdate_epochs("Spammy Name")
    assert len(recorded) == download_ledger._MAX_EPOCHS_PER_NAME
    # The most recently recorded entry is retained; the oldest is dropped.
    newest = base + (download_ledger._MAX_EPOCHS_PER_NAME + 4) * 86400
    assert newest in recorded
    assert base not in recorded


def test_corrupt_ledger_file_is_tolerated(tmp_path):
    (tmp_path / download_ledger._LEDGER_FILENAME).write_text("{ not json", "utf-8")
    assert not download_ledger.downloaded_pubdate_epochs("Movie X")
    # A subsequent record still works (corrupt file is overwritten).
    download_ledger.record_download("Movie X", _DEC15)
    assert download_ledger.downloaded_pubdate_epochs("Movie X") == [1639569600]


def _epoch_to_rfc2822(epoch):
    from datetime import datetime, timezone
    from email.utils import format_datetime

    return format_datetime(datetime.fromtimestamp(epoch, tz=timezone.utc))
