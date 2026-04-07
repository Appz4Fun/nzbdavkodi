# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import patch
from urllib.error import URLError

from resources.lib.nzbdav_api import get_job_status, submit_nzb


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_submit_nzb_returns_none_on_malformed_json(mock_http, mock_settings):
    """submit_nzb() should return None (not raise) when response is not valid JSON."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.return_value = "{this is not json"

    result = submit_nzb("http://hydra/getnzb/badjson", "Bad.JSON")

    assert result is None


@patch("resources.lib.nzbdav_api._get_settings")
@patch("resources.lib.nzbdav_api._http_get")
def test_get_job_status_returns_none_on_connection_error(mock_http, mock_settings):
    """get_job_status() should return None (not raise) on network error."""
    mock_settings.return_value = ("http://nzbdav:3000", "testkey")
    mock_http.side_effect = URLError("Connection timed out")

    status = get_job_status("SABnzbd_nzo_timeout")

    assert status is None
