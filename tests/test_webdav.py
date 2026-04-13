# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import unquote

from resources.lib.webdav import (
    build_webdav_url,
    check_file_available,
    find_video_file,
    get_webdav_stream_url,
    get_webdav_stream_url_for_path,
    probe_webdav_reachable,
)

_SETTINGS_WITH_AUTH = {
    "nzbdav_url": "http://nzbdav:3000",
    "username": "user",
    "password": "pass",
}

_SETTINGS_NO_AUTH = {
    "nzbdav_url": "http://nzbdav:3000",
    "username": "",
    "password": "",
}


@patch("resources.lib.webdav._get_settings")
def test_build_webdav_url(mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    url = build_webdav_url("The.Matrix.1999.2160p.mkv")
    assert url.startswith("http://nzbdav:3000/")
    assert "The.Matrix.1999.2160p.mkv" in url


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_check_file_available_returns_true_on_200(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 200
    available = check_file_available("movie.mkv")
    assert available is True


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_check_file_available_returns_false_on_404(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 404
    available = check_file_available("movie.mkv")
    assert available is False


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_check_file_available_returns_false_on_error(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.side_effect = Exception("Connection refused")
    available = check_file_available("movie.mkv")
    assert available is False


# --- URL encoding round-trip tests ---


@patch("resources.lib.webdav._get_settings")
def test_build_webdav_url_special_characters(mock_settings):
    """Filenames with spaces and special chars should be URL-encoded."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    filename = "Movie (2024) [1080p].mkv"
    url = build_webdav_url(filename)
    # The filename should be URL-encoded in the URL
    assert "Movie" in url
    # Verify the encoded filename can be decoded back
    encoded_part = url.split("/")[-1]
    assert unquote(encoded_part) == filename


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_with_auth(mock_settings):
    """Stream URL should use Kodi pipe-separated auth header."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    url, headers = get_webdav_stream_url("movie.mkv")
    assert url == "http://nzbdav:3000/movie.mkv"
    import base64

    auth_part = headers["Authorization"].split("Basic ")[1]
    assert base64.b64decode(auth_part).decode() == "user:pass"


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_without_auth(mock_settings):
    """Stream URL without auth should be a plain URL with empty headers."""
    mock_settings.return_value = _SETTINGS_NO_AUTH
    url, headers = get_webdav_stream_url("movie.mkv")
    assert url == "http://nzbdav:3000/movie.mkv"
    assert not headers


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_special_chars_in_credentials(mock_settings):
    """Credentials with special chars should be base64-encoded in auth header."""
    import base64

    mock_settings.return_value = {
        "nzbdav_url": "http://nzbdav:3000",
        "username": "user@domain",
        "password": "p@ss:word",
    }
    url, headers = get_webdav_stream_url("movie.mkv")
    assert url == "http://nzbdav:3000/movie.mkv"
    auth_part = headers["Authorization"].split("Basic ")[1]
    assert base64.b64decode(auth_part).decode() == "user@domain:p@ss:word"


# --- probe_webdav_reachable tests ---


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_success_on_200(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 200
    reachable, error = probe_webdav_reachable()
    assert reachable is True
    assert error is None


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_success_on_207(mock_head, mock_settings):
    """207 Multi-Status is the canonical WebDAV success response."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 207
    reachable, error = probe_webdav_reachable()
    assert reachable is True
    assert error is None


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_treats_404_as_reachable(mock_head, mock_settings):
    """Key behavior change from C3: a 404 on HEAD /content/ means the
    server is up but doesn't route HEAD to the collection handler — it
    must NOT be classified as an error."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 404
    reachable, error = probe_webdav_reachable()
    assert reachable is True
    assert error is None


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_treats_405_as_reachable(mock_head, mock_settings):
    """405 Method Not Allowed on a collection is a common WebDAV quirk
    and means the server is up."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 405
    reachable, error = probe_webdav_reachable()
    assert reachable is True
    assert error is None


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_auth_failed_401(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 401
    reachable, error = probe_webdav_reachable()
    assert reachable is False
    assert error == "auth_failed"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_auth_failed_403(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 403
    reachable, error = probe_webdav_reachable()
    assert reachable is False
    assert error == "auth_failed"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_server_error_500(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 500
    reachable, error = probe_webdav_reachable()
    assert reachable is False
    assert error == "server_error"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_retries_then_succeeds(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.side_effect = [Exception("conn refused"), 200]
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    reachable, error = probe_webdav_reachable(
        monitor=monitor, max_retries=3, retry_delay=0
    )
    assert reachable is True
    assert error is None
    assert mock_head.call_count == 2


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_exhausts_retries(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.side_effect = Exception("conn refused")
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    reachable, error = probe_webdav_reachable(
        monitor=monitor, max_retries=2, retry_delay=0
    )
    assert reachable is False
    assert error == "connection_error"
    # max_retries=2 means 3 total attempts (1 initial + 2 retries).
    assert mock_head.call_count == 3


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_waits_via_monitor(mock_head, mock_settings):
    """Proves the C4 fix: the retry delay goes through
    Monitor.waitForAbort, not time.sleep. Since the time import is
    removed from webdav.py in Task 5, no separate 'time.sleep not
    called' assertion is needed."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.side_effect = [Exception("conn refused"), 200]
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    probe_webdav_reachable(monitor=monitor, max_retries=1, retry_delay=5)
    monitor.waitForAbort.assert_called_once_with(5)


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_aborts_on_shutdown_signal(mock_head, mock_settings):
    """If waitForAbort returns True mid-retry, bail out immediately
    instead of re-probing. This is the other half of the C4 fix —
    cooperative shutdown."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.side_effect = Exception("conn refused")
    monitor = MagicMock()
    monitor.waitForAbort.return_value = True
    reachable, error = probe_webdav_reachable(
        monitor=monitor, max_retries=3, retry_delay=0
    )
    assert reachable is False
    assert error == "connection_error"
    # Only the initial attempt ran; the retry was short-circuited by
    # the shutdown signal.
    assert mock_head.call_count == 1


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_hits_content_root(mock_head, mock_settings):
    """The probe URL must be {base}/content/ — the nzbdav content root.
    Verifies the URL construction and the defense-in-depth rstrip."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 200
    probe_webdav_reachable()
    called_url = mock_head.call_args[0][0]
    assert called_url == "http://webdav:8080/content/"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_probe_reachable_uses_nzbdav_url_fallback(mock_head, mock_settings):
    """When webdav_url is empty, fall back to nzbdav_url (same pattern
    as build_webdav_url at webdav.py:42)."""
    mock_settings.return_value = _SETTINGS_FALLBACK
    mock_head.return_value = 200
    probe_webdav_reachable()
    called_url = mock_head.call_args[0][0]
    assert called_url == "http://nzbdav:3000/content/"


# --- find_video_file tests ---

_PROPFIND_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/Send%20Help%202026/</D:href>
    <D:propstat>
      <D:prop>
        <D:resourcetype><D:collection/></D:resourcetype>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/Send%20Help%202026/Send.Help.2026.1080p.NLsubs.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:getcontentlength>4294967296</D:getcontentlength>
        <D:resourcetype/>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_returns_path(mock_urlopen, mock_settings):
    """find_video_file returns the path of the video file found via PROPFIND."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = _PROPFIND_RESPONSE.encode("utf-8")
    mock_urlopen.return_value = mock_resp

    path = find_video_file("/content/uncategorized/Send Help 2026/")
    assert path is not None
    assert path.endswith(".mkv")
    assert "Send.Help.2026" in path


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_returns_none_when_no_video(mock_urlopen, mock_settings):
    """find_video_file returns None when no video file is found in the folder."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    empty_response = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/Empty/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = empty_response.encode("utf-8")
    mock_urlopen.return_value = mock_resp

    path = find_video_file("/content/uncategorized/Empty/")
    assert path is None


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_returns_none_on_error(mock_urlopen, mock_settings):
    """find_video_file returns None on network/parse errors."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_urlopen.side_effect = Exception("Connection refused")

    path = find_video_file("/content/uncategorized/Some Folder/")
    assert path is None


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_returns_none_on_403(mock_urlopen, mock_settings):
    """find_video_file returns None (not raise) on HTTP 403 auth failure."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_urlopen.side_effect = HTTPError(
        url="http://webdav:8080/content/forbidden/",
        code=403,
        msg="Forbidden",
        hdrs=None,
        fp=None,
    )

    path = find_video_file("/content/uncategorized/Forbidden/")
    assert path is None


# --- get_webdav_stream_url_for_path tests ---


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_for_path_with_auth(mock_settings):
    """get_webdav_stream_url_for_path builds WebDAV URL with auth headers."""
    import base64

    mock_settings.return_value = _SETTINGS_WITH_AUTH
    file_path = "/content/uncategorized/Movie/Movie.mkv"
    url, headers = get_webdav_stream_url_for_path(file_path)
    assert url == "http://nzbdav:3000/content/uncategorized/Movie/Movie.mkv"
    auth_part = headers["Authorization"].split("Basic ")[1]
    assert base64.b64decode(auth_part).decode() == "user:pass"


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_for_path_without_auth(mock_settings):
    """get_webdav_stream_url_for_path returns plain URL when no credentials."""
    mock_settings.return_value = _SETTINGS_NO_AUTH
    file_path = "/content/uncategorized/Movie/Movie.mkv"
    url, headers = get_webdav_stream_url_for_path(file_path)
    assert url == "http://nzbdav:3000/content/uncategorized/Movie/Movie.mkv"
    assert not headers


# --- find_video_file hardening tests ---

_PROPFIND_WITH_EMPTY_HREF = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href></D:href>
    <D:propstat>
      <D:prop><D:resourcetype/></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/Movie/Good.Movie.2024.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:getcontentlength>2147483648</D:getcontentlength>
        <D:resourcetype/>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_handles_malformed_href(mock_urlopen, mock_settings):
    """find_video_file should skip malformed hrefs without crashing."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = _PROPFIND_WITH_EMPTY_HREF.encode("utf-8")
    mock_urlopen.return_value = mock_resp

    path = find_video_file("/content/uncategorized/Movie/")
    assert path is not None
    assert path.endswith(".mkv")
    assert "Good.Movie.2024" in path


_PROPFIND_RELATIVE_HREFS = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/Relative%20Movie/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/Relative%20Movie/Relative.Movie.2024.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:getcontentlength>3221225472</D:getcontentlength>
        <D:resourcetype/>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_handles_relative_href(mock_urlopen, mock_settings):
    """find_video_file should handle relative path hrefs (no http://host prefix)."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = _PROPFIND_RELATIVE_HREFS.encode("utf-8")
    mock_urlopen.return_value = mock_resp

    path = find_video_file("/content/uncategorized/Relative Movie/")
    assert path is not None
    assert path.endswith(".mkv")
    assert "Relative.Movie.2024" in path
