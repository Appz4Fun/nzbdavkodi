# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch

from resources.lib.http_util import http_get, notify, redact_url


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
def test_http_get_sends_user_agent(mock_urlopen):
    """http_get should identify itself instead of using urllib's default UA."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    http_get("http://example.com/api")

    request = mock_urlopen.call_args[0][0]
    assert request.get_header("User-agent") == "NZB-DAV Kodi Addon"


@patch("resources.lib.http_util.urlopen")
def test_http_get_rejects_non_success_status_without_http_error(mock_urlopen):
    """If a custom opener returns a response object for 5xx, reject it."""
    import pytest

    mock_resp = MagicMock()
    mock_resp.getcode.return_value = 503
    mock_resp.read.return_value = b"unavailable"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    with pytest.raises(OSError, match="HTTP status 503"):
        http_get("http://example.com/api")


@patch("resources.lib.http_util.urlopen")
def test_http_get_replaces_invalid_utf8(mock_urlopen):
    """Bad upstream bytes should not escape as UnicodeDecodeError."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"ok\xff"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    assert http_get("http://example.com/api") == "ok\ufffd"


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


@patch("resources.lib.http_util.urlopen")
def test_http_get_rejects_non_http_schemes(mock_urlopen):
    """TODO.md §H.2-H14: http_get must reject file:// / ftp:// schemes
    so a misconfigured URL setting can't read /etc/passwd via urllib's
    default opener. urlopen should never be invoked for these."""
    import pytest

    for bad_url in (
        "file:///etc/passwd",
        "ftp://anonymous@example.com/etc/passwd",
        "gopher://example.com/",
        "data:text/plain,hello",
    ):
        with pytest.raises(ValueError):
            http_get(bad_url)
    assert mock_urlopen.call_count == 0


@patch("resources.lib.http_util.urlopen")
def test_http_get_accepts_http_and_https(mock_urlopen):
    """The scheme guard must not break the legitimate http/https paths."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    assert http_get("http://example.com/api") == "ok"
    assert http_get("https://example.com/api") == "ok"
    assert mock_urlopen.call_count == 2


def test_notify_does_not_crash():
    """notify should call xbmc.executebuiltin without error."""
    notify("Test", "Message", 3000)


def test_notify_default_duration_does_not_crash():
    """notify should work with default duration."""
    notify("Heading", "Body")


def test_notify_escapes_builtin_metacharacters():
    """notify must not let a `,` or `)` in heading/message break out of
    the Notification(...) builtin call. TODO.md §H.2-H15 / §H.3 fix.

    The previous implementation interpolated the upstream-controlled
    text directly into the executebuiltin string, so an apikey-bearing
    error like "HTTP 401, key=abc)" would terminate the Notification
    call early and let the rest run as a separate builtin. The escape
    maps the two structural metacharacters to visually-similar Unicode
    that the Kodi parser treats as inert characters.
    """
    import sys

    captured = []
    saved = sys.modules["xbmc"].executebuiltin
    sys.modules["xbmc"].executebuiltin = captured.append
    try:
        notify("Header, with ),; tricks", "Body, also ); evil", 3000)
    finally:
        sys.modules["xbmc"].executebuiltin = saved

    assert len(captured) == 1
    cmd = captured[0]
    # The injected commas/parens from heading/message are gone.
    assert "Header, with " not in cmd
    assert "Body, also );" not in cmd
    # And the escaped lookalikes are present in their stead.
    assert "،" in cmd or "❩" in cmd


def test_redact_url_hides_apikey():
    """redact_url should replace apikey values with ***."""
    url = "http://hydra:5076/api?apikey=secretkey123&t=movie&imdbid=tt1234567"
    result = redact_url(url)
    assert "secretkey123" not in result
    assert "apikey=REDACTED" in result
    assert "t=movie" in result
    assert "imdbid=tt1234567" in result


def test_redact_url_preserves_url_without_apikey():
    """redact_url should pass through URLs without apikey unchanged."""
    url = "http://example.com/api?mode=history&limit=200"
    result = redact_url(url)
    assert result == url


def test_redact_url_hides_extended_credential_keys():
    """TODO.md §H.2-H2c: the redaction set covers more than just apikey.
    `key`, `access_token`, `bearer`, `session`, `sessionid`, `password`,
    `passwd`, `token`, `auth`, `secret` should all be redacted."""
    for keyword in (
        "key",
        "access_token",
        "bearer",
        "session",
        "sessionid",
        "password",
        "passwd",
        "token",
        "auth",
        "secret",
    ):
        url = "http://example.com/api?{}=secretval123".format(keyword)
        result = redact_url(url)
        assert "secretval123" not in result, "leaked secret for {}".format(keyword)
        assert "{}=REDACTED".format(keyword) in result


def test_redact_url_hides_userinfo_password():
    """TODO.md §H.2-H2d: `user:password@host` userinfo in the netloc
    must be redacted before logging. Strip the password half but
    preserve the username so logs are still useful."""
    url = "http://alice:supersecret@host.example.com/path?q=v"
    result = redact_url(url)
    assert "supersecret" not in result
    assert "alice:REDACTED@host.example.com" in result


def test_redact_url_preserves_userinfo_without_password():
    """If the userinfo half has no password (just a username), don't
    invent a `:REDACTED` that wasn't there."""
    url = "http://alice@host.example.com/path"
    result = redact_url(url)
    assert "REDACTED" not in result
    assert "alice@host.example.com" in result
