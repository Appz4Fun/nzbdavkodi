# SPDX-License-Identifier: GPL-3.0-or-later
from resources.lib.nzbget_resolver import nzbget_smb_target, pick_largest_video


def test_smb_target_appends_release_folder():
    target = nzbget_smb_target(
        "smb://user:pw@host/completed",
        "/downloads/completed/movies/The.Movie.2024.1080p",
    )
    assert target == "smb://user:pw@host/completed/The.Movie.2024.1080p"


def test_smb_target_handles_trailing_slashes():
    target = nzbget_smb_target("smb://host/completed/", "/dl/completed/Show.S01E01/")
    assert target == "smb://host/completed/Show.S01E01"


def test_smb_target_empty_destdir_returns_none():
    assert nzbget_smb_target("smb://host/completed", "") is None


def test_pick_largest_video_chooses_biggest_video_extension():
    files = ["sample.mkv", "movie.mkv", "readme.txt"]
    sizes = {"sample.mkv": 50, "movie.mkv": 8000, "readme.txt": 1}
    assert pick_largest_video(files, lambda f: sizes[f]) == "movie.mkv"


def test_pick_largest_video_ignores_non_video():
    files = ["notes.nfo", "art.jpg"]
    assert pick_largest_video(files, lambda f: 100) is None
