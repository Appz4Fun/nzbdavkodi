# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
from unittest.mock import patch

from resources.lib.nzbdav_api import (
    _DEFAULT_SUBMIT_TIMEOUT,
    _get_submit_timeout,
    _sanitize_server_message,
    cancel_job,
    get_completed_names,
    get_job_history,
    get_job_status,
    submit_nzb,
)


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


@patch("resources.lib.nzbdav_api._get_submit_timeout")
@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_submit_nzb_passes_configured_timeout(mock_http, mock_settings, mock_timeout):
    """submit_nzb should read the submit_timeout setting and pass it to http_get."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_timeout.return_value = 90
    mock_http.return_value = json.dumps({"status": True, "nzo_ids": ["nzo_1"]})

    submit_nzb("http://hydra:5076/getnzb/abc123", "Test")

    assert mock_http.call_args.kwargs["timeout"] == 90


@patch("resources.lib.nzbdav_api.xbmcaddon")
def test_get_submit_timeout_reads_setting(mock_xbmcaddon):
    """_get_submit_timeout returns the parsed setting value."""
    mock_xbmcaddon.Addon.return_value.getSetting.return_value = "120"
    assert _get_submit_timeout() == 120


@patch("resources.lib.nzbdav_api.xbmcaddon")
def test_get_submit_timeout_falls_back_on_empty(mock_xbmcaddon):
    """_get_submit_timeout returns the default when the setting is empty."""
    mock_xbmcaddon.Addon.return_value.getSetting.return_value = ""
    assert _get_submit_timeout() == _DEFAULT_SUBMIT_TIMEOUT


@patch("resources.lib.nzbdav_api.xbmcaddon")
def test_get_submit_timeout_falls_back_on_garbage(mock_xbmcaddon):
    """_get_submit_timeout returns the default when the setting is non-numeric."""
    mock_xbmcaddon.Addon.return_value.getSetting.return_value = "not a number"
    assert _get_submit_timeout() == _DEFAULT_SUBMIT_TIMEOUT


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


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_completed_names_returns_set(mock_http, mock_settings):
    """get_completed_names returns a set of completed download names."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = json.dumps(
        {
            "history": {
                "slots": [
                    {"name": "Movie.A.2024", "status": "Completed"},
                    {"name": "Movie.B.2023", "status": "Completed"},
                    {"name": "Movie.C.2022", "status": "Failed"},
                ]
            }
        }
    )
    names = get_completed_names()
    assert names == {"Movie.A.2024", "Movie.B.2023"}


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_completed_names_returns_empty_on_error(mock_http, mock_settings):
    """get_completed_names returns empty set on connection error."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.side_effect = Exception("Connection refused")
    names = get_completed_names()
    assert names == set()


# --- _sanitize_server_message tests ---


def test_sanitize_empty_string():
    assert _sanitize_server_message("") == ""


def test_sanitize_none():
    """Defensive: don't crash when passed None (e.g., from a missing body)."""
    assert _sanitize_server_message(None) == ""


def test_sanitize_plain_text():
    assert _sanitize_server_message("  hello world  ") == "hello world"


def test_sanitize_strips_html_tags():
    assert _sanitize_server_message("<b>bold</b> text") == "bold text"


def test_sanitize_collapses_whitespace():
    assert _sanitize_server_message("a\n\nb\t\tc") == "a b c"


def test_sanitize_handles_nested_tags():
    assert _sanitize_server_message("<div><p>x</p></div>") == "x"


def test_sanitize_returns_empty_when_only_whitespace():
    assert _sanitize_server_message("   \n\t  ") == ""


# --- cancel_job tests ---


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_cancel_job_succeeds_on_queue(mock_http, mock_settings):
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = '{"status":true,"error":null}'

    result = cancel_job("nzo_xyz")

    assert result is True
    assert mock_http.call_count == 1
    called_url = mock_http.call_args[0][0]
    assert "mode=queue" in called_url
    assert "name=delete" in called_url
    assert "value=nzo_xyz" in called_url


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_cancel_job_returns_false_when_not_in_queue(mock_http, mock_settings):
    """When nzbdav reports the job isn't in the queue (e.g. it raced into
    history before our cleanup ran), cancel_job returns False but does
    NOT treat it as an error — this is the normal race case."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = '{"status":false,"error":"Unrecognized Guid format."}'

    result = cancel_job("nzo_xyz")

    assert result is False
    assert mock_http.call_count == 1


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_cancel_job_returns_false_on_network_error(mock_http, mock_settings):
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.side_effect = Exception("connection refused")

    result = cancel_job("nzo_xyz")

    assert result is False


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_cancel_job_returns_false_on_settings_error(mock_http, mock_settings):
    mock_settings.side_effect = Exception("settings unavailable")

    result = cancel_job("nzo_xyz")

    assert result is False
    assert mock_http.call_count == 0  # _http_get never called


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_cancel_job_uses_default_3s_timeout(mock_http, mock_settings):
    """The default timeout is 3 seconds — short enough to feel responsive
    on user cancel and Kodi shutdown."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = '{"status":true}'

    cancel_job("nzo_xyz")

    # _http_get is called with timeout=3 as a keyword argument
    assert mock_http.call_args.kwargs["timeout"] == 3


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_cancel_job_respects_custom_timeout(mock_http, mock_settings):
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = '{"status":true}'

    cancel_job("nzo_xyz", timeout=10)

    assert mock_http.call_args.kwargs["timeout"] == 10


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_cancel_job_does_not_touch_history(mock_http, mock_settings):
    """cancel_job is queue-only by design. The spec deliberately does
    NOT call mode=history&name=delete because erasing history on a
    race-into-terminal-state would contradict the Group B 'preserve
    failure history' rationale."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = '{"status":true}'

    cancel_job("nzo_xyz")

    # Exactly one call (queue), not two (queue + history)
    assert mock_http.call_count == 1
    called_url = mock_http.call_args[0][0]
    assert "mode=queue" in called_url
    assert "mode=history" not in called_url
