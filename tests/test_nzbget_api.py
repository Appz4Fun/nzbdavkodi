# SPDX-License-Identifier: GPL-3.0-or-later
import base64
from unittest.mock import patch

from resources.lib.nzbget_api import (
    _get_settings,
    _rpc_call,
    _rpc_url,
    append_nzb,
    cancel_job,
    group_status,
    history_status,
)

# Import the production probe under a NON-``test_*`` alias: imported at module
# scope, ``test_connection`` would otherwise be collected and run by pytest as
# a phantom test (asserting nothing, returning a tuple).
from resources.lib.nzbget_api import test_connection as connection_probe


def _getter(values):
    return lambda key, default="": values.get(key, default)


def test_get_settings_strips_and_defaults():
    getter = _getter(
        {
            "nzbget_url": "http://box:6789/",
            "nzbget_username": "nzbget",
            "nzbget_password": "pw",
            "nzbget_category": "movies",
        }
    )
    url, user, password, category = _get_settings(settings_getter=getter)
    assert url == "http://box:6789"
    assert user == "nzbget"
    assert password == "pw"
    assert category == "movies"


def test_rpc_url_appends_jsonrpc():
    assert _rpc_url("http://box:6789") == "http://box:6789/jsonrpc"


def test_append_nzb_fetches_encodes_and_returns_nzbid():
    getter = _getter(
        {
            "nzbget_url": "http://box:6789",
            "nzbget_username": "nzbget",
            "nzbget_password": "pw",
            "nzbget_category": "movies",
        }
    )
    captured = {}

    def fake_post(url, payload, timeout=0, basic_auth=None):
        captured["payload"] = payload
        return '{"result": 42, "error": null}'

    with patch(
        "resources.lib.nzbget_api._http_get", return_value="<nzb>data</nzb>"
    ), patch("resources.lib.nzbget_api._http_post_json", side_effect=fake_post):
        nzbid, error = append_nzb(
            "http://indexer/x.nzb", "The.Movie.2024", settings_getter=getter
        )

    assert nzbid == 42
    assert error is None
    params = captured["payload"]["params"]
    # NZBGet append signature (nzbget.com v16+/v26, confirmed against a live
    # 26.1 box): Filename, Content(base64), Category, Priority, AddToTop,
    # AddPaused, DupeKey, DupeScore, DupeMode, AutoCategory, PPParameters.
    # The trailing AutoCategory + PPParameters are NOT optional in practice:
    # omitting them yields ``Invalid parameter (Parameters)`` (JSON-RPC code 2)
    # and the NZB never enters the queue.
    assert len(params) == 11
    assert params[0] == "The.Movie.2024.nzb"
    assert base64.b64decode(params[1]).decode("utf-8") == "<nzb>data</nzb>"
    assert params[2] == "movies"
    assert params[8] == "SCORE"  # DupeMode
    # AutoCategory=False so NZBGet keeps the category we send (the SMB path
    # mapping depends on it); PPParameters=[] (no post-processing params).
    assert params[9] is False
    assert params[10] == []


def test_append_nzb_returns_error_when_nzbid_not_positive():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._http_get", return_value="<nzb/>"), patch(
        "resources.lib.nzbget_api._http_post_json",
        return_value='{"result": 0, "error": null}',
    ):
        nzbid, error = append_nzb("http://i/x.nzb", "X", settings_getter=getter)
    assert nzbid is None
    assert error is not None


def test_append_nzb_returns_error_when_fetch_fails():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._http_get", side_effect=OSError("dead")):
        nzbid, error = append_nzb("http://i/x.nzb", "X", settings_getter=getter)
    assert nzbid is None
    assert error is not None


def test_group_status_returns_progress_for_matching_nzbid():
    getter = _getter({"nzbget_url": "http://box:6789"})
    groups = [
        {
            "NZBID": 42,
            "Status": "DOWNLOADING",
            "DownloadedSizeMB": 250,
            "FileSizeMB": 1000,
        }
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(groups, None)):
        status = group_status(42, settings_getter=getter)
    assert status["present"] is True
    assert status["status"] == "DOWNLOADING"
    assert status["percent"] == 25


def test_group_status_absent_when_nzbid_not_in_queue():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._rpc_call", return_value=([], None)):
        status = group_status(42, settings_getter=getter)
    assert status["present"] is False


def test_history_status_success_returns_destdir():
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [{"NZBID": 42, "Status": "SUCCESS/ALL", "DestDir": "/dl/movies/X"}]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        status = history_status(42, settings_getter=getter)
    assert status["present"] is True
    assert status["success"] is True
    assert status["dest_dir"] == "/dl/movies/X"


def test_history_status_prefers_finaldir_over_destdir():
    # A post-processing script that moves the output sets FinalDir; the SMB
    # target must follow the file to its final location, not the stale DestDir.
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [
        {
            "NZBID": 42,
            "Status": "SUCCESS/ALL",
            "DestDir": "/dl/intermediate/X",
            "FinalDir": "/dl/movies/X",
        }
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        status = history_status(42, settings_getter=getter)
    assert status["dest_dir"] == "/dl/movies/X"


def test_find_completed_by_name_returns_finaldir_for_success():
    from resources.lib.nzbget_api import find_completed_by_name

    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [
        {"Name": "Other.Show", "Status": "SUCCESS/ALL", "DestDir": "/dl/other"},
        {
            "NZBName": "The.Movie.2024.nzb",
            "Status": "SUCCESS/ALL",
            "DestDir": "/dl/dest/The.Movie.2024",
            "FinalDir": "/dl/movies/The.Movie.2024",
        },
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        dest = find_completed_by_name("The.Movie.2024", settings_getter=getter)
    assert dest == "/dl/movies/The.Movie.2024"


def test_find_completed_by_name_ignores_non_success_and_mismatch():
    from resources.lib.nzbget_api import find_completed_by_name

    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [
        {"Name": "The.Movie", "Status": "FAILURE/UNPACK", "DestDir": "/dl/x"},
        {"Name": "Different.Movie", "Status": "SUCCESS/ALL", "DestDir": "/dl/y"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        assert find_completed_by_name("The.Movie", settings_getter=getter) is None


def test_find_active_by_name_returns_nzbid_for_queued_job():
    from resources.lib.nzbget_api import find_active_by_name

    getter = _getter({"nzbget_url": "http://box:6789"})
    groups = [
        {"NZBID": 7, "NZBName": "Unrelated.nzb"},
        {"NZBID": 99, "NZBName": "The.Movie.2024.nzb"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(groups, None)):
        nzbid = find_active_by_name("The.Movie.2024", settings_getter=getter)
    assert nzbid == 99


def test_find_active_by_name_none_on_rpc_error():
    from resources.lib.nzbget_api import find_active_by_name

    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(None, "boom")):
        assert find_active_by_name("X", settings_getter=getter) is None


def test_history_status_failure_flagged():
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [{"NZBID": 42, "Status": "FAILURE/UNPACK", "DestDir": "/dl/x"}]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        status = history_status(42, settings_getter=getter)
    assert status["present"] is True
    assert status["success"] is False


def test_history_status_warning_is_failure_even_with_destdir():
    # Spec decision #3 guarantees a repaired/unpacked/playable file, so a
    # terminal WARNING/* (e.g. WARNING/REPAIRABLE / WARNING/DAMAGED, where
    # par2 repair did not run) is classified as a failure even when a DestDir
    # is reported — we don't risk playing a corrupt file.
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [{"NZBID": 42, "Status": "WARNING/HEALTH", "DestDir": "/dl/movies/X"}]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        status = history_status(42, settings_getter=getter)
    assert status["present"] is True
    assert status["success"] is False


def test_history_status_deleted_dupe_is_failure_even_with_destdir():
    # DELETED/DUPE must NOT auto-pass even if a DestDir is reported.
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [{"NZBID": 42, "Status": "DELETED/DUPE", "DestDir": "/dl/movies/X"}]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        status = history_status(42, settings_getter=getter)
    assert status["present"] is True
    assert status["success"] is False


def test_test_connection_ok_on_version():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._rpc_call", return_value=("24.0", None)):
        ok, error = connection_probe(settings_getter=getter)
    assert ok is True
    assert error is None


def test_test_connection_fails_on_error():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(None, "401")):
        ok, error = connection_probe(settings_getter=getter)
    assert ok is False
    assert error == "401"


def test_cancel_job_issues_group_then_history_delete():
    getter = _getter({"nzbget_url": "http://box:6789"})
    calls = []

    def fake_rpc(method, params, settings_getter=None, timeout=30):
        calls.append((method, params))
        return (True, None)

    with patch("resources.lib.nzbget_api._rpc_call", side_effect=fake_rpc):
        cancel_job(42, settings_getter=getter)

    # Both deletes must fire, in order, each targeting the NZBID. The modern
    # v18+ editqueue shape is (Command, Args, IDs) — exactly 3 params with NO
    # legacy int Offset, and the NZBID list last.
    assert [c[0] for c in calls] == ["editqueue", "editqueue"]
    assert calls[0][1] == ["GroupFinalDelete", "", [42]]
    assert calls[1][1] == ["HistoryFinalDelete", "", [42]]
    # Guard against a regression to the pre-v18 (Command, Offset, Text, IDs):
    assert len(calls[0][1]) == 3
    assert not isinstance(calls[0][1][1], int)


def test_rpc_call_not_configured_when_url_blank():
    result, error = _rpc_call("version", [], settings_getter=_getter({}))
    assert result is None
    assert error == "not_configured"


def test_rpc_call_maps_jsonrpc_error_field():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch(
        "resources.lib.nzbget_api._http_post_json",
        return_value='{"error": {"message": "bad"}}',
    ):
        result, error = _rpc_call("version", [], settings_getter=getter)
    assert result is None
    assert "bad" in error


def test_rpc_call_redacts_url_password_in_error():
    # A connection error that echoes the RPC URL with embedded userinfo must
    # not leak the password into the returned/logged message.
    getter = _getter({"nzbget_url": "http://user:supersecret@box:6789"})
    with patch(
        "resources.lib.nzbget_api._http_post_json",
        side_effect=OSError("refused http://user:supersecret@box:6789/jsonrpc"),
    ):
        result, error = _rpc_call("version", [], settings_getter=getter)
    assert result is None
    assert "supersecret" not in error


def test_group_status_returns_error_on_rpc_failure():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(None, "boom")):
        status = group_status(42, settings_getter=getter)
    assert status["present"] is False
    assert status["status"] == "ERROR"


def test_group_status_tolerates_string_sizes_and_non_dict_rows():
    getter = _getter({"nzbget_url": "http://box:6789"})
    groups = [
        None,
        {
            "NZBID": 42,
            "Status": "DOWNLOADING",
            "DownloadedSizeMB": "250",
            "FileSizeMB": "1000",
        },
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(groups, None)):
        status = group_status(42, settings_getter=getter)
    assert status["present"] is True
    assert status["percent"] == 25


def test_history_status_returns_empty_on_rpc_failure():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(None, "boom")):
        status = history_status(42, settings_getter=getter)
    assert status["present"] is False
    assert status["success"] is False


def test_history_status_tolerates_non_string_status_and_non_dict_rows():
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = ["junk", {"NZBID": 42, "Status": None, "DestDir": "/dl/x"}]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        status = history_status(42, settings_getter=getter)
    assert status["present"] is True
    assert status["success"] is False


def test_append_nzb_returns_error_when_rpc_errors():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._http_get", return_value="<nzb/>"), patch(
        "resources.lib.nzbget_api._rpc_call",
        return_value=(None, "401 auth"),
    ):
        nzbid, error = append_nzb("http://i/x.nzb", "X", settings_getter=getter)
    assert nzbid is None
    assert error == "401 auth"
