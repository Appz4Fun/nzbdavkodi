# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Unit tests for stream_proxy.py remux and range-serving logic."""

from unittest.mock import MagicMock, patch

from resources.lib.stream_proxy import _StreamHandler

# ---------------------------------------------------------------------------
# _StreamHandler._parse_range
# ---------------------------------------------------------------------------


def _make_handler():
    return _StreamHandler.__new__(_StreamHandler)


def _make_handler_with_server(ctx, range_header=None, current_byte_pos=0):
    """Create a _StreamHandler wired to a mock server for handler-level tests."""
    import threading

    handler = _StreamHandler.__new__(_StreamHandler)

    handler.server = MagicMock()
    handler.server.stream_context = ctx
    handler.server.active_ffmpeg = None
    handler.server.current_byte_pos = current_byte_pos
    handler.server.ffmpeg_lock = threading.Lock()

    handler.headers = {"Range": range_header} if range_header else {}
    handler.wfile = MagicMock()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.send_error = MagicMock()

    return handler


def test_parse_range_standard():
    h = _make_handler()
    assert h._parse_range("bytes=0-999", 10000) == (0, 999)


def test_parse_range_open_ended():
    h = _make_handler()
    assert h._parse_range("bytes=500-", 10000) == (500, 9999)


def test_parse_range_suffix():
    h = _make_handler()
    assert h._parse_range("bytes=-100", 10000) == (9900, 9999)


def test_parse_range_clamps():
    h = _make_handler()
    assert h._parse_range("bytes=0-99999", 1000) == (0, 999)


def test_parse_range_invalid():
    h = _make_handler()
    assert h._parse_range("invalid", 10000) == (None, None)


def test_parse_range_zero_start():
    h = _make_handler()
    assert h._parse_range("bytes=0-", 500) == (0, 499)


# ---------------------------------------------------------------------------
# _validate_url
# ---------------------------------------------------------------------------


def test_validate_url_rejects_none():
    import pytest
    from resources.lib.stream_proxy import _validate_url

    with pytest.raises(ValueError, match="None"):
        _validate_url(None)


def test_validate_url_rejects_ftp():
    import pytest
    from resources.lib.stream_proxy import _validate_url

    with pytest.raises(ValueError):
        _validate_url("ftp://host/file.mp4")


def test_validate_url_accepts_http():
    from resources.lib.stream_proxy import _validate_url

    _validate_url("http://host/file.mp4")  # should not raise


def test_validate_url_accepts_https():
    from resources.lib.stream_proxy import _validate_url

    _validate_url("https://host/file.mp4")  # should not raise


# ---------------------------------------------------------------------------
# _embed_auth_in_url
# ---------------------------------------------------------------------------


def test_embed_auth_none_header():
    from resources.lib.stream_proxy import _embed_auth_in_url

    assert _embed_auth_in_url("http://host/file.mp4", None) == "http://host/file.mp4"


def test_embed_auth_basic():
    import base64

    from resources.lib.stream_proxy import _embed_auth_in_url

    auth = "Basic " + base64.b64encode(b"user:pass").decode()
    result = _embed_auth_in_url("http://host/file.mp4", auth)
    assert result == "http://user:pass@host/file.mp4"


def test_embed_auth_non_basic_ignored():
    from resources.lib.stream_proxy import _embed_auth_in_url

    assert (
        _embed_auth_in_url("http://host/file.mp4", "Bearer tok")
        == "http://host/file.mp4"
    )


# ---------------------------------------------------------------------------
# StreamProxy._detect_content_type
# ---------------------------------------------------------------------------


def test_detect_content_type_mkv():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    assert sp._detect_content_type("http://host/file.mkv") == "video/x-matroska"


def test_detect_content_type_mp4():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    assert sp._detect_content_type("http://host/file.mp4") == "video/mp4"


def test_detect_content_type_avi():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    assert sp._detect_content_type("http://host/film.avi") == "video/x-msvideo"


# ---------------------------------------------------------------------------
# StreamProxy lifecycle
# ---------------------------------------------------------------------------


def test_stream_proxy_start_assigns_port():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy()
    sp.start()
    try:
        assert sp.port > 0
    finally:
        sp.stop()


def test_stream_proxy_stop_idempotent():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy()
    sp.stop()


# ---------------------------------------------------------------------------
# StreamProxy._get_content_length
# ---------------------------------------------------------------------------


def test_get_content_length_from_head():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.headers.get.return_value = "12345"

    with patch("resources.lib.stream_proxy.urlopen", return_value=mock_resp):
        assert sp._get_content_length("http://host/file.mp4", None) == 12345


def test_get_content_length_returns_zero_on_failure():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    with patch("resources.lib.stream_proxy.urlopen", side_effect=OSError("fail")):
        assert sp._get_content_length("http://host/file.mp4", None) == 0


# ---------------------------------------------------------------------------
# StreamProxy.prepare_stream — remux vs proxy
# ---------------------------------------------------------------------------


def test_prepare_stream_remuxes_mp4_when_ffmpeg_available():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    mock_proc = MagicMock()
    mock_proc.stderr = iter([b"  Duration: 01:00:00.00, start: 0.000000\n"])

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
    ), patch.object(sp, "_get_content_length", return_value=5000000000), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ), patch(
        "resources.lib.stream_proxy.fetch_remote_mp4_layout", return_value=None
    ), patch.object(sp, "_prepare_tempfile_faststart", return_value=None):
        auth = "Basic " + __import__("base64").b64encode(b"user:pass").decode()
        url, info = sp.prepare_stream("http://host/film.mp4", auth_header=auth)

    assert url == "http://127.0.0.1:9999/stream"
    ctx = sp._server.stream_context
    assert ctx["remux"] is True
    assert ctx["seekable"] is True
    assert ctx["duration_seconds"] == 3600.0
    assert info["duration_seconds"] == 3600.0
    assert info["seekable"] is True


def test_prepare_stream_proxies_mkv():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    with patch.object(sp, "_get_content_length", return_value=100000):
        sp.prepare_stream("http://host/film.mkv")

    ctx = sp._server.stream_context
    assert ctx["remux"] is False
    assert ctx["content_length"] == 100000


def test_prepare_stream_falls_back_to_proxy_without_ffmpeg():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value=None
    ), patch.object(sp, "_get_content_length", return_value=500000), patch(
        "resources.lib.stream_proxy.fetch_remote_mp4_layout", return_value=None
    ):
        sp.prepare_stream("http://host/film.mp4")

    ctx = sp._server.stream_context
    assert ctx["remux"] is False


# ---------------------------------------------------------------------------
# _probe_duration — parse duration from ffmpeg stderr
# ---------------------------------------------------------------------------


def test_probe_duration_parses_hms():
    from resources.lib.stream_proxy import _parse_ffmpeg_duration

    stderr = "  Duration: 01:30:45.67, start: 0.000000, bitrate: 30000 kb/s\n"
    assert _parse_ffmpeg_duration(stderr) == 5445.67


def test_probe_duration_parses_minutes_only():
    from resources.lib.stream_proxy import _parse_ffmpeg_duration

    stderr = "  Duration: 00:02:30.00, start: 0.000000\n"
    assert _parse_ffmpeg_duration(stderr) == 150.0


def test_probe_duration_returns_none_on_missing():
    from resources.lib.stream_proxy import _parse_ffmpeg_duration

    assert _parse_ffmpeg_duration("no duration here") is None


def test_probe_duration_returns_none_on_n_a():
    from resources.lib.stream_proxy import _parse_ffmpeg_duration

    stderr = "  Duration: N/A, start: 0.000000\n"
    assert _parse_ffmpeg_duration(stderr) is None


# ---------------------------------------------------------------------------
# StreamProxy.prepare_stream — duration probe for MP4
# ---------------------------------------------------------------------------


def test_prepare_stream_probes_duration_for_mp4():
    import base64

    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    mock_proc = MagicMock()
    mock_proc.stderr = iter(
        [b"  Duration: 02:00:00.00, start: 0.000000, bitrate: 30000 kb/s\n"]
    )

    auth = "Basic " + base64.b64encode(b"user:pass").decode()
    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
    ), patch.object(sp, "_get_content_length", return_value=5000000000), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ), patch(
        "resources.lib.stream_proxy.fetch_remote_mp4_layout", return_value=None
    ), patch.object(sp, "_prepare_tempfile_faststart", return_value=None):
        sp.prepare_stream("http://host/film.mp4", auth_header=auth)

    ctx = sp._server.stream_context
    assert ctx["remux"] is True
    assert ctx["duration_seconds"] == 7200.0
    assert ctx["total_bytes"] == 5000000000
    assert ctx["seekable"] is True


def test_prepare_stream_falls_back_to_non_seekable_on_probe_failure():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    mock_proc = MagicMock()
    mock_proc.stderr = iter([b"some error\n"])

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
    ), patch.object(sp, "_get_content_length", return_value=5000000000), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ), patch(
        "resources.lib.stream_proxy.fetch_remote_mp4_layout", return_value=None
    ), patch.object(sp, "_prepare_tempfile_faststart", return_value=None):
        sp.prepare_stream("http://host/film.mp4")

    ctx = sp._server.stream_context
    assert ctx["remux"] is True
    assert ctx["seekable"] is False


# ---------------------------------------------------------------------------
# Seek detection — is_seek_request
# ---------------------------------------------------------------------------


def test_seek_detection_continuation():
    """Request within threshold of current position is NOT a seek."""
    from resources.lib.stream_proxy import _SEEK_THRESHOLD, _is_seek_request

    assert _is_seek_request(0, _SEEK_THRESHOLD - 1) is False


def test_seek_detection_forward_jump():
    """Request beyond threshold IS a seek."""
    from resources.lib.stream_proxy import _SEEK_THRESHOLD, _is_seek_request

    assert _is_seek_request(0, _SEEK_THRESHOLD + 1) is True


def test_seek_detection_backward():
    """Any backward request IS a seek."""
    from resources.lib.stream_proxy import _is_seek_request

    assert _is_seek_request(50000000, 10000000) is True


def test_seek_detection_from_zero():
    """Request at 0 when current is 0 is NOT a seek."""
    from resources.lib.stream_proxy import _is_seek_request

    assert _is_seek_request(0, 0) is False


# ---------------------------------------------------------------------------
# _build_ffmpeg_cmd — subtitle flag toggling
# ---------------------------------------------------------------------------


def test_build_ffmpeg_cmd_includes_subs_by_default():
    """Default: subtitle mapping flags are present."""
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
    }
    cmd = handler._build_ffmpeg_cmd(ctx)
    assert "-map" in cmd
    # Check subs mapping is present (0:s? appears after 0:a)
    assert "0:s?" in cmd
    assert "srt" in cmd


def test_build_ffmpeg_cmd_excludes_subs_when_setting_off():
    """When proxy_convert_subs is false, no subtitle flags."""
    import sys

    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
    }

    mock_addon = MagicMock()
    mock_addon.getSetting.return_value = "false"
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        cmd = handler._build_ffmpeg_cmd(ctx)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    assert "0:s?" not in cmd
    assert "srt" not in cmd


def test_build_ffmpeg_cmd_includes_seek():
    """When seek_seconds is set, -ss appears before -i."""
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
    }
    cmd = handler._build_ffmpeg_cmd(ctx, seek_seconds=3600.5)
    ss_idx = cmd.index("-ss")
    i_idx = cmd.index("-i")
    assert ss_idx < i_idx
    assert cmd[ss_idx + 1] == "3600.500"


def test_build_ffmpeg_cmd_no_seek_when_none():
    """When seek_seconds is None, -ss is not in the command."""
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
    }
    cmd = handler._build_ffmpeg_cmd(ctx, seek_seconds=None)
    assert "-ss" not in cmd


def test_build_ffmpeg_cmd_embeds_basic_auth():
    """Basic auth header is embedded in the URL for ffmpeg."""
    import base64

    handler = _make_handler()
    auth = "Basic " + base64.b64encode(b"user:pass").decode()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": auth,
    }
    cmd = handler._build_ffmpeg_cmd(ctx)
    i_idx = cmd.index("-i")
    assert "user:pass@host" in cmd[i_idx + 1]


# ---------------------------------------------------------------------------
# _serve_remux — handler-level tests
# ---------------------------------------------------------------------------


def test_serve_remux_continuation_seeks_to_position():
    """A continuation request (within threshold of current pos) must still
    seek ffmpeg to the requested byte position, not restart from byte 0."""
    ctx = {
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 10000000000,
        "duration_seconds": 7200.0,
        "seekable": True,
        "remux": True,
    }

    # 5 MB ahead of current — within 10 MB threshold, classified as continuation
    handler = _make_handler_with_server(
        ctx, range_header="bytes=500000000-", current_byte_pos=495000000
    )

    mock_proc = MagicMock()
    mock_proc.stdout.read.return_value = b""
    mock_proc.stderr.read.return_value = b""

    with patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        handler._serve_remux(ctx)

    cmd = mock_popen.call_args[0][0]
    assert "-ss" in cmd, "Continuation should use -ss to resume at position"
    ss_idx = cmd.index("-ss")
    seek_val = float(cmd[ss_idx + 1])
    # 500000000 / 10000000000 * 7200 = 360.0 seconds
    assert abs(seek_val - 360.0) < 0.1


def test_serve_remux_explicit_seek_kills_existing():
    """An explicit seek (large jump) kills the existing ffmpeg process."""
    ctx = {
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 10000000000,
        "duration_seconds": 7200.0,
        "seekable": True,
        "remux": True,
    }

    old_proc = MagicMock()
    handler = _make_handler_with_server(
        ctx, range_header="bytes=5000000000-", current_byte_pos=100000000
    )
    handler.server.active_ffmpeg = old_proc

    mock_proc = MagicMock()
    mock_proc.stdout.read.return_value = b""
    mock_proc.stderr.read.return_value = b""

    with patch("resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc):
        handler._serve_remux(ctx)

    old_proc.kill.assert_called_once()
    old_proc.wait.assert_called_once()


def test_serve_remux_non_seekable_no_ss():
    """Non-seekable remux does not include -ss even with a Range header."""
    ctx = {
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 10000000000,
        "duration_seconds": None,
        "seekable": False,
        "remux": True,
    }

    handler = _make_handler_with_server(ctx, range_header="bytes=500000000-")

    mock_proc = MagicMock()
    mock_proc.stdout.read.return_value = b""
    mock_proc.stderr.read.return_value = b""

    with patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        handler._serve_remux(ctx)

    cmd = mock_popen.call_args[0][0]
    assert "-ss" not in cmd


# ---------------------------------------------------------------------------
# do_HEAD — handler-level tests
# ---------------------------------------------------------------------------


def test_head_seekable_remux_returns_accept_ranges():
    """HEAD on a seekable remux context returns Accept-Ranges: none (piped MKV has no Cues)."""
    ctx = {
        "remux": True,
        "seekable": True,
        "total_bytes": 10000000000,
    }
    handler = _make_handler_with_server(ctx)
    handler.do_HEAD()

    handler.send_response.assert_called_once_with(200)
    handler.send_header.assert_any_call("Accept-Ranges", "none")


def test_head_non_seekable_remux_no_ranges():
    """HEAD on a non-seekable remux context returns Accept-Ranges: none."""
    ctx = {
        "remux": True,
        "seekable": False,
        "total_bytes": 0,
    }
    handler = _make_handler_with_server(ctx)
    handler.do_HEAD()

    handler.send_response.assert_called_once_with(200)
    handler.send_header.assert_any_call("Accept-Ranges", "none")


def test_head_no_context_returns_404():
    """HEAD with no stream context returns 404."""
    handler = _make_handler_with_server(ctx=None)
    handler.do_HEAD()

    handler.send_error.assert_called_once_with(404)


# ---------------------------------------------------------------------------
# prepare_stream — faststart proxy path
# ---------------------------------------------------------------------------


def test_prepare_stream_uses_faststart_for_mp4():
    """prepare_stream returns faststart proxy for MP4 files."""
    import threading

    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_context = None
    sp._context_lock = threading.Lock()
    sp.port = 9999

    mock_layout = {
        "ftyp_data": b"\x00" * 32,
        "ftyp_end": 32,
        "moov_data": b"\x00" * 100,
        "mdat_offset": 32,
        "original_moov_offset": 5000000032,
        "moov_before_mdat": False,
    }
    mock_faststart = {
        "header_data": b"\x00" * 132,
        "virtual_size": 5000000132,
        "payload_remote_start": 32,
        "payload_remote_end": 5000000032,
        "payload_size": 5000000000,
        "already_faststart": False,
    }

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value=None
    ), patch.object(sp, "_get_content_length", return_value=5000000132), patch(
        "resources.lib.stream_proxy.fetch_remote_mp4_layout",
        return_value=mock_layout,
    ), patch(
        "resources.lib.stream_proxy.build_faststart_layout",
        return_value=mock_faststart,
    ):
        url, info = sp.prepare_stream(
            "http://host/film.mp4", auth_header="Basic dXNlcjpwYXNz"
        )

    assert url == "http://127.0.0.1:9999/stream"
    ctx = sp._server.stream_context
    assert ctx["faststart"] is True
    assert ctx["remux"] is False
    assert info["seekable"] is True
    assert info["virtual_size"] == 5000000132
