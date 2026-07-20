# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import time as _time_module
from unittest.mock import MagicMock, patch

from resources.lib.nzbget_resolver import (
    _HEALTHCHECK_WARNED,
    _dupe_check_disabled,
    _handle_poll_failure,
    _read_poll_interval,
    _read_settings,
    _snapshot_conn_getter,
    _spawn_dupe_backups,
    _submit_dupe_backups,
    _tick_group_follow,
    _warn_if_healthcheck_pauses,
    play_nzbget,
    poll_nzbget_job,
    resolve_and_play_nzbget,
    resolve_smb_video,
)


class _Dialog:
    def __init__(self):
        self.canceled = False
        self.lines = []

    def iscanceled(self):
        return self.canceled

    def update(self, percent, message=""):
        self.lines.append((percent, message))


class _Monitor:  # pylint: disable=too-few-public-methods
    def __init__(self, aborts_after=999):
        self.calls = 0
        self.aborts_after = aborts_after

    def waitForAbort(self, timeout=0.0):
        self.calls += 1
        return self.calls >= self.aborts_after


def _settings(values):
    return lambda k, d="": values.get(k, d)


def _full_settings():
    return _settings(
        {
            "nzbget_url": "http://box:6789",
            "nzbget_smb_root": "smb://host/completed",
            "download_timeout": "600",
        }
    )


def _season_pack_params():
    return {
        "nzburl": "",
        "title": "Spider-Noir.S01",
        "_season_pack": {
            "backend": "nzbget",
            "job_id": "41",
            "job_name": "Spider-Noir.S01",
            "folder": "/downloads/Spider-Noir.S01",
            "title": "Spider-Noir",
            "imdb": "",
            "tvdb": "451234",
            "tmdb_id": "",
            "season": 1,
            "episodes": [1, 2],
            "last_confirmed": 1,
        },
        "_episode_context": {
            "type": "episode",
            "title": "Spider-Noir",
            "imdb": "",
            "tvdb": "451234",
            "tmdb_id": "",
            "season": 1,
            "episode": 2,
        },
    }


def test_handle_stale_pack_shows_one_notice_and_resolves_false():
    from resources.lib import nzbget_resolver, season_pack_reuse

    with patch(
        "resources.lib.season_pack_reuse.reuse_exact_job",
        return_value=season_pack_reuse.ReuseResult("stale", None, None),
    ), patch("resources.lib.nzbget_resolver._notify") as notify, patch(
        "resources.lib.nzbget_resolver.xbmcplugin.setResolvedUrl"
    ) as resolved, patch.object(
        nzbget_resolver.nzbget_api, "append_nzb"
    ) as submit:
        resolve_and_play_nzbget(7, _season_pack_params(), _full_settings())

    notify.assert_called_once_with(
        nzbget_resolver._addon_name(), nzbget_resolver._string(30365), 4000
    )
    assert resolved.call_count == 1
    assert resolved.call_args.args[:2] == (7, False)
    submit.assert_not_called()


def test_handle_unreadable_pack_fails_closed_without_generic_toast():
    # The probe layer already toasted the specific restart-Kodi warning; the
    # pack failure path must not stack the generic no-video message on top,
    # and must not fall through to a submit.
    from resources.lib import nzbget_resolver, season_pack_reuse

    with patch(
        "resources.lib.season_pack_reuse.reuse_exact_job",
        return_value=season_pack_reuse.ReuseResult("unreadable", None, None),
    ), patch("resources.lib.nzbget_resolver._notify") as notify, patch(
        "resources.lib.nzbget_resolver.xbmcplugin.setResolvedUrl"
    ) as resolved, patch.object(
        nzbget_resolver.nzbget_api, "append_nzb"
    ) as submit:
        resolve_and_play_nzbget(7, _season_pack_params(), _full_settings())

    notify.assert_not_called()
    assert resolved.call_count == 1
    assert resolved.call_args.args[:2] == (7, False)
    submit.assert_not_called()


def test_handleless_stale_pack_shows_one_notice_without_starting_player():
    from resources.lib import nzbget_resolver, season_pack_reuse

    params = _season_pack_params()
    with patch(
        "resources.lib.season_pack_reuse.reuse_exact_job",
        return_value=season_pack_reuse.ReuseResult("stale", None, None),
    ), patch("resources.lib.nzbget_resolver._notify") as notify, patch(
        "resources.lib.nzbget_resolver.xbmc.Player"
    ) as player, patch.object(
        nzbget_resolver.nzbget_api, "append_nzb"
    ) as submit:
        play_nzbget(
            "", params["title"], params=params, settings_getter=_full_settings()
        )

    notify.assert_called_once_with(
        nzbget_resolver._addon_name(), nzbget_resolver._string(30365), 4000
    )
    player.return_value.play.assert_not_called()
    submit.assert_not_called()


def test_resolve_smb_video_returns_largest_file_url():
    xbmcvfs = sys.modules["xbmcvfs"]

    def fake_stat(path):
        st = MagicMock()
        st.st_size.return_value = 9000 if path.endswith("movie.mkv") else 10
        return st

    with patch.object(
        xbmcvfs, "listdir", return_value=([], ["sample.mkv", "movie.mkv"])
    ), patch.object(xbmcvfs, "Stat", side_effect=fake_stat):
        url = resolve_smb_video("smb://host/completed/The.Movie", monitor=_Monitor())
    assert url == "smb://host/completed/The.Movie/movie.mkv"


def test_resolve_smb_video_keeps_literal_spaces_unencoded_in_returned_url():
    # NZBGet's DestDir mirrors the NZB post title verbatim, so real release
    # folders routinely contain literal spaces. Kodi's own SMB VFS percent-
    # encodes the path internally before it hits the wire, so this module
    # must NOT pre-encode: doing so double-encodes ("%20" becomes literal
    # "%2520" on the wire) and 404s with "No such file or directory" even
    # though the identical raw path lists/stats/reads fine through xbmcvfs.
    xbmcvfs = sys.modules["xbmcvfs"]
    folder = "smb://host/completed/Logan 2017 2160p WEB-DL DV HDR-FLUX"

    def fake_listdir(path):
        assert path == folder, "listdir must receive the raw, unencoded path"
        return [], ["Logan 2017 2160p WEB-DL DV HDR-FLUX.mkv"]

    def fake_stat(path):
        assert " " in path, "the read/stat probe must receive the raw path"
        st = MagicMock()
        st.st_size.return_value = 9000
        return st

    def fake_file(path):
        assert " " in path, "the readability probe must receive the raw path"
        handle = MagicMock()
        handle.readBytes.return_value = b"data"
        return handle

    with patch.object(xbmcvfs, "listdir", side_effect=fake_listdir), patch.object(
        xbmcvfs, "Stat", side_effect=fake_stat
    ), patch.object(xbmcvfs, "File", side_effect=fake_file):
        url = resolve_smb_video(folder, monitor=_Monitor())
    assert url == (
        "smb://host/completed/Logan 2017 2160p WEB-DL DV HDR-FLUX/"
        "Logan 2017 2160p WEB-DL DV HDR-FLUX.mkv"
    )


def test_resolve_smb_video_selects_requested_episode_over_largest():
    xbmcvfs = sys.modules["xbmcvfs"]
    files = ["Spider-Noir.S01E01.mkv", "Spider-Noir.S01E05.mkv"]

    def fake_stat(path):
        stat = MagicMock()
        stat.st_size.return_value = 7_000 if "E05" in path else 6_000
        return stat

    seen = []
    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", return_value=([], files)
    ), patch.object(xbmcvfs, "Stat", side_effect=fake_stat):
        url = resolve_smb_video(
            "smb://host/Spider-Noir",
            monitor=_Monitor(),
            requested_episode=(1, 1),
            on_inventory=seen.append,
        )

    assert url == "smb://host/Spider-Noir/Spider-Noir.S01E01.mkv"
    assert seen[0].episodes == (1, 5)


def test_smb_inventory_accepts_requested_tuple_for_nested_pack():
    from resources.lib.nzbget_resolver import _smb_inventory

    xbmcvfs = sys.modules["xbmcvfs"]
    tree = {
        "smb://host/Spider-Noir": (["Season 01"], []),
        "smb://host/Spider-Noir/Season 01": (
            [],
            ["Spider-Noir.S01E05.mkv", "Spider-Noir.S01E01.mkv"],
        ),
    }

    def fake_listdir(path):
        return tree[path]

    def fake_stat(path):
        stat = MagicMock()
        stat.st_size.return_value = 7_000 if "E05" in path else 6_000
        return stat

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", side_effect=fake_listdir
    ), patch.object(xbmcvfs, "Stat", side_effect=fake_stat):
        inventory = _smb_inventory("smb://host/Spider-Noir", requested_episode=(1, 1))

    assert inventory.selected_path.endswith("S01E01.mkv")
    assert inventory.episodes == (1, 5)


def test_smb_candidate_inventory_probes_directory_urls_with_trailing_slash():
    from resources.lib.nzbget_resolver import _smb_video_candidates_in_tree

    xbmcvfs = sys.modules["xbmcvfs"]
    probed = []
    tree = {
        "smb://host/Show": (["nested"], []),
        "smb://host/Show/nested": ([], ["Show.S01E01.mkv"]),
    }

    def fake_exists(path):
        probed.append(path)
        return path.endswith("/")

    with patch.object(xbmcvfs, "exists", side_effect=fake_exists), patch.object(
        xbmcvfs, "listdir", side_effect=lambda path: tree[path]
    ), patch.object(xbmcvfs, "Stat", MagicMock()):
        rows = _smb_video_candidates_in_tree("smb://host/Show")

    assert rows and rows[0][0].endswith("Show.S01E01.mkv")
    assert probed == ["smb://host/Show/", "smb://host/Show/nested/"]


def test_resolve_smb_video_does_not_play_named_wrong_episode():
    xbmcvfs = sys.modules["xbmcvfs"]
    seen = []
    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", return_value=([], ["Show.S01E05.mkv"])
    ), patch.object(xbmcvfs, "Stat", MagicMock()):
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(),
            budget=0,
            requested_episode=(1, 1),
            on_inventory=seen.append,
        )

    assert url is None
    assert len(seen) == 1
    assert seen[0].has_tagged_files is True


def test_resolve_smb_video_waits_for_requested_episode_after_wrong_sibling():
    xbmcvfs = sys.modules["xbmcvfs"]
    listings = [
        ([], ["Show.S01E05.mkv"]),
        ([], ["Show.S01E05.mkv", "Show.S01E01.mkv"]),
    ]
    seen = []
    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", side_effect=listings
    ), patch.object(xbmcvfs, "Stat", MagicMock()):
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(),
            requested_episode=(1, 1),
            on_inventory=seen.append,
        )

    assert url == "smb://host/Show/Show.S01E01.mkv"
    assert len(seen) == 1
    assert seen[0].selected_path == url


def test_resolve_smb_video_waits_for_exact_after_multiple_generic_files():
    xbmcvfs = sys.modules["xbmcvfs"]
    listings = [
        ([], ["video-a.mkv", "video-b.mkv"]),
        ([], ["video-a.mkv", "video-b.mkv", "Show.S01E01.mkv"]),
    ]
    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", side_effect=listings
    ), patch.object(xbmcvfs, "Stat", MagicMock()):
        url = resolve_smb_video(
            "smb://host/Show", monitor=_Monitor(), requested_episode=(1, 1)
        )

    assert url == "smb://host/Show/Show.S01E01.mkv"


def test_resolve_smb_video_explicit_episode_rejects_multiple_generic_videos():
    """Exact episode playback must not guess among an untagged multi-file job."""
    xbmcvfs = sys.modules["xbmcvfs"]

    def fake_stat(path):
        stat = MagicMock()
        stat.st_size.return_value = 9_000 if path.endswith("video-large.mkv") else 4_000
        return stat

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs,
        "listdir",
        return_value=([], ["video-small.mkv", "video-large.mkv"]),
    ), patch.object(xbmcvfs, "Stat", side_effect=fake_stat):
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(),
            requested_episode=(1, 1),
        )

    assert url is None


def test_resolve_smb_video_requested_episode_allows_one_generic_file():
    xbmcvfs = sys.modules["xbmcvfs"]

    def fake_stat(path):
        stat = MagicMock()
        stat.st_size.return_value = 9_000
        return stat

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", return_value=([], ["video.mkv"])
    ), patch.object(xbmcvfs, "Stat", side_effect=fake_stat):
        url = resolve_smb_video(
            "smb://host/Show", monitor=_Monitor(), requested_episode=(1, 1)
        )

    assert url == "smb://host/Show/video.mkv"


def test_resolve_smb_video_without_episode_keeps_legacy_largest_selection():
    xbmcvfs = sys.modules["xbmcvfs"]

    def fake_stat(path):
        stat = MagicMock()
        stat.st_size.return_value = 9_000 if "E05" in path else 5_000
        return stat

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs,
        "listdir",
        return_value=([], ["Show.S01E01.mkv", "Show.S01E05.mkv"]),
    ), patch.object(xbmcvfs, "Stat", side_effect=fake_stat):
        url = resolve_smb_video("smb://host/Show", monitor=_Monitor())

    assert url == "smb://host/Show/Show.S01E05.mkv"


def test_resolve_smb_video_retries_transient_list_error_without_empty_callback():
    xbmcvfs = sys.modules["xbmcvfs"]
    seen = []
    listdir = MagicMock(
        side_effect=[OSError("share settling"), ([], ["Show.S01E01.mkv"])]
    )
    stat = MagicMock()
    stat.st_size.return_value = 5_000
    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", listdir
    ), patch.object(xbmcvfs, "Stat", return_value=stat):
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(),
            requested_episode=(1, 1),
            on_inventory=seen.append,
        )

    assert url == "smb://host/Show/Show.S01E01.mkv"
    assert listdir.call_count == 2
    assert len(seen) == 1
    assert seen[0].files


def _stat_9000(path):  # pylint: disable=unused-argument
    stat = MagicMock()
    stat.st_size.return_value = 9_000
    return stat


def test_resolve_smb_video_waits_until_selection_is_readable():
    xbmcvfs = sys.modules["xbmcvfs"]
    reads = [b"", b"", b"\x00" * 16]

    class _SettlingFile:  # pylint: disable=too-few-public-methods
        def __init__(self, path, *args):
            self.path = path

        def readBytes(self, _num):
            return reads.pop(0)

        def close(self):
            pass

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", return_value=([], ["movie.mkv"])
    ), patch.object(xbmcvfs, "Stat", side_effect=_stat_9000), patch.object(
        xbmcvfs, "File", _SettlingFile
    ):
        url = resolve_smb_video(
            "smb://host/completed/The.Movie", monitor=_Monitor(), interval=0
        )

    assert url == "smb://host/completed/The.Movie/movie.mkv"
    assert not reads  # two unreadable probes were retried, third succeeded


def test_resolve_smb_video_unreadable_selection_fails_with_restart_hint():
    from resources.lib import nzbget_resolver

    xbmcvfs = sys.modules["xbmcvfs"]

    class _DeniedFile:  # pylint: disable=too-few-public-methods
        def __init__(self, path, *args):
            self.path = path

        def readBytes(self, _num):
            return b""

        def close(self):
            pass

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", return_value=([], ["movie.mkv"])
    ), patch.object(xbmcvfs, "Stat", side_effect=_stat_9000), patch.object(
        xbmcvfs, "File", _DeniedFile
    ), patch(
        "resources.lib.nzbget_resolver._notify"
    ) as notify:
        url = resolve_smb_video(
            "smb://host/completed/The.Movie",
            monitor=_Monitor(),
            interval=0,
            budget=0.0,
        )

    assert url is nzbget_resolver.SMB_UNREADABLE
    assert not url  # falsy: unaware truth-testing callers still see a miss
    notify.assert_called_once_with(
        nzbget_resolver._addon_name(), nzbget_resolver._string(30366), 7000
    )


def test_resolve_smb_video_unreadable_logs_redact_smb_credentials():
    xbmcvfs = sys.modules["xbmcvfs"]

    class _DeniedFile:  # pylint: disable=too-few-public-methods
        def __init__(self, path, *args):
            self.path = path

        def readBytes(self, _num):
            return b""

        def close(self):
            pass

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", return_value=([], ["movie.mkv"])
    ), patch.object(xbmcvfs, "Stat", side_effect=_stat_9000), patch.object(
        xbmcvfs, "File", _DeniedFile
    ), patch(
        "resources.lib.nzbget_resolver._notify"
    ), patch(
        "resources.lib.nzbget_resolver.xbmc"
    ) as kodi:
        url = resolve_smb_video(
            "smb://user:hunter2@host/completed/The.Movie",
            monitor=_Monitor(),
            interval=0,
            budget=0.0,
        )

    assert not url
    logged = " ".join(str(call.args[0]) for call in kodi.log.call_args_list)
    assert "hunter2" not in logged  # both the waiting and deadline logs redact
    assert "REDACTED" in logged


def test_resolve_smb_video_open_error_counts_as_unreadable():
    xbmcvfs = sys.modules["xbmcvfs"]

    def _raise(path, *args):
        raise OSError("open denied")

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", return_value=([], ["movie.mkv"])
    ), patch.object(xbmcvfs, "Stat", side_effect=_stat_9000), patch.object(
        xbmcvfs, "File", _raise
    ), patch(
        "resources.lib.nzbget_resolver._notify"
    ):
        url = resolve_smb_video(
            "smb://host/completed/The.Movie",
            monitor=_Monitor(),
            interval=0,
            budget=0.0,
        )

    from resources.lib.nzbget_resolver import SMB_UNREADABLE

    assert url is SMB_UNREADABLE


def test_resolve_smb_video_unreadable_does_not_record_inventory():
    # An unplayable completion must not enter the season-pack catalog: a
    # recorded row would shadow every later episode pick with the same
    # unreadable transient failure.
    xbmcvfs = sys.modules["xbmcvfs"]
    seen = []

    class _DeniedFile:  # pylint: disable=too-few-public-methods
        def __init__(self, path, *args):
            self.path = path

        def readBytes(self, _num):
            return b""

        def close(self):
            pass

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", return_value=([], ["Show.S01E01.mkv"])
    ), patch.object(xbmcvfs, "Stat", side_effect=_stat_9000), patch.object(
        xbmcvfs, "File", _DeniedFile
    ), patch(
        "resources.lib.nzbget_resolver._notify"
    ):
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(),
            interval=0,
            budget=0.0,
            requested_episode=(1, 1),
            on_inventory=seen.append,
        )

    assert not url
    assert not seen


def test_resolve_smb_video_unreadable_then_cleaned_up_is_ordinary_miss():
    # A file that probed unreadable once but then vanished (concurrent
    # cleanup) must report an ordinary miss at the deadline -- not the
    # SMB_UNREADABLE sentinel, which would make the completed-reuse caller
    # fail closed instead of falling back to a fresh submit.
    xbmcvfs = sys.modules["xbmcvfs"]
    seen = []
    listings = iter([([], ["movie.mkv"])])

    def fake_listdir(_folder):
        try:
            return next(listings)
        except StopIteration:
            return ([], [])

    class _DeniedFile:  # pylint: disable=too-few-public-methods
        def __init__(self, path, *args):
            self.path = path

        def readBytes(self, _num):
            return b""

        def close(self):
            pass

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", side_effect=fake_listdir
    ), patch.object(xbmcvfs, "Stat", side_effect=_stat_9000), patch.object(
        xbmcvfs, "File", _DeniedFile
    ), patch(
        "resources.lib.nzbget_resolver._notify"
    ) as notify:
        url = resolve_smb_video(
            "smb://host/completed/The.Movie",
            monitor=_Monitor(aborts_after=10**9),
            interval=0,
            budget=0.05,
            on_inventory=seen.append,
        )

    assert url is None
    notify.assert_not_called()  # no restart hint for a vanished file
    assert len(seen) == 1 and seen[0].files == ()  # deadline miss reported


def test_resolve_smb_video_share_blip_keeps_unreadable_state():
    # A pack whose selection probed unreadable, followed by an INCOMPLETE
    # scan (share blip) until the deadline: the blip proves nothing, so the
    # resolve must still fail closed as unreadable and must NOT report the
    # stale complete inventory -- recording it would catalog an unplayable
    # pack that shadows future episode picks.
    from resources.lib.nzbget_resolver import SMB_UNREADABLE

    xbmcvfs = sys.modules["xbmcvfs"]
    seen = []
    listings = iter([([], ["Show.S01E01.mkv"])])

    def fake_listdir(_folder):
        try:
            return next(listings)
        except StopIteration as exc:
            raise OSError("share blip") from exc

    class _DeniedFile:  # pylint: disable=too-few-public-methods
        def __init__(self, path, *args):
            self.path = path

        def readBytes(self, _num):
            return b""

        def close(self):
            pass

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", side_effect=fake_listdir
    ), patch.object(xbmcvfs, "Stat", side_effect=_stat_9000), patch.object(
        xbmcvfs, "File", _DeniedFile
    ), patch(
        "resources.lib.nzbget_resolver._notify"
    ) as notify:
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(aborts_after=10**9),
            interval=0,
            budget=0.05,
            requested_episode=(1, 1),
            on_inventory=seen.append,
        )

    assert url is SMB_UNREADABLE
    assert not seen  # stale complete inventory never reaches the catalog
    notify.assert_called_once()  # restart hint fired


def test_resolve_smb_video_ambiguous_scan_keeps_unreadable_state():
    # After the selection probes unreadable, a second untagged video appears
    # (complete scan, selection fails closed as ambiguous) while the
    # unreadable file is still listed. The state must survive: fail closed
    # at the deadline, don't catalog the ambiguous inventory, and don't let
    # the reuse caller resubmit while the unreadable file still exists.
    from resources.lib.nzbget_resolver import SMB_UNREADABLE

    xbmcvfs = sys.modules["xbmcvfs"]
    seen = []
    listings = iter([([], ["movie.mkv"])])

    def fake_listdir(_folder):
        try:
            return next(listings)
        except StopIteration:
            return ([], ["movie.mkv", "other.mkv"])

    class _DeniedFile:  # pylint: disable=too-few-public-methods
        def __init__(self, path, *args):
            self.path = path

        def readBytes(self, _num):
            return b""

        def close(self):
            pass

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", side_effect=fake_listdir
    ), patch.object(xbmcvfs, "Stat", side_effect=_stat_9000), patch.object(
        xbmcvfs, "File", _DeniedFile
    ), patch(
        "resources.lib.nzbget_resolver._notify"
    ) as notify:
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(aborts_after=10**9),
            interval=0,
            budget=0.05,
            requested_episode=(1, 1),
            on_inventory=seen.append,
        )

    assert url is SMB_UNREADABLE
    assert not seen  # ambiguous inventory never reaches the catalog
    notify.assert_called_once()  # restart hint fired


def test_resolve_smb_video_does_not_report_unreachable_empty_inventory():
    xbmcvfs = sys.modules["xbmcvfs"]
    seen = []
    with patch.object(xbmcvfs, "exists", return_value=False), patch.object(
        xbmcvfs, "listdir", return_value=([], [])
    ):
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(),
            budget=0,
            on_inventory=seen.append,
        )

    assert url is None
    assert not seen


def test_resolve_smb_video_reports_reachable_empty_inventory_at_deadline():
    xbmcvfs = sys.modules["xbmcvfs"]
    seen = []
    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", return_value=([], [])
    ):
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(),
            budget=0,
            on_inventory=seen.append,
        )

    assert url is None
    assert len(seen) == 1
    assert seen[0].files == ()


def test_resolve_smb_video_plays_visible_exact_episode_from_partial_tree():
    xbmcvfs = sys.modules["xbmcvfs"]

    def listdir(path):
        if path == "smb://host/Show":
            return ["unreadable"], ["Show.S01E01.mkv"]
        raise OSError("child unavailable")

    seen = []
    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", side_effect=listdir
    ), patch.object(xbmcvfs, "Stat", MagicMock()):
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(),
            budget=0,
            requested_episode=(1, 1),
            on_inventory=seen.append,
        )

    assert url == "smb://host/Show/Show.S01E01.mkv"
    assert not seen


def test_resolve_smb_video_rejects_generic_explicit_fallback_from_partial_tree():
    xbmcvfs = sys.modules["xbmcvfs"]

    def listdir(path):
        if path == "smb://host/Show":
            return ["unreadable"], ["video.mkv"]
        raise OSError("child unavailable")

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", side_effect=listdir
    ), patch.object(xbmcvfs, "Stat", MagicMock()):
        url = resolve_smb_video(
            "smb://host/Show",
            monitor=_Monitor(),
            budget=0,
            requested_episode=(1, 1),
        )

    assert url is None


def test_smb_inventory_rejects_partial_tree_even_with_visible_exact_episode():
    from resources.lib.nzbget_resolver import _smb_inventory

    xbmcvfs = sys.modules["xbmcvfs"]

    def listdir(path):
        if path == "smb://host/Show":
            return ["unreadable"], ["Show.S01E01.mkv"]
        raise OSError("child unavailable")

    with patch.object(xbmcvfs, "exists", return_value=True), patch.object(
        xbmcvfs, "listdir", side_effect=listdir
    ), patch.object(xbmcvfs, "Stat", MagicMock()):
        inventory = _smb_inventory("smb://host/Show", requested_episode=(1, 1))

    assert inventory is None


def test_resolve_completed_smb_forwards_episode_and_inventory_callback():
    from resources.lib.nzbget_resolver import _resolve_completed_smb, _SubmitCtx

    callback = MagicMock()
    ctx = _SubmitCtx("smb://host/completed", "", "/downloads", None, 1, 60)
    with patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/Show/Show.S01E01.mkv",
    ) as resolve:
        result = _resolve_completed_smb(
            "/downloads/Show",
            ctx,
            requested_episode=(1, 1),
            on_inventory=callback,
        )

    assert result.endswith("Show.S01E01.mkv")
    assert resolve.call_args.kwargs["requested_episode"] == (1, 1)
    assert resolve.call_args.kwargs["on_inventory"] is callback


def test_reuse_completed_job_forwards_episode_and_inventory_callback():
    from resources.lib.nzbget_resolver import _reuse_completed_job, _SubmitCtx

    callback = MagicMock()
    ctx = _SubmitCtx("smb://host/completed", "", "/downloads", None, 1, 60)
    with patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/Show/Show.S01E01.mkv",
    ) as resolve:
        result = _reuse_completed_job(
            {"dest_dir": "/downloads/Show"},
            ctx,
            requested_episode=(1, 1),
            on_inventory=callback,
        )

    assert result.endswith("Show.S01E01.mkv")
    assert resolve.call_args.kwargs["requested_episode"] == (1, 1)
    assert resolve.call_args.kwargs["on_inventory"] is callback


def test_reuse_completed_job_records_backend_native_dest_dir():
    from resources.lib.episode_inventory import build_video_inventory
    from resources.lib.nzbget_resolver import _reuse_completed_job, _SubmitCtx

    context = {
        "type": "episode",
        "title": "Show",
        "season": 1,
        "episode": 1,
    }
    inventory = build_video_inventory(
        [("smb://host/Show.S01E01.mkv", 1), ("smb://host/Show.S01E02.mkv", 2)],
        requested=(1, 1),
    )
    completed = {
        "nzbid": 77,
        "name": "Show.S01",
        "dest_dir": "/downloads/tv/Show.S01",
    }
    ctx = _SubmitCtx("smb://host/completed", "tv", "/downloads", None, 1, 60)
    ctx.episode_context = context

    def resolve(_folder, **kwargs):
        kwargs["on_inventory"](inventory)
        return "smb://host/Show.S01E01.mkv"

    with patch(
        "resources.lib.nzbget_resolver.resolve_smb_video", side_effect=resolve
    ), patch("resources.lib.season_pack_recording.season_pack.upsert") as upsert:
        result = _reuse_completed_job(
            completed,
            ctx,
        )

    assert result.endswith("Show.S01E01.mkv")
    assert upsert.call_args.args[0]["folder"] == completed["dest_dir"]


def test_resolve_smb_video_descends_into_subdirectory():
    # Common archive layout: the release folder holds only a nested
    # subdirectory, and the video lives inside it. The resolver must descend
    # rather than fail with "No video file found on SMB share".
    xbmcvfs = sys.modules["xbmcvfs"]
    tree = {
        "smb://host/completed/The.Movie": (["The.Movie"], ["readme.nfo"]),
        "smb://host/completed/The.Movie/The.Movie": ([], ["movie.mkv"]),
    }

    def fake_listdir(path):
        return tree.get(path.rstrip("/"), ([], []))

    def fake_stat(path):
        st = MagicMock()
        st.st_size.return_value = 9000 if path.endswith("movie.mkv") else 10
        return st

    with patch.object(xbmcvfs, "listdir", side_effect=fake_listdir), patch.object(
        xbmcvfs, "Stat", side_effect=fake_stat
    ):
        url = resolve_smb_video("smb://host/completed/The.Movie", monitor=_Monitor())
    assert url == "smb://host/completed/The.Movie/The.Movie/movie.mkv"


def test_resolve_smb_video_keeps_retrying_past_legacy_budget():
    # Regression: NZBGet reports SUCCESS but the moved files take longer than
    # the old ~4s (5×1s) window to become listable over SMB. The resolver must
    # keep retrying within the wider post-success budget instead of giving up
    # with "No video file found on SMB share".
    xbmcvfs = sys.modules["xbmcvfs"]
    calls = {"n": 0}

    def fake_listdir(path):
        calls["n"] += 1
        # File becomes visible only on the 8th listing — past the legacy
        # 5-attempt cap, within the wider budget.
        if calls["n"] >= 8:
            return ([], ["movie.mkv"])
        return ([], [])

    def fake_stat(path):
        st = MagicMock()
        st.st_size.return_value = 9000
        return st

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]

    with patch.object(xbmcvfs, "listdir", side_effect=fake_listdir), patch.object(
        xbmcvfs, "Stat", side_effect=fake_stat
    ), patch(
        "resources.lib.nzbget_resolver.time.monotonic", side_effect=fake_monotonic
    ):
        url = resolve_smb_video("smb://host/completed/The.Movie", monitor=_Monitor())
    assert url == "smb://host/completed/The.Movie/movie.mkv"
    assert calls["n"] >= 8


def test_resolve_smb_video_drives_progress_dialog_during_wait():
    # The user must see a progress bar while the file settles onto the share,
    # not get dropped back to the home screen. With a dialog supplied, the
    # resolve loop drives dialog.update over the wait window.
    xbmcvfs = sys.modules["xbmcvfs"]
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    with patch.object(xbmcvfs, "listdir", return_value=([], [])), patch.object(
        xbmcvfs, "Stat", MagicMock()
    ), patch(
        "resources.lib.nzbget_resolver.time.monotonic", side_effect=fake_monotonic
    ):
        result = resolve_smb_video(
            "smb://host/x", monitor=_Monitor(), dialog=dialog, interval=1, budget=5
        )
    assert result is None
    dialog.update.assert_called()  # progress bar driven during the wait
    # percent is a clamped 0..100 int
    pct = dialog.update.call_args[0][0]
    assert isinstance(pct, int) and 0 <= pct <= 100


def test_resolve_smb_video_honors_dialog_cancel():
    # Canceling the progress dialog aborts the SMB wait immediately rather than
    # spinning out the whole budget.
    xbmcvfs = sys.modules["xbmcvfs"]
    listdir = MagicMock(return_value=([], []))
    dialog = MagicMock()
    dialog.iscanceled.return_value = True
    with patch.object(xbmcvfs, "listdir", listdir), patch.object(
        xbmcvfs, "Stat", MagicMock()
    ):
        result = resolve_smb_video(
            "smb://host/x", monitor=_Monitor(), dialog=dialog, budget=60
        )
    assert result is None
    assert listdir.call_count == 1  # bailed on cancel after the first listing


def test_resolve_smb_video_returns_none_when_no_video():
    xbmcvfs = sys.modules["xbmcvfs"]
    # Inject a fast monitor (never aborts, never sleeps) so retry exhaustion
    # does not wait ~4 real seconds via the conftest waitForAbort sleep.
    with patch.object(
        xbmcvfs, "listdir", return_value=([], ["readme.txt"])
    ), patch.object(xbmcvfs, "Stat", MagicMock()):
        result = resolve_smb_video("smb://host/completed/X", monitor=_Monitor())
    assert result is None


def test_resolve_smb_video_aborts_during_retry():
    xbmcvfs = sys.modules["xbmcvfs"]
    listdir = MagicMock(return_value=([], ["readme.txt"]))
    with patch.object(xbmcvfs, "listdir", listdir), patch.object(
        xbmcvfs, "Stat", MagicMock()
    ):
        # Monitor aborts on the first waitForAbort -> early return after a
        # single listing, not all _SMB_LIST_RETRIES attempts.
        result = resolve_smb_video(
            "smb://host/completed/X", monitor=_Monitor(aborts_after=1)
        )
    assert result is None
    assert listdir.call_count == 1


def test_poll_returns_success_with_dest_dir():
    def getter(k, d=""):
        return {"nzbget_url": "http://box"}.get(k, d)

    group_seq = [
        {"present": True, "status": "DOWNLOADING", "percent": 50},
        {"present": False, "status": "", "percent": 0},
    ]
    hist = {
        "present": True,
        "success": True,
        "status": "SUCCESS/ALL",
        "dest_dir": "/dl/movies/X",
        "nzbid": 42,
        "job_name": "X",
    }
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.group_status",
        side_effect=group_seq,
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.history_status",
        return_value=hist,
    ):
        result = poll_nzbget_job(
            42, _Dialog(), _Monitor(), timeout=60, settings_getter=getter
        )
    assert result["outcome"] == "success"
    assert result["dest_dir"] == "/dl/movies/X"
    assert result["nzbid"] == 42
    assert result["job_name"] == "X"


def test_poll_promoted_success_returns_promoted_id_not_original_pick():
    state = {
        "current": None,
        "promotion_deadline": 999,
        "exclude": 41,
        "stale_successes": (),
        "paused_nzbids": (),
    }
    completed = {
        "present": True,
        "nzbid": 42,
        "job_name": "same name",
        "dest_dir": "/dl/tv/promoted",
    }
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.history_success_by_dupekey",
        return_value=completed,
    ):
        result = _tick_group_follow(state, _Dialog(), None, "dupe-key", None)

    assert result == {
        "outcome": "success",
        "nzbid": 42,
        "job_name": "same name",
        "dest_dir": "/dl/tv/promoted",
    }


def test_poll_returns_failed_on_history_failure():
    def getter(k, d=""):
        return {"nzbget_url": "http://box"}.get(k, d)

    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.group_status",
        return_value={"present": False, "status": "", "percent": 0},
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.history_status",
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/UNPACK",
            "dest_dir": "",
        },
    ):
        result = poll_nzbget_job(
            42, _Dialog(), _Monitor(), timeout=60, settings_getter=getter
        )
    assert result["outcome"] == "failed"


def test_poll_returns_canceled_when_dialog_canceled():
    def getter(k, d=""):
        return {"nzbget_url": "http://box"}.get(k, d)

    dialog = _Dialog()
    dialog.canceled = True
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.group_status",
        return_value={"present": True, "status": "DOWNLOADING", "percent": 10},
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.history_status",
        return_value={"present": False, "success": False, "status": "", "dest_dir": ""},
    ):
        result = poll_nzbget_job(
            42, dialog, _Monitor(), timeout=60, settings_getter=getter
        )
    assert result["outcome"] == "canceled"


def test_poll_returns_aborted_when_monitor_aborts():
    def getter(k, d=""):
        return {"nzbget_url": "http://box"}.get(k, d)

    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.group_status",
        return_value={"present": True, "status": "DOWNLOADING", "percent": 10},
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.history_status",
        return_value={"present": False, "success": False, "status": "", "dest_dir": ""},
    ):
        result = poll_nzbget_job(
            42, _Dialog(), _Monitor(aborts_after=1), timeout=60, settings_getter=getter
        )
    assert result["outcome"] == "aborted"


def test_poll_returns_timeout_when_budget_exhausted():
    def getter(k, d=""):
        return {"nzbget_url": "http://box"}.get(k, d)

    # Monitor never aborts; the wall clock advances past the budget so the
    # real timeout branch fires (not aborted). Drive monotonic with a
    # controlled clock that ticks 1s per read so the deadline is reached
    # deterministically regardless of harness timing.
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]

    with patch(
        "resources.lib.nzbget_resolver.time.monotonic", side_effect=fake_monotonic
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.group_status",
        return_value={"present": True, "status": "DOWNLOADING", "percent": 10},
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.history_status",
        return_value={"present": False, "success": False, "status": "", "dest_dir": ""},
    ):
        result = poll_nzbget_job(
            42, _Dialog(), _Monitor(), timeout=2, settings_getter=getter
        )
    assert result["outcome"] == "timeout"


def test_poll_timeout_is_wall_clock_not_per_tick_accumulation():
    # Regression for the wall-clock-blind timeout: the budget must track real
    # elapsed time (time.monotonic), so a slow box whose RPCs consume far more
    # than `interval` per tick can't stretch the configured timeout. Here each
    # tick "costs" 30s of wall time but the interval arg is only 2s; a
    # per-tick accumulator (+= interval) would allow ~30 ticks for timeout=60,
    # while a monotonic deadline allows ~2.
    def getter(k, d=""):
        return {"nzbget_url": "http://box"}.get(k, d)

    clock = {"t": 1000.0}

    def fake_monotonic():
        return clock["t"]

    ticks = {"n": 0}

    def group(nzbid, settings_getter=None):
        ticks["n"] += 1
        clock["t"] += 30.0  # each RPC tick burns 30s of wall time
        return {"present": True, "status": "DOWNLOADING", "percent": 10}

    with patch(
        "resources.lib.nzbget_resolver.time.monotonic", side_effect=fake_monotonic
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.group_status",
        side_effect=group,
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.history_status",
        return_value={"present": False, "success": False, "status": "", "dest_dir": ""},
    ):
        result = poll_nzbget_job(
            42, _Dialog(), _Monitor(), timeout=60, settings_getter=getter, interval=2
        )
    assert result["outcome"] == "timeout"
    # With a 60s budget and 30s burned per tick, the deadline is reached after
    # ~2 ticks — NOT the ~30 a per-interval (2s) accumulator would have run.
    assert ticks["n"] <= 3


def test_poll_post_processing_status_shows_pp_label():
    # An in-queue post-processing stage (e.g. UNPACKING) is reported by
    # NZBGet as a bare status while the group is still present; it must show
    # "Post-processing..." (30219), not a frozen "Downloading... 100%".
    def getter(k, d=""):
        return {"nzbget_url": "http://box"}.get(k, d)

    group_seq = [
        {"present": True, "status": "UNPACKING", "percent": 100},
        {"present": False, "status": "", "percent": 0},
    ]
    dialog = _Dialog()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.group_status",
        side_effect=group_seq,
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.history_status",
        return_value={
            "present": True,
            "success": True,
            "status": "SUCCESS/ALL",
            "dest_dir": "/dl/movies/X",
        },
    ):
        result = poll_nzbget_job(
            42, dialog, _Monitor(), timeout=60, settings_getter=getter
        )
    assert result["outcome"] == "success"
    # The UNPACKING tick must have used the post-processing string (30219),
    # not the "Downloading... {}%" template (30105).
    pp_messages = [msg for _pct, msg in dialog.lines if msg]
    assert any("30219" in m or "Post-processing" in m for m in pp_messages)
    assert not any("30105" in m or "Downloading" in m for m in pp_messages)


def test_poll_honors_custom_interval():
    # The configured poll_interval must drive waitForAbort, not a hardcoded
    # cadence. With a tiny timeout the loop exhausts after one tick; assert
    # the monitor saw the custom interval.
    def getter(k, d=""):
        return {"nzbget_url": "http://box"}.get(k, d)

    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    clock = {"t": 0.0}

    def fake_monotonic():
        # Advance one tick per read: start, one loop body, then past the
        # deadline. Guarantees exactly one polling iteration so the interval
        # assertion can't pass vacuously on a zero-iteration loop.
        clock["t"] += 1.0
        return clock["t"]

    with patch(
        "resources.lib.nzbget_resolver.time.monotonic", side_effect=fake_monotonic
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.group_status",
        return_value={"present": True, "status": "DOWNLOADING", "percent": 10},
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.history_status",
        return_value={"present": False, "success": False, "status": "", "dest_dir": ""},
    ):
        poll_nzbget_job(
            42, _Dialog(), monitor, timeout=2, settings_getter=getter, interval=7
        )
    # The loop ran at least once and every wait used the configured interval.
    monitor.waitForAbort.assert_called()
    assert all(call[0][0] == 7 for call in monitor.waitForAbort.call_args_list)


def test_resolve_missing_config_resolves_false():
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    xbmc_mod = sys.modules["xbmc"]
    xbmc_mod.PlayList = MagicMock()
    resolve_and_play_nzbget(
        7,
        {"nzburl": "http://i/x.nzb", "title": "X"},
        settings_getter=_settings({}),
    )
    plugin.setResolvedUrl.assert_called_once()
    assert plugin.setResolvedUrl.call_args[0][1] is False
    # Failure path must clear the video playlist (the v0.6.8 retry-loop
    # guard) to match resolver.resolve()'s failure contract.
    xbmc_mod.PlayList.assert_called_with(xbmc_mod.PLAYLIST_VIDEO)
    xbmc_mod.PlayList.return_value.clear.assert_called()


def test_resolve_failed_outcome_clears_playlist():
    # A failed NZBGet download (history FAILURE) must also clear the
    # playlist on the handle path, not just the missing-config exit.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    xbmc_mod = sys.modules["xbmc"]
    xbmc_mod.PlayList = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "failed", "status": "FAILURE/UNPACK"},
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "X"},
            settings_getter=_full_settings(),
        )
    assert plugin.setResolvedUrl.call_args[0][1] is False
    xbmc_mod.PlayList.return_value.clear.assert_called()


def test_resolve_success_resolves_true_with_smb_url():
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "The.Movie"},
            settings_getter=_full_settings(),
        )
    plugin.setResolvedUrl.assert_called_once()
    assert plugin.setResolvedUrl.call_args[0][1] is True


def test_resolve_episode_threads_exact_request_to_smb_selection():
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/tv/Spider-Noir"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/Spider-Noir/Spider-Noir.S01E01.mkv",
    ) as resolve_smb:
        resolve_and_play_nzbget(
            7,
            {
                "nzburl": "http://i/pack.nzb",
                "title": "Spider-Noir.S01.2160p",
                "_episode_context": {
                    "type": "episode",
                    "title": "Spider-Noir",
                    "imdb": "tt1234567",
                    "tvdb": "451234",
                    "tmdb_id": "987",
                    "season": 1,
                    "episode": 1,
                },
            },
            settings_getter=_full_settings(),
        )

    assert resolve_smb.call_args.kwargs["requested_episode"] == (1, 1)


def test_resolve_movie_leaves_smb_selection_without_episode_request():
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ) as resolve_smb:
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/movie.nzb", "title": "The.Movie"},
            settings_getter=_full_settings(),
        )

    assert resolve_smb.call_args.kwargs.get("requested_episode") is None


def test_nzbget_completion_preserves_full_context_until_smb_boundary():
    from types import SimpleNamespace

    from resources.lib.nzbget_resolver import _play_completed_download

    episode_context = {
        "type": "episode",
        "title": "Spider-Noir",
        "imdb": "tt1234567",
        "tvdb": "451234",
        "tmdb_id": "987",
        "season": 1,
        "episode": 1,
    }
    ctx = SimpleNamespace(
        smb_root="smb://host/completed",
        category="tv",
        completed_base="/downloads",
        dialog=None,
        interval=1,
        on_failure=MagicMock(),
        on_success=MagicMock(),
        dupe=None,
        episode_context=episode_context,
    )
    with patch("resources.lib.nzbget_resolver.record_download"), patch(
        "resources.lib.nzbget_resolver._resolve_completed_smb",
        return_value="smb://host/completed/Spider-Noir.S01E01.mkv",
    ) as resolve_completed:
        _play_completed_download(ctx, "/downloads/show", "pack", None, None)

    assert resolve_completed.call_args.args[1].episode_context == episode_context


def test_nzbget_completion_records_exact_nzbid_and_backend_native_folder():
    from types import SimpleNamespace

    from resources.lib.episode_inventory import build_video_inventory
    from resources.lib.nzbget_resolver import _play_completed_download

    context = {
        "type": "episode",
        "title": "Spider-Noir",
        "imdb": "tt123",
        "tvdb": "456",
        "tmdb_id": "789",
        "season": 1,
        "episode": 1,
    }
    inventory = build_video_inventory(
        [
            ("smb://host/completed/tv/Spider/Spider.S01E01.mkv", 6000),
            ("smb://host/completed/tv/Spider/Spider.S01E02.mkv", 7000),
        ],
        requested=(1, 1),
    )
    ctx = SimpleNamespace(
        smb_root="smb://host/completed",
        category="tv",
        completed_base="/downloads",
        dialog=None,
        interval=0,
        on_failure=MagicMock(),
        on_success=MagicMock(),
        dupe=None,
        episode_context=context,
    )

    def resolve_folder(_folder, **kwargs):
        kwargs["on_inventory"](inventory)
        return "smb://host/completed/tv/Spider/Spider.S01E01.mkv"

    with patch("resources.lib.nzbget_resolver.record_download"), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        side_effect=resolve_folder,
    ), patch("resources.lib.season_pack_recording.season_pack.upsert") as upsert:
        _play_completed_download(
            ctx,
            "/downloads/tv/Spider",
            "Spider-Noir.S01.2160p",
            None,
            None,
            job_id=42,
            job_name="same name",
        )

    record = upsert.call_args.args[0]
    assert (record["backend"], record["job_id"], record["job_name"]) == (
        "nzbget",
        "42",
        "same name",
    )
    assert record["folder"] == "/downloads/tv/Spider"
    ctx.on_success.assert_called_once()


def test_submit_flow_records_promoted_result_id_instead_of_original_pick():
    import threading
    from types import SimpleNamespace

    from resources.lib.nzbget_resolver import _submit_poll_resolve

    ctx = SimpleNamespace(
        settings_getter=None,
        dupe=None,
        dialog=_Dialog(),
        timeout=60,
        interval=0,
        cancel_event=threading.Event(),
        submitted_nzbids=[],
        on_failure=MagicMock(),
        episode_context={"type": "episode", "title": "Show", "season": 1},
    )
    with patch(
        "resources.lib.nzbget_resolver._submit_pick", return_value=(41, None)
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={
            "outcome": "success",
            "dest_dir": "/downloads/tv/promoted",
            "nzbid": 42,
            "job_name": "promoted release",
        },
    ), patch(
        "resources.lib.nzbget_resolver._play_completed_download"
    ) as play_completed:
        _submit_poll_resolve(ctx, "http://i/pick.nzb", "same name", None, None)

    assert play_completed.call_args.kwargs == {
        "job_id": 42,
        "job_name": "promoted release",
    }
    assert play_completed.call_args.args[1] == "/downloads/tv/promoted"


def test_smb_boundary_converts_full_context_to_requested_episode():
    from resources.lib.nzbget_resolver import _resolve_completed_smb, _SubmitCtx

    context = {
        "type": "episode",
        "title": "Spider-Noir",
        "season": 1,
        "episode": 1,
    }
    ctx = _SubmitCtx("smb://host/completed", "tv", "/downloads", None, 1, 60)
    ctx.episode_context = context
    with patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/Spider-Noir.S01E01.mkv",
    ) as resolve_smb:
        _resolve_completed_smb(
            "/downloads/show",
            ctx,
        )

    assert resolve_smb.call_args.kwargs["requested_episode"] == (1, 1)


def test_resolve_success_applies_resume_offset_to_listitem():
    # The resume position carried from the scrubbed bookmark must be set on the
    # ListItem as StartOffset so a replay resumes instead of restarting.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    li = MagicMock()
    with patch.object(sys.modules["xbmcgui"], "ListItem", return_value=li), patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "The.Movie"},
            settings_getter=_full_settings(),
            resume_seconds=137.0,
        )
    li.setProperty.assert_called_with("StartOffset", "137.0")


def test_resolve_success_arms_playback_monitor_window_properties():
    # On success the resolver must hand the SMB session to the background
    # NzbdavPlayer monitor (gated on nzbdav.active="true") so a resume point is
    # actually saved/read for the NZBGet/SMB path — the persistence gap. Assert
    # all five monitor window properties, including the supplied resume_key.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    home = MagicMock()
    with patch.object(
        sys.modules["xbmcgui"], "Window", MagicMock(return_value=home)
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "The.Movie"},
            settings_getter=_full_settings(),
            resume_seconds=137.0,
            resume_key="The.Movie|123|pub",
        )
    home.setProperty.assert_any_call(
        "nzbdav.stream_url", "smb://host/completed/The.Movie/movie.mkv"
    )
    home.setProperty.assert_any_call("nzbdav.resume_key", "The.Movie|123|pub")
    home.setProperty.assert_any_call("nzbdav.resume_offset", "137.0")
    home.setProperty.assert_any_call("nzbdav.stream_title", "movie.mkv")
    home.setProperty.assert_any_call("nzbdav.active", "true")
    # setResolvedUrl contract unchanged: exactly one resolution, success True.
    plugin.setResolvedUrl.assert_called_once()
    assert plugin.setResolvedUrl.call_args[0][1] is True


def test_resolve_success_resume_key_falls_back_to_stream_url():
    # No release identity threaded (e.g. a bare script/widget play): the monitor
    # still keys resume on the playable SMB URL so the session is monitored.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    home = MagicMock()
    with patch.object(
        sys.modules["xbmcgui"], "Window", MagicMock(return_value=home)
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "The.Movie"},
            settings_getter=_full_settings(),
        )
    home.setProperty.assert_any_call(
        "nzbdav.resume_key", "smb://host/completed/The.Movie/movie.mkv"
    )
    home.setProperty.assert_any_call("nzbdav.active", "true")


def test_resolve_cancel_deletes_job_and_resolves_false():
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "canceled"},
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_jobs"
    ) as cancel:
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "X"},
            settings_getter=_full_settings(),
        )
    cancel.assert_called_once()
    assert cancel.call_args.args[0] == [42]  # id-scoped: just the pick
    assert plugin.setResolvedUrl.call_args[0][1] is False


def test_resolve_timeout_leaves_job_and_resolves_false():
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "timeout"},
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_job"
    ) as cancel:
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "X"},
            settings_getter=_full_settings(),
        )
    cancel.assert_not_called()
    assert plugin.setResolvedUrl.call_args[0][1] is False


def test_play_nzbget_success_starts_player_with_smb_url():
    player = MagicMock()
    with patch.object(
        sys.modules["xbmc"], "Player", MagicMock(return_value=player)
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        play_nzbget("http://i/x.nzb", "The.Movie", settings_getter=_full_settings())
    # Handle-less path: playback starts via xbmc.Player().play, never
    # setResolvedUrl.
    player.play.assert_called_once()
    assert player.play.call_args[0][0] == "smb://host/completed/The.Movie/movie.mkv"


def test_play_nzbget_success_arms_playback_monitor_and_applies_offset():
    # Handle-less path: the SMB session must also be handed to the background
    # monitor (all five window properties incl. nzbdav.active="true" and the
    # resume_key) and StartOffset applied, with NO setResolvedUrl call.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    player = MagicMock()
    li = MagicMock()
    home = MagicMock()
    with patch.object(
        sys.modules["xbmc"], "Player", MagicMock(return_value=player)
    ), patch.object(sys.modules["xbmcgui"], "ListItem", return_value=li), patch.object(
        sys.modules["xbmcgui"], "Window", MagicMock(return_value=home)
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        play_nzbget(
            "http://i/x.nzb",
            "The.Movie",
            settings_getter=_full_settings(),
            resume_seconds=137.0,
            resume_key="The.Movie|123|pub",
        )
    home.setProperty.assert_any_call(
        "nzbdav.stream_url", "smb://host/completed/The.Movie/movie.mkv"
    )
    home.setProperty.assert_any_call("nzbdav.resume_key", "The.Movie|123|pub")
    home.setProperty.assert_any_call("nzbdav.resume_offset", "137.0")
    home.setProperty.assert_any_call("nzbdav.stream_title", "movie.mkv")
    home.setProperty.assert_any_call("nzbdav.active", "true")
    li.setProperty.assert_called_with("StartOffset", "137.0")
    # Handle-less contract: playback via xbmc.Player().play, never setResolvedUrl.
    player.play.assert_called_once()
    plugin.setResolvedUrl.assert_not_called()


def test_play_nzbget_success_resume_key_falls_back_to_stream_url():
    # No release identity threaded: key resume on the playable SMB URL.
    player = MagicMock()
    home = MagicMock()
    with patch.object(
        sys.modules["xbmc"], "Player", MagicMock(return_value=player)
    ), patch.object(
        sys.modules["xbmcgui"], "Window", MagicMock(return_value=home)
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        play_nzbget("http://i/x.nzb", "The.Movie", settings_getter=_full_settings())
    home.setProperty.assert_any_call(
        "nzbdav.resume_key", "smb://host/completed/The.Movie/movie.mkv"
    )
    home.setProperty.assert_any_call("nzbdav.active", "true")
    player.play.assert_called_once()


def test_play_nzbget_missing_config_does_not_start_player():
    player = MagicMock()
    with patch.object(sys.modules["xbmc"], "Player", MagicMock(return_value=player)):
        play_nzbget("http://i/x.nzb", "X", settings_getter=_settings({}))
    player.play.assert_not_called()


def test_read_settings_none_uses_single_arg_getsetting():
    # Regression: real Kodi Addon.getSetting takes ONE positional id; the
    # settings_getter=None branch must not call it with a (key, default)
    # two-arg shape (which raises TypeError under real Kodi).
    values = {
        "nzbget_url": "http://box:6789",
        "nzbget_smb_root": "smb://host/completed",
        "download_timeout": "600",
    }

    def get_setting(key):  # one positional arg only — errors on a second
        return values.get(key, "")

    addon = MagicMock()
    addon.getSetting = MagicMock(side_effect=get_setting)
    with patch.object(sys.modules["xbmcaddon"], "Addon", return_value=addon):
        url, smb_root, timeout = _read_settings(None)
    assert url == "http://box:6789"
    assert smb_root == "smb://host/completed"
    assert timeout == 600


def test_read_settings_defaults_url_to_schema_default():
    # nzbget_url left at its schema default is absent from the profile XML, so
    # the injected getter returns the default we pass — which must be the
    # settings.xml default, not "" (else "not configured" on the widget path).
    def getter(key, default=""):
        return {"nzbget_smb_root": "smb://host/done"}.get(key, default)

    url, smb_root, _timeout = _read_settings(getter)
    assert url == "http://localhost:6789"
    assert smb_root == "smb://host/done"


def test_read_poll_interval_reads_and_clamps():
    # The shared poll_interval setting is honored and clamped to [1..60].
    assert _read_poll_interval(_settings({"poll_interval": "5"})) == 5
    assert _read_poll_interval(_settings({"poll_interval": "0"})) == 1
    assert _read_poll_interval(_settings({"poll_interval": "999"})) == 60
    # Missing / blank -> default of 1.
    assert _read_poll_interval(_settings({})) == 1


def test_resolve_success_with_category_includes_category_in_smb_target():
    # End-to-end: with a category configured, the SMB folder handed to
    # resolve_smb_video must include the category subfolder.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    sys.modules["xbmc"].PlayList = MagicMock()
    getter = _settings(
        {
            "nzbget_url": "http://box:6789",
            "nzbget_smb_root": "smb://host/completed",
            "download_timeout": "600",
            "nzbget_category": "movies",
        }
    )
    captured = {}

    def fake_resolve(folder, monitor=None, **_kwargs):
        captured["folder"] = folder
        return "{}/movie.mkv".format(folder)

    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={
            "outcome": "success",
            "dest_dir": "/downloads/completed/movies/The.Movie",
        },
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        side_effect=fake_resolve,
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "The.Movie"},
            settings_getter=getter,
        )
    assert captured["folder"] == "smb://host/completed/movies/The.Movie"
    assert plugin.setResolvedUrl.call_args[0][1] is True


def test_play_nzbget_failure_does_not_clear_playlist():
    # The handle-less path has no plugin handle and must NOT clear the
    # playlist (mirrors nzbdav resolve_and_play): only notify.
    player = MagicMock()
    xbmc_mod = sys.modules["xbmc"]
    xbmc_mod.PlayList = MagicMock()
    with patch.object(xbmc_mod, "Player", MagicMock(return_value=player)):
        play_nzbget("http://i/x.nzb", "X", settings_getter=_settings({}))
    player.play.assert_not_called()
    xbmc_mod.PlayList.return_value.clear.assert_not_called()


_PUBDATE = "Wed, 15 Dec 2021 12:00:00 +0000"


def test_resolve_success_records_download_pubdate_in_ledger():
    # The picker's NZBGet-mode DL tag uses the same download-ledger pubdate
    # gate as the nzbdav path, so a completed NZBGet download must record the
    # selected result's pubdate under the job title.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ), patch(
        "resources.lib.nzbget_resolver.record_download"
    ) as record:
        resolve_and_play_nzbget(
            7,
            {
                "nzburl": "http://i/x.nzb",
                "title": "The.Movie",
                "_download_pubdate": _PUBDATE,
                "_download_size": 123456,
            },
            settings_getter=_full_settings(),
        )
    record.assert_called_once_with("The.Movie", _PUBDATE, 123456)
    assert plugin.setResolvedUrl.call_args[0][1] is True


def test_resolve_records_ledger_even_when_smb_resolve_fails():
    # NZBGet completed the download (it IS in history as SUCCESS, so the
    # picker will tag it DL); a later SMB-mapping failure must not lose the
    # pubdate record.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video", return_value=None
    ), patch(
        "resources.lib.nzbget_resolver.record_download"
    ) as record:
        resolve_and_play_nzbget(
            7,
            {
                "nzburl": "http://i/x.nzb",
                "title": "The.Movie",
                "_download_pubdate": _PUBDATE,
            },
            settings_getter=_full_settings(),
        )
    record.assert_called_once_with("The.Movie", _PUBDATE, None)
    assert plugin.setResolvedUrl.call_args[0][1] is False


def test_resolve_failed_outcome_does_not_record_ledger():
    # A failed/dupe-deleted job downloaded nothing under this pubdate.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "failed", "status": "DELETED/DUPE"},
    ), patch(
        "resources.lib.nzbget_resolver.record_download"
    ) as record:
        resolve_and_play_nzbget(
            7,
            {
                "nzburl": "http://i/x.nzb",
                "title": "The.Movie",
                "_download_pubdate": _PUBDATE,
            },
            settings_getter=_full_settings(),
        )
    record.assert_not_called()
    assert plugin.setResolvedUrl.call_args[0][1] is False


def test_play_nzbget_records_ledger_from_params():
    # The handle-less picker/script path threads the selected result's
    # pubdate the same way.
    player = MagicMock()
    with patch.object(
        sys.modules["xbmc"], "Player", MagicMock(return_value=player)
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ), patch(
        "resources.lib.nzbget_resolver.record_download"
    ) as record:
        play_nzbget(
            "http://i/x.nzb",
            "The.Movie",
            params={"_download_pubdate": _PUBDATE, "_download_size": 9},
            settings_getter=_full_settings(),
        )
    record.assert_called_once_with("The.Movie", _PUBDATE, 9)
    player.play.assert_called_once()


_COMPLETED_JOB = {
    "name": "The.Movie",
    "status": "SUCCESS/UNPACK",
    "bytes": 60_000_000_000,
    "nzbid": 7,
    "dest_dir": "/dl/movies/The.Movie",
}


def test_resolve_reuses_completed_job_without_resubmitting():
    # A picker-corroborated SUCCESS history match plays the already-completed
    # files; re-submitting would hit NZBGet's duplicate check (DupeCheck=yes
    # default), get dupe-deleted, and fail the resolve.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    captured = {}

    def fake_resolve_smb(folder, **kwargs):
        captured["folder"] = folder
        captured["budget"] = kwargs.get("budget")
        return folder + "/movie.mkv"

    with patch("resources.lib.nzbget_resolver.nzbget_api.append_nzb") as append, patch(
        "resources.lib.nzbget_resolver.nzbget_api.completed_base_dir",
        return_value="/dl",
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        side_effect=fake_resolve_smb,
    ), patch(
        "resources.lib.nzbget_resolver.record_download"
    ) as record:
        resolve_and_play_nzbget(
            7,
            {
                "nzburl": "http://i/x.nzb",
                "title": "The.Movie",
                "_nzbget_completed_job": _COMPLETED_JOB,
            },
            settings_getter=_full_settings(),
        )

    append.assert_not_called()
    # Reuse is not a download: the ledger entry was written by the original
    # download, mirroring the nzbdav cache-hit no-record behavior.
    record.assert_not_called()
    # dest_dir mapped onto the SMB root relative to the completed base.
    assert captured["folder"] == "smb://host/completed/movies/The.Movie"
    # Short probe budget: stale rows must not delay the fallback submit.
    assert captured["budget"] is not None and captured["budget"] <= 10
    assert plugin.setResolvedUrl.call_args[0][1] is True


def test_resolve_reuse_probe_miss_falls_back_to_submit():
    # History row exists but the files are gone from the share (cleanup):
    # fall through to the normal submit flow.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    smb_results = iter([None, "smb://host/completed/The.Movie/movie.mkv"])

    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb",
        return_value=(42, None),
    ) as append, patch(
        "resources.lib.nzbget_resolver.nzbget_api.completed_base_dir",
        return_value="/dl",
    ), patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        side_effect=lambda *a, **k: next(smb_results),
    ):
        resolve_and_play_nzbget(
            7,
            {
                "nzburl": "http://i/x.nzb",
                "title": "The.Movie",
                "_nzbget_completed_job": _COMPLETED_JOB,
            },
            settings_getter=_full_settings(),
        )

    append.assert_called_once()
    assert plugin.setResolvedUrl.call_args[0][1] is True


def test_resolve_reuse_unreadable_fails_closed_without_resubmit():
    # Visible-but-unreadable completed files (stale Kodi SMB session): the
    # SUCCESS row must NOT be re-submitted -- NZBGet would dupe-delete the
    # re-submission and bury the restart-Kodi hint that already toasted.
    from resources.lib.nzbget_resolver import SMB_UNREADABLE

    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()

    with patch("resources.lib.nzbget_resolver.nzbget_api.append_nzb") as append, patch(
        "resources.lib.nzbget_resolver.nzbget_api.completed_base_dir",
        return_value="/dl",
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value=SMB_UNREADABLE,
    ), patch(
        "resources.lib.nzbget_resolver._notify"
    ) as notify:
        resolve_and_play_nzbget(
            7,
            {
                "nzburl": "http://i/x.nzb",
                "title": "The.Movie",
                "_nzbget_completed_job": _COMPLETED_JOB,
            },
            settings_getter=_full_settings(),
        )

    append.assert_not_called()
    assert plugin.setResolvedUrl.call_args[0][1] is False
    # No second toast: the unreadable warning fired inside resolve_smb_video
    # (patched out here), and the fail-closed path passes message=None.
    notify.assert_not_called()


def test_play_completed_download_unreadable_skips_no_video_toast():
    from resources.lib.nzbget_resolver import SMB_UNREADABLE, _play_completed_download

    ctx = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver._resolve_completed_smb",
        return_value=SMB_UNREADABLE,
    ), patch("resources.lib.nzbget_resolver.record_download"):
        _play_completed_download(ctx, "/dl/movies/The.Movie", "The.Movie", 0, 0)

    ctx.on_failure.assert_called_once_with(None)
    ctx.on_success.assert_not_called()


def test_play_nzbget_reuses_completed_job():
    # Handle-less picker/script path: same reuse, playback via xbmc.Player.
    player = MagicMock()
    with patch.object(
        sys.modules["xbmc"], "Player", MagicMock(return_value=player)
    ), patch("resources.lib.nzbget_resolver.nzbget_api.append_nzb") as append, patch(
        "resources.lib.nzbget_resolver.nzbget_api.completed_base_dir",
        return_value="/dl",
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/movies/The.Movie/movie.mkv",
    ):
        play_nzbget(
            "http://i/x.nzb",
            "The.Movie",
            params={"_nzbget_completed_job": _COMPLETED_JOB},
            settings_getter=_full_settings(),
        )
    append.assert_not_called()
    player.play.assert_called_once()
    assert (
        player.play.call_args[0][0] == "smb://host/completed/movies/The.Movie/movie.mkv"
    )


def test_resolve_reuse_applies_resume_offset():
    # Replaying a tagged result must resume where the user left off.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    li = MagicMock()
    with patch.object(sys.modules["xbmcgui"], "ListItem", return_value=li), patch(
        "resources.lib.nzbget_resolver.nzbget_api.append_nzb"
    ) as append, patch(
        "resources.lib.nzbget_resolver.nzbget_api.completed_base_dir",
        return_value="/dl",
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/movies/The.Movie/movie.mkv",
    ):
        resolve_and_play_nzbget(
            7,
            {
                "nzburl": "http://i/x.nzb",
                "title": "The.Movie",
                "_nzbget_completed_job": _COMPLETED_JOB,
            },
            settings_getter=_full_settings(),
            resume_seconds=137.0,
        )
    append.assert_not_called()
    li.setProperty.assert_called_with("StartOffset", "137.0")


# ---------------------------------------------------------------------------
# #372 NZBGet Smart Duplicates (DupeKey / DupeScore / DupeMode)
# ---------------------------------------------------------------------------

_APPEND = "resources.lib.nzbget_resolver.nzbget_api.append_nzb"


def test_submit_dupe_backups_appends_with_shared_key_and_scores():
    # Each backup carries the SHARED DupeKey, its own (per-picker) DupeScore, and
    # DupeMode=SCORE; NZBGet groups them by key and keeps the highest active.
    backups = [
        {"link": "http://i/a.nzb", "title": "The Movie GROUP2", "score": 999},
        {"link": "http://i/b.nzb", "title": "The Movie GROUP3", "score": 998},
    ]
    with patch(_APPEND, return_value=(5, None)) as append, patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append", return_value=False
    ):
        submitted = _submit_dupe_backups(backups, "imdb=1234567", _settings({}))
    assert [c.args[0] for c in append.call_args_list] == [
        "http://i/a.nzb",
        "http://i/b.nzb",
    ]
    assert all(c.kwargs["dupe_key"] == "imdb=1234567" for c in append.call_args_list)
    assert [c.kwargs["dupe_score"] for c in append.call_args_list] == [999, 998]
    assert all(c.kwargs["dupe_mode"] == "SCORE" for c in append.call_args_list)
    # Distinct job names (DupeKey groups them, so names need not match).
    assert len({c.args[1] for c in append.call_args_list}) == 2
    assert len(submitted) == 2


def test_submit_dupe_backups_skips_bad_and_duplicate_urls():
    backups = [
        {"link": "http://i/a.nzb", "title": "A", "score": 9},
        "not-a-dict",
        {"title": "no link", "score": 8},
        {"link": "http://i/a.nzb", "title": "dup", "score": 7},
        {"link": "http://i/b.nzb", "title": "B", "score": 6},
    ]
    with patch(_APPEND, return_value=(5, None)) as append, patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append", return_value=False
    ):
        _submit_dupe_backups(backups, "k", _settings({}))
    assert [c.args[0] for c in append.call_args_list] == [
        "http://i/a.nzb",
        "http://i/b.nzb",
    ]


def test_submit_dupe_backups_is_fail_soft_per_backup():
    backups = [
        {"link": "http://i/a.nzb", "title": "A", "score": 3},
        {"link": "http://i/b.nzb", "title": "B", "score": 2},
        {"link": "http://i/c.nzb", "title": "C", "score": 1},
    ]

    def flaky(nzb_url, *a, **k):
        if nzb_url == "http://i/b.nzb":
            raise RuntimeError("boom")
        return (7, None)

    with patch(_APPEND, side_effect=flaky) as append, patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append", return_value=False
    ):
        submitted = _submit_dupe_backups(backups, "k", _settings({}))
    assert append.call_count == 3
    assert len(submitted) == 2  # a and c despite b raising


class _InlineThread:  # pylint: disable=too-few-public-methods
    """Thread stand-in that runs its target synchronously on start()."""

    def __init__(self, target=None, name=None, daemon=None):
        self._target = target
        self.daemon = daemon

    def start(self):
        self._target()


def _dupe_ctx(dupe, getter=None):
    import threading
    from types import SimpleNamespace

    return SimpleNamespace(
        settings_getter=getter or _settings({}),
        dupe=dupe,
        cancel_event=threading.Event(),
    )


def test_spawn_dupe_backups_submits_and_warns_healthcheck():
    dupe = {"key": "imdb=1", "pick_score": 1000, "backups": [{"link": "u", "score": 9}]}
    ctx = _dupe_ctx(dupe)
    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        "resources.lib.nzbget_resolver._submit_dupe_backups", return_value=[5]
    ) as core, patch(
        "resources.lib.nzbget_resolver._warn_if_healthcheck_pauses"
    ) as warn, patch(
        "resources.lib.nzbget_resolver._dupe_check_disabled", return_value=False
    ):
        _spawn_dupe_backups(ctx)
    core.assert_called_once()
    assert core.call_args.args[0] == dupe["backups"]
    assert core.call_args.args[1] == "imdb=1"  # shared key
    warn.assert_called_once()


def test_spawn_dupe_backups_skips_when_dupecheck_disabled():
    # DupeCheck=no -> backups would download in parallel -> skip submission.
    dupe = {"key": "imdb=1", "pick_score": 2, "backups": [{"link": "u", "score": 1}]}
    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        "resources.lib.nzbget_resolver._submit_dupe_backups"
    ) as core, patch(
        "resources.lib.nzbget_resolver._warn_if_healthcheck_pauses"
    ), patch(
        "resources.lib.nzbget_resolver._dupe_check_disabled", return_value=True
    ):
        _spawn_dupe_backups(_dupe_ctx(dupe))
    core.assert_not_called()


def test_snapshot_conn_getter_preserves_blank_username():
    # A blank nzbget_username must survive into the worker getter (NZBGet's empty
    # ControlUsername disables username checking) -- not be defaulted to "nzbget".
    getter = _settings({"nzbget_url": "http://box:6789", "nzbget_username": ""})
    snap = _snapshot_conn_getter(getter)
    assert snap("nzbget_username", "nzbget") == ""
    assert snap("nzbget_url", "http://localhost:6789") == "http://box:6789"


def test_dupe_check_disabled_reads_config():
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.config_option", return_value="no"
    ):
        assert _dupe_check_disabled(_settings({})) is True
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.config_option", return_value="yes"
    ):
        assert _dupe_check_disabled(_settings({})) is False
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.config_option",
        side_effect=RuntimeError("boom"),
    ):
        assert _dupe_check_disabled(_settings({})) is False  # best-effort


def test_spawn_dupe_backups_noop_without_backups_or_key():
    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        "resources.lib.nzbget_resolver._submit_dupe_backups"
    ) as core:
        assert _spawn_dupe_backups(_dupe_ctx({"key": "k", "backups": []})) is None
        assert (
            _spawn_dupe_backups(_dupe_ctx({"key": "", "backups": [{"link": "u"}]}))
            is None
        )
        assert _spawn_dupe_backups(_dupe_ctx(None)) is None
    core.assert_not_called()


def test_spawn_dupe_backups_swallows_thread_start_error():
    class _BoomThread:  # pylint: disable=too-few-public-methods
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    ctx = _dupe_ctx({"key": "k", "backups": [{"link": "u", "score": 1}]})
    with patch("resources.lib.nzbget_resolver.threading.Thread", _BoomThread):
        assert _spawn_dupe_backups(ctx) is None  # must not raise


def test_warn_if_healthcheck_pauses_notifies_once_on_pause():
    _HEALTHCHECK_WARNED[0] = False
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.config_option", return_value="pause"
    ), patch("resources.lib.nzbget_resolver._notify") as notify:
        _warn_if_healthcheck_pauses(_settings({}))
        _warn_if_healthcheck_pauses(_settings({}))
    notify.assert_called_once()  # at most once per session


def test_warn_if_healthcheck_pauses_silent_when_not_pause():
    _HEALTHCHECK_WARNED[0] = False
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.config_option", return_value="delete"
    ), patch("resources.lib.nzbget_resolver._notify") as notify:
        _warn_if_healthcheck_pauses(_settings({}))
    notify.assert_not_called()


def test_resolve_submits_pick_with_dupe_key_and_spawns_backups():
    # With a picker-computed dupe submission, the PICK is appended with the shared
    # DupeKey at the top DupeScore, and the backups are spawned.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    dupe = {
        "key": "imdb=1234567",
        "pick_score": 1000,
        "backups": [{"link": "http://i/b.nzb", "title": "B", "score": 999}],
    }
    with patch(_APPEND, return_value=(42, None)) as append, patch(
        "resources.lib.nzbget_resolver._spawn_dupe_backups"
    ) as spawn, patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "The.Movie", "_nzbget_dupe": dupe},
            settings_getter=_full_settings(),
        )
    append.assert_called_once()
    assert append.call_args.args[0] == "http://i/x.nzb"
    assert append.call_args.kwargs["dupe_key"] == "imdb=1234567"
    assert append.call_args.kwargs["dupe_score"] == 1000  # top score
    assert append.call_args.kwargs["dupe_mode"] == "SCORE"
    spawn.assert_called_once()


def test_resolve_without_dupe_submits_plain_pick():
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(_APPEND, return_value=(42, None)) as append, patch(
        "resources.lib.nzbget_resolver._spawn_dupe_backups"
    ) as spawn, patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "The.Movie"},
            settings_getter=_full_settings(),
        )
    assert append.call_count == 1
    assert append.call_args.kwargs.get("dupe_key", "") == ""
    assert append.call_args.kwargs.get("dupe_score", 0) == 0
    spawn.assert_not_called()
    assert plugin.setResolvedUrl.call_args[0][1] is True


def test_resolve_with_none_getter_and_dupe_does_not_crash():
    # Real Kodi handle-based path passes settings_getter=None; _build_submit_ctx
    # binds it so the background dupe thread carries a callable getter.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    dupe = {"key": "imdb=1", "pick_score": 1000, "backups": [{"link": "u", "score": 9}]}
    with patch(
        "resources.lib.nzbget_resolver._read_settings",
        return_value=("http://box", "smb://host/c", 600),
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api._get_settings",
        return_value=("http://box", "u", "p", ""),
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.completed_base_dir",
        return_value="/dl",
    ), patch(
        "resources.lib.nzbget_resolver._read_poll_interval", return_value=1
    ), patch(
        _APPEND, return_value=(42, None)
    ), patch(
        "resources.lib.nzbget_resolver._spawn_dupe_backups"
    ) as spawn, patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/X"},
    ), patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/c/X/x.mkv",
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "The.Movie", "_nzbget_dupe": dupe},
        )  # settings_getter omitted -> None
    plugin.setResolvedUrl.assert_called_once()
    assert plugin.setResolvedUrl.call_args[0][1] is True
    spawn.assert_called_once()  # dupe backups spawned; primary auth left raw


# ---------------------------------------------------------------------------
# #372 round 2: poll follows the promoted backup + cancel cleans up the group
# ---------------------------------------------------------------------------

_GS = "resources.lib.nzbget_resolver.nzbget_api.group_status"
_HS = "resources.lib.nzbget_resolver.nzbget_api.history_status"
_ACT = "resources.lib.nzbget_resolver.nzbget_api.active_group_by_dupekey"
_SUC = "resources.lib.nzbget_resolver.nzbget_api.history_success_by_dupekey"
_ACT_NAME = "resources.lib.nzbget_resolver.nzbget_api.active_group_by_name"


def _seq_group_status(sequences):
    def _fn(nzbid, settings_getter=None):
        seq = sequences.get(nzbid, [])
        present = seq.pop(0) if seq else False
        return {"present": present, "status": "DOWNLOADING", "percent": 10}

    return _fn


def test_poll_follows_promoted_backup_to_success():
    # Pick(1) fails -> NZBGet promotes backup(9) -> poll switches to 9 -> 9 succeeds.
    dialog = _Dialog()
    hs = {
        1: {
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
        9: {
            "present": True,
            "success": True,
            "status": "SUCCESS/ALL",
            "dest_dir": "/dl/B",
        },
    }
    with patch(
        _GS, side_effect=_seq_group_status({1: [True, False], 9: [True, False]})
    ), patch(
        _HS, side_effect=lambda n, settings_getter=None: hs.get(n, {"present": False})
    ), patch(
        _ACT,
        return_value={
            "present": True,
            "nzbid": 9,
            "status": "DOWNLOADING",
            "percent": 5,
        },
    ), patch(
        _SUC, return_value={"present": False}
    ):
        result = poll_nzbget_job(1, dialog, _Monitor(), 60, interval=0, dupe_key="k")
    assert result == {
        "outcome": "success",
        "dest_dir": "/dl/B",
        "nzbid": 9,
        "job_name": "",
    }


def test_poll_plays_already_succeeded_group_member():
    # Pick(1) fails but a group member already completed in history -> play it.
    dialog = _Dialog()
    with patch(_GS, side_effect=_seq_group_status({1: [False]})), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
    ), patch(_ACT, return_value={"present": False}), patch(
        _SUC, return_value={"present": True, "nzbid": 2, "dest_dir": "/dl/done"}
    ):
        result = poll_nzbget_job(1, dialog, _Monitor(), 60, interval=0, dupe_key="k")
    assert result == {
        "outcome": "success",
        "dest_dir": "/dl/done",
        "nzbid": 2,
        "job_name": "",
    }


def test_poll_reports_failed_when_group_exhausted():
    # Pick fails and no backup is promoted within the grace window -> failed.
    dialog = _Dialog()
    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        _GS, side_effect=_seq_group_status({1: [False]})
    ), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
    ), patch(
        _ACT, return_value={"present": False}
    ), patch(
        _SUC, return_value={"present": False}
    ):
        result = poll_nzbget_job(1, dialog, _Monitor(), 60, interval=0, dupe_key="k")
    assert result["outcome"] == "failed"


def test_poll_waits_for_backup_submitter_before_declaring_failed():
    # A fast-failing primary can hit the promotion grace before the backup daemon
    # has even appended a backup (each NZB fetch can take up to the 30s timeout).
    # The poll must not declare the group exhausted while backups are still being
    # submitted, else automatic failover is lost (round-2 review finding).
    dialog = _Dialog()
    calls = {"n": 0}

    def _is_submitting():
        calls["n"] += 1
        return calls["n"] < 3  # still appending for the first two exhaustion checks

    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        _GS, side_effect=_seq_group_status({1: [False]})
    ), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
    ), patch(
        _ACT, return_value={"present": False}
    ), patch(
        _SUC, return_value={"present": False}
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="k",
            fleet={"is_submitting": _is_submitting},
        )
    assert result["outcome"] == "failed"
    assert calls["n"] >= 3  # it kept waiting while the submitter was alive


def test_poll_canceled_carries_tracked_nzbid_after_failover():
    # Cancel after failover switched to a promoted backup: the canceled outcome
    # must carry the CURRENTLY tracked NZBID (the promoted backup), so the
    # cancel path can final-delete it directly -- the DupeKey sweep alone can
    # miss it on a transient listgroups error (round-3 review finding).
    dialog = _Dialog()

    def _act(dupe_key, exclude_nzbid=None, settings_getter=None):
        dialog.canceled = True  # user cancels right as the promotion is seen
        return {"present": True, "nzbid": 9, "status": "DOWNLOADING", "percent": 5}

    with patch(_GS, side_effect=_seq_group_status({1: [False]})), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
    ), patch(_ACT, side_effect=_act), patch(_SUC, return_value={"present": False}):
        result = poll_nzbget_job(1, dialog, _Monitor(), 60, interval=0, dupe_key="k")
    assert result["outcome"] == "canceled"
    assert result["nzbid"] == 9  # the promoted backup, not the original pick


def test_handle_poll_failure_cancel_also_cancels_promoted_backup():
    # With failover already switched to backup 9, cancel must delete the
    # tracked backup as well as the original pick.
    import threading

    deleted = []
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_jobs",
        side_effect=lambda ids, settings_getter=None: deleted.append(list(ids)),
    ):
        handled, leave = _handle_poll_failure(
            "canceled",
            5,
            _settings({}),
            lambda m: None,
            cancel_event=threading.Event(),
            poll_result={"outcome": "canceled", "nzbid": 9},
        )
    assert (handled, leave) == (True, False)
    assert deleted == [[9, 5]]  # tracked backup first, then the pick


def test_poll_holds_failover_while_promotion_sits_paused():
    # A promoted backup queued PAUSED (e.g. NZBGet globally paused) is not an
    # exhausted group: the poll must keep waiting (bounded by the outer
    # timeout) instead of reporting FAILURE/DUPE at grace expiry, and fail only
    # once no paused member remains (round-3 review finding).
    dialog = _Dialog()
    act_results = [
        {"present": False, "paused_present": True},  # grace expired, but paused
        {"present": False, "paused_present": False},  # paused member gone
    ]

    def _act(dupe_key, exclude_nzbid=None, settings_getter=None):
        return act_results.pop(0) if act_results else {"present": False}

    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        _GS, side_effect=_seq_group_status({1: [False]})
    ), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
    ), patch(
        _ACT, side_effect=_act
    ), patch(
        _SUC, return_value={"present": False}
    ):
        result = poll_nzbget_job(1, dialog, _Monitor(), 60, interval=0, dupe_key="k")
    assert result["outcome"] == "failed"
    assert not act_results  # first (paused) tick did NOT fail; it kept waiting


def test_poll_without_dupe_key_fails_on_primary_failure_unchanged():
    # No dupe_key -> pre-#372 behavior: a primary failure ends the poll at once.
    dialog = _Dialog()
    with patch(_GS, side_effect=_seq_group_status({1: [False]})), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/PAR",
            "dest_dir": "",
        },
    ), patch(_ACT) as act:
        result = poll_nzbget_job(1, dialog, _Monitor(), 60, interval=0)
    assert result["outcome"] == "failed"
    act.assert_not_called()  # never consults the group without a dupe_key


def test_handle_poll_failure_cancel_deletes_own_jobs_and_stops_worker():
    import threading

    ev = threading.Event()
    deleted = []
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_jobs",
        side_effect=lambda ids, settings_getter=None: deleted.append(list(ids)),
    ):
        handled, leave = _handle_poll_failure(
            "canceled", 5, _settings({}), lambda m: None, cancel_event=ev
        )
    assert (handled, leave) == (True, False)
    assert ev.is_set()  # backup worker signaled to stop
    assert deleted == [[5]]  # id-scoped: only this resolve's pick


def test_submit_dupe_backups_stops_on_cancel_event():
    import threading

    ev = threading.Event()
    ev.set()
    with patch(_APPEND) as append:
        submitted = _submit_dupe_backups(
            [{"link": "http://i/a.nzb", "title": "A", "score": 1}],
            "k",
            _settings({}),
            cancel_event=ev,
        )
    append.assert_not_called()
    assert not submitted


def test_extra_backups_from_loader_dedups_caps_and_scores_descending():
    from resources.lib.nzbget_resolver import _extra_backups_from_loader

    candidates = [
        {"link": "http://i/same.nzb", "title": "dup of same-name"},  # already seen
        {"link": "http://i/x.nzb", "title": "720p mirror"},
        {"link": "http://i/x.nzb", "title": "dup link"},  # dedup
        {"link": "", "title": "no link"},
        {"link": "http://i/y.nzb", "title": "another"},
    ]
    extras = _extra_backups_from_loader(lambda: candidates, ["http://i/same.nzb"])
    assert [e["link"] for e in extras] == ["http://i/x.nzb", "http://i/y.nzb"]
    # Scored descending from 0 (below every same-name backup, which are >= 1).
    assert [e["score"] for e in extras] == [0, -1]


def test_extra_backups_from_loader_best_effort_on_none_or_error():
    from resources.lib.nzbget_resolver import _extra_backups_from_loader

    assert not _extra_backups_from_loader(None, [])
    assert not _extra_backups_from_loader(lambda: "DISABLED_SENTINEL", [])

    def _boom():
        raise RuntimeError("indexer down")

    assert not _extra_backups_from_loader(_boom, [])


def test_extra_backups_from_loader_honors_limit():
    # Loader extras must be bounded by the caller's remaining standby-cap slots so
    # same-name backups + extras never exceed "Maximum standby fallback streams"
    # (round-2 review finding: extras were added on top of the cap).
    from resources.lib.nzbget_resolver import _extra_backups_from_loader

    cands = [{"link": "x"}, {"link": "y"}, {"link": "z"}]
    got = _extra_backups_from_loader(lambda: cands, [], limit=2)
    assert [e["link"] for e in got] == ["x", "y"]
    assert not _extra_backups_from_loader(lambda: cands, [], limit=0)


def test_extra_backups_from_loader_no_ceiling_above_five():
    # No code-level ceiling: a limit above the old hard-coded cap of 5 is
    # honored as-is, matching fallback_streams_max having no artificial max.
    from resources.lib.nzbget_resolver import _extra_backups_from_loader

    cands = [{"link": "x{}".format(i)} for i in range(8)]
    got = _extra_backups_from_loader(lambda: cands, [], limit=8)
    assert [e["link"] for e in got] == ["x{}".format(i) for i in range(8)]


def test_spawn_dupe_backups_fail_soft_when_snapshot_raises():
    # Reading the connection snapshot can raise (a bad injected getter / Kodi
    # settings read). Backups are pure insurance submitted AFTER the primary is
    # already accepted -- a snapshot failure must skip them, never propagate out
    # and fail the primary's playback (round-2 review finding: fail-soft snapshot).
    dupe = {"key": "k", "pick_score": 2, "backups": [{"link": "u", "score": 1}]}
    with patch(
        "resources.lib.nzbget_resolver._snapshot_conn_getter",
        side_effect=RuntimeError("settings read failed"),
    ), patch("resources.lib.nzbget_resolver.threading.Thread") as thread:
        result = _spawn_dupe_backups(_dupe_ctx(dupe))
    assert result is None  # skipped, did not raise
    thread.assert_not_called()  # no worker spawned


def test_spawn_dupe_backups_bounds_extras_by_remaining_standby_slots():
    # With max_backups=2 already spent on two same-name backups, the loader extras
    # get 0 remaining slots -- the widening must not exceed the standby cap.
    dupe = {
        "key": "k",
        "pick_score": 3,
        "backups": [{"link": "a", "score": 2}, {"link": "b", "score": 1}],
        "max_backups": 2,
        "loader": lambda: [{"link": "x"}],
    }
    seen = {}

    def _extra(loader, seen_links, limit=5, score_base=0, reserve=0):
        seen["limit"] = limit
        return []

    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        "resources.lib.nzbget_resolver._submit_dupe_backups", return_value=[10, 11]
    ), patch("resources.lib.nzbget_resolver._warn_if_healthcheck_pauses"), patch(
        "resources.lib.nzbget_resolver._dupe_check_disabled", return_value=False
    ), patch(
        "resources.lib.nzbget_resolver._extra_backups_from_loader", side_effect=_extra
    ):
        _spawn_dupe_backups(_dupe_ctx(dupe))
    assert seen["limit"] == 0  # 2 cap - 2 live same-name backups = 0 slots left


def test_poll_excludes_just_failed_member_from_promotion_scan():
    # NZBGet's queue->history transition is not atomic: the just-failed pick can
    # still linger in listgroups under the same DupeKey for a tick. The promotion
    # scan must exclude that id, else active_group_by_dupekey re-selects the failed
    # member as its own 'promotion', clears the grace deadline, and the poll
    # oscillates/hangs to timeout (round-2 review finding: exclude_nzbid).
    dialog = _Dialog()
    seen_exclude = []

    def _act(dupe_key, exclude_nzbid=None, settings_getter=None):
        seen_exclude.append(exclude_nzbid)
        return {"present": False}

    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        _GS, side_effect=_seq_group_status({1: [False]})
    ), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
    ), patch(
        _ACT, side_effect=_act
    ), patch(
        _SUC, return_value={"present": False}
    ):
        result = poll_nzbget_job(1, dialog, _Monitor(), 60, interval=0, dupe_key="k")
    assert result["outcome"] == "failed"
    assert 1 in seen_exclude  # the just-failed pick id is excluded from promotion


def test_spawn_dupe_backups_cleans_own_submissions_on_cancel_after_submit():
    # A cancel arriving mid-submit can let a backup's in-flight append land in
    # NZBGet AFTER _handle_poll_failure's one-shot cancel_dupekey_group sweep;
    # that orphan would be promoted as the group's new active download. The
    # worker must clean up once it observes the cancel -- deleting exactly the
    # NZBIDs IT submitted, never the whole DupeKey: a fresh retry of the same
    # release shares the stable key and must survive a stale worker's cleanup
    # (round-3 review finding: retry race).
    import threading

    ev = threading.Event()
    dupe = {"key": "imdb=1", "pick_score": 9, "backups": [{"link": "u", "score": 1}]}
    ctx = _dupe_ctx(dupe)
    ctx.cancel_event = ev
    cleaned = []

    def _submit(backups, key, getter, cancel_event=None, submitted_sink=None):
        ev.set()  # cancel observed only after this submit's append is already away
        if submitted_sink is not None:
            submitted_sink.append(7)  # published as the append landed
        return [7]

    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        "resources.lib.nzbget_resolver._submit_dupe_backups", side_effect=_submit
    ), patch("resources.lib.nzbget_resolver._warn_if_healthcheck_pauses"), patch(
        "resources.lib.nzbget_resolver._dupe_check_disabled", return_value=False
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_jobs",
        side_effect=lambda ids, settings_getter=None: cleaned.append(list(ids)),
    ):
        _spawn_dupe_backups(ctx)
    assert cleaned == [[7]]  # exactly this worker's submissions deleted


def test_spawn_dupe_backups_does_not_clean_up_on_normal_completion():
    # The cleanup must fire ONLY on cancel -- a normal, non-canceled run must
    # never delete the backups the worker just submitted (nor the pick's group).
    dupe = {"key": "imdb=1", "pick_score": 9, "backups": [{"link": "u", "score": 1}]}
    cleaned = []
    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        "resources.lib.nzbget_resolver._submit_dupe_backups", return_value=[1]
    ), patch("resources.lib.nzbget_resolver._warn_if_healthcheck_pauses"), patch(
        "resources.lib.nzbget_resolver._dupe_check_disabled", return_value=False
    ), patch(
        "resources.lib.nzbget_resolver._extra_backups_from_loader", return_value=[]
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_jobs",
        side_effect=lambda ids, settings_getter=None: cleaned.append(list(ids)),
    ):
        _spawn_dupe_backups(_dupe_ctx(dupe))
    assert not cleaned  # no cancel -> everything submitted is left intact


def test_extra_backups_scores_sit_on_the_score_base():
    # Loader extras must carry the fleet's wall-clock score base so they too
    # outrank prior same-key successes, while staying strictly below every
    # same-name backup (base+1 and up).
    from resources.lib.nzbget_resolver import _extra_backups_from_loader

    cands = [{"link": "x"}, {"link": "y"}]
    got = _extra_backups_from_loader(lambda: cands, [], limit=5, score_base=500)
    assert [e["score"] for e in got] == [500, 499]


def test_spawn_dupe_backups_threads_score_base_into_extras():
    dupe = {
        "key": "k",
        "pick_score": 100002,
        "score_base": 100000,
        "backups": [{"link": "a", "score": 100001}],
        "max_backups": 3,
        "loader": lambda: [{"link": "x"}],
    }
    seen = {}

    def _extra(loader, seen_links, limit=5, score_base=0, reserve=0):
        seen["limit"] = limit
        seen["score_base"] = score_base
        return []

    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        "resources.lib.nzbget_resolver._submit_dupe_backups", return_value=[7]
    ), patch("resources.lib.nzbget_resolver._warn_if_healthcheck_pauses"), patch(
        "resources.lib.nzbget_resolver._dupe_check_disabled", return_value=False
    ), patch(
        "resources.lib.nzbget_resolver._extra_backups_from_loader", side_effect=_extra
    ):
        _spawn_dupe_backups(_dupe_ctx(dupe))
    # Extras start just below the lowest same-name backup: base - count - 1.
    assert seen["score_base"] == 100000 - 1 - 1
    assert seen["limit"] == 2  # 3 cap - 1 live same-name


def test_spawn_dupe_backups_runs_loader_only_fleet():
    # A loader-only submission (no same-name backups) must still spawn the
    # worker and submit the loader extras under the fleet's DupeKey
    # (review thread: loader-only duplicate backups). Round 6: the extras now
    # flow through the veto-aware fill loop, so they are appended directly.
    dupe = {
        "key": "k",
        "pick_score": 100001,
        "score_base": 100000,
        "backups": [],
        "max_backups": 3,
        "loader": lambda: [{"link": "x", "title": "X"}],
    }
    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        _APPEND, return_value=(11, None)
    ) as append, patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append", return_value=False
    ), patch(
        "resources.lib.nzbget_resolver._warn_if_healthcheck_pauses"
    ), patch(
        "resources.lib.nzbget_resolver._dupe_check_disabled", return_value=False
    ):
        thread = _spawn_dupe_backups(_dupe_ctx(dupe))
    assert thread is not None  # worker ran (not the no-backups noop)
    # The loader extra is appended under the fleet DupeKey, scored just below
    # the (empty) same-name band: base - 0 - 1.
    assert append.call_args.args[0] == "x"
    assert append.call_args.kwargs["dupe_key"] == "k"
    assert append.call_args.kwargs["dupe_score"] == 100000 - 1


def test_poll_group_follow_ignores_stale_preexisting_success():
    # A prior same-key SUCCESS whose files are gone must NOT satisfy the
    # group-follow: only successes that appear AFTER this resolve started may
    # play. The poll snapshots pre-existing success ids and excludes them
    # (review thread: stale successes during failover follow).
    dialog = _Dialog()
    seen_excludes = []

    def _suc(dupe_key, exclude_nzbids=None, settings_getter=None):
        seen_excludes.append(tuple(exclude_nzbids or ()))
        if exclude_nzbids and 3 in tuple(exclude_nzbids):
            return {"present": False}  # stale 3 filtered -> nothing playable yet
        return {"present": True, "nzbid": 3, "dest_dir": "/dl/stale"}

    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        _GS, side_effect=_seq_group_status({1: [False]})
    ), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
    ), patch(
        _ACT, return_value={"present": False, "paused_present": False}
    ), patch(
        _SUC, side_effect=_suc
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids",
        return_value=(3,),
    ):
        result = poll_nzbget_job(1, dialog, _Monitor(), 60, interval=0, dupe_key="k")
    assert result["outcome"] == "failed"  # stale success never played
    assert all(3 in ex for ex in seen_excludes)


def test_backups_submit_under_their_own_release_title():
    # Backups must NOT get decorated fallback job names: a promoted backup that
    # completes becomes the SUCCESS history row the next picker render looks up
    # by EXACT title (completed_history -> _tag_available_nzbget). A decorated
    # name would hide it -- and with the wall-clock score base the replay would
    # then re-download despite the files existing (review thread:
    # completed-history reuse).
    ev = None
    with patch(_APPEND, return_value=(5, None)) as append, patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append", return_value=False
    ):
        row = {"link": "http://i/a.nzb", "title": "The.Movie.2024.1080p-GRP"}
        _submit_dupe_backups(
            [dict(row, score=1)],
            "imdb=1|the-movie-2024-1080p-grp",
            _settings({}),
            cancel_event=ev,
        )
    assert append.call_args.args[1] == "The.Movie.2024.1080p-GRP"


def test_completed_download_records_fleet_pubdates():
    # Failover can complete under ANY fleet member -- a different upload with
    # its own post-date. Success must ledger-record every same-name backup's
    # pubdate under the shared title so the picker's repost-guard
    # (_result_pubdate_consistent_with_downloads) recognizes whichever member
    # completed on the next render (review thread: record the promoted
    # backup's own identity).
    from types import SimpleNamespace

    from resources.lib.nzbget_resolver import _play_completed_download

    ctx = SimpleNamespace(
        smb_root="smb://s/d",
        category="",
        completed_base="",
        dialog=None,
        interval=0,
        on_failure=lambda m: None,
        on_success=lambda url: None,
        dupe={
            "key": "k",
            "backups": [
                {"link": "b1", "pubdate": "Tue, 02 Jun 2026 11:00:00 +0000"},
                {"link": "b2"},  # no pubdate -> skipped, never crashes
            ],
        },
    )
    recorded = []
    with patch(
        "resources.lib.nzbget_resolver.record_download",
        side_effect=lambda title, pubdate, size=None: recorded.append((title, pubdate)),
    ), patch(
        "resources.lib.nzbget_resolver._resolve_completed_smb",
        return_value="smb://s/d/x.mkv",
    ):
        _play_completed_download(
            ctx, "/dl/x", "The Title", "Mon, 01 Jun 2026 10:00:00 +0000", "700"
        )
    assert ("The Title", "Mon, 01 Jun 2026 10:00:00 +0000") in recorded  # the pick
    assert ("The Title", "Tue, 02 Jun 2026 11:00:00 +0000") in recorded  # backup
    assert len(recorded) == 2  # pubdate-less backup skipped


def test_cancel_is_scoped_to_this_resolves_nzbids():
    # Cancel must delete exactly the NZBIDs THIS resolve owns -- the pick, the
    # tracked/promoted member, any paused-promoted member, and the worker's
    # submitted backups -- never a whole-DupeKey sweep: an overlapping play of
    # the same release (another client / an already-queued retry) shares the
    # stable key and must survive this cancel (round-5 review findings:
    # paused NZBIDs + overlapping resolves).
    import threading

    deleted = []
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_jobs",
        side_effect=lambda ids, settings_getter=None: deleted.append(list(ids)),
    ):
        handled, leave = _handle_poll_failure(
            "canceled",
            5,
            _settings({}),
            lambda m: None,
            cancel_event=threading.Event(),
            poll_result={"outcome": "canceled", "nzbid": 9, "paused_nzbids": (12,)},
            submitted_nzbids=[7, 8],
        )
    assert (handled, leave) == (True, False)
    assert deleted == [
        [9, 12, 7, 8, 5]
    ]  # tracked, paused, submitted, pick -- ours only


def test_poll_canceled_carries_paused_nzbids():
    # The canceled outcome must carry the paused-promoted member ids seen in
    # group-follow so the cancel path can delete them directly (the DupeKey
    # sweep is gone; only ids this resolve owns are ever canceled).
    dialog = _Dialog()

    def _act(dupe_key, exclude_nzbid=None, settings_getter=None):
        dialog.canceled = True  # user cancels while the group holds on paused
        return {"present": False, "paused_present": True, "paused_nzbids": [12]}

    with patch(_GS, side_effect=_seq_group_status({1: [False]})), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
    ), patch(_ACT, side_effect=_act), patch(
        _SUC, return_value={"present": False}
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
    ):
        result = poll_nzbget_job(1, dialog, _Monitor(), 60, interval=0, dupe_key="k")
    assert result["outcome"] == "canceled"
    assert tuple(result["paused_nzbids"]) == (12,)


def test_group_follow_never_adopts_a_foreign_active_download():
    # An overlapping play of the same release shares the stable DupeKey, so
    # active_group_by_dupekey can return THEIR active download. Group-follow
    # must not track (or later cancel) an NZBID this resolve doesn't own -- it
    # holds instead, bounded by the outer timeout; their SUCCESS is played via
    # the history lookup and their failure frees the key for OUR backups
    # (review finding: keep failover tracking scoped to this resolve).
    dialog = _Dialog()
    foreign = {"present": True, "nzbid": 77, "status": "DOWNLOADING", "percent": 5}
    act_results = [foreign, {"present": False, "paused_present": False}]

    def _act(dupe_key, exclude_nzbid=None, settings_getter=None):
        return act_results.pop(0) if act_results else {"present": False}

    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        _GS, side_effect=_seq_group_status({1: [False]})
    ), patch(
        _HS,
        return_value={
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
    ), patch(
        _ACT, side_effect=_act
    ), patch(
        _SUC, return_value={"present": False}
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="k",
            fleet={"owned_nzbids": lambda: (1, 7, 8)},  # 77 is NOT ours
        )
    assert result["outcome"] == "failed"
    # Two _ACT calls prove the foreign-active tick HELD (did not fail, did not
    # track 77) and only the truly-empty second tick exhausted the group.
    assert not act_results


def test_group_follow_tracks_owned_promoted_backup():
    # NZBGet preserves NZBIDs across history<->queue moves, so OUR promoted
    # backup surfaces with the id we submitted -- with an owned filter present
    # it must still be tracked to success.
    dialog = _Dialog()
    hs = {
        1: {
            "present": True,
            "success": False,
            "status": "FAILURE/HEALTH",
            "dest_dir": "",
        },
        9: {
            "present": True,
            "success": True,
            "status": "SUCCESS/ALL",
            "dest_dir": "/dl/B",
        },
    }
    with patch(
        _GS, side_effect=_seq_group_status({1: [False], 9: [True, False]})
    ), patch(
        _HS, side_effect=lambda n, settings_getter=None: hs.get(n, {"present": False})
    ), patch(
        _ACT,
        return_value={
            "present": True,
            "nzbid": 9,
            "status": "DOWNLOADING",
            "percent": 5,
        },
    ), patch(
        _SUC, return_value={"present": False}
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="k",
            fleet={"owned_nzbids": lambda: (1, 9)},
        )
    assert result == {
        "outcome": "success",
        "dest_dir": "/dl/B",
        "nzbid": 9,
        "job_name": "",
    }


def test_backup_nzbids_publish_into_shared_list_as_appends_land():
    # A cancel mid-batch snapshots ctx.submitted_nzbids BEFORE
    # _submit_dupe_backups returns; each NZBID must be published into the
    # shared list AS ITS APPEND SUCCEEDS so the immediate id-scoped cancel
    # already sees it (round-6 review finding).
    shared = []
    seen_during = []

    def fake_append(url, name, settings_getter=None, **kw):
        seen_during.append(list(shared))  # snapshot BEFORE this append lands
        return (100 + len(seen_during), None)

    with patch(_APPEND, side_effect=fake_append), patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append", return_value=False
    ):
        _submit_dupe_backups(
            [
                {"link": "http://i/a.nzb", "title": "A", "score": 2},
                {"link": "http://i/b.nzb", "title": "B", "score": 1},
            ],
            "k",
            _settings({}),
            submitted_sink=shared,
        )
    # By the time the SECOND append starts, the first id is already published.
    assert seen_during[1] == [101]
    assert shared == [101, 102]


# ---------------------------------------------------------------------------
# #372 round 6: recover from NZBGet's content-fingerprint DELETED/COPY veto
#   - reactive one-shot FORCE rescue of a pick that never entered the queue
#   - veto-aware backfill of a COPY-vetoed backup slot from the loader pool
#   - honest failure message + short grace when the pick died DELETED/COPY
# ---------------------------------------------------------------------------

_COPY = {"present": True, "success": False, "status": "DELETED/COPY", "dest_dir": ""}


def _hist_ok(dest_dir):
    return {
        "present": True,
        "success": True,
        "status": "SUCCESS/ALL",
        "dest_dir": dest_dir,
    }


def _hist_fail(status):
    return {"present": True, "success": False, "status": status, "dest_dir": ""}


def test_copy_vetoed_after_append_is_affirmative_only():
    # Only an AFFIRMATIVE visible DELETED/COPY history row counts as vetoed:
    # an RPC error, an absent row, or any other status falls back to LIVE so a
    # misclassification can only degrade to today's behavior, never drop a good
    # backup.
    from resources.lib.nzbget_resolver import _copy_vetoed_after_append

    with patch(_HS, side_effect=RuntimeError("boom")):
        assert _copy_vetoed_after_append(7, _settings({})) is False
    with patch(_HS, return_value={"present": False, "status": "", "dest_dir": ""}):
        assert _copy_vetoed_after_append(7, _settings({})) is False
    with patch(_HS, return_value={"present": True, "status": "FAILURE/HEALTH"}):
        assert _copy_vetoed_after_append(7, _settings({})) is False
    with patch(_HS, return_value={"present": True, "status": "DELETED/DUPE"}):
        assert _copy_vetoed_after_append(7, _settings({})) is False
    for status in ("DELETED/COPY", "deleted/copy", "  Deleted/Copy  "):
        with patch(_HS, return_value={"present": True, "status": status}):
            assert _copy_vetoed_after_append(7, _settings({})) is True


def test_submit_dupe_backups_sinks_but_does_not_count_vetoed_ids():
    # A COPY-vetoed backup id must be EXCLUDED from the LIVE return (that slot
    # was never really filled) yet still land in the shared sink so a cancel
    # deletes its DELETED/COPY history row too.
    backups = [
        {"link": "http://i/a.nzb", "title": "A", "score": 2},
        {"link": "http://i/b.nzb", "title": "B", "score": 1},
    ]
    ids = {"http://i/a.nzb": 10, "http://i/b.nzb": 11}
    sink = []

    def fake_append(url, name, settings_getter=None, **kw):
        return (ids[url], None)

    with patch(_APPEND, side_effect=fake_append), patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append",
        side_effect=lambda nzbid, getter: nzbid == 10,  # 'a' vetoed
    ):
        live = _submit_dupe_backups(backups, "k", _settings({}), submitted_sink=sink)
    assert live == [11]  # vetoed 10 not counted as a live backup
    assert sink == [10, 11]  # both sink -> cancel deletes the vetoed row too


def test_extra_backups_from_loader_reserve_widens_list_only():
    from resources.lib.nzbget_resolver import _extra_backups_from_loader

    cands = [{"link": "a"}, {"link": "b"}, {"link": "c"}, {"link": "d"}]
    # Default reserve=0 keeps the pre-round-6 behavior byte-identical.
    base = _extra_backups_from_loader(lambda: cands, [], limit=2)
    assert [e["link"] for e in base] == ["a", "b"]
    # reserve widens the candidate LIST (backfill headroom) beyond the cap.
    widened = _extra_backups_from_loader(lambda: cands, [], limit=2, reserve=2)
    assert [e["link"] for e in widened] == ["a", "b", "c", "d"]
    # Scores stay strictly descending from the anchor across the whole list.
    assert [e["score"] for e in widened] == [0, -1, -2, -3]
    scored = _extra_backups_from_loader(
        lambda: cands, [], limit=2, reserve=2, score_base=500
    )
    assert [e["score"] for e in scored] == [500, 499, 498, 497]
    # cap<=0 short-circuits regardless of reserve.
    assert not _extra_backups_from_loader(lambda: cands, [], limit=0, reserve=5)


def test_backup_fleet_backfills_vetoed_same_name_slot_from_loader():
    # cap 2, one same-name backup COPY-vetoed -> the freed slot is backfilled
    # with a loader candidate the pre-round-6 budget (2 cap - 2 same-name = 0)
    # would never have submitted.
    import threading

    from resources.lib.nzbget_resolver import _submit_backup_fleet

    dupe = {
        "key": "k",
        "score_base": 1000,
        "max_backups": 2,
        "backups": [
            {"link": "a", "title": "A", "score": 5},
            {"link": "b", "title": "B", "score": 4},
        ],
        "loader": lambda: [{"link": "x", "title": "X"}],
    }
    ids = {"a": 1, "b": 2, "x": 3}
    sink = []

    def fake_append(url, name, settings_getter=None, **kw):
        return (ids[url], None)

    with patch(_APPEND, side_effect=fake_append) as append, patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append",
        side_effect=lambda nzbid, getter: nzbid == 1,  # same-name 'a' vetoed
    ):
        _submit_backup_fleet(_settings({}), threading.Event(), "k", dupe, sink)
    appended = [c.args[0] for c in append.call_args_list]
    assert appended == ["a", "b", "x"]  # x backfilled the vetoed 'a' slot
    assert sink == [1, 2, 3]  # every appended id sinks, vetoed one included


def test_backup_fleet_replaces_vetoed_extras_until_pool_or_attempt_bound():
    import threading

    from resources.lib.nzbget_resolver import (
        _MAX_VETO_REPLACEMENTS,
        _submit_extras_until_filled,
    )

    candidates = [
        {"link": "u%d" % i, "title": "T%d" % i, "score": 100 - i} for i in range(20)
    ]

    # Everything vetoed -> attempts stop at remaining + _MAX_VETO_REPLACEMENTS.
    calls = {"n": 0}

    def append_all(url, name, settings_getter=None, **kw):
        calls["n"] += 1
        return (calls["n"], None)

    with patch(_APPEND, side_effect=append_all), patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append", return_value=True
    ):
        live = _submit_extras_until_filled(
            candidates, 2, "k", _settings({}), threading.Event(), []
        )
    assert not live  # never reached the live target
    assert calls["n"] == 2 + _MAX_VETO_REPLACEMENTS  # bounded attempt budget

    # Keeps drawing past vetoed candidates to reach the live target.
    calls2 = {"n": 0}

    def append_seq(url, name, settings_getter=None, **kw):
        calls2["n"] += 1
        return (calls2["n"], None)

    with patch(_APPEND, side_effect=append_seq), patch(
        "resources.lib.nzbget_resolver._copy_vetoed_after_append",
        side_effect=lambda nzbid, getter: nzbid in (1, 2),  # first two vetoed
    ):
        live2 = _submit_extras_until_filled(
            candidates, 2, "k", _settings({}), threading.Event(), []
        )
    assert live2 == [3, 4]  # skipped two vetoed, filled two live
    assert calls2["n"] == 4  # within the 2 + 5 attempt bound


def _rescue_stub(new_id, counter):
    def _rescue():
        counter["n"] += 1
        return new_id

    return _rescue


def test_poll_rescues_copy_vetoed_pick_and_plays_force_resubmit():
    # The pick dies DELETED/COPY (never entered the queue) and the group is
    # otherwise exhausted -> the poll invokes the one-shot FORCE rescue, tracks
    # the new NZBID, and plays it when it succeeds.
    dialog = _Dialog()
    counter = {"n": 0}
    hs = {
        1: dict(_COPY),
        7: _hist_ok("/dl/R"),
    }
    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        "resources.lib.nzbget_resolver._COPY_VETO_GRACE", 0
    ), patch(_GS, side_effect=_seq_group_status({1: [False], 7: [True, False]})), patch(
        _HS, side_effect=lambda n, settings_getter=None: hs.get(n, {"present": False})
    ), patch(
        _ACT, return_value={"present": False}
    ), patch(
        _SUC, return_value={"present": False}
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="k",
            fleet={"rescue": _rescue_stub(7, counter)},
        )
    assert result == {
        "outcome": "success",
        "dest_dir": "/dl/R",
        "nzbid": 7,
        "job_name": "",
    }
    assert counter["n"] == 1  # rescue fired exactly once


def test_pick_rescue_callable_force_resubmits_under_same_key_and_records_id():
    from types import SimpleNamespace

    from resources.lib.nzbget_resolver import _pick_rescue_callable

    ctx = SimpleNamespace(
        settings_getter=_settings({}),
        dupe={"key": "imdb=9", "pick_score": 1000},
        submitted_nzbids=[],
    )
    rescue = _pick_rescue_callable(ctx, "http://i/x.nzb", "The.Movie")
    with patch(_ACT_NAME, return_value=False), patch(
        _APPEND, return_value=(55, None)
    ) as append:
        new_id = rescue()
    assert new_id == 55
    assert append.call_args.args[0] == "http://i/x.nzb"
    assert append.call_args.kwargs["dupe_key"] == "imdb=9"  # same key reused
    assert append.call_args.kwargs["dupe_score"] == 1000  # pick score reused
    assert append.call_args.kwargs["dupe_mode"] == "FORCE"  # overrides the veto
    assert ctx.submitted_nzbids == [55]  # recorded BEFORE return (owned/cancel)

    # A failed append returns None and records nothing, never raises.
    ctx.submitted_nzbids = []
    with patch(_ACT_NAME, return_value=False), patch(
        _APPEND, return_value=(None, "append returned 0")
    ):
        assert rescue() is None
    assert ctx.submitted_nzbids == []
    with patch(_ACT_NAME, return_value=False), patch(
        _APPEND, side_effect=RuntimeError("boom")
    ):
        assert rescue() is None
    assert ctx.submitted_nzbids == []


def test_pick_rescue_callable_skips_force_when_foreign_active_present():
    # A COPY veto can shadow a download that's still actively queued (another
    # client, or an overlapping resolve of the same release) rather than a
    # purely historical duplicate. FORCE would otherwise race a wasteful
    # parallel download of identical content, so the rescue checks by name
    # (the plain path has no DupeKey to check via active_group_by_dupekey)
    # and skips without ever calling append.
    from types import SimpleNamespace

    from resources.lib.nzbget_resolver import _pick_rescue_callable

    ctx = SimpleNamespace(settings_getter=_settings({}), dupe=None, submitted_nzbids=[])
    rescue = _pick_rescue_callable(ctx, "http://i/x.nzb", "The.Movie")
    with patch(_ACT_NAME, return_value=True) as active_by_name, patch(
        _APPEND
    ) as append:
        assert rescue() is None
    active_by_name.assert_called_once_with(
        "The.Movie", settings_getter=ctx.settings_getter
    )
    append.assert_not_called()
    assert ctx.submitted_nzbids == []


def test_poll_rescue_is_one_shot():
    # The FORCE re-submit is tried once. If the rescued download later fails a
    # real health check with no promotion, the group reports FAILURE/DUPE and
    # the rescue is NOT attempted again.
    dialog = _Dialog()
    counter = {"n": 0}
    hs = {
        1: dict(_COPY),
        7: _hist_fail("FAILURE/HEALTH"),
    }
    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        "resources.lib.nzbget_resolver._COPY_VETO_GRACE", 0
    ), patch(_GS, side_effect=_seq_group_status({1: [False], 7: [False]})), patch(
        _HS, side_effect=lambda n, settings_getter=None: hs.get(n, {"present": False})
    ), patch(
        _ACT, return_value={"present": False}
    ), patch(
        _SUC, return_value={"present": False}
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="k",
            fleet={"rescue": _rescue_stub(7, counter)},
        )
    assert result == {"outcome": "failed", "status": "FAILURE/DUPE"}
    assert counter["n"] == 1


def test_poll_reports_failure_copy_when_rescue_unavailable_or_fails():
    def _run(fleet):
        dialog = _Dialog()
        with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
            "resources.lib.nzbget_resolver._COPY_VETO_GRACE", 0
        ), patch(_GS, side_effect=_seq_group_status({1: [False]})), patch(
            _HS, return_value=dict(_COPY)
        ), patch(
            _ACT, return_value={"present": False}
        ), patch(
            _SUC, return_value={"present": False}
        ), patch(
            "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
        ):
            return poll_nzbget_job(
                1, dialog, _Monitor(), 60, interval=0, dupe_key="k", fleet=fleet
            )

    assert _run({})["status"] == "FAILURE/COPY"  # no rescue callable
    assert _run({"rescue": lambda: None})["status"] == "FAILURE/COPY"  # append failed


def test_poll_copy_veto_uses_short_grace():
    # With _PROMOTION_GRACE huge but _COPY_VETO_GRACE 0, the COPY branch's short
    # grace still lets exhaustion/rescue be reached promptly (no ~20s stall).
    dialog = _Dialog()
    counter = {"n": 0}
    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 9999), patch(
        "resources.lib.nzbget_resolver._COPY_VETO_GRACE", 0
    ), patch(_GS, side_effect=_seq_group_status({1: [False]})), patch(
        _HS, return_value=dict(_COPY)
    ), patch(
        _ACT, return_value={"present": False}
    ), patch(
        _SUC, return_value={"present": False}
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="k",
            fleet={"rescue": _rescue_stub(None, counter)},
        )
    assert result["status"] == "FAILURE/COPY"
    assert counter["n"] == 1


def test_poll_waits_for_worker_before_copy_rescue():
    # The rescue must not fire while the backup worker is still appending -- a
    # sibling that escapes the veto should be adopted first.
    dialog = _Dialog()
    calls = {"submit": 0, "rescue": 0}

    def _is_submitting():
        calls["submit"] += 1
        return calls["submit"] < 3  # still appending for the first two checks

    def _rescue():
        calls["rescue"] += 1  # returns None -> rescue append could not help

    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        "resources.lib.nzbget_resolver._COPY_VETO_GRACE", 0
    ), patch(_GS, side_effect=_seq_group_status({1: [False]})), patch(
        _HS, return_value=dict(_COPY)
    ), patch(
        _ACT, return_value={"present": False}
    ), patch(
        _SUC, return_value={"present": False}
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="k",
            fleet={"rescue": _rescue, "is_submitting": _is_submitting},
        )
    assert result["status"] == "FAILURE/COPY"
    assert calls["submit"] >= 3  # held while the worker was alive
    assert calls["rescue"] == 1  # rescued only after the worker drained


def test_copy_veto_rearms_short_grace_not_full_promotion_grace_while_pending():
    # Regression (Codex review on PR #406): the "promotion still pending"
    # re-arm branch unconditionally extended by the full _PROMOTION_GRACE,
    # even when the pick died COPY -- defeating the whole point of the short
    # _COPY_VETO_GRACE (a worker that drains moments later would still wait
    # out the full ~20s stall this change exists to avoid).
    state = {
        "current": None,
        "exclude": 1,
        "pick": 1,
        "copy_vetoed": True,
        "promotion_deadline": _time_module.monotonic() - 1,  # already expired
        "paused_nzbids": (),
    }
    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 9999), patch(
        "resources.lib.nzbget_resolver._COPY_VETO_GRACE", 3
    ), patch(_SUC, return_value={"present": False}), patch(
        _ACT, return_value={"present": False, "paused_present": False}
    ):
        before = _time_module.monotonic()
        outcome = _tick_group_follow(
            state,
            _Dialog(),
            None,
            "k",
            {"is_submitting": lambda: True},  # still pending -> re-arm, not rescue
        )
    assert outcome is None
    # Re-armed on the short grace (~3s out), nowhere near the full 9999s.
    assert state["promotion_deadline"] < before + 3 + 2
    assert state["promotion_deadline"] > before + 3 - 2


def test_poll_prefers_live_owned_backup_over_rescue():
    # The pick dies COPY, but an owned promoted backup surfaces -> it is adopted
    # and the rescue never fires.
    dialog = _Dialog()
    counter = {"n": 0}
    hs = {
        1: dict(_COPY),
        9: _hist_ok("/dl/B"),
    }
    with patch(
        _GS, side_effect=_seq_group_status({1: [False], 9: [True, False]})
    ), patch(
        _HS, side_effect=lambda n, settings_getter=None: hs.get(n, {"present": False})
    ), patch(
        _ACT,
        return_value={
            "present": True,
            "nzbid": 9,
            "status": "DOWNLOADING",
            "percent": 5,
        },
    ), patch(
        _SUC, return_value={"present": False}
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="k",
            fleet={
                "rescue": _rescue_stub(999, counter),
                "owned_nzbids": lambda: (1, 9),
            },
        )
    assert result == {
        "outcome": "success",
        "dest_dir": "/dl/B",
        "nzbid": 9,
        "job_name": "",
    }
    assert counter["n"] == 0  # adopted the owned backup; rescue never called


def test_poll_no_dupe_key_rescues_copy_vetoed_plain_submit():
    # The same COPY veto also strikes the plain (no-fleet) submit path; the poll
    # carries a rescue callable there too and recovers via the FORCE re-submit.
    dialog = _Dialog()
    counter = {"n": 0}
    hs = {
        1: dict(_COPY),
        7: _hist_ok("/dl/R"),
    }
    with patch(
        _GS, side_effect=_seq_group_status({1: [False], 7: [True, False]})
    ), patch(
        _HS, side_effect=lambda n, settings_getter=None: hs.get(n, {"present": False})
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="",
            fleet={"rescue": _rescue_stub(7, counter)},
        )
    assert result == {
        "outcome": "success",
        "dest_dir": "/dl/R",
        "nzbid": 7,
        "job_name": "",
    }
    assert counter["n"] == 1


def test_poll_canceled_after_rescue_carries_rescued_nzbid():
    # Canceling right after the rescue adoption must carry the rescued NZBID so
    # the cancel path final-deletes exactly it.
    dialog = _Dialog()

    def _group(nzbid, settings_getter=None):
        if nzbid == 7:
            dialog.canceled = True  # cancel as the rescued id is first polled
            return {"present": True, "status": "DOWNLOADING", "percent": 5}
        return {"present": False, "status": "", "percent": 0}

    hs = {1: dict(_COPY)}
    with patch("resources.lib.nzbget_resolver._PROMOTION_GRACE", 0), patch(
        "resources.lib.nzbget_resolver._COPY_VETO_GRACE", 0
    ), patch(_GS, side_effect=_group), patch(
        _HS, side_effect=lambda n, settings_getter=None: hs.get(n, {"present": False})
    ), patch(
        _ACT, return_value={"present": False}
    ), patch(
        _SUC, return_value={"present": False}
    ), patch(
        "resources.lib.nzbget_resolver._preexisting_success_ids", return_value=()
    ):
        result = poll_nzbget_job(
            1,
            dialog,
            _Monitor(),
            60,
            interval=0,
            dupe_key="k",
            fleet={"rescue": lambda: 7},
        )
    assert result["outcome"] == "canceled"
    assert result["nzbid"] == 7


def test_handle_poll_failure_copy_status_uses_honest_message():
    # A COPY-shaped terminal status (synthetic FAILURE/COPY or raw DELETED/COPY)
    # surfaces the honest "already in history, re-queue failed" message (30231);
    # any other failure keeps the generic 30220.
    seen = []
    with patch("resources.lib.nzbget_resolver._string", side_effect=str):
        for status in ("FAILURE/COPY", "DELETED/COPY", "FAILURE/HEALTH"):
            _handle_poll_failure(
                "failed",
                5,
                _settings({}),
                seen.append,
                poll_result={"outcome": "failed", "status": status},
            )
    assert seen == ["30231", "30231", "30220"]
