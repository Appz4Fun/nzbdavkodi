# SPDX-License-Identifier: GPL-3.0-or-later
import sys
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
        "resources.lib.nzbget_resolver.nzbget_api.cancel_job"
    ) as cancel:
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "X"},
            settings_getter=_full_settings(),
        )
    cancel.assert_called_once()
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
    with patch(_APPEND, return_value=(5, None)) as append:
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
    with patch(_APPEND, return_value=(5, None)) as append:
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

    with patch(_APPEND, side_effect=flaky) as append:
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
        "resources.lib.nzbget_resolver._submit_dupe_backups"
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
    assert result == {"outcome": "success", "dest_dir": "/dl/B"}


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
    assert result == {"outcome": "success", "dest_dir": "/dl/done"}


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


def test_handle_poll_failure_cancel_deletes_group_and_stops_worker():
    import threading

    ev = threading.Event()
    calls = []
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_dupekey_group",
        side_effect=lambda k, settings_getter=None: calls.append(k),
    ), patch("resources.lib.nzbget_resolver.nzbget_api.cancel_job") as cancel_job:
        handled, leave = _handle_poll_failure(
            "canceled", 5, _settings({}), lambda m: None, dupe_key="k", cancel_event=ev
        )
    assert (handled, leave) == (True, False)
    assert calls == ["k"]  # whole group deleted
    assert ev.is_set()  # backup worker signaled to stop
    cancel_job.assert_not_called()  # group path, not the single-job path


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


def test_spawn_dupe_backups_resweeps_group_on_cancel_after_submit():
    # A cancel arriving mid-submit can let a backup's in-flight append land in
    # NZBGet AFTER _handle_poll_failure's one-shot cancel_dupekey_group sweep; that
    # orphan would be promoted as the group's new active download. The worker must
    # re-sweep once it observes the cancel (round-2 review finding: cancel-race).
    import threading

    ev = threading.Event()
    dupe = {"key": "imdb=1", "pick_score": 9, "backups": [{"link": "u", "score": 1}]}
    ctx = _dupe_ctx(dupe)
    ctx.cancel_event = ev
    swept = []

    def _submit(backups, key, getter, cancel_event=None):
        ev.set()  # cancel observed only after this submit's append is already away
        return 1

    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        "resources.lib.nzbget_resolver._submit_dupe_backups", side_effect=_submit
    ), patch("resources.lib.nzbget_resolver._warn_if_healthcheck_pauses"), patch(
        "resources.lib.nzbget_resolver._dupe_check_disabled", return_value=False
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_dupekey_group",
        side_effect=lambda k, settings_getter=None: swept.append(k),
    ):
        _spawn_dupe_backups(ctx)
    assert swept == ["imdb=1"]  # worker re-swept the group after the mid-submit cancel


def test_spawn_dupe_backups_does_not_resweep_on_normal_completion():
    # The re-sweep must fire ONLY on cancel -- a normal, non-canceled run must never
    # delete the pick's own DupeKey group (that would wipe the active download and
    # every backup the worker just submitted).
    dupe = {"key": "imdb=1", "pick_score": 9, "backups": [{"link": "u", "score": 1}]}
    swept = []
    with patch("resources.lib.nzbget_resolver.threading.Thread", _InlineThread), patch(
        "resources.lib.nzbget_resolver._submit_dupe_backups", return_value=1
    ), patch("resources.lib.nzbget_resolver._warn_if_healthcheck_pauses"), patch(
        "resources.lib.nzbget_resolver._dupe_check_disabled", return_value=False
    ), patch(
        "resources.lib.nzbget_resolver._extra_backups_from_loader", return_value=[]
    ), patch(
        "resources.lib.nzbget_resolver.nzbget_api.cancel_dupekey_group",
        side_effect=lambda k, settings_getter=None: swept.append(k),
    ):
        _spawn_dupe_backups(_dupe_ctx(dupe))
    assert not swept  # no cancel -> the group is left intact
