# SPDX-License-Identifier: GPL-3.0-or-later
import base64
from unittest.mock import patch

from resources.lib.nzbget_api import (
    _get_settings,
    _rpc_call,
    _rpc_url,
    append_nzb,
    cancel_job,
    completed_history,
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


def test_history_status_preserves_exact_nzbid_and_job_name():
    row = {
        "NZBID": 42,
        "Name": "Spider-Noir.S01.2160p",
        "Status": "SUCCESS/ALL",
        "FinalDir": "/downloads/tv/Spider-Noir",
    }
    with patch("resources.lib.nzbget_api._rpc_call", return_value=([row], None)):
        result = history_status(42)

    assert result["nzbid"] == 42
    assert result["job_name"] == "Spider-Noir.S01.2160p"


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


def test_append_nzb_defaults_leave_dupe_fields_neutral():
    """Absent dupe args, the append is byte-for-byte the pre-#372 single submit."""
    getter = _getter({"nzbget_url": "http://box:6789"})
    captured = {}

    def fake_post(url, payload, timeout=0, basic_auth=None):
        captured["payload"] = payload
        return '{"result": 7, "error": null}'

    with patch("resources.lib.nzbget_api._http_get", return_value="<nzb/>"), patch(
        "resources.lib.nzbget_api._http_post_json", side_effect=fake_post
    ):
        nzbid, error = append_nzb("http://i/x.nzb", "X", settings_getter=getter)

    assert (nzbid, error) == (7, None)
    params = captured["payload"]["params"]
    assert params[6] == ""  # DupeKey
    assert params[7] == 0  # DupeScore
    assert params[8] == "SCORE"  # DupeMode


def test_append_nzb_sends_dupe_key_score_and_mode():
    """A Smart-Duplicates submission carries the shared DupeKey, its DupeScore,
    and DupeMode so NZBGet groups the release and picks the highest score."""
    getter = _getter({"nzbget_url": "http://box:6789"})
    captured = {}

    def fake_post(url, payload, timeout=0, basic_auth=None):
        captured["payload"] = payload
        return '{"result": 9, "error": null}'

    with patch("resources.lib.nzbget_api._http_get", return_value="<nzb/>"), patch(
        "resources.lib.nzbget_api._http_post_json", side_effect=fake_post
    ):
        nzbid, error = append_nzb(
            "http://i/x.nzb",
            "X",
            settings_getter=getter,
            dupe_key="imdb=1234567",
            dupe_score=100,
            dupe_mode="SCORE",
        )

    assert (nzbid, error) == (9, None)
    params = captured["payload"]["params"]
    assert params[6] == "imdb=1234567"  # DupeKey
    assert params[7] == 100  # DupeScore
    assert params[8] == "SCORE"  # DupeMode


def test_config_option_reads_named_value_lowercased():
    """config() returns [{Name,Value}]; read one option (e.g. HealthCheck)."""
    from resources.lib.nzbget_api import config_option

    getter = _getter({"nzbget_url": "http://box:6789"})
    cfg = [
        {"Name": "MainDir", "Value": "/downloads"},
        {"Name": "HealthCheck", "Value": "Pause"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(cfg, None)):
        assert config_option("HealthCheck", settings_getter=getter) == "pause"
        assert config_option("healthcheck", settings_getter=getter) == "pause"
    # Absent option / RPC error -> None (best-effort).
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(cfg, None)):
        assert config_option("Missing", settings_getter=getter) is None
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(None, "boom")):
        assert config_option("HealthCheck", settings_getter=getter) is None


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


def test_get_settings_uses_schema_defaults_via_injected_getter():
    # _get_script_setting returns the supplied default for settings the user
    # left at their schema default (absent from the profile file). Those
    # fallbacks must match settings.xml, or a user who only set the SMB root is
    # sent down the NZBGet path and fails "not configured" on a blank URL.
    def getter(key, default=""):
        return {"nzbget_smb_root": "smb://host/done"}.get(key, default)

    url, user, password, category = _get_settings(settings_getter=getter)
    assert url == "http://localhost:6789"
    assert user == "nzbget"
    assert password == ""
    assert category == ""


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


def test_rpc_call_places_id_before_params():
    # NZBGet's legacy parser can mis-read fields after ``params``; the payload
    # must serialize ``id`` before ``params``.
    captured = {}

    def fake_post(url, payload, timeout=0, basic_auth=None):
        captured["payload"] = payload
        return '{"result": 1, "error": null}'

    with patch("resources.lib.nzbget_api._http_post_json", side_effect=fake_post):
        _rpc_call("version", [], settings_getter=_getter({"nzbget_url": "http://box"}))
    keys = list(captured["payload"].keys())
    assert keys.index("id") < keys.index("params")


def test_group_status_matches_string_nzbid():
    # NZBGet may serialize NZBID as a string; an int caller must still match.
    getter = _getter({"nzbget_url": "http://box"})
    groups = [
        {
            "NZBID": "42",
            "Status": "DOWNLOADING",
            "DownloadedSizeMB": 100,
            "FileSizeMB": 200,
        }
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(groups, None)):
        status = group_status(42, settings_getter=getter)
    assert status["present"] is True


def test_history_status_matches_string_nzbid():
    getter = _getter({"nzbget_url": "http://box"})
    hist = [{"NZBID": "42", "Status": "SUCCESS/ALL", "DestDir": "/dl/X"}]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        status = history_status(42, settings_getter=getter)
    assert status["present"] is True
    assert status["success"] is True


def test_completed_base_dir_returns_config_destdir():
    from resources.lib.nzbget_api import completed_base_dir

    getter = _getter({"nzbget_url": "http://box"})
    cfg = [
        {"Name": "MainDir", "Value": "/downloads"},
        {"Name": "DestDir", "Value": "/downloads/completed"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(cfg, None)):
        assert completed_base_dir(settings_getter=getter) == "/downloads/completed"


def test_completed_base_dir_none_for_unexpanded_template():
    from resources.lib.nzbget_api import completed_base_dir

    getter = _getter({"nzbget_url": "http://box"})
    cfg = [{"Name": "DestDir", "Value": "${MainDir}/completed"}]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(cfg, None)):
        assert completed_base_dir(settings_getter=getter) is None


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
    # An explicitly-blank URL short-circuits to "not_configured" before any
    # HTTP. (An *absent* nzbget_url now falls back to the schema default
    # http://localhost:6789 — see test_get_settings_uses_schema_defaults_*.)
    result, error = _rpc_call(
        "version", [], settings_getter=_getter({"nzbget_url": ""})
    )
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


def test_completed_history_keys_success_items_by_name():
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [
        {
            "NZBID": 1,
            "Name": "Movie.mkv",
            "Status": "SUCCESS/UNPACK",
            "FileSizeHi": 1,
            "FileSizeLo": 5,
            "DestDir": "/dl/intermediate/Movie",
            "FinalDir": "/dl/movies/Movie",
        },
        {"NZBID": 2, "Name": "Failed.mkv", "Status": "FAILURE/PAR"},
        {"NZBID": 3, "Name": "Dupe.mkv", "Status": "DELETED/DUPE"},
        {"NZBID": 4, "Name": "Warn.mkv", "Status": "WARNING/HEALTH"},
        "junk",
        {"NZBID": 5, "Status": "SUCCESS/ALL"},  # nameless -> skipped
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)) as rpc:
        jobs = completed_history(settings_getter=getter)

    # Only SUCCESS/* rows are "pre-cached": only their completed files can be
    # reused on selection, so they're the only ones the picker may tag DL.
    assert set(jobs) == {"Movie.mkv"}
    assert jobs["Movie.mkv"]["status"] == "SUCCESS/UNPACK"
    # Exact 64-bit size from the Hi/Lo pair.
    assert jobs["Movie.mkv"]["bytes"] == (1 << 32) + 5
    assert jobs["Movie.mkv"]["name"] == "Movie.mkv"
    assert jobs["Movie.mkv"]["nzbid"] == 1
    # FinalDir wins over DestDir (post-processing-script move), same
    # preference as history_status.
    assert jobs["Movie.mkv"]["dest_dir"] == "/dl/movies/Movie"
    # Successful lookup is marked done even after filtering.
    assert getattr(jobs, "_lookup_done", False) is True
    # Visible history only, same shape history_status uses.
    assert rpc.call_args[0][0] == "history"
    assert rpc.call_args[0][1] == [False]
    # Picker-render path: bounded like nzbdav's 10s picker timeout, not the
    # 30s resolver-path _RPC_TIMEOUT.
    assert rpc.call_args.kwargs.get("timeout") == 10


def test_completed_history_never_raises_on_broken_settings_getter():
    # _tag_available's plugin call sites have no try/except; a raising
    # injected getter must degrade to "no tags", not crash the picker.
    def broken_getter(key, default=""):
        raise ValueError("corrupt settings")

    jobs = completed_history(settings_getter=broken_getter)
    assert jobs == {}
    assert getattr(jobs, "_lookup_done", False) is False


def test_completed_history_falls_back_to_mb_and_tolerates_strings():
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [
        {
            "Name": "Movie.mkv",
            "Status": "SUCCESS/ALL",
            "FileSizeHi": "0",
            "FileSizeLo": None,
            "FileSizeMB": "2048",
        }
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        jobs = completed_history(settings_getter=getter)
    assert jobs["Movie.mkv"]["bytes"] == 2048 * 1048576


def test_completed_history_unknown_size_is_none():
    # No size fields at all -> bytes None, so the router's size gate fails
    # open (name-only match) instead of treating it as a zero-byte mismatch.
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [{"Name": "Movie.mkv", "Status": "SUCCESS/ALL"}]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        jobs = completed_history(settings_getter=getter)
    assert jobs["Movie.mkv"]["bytes"] is None


def test_completed_history_rpc_failure_returns_unmarked_empty():
    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(None, "401")):
        jobs = completed_history(settings_getter=getter)
    assert jobs == {}
    assert getattr(jobs, "_lookup_done", False) is False

    # A non-list result is malformed, not an empty history.
    with patch("resources.lib.nzbget_api._rpc_call", return_value=({"x": 1}, None)):
        jobs = completed_history(settings_getter=getter)
    assert jobs == {}
    assert getattr(jobs, "_lookup_done", False) is False


def test_completed_history_keeps_newest_same_name_entry():
    # NZBGet returns history newest-first; for same-name SUCCESS rows the
    # newest row is what a replay would land on, so keep its size.
    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [
        {"Name": "Movie.mkv", "Status": "SUCCESS/ALL", "FileSizeMB": 100},
        {"Name": "Movie.mkv", "Status": "SUCCESS/ALL", "FileSizeMB": 999},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        jobs = completed_history(settings_getter=getter)
    assert jobs["Movie.mkv"]["bytes"] == 100 * 1048576


def test_active_group_by_dupekey_finds_unpaused_promoted_backup():
    from resources.lib.nzbget_api import active_group_by_dupekey

    getter = _getter({"nzbget_url": "http://box:6789"})
    groups = [
        {"NZBID": 5, "DupeKey": "k", "Status": "PAUSED", "FileSizeMB": 100},
        {
            "NZBID": 9,
            "DupeKey": "K",  # case-insensitive match
            "Status": "DOWNLOADING",
            "DownloadedSizeMB": 50,
            "FileSizeMB": 100,
        },
        {"NZBID": 3, "DupeKey": "other", "Status": "DOWNLOADING"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(groups, None)):
        g = active_group_by_dupekey("k", settings_getter=getter)
    assert g["present"] is True
    assert g["nzbid"] == 9  # the unpaused one, not the PAUSED 5 or other-key 3
    assert g["percent"] == 50


def test_active_group_by_dupekey_absent_when_only_paused_or_error():
    from resources.lib.nzbget_api import active_group_by_dupekey

    getter = _getter({"nzbget_url": "http://box:6789"})
    with patch(
        "resources.lib.nzbget_api._rpc_call",
        return_value=([{"NZBID": 5, "DupeKey": "k", "Status": "PAUSED"}], None),
    ):
        assert active_group_by_dupekey("k", settings_getter=getter)["present"] is False
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(None, "boom")):
        assert active_group_by_dupekey("k", settings_getter=getter)["present"] is False
    assert active_group_by_dupekey("", settings_getter=getter)["present"] is False


def test_active_group_by_name_matches_case_insensitively_and_excludes_id():
    from resources.lib.nzbget_api import active_group_by_name

    getter = _getter({"nzbget_url": "http://box:6789"})
    groups = [
        {"NZBID": 2, "NZBName": "the.movie"},  # case-insensitive match
        {"NZBID": 3, "NZBName": "Other.Movie"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(groups, None)):
        assert active_group_by_name("The.Movie", settings_getter=getter) is True
        # Excluding the only matching id leaves nothing.
        assert (
            active_group_by_name("The.Movie", exclude_nzbid=2, settings_getter=getter)
            is False
        )
        assert active_group_by_name("Unrelated", settings_getter=getter) is False


def test_active_group_by_name_absent_on_blank_name_or_rpc_error():
    from resources.lib.nzbget_api import active_group_by_name

    getter = _getter({"nzbget_url": "http://box:6789"})
    assert active_group_by_name("", settings_getter=getter) is False
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(None, "boom")):
        assert active_group_by_name("The.Movie", settings_getter=getter) is False


def test_history_success_by_dupekey_returns_completed_member():
    from resources.lib.nzbget_api import history_success_by_dupekey

    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [
        {"NZBID": 1, "DupeKey": "k", "Status": "FAILURE/HEALTH"},
        {"NZBID": 2, "DupeKey": "k", "Status": "SUCCESS/ALL", "DestDir": "/dl/X"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        s = history_success_by_dupekey("k", settings_getter=getter)
    assert s["present"] is True
    assert s["nzbid"] == 2
    assert s["dest_dir"] == "/dl/X"


def test_cancel_jobs_deletes_history_then_queue_and_skips_empty():
    # cancel_jobs final-deletes a batch of NZBIDs -- history first (parked DUP
    # backups deleted so nothing can be promoted), then the queue -- and is a
    # no-op on empty input (round-3 review: id-scoped post-cancel cleanup).
    from resources.lib.nzbget_api import cancel_jobs

    getter = _getter({"nzbget_url": "http://box:6789"})
    calls = []

    def fake_rpc(method, params, settings_getter=None):
        calls.append((method, list(params)))
        return (None, None)

    with patch("resources.lib.nzbget_api._rpc_call", side_effect=fake_rpc):
        cancel_jobs([7, None, 9], settings_getter=getter)
    assert calls == [
        ("editqueue", ["HistoryFinalDelete", "", [7, 9]]),
        ("editqueue", ["GroupFinalDelete", "", [7, 9]]),
    ]
    calls.clear()
    with patch("resources.lib.nzbget_api._rpc_call", side_effect=fake_rpc):
        cancel_jobs([], settings_getter=getter)
        cancel_jobs(None, settings_getter=getter)
    assert not calls  # empty input -> no RPC round-trips


def test_active_group_by_dupekey_reports_paused_presence():
    # A same-key member queued PAUSED (e.g. NZBGet globally paused when the
    # backup was promoted) is not a promotion -- but it is not an exhausted
    # group either. The scan reports it so the poll can keep waiting instead of
    # declaring FAILURE/DUPE (round-3 review finding). The excluded (just
    # failed) member's own paused row does NOT count.
    from resources.lib.nzbget_api import active_group_by_dupekey

    getter = _getter({"nzbget_url": "http://box:6789"})
    paused_other = {"NZBID": 9, "DupeKey": "k", "Status": "PAUSED"}
    paused_failed = {"NZBID": 1, "DupeKey": "k", "Status": "PAUSED"}
    with patch(
        "resources.lib.nzbget_api._rpc_call",
        return_value=([paused_failed, paused_other], None),
    ):
        got = active_group_by_dupekey("k", exclude_nzbid=1, settings_getter=getter)
    assert got["present"] is False
    assert got["paused_present"] is True
    with patch(
        "resources.lib.nzbget_api._rpc_call", return_value=([paused_failed], None)
    ):
        got = active_group_by_dupekey("k", exclude_nzbid=1, settings_getter=getter)
    assert got["present"] is False
    assert got["paused_present"] is False  # only the excluded member is paused


def test_history_success_by_dupekey_excludes_stale_ids():
    from resources.lib.nzbget_api import history_success_by_dupekey

    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [
        {"NZBID": 3, "DupeKey": "k", "Status": "SUCCESS/ALL", "DestDir": "/old"},
        {"NZBID": 9, "DupeKey": "k", "Status": "SUCCESS/ALL", "DestDir": "/new"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        got = history_success_by_dupekey(
            "k", exclude_nzbids=(3,), settings_getter=getter
        )
    assert got["present"] is True and got["nzbid"] == 9
    with patch("resources.lib.nzbget_api._rpc_call", return_value=([hist[0]], None)):
        got = history_success_by_dupekey(
            "k", exclude_nzbids=(3,), settings_getter=getter
        )
    assert got["present"] is False


def test_success_ids_by_dupekey_lists_matching_success_rows():
    from resources.lib.nzbget_api import success_ids_by_dupekey

    getter = _getter({"nzbget_url": "http://box:6789"})
    hist = [
        {"NZBID": 3, "DupeKey": "k", "Status": "SUCCESS/ALL"},
        {"NZBID": 4, "DupeKey": "k", "Status": "FAILURE/PAR"},
        {"NZBID": 5, "DupeKey": "other", "Status": "SUCCESS/ALL"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(hist, None)):
        assert success_ids_by_dupekey("k", settings_getter=getter) == [3]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(None, "boom")):
        assert not success_ids_by_dupekey("k", settings_getter=getter)


def test_active_group_by_dupekey_collects_paused_nzbids():
    # A promotion that lands while NZBGet is paused never becomes the tracked
    # member -- the cancel path still needs its NZBID, so the scan reports the
    # paused same-key ids alongside paused_present (round-5 review finding).
    from resources.lib.nzbget_api import active_group_by_dupekey

    getter = _getter({"nzbget_url": "http://box:6789"})
    rows = [
        {"NZBID": 1, "DupeKey": "k", "Status": "PAUSED"},  # excluded (failed pick)
        {"NZBID": 9, "DupeKey": "k", "Status": "PAUSED"},
        {"NZBID": 12, "DupeKey": "k", "Status": "PAUSED"},
    ]
    with patch("resources.lib.nzbget_api._rpc_call", return_value=(rows, None)):
        got = active_group_by_dupekey("k", exclude_nzbid=1, settings_getter=getter)
    assert got["present"] is False
    assert got["paused_present"] is True
    assert got["paused_nzbids"] == [9, 12]


def test_cancel_jobs_coerces_and_dedups_string_nzbids():
    # listgroups can serialize NZBIDs as strings (the module tolerates that
    # via _same_nzbid elsewhere); editqueue expects an integer ID array, so
    # the batch deleter must coerce -- and dedup across types -- or one string
    # member fails the whole HistoryFinalDelete/GroupFinalDelete batch
    # (round-6 review finding).
    from resources.lib.nzbget_api import cancel_jobs

    getter = _getter({"nzbget_url": "http://box:6789"})
    calls = []

    def fake_rpc(method, params, settings_getter=None):
        calls.append((method, list(params)))
        return (None, None)

    with patch("resources.lib.nzbget_api._rpc_call", side_effect=fake_rpc):
        cancel_jobs(["9", 9, 7, None, "junk"], settings_getter=getter)
    assert calls == [
        ("editqueue", ["HistoryFinalDelete", "", [9, 7]]),
        ("editqueue", ["GroupFinalDelete", "", [9, 7]]),
    ]
