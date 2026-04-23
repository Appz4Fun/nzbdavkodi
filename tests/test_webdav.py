# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from resources.lib.webdav import (
    find_video_file,
    get_webdav_stream_url_for_path,
    probe_webdav_reachable,
)

_SETTINGS_WITH_AUTH = {
    "webdav_url": "",
    "nzbdav_url": "http://nzbdav:3000",
    "username": "user",
    "password": "pass",
}

_SETTINGS_NO_AUTH = {
    "webdav_url": "",
    "nzbdav_url": "http://nzbdav:3000",
    "username": "",
    "password": "",
}


def test_legacy_flat_webdav_helpers_are_retired():
    import resources.lib.webdav as webdav

    assert not hasattr(webdav, "build_webdav_url")
    assert not hasattr(webdav, "get_webdav_stream_url")
    assert not hasattr(webdav, "check_file_available")
    assert not hasattr(webdav, "validate_stream")


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
    """The probe URL must be {nzbdav_url}/content/ — the nzbdav content root.
    Verifies the URL construction and the defense-in-depth rstrip."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
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


_PROPFIND_CROSS_ORIGIN_HREFS = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>http://localhost:8080/content/uncategorized/Greyhound/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>http://localhost:8080/content/uncategorized/Greyhound/Greyhound.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:getcontentlength>80000000000</D:getcontentlength>
        <D:resourcetype/>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_accepts_cross_origin_href_path(mock_urlopen, mock_settings):
    """nzbdav legitimately returns its INTERNAL hostname (e.g. localhost:8080)
    in PROPFIND hrefs even when the client addresses it via a different public
    endpoint (e.g. 192.168.1.93:3000). The client must trust the href's PATH
    portion while ignoring the host — follow-up requests still go to the
    configured WebDAV host, so there's no off-server redirect risk.

    Regression guard for the Greyhound 2026-04-23 incident where v1.0.0-pre-
    alpha / v1.0.1 rejected every href on host mismatch and repeatedly logged
    "Completed but no video found" until the resolve dialog gave up."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH  # nzbdav_url = nzbdav:3000
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = _PROPFIND_CROSS_ORIGIN_HREFS.encode("utf-8")
    mock_urlopen.return_value = mock_resp

    path = find_video_file("/content/uncategorized/Greyhound/")
    assert path is not None, "cross-origin href must not cause 'no video found'"
    assert path.endswith(".mkv")
    assert "Greyhound" in path


# --- _build_auth_headers + check_file_in_folder coverage ---


def test_build_auth_headers_empty_username_returns_empty_dict():
    """No username → no Authorization header. Matches ``if not username``."""
    from resources.lib.webdav import _build_auth_headers

    assert _build_auth_headers("", "irrelevant") == {}
    assert _build_auth_headers(None, "irrelevant") == {}


def test_build_auth_headers_encodes_basic_credentials():
    """With a username, emit a proper ``Basic <base64>`` header."""
    import base64

    from resources.lib.webdav import _build_auth_headers

    h = _build_auth_headers("alice", "s3cret")
    assert "Authorization" in h
    scheme, _, token = h["Authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(token).decode() == "alice:s3cret"


def test_build_auth_headers_strips_cr_lf_to_prevent_header_injection():
    """CR/LF in credentials would let a hostile setting split the
    Authorization header. The helper must strip them defensively."""
    import base64

    from resources.lib.webdav import _build_auth_headers

    h = _build_auth_headers("alice\r\n X-Injected: yes", "s3cret\r\n")
    token = h["Authorization"].partition(" ")[2]
    decoded = base64.b64decode(token).decode()
    assert "\r" not in decoded
    assert "\n" not in decoded
    assert decoded == "alice X-Injected: yes:s3cret"


def test_build_auth_headers_handles_none_password():
    """Some settings serialize empty password as None rather than ''.
    Must not raise AttributeError on .replace()."""
    from resources.lib.webdav import _build_auth_headers

    h = _build_auth_headers("alice", None)
    assert "Authorization" in h


@patch("resources.lib.webdav.find_video_file")
def test_check_file_in_folder_returns_path_on_hit(mock_find):
    """check_file_in_folder forwards find_video_file's result on success."""
    from resources.lib.webdav import check_file_in_folder

    mock_find.return_value = "/content/Movie/movie.mkv"

    path, err = check_file_in_folder("/content/Movie/")
    assert path == "/content/Movie/movie.mkv"
    assert err is None


@patch("resources.lib.webdav.find_video_file")
def test_check_file_in_folder_returns_not_found_when_missing(mock_find):
    """When find_video_file returns None, surface a ``not_found`` error
    tag so the caller can distinguish from a reachability failure."""
    from resources.lib.webdav import check_file_in_folder

    mock_find.return_value = None

    path, err = check_file_in_folder("/content/Missing/")
    assert path is None
    assert err == "not_found"
