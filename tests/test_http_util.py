# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

from resources.lib.http_util import http_get, notify


@patch("resources.lib.http_util.urlopen")
def test_http_get_returns_decoded_response(mock_urlopen):
    """http_get should return decoded UTF-8 string."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"status": true}'
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    result = http_get("http://example.com/api")
    assert result == '{"status": true}'


@patch("resources.lib.http_util.urlopen")
def test_http_get_passes_timeout(mock_urlopen):
    """http_get should forward the timeout argument to urlopen."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    http_get("http://example.com/api", timeout=30)
    _, kwargs = mock_urlopen.call_args
    assert kwargs.get("timeout") == 30


def test_notify_does_not_crash():
    """notify should call xbmc.executebuiltin without error."""
    notify("Test", "Message", 3000)


def test_notify_default_duration_does_not_crash():
    """notify should work with default duration."""
    notify("Heading", "Body")
