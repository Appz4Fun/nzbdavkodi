# SPDX-License-Identifier: GPL-3.0-or-later
import sys
from unittest.mock import MagicMock, patch

from resources.lib.nzbget_resolver import (
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


class _Monitor:
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

    # Monitor never aborts; a tiny timeout drives the loop to budget
    # exhaustion -> exercises the real timeout branch, not aborted.
    with patch(
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


def test_resolve_missing_config_resolves_false():
    plugin = sys.modules["xbmcplugin"]
    plugin.setResolvedUrl = MagicMock()
    resolve_and_play_nzbget(
        7,
        {"nzburl": "http://i/x.nzb", "title": "X"},
        settings_getter=_settings({}),
    )
    plugin.setResolvedUrl.assert_called_once()
    assert plugin.setResolvedUrl.call_args[0][1] is False


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
