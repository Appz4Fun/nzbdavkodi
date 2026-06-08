# SPDX-License-Identifier: GPL-3.0-or-later
from resources.lib.nzbget_resolver import nzbget_smb_target, pick_largest_video


def test_smb_target_appends_release_folder():
    # No category configured -> just the release folder under the root.
    target = nzbget_smb_target(
        "smb://user:pw@host/completed",
        "/downloads/completed/movies/The.Movie.2024.1080p",
    )
    assert target == "smb://user:pw@host/completed/The.Movie.2024.1080p"


def test_smb_target_includes_category_subfolder():
    # With NZBGet's default AppendCategoryDir=yes the release lands under
    # <completed>/<category>/<release>; the SMB target must include the
    # category segment or it resolves to a folder that does not exist.
    target = nzbget_smb_target(
        "smb://user:pw@host/completed",
        "/downloads/completed/movies/The.Movie.2024.1080p",
        category="movies",
    )
    assert target == "smb://user:pw@host/completed/movies/The.Movie.2024.1080p"


def test_smb_target_maps_relative_to_completed_base():
    # With NZBGet's global completed base known, DestDir maps relative to it —
    # exact even for a category-specific custom folder whose name differs from
    # the category setting (Codex: category=movies but DestDir under films/).
    target = nzbget_smb_target(
        "smb://host/completed",
        "/downloads/completed/films/The.Movie.2024",
        category="movies",
        completed_base="/downloads/completed",
    )
    assert target == "smb://host/completed/films/The.Movie.2024"


def test_smb_target_completed_base_no_category_dir():
    # AppendCategoryDir=no: DestDir sits directly under the completed base.
    target = nzbget_smb_target(
        "smb://host/completed",
        "/downloads/completed/The.Movie.2024",
        category="movies",
        completed_base="/downloads/completed",
    )
    assert target == "smb://host/completed/The.Movie.2024"


def test_smb_target_completed_base_avoids_doubled_tail():
    # smb_root pointed at a subfolder of the completed base must not double the
    # relative segment.
    target = nzbget_smb_target(
        "smb://host/completed/movies",
        "/downloads/completed/movies/The.Movie.2024",
        category="movies",
        completed_base="/downloads/completed",
    )
    assert target == "smb://host/completed/movies/The.Movie.2024"


def test_smb_target_no_base_share_alias_maps_release_directly():
    # completed_base unavailable; AppendCategoryDir=no with an SMB share alias
    # whose name differs from the server-side completed folder. The release must
    # map directly under the SMB root, NOT nest the server folder name.
    target = nzbget_smb_target(
        "smb://nas/nzbget",
        "/downloads/completed/Release",
        category="movies",
    )
    assert target == "smb://nas/nzbget/Release"


def test_smb_target_omits_category_when_destdir_not_nested():
    # AppendCategoryDir=no (or a category-specific DestDir): NZBGet reports the
    # release directly under completed, with no category folder. Even though a
    # category is configured, the SMB target must follow the *actual* DestDir
    # layout and NOT insert a synthetic category segment that 404s.
    target = nzbget_smb_target(
        "smb://user:pw@host/completed",
        "/downloads/completed/The.Movie.2024.1080p",
        category="movies",
    )
    assert target == "smb://user:pw@host/completed/The.Movie.2024.1080p"


def test_smb_target_does_not_double_category_when_root_already_nested():
    # If the user pointed nzbget_smb_root at the category subdir already,
    # don't insert the category twice.
    target = nzbget_smb_target(
        "smb://host/completed/movies",
        "/downloads/completed/movies/The.Movie.2024",
        category="movies",
    )
    assert target == "smb://host/completed/movies/The.Movie.2024"


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
