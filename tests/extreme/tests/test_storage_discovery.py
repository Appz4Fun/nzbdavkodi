# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import importlib

_storage_discovery = importlib.import_module("tests.extreme.scripts._storage_discovery")


def test_discover_cinefile_storages_non_positive_limit_skips_propfind(monkeypatch):
    monkeypatch.setattr(
        _storage_discovery,
        "_propfind",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("PROPFIND should not run")
        ),
    )

    assert _storage_discovery.discover_cinefile_storages(limit=0) == []
