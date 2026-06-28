# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import time
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from resources.lib.webdav import (
    find_video_file,
    find_video_stream_for_folder,
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
    from resources.lib import webdav

    assert not hasattr(webdav, "build_webdav_url")
    assert not hasattr(webdav, "get_webdav_stream_url")
    assert not hasattr(webdav, "check_file_available")
    assert not hasattr(webdav, "validate_stream")


@patch("resources.lib.webdav._get_settings")
def test_get_webdav_stream_url_encodes_path_spaces(mock_settings):
    mock_settings.return_value = _SETTINGS_WITH_AUTH

    url, _headers = get_webdav_stream_url_for_path(
        "/content/Dune Part Two/Dune Part Two DD+7.1.mkv"
    )

    assert url == (
        "http://nzbdav:3000/content/Dune%20Part%20Two/"
        "Dune%20Part%20Two%20DD%2B7.1.mkv"
    )


@patch("resources.lib.webdav.urlopen")
def test_webdav_head_probe_uses_30s_timeout(mock_urlopen):
    from resources.lib.webdav import _http_head

    response = MagicMock()
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    response.getcode.return_value = 200
    mock_urlopen.return_value = response

    assert _http_head("http://nzbdav:3000/content/") == 200
    assert mock_urlopen.call_args.kwargs["timeout"] == 30


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
def test_find_video_file_records_propfind_content_length_hint(
    mock_urlopen, mock_settings
):
    """The resolver should not have to HEAD the selected WebDAV file again."""
    from resources.lib import webdav

    mock_settings.return_value = _SETTINGS_WITH_AUTH
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = _PROPFIND_RESPONSE.encode("utf-8")
    mock_urlopen.return_value = mock_resp

    path = find_video_file("/content/uncategorized/Send Help 2026/")

    assert path is not None
    assert webdav.get_video_file_size_hint(path) == 4294967296


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


@patch("xbmcaddon.Addon", side_effect=RuntimeError("Kodi settings unavailable"))
@patch("resources.lib.webdav.urlopen")
def test_find_video_stream_for_folder_uses_settings_getter_without_kodi_addon(
    mock_urlopen, mock_addon
):
    def settings_getter(key, default=""):
        return {
            "webdav_url": "",
            "nzbdav_url": "http://nzbdav:3000",
            "webdav_username": "user",
            "webdav_password": "pass",
        }.get(key, default)

    mock_urlopen.return_value = _webdav_response(
        _propfind_listing(
            [
                ("/content/uncategorized/Movie/", True, None),
                ("/content/uncategorized/Movie/Movie.mkv", False, 1234),
            ]
        )
    )

    path, url, headers = find_video_stream_for_folder(
        "/content/uncategorized/Movie/",
        settings_getter=settings_getter,
    )

    assert path == "/content/uncategorized/Movie/Movie.mkv"
    assert url == "http://nzbdav:3000/content/uncategorized/Movie/Movie.mkv"
    assert "Authorization" in headers
    mock_addon.assert_not_called()


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


_PROPFIND_ENCODED_PARENT = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/Parent%20Movie/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/Parent%20Movie/Disc%201/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""

_PROPFIND_ENCODED_CHILD = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/Parent%20Movie/Disc%201/Parent.Movie.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:getcontentlength>1234</D:getcontentlength>
        <D:resourcetype/>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_recurses_without_double_encoding_href(
    mock_urlopen, mock_settings
):
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent_resp = MagicMock()
    parent_resp.__enter__ = lambda s: s
    parent_resp.__exit__ = MagicMock(return_value=False)
    parent_resp.read.return_value = _PROPFIND_ENCODED_PARENT.encode("utf-8")
    child_resp = MagicMock()
    child_resp.__enter__ = lambda s: s
    child_resp.__exit__ = MagicMock(return_value=False)
    child_resp.read.return_value = _PROPFIND_ENCODED_CHILD.encode("utf-8")
    mock_urlopen.side_effect = [parent_resp, child_resp]

    path = find_video_file("/content/uncategorized/Parent Movie/")

    assert path == "/content/uncategorized/Parent%20Movie/Disc%201/Parent.Movie.mkv"
    second_url = mock_urlopen.call_args_list[1][0][0].full_url
    assert "Disc%201" in second_url
    assert "%2520" not in second_url


def _webdav_response(body):
    response = MagicMock()
    response.__enter__ = lambda s: s
    response.__exit__ = MagicMock(return_value=False)
    response.read.return_value = body.encode("utf-8")
    return response


def _propfind_listing(hrefs):
    responses = []
    for href, is_collection, size in hrefs:
        collection = (
            "<D:resourcetype><D:collection/></D:resourcetype>"
            if is_collection
            else "<D:resourcetype/>"
        )
        length = (
            "<D:getcontentlength>{}</D:getcontentlength>".format(size)
            if size is not None
            else ""
        )
        responses.append("""
  <D:response>
    <D:href>{}</D:href>
    <D:propstat>
      <D:prop>{}{}</D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>""".format(href, length, collection))
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<D:multistatus xmlns:D="DAV:">' + "".join(responses) + "\n</D:multistatus>"
    )


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_parses_request_url_once_for_large_propfind_listing(
    mock_urlopen, mock_settings
):
    """Completed-stream WebDAV discovery should not reparse invariants per row."""
    import urllib.parse

    mock_settings.return_value = _SETTINGS_WITH_AUTH
    hrefs = [("/content/uncategorized/Many/", True, None)]
    hrefs.extend(
        (
            "/content/uncategorized/Many/Sample{:03d}.nfo".format(index),
            False,
            100 + index,
        )
        for index in range(40)
    )
    hrefs.append(("/content/uncategorized/Many/Movie.mkv", False, 1234))
    mock_urlopen.return_value = _webdav_response(_propfind_listing(hrefs))

    request_url = "http://nzbdav:3000/content/uncategorized/Many/"
    real_urlparse = urllib.parse.urlparse
    request_url_parses = []

    def counted_urlparse(value, *args, **kwargs):
        if value == request_url:
            request_url_parses.append(time.perf_counter())
            time.sleep(0.002)
        return real_urlparse(value, *args, **kwargs)

    with patch("urllib.parse.urlparse", side_effect=counted_urlparse):
        started = time.perf_counter()
        path = find_video_file("/content/uncategorized/Many/")
        elapsed = time.perf_counter() - started

    assert path == "/content/uncategorized/Many/Movie.mkv"
    assert len(request_url_parses) == 1, (
        "request URL was parsed {} times; completed-stream discovery took {:.3f}s"
    ).format(len(request_url_parses), elapsed)


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_parallelizes_sibling_subfolders_for_post_picker_start(
    mock_urlopen, mock_settings
):
    """WebDAV video discovery should not pay one sibling RTT at a time."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/uncategorized/Serial/", True, None),
            ("/content/uncategorized/Serial/A/", True, None),
            ("/content/uncategorized/Serial/B/", True, None),
            ("/content/uncategorized/Serial/C/", True, None),
        ]
    )
    empty_a = _propfind_listing([("/content/uncategorized/Serial/A/", True, None)])
    empty_b = _propfind_listing([("/content/uncategorized/Serial/B/", True, None)])
    with_video = _propfind_listing(
        [
            ("/content/uncategorized/Serial/C/", True, None),
            ("/content/uncategorized/Serial/C/Movie.mkv", False, 1234),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Serial/"):
            return _webdav_response(parent)
        if url.endswith("/Serial/A/"):
            time.sleep(0.08)
            return _webdav_response(empty_a)
        if url.endswith("/Serial/B/"):
            time.sleep(0.18)
            return _webdav_response(empty_b)
        if url.endswith("/Serial/C/"):
            time.sleep(0.18)
            return _webdav_response(with_video)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    started = time.perf_counter()
    path = find_video_file("/content/uncategorized/Serial/")
    elapsed = time.perf_counter() - started

    assert path == "/content/uncategorized/Serial/C/Movie.mkv"
    assert elapsed < 0.30, "WebDAV sibling discovery took {:.3f}s".format(elapsed)


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_overlaps_first_sibling_probe_for_post_picker_start(
    mock_urlopen, mock_settings
):
    """Sibling probes run concurrently; a size tie keeps the earliest sibling.

    We now scan every sibling (no early-exit) to pick the largest, so total
    time tracks the SLOWEST sibling — but probes still overlap, so it stays
    near max(sibling) rather than the serial sum. B and C are the same size,
    so the tie resolves to the earlier-listed B.
    """
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/uncategorized/Overlap/", True, None),
            ("/content/uncategorized/Overlap/A/", True, None),
            ("/content/uncategorized/Overlap/B/", True, None),
            ("/content/uncategorized/Overlap/C/", True, None),
        ]
    )
    empty_a = _propfind_listing([("/content/uncategorized/Overlap/A/", True, None)])
    with_video_b = _propfind_listing(
        [
            ("/content/uncategorized/Overlap/B/", True, None),
            ("/content/uncategorized/Overlap/B/Movie.mkv", False, 1234),
        ]
    )
    slow_c = _propfind_listing(
        [
            ("/content/uncategorized/Overlap/C/", True, None),
            ("/content/uncategorized/Overlap/C/Slow.mkv", False, 1234),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Overlap/"):
            return _webdav_response(parent)
        if url.endswith("/Overlap/A/"):
            time.sleep(0.16)
            return _webdav_response(empty_a)
        if url.endswith("/Overlap/B/"):
            time.sleep(0.16)
            return _webdav_response(with_video_b)
        if url.endswith("/Overlap/C/"):
            time.sleep(0.6)
            return _webdav_response(slow_c)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    started = time.perf_counter()
    path = find_video_file("/content/uncategorized/Overlap/")
    elapsed = time.perf_counter() - started

    assert path == "/content/uncategorized/Overlap/B/Movie.mkv"
    # Overlapped: near the slowest sibling (~0.6s), well under the serial sum
    # (0.16 + 0.16 + 0.6 = 0.92s).
    assert elapsed < 0.85, "first-sibling WebDAV overlap took {:.3f}s".format(elapsed)


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_reuses_settings_during_recursive_post_picker_scan(
    mock_urlopen, mock_settings
):
    """Recursive WebDAV discovery should not re-read Kodi settings per sibling."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH

    def slow_settings():
        time.sleep(0.04)
        return _SETTINGS_WITH_AUTH

    mock_settings.side_effect = slow_settings
    parent = _propfind_listing(
        [
            ("/content/uncategorized/SettingsFanout/", True, None),
            ("/content/uncategorized/SettingsFanout/A/", True, None),
            ("/content/uncategorized/SettingsFanout/B/", True, None),
        ]
    )
    empty_a = _propfind_listing(
        [("/content/uncategorized/SettingsFanout/A/", True, None)]
    )
    with_video_b = _propfind_listing(
        [
            ("/content/uncategorized/SettingsFanout/B/", True, None),
            ("/content/uncategorized/SettingsFanout/B/Movie.mkv", False, 1234),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/SettingsFanout/"):
            return _webdav_response(parent)
        if url.endswith("/SettingsFanout/A/"):
            time.sleep(0.02)
            return _webdav_response(empty_a)
        if url.endswith("/SettingsFanout/B/"):
            time.sleep(0.02)
            return _webdav_response(with_video_b)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    started = time.perf_counter()
    path = find_video_file("/content/uncategorized/SettingsFanout/")
    elapsed = time.perf_counter() - started

    assert path == "/content/uncategorized/SettingsFanout/B/Movie.mkv"
    assert (
        elapsed < 0.5
    ), "settings fanout WebDAV discovery took {:.3f}s with {} settings reads".format(
        elapsed, mock_settings.call_count
    )
    assert mock_settings.call_count == 1


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_waits_for_larger_slower_later_sibling(
    mock_urlopen, mock_settings
):
    """A larger video in a slower, later-listed sibling must still win.

    The old code returned the first sibling with any video (here B) and never
    waited for the slower C. Now we scan all siblings and pick the largest, so
    the bigger C wins even though it is listed later and responds slower —
    exactly what prevents a smaller early release from hijacking the real one.
    """
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/uncategorized/Ordered/", True, None),
            ("/content/uncategorized/Ordered/A/", True, None),
            ("/content/uncategorized/Ordered/B/", True, None),
            ("/content/uncategorized/Ordered/C/", True, None),
        ]
    )
    empty_a = _propfind_listing([("/content/uncategorized/Ordered/A/", True, None)])
    with_video_b = _propfind_listing(
        [
            ("/content/uncategorized/Ordered/B/", True, None),
            ("/content/uncategorized/Ordered/B/Small.mkv", False, 1234),
        ]
    )
    with_video_c = _propfind_listing(
        [
            ("/content/uncategorized/Ordered/C/", True, None),
            ("/content/uncategorized/Ordered/C/Large.mkv", False, 999999),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Ordered/"):
            return _webdav_response(parent)
        if url.endswith("/Ordered/A/"):
            time.sleep(0.08)
            return _webdav_response(empty_a)
        if url.endswith("/Ordered/B/"):
            time.sleep(0.18)
            return _webdav_response(with_video_b)
        if url.endswith("/Ordered/C/"):
            time.sleep(0.5)
            return _webdav_response(with_video_c)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/uncategorized/Ordered/")

    assert path == "/content/uncategorized/Ordered/C/Large.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_skips_hidden_dot_subfolders(mock_urlopen, mock_settings):
    """Hidden (dot-prefixed) sibling folders must be ignored entirely.

    Regression for the polluted FraMeSToR release whose hidden
    '.and_justice_for_all...1080p...' child folder hijacked playback of the
    real 2160p release. A leading-dot subfolder is skipped (never even
    PROPFIND'd) regardless of how large its contents are.
    """
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/uncategorized/Polluted/", True, None),
            ("/content/uncategorized/Polluted/.and_justice_1080p/", True, None),
            ("/content/uncategorized/Polluted/Real/", True, None),
        ]
    )
    real = _propfind_listing(
        [
            ("/content/uncategorized/Polluted/Real/", True, None),
            ("/content/uncategorized/Polluted/Real/Silence.2160p.mkv", False, 9000),
        ]
    )
    visited_urls = []

    def propfind(req, **_kwargs):
        url = req.full_url
        visited_urls.append(url)
        if url.endswith("/Polluted/"):
            return _webdav_response(parent)
        if url.endswith("/Real/"):
            return _webdav_response(real)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/uncategorized/Polluted/")

    assert path == "/content/uncategorized/Polluted/Real/Silence.2160p.mkv"
    assert not any(
        ".and_justice" in url for url in visited_urls
    ), "hidden dot-folder must not be probed: {}".format(visited_urls)


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_returns_largest_across_sibling_folders(
    mock_urlopen, mock_settings
):
    """Across sibling folders, the LARGEST video wins regardless of order.

    The old code returned the first sibling that had any video, so a smaller
    junk release in an earlier-listed sibling beat the real, larger release in
    a later sibling.
    """
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/uncategorized/Multi/", True, None),
            ("/content/uncategorized/Multi/A/", True, None),
            ("/content/uncategorized/Multi/B/", True, None),
        ]
    )
    small_a = _propfind_listing(
        [
            ("/content/uncategorized/Multi/A/", True, None),
            ("/content/uncategorized/Multi/A/Junk.1080p.mkv", False, 1000),
        ]
    )
    large_b = _propfind_listing(
        [
            ("/content/uncategorized/Multi/B/", True, None),
            ("/content/uncategorized/Multi/B/Real.2160p.mkv", False, 90000),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Multi/"):
            return _webdav_response(parent)
        if url.endswith("/Multi/A/"):
            return _webdav_response(small_a)
        if url.endswith("/Multi/B/"):
            return _webdav_response(large_b)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/uncategorized/Multi/")
    assert path == "/content/uncategorized/Multi/B/Real.2160p.mkv"


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

    assert not _build_auth_headers("", "irrelevant")
    assert not _build_auth_headers(None, "irrelevant")


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


# ---------------------------------------------------------------------------
# F8: episode-hint preference for multi-episode packs
# ---------------------------------------------------------------------------


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_prefers_title_hint_over_largest(mock_urlopen, mock_settings):
    """With a requested episode hint, the name-matching video wins even when a
    different episode in the same pack is larger."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.S02E05.1080p.mkv", False, 2000000000),
            ("/content/Show/Show.S02E06.1080p.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file("/content/Show/", title_hint="Show.S02E05.1080p.WEB-DL")

    assert path == "/content/Show/Show.S02E05.1080p.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_without_hint_keeps_largest(mock_urlopen, mock_settings):
    """No hint -> preserve the existing largest-video behavior."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.S02E05.1080p.mkv", False, 2000000000),
            ("/content/Show/Show.S02E06.1080p.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file("/content/Show/")

    assert path == "/content/Show/Show.S02E06.1080p.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_without_hint_recurses_past_sizeless_top_level_video(
    mock_urlopen, mock_settings
):
    """A top-level video with a missing getcontentlength (size 0) must NOT
    pre-empt recursion on the no-hint path. main kept best_size=0 with a
    strict ``size > best_size``, so a size-0 file was never adopted and the
    function recursed into the subdir holding the real feature. The
    hinted-selection rewrite must reproduce that EXACTLY (deliberate decision
    f12b3c3); otherwise a top-level placeholder/teaser lacking a content
    length is played instead of the real movie in the subfolder."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Movie/", True, None),
            ("/content/Movie/teaser.mkv", False, None),  # no getcontentlength
            ("/content/Movie/Disc1/", True, None),
        ]
    )
    disc1 = _propfind_listing(
        [
            ("/content/Movie/Disc1/", True, None),
            ("/content/Movie/Disc1/feature.mkv", False, 9000000000),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Movie/"):
            return _webdav_response(parent)
        if url.endswith("/Disc1/"):
            return _webdav_response(disc1)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/Movie/")

    assert path == "/content/Movie/Disc1/feature.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_hint_no_match_falls_back_to_largest(
    mock_urlopen, mock_settings
):
    """A hint that matches no sibling video still returns the largest video."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.S02E05.1080p.mkv", False, 2000000000),
            ("/content/Show/Show.S02E06.1080p.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file("/content/Show/", title_hint="Show.S05E99.1080p.WEB-DL")

    assert path == "/content/Show/Show.S02E06.1080p.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_prefers_hint_across_sibling_subfolders(
    mock_urlopen, mock_settings
):
    """The episode hint must steer selection across sibling subfolders, not
    just files within one folder."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Pack/", True, None),
            ("/content/Pack/E05/", True, None),
            ("/content/Pack/E06/", True, None),
        ]
    )
    e05 = _propfind_listing(
        [
            ("/content/Pack/E05/", True, None),
            ("/content/Pack/E05/Show.S02E05.1080p.mkv", False, 2000000000),
        ]
    )
    e06 = _propfind_listing(
        [
            ("/content/Pack/E06/", True, None),
            ("/content/Pack/E06/Show.S02E06.1080p.mkv", False, 9000000000),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Pack/"):
            return _webdav_response(parent)
        if url.endswith("/E05/"):
            return _webdav_response(e05)
        if url.endswith("/E06/"):
            return _webdav_response(e06)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/Pack/", title_hint="Show.S02E05.1080p.WEB-DL")

    assert path == "/content/Pack/E05/Show.S02E05.1080p.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_prefers_dir_tagged_episode_over_larger_sibling(
    mock_urlopen, mock_settings
):
    """When the SxxExx tag is on the DIRECTORY and the file is generically
    named, the requested episode's folder must win over a larger wrong-episode
    sibling -- the basename carries no tag, so the parent dir supplies it."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.S01E02.1080p/", True, None),
            ("/content/Show/Show.S01E03.1080p/", True, None),
        ]
    )
    e02 = _propfind_listing(
        [
            ("/content/Show/Show.S01E02.1080p/", True, None),
            ("/content/Show/Show.S01E02.1080p/video.mkv", False, 1000000000),
        ]
    )
    e03 = _propfind_listing(
        [
            ("/content/Show/Show.S01E03.1080p/", True, None),
            ("/content/Show/Show.S01E03.1080p/video.mkv", False, 5000000000),
        ]
    )

    def propfind(req, **_kw):
        u = req.full_url
        if u.endswith("/Show/"):
            return _webdav_response(parent)
        if u.endswith("/Show.S01E02.1080p/"):
            return _webdav_response(e02)
        if u.endswith("/Show.S01E03.1080p/"):
            return _webdav_response(e03)
        raise AssertionError("unexpected PROPFIND URL: {}".format(u))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/Show/", title_hint="Show.S01E02.1080p.WEB-DL")

    assert path == "/content/Show/Show.S01E02.1080p/video.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_nearest_dir_tag_not_masked_by_ancestor_pack(
    mock_urlopen, mock_settings
):
    """When the NEAREST parent dir carries its OWN episode tag it is
    authoritative -- an ancestor pack folder (Show.S01E02.Pack) must NOT
    rescue a wrong nearer subfolder (Show.S01E03). The real S01E02 sibling
    wins, not the larger S01E03."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Show.S01E02.Pack/", True, None),
            ("/content/Show.S01E02.Pack/Show.S01E02/", True, None),
            ("/content/Show.S01E02.Pack/Show.S01E03/", True, None),
        ]
    )
    e02 = _propfind_listing(
        [
            ("/content/Show.S01E02.Pack/Show.S01E02/", True, None),
            ("/content/Show.S01E02.Pack/Show.S01E02/video.mkv", False, 1000000000),
        ]
    )
    e03 = _propfind_listing(
        [
            ("/content/Show.S01E02.Pack/Show.S01E03/", True, None),
            ("/content/Show.S01E02.Pack/Show.S01E03/video.mkv", False, 5000000000),
        ]
    )

    def propfind(req, **_kw):
        u = req.full_url
        if u.endswith("/Show.S01E02.Pack/"):
            return _webdav_response(parent)
        if u.endswith("/Show.S01E02/"):
            return _webdav_response(e02)
        if u.endswith("/Show.S01E03/"):
            return _webdav_response(e03)
        raise AssertionError("unexpected PROPFIND URL: {}".format(u))

    mock_urlopen.side_effect = propfind

    path = find_video_file(
        "/content/Show.S01E02.Pack/", title_hint="Show.S01E02.1080p.WEB-DL"
    )

    # NOT the larger S01E03: the nearest dir's own tag rules it out even
    # though the ancestor pack (Show.S01E02.Pack) covers the requested E02.
    assert path == "/content/Show.S01E02.Pack/Show.S01E02/video.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_grandparent_tag_matches_when_nearest_dir_generic(
    mock_urlopen, mock_settings
):
    """When the nearest dir is generic ("1080p") with no tag of its own, the
    fallback widens to the full ancestor path so a grandparent's episode tag
    (Show.S01E02) still supplies the match -- locks in the widen branch."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Show.S01E02/", True, None),
            ("/content/Show.S01E02/1080p/", True, None),
        ]
    )
    nested = _propfind_listing(
        [
            ("/content/Show.S01E02/1080p/", True, None),
            ("/content/Show.S01E02/1080p/video.mkv", False, 1000000000),
        ]
    )

    def propfind(req, **_kw):
        u = req.full_url
        if u.endswith("/Show.S01E02/"):
            return _webdav_response(parent)
        if u.endswith("/1080p/"):
            return _webdav_response(nested)
        raise AssertionError("unexpected PROPFIND URL: {}".format(u))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/Show.S01E02/", title_hint="Show.S01E02.1080p")

    assert path == "/content/Show.S01E02/1080p/video.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_basename_episode_overrides_parent_range_dir(
    mock_urlopen, mock_settings
):
    """Regression guard for layered-not-union: a matching DIRECTORY range tag
    must NOT mask a wrong-episode FILENAME. The basename's own tag is
    authoritative, so the larger wrong-episode file stays rejected."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show.S02E01-E10.Pack/", True, None),
            (
                "/content/Show.S02E01-E10.Pack/Show.S02E07.1080p.mkv",
                False,
                9000000000,
            ),
            (
                "/content/Show.S02E01-E10.Pack/Show.S02E05.1080p.mkv",
                False,
                2000000000,
            ),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file(
        "/content/Show.S02E01-E10.Pack/", title_hint="Show.S02E05.1080p.WEB-DL"
    )

    # NOT the larger E07: its own basename tag rules it out even though the
    # parent dir range (S02E01-E10) covers the requested E05.
    assert path == "/content/Show.S02E01-E10.Pack/Show.S02E05.1080p.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_season_complete_parent_does_not_falsely_match(
    mock_urlopen, mock_settings
):
    """Boundary: a season-complete parent folder (no episode tag) must not
    falsely supply an episode tag to generic videos -- largest-wins holds."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show.S01.Complete/", True, None),
            ("/content/Show.S01.Complete/part1.mkv", False, 2000000000),
            ("/content/Show.S01.Complete/part2.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file("/content/Show.S01.Complete/", title_hint="Show.S01E02")

    assert path == "/content/Show.S01.Complete/part2.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_recurses_past_wrong_episode_at_current_level(
    mock_urlopen, mock_settings
):
    """When the only video at the current level is an explicit episode MISMATCH
    and a hint was given, prefer recursing into siblings that may hold the
    requested episode before falling back to the wrong-episode file."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Pack/", True, None),
            # A wrong-episode video sits at the CURRENT level (would normally be
            # returned immediately, blocking the descent into Extras/).
            ("/content/Pack/Show.S02E06.1080p.mkv", False, 9000000000),
            ("/content/Pack/Extras/", True, None),
        ]
    )
    extras = _propfind_listing(
        [
            ("/content/Pack/Extras/", True, None),
            ("/content/Pack/Extras/Show.S02E05.1080p.mkv", False, 2000000000),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Pack/"):
            return _webdav_response(parent)
        if url.endswith("/Extras/"):
            return _webdav_response(extras)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/Pack/", title_hint="Show.S02E05.1080p.WEB-DL")

    assert path == "/content/Pack/Extras/Show.S02E05.1080p.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_keeps_wrong_episode_when_siblings_have_no_match(
    mock_urlopen, mock_settings
):
    """If recursion finds no matching episode, fall back to the current-level
    (mismatched) video rather than returning nothing."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Pack/", True, None),
            ("/content/Pack/Show.S02E06.1080p.mkv", False, 9000000000),
            ("/content/Pack/Empty/", True, None),
        ]
    )
    empty = _propfind_listing([("/content/Pack/Empty/", True, None)])

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Pack/"):
            return _webdav_response(parent)
        if url.endswith("/Empty/"):
            return _webdav_response(empty)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/Pack/", title_hint="Show.S02E05.1080p.WEB-DL")

    assert path == "/content/Pack/Show.S02E06.1080p.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_matches_middle_episode_of_multi_ep_tag(
    mock_urlopen, mock_settings
):
    """A multi-episode file like S01E01E02E03 must match a request for a middle
    episode (E02), not only the first listed episode."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.S01E01E02E03.1080p.mkv", False, 2000000000),
            ("/content/Show/Show.S01E04.1080p.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file("/content/Show/", title_hint="Show.S01E02.1080p.WEB-DL")

    assert path == "/content/Show/Show.S01E01E02E03.1080p.mkv"


def test_episode_tags_recognizes_nxnn_and_ignores_resolution():
    from resources.lib.webdav import _episode_tags

    assert _episode_tags("Show.2x05.mkv") == frozenset({(2, 5)})
    assert _episode_tags("Show.02x05.1080p.mkv") == frozenset({(2, 5)})
    assert _episode_tags("Movie.1920x1080.mkv") == frozenset()
    assert _episode_tags("Movie.2160p.x265.mkv") == frozenset()
    assert _episode_tags("Show.S02E05.mkv") == frozenset({(2, 5)})  # unchanged
    # Adjacent NxNN tags must ALL register: a consuming boundary ate the
    # separator between neighbours and dropped the second (and the middle of
    # a 3-pack). Zero-width digit lookarounds keep every tag.
    assert _episode_tags("Show.1x01.1x02.mkv") == frozenset({(1, 1), (1, 2)})
    assert _episode_tags("Doctor.Who.2x05.2x06.mkv") == frozenset({(2, 5), (2, 6)})
    assert _episode_tags("Pack.1x01.1x02.1x03.mkv") == frozenset(
        {(1, 1), (1, 2), (1, 3)}
    )
    # Exclusions must still hold under the lookaround (the \d{1,2} season cap
    # is what excludes resolutions/codecs, not the boundary).
    assert _episode_tags("Movie.1920x1080.mkv") == frozenset()
    assert _episode_tags("Movie.2160p.x265.mkv") == frozenset()


def test_episode_tags_expands_episode_ranges():
    from resources.lib.webdav import _episode_tags

    # SxxEaa-Ebb and SxxEaa-bb both cover the middle episode.
    assert _episode_tags("Show.S01E01-E03.1080p.mkv") == frozenset(
        {(1, 1), (1, 2), (1, 3)}
    )
    assert _episode_tags("Show.S01E01-03.1080p.mkv") == frozenset(
        {(1, 1), (1, 2), (1, 3)}
    )
    # Single episode is unchanged (no over-expansion).
    assert _episode_tags("Show.S01E02.mkv") == frozenset({(1, 2)})
    # Absurd / reversed ranges do not explode; literal endpoints survive.
    assert _episode_tags("Show.S01E01-E999.mkv") == frozenset({(1, 1), (1, 999)})
    assert _episode_tags("Show.S01E05-E01.mkv") == frozenset({(1, 1), (1, 5)})
    # Over-match guard: resolutions/codecs/season ranges do NOT register.
    assert _episode_tags("Movie.2019.1920x1080.x265.mkv") == frozenset()
    assert _episode_tags("Show.S01-S03.Complete.1080p.mkv") == frozenset()


def test_episode_tags_expands_nxn_ranges():
    from resources.lib.webdav import _episode_tags

    # NxN range notation ("1x01-03", "1x01-1x03") covers the middle episode,
    # mirroring the SxxExx range expansion.
    assert _episode_tags("Show.1x01-03.mkv") == frozenset({(1, 1), (1, 2), (1, 3)})
    assert _episode_tags("Show.1x01-1x03.mkv") == frozenset({(1, 1), (1, 2), (1, 3)})
    assert _episode_tags("Show.10x01-10x12.mkv") == frozenset(
        {(10, e) for e in range(1, 13)}
    )
    # Single NxN unchanged; resolutions/codecs still excluded.
    assert _episode_tags("Show.1x02.mkv") == frozenset({(1, 2)})
    assert _episode_tags("Movie.1920x1080.mkv") == frozenset()
    assert _episode_tags("Movie.1280x720.x264.mkv") == frozenset()
    assert _episode_tags("Movie.3840x2160.mkv") == frozenset()
    # Adjacent dot-separated NxN tags must NOT collapse into a range (the
    # range regex requires a literal '-').
    assert _episode_tags("Doctor.Who.2x05.2x06.mkv") == frozenset({(2, 5), (2, 6)})
    # Reversed / cross-season spans: literal endpoints survive via the
    # standalone NxN loop, no explosion.
    assert _episode_tags("Show.1x05-1x02.mkv") == frozenset({(1, 2), (1, 5)})
    assert _episode_tags("Show.1x10-2x02.mkv") == frozenset({(1, 10), (2, 2)})


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_matches_nxn_episode_in_range_pack(mock_urlopen, mock_settings):
    """A request for a middle episode (1x02) must pick the NxN range pack that
    covers it (1x01-03) over a larger non-covering range pack (1x04-06)."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.1x01-03.mkv", False, 8000000000),
            ("/content/Show/Show.1x04-06.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)
    path = find_video_file("/content/Show/", title_hint="Show 1x02")
    assert path == "/content/Show/Show.1x01-03.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_matches_episode_in_range_pack(mock_urlopen, mock_settings):
    """A request for a middle episode (E02) must pick the range pack that
    covers it (S01E01-E03) over a larger non-covering range pack."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.S01E01-E03.1080p.mkv", False, 8000000000),
            ("/content/Show/Show.S01E04-E06.1080p.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)
    path = find_video_file("/content/Show/", title_hint="Show.S01E02.1080p.WEB-DL")
    assert path == "/content/Show/Show.S01E01-E03.1080p.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_matches_nxnn_episode_notation(mock_urlopen, mock_settings):
    """A request whose title uses NxNN notation (2x05) must pick the requested
    episode, not the larger sibling -- same guarantee as SxxExx."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.2x05.1080p.mkv", False, 1000000000),
            ("/content/Show/Show.2x06.1080p.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file("/content/Show/", title_hint="Show 2x05")

    assert path == "/content/Show/Show.2x05.1080p.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_matches_nxnn_multi_episode_pack(mock_urlopen, mock_settings):
    """A folder whose only/largest file is an adjacent-NxNN multi-episode pack
    (2x05.2x06) must be returned for a hint covering one of its episodes,
    guarding the +1000 scoring short-circuit (not just the regex)."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Doctor.Who.2x05.2x06.mkv", False, 5000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file("/content/Show/", title_hint="Doctor Who 2x06")

    assert path == "/content/Show/Doctor.Who.2x05.2x06.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_keeps_larger_current_level_over_smaller_equalscore_sibling(
    mock_urlopen, mock_settings
):
    """A deferred current-level fallback (generic, ep_score 0) must not be
    replaced by a SMALLER sibling that merely ties on episode score."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Pack/", True, None),
            ("/content/Pack/Show.Complete.1080p.BIG.mkv", False, 9000000000),
            ("/content/Pack/Extras/", True, None),
        ]
    )
    extras = _propfind_listing(
        [
            ("/content/Pack/Extras/", True, None),
            ("/content/Pack/Extras/blooper.SMALL.mkv", False, 1000000),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Pack/"):
            return _webdav_response(parent)
        if url.endswith("/Extras/"):
            return _webdav_response(extras)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/Pack/", title_hint="Show.S02E05.1080p.WEB-DL")

    assert path == "/content/Pack/Show.Complete.1080p.BIG.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_stream_for_folder_passes_title_hint(mock_urlopen, mock_settings):
    """find_video_stream_for_folder threads the hint into find_video_file."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.S02E05.1080p.mkv", False, 2000000000),
            ("/content/Show/Show.S02E06.1080p.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    video_path, stream_url, _headers = find_video_stream_for_folder(
        "/content/Show/", title_hint="Show.S02E05.1080p.WEB-DL"
    )

    assert video_path == "/content/Show/Show.S02E05.1080p.mkv"
    assert stream_url.endswith("/content/Show/Show.S02E05.1080p.mkv")


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_recurses_past_generic_nonepisode_at_current_level(
    mock_urlopen, mock_settings
):
    """A generic current-level video that shares show tokens but carries NO
    SxxExx tag scores non-negative and would be returned before scanning
    subdirs. With an episode hint, recurse first so the requested episode
    living in a subfolder is found instead of the loose token match."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Pack/", True, None),
            # Generic (no SxxExx) video at the CURRENT level. It shares show
            # tokens with the hint, so it scores >= 0 and the old gate (which
            # only recursed on a confirmed wrong-episode < 0 score) returned it
            # immediately, never scanning E05/.
            ("/content/Pack/Show.Sample.1080p.WEB-DL.mkv", False, 50000000),
            ("/content/Pack/E05/", True, None),
        ]
    )
    e05 = _propfind_listing(
        [
            ("/content/Pack/E05/", True, None),
            ("/content/Pack/E05/Show.S02E05.1080p.WEB-DL.mkv", False, 2000000000),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Pack/"):
            return _webdav_response(parent)
        if url.endswith("/E05/"):
            return _webdav_response(e05)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/Pack/", title_hint="Show.S02E05.1080p.WEB-DL")

    assert path == "/content/Pack/E05/Show.S02E05.1080p.WEB-DL.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_movie_hint_keeps_largest_over_token_heavy_extra(
    mock_urlopen, mock_settings
):
    """For a movie hint (no SxxExx tag) the large feature must win over a
    small, token-rich extra/trailer. Splitting the score so raw token overlap
    ranks BELOW size keeps the feature from being hijacked by a featurette
    that happens to share more tokens with the release name."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Dune/", True, None),
            ("/content/Dune/Dune.mkv", False, 80_000_000_000),
            (
                "/content/Dune/Dune.Part.Two.2024.Behind.The.Scenes.2160p.mkv",
                False,
                300_000_000,
            ),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file(
        "/content/Dune/",
        title_hint="Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX-FraMeSToR",
    )

    assert path == "/content/Dune/Dune.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_movie_hint_keeps_largest_across_sibling_subfolders(
    mock_urlopen, mock_settings
):
    """The same size-over-token-overlap rule must hold across sibling
    subfolders (covers the _find_video_file_in_subdirs key change): the large
    feature in Movie/ beats the small token-heavy extra in Extras/."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Dune/", True, None),
            ("/content/Dune/Movie/", True, None),
            ("/content/Dune/Extras/", True, None),
        ]
    )
    movie = _propfind_listing(
        [
            ("/content/Dune/Movie/", True, None),
            ("/content/Dune/Movie/Dune.mkv", False, 80_000_000_000),
        ]
    )
    extras = _propfind_listing(
        [
            ("/content/Dune/Extras/", True, None),
            (
                "/content/Dune/Extras/Dune.Part.Two.2024.Behind.The.Scenes.2160p.mkv",
                False,
                300_000_000,
            ),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Dune/"):
            return _webdav_response(parent)
        if url.endswith("/Movie/"):
            return _webdav_response(movie)
        if url.endswith("/Extras/"):
            return _webdav_response(extras)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file(
        "/content/Dune/",
        title_hint="Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX-FraMeSToR",
    )

    assert path == "/content/Dune/Movie/Dune.mkv"


def test_episode_tags_ignores_aspect_ratio_nxn_tokens():
    """Aspect-ratio NxN tokens (16x9, 4x3, 21x9, ...) must NOT register as
    episodes, while real un-padded NxN episodes (2x3, 1x9) still do."""
    from resources.lib.webdav import _episode_tags

    # Well-known aspect ratios yield no episode tag.
    assert _episode_tags("video.16x9.mkv") == frozenset()
    assert _episode_tags("video.4x3.mkv") == frozenset()
    assert _episode_tags("video.21x9.mkv") == frozenset()
    assert _episode_tags("video.16x10.mkv") == frozenset()
    # Real episodes -- including the un-padded forms -- still register.
    assert _episode_tags("Show.2x3.mkv") == frozenset({(2, 3)})
    assert _episode_tags("Show.1x9.mkv") == frozenset({(1, 9)})
    assert _episode_tags("Show.2x05.mkv") == frozenset({(2, 5)})
    assert _episode_tags("Show.10x01.mkv") == frozenset({(10, 1)})
    assert _episode_tags("Show.1x02.mkv") == frozenset({(1, 2)})
    assert _episode_tags("Show.12x05.mkv") == frozenset({(12, 5)})


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_aspect_ratio_file_does_not_beat_real_episode(
    mock_urlopen, mock_settings
):
    """An aspect-ratio-named file (video.16x9.mkv) under the requested
    episode's folder must NOT be mis-parsed as episode (16, 9). Under the bug
    its basename tag (16, 9) was treated as authoritative and never fell back
    to the parent's real S01E02, so the file scored a wrong-episode -1000 and a
    larger wrong-episode sibling (S01E03) wrongly won. After the fix the 16x9
    basename carries no tag, falls back to the parent S01E02, and wins."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.S01E02/", True, None),
            ("/content/Show/Show.S01E03/", True, None),
        ]
    )
    e02 = _propfind_listing(
        [
            ("/content/Show/Show.S01E02/", True, None),
            ("/content/Show/Show.S01E02/video.16x9.mkv", False, 2000000000),
        ]
    )
    e03 = _propfind_listing(
        [
            ("/content/Show/Show.S01E03/", True, None),
            ("/content/Show/Show.S01E03/video.mkv", False, 9000000000),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Show/"):
            return _webdav_response(parent)
        if url.endswith("/Show.S01E02/"):
            return _webdav_response(e02)
        if url.endswith("/Show.S01E03/"):
            return _webdav_response(e03)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file("/content/Show/", title_hint="Show.S01E02.1080p.WEB-DL")

    assert path == "/content/Show/Show.S01E02/video.16x9.mkv"


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_movie_hint_recurses_past_zero_size_token_file(
    mock_urlopen, mock_settings
):
    """A movie hint (no episode tags) must not adopt a zero-size token-matching
    placeholder at the current level; recursion finds the real large feature in
    a subfolder. Token overlap must not outrank size."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    parent = _propfind_listing(
        [
            ("/content/Dune/", True, None),
            ("/content/Dune/Dune.Part.Two.teaser.mkv", False, None),
            ("/content/Dune/Feature/", True, None),
        ]
    )
    feature = _propfind_listing(
        [
            ("/content/Dune/Feature/", True, None),
            ("/content/Dune/Feature/Dune.Part.Two.2024.mkv", False, 9000000000),
        ]
    )

    def propfind(req, **_kwargs):
        url = req.full_url
        if url.endswith("/Dune/"):
            return _webdav_response(parent)
        if url.endswith("/Feature/"):
            return _webdav_response(feature)
        raise AssertionError("unexpected PROPFIND URL: {}".format(url))

    mock_urlopen.side_effect = propfind

    path = find_video_file(
        "/content/Dune/", title_hint="Dune.Part.Two.2024.2160p.BluRay"
    )

    assert path == "/content/Dune/Feature/Dune.Part.Two.2024.mkv"


def test_episode_tags_expands_repeated_season_ranges():
    """The repeated-season range form S01E01-S01E03 must expand to the full
    inclusive span, same as S01E01-E03."""
    from resources.lib.webdav import _episode_tags

    assert _episode_tags("Show.S01E01-S01E03.1080p.mkv") == frozenset(
        {(1, 1), (1, 2), (1, 3)}
    )
    # A wider repeated-season range still expands.
    assert _episode_tags("Show.S02E01-S02E05.mkv") == frozenset(
        {(2, 1), (2, 2), (2, 3), (2, 4), (2, 5)}
    )
    # Cross-season range keeps only the literal endpoints (backref forces the
    # same season, so the repeated-season expansion does not apply).
    assert _episode_tags("Show.S01E10-S02E02.mkv") == frozenset({(1, 10), (2, 2)})
    # Resolutions / codecs still register nothing.
    assert _episode_tags("Movie.1920x1080.x265.mkv") == frozenset()


@patch("resources.lib.webdav._get_settings")
@patch("resources.lib.webdav.urlopen")
def test_find_video_file_repeated_season_pack_covers_middle_episode(
    mock_urlopen, mock_settings
):
    """A hint for a covered middle episode (S01E02) must select the
    repeated-season pack S01E01-S01E03 over a larger non-covering sibling."""
    mock_settings.return_value = _SETTINGS_WITH_AUTH
    listing = _propfind_listing(
        [
            ("/content/Show/", True, None),
            ("/content/Show/Show.S01E01-S01E03.1080p.mkv", False, 2000000000),
            ("/content/Show/Show.S01E05.1080p.mkv", False, 9000000000),
        ]
    )
    mock_urlopen.return_value = _webdav_response(listing)

    path = find_video_file("/content/Show/", title_hint="Show.S01E02.1080p.WEB-DL")

    assert path == "/content/Show/Show.S01E01-S01E03.1080p.mkv"
