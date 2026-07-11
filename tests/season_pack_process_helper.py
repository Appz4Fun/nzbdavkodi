# SPDX-License-Identifier: GPL-3.0-or-later

"""Spawn-safe worker used by the season-pack process-lock regression."""

from threading import BrokenBarrierError

from tests.kodi_mocks import install_kodi_mocks


def racing_upsert(catalog_dir, record, barrier, results):
    """Force the historical load/save race in a separate process."""
    install_kodi_mocks()
    from resources.lib import season_pack

    season_pack._catalog_dir = lambda: catalog_dir
    original_load = season_pack._load_records_unlocked

    def synchronized_load(path=None):
        rows = original_load(path=path)
        if not rows:
            try:
                barrier.wait(timeout=0.5)
            except BrokenBarrierError:
                pass
        return rows

    season_pack._load_records_unlocked = synchronized_load
    results.put(season_pack.upsert(record))
