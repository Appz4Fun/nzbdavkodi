# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
import socket
from unittest.mock import MagicMock, patch

from resources.lib.http_util import (
    HttpResponseTooLarge,
    clean_search_query,
    http_get,
    http_post_json,
    iso8601_to_rfc2822,
    notify,
    prefer_ipv4_connections,
    pubdate_to_epoch,
    redact_text,
    redact_url,
)


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
def test_http_get_passes_extra_headers_and_reads_with_max_bytes(mock_urlopen):
    """http_get should merge caller headers and cap reads when requested."""
    mock_resp = MagicMock()
    mock_resp.headers.get.return_value = "0"
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    result = http_get("http://example.com/api", headers={"X-Test": "1"}, max_bytes=2)

    assert result == "ok"

    request = mock_urlopen.call_args[0][0]
    assert request.get_header("User-agent") == "NZB-DAV Kodi Addon"
    assert request.get_header("X-test") == "1"
    mock_resp.read.assert_called_once_with(3)


@patch("resources.lib.http_util.urlopen")
def test_http_get_rejects_oversized_content_length(mock_urlopen):
    """Content-Length larger than max_bytes should fail before body read."""
    import pytest

    mock_resp = MagicMock()
    mock_resp.headers.get.return_value = "5"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    with pytest.raises(HttpResponseTooLarge):
        http_get("http://example.com/api", max_bytes=4)
    mock_resp.read.assert_not_called()


@patch("resources.lib.http_util.urlopen")
def test_http_get_rejects_body_larger_than_max_bytes(mock_urlopen):
    """Bodies without Content-Length should still be capped after read."""
    import pytest

    mock_resp = MagicMock()
    mock_resp.headers.get.return_value = "0"
    mock_resp.read.return_value = b"12345"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    with pytest.raises(HttpResponseTooLarge):
        http_get("http://example.com/api", max_bytes=4)
    mock_resp.read.assert_called_once_with(5)


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


def test_redact_text_redacts_multiple_credential_params():
    """The ``<key>=<value>`` redaction (backreference replacement) must scrub
    every credential pair in a free-form string while keeping the key names."""
    msg = "GET failed: apikey=secretA123 token=secretB456 imdbid=tt1 t=movie"
    result = redact_text(msg)

    assert "secretA123" not in result
    assert "secretB456" not in result
    assert "apikey=REDACTED" in result
    assert "token=REDACTED" in result
    # Non-credential params are untouched.
    assert "imdbid=tt1" in result
    assert "t=movie" in result


def test_redact_text_redacts_digit_prefixed_values():
    """The ``\\1=REDACTED`` backreference must not misfire when the secret value
    begins with a digit — the literal ``=`` terminates the group, so there is no
    ``\\1<digit>`` ambiguity."""
    result = redact_text("apikey=123secret token=999 key=0abc&next=1")

    assert "123secret" not in result
    assert "token=999" not in result
    assert "key=0abc" not in result
    assert "apikey=REDACTED" in result
    assert "token=REDACTED" in result
    assert "key=REDACTED" in result


def test_redact_text_strips_embedded_url_userinfo_password():
    """redact_text must also scrub `scheme://user:pass@host` userinfo when
    a URL is embedded in a free-form error string (e.g. a urllib/xbmcvfs
    error echoing the NZBGet RPC URL or the smb://user:pass@host root).
    redact_url only handles a parseable URL; this covers the in-text case."""
    msg = "URLError refused smb://alice:supersecret@host/completed/The.Movie"
    result = redact_text(msg)
    assert "supersecret" not in result
    assert "alice:REDACTED@host" in result


def test_redact_text_preserves_userinfo_without_password():
    """A bare `user@host` (no password half) must round-trip unchanged."""
    result = redact_text("connecting to ftp://alice@host/x")
    assert "REDACTED" not in result
    assert "alice@host" in result


def test_redact_text_redacts_password_containing_at_sign():
    """A password with a literal `@` (common in SMB/NZBGet creds) must be
    fully scrubbed — the naive userinfo regex stopped at the FIRST `@` and
    leaked the tail. Split on the LAST `@` of the authority instead."""
    result = redact_text("refused http://user:p@ss99@box:6789/jsonrpc")
    assert "ss99" not in result
    assert "p@ss99" not in result
    assert "user:REDACTED@box:6789" in result


def test_redact_text_redacts_smb_password_containing_at_sign():
    result = redact_text("SMB error smb://media:My@Secret!@nas.local/completed/Show")
    assert "Secret!" not in result
    assert "media:REDACTED@nas.local" in result


def test_redact_text_redacts_empty_username_password():
    """`smb://:password@host` (guest/anonymous share, empty username) must be
    redacted — the old regex required a non-empty username and left it
    entirely unscrubbed."""
    result = redact_text("SMB error: smb://:GuestPw123@nas.local/completed/X")
    assert "GuestPw123" not in result
    assert ":REDACTED@nas.local" in result


def test_pubdate_to_epoch_parses_rfc2822_with_timezone():
    """An RFC-2822 pubdate with an explicit timezone offset converts to
    the correct absolute UTC epoch (the offset is normalized away)."""
    # 2021-12-15 12:00:00 +0000
    assert pubdate_to_epoch("Wed, 15 Dec 2021 12:00:00 +0000") == 1639569600
    # Same instant expressed as -0500 must yield the SAME epoch.
    assert pubdate_to_epoch("Wed, 15 Dec 2021 07:00:00 -0500") == 1639569600


def test_pubdate_to_epoch_assumes_utc_when_naive():
    """A pubdate with no timezone is treated as UTC rather than local
    time, so the epoch is deterministic across machines."""
    assert pubdate_to_epoch("Wed, 15 Dec 2021 12:00:00") == 1639569600


def test_pubdate_to_epoch_returns_none_on_garbage():
    """Unparseable / empty input returns None (caller fails open)."""
    assert pubdate_to_epoch("") is None
    assert pubdate_to_epoch("not a date") is None
    assert pubdate_to_epoch(None) is None


# --- iso8601_to_rfc2822 (Prowlarr native-JSON publishDate normalization) ---


def test_iso8601_to_rfc2822_round_trips_through_pubdate_to_epoch():
    """The whole point: an ISO-8601 publishDate must normalize to an
    RFC-2822 string that pubdate_to_epoch can then parse to the correct
    absolute epoch (it rejects raw ISO-8601)."""
    # Sanity: pubdate_to_epoch genuinely cannot read ISO-8601.
    assert pubdate_to_epoch("2021-12-15T12:00:00Z") is None
    rfc = iso8601_to_rfc2822("2021-12-15T12:00:00Z")
    assert "T" not in rfc  # converted, not passed through
    assert pubdate_to_epoch(rfc) == 1639569600


def test_iso8601_to_rfc2822_handles_offset_and_naive_and_fractional():
    """Explicit offsets normalize to UTC; naive is assumed UTC; .NET-style
    7-digit fractional seconds are tolerated."""
    # Same instant via a -0500 offset.
    assert (
        pubdate_to_epoch(iso8601_to_rfc2822("2021-12-15T07:00:00-05:00")) == 1639569600
    )
    # No timezone -> assumed UTC.
    assert pubdate_to_epoch(iso8601_to_rfc2822("2021-12-15T12:00:00")) == 1639569600
    # Fractional seconds (7 digits, as .NET emits) are dropped, not fatal.
    assert (
        pubdate_to_epoch(iso8601_to_rfc2822("2021-12-15T12:00:00.1234567Z"))
        == 1639569600
    )


def test_iso8601_to_rfc2822_returns_empty_on_bad_input():
    """Empty / non-string / unparseable input returns '' (parse loops
    treat a missing field as an empty string, never an exception)."""
    assert iso8601_to_rfc2822("") == ""
    assert iso8601_to_rfc2822(None) == ""
    assert iso8601_to_rfc2822("not a date") == ""
    assert iso8601_to_rfc2822(12345) == ""


def test_http_post_json_posts_body_and_returns_text():
    captured = {}

    class FakeResp:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getcode(self):
            return 200

        def read(self):
            return b'{"result": 7}'

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["auth"] = req.get_header("Authorization")
        captured["ctype"] = req.get_header("Content-type")
        return FakeResp()

    with patch("resources.lib.http_util.urlopen", side_effect=fake_urlopen):
        body = http_post_json(
            "http://host:6789/jsonrpc",
            {"method": "version"},
            timeout=10,
            basic_auth=("nzbget", "pw"),
        )

    assert json.loads(body) == {"result": 7}
    assert captured["url"] == "http://host:6789/jsonrpc"
    assert json.loads(captured["data"].decode("utf-8")) == {"method": "version"}
    assert captured["ctype"] == "application/json"
    assert captured["auth"].startswith("Basic ")


def test_http_post_json_rejects_non_http_scheme():
    import pytest

    with pytest.raises(ValueError):
        http_post_json("file:///etc/passwd", {}, timeout=5)


# --- prefer_ipv4_connections: IPv6-before-IPv4 loopback delay fix (~10s stall) ---


def _fake_getaddrinfo(host, port, *args, **kwargs):
    """Mimics real getaddrinfo("localhost", ...) ordering: IPv6 first."""
    return [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
    ]


def test_prefer_ipv4_connections_sorts_ipv4_first():
    """The whole point of the fix: within the `with` block, IPv4 results
    must come before IPv6 ones so urllib tries the working address first
    instead of eating a ~2s refusal on the unreachable IPv6 loopback."""
    with patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo):
        with prefer_ipv4_connections():
            results = socket.getaddrinfo("localhost", 3000)

    families = [r[0] for r in results]
    assert families == [socket.AF_INET, socket.AF_INET6]


def test_prefer_ipv4_connections_does_not_drop_ipv6():
    """Reordering must never remove IPv6 entries -- a host reachable only
    over IPv6 still needs to be tried, just second."""
    with patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo):
        with prefer_ipv4_connections():
            results = socket.getaddrinfo("localhost", 3000)

    assert len(results) == 2
    assert any(r[0] == socket.AF_INET6 for r in results)


def test_prefer_ipv4_connections_restores_original_getaddrinfo_on_exit():
    """The monkeypatch must be scoped to the `with` block -- leaking it
    would affect unrelated connections made after the block exits."""
    original = socket.getaddrinfo
    with prefer_ipv4_connections():
        assert socket.getaddrinfo is not original
    assert socket.getaddrinfo is original


def test_prefer_ipv4_connections_restores_original_even_on_exception():
    """A failed request inside the block must not leave getaddrinfo patched."""
    import pytest

    original = socket.getaddrinfo
    with pytest.raises(RuntimeError):
        with prefer_ipv4_connections():
            raise RuntimeError("boom")
    assert socket.getaddrinfo is original


def test_prefer_ipv4_connections_preserves_ipv4_only_results():
    """A host that only resolves to IPv4 must pass through unchanged."""

    def ipv4_only(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    with patch("socket.getaddrinfo", side_effect=ipv4_only):
        with prefer_ipv4_connections():
            results = socket.getaddrinfo("localhost", 3000)

    assert len(results) == 1
    assert results[0][0] == socket.AF_INET


# --- clean_search_query: strip query-breaking '&' from keyword searches (#294) ---


def test_clean_search_query_drops_ampersand():
    """A literal '&' in the title (e.g. 'Your Friends & Neighbors') must not
    reach the indexer keyword search: release names spell it 'and' or omit it,
    so an '&' token matches nothing and the search returns zero results (#294).
    """
    assert clean_search_query("Your Friends & Neighbors") == "Your Friends Neighbors"


def test_clean_search_query_collapses_whitespace_left_by_ampersand():
    """Removing '&' must not leave a double space that becomes an empty token."""
    assert clean_search_query("Law & Order") == "Law Order"
    assert clean_search_query("AT&T") == "AT T"


def test_clean_search_query_leaves_plain_title_untouched():
    assert clean_search_query("The Matrix") == "The Matrix"


def test_clean_search_query_handles_empty_and_none():
    assert clean_search_query("") == ""
    assert clean_search_query(None) == ""
