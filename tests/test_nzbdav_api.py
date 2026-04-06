# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
from unittest.mock import patch

from resources.lib.nzbdav_api import get_job_history, get_job_status, submit_nzb


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_submit_nzb_returns_nzo_id(mock_http, mock_settings):
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps(
        {"status": True, "nzo_ids": ["SABnzbd_nzo_abc123"]}
    )
    nzo_id = submit_nzb(
        "http://hydra:5076/getnzb/abc123?apikey=testkey", "The.Matrix.1999"
    )
    assert nzo_id == "SABnzbd_nzo_abc123"
    call_url = mock_http.call_args[0][0]
    assert "mode=addurl" in call_url
    assert "apikey=testkey" in call_url


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_submit_nzb_failure_returns_none(mock_http, mock_settings):
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps({"status": False, "nzo_ids": []})
    nzo_id = submit_nzb("http://hydra:5076/getnzb/abc123", "The.Matrix")
    assert nzo_id is None


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_submit_nzb_connection_error(mock_http, mock_settings):
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.side_effect = Exception("Connection refused")
    nzo_id = submit_nzb("http://hydra:5076/getnzb/abc123", "The.Matrix")
    assert nzo_id is None


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_status_downloading(mock_http, mock_settings):
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps(
        {
            "queue": {
                "slots": [
                    {
                        "nzo_id": "SABnzbd_nzo_abc123",
                        "status": "Downloading",
                        "percentage": "45",
                        "filename": "The.Matrix.1999.2160p",
                    }
                ]
            }
        }
    )
    status = get_job_status("SABnzbd_nzo_abc123")
    assert status["status"] == "Downloading"
    assert status["percentage"] == "45"


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_status_not_found(mock_http, mock_settings):
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps({"queue": {"slots": []}})
    status = get_job_status("SABnzbd_nzo_abc123")
    assert status is None


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_status_connection_error(mock_http, mock_settings):
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.side_effect = Exception("Connection refused")
    status = get_job_status("SABnzbd_nzo_abc123")
    assert status is None


# --- New tests ---


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_submit_nzb_special_characters_in_name(mock_http, mock_settings):
    """submit_nzb should handle special characters in the NZB name without crashing."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps(
        {"status": True, "nzo_ids": ["SABnzbd_nzo_special99"]}
    )
    nzb_name = "Spider-Man: No Way Home (2021) 1080p BluRay & extras"
    nzo_id = submit_nzb("http://hydra:5076/getnzb/special", nzb_name)
    assert (
        nzo_id == "SABnzbd_nzo_special99"
    ), "submit_nzb should return nzo_id even when name has special characters"
    call_url = mock_http.call_args[0][0]
    assert "mode=addurl" in call_url, "URL should contain addurl mode"
    # The name should appear URL-encoded somewhere in the request
    assert "nzbname=" in call_url, "URL should contain nzbname parameter"


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_status_multiple_slots_finds_correct(mock_http, mock_settings):
    """get_job_status should find the correct slot when multiple jobs are queued."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps(
        {
            "queue": {
                "slots": [
                    {
                        "nzo_id": "SABnzbd_nzo_other1",
                        "status": "Downloading",
                        "percentage": "20",
                        "filename": "Other.Movie.2024",
                    },
                    {
                        "nzo_id": "SABnzbd_nzo_target",
                        "status": "Queued",
                        "percentage": "0",
                        "filename": "The.Matrix.1999.2160p",
                    },
                    {
                        "nzo_id": "SABnzbd_nzo_other2",
                        "status": "Paused",
                        "percentage": "75",
                        "filename": "Another.Show.S01E01",
                    },
                ]
            }
        }
    )
    status = get_job_status("SABnzbd_nzo_target")
    assert status is not None, "Should find the target job among multiple slots"
    assert (
        status["status"] == "Queued"
    ), "Should return the status of the correct (target) slot"
    assert status["percentage"] == "0", "Percentage should match the target slot"


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_status_returns_correct_fields(mock_http, mock_settings):
    """get_job_status should return a dict with status, percentage, filename."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps(
        {
            "queue": {
                "slots": [
                    {
                        "nzo_id": "SABnzbd_nzo_fields_test",
                        "status": "Downloading",
                        "percentage": "62",
                        "filename": "Dune.Part.Two.2024.2160p",
                    }
                ]
            }
        }
    )
    status = get_job_status("SABnzbd_nzo_fields_test")
    assert status is not None
    assert "status" in status, "Result must contain 'status' key"
    assert "percentage" in status, "Result must contain 'percentage' key"
    assert "filename" in status, "Result must contain 'filename' key"
    assert status["status"] == "Downloading"
    assert status["percentage"] == "62"
    assert status["filename"] == "Dune.Part.Two.2024.2160p"


# --- get_job_history tests ---


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_history_completed(mock_http, mock_settings):
    """get_job_history returns dict with status/storage/name for a completed job."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps(
        {
            "history": {
                "slots": [
                    {
                        "nzo_id": "SABnzbd_nzo_hist1",
                        "status": "Completed",
                        "storage": (
                            "/mnt/nzbdav/completed-symlinks/"
                            "uncategorized/Send Help 2026"
                        ),
                        "name": "Send Help 2026 1080p",
                    }
                ]
            }
        }
    )
    result = get_job_history("SABnzbd_nzo_hist1")
    assert result is not None
    assert result["status"] == "Completed"
    assert (
        result["storage"]
        == "/mnt/nzbdav/completed-symlinks/uncategorized/Send Help 2026"
    )
    assert result["name"] == "Send Help 2026 1080p"
    call_url = mock_http.call_args[0][0]
    assert "mode=history" in call_url
    assert "apikey=testkey" in call_url


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_history_not_found(mock_http, mock_settings):
    """get_job_history returns None when job is not in history."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps({"history": {"slots": []}})
    result = get_job_history("SABnzbd_nzo_missing")
    assert result is None


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_history_connection_error(mock_http, mock_settings):
    """get_job_history returns None on connection error."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.side_effect = Exception("Connection refused")
    result = get_job_history("SABnzbd_nzo_abc123")
    assert result is None


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_submit_nzb_handles_malformed_response(mock_http, mock_settings):
    """submit_nzb handles malformed API responses gracefully."""
    mock_settings.return_value = ("http://nzbdav:3333", "testkey")
    # status false with null nzo_id
    mock_http.return_value = '{"status": false, "nzo_ids": [null]}'
    assert submit_nzb("http://nzb/test.nzb", "test") is None

    # empty nzo_ids list
    mock_http.return_value = '{"status": true, "nzo_ids": []}'
    assert submit_nzb("http://nzb/test.nzb", "test") is None

    # missing nzo_ids entirely
    mock_http.return_value = '{"status": true}'
    assert submit_nzb("http://nzb/test.nzb", "test") is None


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_history_finds_correct_slot(mock_http, mock_settings):
    """get_job_history finds the correct slot among multiple history entries."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps(
        {
            "history": {
                "slots": [
                    {
                        "nzo_id": "SABnzbd_nzo_other",
                        "status": "Completed",
                        "storage": (
                            "/mnt/nzbdav/completed-symlinks/uncategorized/Other Movie"
                        ),
                        "name": "Other Movie",
                    },
                    {
                        "nzo_id": "SABnzbd_nzo_target",
                        "status": "Completed",
                        "storage": (
                            "/mnt/nzbdav/completed-symlinks/uncategorized/Target Movie"
                        ),
                        "name": "Target Movie",
                    },
                ]
            }
        }
    )
    result = get_job_history("SABnzbd_nzo_target")
    assert result is not None
    assert result["name"] == "Target Movie"
    assert result["storage"].endswith("Target Movie")
