# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch
from urllib.parse import unquote

from resources.lib.webdav import (
    build_webdav_url,
    check_file_available,
    check_file_available_with_retry,
    find_video_file,
    get_webdav_stream_url,
    get_webdav_stream_url_for_path,
)

_SETTINGS_WITH_AUTH = {
    "webdav_url": "http://webdav:8080",
    "nzbdav_url": "http://nzbdav:3000",
    "username": "user",
    "password": "pass",
}

_SETTINGS_NO_AUTH = {
    "webdav_url": "http://webdav:8080",
    "nzbdav_url": "http://nzbdav:3000",
    "username": "",
    "password": "",
}

_SETTINGS_FALLBACK = {
    "webdav_url": "",
    "nzbdav_url": "http://nzbdav:3000",
    "username": "user",
    "password": "pass",
}


@patch("resources.lib.webdav._get_settings")
def test_build_webdav_url_with_explicit_url(mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    url = build_webdav_url("The.Matrix.1999.2160p.mkv")
    assert url.startswith("http://webdav:8080/")
    assert "The.Matrix.1999.2160p.mkv" in url


@patch("resources.lib.webdav._get_settings")
def test_build_webdav_url_falls_back_to_nzbdav_url(mock_settings):
    mock_settings.return_value = _SETTINGS_FALLBACK
    url = build_webdav_url("movie.mkv")
    assert url.startswith("http://nzbdav:3000/")


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
    url = get_webdav_stream_url("movie.mkv")
    assert url.startswith("http://webdav:8080/movie.mkv|Authorization=Basic ")
    # Verify base64 decodes to user:pass
    import base64

    auth_part = url.split("Basic ")[1]
    assert base64.b64decode(auth_part).decode() == "user:pass"


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_without_auth(mock_settings):
    """Stream URL without auth should be a plain URL."""
    mock_settings.return_value = _SETTINGS_NO_AUTH
    url = get_webdav_stream_url("movie.mkv")
    assert url == "http://webdav:8080/movie.mkv"


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_special_chars_in_credentials(mock_settings):
    """Credentials with special chars should be base64-encoded in auth header."""
    import base64

    mock_settings.return_value = {
        "webdav_url": "http://webdav:8080",
        "nzbdav_url": "http://nzbdav:3000",
        "username": "user@domain",
        "password": "p@ss:word",
    }
    url = get_webdav_stream_url("movie.mkv")
    assert "|Authorization=Basic " in url
    auth_part = url.split("Basic ")[1]
    assert base64.b64decode(auth_part).decode() == "user@domain:p@ss:word"


# --- check_file_available_with_retry tests ---


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_check_file_available_with_retry_success(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 200
    available, error = check_file_available_with_retry("movie.mkv")
    assert available is True
    assert error is None


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_check_file_available_with_retry_auth_failed(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 401
    available, error = check_file_available_with_retry("movie.mkv")
    assert available is False
    assert error == "auth_failed"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_check_file_available_with_retry_server_error(mock_head, mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.return_value = 500
    available, error = check_file_available_with_retry("movie.mkv")
    assert available is False
    assert error == "server_error"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav._http_head")
def test_check_file_available_with_retry_retries_on_connection_error(
    mock_head, mock_settings
):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_head.side_effect = [Exception("conn refused"), Exception("conn refused"), 200]
    available, error = check_file_available_with_retry(
        "movie.mkv", max_retries=3, retry_delay=0
    )
    assert available is True
    assert error is None
    assert mock_head.call_count == 3


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


# --- get_webdav_stream_url_for_path tests ---


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_for_path_with_auth(mock_settings):
    """get_webdav_stream_url_for_path uses Kodi pipe-separated auth header."""
    import base64

    mock_settings.return_value = _SETTINGS_WITH_AUTH
    file_path = "/content/uncategorized/Movie/Movie.mkv"
    url = get_webdav_stream_url_for_path(file_path)
    assert url.startswith(
        "http://webdav:8080/content/uncategorized/Movie/Movie.mkv|Authorization=Basic "
    )
    auth_part = url.split("Basic ")[1]
    assert base64.b64decode(auth_part).decode() == "user:pass"


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_for_path_without_auth(mock_settings):
    """get_webdav_stream_url_for_path returns plain URL when no credentials."""
    mock_settings.return_value = _SETTINGS_NO_AUTH
    file_path = "/content/uncategorized/Movie/Movie.mkv"
    url = get_webdav_stream_url_for_path(file_path)
    assert url == "http://webdav:8080/content/uncategorized/Movie/Movie.mkv"
