# SPDX-License-Identifier: GPL-3.0-or-later
import sys
from unittest.mock import MagicMock, patch

from resources.lib.nzbget_resolver import (
    _read_poll_interval,
    _read_settings,
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
        "resources.lib.nzbget_resolver.nzbget_api.find_active_by_name",
        return_value=None,
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
        )
    li.setProperty.assert_called_with("StartOffset", "137.0")


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


def test_resolve_attaches_to_active_job_without_appending():
    # A prior attempt left a still-downloading job. The retry must attach to
    # that NZBID and poll it rather than appending a duplicate.
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    with patch(
        "resources.lib.nzbget_resolver.nzbget_api.find_active_by_name",
        return_value=99,
    ), patch("resources.lib.nzbget_resolver.nzbget_api.append_nzb") as append, patch(
        "resources.lib.nzbget_resolver.poll_nzbget_job",
        return_value={"outcome": "success", "dest_dir": "/dl/movies/The.Movie"},
    ) as poll, patch(
        "resources.lib.nzbget_resolver.resolve_smb_video",
        return_value="smb://host/completed/The.Movie/movie.mkv",
    ):
        resolve_and_play_nzbget(
            7,
            {"nzburl": "http://i/x.nzb", "title": "The.Movie"},
            settings_getter=_full_settings(),
        )
    append.assert_not_called()
    assert poll.call_args[0][0] == 99  # polled the existing job, not a new one
    assert plugin.setResolvedUrl.call_args[0][1] is True


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
