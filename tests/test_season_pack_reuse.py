# SPDX-License-Identifier: GPL-3.0-or-later

"""Exact-job validation for completed season-pack reuse."""

from unittest.mock import MagicMock, patch

import pytest
from resources.lib import season_pack_reuse
from resources.lib.episode_inventory import build_video_inventory
from resources.lib.exact_job import ExactJobLookup


def _context(episode=1):
    return {
        "type": "episode",
        "title": "Spider-Noir",
        "year": 2026,
        "imdb": "tt123",
        "tvdb": "456",
        "tmdb_id": "789",
        "season": 1,
        "episode": episode,
    }


def _record(backend="nzbget", job_id="41", folder="/downloads/show"):
    return {
        "backend": backend,
        "job_id": job_id,
        "job_name": "Spider-Noir.S01",
        "folder": folder,
        "title": "Spider-Noir",
        "year": 2026,
        "imdb": "tt123",
        "tvdb": "456",
        "tmdb_id": "789",
        "season": 1,
        "episodes": [1, 2],
        "last_confirmed": 10,
    }


class _Inventory:  # pylint: disable=too-few-public-methods
    def __init__(self, path=None, files=None, episodes=(1, 2), pack_season=1):
        self.selected_path = path
        paths = list(files if files is not None else ([path] if path else []))
        built = build_video_inventory([(item, 1) for item in paths])
        self.files = built.files
        self.episodes = episodes
        self.pack_season = pack_season


def test_stale_removal_failure_is_transient():
    record = _record()
    with patch(
        "resources.lib.season_pack_reuse.season_pack.remove", return_value=False
    ) as remove:
        result = season_pack_reuse._stale(record)

    assert result.state == "transient"
    remove.assert_called_once_with("nzbget", "41")


def test_stale_removal_exception_is_transient_without_escaping():
    record = _record()
    with patch(
        "resources.lib.season_pack_reuse.season_pack.remove",
        side_effect=OSError("catalog unavailable"),
    ) as remove:
        result = season_pack_reuse._stale(record)

    assert result.state == "transient"
    remove.assert_called_once_with("nzbget", "41")


def test_reuse_missing_job_keeps_picker_record_when_remove_fails(tmp_path, monkeypatch):
    record = _record()
    monkeypatch.setattr(
        season_pack_reuse.season_pack, "_catalog_dir", lambda: str(tmp_path)
    )
    assert season_pack_reuse.season_pack.upsert(record) is True

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.stale(),
    ), patch("resources.lib.season_pack_reuse.season_pack.remove", return_value=False):
        result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbget")

    assert result.state == "transient"
    assert season_pack_reuse.season_pack.find_exact("nzbget", "41") == record


def test_nzbget_valid_exact_job_reuses_requested_episode_without_submit():
    record = _record()
    inventory = _Inventory("smb://box/done/show/Spider-Noir.S01E01.mkv")
    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/downloads/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse._nzbget_folder_for_record",
        return_value="smb://box/done/show",
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory", return_value=inventory
    ) as scan, patch(
        "resources.lib.season_pack_reuse.season_pack.upsert", return_value=True
    ) as upsert, patch(
        "resources.lib.nzbget_api.append_nzb"
    ) as submit:
        result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbget")

    assert result.state == "valid"
    assert result.stream_url.endswith("S01E01.mkv")
    assert result.stream_headers == {}
    scan.assert_called_once_with("smb://box/done/show", requested_episode=(1, 1))
    submit.assert_not_called()
    assert upsert.call_args.args[0]["job_id"] == "41"
    assert upsert.call_args.args[0]["year"] == 2026
    assert upsert.call_args.args[0]["episodes"] == [1, 2]


def test_nzbget_unreadable_selection_is_transient_without_reuse():
    record = _record()
    inventory = _Inventory("smb://box/done/show/Spider-Noir.S01E01.mkv")
    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/downloads/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse._nzbget_folder_for_record",
        return_value="smb://box/done/show",
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory", return_value=inventory
    ), patch(
        "resources.lib.season_pack_reuse._smb_selection_readable",
        return_value=False,
    ) as probe, patch(
        "resources.lib.season_pack_reuse.season_pack.upsert"
    ) as upsert:
        result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbget")

    assert result.state == "transient"
    assert result.stream_url is None
    probe.assert_called_once_with("smb://box/done/show/Spider-Noir.S01E01.mkv")
    upsert.assert_not_called()


def test_nzbget_reuse_missing_completed_base_is_transient_without_scanning():
    record = _record(folder="/srv/downloads/show")

    def getter(key, default=""):
        return {"nzbget_smb_root": "smb://box/completed"}.get(key, default)

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/srv/downloads/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse.nzbget_api.completed_base_dir",
        return_value=None,
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory"
    ) as scan, patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove:
        result = season_pack_reuse.reuse_exact_job(
            record, _context(), "nzbget", settings_getter=getter
        )

    assert result.state == "transient"
    scan.assert_not_called()
    remove.assert_not_called()


def test_nzbget_reuse_rejects_same_tail_from_outside_completed_base():
    record = _record(folder="/other/native/show")

    def getter(key, default=""):
        return {"nzbget_smb_root": "smb://box/completed"}.get(key, default)

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/other/native/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse.nzbget_api.completed_base_dir",
        return_value="/srv/downloads",
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory"
    ) as scan, patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove:
        result = season_pack_reuse.reuse_exact_job(
            record, _context(), "nzbget", settings_getter=getter
        )

    assert result.state == "transient"
    scan.assert_not_called()
    remove.assert_not_called()


def test_nzbget_reuse_rejects_job_folder_equal_to_completed_base():
    record = _record(folder="/srv/downloads")

    def getter(key, default=""):
        return {"nzbget_smb_root": "smb://box/completed"}.get(key, default)

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/srv/downloads"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse.nzbget_api.completed_base_dir",
        return_value="/srv/downloads",
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory"
    ) as scan, patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove:
        result = season_pack_reuse.reuse_exact_job(
            record, _context(), "nzbget", settings_getter=getter
        )

    assert result.state == "transient"
    scan.assert_not_called()
    remove.assert_not_called()


@pytest.mark.parametrize(
    ("native_folder", "completed_base"),
    [
        ("/srv/downloads/../other/Show", "/srv/downloads"),
        ("/srv/downloads/a/../../other/Show", "/srv/downloads"),
        ("/srv/downloads/a/../b/Show", "/srv/downloads"),
        ("relative/downloads/Show", "/srv/downloads"),
        (r"C:\downloads\..\other\Show", r"C:\downloads"),
        (r"C:downloads\Show", r"C:\downloads"),
        (r"C:\downloads\Show", r"\\server\share\downloads"),
        ("/srv/Downloads/Show", "/srv/downloads"),
    ],
)
def test_nzbget_reuse_rejects_unprovable_or_traversing_exact_mapping(
    native_folder, completed_base
):
    record = _record(folder=native_folder)

    def getter(key, default=""):
        return {"nzbget_smb_root": "smb://box/completed"}.get(key, default)

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid({"nzbid": "41", "dest_dir": native_folder}),
    ), patch(
        "resources.lib.season_pack_reuse.nzbget_api.completed_base_dir",
        return_value=completed_base,
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory"
    ) as scan, patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove:
        result = season_pack_reuse.reuse_exact_job(
            record, _context(), "nzbget", settings_getter=getter
        )

    assert result.state == "transient"
    scan.assert_not_called()
    remove.assert_not_called()


@pytest.mark.parametrize(
    ("native_folder", "completed_base", "smb_root", "expected"),
    [
        (
            "/srv/downloads/shows/Show",
            "/srv/downloads",
            "smb://box/completed",
            "smb://box/completed/shows/Show",
        ),
        (
            r"C:\Downloads\shows\Show",
            r"c:\downloads",
            "smb://box/completed",
            "smb://box/completed/shows/Show",
        ),
        (
            r"\\SERVER\Share\Downloads\Show",
            r"\\server\share\downloads",
            "smb://box/completed",
            "smb://box/completed/Show",
        ),
        (
            "/srv/base/downloads/Show",
            "/srv/base",
            "smb://box/downloads",
            "smb://box/downloads/downloads/Show",
        ),
    ],
)
def test_nzbget_cached_mapping_accepts_canonical_posix_and_windows_children(
    native_folder, completed_base, smb_root, expected
):
    record = _record(folder=native_folder)

    def getter(key, default=""):
        return {"nzbget_smb_root": smb_root}.get(key, default)

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.completed_base_dir",
        return_value=completed_base,
    ):
        assert season_pack_reuse._nzbget_folder_for_record(record, getter) == expected


def test_nzbget_cached_mapping_deduplicates_category_with_exact_evidence():
    record = _record(folder="/srv/base/movies/Show")

    def getter(key, default=""):
        return {
            "nzbget_smb_root": "smb://box/movies",
            "nzbget_category": "movies",
        }.get(key, default)

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.completed_base_dir",
        return_value="/srv/base",
    ):
        mapped = season_pack_reuse._nzbget_folder_for_record(record, getter)

    assert mapped == "smb://box/movies/Show"


def test_nzbget_cached_mapping_preserves_coincidental_tail_on_category_mismatch():
    record = _record(folder="/srv/base/movies/Show")

    def getter(key, default=""):
        return {
            "nzbget_smb_root": "smb://box/movies",
            "nzbget_category": "shows",
        }.get(key, default)

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.completed_base_dir",
        return_value="/srv/base",
    ):
        mapped = season_pack_reuse._nzbget_folder_for_record(record, getter)

    assert mapped == "smb://box/movies/movies/Show"


_AMBIGUOUS_SMB_ROOTS = (
    "smb://box",
    "smb:///completed",
    "smb://box/share//nested",
    "smb://box/share/./nested",
    "smb://box/share/../nested",
    "smb://box/share?option=1",
    "smb://box/share#fragment",
    "smb://box:not-a-port/share",
    "smb://box:/share",
    "smb://box:70000/share",
    "smb://[2001:db8::1/share",
    "smb://box/share/%2e%2e/nested",
    "smb://box/share/evil%2Fnested",
    "smb://box/share/evil%5Cnested",
    "smb://box/share/white%20space",
    "smb://box/share/new%0Aline",
    "smb://box/sha\nre",
    "smb://bo%2Fx/share",
)


@pytest.mark.parametrize("smb_root", _AMBIGUOUS_SMB_ROOTS)
def test_nzbget_cached_mapping_rejects_structurally_ambiguous_smb_roots(smb_root):
    assert (
        season_pack_reuse._exact_cached_smb_mapping(
            smb_root,
            "/srv/base/Show",
            "/srv/base",
        )
        is None
    )


@pytest.mark.parametrize(
    ("smb_root", "expected"),
    [
        ("smb://box/share", "smb://box/share/Show"),
        ("smb://192.168.1.20/share", "smb://192.168.1.20/share/Show"),
        ("smb://box/share/nested", "smb://box/share/nested/Show"),
        (
            "smb://user:p@ss@box/share",
            "smb://user:p@ss@box/share/Show",
        ),
        (
            "smb://user:p%40ss@box/share",
            "smb://user:p%40ss@box/share/Show",
        ),
        ("smb://[2001:db8::1]/share", "smb://[2001:db8::1]/share/Show"),
        ("smb://box:445/share", "smb://box:445/share/Show"),
    ],
)
def test_nzbget_cached_mapping_accepts_structurally_valid_smb_roots(smb_root, expected):
    assert (
        season_pack_reuse._exact_cached_smb_mapping(
            smb_root,
            "/srv/base/Show",
            "/srv/base",
        )
        == expected
    )


@pytest.mark.parametrize(
    "smb_root",
    [
        "smb://user:p@ss@box/share",
        "smb://user:p%40ss@box/share",
        "smb://[2001:db8::1]/share",
        "smb://box:445/share",
    ],
)
def test_nzbget_reuse_preserves_valid_structured_smb_root_without_resubmitting(
    smb_root,
):
    record = _record(folder="/srv/base/Show")
    inventory = _Inventory("{}/Show/Spider-Noir.S01E01.mkv".format(smb_root))

    def getter(key, default=""):
        return {"nzbget_smb_root": smb_root}.get(key, default)

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/srv/base/Show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse.nzbget_api.completed_base_dir",
        return_value="/srv/base",
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory", return_value=inventory
    ) as scan, patch(
        "resources.lib.season_pack_reuse.season_pack.upsert", return_value=True
    ), patch(
        "resources.lib.nzbget_api.append_nzb"
    ) as submit:
        result = season_pack_reuse.reuse_exact_job(
            record, _context(), "nzbget", settings_getter=getter
        )

    assert result.state == "valid"
    scan.assert_called_once_with("{}/Show".format(smb_root), requested_episode=(1, 1))
    submit.assert_not_called()


@pytest.mark.parametrize("smb_root", _AMBIGUOUS_SMB_ROOTS)
def test_nzbget_reuse_rejects_ambiguous_smb_root_without_scan_or_delete(smb_root):
    record = _record(folder="/srv/base/Show")

    def getter(key, default=""):
        return {"nzbget_smb_root": smb_root}.get(key, default)

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/srv/base/Show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse.nzbget_api.completed_base_dir",
        return_value="/srv/base",
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory"
    ) as scan, patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove:
        result = season_pack_reuse.reuse_exact_job(
            record, _context(), "nzbget", settings_getter=getter
        )

    assert result.state == "transient"
    scan.assert_not_called()
    remove.assert_not_called()


def test_nzbget_successful_rescan_refreshes_exact_catalog_episode_inventory(
    tmp_path, monkeypatch
):
    record = _record()
    monkeypatch.setattr(
        season_pack_reuse.season_pack, "_catalog_dir", lambda: str(tmp_path)
    )
    assert season_pack_reuse.season_pack.upsert(record)
    inventory = build_video_inventory(
        [
            ("smb://box/show/Spider-Noir.S01E01.mkv", 100),
            ("smb://box/show/Spider-Noir.S01E02.mkv", 100),
            ("smb://box/show/Spider-Noir.S01E03.mkv", 100),
        ],
        requested=(1, 3),
    )
    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/downloads/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse._nzbget_folder_for_record",
        return_value="smb://box/show",
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory", return_value=inventory
    ):
        result = season_pack_reuse.reuse_exact_job(record, _context(3), "nzbget")

    assert result.state == "valid"
    refreshed = season_pack_reuse.season_pack.find_exact("nzbget", "41")
    assert refreshed["episodes"] == [1, 2, 3]
    assert refreshed["season"] == 1
    assert refreshed["last_confirmed"] > 10
    assert refreshed["folder"] == "/downloads/show"


def test_inventory_refresh_preserves_identity_missing_from_current_context(
    tmp_path, monkeypatch
):
    record = _record()
    monkeypatch.setattr(
        season_pack_reuse.season_pack, "_catalog_dir", lambda: str(tmp_path)
    )
    assert season_pack_reuse.season_pack.upsert(record)
    inventory = build_video_inventory(
        [
            ("/show/Spider-Noir.S01E01.mkv", 100),
            ("/show/Spider-Noir.S01E02.mkv", 100),
            ("/show/Spider-Noir.S01E03.mkv", 100),
        ],
        requested=(1, 3),
    )
    partial_context = {
        "type": "episode",
        "title": "",
        "season": 1,
        "episode": 3,
    }

    season_pack_reuse._refresh_inventory(record, partial_context, inventory)

    refreshed = season_pack_reuse.season_pack.find_exact("nzbget", "41")
    assert refreshed["title"] == "Spider-Noir"
    assert refreshed["imdb"] == "tt123"
    assert refreshed["tvdb"] == "456"
    assert refreshed["tmdb_id"] == "789"
    assert (
        season_pack_reuse.season_pack.find_for_episode(_context(3), "nzbget")
        is not None
    )


def test_nzbget_duplicate_name_different_id_is_never_substituted():
    record = _record(job_id="41")
    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.stale(),
    ), patch("resources.lib.season_pack_reuse.season_pack.remove") as remove, patch(
        "resources.lib.season_pack_reuse._smb_inventory"
    ) as scan:
        result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbget")

    assert result.state == "stale"
    remove.assert_called_once_with("nzbget", "41")
    scan.assert_not_called()


def test_nzbget_folder_mismatch_removes_only_exact_record():
    record = _record(job_id="41", folder="/downloads/original")
    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/downloads/other"}
        ),
    ), patch("resources.lib.season_pack_reuse.season_pack.remove") as remove:
        result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbget")

    assert result.state == "stale"
    remove.assert_called_once_with("nzbget", "41")


def test_nzbget_transient_lookup_or_incomplete_scan_preserves_record():
    record = _record()
    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.transient(),
    ), patch("resources.lib.season_pack_reuse.season_pack.remove") as remove:
        assert (
            season_pack_reuse.reuse_exact_job(record, _context(), "nzbget").state
            == "transient"
        )
    remove.assert_not_called()

    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/downloads/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse._nzbget_folder_for_record",
        return_value="smb://box/done/show",
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory", return_value=None
    ), patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove:
        assert (
            season_pack_reuse.reuse_exact_job(record, _context(), "nzbget").state
            == "transient"
        )
    remove.assert_not_called()


def test_nzbget_reachable_empty_or_missing_episode_removes_exact_record():
    record = _record()
    lookup = ExactJobLookup.valid({"nzbid": "41", "dest_dir": "/downloads/show"})
    for inventory in (_Inventory(), _Inventory(None, files=["S01E02.mkv"])):
        with patch(
            "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
            return_value=lookup,
        ), patch(
            "resources.lib.season_pack_reuse._nzbget_folder_for_record",
            return_value="smb://box/done/show",
        ), patch(
            "resources.lib.season_pack_reuse._smb_inventory", return_value=inventory
        ), patch(
            "resources.lib.season_pack_reuse.season_pack.remove"
        ) as remove:
            result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbget")
        assert result.state == "stale"
        remove.assert_called_once_with("nzbget", "41")


def test_nzbget_cached_pack_reuse_rejects_generic_untagged_video():
    record = _record()
    inventory = build_video_inventory(
        [("smb://box/show/video.mkv", 100)], requested=(1, 2)
    )
    assert inventory.selected_path.endswith("video.mkv")
    with patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzbid": "41", "dest_dir": "/downloads/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse._nzbget_folder_for_record",
        return_value="smb://box/show",
    ), patch(
        "resources.lib.season_pack_reuse._smb_inventory", return_value=inventory
    ), patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove, patch(
        "resources.lib.nzbget_api.append_nzb"
    ) as submit:
        result = season_pack_reuse.reuse_exact_job(record, _context(2), "nzbget")

    assert result.state == "stale"
    remove.assert_called_once_with("nzbget", "41")
    submit.assert_not_called()


def test_cached_pack_reuse_accepts_exact_tag_from_multi_episode_file():
    inventory = build_video_inventory(
        [
            ("/show/Spider-Noir.S01E01E02.mkv", 200),
            ("/show/Spider-Noir.S01E03.mkv", 100),
        ],
        requested=(1, 2),
    )

    assert inventory.selected_path.endswith("S01E01E02.mkv")
    assert season_pack_reuse._inventory_selected_exact(inventory, (1, 2)) is True


def test_nzbdav_valid_exact_job_requires_playable_body():
    record = _record("nzbdav", "nzo-1", "/data/completed/show")
    inventory = _Inventory("/completed/show/Spider-Noir.S01E01.mkv")
    lookup = ExactJobLookup.valid(
        {"nzo_id": "nzo-1", "storage": "/data/completed/show"}
    )
    with patch(
        "resources.lib.nzbdav_api.lookup_completed_job_exact",
        return_value=lookup,
    ), patch(
        "resources.lib.season_pack_reuse._webdav_folder_for_record",
        return_value="/completed/show",
    ), patch(
        "resources.lib.season_pack_reuse.webdav.folder_video_inventory",
        return_value=inventory,
    ), patch(
        "resources.lib.season_pack_reuse.webdav.get_webdav_stream_url_for_path",
        return_value=("http://box/show/S01E01.mkv", {"Authorization": "x"}),
    ), patch(
        "resources.lib.season_pack_reuse._stream_body_available", return_value=True
    ) as body, patch(
        "resources.lib.season_pack_reuse.season_pack.upsert", return_value=True
    ):
        result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbdav")

    assert result.state == "valid"
    assert result.stream_url == "http://box/show/S01E01.mkv"
    assert result.stream_headers == {"Authorization": "x"}
    body.assert_called_once()


def test_nzbdav_successful_rescan_refreshes_exact_catalog_episode_inventory(
    tmp_path, monkeypatch
):
    record = _record("nzbdav", "nzo-1", "/data/completed/show")
    monkeypatch.setattr(
        season_pack_reuse.season_pack, "_catalog_dir", lambda: str(tmp_path)
    )
    assert season_pack_reuse.season_pack.upsert(record)
    inventory = build_video_inventory(
        [
            ("/completed/show/Spider-Noir.S01E01.mkv", 100),
            ("/completed/show/Spider-Noir.S01E02.mkv", 100),
            ("/completed/show/Spider-Noir.S01E03.mkv", 100),
        ],
        requested=(1, 3),
    )
    with patch(
        "resources.lib.nzbdav_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzo_id": "nzo-1", "storage": "/data/completed/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse._webdav_folder_for_record",
        return_value="/completed/show",
    ), patch(
        "resources.lib.season_pack_reuse.webdav.folder_video_inventory",
        return_value=inventory,
    ), patch(
        "resources.lib.season_pack_reuse.webdav.get_webdav_stream_url_for_path",
        return_value=("http://box/show/S01E03.mkv", {}),
    ), patch(
        "resources.lib.season_pack_reuse._stream_body_available", return_value=True
    ):
        result = season_pack_reuse.reuse_exact_job(record, _context(3), "nzbdav")

    assert result.state == "valid"
    refreshed = season_pack_reuse.season_pack.find_exact("nzbdav", "nzo-1")
    assert refreshed["episodes"] == [1, 2, 3]
    assert refreshed["season"] == 1
    assert refreshed["last_confirmed"] > 10
    assert refreshed["folder"] == "/data/completed/show"


def test_nzbdav_body_unavailable_is_transient_and_preserves_record():
    record = _record("nzbdav", "nzo-1", "/data/completed/show")
    inventory = _Inventory("/completed/show/Spider-Noir.S01E01.mkv")
    with patch(
        "resources.lib.nzbdav_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzo_id": "nzo-1", "storage": "/data/completed/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse._webdav_folder_for_record",
        return_value="/completed/show",
    ), patch(
        "resources.lib.season_pack_reuse.webdav.folder_video_inventory",
        return_value=inventory,
    ), patch(
        "resources.lib.season_pack_reuse.webdav.get_webdav_stream_url_for_path",
        return_value=("http://box/show/S01E01.mkv", {}),
    ), patch(
        "resources.lib.season_pack_reuse._stream_body_available", return_value=False
    ), patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove, patch(
        "resources.lib.season_pack_reuse.season_pack.upsert"
    ) as upsert:
        result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbdav")

    assert result.state == "transient"
    remove.assert_not_called()
    upsert.assert_not_called()


def test_nzbdav_missing_or_folder_mismatch_removes_only_exact_record():
    record = _record("nzbdav", "nzo-1", "/data/completed/show")
    lookups = (
        ExactJobLookup.stale(),
        ExactJobLookup.valid(
            {"nzo_id": "nzo-1", "storage": "/data/completed/different"}
        ),
    )
    for lookup in lookups:
        with patch(
            "resources.lib.nzbdav_api.lookup_completed_job_exact",
            return_value=lookup,
        ), patch("resources.lib.season_pack_reuse.season_pack.remove") as remove:
            result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbdav")
        assert result.state == "stale"
        remove.assert_called_once_with("nzbdav", "nzo-1")


def test_nzbdav_transient_or_incomplete_inventory_preserves_record():
    record = _record("nzbdav", "nzo-1", "/data/completed/show")
    with patch(
        "resources.lib.nzbdav_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.transient(),
    ), patch("resources.lib.season_pack_reuse.season_pack.remove") as remove:
        assert (
            season_pack_reuse.reuse_exact_job(record, _context(), "nzbdav").state
            == "transient"
        )
    remove.assert_not_called()

    with patch(
        "resources.lib.nzbdav_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzo_id": "nzo-1", "storage": "/data/completed/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse._webdav_folder_for_record",
        return_value="/completed/show",
    ), patch(
        "resources.lib.season_pack_reuse.webdav.folder_video_inventory",
        return_value=None,
    ), patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove:
        assert (
            season_pack_reuse.reuse_exact_job(record, _context(), "nzbdav").state
            == "transient"
        )
    remove.assert_not_called()


def test_nzbdav_reachable_empty_or_missing_requested_episode_removes_record():
    record = _record("nzbdav", "nzo-1", "/data/completed/show")
    lookup = ExactJobLookup.valid(
        {"nzo_id": "nzo-1", "storage": "/data/completed/show"}
    )
    for inventory in (_Inventory(), _Inventory(None, files=["S01E02.mkv"])):
        with patch(
            "resources.lib.nzbdav_api.lookup_completed_job_exact",
            return_value=lookup,
        ), patch(
            "resources.lib.season_pack_reuse._webdav_folder_for_record",
            return_value="/completed/show",
        ), patch(
            "resources.lib.season_pack_reuse.webdav.folder_video_inventory",
            return_value=inventory,
        ), patch(
            "resources.lib.season_pack_reuse.season_pack.remove"
        ) as remove:
            result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbdav")
        assert result.state == "stale"
        remove.assert_called_once_with("nzbdav", "nzo-1")


def test_nzbdav_cached_pack_reuse_rejects_generic_untagged_video():
    record = _record("nzbdav", "nzo-1", "/data/completed/show")
    inventory = build_video_inventory(
        [("/completed/show/video.mkv", 100)], requested=(1, 2)
    )
    assert inventory.selected_path.endswith("video.mkv")
    with patch(
        "resources.lib.nzbdav_api.lookup_completed_job_exact",
        return_value=ExactJobLookup.valid(
            {"nzo_id": "nzo-1", "storage": "/data/completed/show"}
        ),
    ), patch(
        "resources.lib.season_pack_reuse._webdav_folder_for_record",
        return_value="/completed/show",
    ), patch(
        "resources.lib.season_pack_reuse.webdav.folder_video_inventory",
        return_value=inventory,
    ), patch(
        "resources.lib.season_pack_reuse.webdav.get_webdav_stream_url_for_path"
    ) as stream_url, patch(
        "resources.lib.season_pack_reuse.season_pack.remove"
    ) as remove, patch(
        "resources.lib.nzbdav_api.submit_nzb"
    ) as submit:
        result = season_pack_reuse.reuse_exact_job(record, _context(2), "nzbdav")

    assert result.state == "stale"
    remove.assert_called_once_with("nzbdav", "nzo-1")
    stream_url.assert_not_called()
    submit.assert_not_called()


def test_active_backend_isolation_rejects_record_without_deleting_it():
    record = _record("nzbget")
    with patch("resources.lib.season_pack_reuse.season_pack.remove") as remove, patch(
        "resources.lib.season_pack_reuse.nzbget_api.lookup_completed_job_exact"
    ) as lookup:
        result = season_pack_reuse.reuse_exact_job(record, _context(), "nzbdav")

    assert result.state == "not_applicable"
    remove.assert_not_called()
    lookup.assert_not_called()


def test_nzbget_backend_entrypoint_reuses_pack_before_requiring_nzb_url():
    from resources.lib import nzbget_resolver

    success = MagicMock()
    failure = MagicMock()
    record = _record()
    reuse = season_pack_reuse.ReuseResult("valid", "smb://box/show/S01E01.mkv", {})

    def getter(key, default=""):
        return {
            "nzbget_url": "http://get:6789",
            "nzbget_smb_root": "smb://box/done",
        }.get(key, default)

    with patch.object(
        nzbget_resolver.nzbget_api, "completed_base_dir", return_value="/downloads"
    ), patch(
        "resources.lib.season_pack_reuse.reuse_exact_job", return_value=reuse
    ), patch.object(
        nzbget_resolver.nzbget_api, "append_nzb"
    ) as submit:
        nzbget_resolver._run_nzbget_backend(
            "",
            "Spider-Noir.S01",
            getter,
            success,
            failure,
            season_pack_record=record,
            episode_context=_context(),
        )

    success.assert_called_once_with("smb://box/show/S01E01.mkv")
    failure.assert_not_called()
    submit.assert_not_called()


def test_nzbdav_acquire_entrypoint_reuses_pack_without_submit():
    from resources.lib import resolver

    effects = MagicMock()
    effects.episode_context = _context()
    record = _record("nzbdav", "nzo-1", "/data/completed/show")
    reuse = season_pack_reuse.ReuseResult(
        "valid", "http://box/show/S01E01.mkv", {"Authorization": "x"}
    )
    with patch(
        "resources.lib.season_pack_reuse.reuse_exact_job", return_value=reuse
    ), patch("resources.lib.resolver._resolve_submit_and_poll") as submit:
        result = resolver._resolve_acquire_stream(
            "", "Spider-Noir", {"_season_pack": record}, set(), effects
        )

    assert result == (
        "http://box/show/S01E01.mkv",
        {"Authorization": "x"},
        None,
    )
    effects.disable_fallbacks.assert_called_once_with()
    submit.assert_not_called()


def test_nzbdav_pack_reuse_disables_delayed_fallback_provider_submissions():
    from resources.lib import resolver

    loader = MagicMock()
    effects = resolver._ResolveSideEffects(
        {},
        [{"nzb_url": "http://provider/backup.nzb"}],
        loader,
        "",
        MagicMock(),
    )
    effects.disable_fallbacks()

    with patch("resources.lib.resolver._start_playback_state_cleanup"), patch(
        "resources.lib.resolver._start_fallback_submit_worker",
        return_value={"finished": True},
    ) as start:
        effects.start_fallback_after_primary(None)

    assert start.call_args.args[0] == []
    assert start.call_args.kwargs["candidate_loader"] is None


def test_pack_entry_paths_drop_provider_loader_entirely():
    from resources.lib import resolver

    raw_loader = MagicMock()
    params = {
        "_season_pack": _record("nzbdav", "nzo-1", "/data/completed/show"),
        "_fallback_candidate_loader": raw_loader,
    }
    with patch(
        "resources.lib.resolver._prefetch_fallback_candidate_loader"
    ) as prefetch:
        handle_loader = resolver._entry_fallback_candidate_loader(params)
        effects = resolver._resolve_and_play_make_effects(
            params, params, "", settings_getter=None
        )

    assert handle_loader is None
    assert effects._loader is None
    prefetch.assert_not_called()
    raw_loader.assert_not_called()


def test_stale_nzbdav_pack_notifies_and_fails_without_provider_submit():
    from resources.lib import resolver

    raw_loader = MagicMock()
    params = {
        "_season_pack": _record("nzbdav", "nzo-1", "/data/completed/show"),
        "_fallback_candidate_loader": raw_loader,
        "_episode_context": _context(),
    }
    provider_url = "http://provider/episode.nzb"
    effects = resolver._ResolveSideEffects(
        params, [], raw_loader, provider_url, MagicMock()
    )
    with patch(
        "resources.lib.season_pack_reuse.reuse_exact_job",
        return_value=season_pack_reuse.ReuseResult("stale", None, None),
    ), patch(
        "resources.lib.resolver._resolve_submit_and_poll",
        return_value=(None, None, None),
    ) as submit, patch(
        "resources.lib.resolver._notify", side_effect=RuntimeError("no UI")
    ) as notify:
        result = resolver._resolve_acquire_stream(
            provider_url, "Spider-Noir", params, set(), effects
        )

    assert result == (None, None, None)
    submit.assert_not_called()
    raw_loader.assert_not_called()
    notify.assert_called_once_with(
        resolver._addon_name(), resolver._string(30365), 4000
    )


def test_transient_nzbdav_pack_fails_closed_without_notice_or_submit():
    from resources.lib import resolver

    params = {
        "_season_pack": _record("nzbdav", "nzo-1", "/data/completed/show"),
        "_episode_context": _context(),
    }
    provider_url = "http://provider/episode.nzb"
    effects = resolver._ResolveSideEffects(params, [], None, provider_url, MagicMock())
    with patch(
        "resources.lib.season_pack_reuse.reuse_exact_job",
        return_value=season_pack_reuse.ReuseResult("transient", None, None),
    ), patch(
        "resources.lib.resolver._resolve_submit_and_poll",
        return_value=(None, None, None),
    ) as submit, patch(
        "resources.lib.resolver._notify"
    ) as notify:
        result = resolver._resolve_acquire_stream(
            provider_url, "Spider-Noir", params, set(), effects
        )

    assert result == (None, None, None)
    submit.assert_not_called()
    notify.assert_not_called()


def test_nzbget_stale_pack_notifies_and_fails_without_provider_submit():
    from resources.lib import nzbget_resolver

    ctx = MagicMock()
    ctx.season_pack_record = _record("nzbget")
    ctx.episode_context = _context()
    ctx.settings_getter = None
    with patch(
        "resources.lib.season_pack_reuse.reuse_exact_job",
        return_value=season_pack_reuse.ReuseResult("stale", None, None),
    ), patch.object(
        nzbget_resolver, "_submit_poll_resolve", return_value=True
    ) as submit, patch.object(
        nzbget_resolver, "_notify", side_effect=RuntimeError("no UI")
    ) as notify:
        result = nzbget_resolver._reuse_or_submit(
            ctx,
            "http://provider/episode.nzb",
            "Spider-Noir.S01",
            None,
            (None, None),
        )

    assert result is False
    notify.assert_called_once_with(
        nzbget_resolver._addon_name(), nzbget_resolver._string(30365), 4000
    )
    ctx.on_failure.assert_called_once_with(None)
    submit.assert_not_called()


def test_nzbget_transient_pack_fails_closed_without_notice_or_submit():
    from resources.lib import nzbget_resolver

    ctx = MagicMock()
    ctx.season_pack_record = _record("nzbget")
    ctx.episode_context = _context()
    ctx.settings_getter = None
    with patch(
        "resources.lib.season_pack_reuse.reuse_exact_job",
        return_value=season_pack_reuse.ReuseResult("transient", None, None),
    ), patch.object(
        nzbget_resolver, "_submit_poll_resolve", return_value=True
    ) as submit, patch.object(
        nzbget_resolver, "_notify"
    ) as notify:
        result = nzbget_resolver._reuse_or_submit(
            ctx,
            "http://provider/episode.nzb",
            "Spider-Noir.S01",
            None,
            (None, None),
        )

    assert result is False
    ctx.on_failure.assert_called_once_with(nzbget_resolver._string(30223))
    submit.assert_not_called()
    notify.assert_not_called()


def test_handle_resolve_pack_failure_still_sets_resolved_url_false():
    from resources.lib import resolver

    params = {
        "nzburl": "",
        "title": "Spider-Noir",
        "_season_pack": _record("nzbdav", "nzo-1", "/data/completed/show"),
        "_episode_context": _context(),
    }
    with patch("resources.lib.resolver._nzbget_enabled", return_value=False), patch(
        "resources.lib.resolver._resolve_acquire_stream",
        return_value=(None, None, None),
    ), patch(
        "resources.lib.resolver._resolve_finish_or_reject",
        wraps=resolver._resolve_finish_or_reject,
    ), patch(
        "resources.lib.resolver.xbmcplugin.setResolvedUrl"
    ) as resolved:
        resolver.resolve(7, params)

    assert resolved.call_count == 1
    assert resolved.call_args.args[:2] == (7, False)
