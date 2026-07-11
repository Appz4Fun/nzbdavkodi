# SPDX-License-Identifier: GPL-3.0-or-later

"""Tri-state exact completed-history lookup API tests."""

import json
from unittest.mock import patch

from resources.lib import nzbdav_api, nzbget_api


def _getter(key, default=""):
    return {
        "nzbdav_url": "http://dav",
        "nzbdav_api_key": "secret",
        "nzbget_url": "http://get:6789",
        "nzbget_username": "user",
        "nzbget_password": "pw",
    }.get(key, default)


def test_nzbdav_exact_lookup_distinguishes_found_missing_and_transient():
    payload = {
        "history": {
            "slots": [
                {
                    "nzo_id": "same-name-other-id",
                    "name": "Spider-Noir.S01",
                    "status": "Completed",
                    "storage": "/other",
                },
                {
                    "nzo_id": "wanted",
                    "name": "Spider-Noir.S01",
                    "status": "Completed",
                    "storage": "/wanted",
                },
            ]
        }
    }
    with patch.object(nzbdav_api, "_http_get", return_value=json.dumps(payload)):
        found = nzbdav_api.lookup_completed_job_exact("wanted", _getter)
        missing = nzbdav_api.lookup_completed_job_exact("absent", _getter)
    assert found.state == "valid"
    assert found.job["storage"] == "/wanted"
    assert missing.state == "stale"

    with patch.object(nzbdav_api, "_http_get", side_effect=OSError("offline")):
        assert nzbdav_api.lookup_completed_job_exact("wanted", _getter).state == (
            "transient"
        )
    with patch.object(nzbdav_api, "_http_get", return_value='{"history": null}'):
        assert nzbdav_api.lookup_completed_job_exact("wanted", _getter).state == (
            "transient"
        )


def test_nzbget_exact_lookup_never_substitutes_duplicate_name():
    history = [
        {
            "NZBID": 42,
            "Name": "Spider-Noir.S01",
            "Status": "SUCCESS/ALL",
            "FinalDir": "/downloads/other",
        },
        {
            "NZBID": 41,
            "Name": "Spider-Noir.S01",
            "Status": "SUCCESS/ALL",
            "FinalDir": "/downloads/wanted",
        },
    ]
    with patch.object(nzbget_api, "_rpc_call", return_value=(history, None)):
        found = nzbget_api.lookup_completed_job_exact("41", _getter)
        missing = nzbget_api.lookup_completed_job_exact("99", _getter)
    assert found.state == "valid"
    assert found.job["dest_dir"] == "/downloads/wanted"
    assert missing.state == "stale"

    with patch.object(nzbget_api, "_rpc_call", return_value=(None, "timeout")):
        assert nzbget_api.lookup_completed_job_exact("41", _getter).state == (
            "transient"
        )
    with patch.object(nzbget_api, "_rpc_call", return_value=({}, None)):
        assert nzbget_api.lookup_completed_job_exact("41", _getter).state == (
            "transient"
        )


def test_noncompleted_exact_jobs_are_conclusively_stale():
    nzbget_history = [
        {"NZBID": 41, "Status": "FAILURE/HEALTH", "DestDir": "/downloads/show"}
    ]
    with patch.object(nzbget_api, "_rpc_call", return_value=(nzbget_history, None)):
        assert nzbget_api.lookup_completed_job_exact(41, _getter).state == "stale"

    payload = {
        "history": {
            "slots": [
                {
                    "nzo_id": "wanted",
                    "status": "Failed",
                    "storage": "/downloads/show",
                }
            ]
        }
    }
    with patch.object(nzbdav_api, "_http_get", return_value=json.dumps(payload)):
        assert nzbdav_api.lookup_completed_job_exact("wanted", _getter).state == (
            "stale"
        )


def test_malformed_nzbget_history_cannot_prove_exact_job_missing_or_stale():
    malformed_payloads = (
        ["broken-row"],
        [{"Name": "Spider-Noir.S01", "Status": "SUCCESS/ALL"}],
        [{"NZBID": 41, "DestDir": "/downloads/show"}],
        [{"NZBID": 41, "Status": "SUCCESS/ALL"}],
    )
    for history in malformed_payloads:
        with patch.object(nzbget_api, "_rpc_call", return_value=(history, None)):
            result = nzbget_api.lookup_completed_job_exact(41, _getter)
        assert result.state == "transient"


def test_nzbget_exact_success_status_is_token_bounded():
    row = {
        "NZBID": 41,
        "Status": "SUCCESSFUL/ALL",
        "DestDir": "/downloads/show",
    }
    with patch.object(nzbget_api, "_rpc_call", return_value=([row], None)):
        assert nzbget_api.lookup_completed_job_exact(41, _getter).state == "stale"

    for status in ("SUCCESS", "SUCCESS/ALL"):
        row["Status"] = status
        with patch.object(nzbget_api, "_rpc_call", return_value=([dict(row)], None)):
            assert nzbget_api.lookup_completed_job_exact(41, _getter).state == "valid"


def test_malformed_nzbdav_history_cannot_prove_exact_job_missing_or_stale():
    malformed_slots = (
        ["broken-row"],
        [{"name": "Spider-Noir.S01", "status": "Completed"}],
        [{"nzo_id": "wanted", "storage": "/downloads/show"}],
        [{"nzo_id": "wanted", "status": "Completed"}],
    )
    for slots in malformed_slots:
        payload = {"history": {"slots": slots}}
        with patch.object(nzbdav_api, "_http_get", return_value=json.dumps(payload)):
            result = nzbdav_api.lookup_completed_job_exact("wanted", _getter)
        assert result.state == "transient"


def test_conclusive_failure_status_does_not_require_completed_folder_fields():
    with patch.object(
        nzbget_api,
        "_rpc_call",
        return_value=([{"NZBID": 41, "Status": "FAILURE/HEALTH"}], None),
    ):
        assert nzbget_api.lookup_completed_job_exact(41, _getter).state == "stale"

    payload = {"history": {"slots": [{"nzo_id": "wanted", "status": "Failed"}]}}
    with patch.object(nzbdav_api, "_http_get", return_value=json.dumps(payload)):
        assert nzbdav_api.lookup_completed_job_exact("wanted", _getter).state == (
            "stale"
        )
