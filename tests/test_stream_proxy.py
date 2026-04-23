# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Unit tests for stream_proxy.py remux and range-serving logic."""

import io
import threading
from unittest.mock import MagicMock, patch

import pytest
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
    handler.server.stream_sessions = {}
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


def _make_prepare_post_handler(body=b"{}", content_length=None, path="/prepare"):
    """Construct a minimal POST handler for /prepare tests."""
    handler = _StreamHandler.__new__(_StreamHandler)
    handler.path = path
    handler.server = MagicMock()
    handler.server.owner_proxy = MagicMock()
    length = content_length if content_length is not None else len(body)
    handler.headers = {"Content-Length": str(length)}
    handler.rfile = io.BytesIO(body)
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


@pytest.mark.parametrize(
    ("range_header", "content_length"),
    [
        ("bytes=10-5", 1000),
        ("bytes=-1001", 1000),
        ("bytes=1000-", 1000),
        ("bytes=1000-1001", 1000),
        ("bytes=1-two", 1000),
        ("bytes=0-1,2-3", 1000),
    ],
)
def test_parse_range_rejects_malformed_invariants(range_header, content_length):
    h = _make_handler()
    assert h._parse_range(range_header, content_length) == (None, None)


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


def test_embed_auth_percent_encodes_reserved_chars():
    import base64

    from resources.lib.stream_proxy import _embed_auth_in_url

    auth = "Basic " + base64.b64encode(b"user@domain:pa/ss?#word").decode()
    result = _embed_auth_in_url("http://host/file.mp4", auth)
    assert result == "http://user%40domain:pa%2Fss%3F%23word@host/file.mp4"


def test_embed_auth_non_basic_ignored():
    from resources.lib.stream_proxy import _embed_auth_in_url

    assert (
        _embed_auth_in_url("http://host/file.mp4", "Bearer tok")
        == "http://host/file.mp4"
    )


def test_embed_auth_invalid_basic_ignored():
    from resources.lib.stream_proxy import _embed_auth_in_url

    assert (
        _embed_auth_in_url("http://host/file.mp4", "Basic !!!")
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


def test_stream_proxy_start_primes_ffmpeg_capabilities():
    from resources.lib.stream_proxy import StreamProxy

    with patch.object(
        StreamProxy, "_refresh_ffmpeg_capabilities", return_value={}
    ) as mock_refresh:
        sp = StreamProxy()
        sp.start()
        try:
            mock_refresh.assert_called_once_with()
        finally:
            sp.stop()


def test_probe_hls_fmp4_capability_requires_required_flags():
    from resources.lib.stream_proxy import StreamProxy

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (
        b"  -hls_segment_type <string>\n  -hls_fmp4_init_filename <string>\n",
        b"",
    )

    with patch("resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc):
        assert StreamProxy._probe_hls_fmp4_capability("/usr/bin/ffmpeg") is True


def test_probe_hls_fmp4_capability_rejects_missing_required_flags():
    from resources.lib.stream_proxy import StreamProxy

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"  -hls_segment_type <string>\n", b"")

    with patch("resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc):
        assert StreamProxy._probe_hls_fmp4_capability("/usr/bin/ffmpeg") is False


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
    ), patch(
        "resources.lib.stream_proxy._find_ffprobe", return_value=None
    ), patch.object(
        sp, "_get_content_length", return_value=5000000000
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ), patch(
        "resources.lib.stream_proxy.fetch_remote_mp4_layout", return_value=None
    ), patch.object(
        sp, "_prepare_tempfile_faststart", return_value=None
    ):
        auth = "Basic " + __import__("base64").b64encode(b"user:pass").decode()
        url, info = sp.prepare_stream("http://host/film.mp4", auth_header=auth)

    assert url.startswith("http://127.0.0.1:9999/stream/")
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
        url, _ = sp.prepare_stream("http://host/film.mkv")

    assert url.startswith("http://127.0.0.1:9999/stream/")
    ctx = sp._server.stream_context
    assert ctx["remux"] is False
    assert ctx["content_length"] == 100000


def test_prepare_stream_forces_remux_for_large_mkv():
    """Large MKV above threshold must route through ffmpeg remux so Kodi
    never sees a >4 GB Content-Length — the pass-through path overflows on
    32-bit Kodi builds with `Open - Unhandled exception`.

    Output is piped Matroska via the standard remux path, the same
    shape used by the MP4 Tier 3 fallback. An earlier iteration routed
    large files through an HLS VOD playlist for proper random-access
    seek, but Dolby Vision HEVC RPU metadata breaks across HLS segment
    boundaries on Amlogic hardware. HLS machinery stays in-tree for a
    future DV-aware router.
    """
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    huge = 15 * 1024 * 1024 * 1024  # 15 GB
    mock_proc = MagicMock()
    mock_proc.stderr = iter([b"  Duration: 01:00:00.00, start: 0.000000\n"])

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
    ), patch(
        "resources.lib.stream_proxy._find_ffprobe", return_value=None
    ), patch.object(
        sp, "_get_content_length", return_value=huge
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ):
        url, info = sp.prepare_stream("http://host/film.mkv")

    ctx = sp._server.stream_context
    assert ctx["remux"] is True
    assert ctx.get("mode") != "hls"
    assert ctx["content_type"] == "video/x-matroska"
    assert ctx["total_bytes"] == huge
    assert ctx["duration_seconds"] == 3600.0
    assert ctx["seekable"] is True
    assert ctx["ffmpeg_path"] == "/usr/bin/ffmpeg"
    # Pass-through /stream/ URL, not an HLS playlist.
    assert url.startswith("http://127.0.0.1:9999/stream/")
    assert "/hls/" not in url
    assert info["seekable"] is True


def test_prepare_stream_large_mkv_falls_back_without_ffmpeg():
    """If ffmpeg is missing we can't force remux; fall back to pass-through
    and let the user know why their large file will fail."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    huge = 15 * 1024 * 1024 * 1024

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value=None
    ), patch.object(sp, "_get_content_length", return_value=huge):
        sp.prepare_stream("http://host/film.mkv")

    ctx = sp._server.stream_context
    assert ctx["remux"] is False
    assert ctx["content_length"] == huge


def test_prepare_stream_respects_disabled_threshold():
    """Setting the threshold to 0 disables force remux entirely even for
    huge files — escape hatch for users who know their platform is fine."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    huge = 15 * 1024 * 1024 * 1024

    mock_addon = MagicMock()
    mock_addon.getSetting.return_value = "0"
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch.object(sp, "_get_content_length", return_value=huge):
            sp.prepare_stream("http://host/film.mkv")
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    ctx = sp._server.stream_context
    assert ctx["remux"] is False
    assert ctx["content_length"] == huge


def test_force_remux_threshold_default_is_nonzero():
    """The shipped default must force-remux large MKVs on 32-bit Kodi.

    When the user hasn't set the setting (empty string or unset), the
    default must be high enough that a 12 GB MKV still goes pass-through
    (preserving native seek / zero-fill recovery on medium files) but
    low enough that a 58 GB REMUX is remuxed through ffmpeg before
    Kodi's 32-bit cache overflows. Regression test for the Shawshank
    replay crash documented in memory/project_32bit_kodi_largefile_limit.md.
    """
    import sys

    from resources.lib.stream_proxy import _get_force_remux_threshold_bytes

    mock_addon = MagicMock()
    mock_addon.getSetting.return_value = ""
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        threshold = _get_force_remux_threshold_bytes()
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    # 12 GB: pass-through tested clean on CoreELEC — must NOT be remuxed.
    assert (
        threshold > 12 * 1024 * 1024 * 1024
    ), "Default threshold must not remux 12 GB MKVs (pass-through works)"
    # 58 GB: known-bad on 32-bit Kodi — must be remuxed.
    assert (
        threshold < 58 * 1024 * 1024 * 1024
    ), "Default threshold must remux 58 GB files (pass-through crashes)"


@patch("resources.lib.stream_proxy.xbmc")
def test_get_force_remux_threshold_clamps_negative_to_zero_and_logs(mock_xbmc):
    import sys

    from resources.lib.stream_proxy import _get_force_remux_threshold_bytes

    mock_addon = MagicMock()
    mock_addon.getSetting.return_value = "-1"
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        assert _get_force_remux_threshold_bytes() == 0
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    assert mock_xbmc.log.call_count == 1
    assert "force_remux_threshold_mb" in mock_xbmc.log.call_args[0][0]


@patch("resources.lib.stream_proxy.xbmc")
def test_get_force_remux_threshold_clamps_typo_high_and_logs(mock_xbmc):
    import sys

    from resources.lib.stream_proxy import (
        _FORCE_REMUX_THRESHOLD_MB_MAX,
        _get_force_remux_threshold_bytes,
    )

    mock_addon = MagicMock()
    mock_addon.getSetting.return_value = "999999999"
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        assert (
            _get_force_remux_threshold_bytes()
            == _FORCE_REMUX_THRESHOLD_MB_MAX * 1024 * 1024
        )
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    assert mock_xbmc.log.call_count == 1
    assert "force_remux_threshold_mb" in mock_xbmc.log.call_args[0][0]


def test_get_force_remux_mode_default_returns_matroska():
    """Unset / empty / '0' all return 'matroska'."""
    import sys

    from resources.lib.stream_proxy import _get_force_remux_mode

    mock_addon = MagicMock()
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        for raw in ("", "0", None):
            mock_addon.getSetting.return_value = raw
            assert _get_force_remux_mode() == "matroska"
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original


def test_get_force_remux_mode_hls_fmp4_on_one():
    """Setting '1' returns 'hls_fmp4'."""
    import sys

    from resources.lib.stream_proxy import _get_force_remux_mode

    mock_addon = MagicMock()
    mock_addon.getSetting.return_value = "1"
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        assert _get_force_remux_mode() == "hls_fmp4"
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original


def test_get_force_remux_mode_unknown_value_falls_back_to_matroska():
    """Any other value safely falls back to matroska."""
    import sys

    from resources.lib.stream_proxy import _get_force_remux_mode

    mock_addon = MagicMock()
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        for raw in ("2", "true", "garbage", "-1"):
            mock_addon.getSetting.return_value = raw
            assert _get_force_remux_mode() == "matroska"
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original


def test_prepare_stream_force_remuxes_huge_mkv_with_default_threshold():
    """A 58 GB MKV must be force-remuxed even when the user leaves the
    threshold setting at its shipped default. Regression test for
    memory/project_32bit_kodi_largefile_limit.md — pass-through of huge
    MKVs crashes 32-bit Kodi with `Open - Unhandled exception`."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    huge = 58 * 1024 * 1024 * 1024  # 58 GB, matches Shawshank REMUX
    mock_proc = MagicMock()
    mock_proc.stderr = iter([b"  Duration: 02:22:12.00, start: 0.000000\n"])

    mock_addon = MagicMock()
    # Empty string — user left the setting at its default.
    mock_addon.getSetting.return_value = ""
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch(
            "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
        ), patch(
            "resources.lib.stream_proxy._find_ffprobe", return_value=None
        ), patch.object(
            sp, "_get_content_length", return_value=huge
        ), patch(
            "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
        ):
            sp.prepare_stream("http://host/shawshank.mkv")
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    ctx = sp._server.stream_context
    assert (
        ctx["remux"] is True
    ), "58 GB MKV must be force-remuxed with the default threshold"
    assert ctx.get("mode") != "hls"
    assert ctx["content_type"] == "video/x-matroska"
    assert ctx["total_bytes"] == huge
    assert ctx["duration_seconds"] == 8532.0
    assert ctx["seekable"] is True
    assert ctx.get("hls_segment_format") is None


def test_prepare_stream_force_remux_hls_fmp4_setting_produces_hls_ctx():
    """With force_remux_mode=1 and a duration probe that succeeds,
    prepare_stream builds an HLS fmp4 ctx instead of the matroska
    ctx. Producer creation happens in _register_session (not tested
    here) — this test only asserts the ctx shape that prepare_stream
    hands to _register_session."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    huge = 58 * 1024 * 1024 * 1024
    mock_proc = MagicMock()
    mock_proc.stderr = iter([b"  Duration: 02:22:12.00, start: 0.000000\n"])

    mock_addon = MagicMock()

    def get_setting(key):
        if key == "force_remux_mode":
            return "1"
        if key == "force_remux_threshold_mb":
            return ""
        return ""

    mock_addon.getSetting.side_effect = get_setting
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch(
            "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
        ), patch(
            "resources.lib.stream_proxy._find_ffprobe", return_value=None
        ), patch.object(
            sp, "_get_content_length", return_value=huge
        ), patch(
            "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
        ), patch.object(
            StreamProxy, "_probe_dv_profile", return_value=None
        ), patch(
            "resources.lib.stream_proxy.HlsProducer"
        ) as mock_producer_cls:
            mock_producer_cls.return_value = MagicMock()
            sp.prepare_stream("http://host/shawshank.mkv")
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    ctx = sp._server.stream_context
    assert ctx["mode"] == "hls"
    assert ctx["hls_segment_format"] == "fmp4"
    assert ctx["content_type"] == "application/vnd.apple.mpegurl"
    assert ctx["remux"] is True
    assert ctx["total_bytes"] == huge
    assert ctx["duration_seconds"] == 8532.0
    assert ctx["seekable"] is True
    assert ctx["ffmpeg_path"] == "/usr/bin/ffmpeg"


def test_prepare_stream_force_remux_hls_fmp4_falls_back_when_duration_probe_fails():
    """With force_remux_mode=1 but duration probing returning None,
    prepare_stream falls back to the matroska ctx shape (not fmp4).
    Rationale: fmp4 HLS needs duration for the playlist; without it,
    the matroska branch is the safer default."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    huge = 58 * 1024 * 1024 * 1024
    mock_proc = MagicMock()
    # No "Duration:" line in stderr — _probe_duration returns None.
    mock_proc.stderr = iter([b"  Stream #0:0: Video: hevc\n"])

    mock_addon = MagicMock()

    def get_setting(key):
        if key == "force_remux_mode":
            return "1"
        return ""

    mock_addon.getSetting.side_effect = get_setting
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch(
            "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
        ), patch(
            "resources.lib.stream_proxy._find_ffprobe", return_value=None
        ), patch.object(
            sp, "_get_content_length", return_value=huge
        ), patch(
            "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
        ):
            sp.prepare_stream("http://host/shawshank.mkv")
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    ctx = sp._server.stream_context
    assert ctx.get("mode") != "hls"
    assert ctx["content_type"] == "video/x-matroska"
    assert ctx["remux"] is True


def test_prepare_stream_force_remux_hls_fmp4_falls_back_when_capability_probe_fails():
    """If the startup capability probe says this ffmpeg lacks fmp4 HLS
    support, prepare_stream must not route into the HLS branch even when
    the user opts into force_remux_mode=hls_fmp4."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = threading.Lock()
    sp.port = 9999
    sp._ffmpeg_capabilities = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "hls_fmp4": False,
    }

    huge = 58 * 1024 * 1024 * 1024
    mock_proc = MagicMock()
    mock_proc.stderr = iter([b"  Duration: 02:22:12.00, start: 0.000000\n"])

    mock_addon = MagicMock()

    def get_setting(key):
        if key == "force_remux_mode":
            return "1"
        return ""

    mock_addon.getSetting.side_effect = get_setting
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch(
            "resources.lib.stream_proxy._find_ffprobe", return_value=None
        ), patch.object(sp, "_get_content_length", return_value=huge), patch(
            "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
        ), patch(
            "resources.lib.stream_proxy.HlsProducer"
        ) as mock_producer_cls:
            sp.prepare_stream("http://host/shawshank.mkv")
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    mock_producer_cls.assert_not_called()
    ctx = sp._server.stream_context
    assert ctx.get("mode") != "hls"
    assert ctx["content_type"] == "video/x-matroska"


def test_prepare_stream_rejects_invalid_scheme():
    import pytest
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    with pytest.raises(ValueError):
        sp.prepare_stream("file:///etc/passwd")


def test_do_post_rejects_prepare_bodies_over_64k():
    handler = _make_prepare_post_handler(body=b"{}", content_length=65537)

    handler.do_POST()

    handler.send_error.assert_called_once_with(413)
    handler.server.owner_proxy.prepare_stream.assert_not_called()


def test_prepare_stream_uses_unique_session_urls():
    """Each prepare_stream must produce a unique session URL, and the
    previous session must be torn down so at most one session is live
    at a time (prevents zombie ffmpeg processes from lingering after a
    Kodi stall that never fired onPlayBackStopped)."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    with patch.object(sp, "_get_content_length", return_value=100000):
        url1, _ = sp.prepare_stream("http://host/one.mkv")
        url2, _ = sp.prepare_stream("http://host/two.mkv")

    assert url1 != url2
    # The second prepare_stream must have cleared the first session.
    assert len(sp._server.stream_sessions) == 1
    assert url2.rsplit("/", 1)[-1] in sp._server.stream_sessions


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
# _parse_ffmpeg_dv_profile
# ---------------------------------------------------------------------------


def test_parse_dv_profile_7_dual_layer():
    from resources.lib.stream_proxy import _parse_ffmpeg_dv_profile

    stderr = (
        "  Stream #0:0(und): Video: hevc (Main 10) (hvc1 / 0x31637668), ...\n"
        "    Side data:\n"
        "      DOVI configuration record: version: 1.0, profile: 7, "
        "level: 6, rpu flag: 1, el flag: 1, bl flag: 1, compatibility id: 0\n"
    )
    assert _parse_ffmpeg_dv_profile(stderr) == 7


def test_parse_dv_profile_5_single_layer():
    from resources.lib.stream_proxy import _parse_ffmpeg_dv_profile

    stderr = (
        "      DOVI configuration record: version: 1.0, profile: 5, "
        "level: 6, rpu flag: 1, el flag: 0, bl flag: 1, compatibility id: 0\n"
    )
    assert _parse_ffmpeg_dv_profile(stderr) == 5


def test_parse_dv_profile_8_compatible():
    from resources.lib.stream_proxy import _parse_ffmpeg_dv_profile

    stderr = (
        "      DOVI configuration record: version: 1.0, profile: 8, "
        "level: 6, rpu flag: 1, el flag: 0, bl flag: 1, compatibility id: 1\n"
    )
    assert _parse_ffmpeg_dv_profile(stderr) == 8


def test_parse_dv_profile_returns_none_when_absent():
    from resources.lib.stream_proxy import _parse_ffmpeg_dv_profile

    stderr = (
        "  Stream #0:0(und): Video: hevc (Main 10), yuv420p10le, "
        "3840x2160 [SAR 1:1 DAR 16:9], 24 fps, 24 tbr, 1k tbn\n"
        "  Stream #0:1(eng): Audio: truehd, 48000 Hz, 7.1, s32 (24 bit)\n"
    )
    assert _parse_ffmpeg_dv_profile(stderr) is None


def test_parse_dv_profile_returns_none_on_malformed():
    from resources.lib.stream_proxy import _parse_ffmpeg_dv_profile

    stderr = "DOVI configuration record: version: 1.0, profile: not-a-number\n"
    assert _parse_ffmpeg_dv_profile(stderr) is None


# ---------------------------------------------------------------------------
# StreamProxy.prepare_stream — DV profile gating for fmp4 HLS
# ---------------------------------------------------------------------------


def _make_fmp4_prepare_fixture(huge_size=58 * 1024 * 1024 * 1024):
    """Build the StreamProxy instance and addon/settings mocks used by the
    DV-profile gating tests. Returns (sp, mock_addon, original_addon,
    duration_proc)."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    duration_proc = MagicMock()
    duration_proc.stderr = iter([b"  Duration: 02:22:12.00, start: 0.000000\n"])

    mock_addon = MagicMock()

    def get_setting(key):
        if key == "force_remux_mode":
            return "1"
        if key == "force_remux_threshold_mb":
            return ""
        return ""

    mock_addon.getSetting.side_effect = get_setting
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    return sp, mock_addon, original, duration_proc, huge_size


def test_prepare_stream_dv_profile_7_falls_back_to_matroska():
    """A confirmed Dolby Vision profile 7 source must NOT be served as
    fmp4 HLS even when force_remux_mode=hls_fmp4. fmp4 HLS has no
    standard way to carry the BL+EL+RPU dual-layer structure, so the
    enhancement layer would be silently dropped and Amlogic's decoder
    is known to stall on the result."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp, _, original, duration_proc, huge = _make_fmp4_prepare_fixture()
    try:
        with patch(
            "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
        ), patch(
            "resources.lib.stream_proxy._find_ffprobe", return_value=None
        ), patch.object(
            sp, "_get_content_length", return_value=huge
        ), patch(
            "resources.lib.stream_proxy.subprocess.Popen", return_value=duration_proc
        ), patch.object(
            StreamProxy, "_probe_dv_profile", return_value=7
        ):
            sp.prepare_stream("http://host/dv-p7-remux.mkv")
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    ctx = sp._server.stream_context
    assert ctx.get("mode") != "hls", "P7 must fall back off the fmp4 path"
    assert ctx["content_type"] == "video/x-matroska"
    assert ctx.get("hls_segment_format") is None


def test_prepare_stream_dv_profile_5_falls_back_to_matroska():
    """Profile 5 (single-layer IPTPQc2) falls back to matroska.

    The original gate allowed P5/P8 through fmp4 on the theory that
    only dual-layer P7 was broken, but 2026-04-15 testing against a
    DV Profile 8 source on the Amlogic CoreELEC build proved the HW
    decoder doesn't actually decode DV on the fmp4 path regardless
    of profile — onAVStarted never fires, the YUV planes stay half-
    green, and Kodi freezes on stop. The gate was broadened to
    route ANY confirmed DV profile back to the matroska pipe."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp, _, original, duration_proc, huge = _make_fmp4_prepare_fixture()
    try:
        with patch(
            "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
        ), patch(
            "resources.lib.stream_proxy._find_ffprobe", return_value=None
        ), patch.object(
            sp, "_get_content_length", return_value=huge
        ), patch(
            "resources.lib.stream_proxy.subprocess.Popen", return_value=duration_proc
        ), patch.object(
            StreamProxy, "_probe_dv_profile", return_value=5
        ):
            sp.prepare_stream("http://host/dv-p5-remux.mkv")
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    ctx = sp._server.stream_context
    assert ctx.get("mode") != "hls"
    assert ctx["content_type"] == "video/x-matroska"


def test_prepare_stream_dv_profile_8_falls_back_to_matroska():
    """Profile 8 (single-layer cross-compatible) falls back to matroska
    for the same reason as P5 — the Amlogic fmp4 HW decoder path
    doesn't decode DV regardless of single-layer vs dual-layer. See
    the docstring on test_prepare_stream_dv_profile_5_falls_back_to
    _matroska for the reproduction details."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp, _, original, duration_proc, huge = _make_fmp4_prepare_fixture()
    try:
        with patch(
            "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
        ), patch(
            "resources.lib.stream_proxy._find_ffprobe", return_value=None
        ), patch.object(
            sp, "_get_content_length", return_value=huge
        ), patch(
            "resources.lib.stream_proxy.subprocess.Popen", return_value=duration_proc
        ), patch.object(
            StreamProxy, "_probe_dv_profile", return_value=8
        ):
            sp.prepare_stream("http://host/dv-p8-remux.mkv")
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    ctx = sp._server.stream_context
    assert ctx.get("mode") != "hls"
    assert ctx["content_type"] == "video/x-matroska"


def test_prepare_stream_dv_probe_none_stays_on_fmp4():
    """A None return from the DV probe (either 'no DV' or 'probe failed')
    is treated as safe — fmp4 is allowed. Only a *confirmed* profile 7
    triggers the matroska fallback."""
    import sys

    from resources.lib.stream_proxy import StreamProxy

    sp, _, original, duration_proc, huge = _make_fmp4_prepare_fixture()
    try:
        with patch(
            "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
        ), patch(
            "resources.lib.stream_proxy._find_ffprobe", return_value=None
        ), patch.object(
            sp, "_get_content_length", return_value=huge
        ), patch(
            "resources.lib.stream_proxy.subprocess.Popen", return_value=duration_proc
        ), patch.object(
            StreamProxy, "_probe_dv_profile", return_value=None
        ), patch(
            "resources.lib.stream_proxy.HlsProducer"
        ) as mock_producer_cls:
            mock_producer_cls.return_value = MagicMock()
            sp.prepare_stream("http://host/sdr-remux.mkv")
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    ctx = sp._server.stream_context
    assert ctx["mode"] == "hls"
    assert ctx["hls_segment_format"] == "fmp4"


# ---------------------------------------------------------------------------
# HlsProducer._build_cmd — fmp4 must emit -tag:v hvc1 for DV compatibility
# ---------------------------------------------------------------------------


def test_hls_producer_fmp4_cmd_emits_hvc1_tag():
    """The fmp4 branch of _build_cmd must pass -tag:v hvc1 to ffmpeg.
    HLS fmp4 spec requires the hvc1 sample entry for HEVC, and Amlogic's
    HLS demuxer uses this tag to locate the dvcC/dvvC DV configuration
    record in the init segment."""
    from resources.lib.stream_proxy import HlsProducer

    producer = HlsProducer.__new__(HlsProducer)
    producer.ffmpeg_path = "/usr/bin/ffmpeg"
    producer.remote_url = "http://host/movie.mkv"
    producer.auth_header = None
    producer.segment_format = "fmp4"
    producer.segment_seconds = 30.0
    producer.session_dir = "/tmp/nzbdav-hls/abc123"

    cmd = producer._build_cmd(start_time=0.0, start_segment=0)
    assert "-tag:v" in cmd
    tag_idx = cmd.index("-tag:v")
    assert cmd[tag_idx + 1] == "hvc1"


def test_hls_producer_mpegts_cmd_omits_hvc1_tag():
    """The mpegts branch does NOT pass -tag:v hvc1. The tag only makes
    sense for fmp4; mpegts carries HEVC as raw NAL units and has no
    sample entry."""
    from resources.lib.stream_proxy import HlsProducer

    producer = HlsProducer.__new__(HlsProducer)
    producer.ffmpeg_path = "/usr/bin/ffmpeg"
    producer.remote_url = "http://host/movie.mkv"
    producer.auth_header = None
    producer.segment_format = "mpegts"
    producer.segment_seconds = 30.0
    producer.session_dir = "/tmp/nzbdav-hls/abc123"

    cmd = producer._build_cmd(start_time=0.0, start_segment=0)
    assert "-tag:v" not in cmd


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
    ), patch(
        "resources.lib.stream_proxy._find_ffprobe", return_value=None
    ), patch.object(
        sp, "_get_content_length", return_value=5000000000
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ), patch(
        "resources.lib.stream_proxy.fetch_remote_mp4_layout", return_value=None
    ), patch.object(
        sp, "_prepare_tempfile_faststart", return_value=None
    ):
        sp.prepare_stream("http://host/film.mp4", auth_header=auth)

    ctx = sp._server.stream_context
    assert ctx["remux"] is True
    assert ctx["duration_seconds"] == 7200.0
    assert ctx["total_bytes"] == 5000000000
    assert ctx["seekable"] is True


def test_probe_duration_prefers_ffprobe_when_available():
    """When ffprobe is on the system, _probe_duration must use it instead of
    parsing ffmpeg stderr — ffmpeg's per-stream warnings can push Duration
    past any reasonable stderr budget on files with many subtitle streams."""
    from resources.lib.stream_proxy import StreamProxy

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"8552.576000\n", b"")

    with patch(
        "resources.lib.stream_proxy._find_ffprobe",
        return_value="/usr/bin/ffprobe",
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        duration = StreamProxy._probe_duration(
            "/usr/bin/ffmpeg",
            "http://host/shawshank.mkv",
            None,
        )

    assert duration == 8552.576
    # ffprobe must have been invoked — check the argv passed to Popen.
    assert mock_popen.called
    argv = mock_popen.call_args[0][0]
    assert argv[0] == "/usr/bin/ffprobe"
    assert "format=duration" in argv


def test_probe_duration_ffprobe_uses_headers_for_auth():
    import base64

    from resources.lib.stream_proxy import StreamProxy

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"8552.576000\n", b"")
    auth = "Basic " + base64.b64encode(b"user:pass").decode()

    with patch(
        "resources.lib.stream_proxy._find_ffprobe",
        return_value="/usr/bin/ffprobe",
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        duration = StreamProxy._probe_duration(
            "/usr/bin/ffmpeg",
            "http://host/shawshank.mkv",
            auth,
        )

    assert duration == 8552.576
    argv = mock_popen.call_args[0][0]
    headers_idx = argv.index("-headers")
    assert argv[headers_idx + 1] == "Authorization: {}\r\n".format(auth)
    assert argv[-1] == "http://host/shawshank.mkv"
    assert all("@host" not in part for part in argv)


def test_probe_duration_ffprobe_returns_none_on_nonzero_exit():
    """A failing ffprobe (bad URL, auth failure, corrupt header) must return
    None so the caller can fall back to the ffmpeg-stderr path."""
    from resources.lib.stream_proxy import StreamProxy

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.communicate.return_value = (b"", b"error\n")

    with patch(
        "resources.lib.stream_proxy._find_ffprobe",
        return_value="/usr/bin/ffprobe",
    ), patch("resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc):
        result = StreamProxy._probe_duration_ffprobe(
            "/usr/bin/ffprobe", "http://host/bad.mkv"
        )

    assert result is None


def test_probe_duration_ffmpeg_fallback_budget_handles_subtitle_wall():
    """Regression test for the Shawshank 30+ subtitle stream bug.

    When ffprobe isn't available, the ffmpeg-stderr fallback must have a
    budget large enough to read through ~30 `Could not find codec parameters
    for stream N (Subtitle: hdmv_pgs_subtitle)` lines before the `Duration:`
    line shows up. The original 8 KB budget truncated well before the
    header finished printing on these files.
    """
    from resources.lib.stream_proxy import StreamProxy

    # Build a realistic ffmpeg stderr stream: banner + 60 subtitle warnings
    # + the Duration line. Each subtitle warning is ~221 bytes (matches the
    # Shawshank probe output seen live), so 60 × 221 ≈ 13 KB — well above
    # the original 8 KB budget, forcing the larger budget path to be taken.
    banner = (
        b"ffmpeg version 6.0.1 Copyright (c) 2000-2023 the FFmpeg developers\n"
        b"  libavutil      58.  2.100 / 58.  2.100\n"
        b"  libavcodec     60.  3.100 / 60.  3.100\n"
    )
    subtitle_warning = (
        b"[matroska,webm @ 0x4a55220] Could not find codec parameters for stream "
        b"N (Subtitle: hdmv_pgs_subtitle (pgssub)): unspecified size\n"
        b"Consider increasing the value for the 'analyzeduration' (0) and "
        b"'probesize' (5000000) options\n"
    )
    warnings_wall = subtitle_warning * 60
    duration_line = b"  Duration: 02:22:32.58, start: 0.000000\n"
    stderr_stream = banner + warnings_wall + duration_line
    total_pre_duration = len(banner) + len(warnings_wall)
    assert (
        total_pre_duration > 8192
    ), "test setup error: pre-Duration bytes must exceed the old 8 KB budget"

    # Feed it to the fallback as a line-by-line iterator.
    mock_proc = MagicMock()
    mock_proc.stderr = iter(stderr_stream.splitlines(keepends=True))

    with patch("resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc):
        duration = StreamProxy._probe_duration_ffmpeg(
            "/usr/bin/ffmpeg", "http://host/shawshank.mkv"
        )

    assert duration == 2 * 3600 + 22 * 60 + 32.58


def test_probe_duration_ffmpeg_fallback_uses_headers_for_auth():
    import base64

    from resources.lib.stream_proxy import StreamProxy

    mock_proc = MagicMock()
    mock_proc.stderr = iter([b"  Duration: 00:10:00.00, start: 0.000000\n"])
    auth = "Basic " + base64.b64encode(b"user:pass").decode()

    with patch(
        "resources.lib.stream_proxy._find_ffprobe",
        return_value=None,
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        duration = StreamProxy._probe_duration(
            "/usr/bin/ffmpeg",
            "http://host/shawshank.mkv",
            auth,
        )

    assert duration == 600.0
    argv = mock_popen.call_args[0][0]
    headers_idx = argv.index("-headers")
    i_idx = argv.index("-i")
    assert headers_idx < i_idx
    assert argv[headers_idx + 1] == "Authorization: {}\r\n".format(auth)
    assert argv[i_idx + 1] == "http://host/shawshank.mkv"
    assert all("@host" not in part for part in argv)


def test_prepare_tempfile_faststart_uses_headers_for_auth():
    import base64
    import os

    from resources.lib.stream_proxy import StreamProxy

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"")
    mock_proc.returncode = 0
    auth = "Basic " + base64.b64encode(b"user:pass").decode()

    with patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        temp_path = StreamProxy._prepare_tempfile_faststart(
            "/usr/bin/ffmpeg",
            "http://host/film.mp4",
            auth,
        )

    try:
        argv = mock_popen.call_args[0][0]
        headers_idx = argv.index("-headers")
        i_idx = argv.index("-i")
        assert headers_idx < i_idx
        assert argv[headers_idx + 1] == "Authorization: {}\r\n".format(auth)
        assert argv[i_idx + 1] == "http://host/film.mp4"
        assert all("@host" not in part for part in argv)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


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
    ), patch(
        "resources.lib.stream_proxy._find_ffprobe", return_value=None
    ), patch.object(
        sp, "_get_content_length", return_value=5000000000
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ), patch(
        "resources.lib.stream_proxy.fetch_remote_mp4_layout", return_value=None
    ), patch.object(
        sp, "_prepare_tempfile_faststart", return_value=None
    ):
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


def test_build_ffmpeg_cmd_copies_subs_for_mkv_input():
    """For MKV inputs the subtitle codec must be `copy`, not `srt`.
    PGS/DVD/HDMV bitmap subs can't be re-encoded to SRT and would abort
    the entire remux; `copy` handles every subtitle codec losslessly."""
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
    }
    cmd = handler._build_ffmpeg_cmd(ctx)
    assert "0:s?" in cmd
    idx = cmd.index("-c:s")
    assert cmd[idx + 1] == "copy"
    assert "srt" not in cmd


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


def test_build_ffmpeg_cmd_mpegts_output_format():
    """output_format='mpegts' emits MPEG-TS with subs dropped.

    Regression test for the Shawshank seek bug. Piped MKV has no Cues;
    MPEG-TS is the format we switched to so Kodi can do real seeks via
    byte-range restart of ffmpeg with `-ss`.
    """
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "output_format": "mpegts",
    }
    cmd = handler._build_ffmpeg_cmd(ctx)
    # MPEG-TS format selector present, matroska absent.
    f_idx = cmd.index("-f")
    assert cmd[f_idx + 1] == "mpegts"
    assert "matroska" not in cmd
    # Subtitles explicitly dropped — MPEG-TS can't carry PGS/HDMV, and
    # ffmpeg can't transcode those, so `-sn` is the only safe choice.
    assert "-sn" in cmd
    # No subtitle mapping or -c:s for the TS path.
    assert "0:s?" not in cmd
    assert "-c:s" not in cmd
    # No -metadata DURATION — MPEG-TS has no container-level duration
    # field for ffmpeg to write, so don't bother.
    assert "-metadata" not in cmd


def test_build_ffmpeg_cmd_mpegts_includes_seek():
    """Seek flag must apply to the MPEG-TS path too."""
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "output_format": "mpegts",
        "duration_seconds": 8552.576,
    }
    cmd = handler._build_ffmpeg_cmd(ctx, seek_seconds=4276.288)
    ss_idx = cmd.index("-ss")
    i_idx = cmd.index("-i")
    assert ss_idx < i_idx
    assert cmd[ss_idx + 1] == "4276.288"


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


def test_build_ffmpeg_cmd_passes_basic_auth_via_headers():
    """Basic auth header must be passed via ffmpeg -headers, not URL userinfo."""
    import base64

    handler = _make_handler()
    auth = "Basic " + base64.b64encode(b"user:pass").decode()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": auth,
    }
    cmd = handler._build_ffmpeg_cmd(ctx)
    headers_idx = cmd.index("-headers")
    i_idx = cmd.index("-i")
    assert headers_idx < i_idx
    assert cmd[headers_idx + 1] == "Authorization: {}\r\n".format(auth)
    assert cmd[i_idx + 1] == "http://host/film.mp4"
    assert all("@host" not in part for part in cmd)


def test_build_ffmpeg_cmd_keeps_url_clean_with_reserved_char_credentials():
    import base64

    handler = _make_handler()
    auth = "Basic " + base64.b64encode(b"user@domain:pa/ss?#word").decode()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": auth,
    }
    cmd = handler._build_ffmpeg_cmd(ctx)
    headers_idx = cmd.index("-headers")
    i_idx = cmd.index("-i")
    assert cmd[headers_idx + 1] == "Authorization: {}\r\n".format(auth)
    assert cmd[i_idx + 1] == "http://host/film.mp4"
    assert all("@host" not in part for part in cmd)


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


def test_serve_remux_write_timeout_exits_loop():
    """If wfile.write raises socket.timeout (Kodi stopped consuming without
    closing the TCP connection) the loop must break and the finally block
    must kill ffmpeg. Otherwise a DB-vacuum-style stall leaves a zombie
    ffmpeg writing into a dead socket forever."""
    import socket

    ctx = {
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 15 * 1024 * 1024 * 1024,
        "duration_seconds": 3600.0,
        "seekable": True,
        "remux": True,
    }

    handler = _make_handler_with_server(ctx)
    # First write returns normally, second raises — simulates the socket
    # send buffer filling up and the timeout firing on the second chunk.
    handler.wfile.write.side_effect = [None, socket.timeout("timed out")]

    mock_proc = MagicMock()
    mock_proc.stdout.read.side_effect = [b"chunk1", b"chunk2", b""]
    mock_proc.stderr.read.return_value = b""

    with patch("resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc):
        handler._serve_remux(ctx)

    # ffmpeg MUST be killed on timeout — otherwise it leaks
    mock_proc.kill.assert_called()
    mock_proc.wait.assert_called()


def test_serve_remux_sets_socket_write_timeout():
    """The remux handler must set a socket write timeout on the connection
    before streaming, so a blocked write from a half-dead client can't hang
    the handler thread indefinitely."""
    from resources.lib.stream_proxy import _REMUX_WRITE_TIMEOUT

    ctx = {
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 15 * 1024 * 1024 * 1024,
        "duration_seconds": 3600.0,
        "seekable": True,
        "remux": True,
    }

    handler = _make_handler_with_server(ctx)
    handler.connection = MagicMock()

    mock_proc = MagicMock()
    mock_proc.stdout.read.return_value = b""
    mock_proc.stderr.read.return_value = b""

    with patch("resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc):
        handler._serve_remux(ctx)

    handler.connection.settimeout.assert_called_once_with(_REMUX_WRITE_TIMEOUT)


def test_prepare_stream_clears_previous_sessions():
    """A second prepare_stream call must tear down ffmpeg processes from
    any prior session before registering the new one. Prevents zombie
    remux ffmpegs from surviving across Kodi plays when the player stalls
    without firing onPlayBackStopped."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    # First play — set up a session with a fake-running ffmpeg attached.
    with patch.object(sp, "_get_content_length", return_value=100000):
        sp.prepare_stream("http://host/one.mkv")
    old_session = next(iter(sp._server.stream_sessions.values()))
    old_proc = MagicMock()
    old_session["active_ffmpeg"] = old_proc

    # Second play — the old ffmpeg must be killed, the old session dropped.
    with patch.object(sp, "_get_content_length", return_value=200000):
        sp.prepare_stream("http://host/two.mkv")

    old_proc.kill.assert_called_once()
    old_proc.wait.assert_called_once()
    assert len(sp._server.stream_sessions) == 1


def test_clear_sessions_kills_all_ffmpegs():
    """StreamProxy.clear_sessions must kill every registered ffmpeg."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()

    proc_a, proc_b = MagicMock(), MagicMock()
    sp._server.stream_sessions = {
        "a": {"active_ffmpeg": proc_a},
        "b": {"active_ffmpeg": proc_b},
    }

    sp.clear_sessions()

    proc_a.kill.assert_called_once()
    proc_b.kill.assert_called_once()
    assert sp._server.stream_sessions == {}


def test_prune_sessions_requires_context_lock_ownership():
    """Debug guard: the locked helper must raise when called without the
    proxy context lock held by the current thread."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = threading.RLock()

    with pytest.raises(AssertionError):
        sp._prune_sessions_locked()


# ---------------------------------------------------------------------------
# HLS playlist / segment handlers
# ---------------------------------------------------------------------------


def _make_hls_handler(ctx, request_path):
    """Construct a _StreamHandler for HLS path dispatch tests.

    Delegates to ``_make_handler_with_server`` for the common mock
    scaffolding (server mock, stream_context, ffmpeg_lock, response
    helpers) and layers on the HLS-specific pieces: ``stream_sessions``
    keyed by session id, ``handler.path``, and the ``connection`` mock
    HLS needs for keep-alive logic.
    """
    handler = _make_handler_with_server(ctx)
    session_id = ctx.get("session_id", "abc123")
    handler.server.stream_sessions = {session_id: ctx}
    handler.path = request_path
    handler.connection = MagicMock()
    return handler


def test_parse_hls_resource_playlist():
    from resources.lib.stream_proxy import _StreamHandler

    assert _StreamHandler._parse_hls_resource("/hls/abc/playlist.m3u8") == (
        "abc",
        "playlist",
    )


def test_parse_hls_resource_segment():
    """Regression (updated shape): a segment URL with the legacy
    .ts extension parses to ('segment', N, 'ts')."""
    from resources.lib.stream_proxy import _StreamHandler

    result = _StreamHandler._parse_hls_resource("/hls/abc/seg_5.ts")
    assert result == ("abc", ("segment", 5, "ts"))


def test_parse_hls_resource_init_mp4_returns_init():
    """/hls/<session>/init.mp4 parses to (session_id, 'init')."""
    from resources.lib.stream_proxy import _StreamHandler

    result = _StreamHandler._parse_hls_resource("/hls/abc123/init.mp4")
    assert result == ("abc123", "init")


def test_parse_hls_resource_segment_m4s_returns_extension():
    """/hls/<s>/seg_5.m4s parses to (session_id, ('segment', 5, 'm4s'))."""
    from resources.lib.stream_proxy import _StreamHandler

    result = _StreamHandler._parse_hls_resource("/hls/abc123/seg_5.m4s")
    assert result == ("abc123", ("segment", 5, "m4s"))


def test_parse_hls_resource_segment_ts_returns_extension():
    """/hls/<s>/seg_5.ts parses to (session_id, ('segment', 5, 'ts'))."""
    from resources.lib.stream_proxy import _StreamHandler

    result = _StreamHandler._parse_hls_resource("/hls/abc123/seg_5.ts")
    assert result == ("abc123", ("segment", 5, "ts"))


def test_parse_hls_resource_segment_padded_index_still_parses():
    """Zero-padded segment indices still parse to the bare int plus
    the extension — regression guard for the URL→int→disk path
    lookup."""
    from resources.lib.stream_proxy import _StreamHandler

    result = _StreamHandler._parse_hls_resource("/hls/abc/seg_000005.ts")
    assert result == ("abc", ("segment", 5, "ts"))


def test_parse_hls_resource_rejects_wrong_init_filename():
    """Anything other than exactly 'init.mp4' (e.g. 'not-init.mp4')
    returns None."""
    from resources.lib.stream_proxy import _StreamHandler

    assert _StreamHandler._parse_hls_resource("/hls/abc/not-init.mp4") is None
    assert _StreamHandler._parse_hls_resource("/hls/abc/init.ts") is None


def test_parse_hls_resource_rejects_unknown_segment_extension():
    """Unknown extensions on seg_ URIs return None."""
    from resources.lib.stream_proxy import _StreamHandler

    assert _StreamHandler._parse_hls_resource("/hls/abc/seg_5.mov") is None
    assert _StreamHandler._parse_hls_resource("/hls/abc/seg_5.mp4") is None


def test_parse_hls_resource_rejects_negative_segment():
    from resources.lib.stream_proxy import _StreamHandler

    assert _StreamHandler._parse_hls_resource("/hls/abc/seg_-1.ts") is None


def test_parse_hls_resource_rejects_malformed():
    from resources.lib.stream_proxy import _StreamHandler

    assert _StreamHandler._parse_hls_resource("/hls/") is None
    assert _StreamHandler._parse_hls_resource("/hls/abc") is None
    assert _StreamHandler._parse_hls_resource("/hls/abc/unknown.txt") is None
    assert _StreamHandler._parse_hls_resource("/hls/abc/seg_abc.ts") is None
    assert _StreamHandler._parse_hls_resource("/stream/abc") is None


def _make_hls_ctx_fmp4():
    """Construct a minimal fmp4 HLS ctx with a MagicMock producer."""
    return {
        "mode": "hls",
        "hls_segment_format": "fmp4",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_producer": MagicMock(),
    }


def _make_hls_ctx_mpegts():
    """Construct a minimal mpegts HLS ctx with a MagicMock producer."""
    return {
        "mode": "hls",
        "hls_segment_format": "mpegts",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_producer": MagicMock(),
    }


def _make_handler_for(path, ctx):
    """Build a minimal _StreamHandler with an injected path and ctx.

    Wires ``handler.server.stream_sessions`` so that
    ``_get_stream_context`` resolves the ``/hls/<session_id>/...``
    path back to ``ctx``. Assumes ``path`` is of the form
    ``/hls/<session_id>/<resource>``.
    """
    from resources.lib.stream_proxy import _StreamHandler

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.path = path
    handler.server = MagicMock()
    handler.server.stream_context = ctx
    # Extract session id from /hls/<session>/... so _get_stream_context
    # finds ctx in stream_sessions.
    parts = path[len("/hls/") :].split("/", 1)
    session_id = parts[0] if parts else "abc"
    handler.server.stream_sessions = {session_id: ctx}
    handler.headers = MagicMock()
    handler.headers.get.return_value = None
    handler.send_response = MagicMock()
    handler.send_error = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    return handler


def test_head_hls_init_returns_video_mp4():
    """HEAD /hls/<s>/init.mp4 against an fmp4 ctx returns 200 +
    Content-Type: video/mp4."""
    handler = _make_handler_for("/hls/abc/init.mp4", _make_hls_ctx_fmp4())
    handler.do_HEAD()
    handler.send_response.assert_called_with(200)
    ct_calls = [
        call
        for call in handler.send_header.call_args_list
        if call.args[0] == "Content-Type"
    ]
    assert ct_calls
    assert ct_calls[0].args[1] == "video/mp4"


def test_head_hls_init_on_mpegts_ctx_returns_404():
    """HEAD /hls/<s>/init.mp4 against an mpegts ctx is 404 (init is
    only valid for fmp4 sessions)."""
    handler = _make_handler_for("/hls/abc/init.mp4", _make_hls_ctx_mpegts())
    handler.do_HEAD()
    handler.send_error.assert_called_with(404)


def test_head_hls_segment_fmp4_returns_video_mp4():
    """HEAD /hls/<s>/seg_0.m4s against an fmp4 ctx returns video/mp4."""
    handler = _make_handler_for("/hls/abc/seg_0.m4s", _make_hls_ctx_fmp4())
    handler.do_HEAD()
    handler.send_response.assert_called_with(200)
    ct_calls = [
        call
        for call in handler.send_header.call_args_list
        if call.args[0] == "Content-Type"
    ]
    assert ct_calls[0].args[1] == "video/mp4"


def test_head_hls_segment_mpegts_returns_video_mp2t():
    """Regression: HEAD /hls/<s>/seg_0.ts on an mpegts ctx returns
    video/mp2t."""
    handler = _make_handler_for("/hls/abc/seg_0.ts", _make_hls_ctx_mpegts())
    handler.do_HEAD()
    handler.send_response.assert_called_with(200)
    ct_calls = [
        call
        for call in handler.send_header.call_args_list
        if call.args[0] == "Content-Type"
    ]
    assert ct_calls[0].args[1] == "video/mp2t"


def test_head_hls_ts_segment_on_fmp4_ctx_returns_404():
    """Strict rejection: .ts URL on fmp4 session is 404."""
    handler = _make_handler_for("/hls/abc/seg_0.ts", _make_hls_ctx_fmp4())
    handler.do_HEAD()
    handler.send_error.assert_called_with(404)


def test_head_hls_m4s_segment_on_mpegts_ctx_returns_404():
    """Strict rejection: .m4s URL on mpegts session is 404."""
    handler = _make_handler_for("/hls/abc/seg_0.m4s", _make_hls_ctx_mpegts())
    handler.do_HEAD()
    handler.send_error.assert_called_with(404)


def test_do_get_hls_init_on_mpegts_ctx_returns_404():
    """GET /hls/<s>/init.mp4 on mpegts ctx is 404."""
    handler = _make_handler_for("/hls/abc/init.mp4", _make_hls_ctx_mpegts())
    handler.do_GET()
    handler.send_error.assert_called_with(404)


def test_do_get_hls_ts_segment_on_fmp4_ctx_returns_404():
    """GET /hls/<s>/seg_0.ts on fmp4 ctx is 404."""
    handler = _make_handler_for("/hls/abc/seg_0.ts", _make_hls_ctx_fmp4())
    # Patch out serve methods so a stray dispatch would be visible
    handler._serve_hls_playlist = MagicMock()
    handler._serve_hls_segment = MagicMock()
    handler.do_GET()
    handler.send_error.assert_called_with(404)
    handler._serve_hls_segment.assert_not_called()


def test_do_get_hls_m4s_segment_on_mpegts_ctx_returns_404():
    """GET /hls/<s>/seg_0.m4s on mpegts ctx is 404."""
    handler = _make_handler_for("/hls/abc/seg_0.m4s", _make_hls_ctx_mpegts())
    handler._serve_hls_segment = MagicMock()
    handler.do_GET()
    handler.send_error.assert_called_with(404)
    handler._serve_hls_segment.assert_not_called()


def test_do_get_hls_routes_init_to_serve_hls_init():
    """GET /hls/<s>/init.mp4 on fmp4 ctx dispatches to
    _serve_hls_init (added in Task 11)."""
    handler = _make_handler_for("/hls/abc/init.mp4", _make_hls_ctx_fmp4())
    handler._serve_hls_init = MagicMock()
    handler.do_GET()
    handler._serve_hls_init.assert_called_once()


def test_serve_hls_playlist_shape():
    """Playlist must be a valid HLS VOD m3u8 with one segment per
    ``#EXTINF`` block. The segment durations must sum to the total
    source duration (modulo floating point slop) so Kodi's seek bar
    shows the correct total time.
    """
    ctx = {
        "session_id": "sess1",
        "mode": "hls",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 8552.576,
        "hls_segment_duration": 10.0,
        "total_bytes": 58339952712,
    }

    handler = _make_hls_handler(ctx, "/hls/sess1/playlist.m3u8")
    handler._serve_hls_playlist(ctx)

    handler.send_response.assert_called_once_with(200)
    header_calls = {
        call.args[0]: call.args[1] for call in handler.send_header.call_args_list
    }
    assert header_calls["Content-Type"] == "application/vnd.apple.mpegurl"
    # The playlist body was written in a single write call.
    assert handler.wfile.write.called
    body = handler.wfile.write.call_args[0][0].decode("utf-8")
    assert body.startswith("#EXTM3U\n")
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in body
    assert "#EXT-X-ENDLIST" in body

    # 8552.576 / 10.0 = 856 segments (ceil). Verify the count.
    extinf_lines = [line for line in body.splitlines() if line.startswith("#EXTINF:")]
    assert len(extinf_lines) == 856, "expected ceil(duration/10) segments"

    # Segment URIs should be relative and sequential.
    seg_uris = [
        line
        for line in body.splitlines()
        if line.startswith("seg_") and line.endswith(".ts")
    ]
    assert len(seg_uris) == 856
    assert seg_uris[0] == "seg_0.ts"
    assert seg_uris[-1] == "seg_855.ts"

    # Sum of EXTINF values ≈ duration (allowing floating-point slop).
    durations = [float(line[len("#EXTINF:") : -1]) for line in extinf_lines]
    assert abs(sum(durations) - 8552.576) < 0.001


def test_serve_hls_playlist_fmp4_version_is_7():
    """fmp4 ctx emits #EXT-X-VERSION:7."""
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {
        "hls_segment_format": "fmp4",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    captured = []
    handler.wfile.write.side_effect = lambda b: captured.append(b)

    handler._serve_hls_playlist(ctx)

    body = b"".join(captured).decode("utf-8")
    assert "#EXT-X-VERSION:7" in body


def test_serve_hls_playlist_fmp4_contains_ext_x_map():
    """fmp4 ctx emits #EXT-X-MAP:URI='init.mp4'."""
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {
        "hls_segment_format": "fmp4",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    captured = []
    handler.wfile.write.side_effect = lambda b: captured.append(b)

    handler._serve_hls_playlist(ctx)
    body = b"".join(captured).decode("utf-8")
    assert '#EXT-X-MAP:URI="init.mp4"' in body


def test_serve_hls_playlist_fmp4_uses_m4s_extension():
    """fmp4 ctx segment URIs end in .m4s."""
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {
        "hls_segment_format": "fmp4",
        "duration_seconds": 60.0,
        "hls_segment_duration": 30.0,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    captured = []
    handler.wfile.write.side_effect = lambda b: captured.append(b)

    handler._serve_hls_playlist(ctx)
    body = b"".join(captured).decode("utf-8")
    assert "seg_0.m4s" in body
    assert "seg_1.m4s" in body
    assert ".ts" not in body


def test_serve_hls_playlist_mpegts_version_is_still_3():
    """mpegts ctx still emits #EXT-X-VERSION:3 (no changes)."""
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {
        "hls_segment_format": "mpegts",
        "duration_seconds": 60.0,
        "hls_segment_duration": 30.0,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    captured = []
    handler.wfile.write.side_effect = lambda b: captured.append(b)

    handler._serve_hls_playlist(ctx)
    body = b"".join(captured).decode("utf-8")
    assert "#EXT-X-VERSION:3" in body
    assert "#EXT-X-VERSION:7" not in body


def test_serve_hls_playlist_mpegts_no_ext_x_map():
    """mpegts ctx must NOT emit #EXT-X-MAP."""
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {
        "hls_segment_format": "mpegts",
        "duration_seconds": 60.0,
        "hls_segment_duration": 30.0,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    captured = []
    handler.wfile.write.side_effect = lambda b: captured.append(b)

    handler._serve_hls_playlist(ctx)
    body = b"".join(captured).decode("utf-8")
    assert "#EXT-X-MAP" not in body


def test_serve_hls_playlist_mpegts_uses_ts_extension():
    """Regression: mpegts segment URIs still end in .ts."""
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {
        "hls_segment_format": "mpegts",
        "duration_seconds": 60.0,
        "hls_segment_duration": 30.0,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    captured = []
    handler.wfile.write.side_effect = lambda b: captured.append(b)

    handler._serve_hls_playlist(ctx)
    body = b"".join(captured).decode("utf-8")
    assert "seg_0.ts" in body
    assert ".m4s" not in body


def test_serve_hls_segment_reads_from_producer_file(tmp_path):
    """The segment handler reads ``hls_producer.wait_for_segment``'s
    returned file path and streams it back with ``Content-Length``,
    not chunked. The producer owns ffmpeg; the handler is just a file
    server for already-produced .ts files.
    """
    seg_file = tmp_path / "seg_000100.ts"
    seg_file.write_bytes(b"TSDATA" * 1000)  # 6000 bytes of dummy payload

    producer = MagicMock()
    producer.wait_for_segment.return_value = str(seg_file)

    ctx = {
        "session_id": "sess1",
        "mode": "hls",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 8552.576,
        "hls_segment_duration": 10.0,
        "total_bytes": 58339952712,
        "hls_producer": producer,
    }

    handler = _make_hls_handler(ctx, "/hls/sess1/seg_100.ts")
    handler._serve_hls_segment(ctx, 100)

    producer.wait_for_segment.assert_called_once_with(100)
    handler.send_response.assert_called_once_with(200)
    header_calls = {
        call.args[0]: call.args[1] for call in handler.send_header.call_args_list
    }
    assert header_calls["Content-Type"] == "video/mp2t"
    assert header_calls["Content-Length"] == "6000"
    # All the bytes must have been written to wfile.
    written = b"".join(call.args[0] for call in handler.wfile.write.call_args_list)
    assert written == b"TSDATA" * 1000


def test_serve_hls_segment_504_on_producer_timeout():
    """If ``wait_for_segment`` returns None (timeout) the handler
    responds 504 Gateway Timeout instead of hanging indefinitely."""
    producer = MagicMock()
    producer.wait_for_segment.return_value = None

    ctx = {
        "session_id": "sess1",
        "mode": "hls",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 8552.576,
        "hls_segment_duration": 10.0,
        "total_bytes": 58339952712,
        "hls_producer": producer,
    }
    handler = _make_hls_handler(ctx, "/hls/sess1/seg_100.ts")
    handler._serve_hls_segment(ctx, 100)

    handler.send_error.assert_called_once_with(504)


def test_serve_hls_init_serves_canonical_cached_bytes(tmp_path):
    """_serve_hls_init serves the producer's canonical init bytes
    cache — NOT the bytes currently on disk. On a seek respawn ffmpeg
    rewrites init.mp4 with a different edit list; the canonical cache
    guarantees every Kodi fetch returns the first generation's init
    so the cached init stays compatible with later segments."""
    import os as _os

    from resources.lib.stream_proxy import _StreamHandler

    init_path = _os.path.join(str(tmp_path), "init.mp4")
    # Write STALE bytes to disk to prove the handler doesn't read them.
    with open(init_path, "wb") as f:
        f.write(b"STALE_DISK_BYTES")

    producer = MagicMock()
    producer.wait_for_init.return_value = init_path
    producer._canonical_init_bytes = b"CANONICAL"
    ctx = {
        "hls_segment_format": "fmp4",
        "hls_producer": producer,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    handler._serve_hls_init(ctx)

    handler.send_response.assert_called_with(200)
    ct_calls = [
        call
        for call in handler.send_header.call_args_list
        if call.args[0] == "Content-Type"
    ]
    assert ct_calls[0].args[1] == "video/mp4"
    cl_calls = [
        call
        for call in handler.send_header.call_args_list
        if call.args[0] == "Content-Length"
    ]
    assert cl_calls[0].args[1] == str(len(b"CANONICAL"))
    handler.wfile.write.assert_called_with(b"CANONICAL")


def test_serve_hls_init_falls_back_to_disk_when_cache_missing(tmp_path):
    """If the canonical cache hasn't been populated yet (very early
    fetch before wait_for_init has actually observed a complete init),
    the handler falls back to reading the on-disk init file. This is
    a defensive path — in practice wait_for_init populates the cache
    before returning a path, so the handler should always hit the
    cache. Regression guard for the legacy behavior just in case."""
    import os as _os

    from resources.lib.stream_proxy import _StreamHandler

    init_path = _os.path.join(str(tmp_path), "init.mp4")
    with open(init_path, "wb") as f:
        f.write(b"DISK_BYTES")

    producer = MagicMock()
    producer.wait_for_init.return_value = init_path
    producer._canonical_init_bytes = None  # cache empty
    ctx = {
        "hls_segment_format": "fmp4",
        "hls_producer": producer,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    handler._serve_hls_init(ctx)

    handler.send_response.assert_called_with(200)
    handler.wfile.write.assert_called_with(b"DISK_BYTES")


def test_serve_hls_init_504_on_producer_timeout():
    """If wait_for_init returns None (timeout), the handler sends 504."""
    from resources.lib.stream_proxy import _StreamHandler

    producer = MagicMock()
    producer.wait_for_init.return_value = None
    ctx = {
        "hls_segment_format": "fmp4",
        "hls_producer": producer,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    handler._serve_hls_init(ctx)
    handler.send_error.assert_called_with(504)


def test_serve_hls_init_500_when_producer_missing():
    """If ctx has no hls_producer, handler sends 500."""
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {"hls_segment_format": "fmp4"}  # no producer

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    handler._serve_hls_init(ctx)
    handler.send_error.assert_called_with(500)


def test_serve_hls_segment_fmp4_ctx_uses_video_mp4_content_type(tmp_path):
    """Regression guard: when ctx is fmp4, _serve_hls_segment must
    set Content-Type: video/mp4 (NOT the legacy mpegts video/mp2t).
    Without this, HEAD and GET would disagree on Content-Type for
    fmp4 segments — flagged by the code reviewer of Tasks 9+10."""
    import os as _os

    from resources.lib.stream_proxy import _StreamHandler

    seg_path = _os.path.join(str(tmp_path), "seg_000000.m4s")
    with open(seg_path, "wb") as f:
        f.write(b"FAKESEG")

    producer = MagicMock()
    producer.wait_for_segment.return_value = seg_path
    producer.segment_path.return_value = seg_path
    ctx = {
        "hls_segment_format": "fmp4",
        "hls_producer": producer,
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
    }

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = MagicMock()
    handler.send_error = MagicMock()

    handler._serve_hls_segment(ctx, 0)

    ct_calls = [
        call
        for call in handler.send_header.call_args_list
        if call.args[0] == "Content-Type"
    ]
    assert ct_calls, "Content-Type header was not set"
    assert ct_calls[0].args[1] == "video/mp4"


def test_build_hls_segment_cmd_includes_cold_start_flags():
    """The HLS segment ffmpeg command must carry the three input-side
    flags that keep cold start fast on large remote MKVs with many
    subtitle streams: ``-probesize``, ``-analyzeduration``, and
    ``-fflags +fastseek``. All three MUST appear before ``-i`` since
    they are input options.

    ``-probesize`` must be large enough to enumerate every subtitle
    track — 32 KB was too small on files with 32 sub tracks, which
    made Kodi's subtitle menu empty.
    """
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
    }
    cmd = _StreamHandler._build_hls_segment_cmd(ctx, 100.0, 10.0)

    i_idx = cmd.index("-i")

    ps_idx = cmd.index("-probesize")
    assert ps_idx < i_idx
    # 1 MB is the smallest tested value that enumerates all 32
    # subtitle tracks on the Shawshank REMUX.
    assert cmd[ps_idx + 1] == "1048576"

    ad_idx = cmd.index("-analyzeduration")
    assert ad_idx < i_idx
    assert cmd[ad_idx + 1] == "0"

    fflags_idx = cmd.index("-fflags")
    assert fflags_idx < i_idx
    assert cmd[fflags_idx + 1] == "+fastseek"


def test_build_hls_segment_cmd_drops_copyts():
    """``-copyts`` must not be on the HLS segment command.

    With ``-copyts`` set, ``-ss`` snaps to keyframes whose source PTS
    values are carried verbatim into the output. Adjacent segments
    produce overlapping source PTS ranges because keyframe snapping
    doesn't align to segment boundaries (seg 99 ends at ~999 s, seg
    100 starts at ~998 s from an earlier keyframe). The Amlogic HW
    decoder logs ``CAMLCodec::GetNextDequeuedBuffer: current pts <=
    last pts`` and replays a few frames of audio, which sounds like
    someone saying a word twice mid-dialogue. Regression test.
    """
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
    }
    cmd = _StreamHandler._build_hls_segment_cmd(ctx, 100.0, 10.0)
    assert "-copyts" not in cmd
    assert "-muxdelay" not in cmd
    assert "-muxpreload" not in cmd


def test_build_hls_segment_cmd_drops_subtitles():
    """Subtitles must be dropped with ``-sn``, not mapped.

    An earlier iteration tried ``-map 0:s? -c:s copy`` to pass PGS
    subtitles through MPEG-TS, but ffmpeg's mpegts muxer wraps PGS
    as a ``private data stream`` that Kodi's MPEG-TS demuxer
    rejects on probe (``Playback failed``). PGS codec parameter
    detection also requires a multi-minute analyze window
    incompatible with the tight probe budget we need for fast
    segment cold start. Regression test for the ``Playback failed``
    dialog on Shawshank after the subs-pass-through attempt.
    """
    from resources.lib.stream_proxy import _StreamHandler

    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
    }
    cmd = _StreamHandler._build_hls_segment_cmd(ctx, 100.0, 10.0)
    assert "-sn" in cmd
    assert "0:s?" not in cmd
    assert "-c:s" not in cmd


def test_build_hls_segment_cmd_passes_basic_auth_via_headers():
    import base64

    from resources.lib.stream_proxy import _StreamHandler

    auth = "Basic " + base64.b64encode(b"user:pass").decode()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mkv",
        "auth_header": auth,
    }
    cmd = _StreamHandler._build_hls_segment_cmd(ctx, 100.0, 10.0)
    headers_idx = cmd.index("-headers")
    i_idx = cmd.index("-i")
    assert headers_idx < i_idx
    assert cmd[headers_idx + 1] == "Authorization: {}\r\n".format(auth)
    assert cmd[i_idx + 1] == "http://host/film.mkv"
    assert all("@host" not in part for part in cmd)


def test_hls_segment_seconds_is_in_reasonable_range():
    """Segment duration must match the HlsProducer architecture.

    The ORIGINAL rationale for a 30 s minimum was that each segment
    spawned a fresh ffmpeg with a 10-15 s cold-start cost to open
    the remote huge MKV. That rationale is obsolete: HlsProducer
    now runs ONE long-lived ffmpeg per session and writes segments
    continuously, so cold-start is paid once per session (and once
    more per seek respawn), not per segment.

    The NEW constraint is on the other end: segments must be long
    enough to contain at least one IDR (so ``-hls_time`` alignment
    works), and short enough that the playlist's fixed-duration
    EXTINF approximation of the real ffmpeg output doesn't drift
    into visible seek misses or A/V desync. 6 s is the CMAF /
    Apple HLS author guide default and matches typical UHD REMUX
    GOP lengths. Anything under ~2 s is a bug; anything over
    ~30 s reintroduces the drift problem.
    """
    from resources.lib.stream_proxy import _HLS_SEGMENT_SECONDS

    assert 2.0 <= _HLS_SEGMENT_SECONDS <= 30.0


def test_serve_hls_segment_out_of_range_404s():
    """Requesting a segment past the end returns 404 — producer is
    never consulted for a segment that doesn't exist in the playlist."""
    producer = MagicMock()

    ctx = {
        "session_id": "sess1",
        "mode": "hls",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 100.0,
        "hls_segment_duration": 10.0,
        "total_bytes": 1000000,
        "hls_producer": producer,
    }

    handler = _make_hls_handler(ctx, "/hls/sess1/seg_99.ts")
    handler._serve_hls_segment(ctx, 99)

    handler.send_error.assert_called_once_with(404)
    producer.wait_for_segment.assert_not_called()


def test_do_get_routes_hls_paths():
    """do_GET must route /hls/<session>/... through _handle_hls rather
    than the default /stream/ handler."""
    ctx = {
        "session_id": "xyz789",
        "mode": "hls",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 100.0,
        "hls_segment_duration": 10.0,
        "total_bytes": 1000000,
    }

    handler = _make_hls_handler(ctx, "/hls/xyz789/playlist.m3u8")

    with patch.object(handler, "_serve_hls_playlist") as mock_serve:
        handler.do_GET()

    mock_serve.assert_called_once()


def test_do_get_hls_rejects_non_hls_mode_session():
    """A legitimate session that is NOT in hls mode must not be exposed
    via /hls/ paths. Forces playlist requests for non-HLS sessions to 404
    so a misconfigured client can't accidentally crash the proxy."""
    ctx = {
        "session_id": "xyz789",
        "mode": "legacy",  # not hls
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
        "duration_seconds": 100.0,
        "total_bytes": 1000000,
    }

    handler = _make_hls_handler(ctx, "/hls/xyz789/playlist.m3u8")

    with patch.object(handler, "_serve_hls_playlist") as mock_serve:
        handler.do_GET()

    mock_serve.assert_not_called()
    handler.send_error.assert_called_once_with(404)


def _make_producer(tmp_path, duration=600.0, seg_dur=30.0):
    """Construct an HlsProducer pointed at a temp working directory."""
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": duration,
        "hls_segment_duration": seg_dur,
    }
    return HlsProducer(ctx, str(tmp_path))


def test_hls_producer_serves_existing_complete_segment(tmp_path):
    """If seg_N.ts AND seg_N+1.ts both already exist on disk,
    wait_for_segment returns immediately without touching ffmpeg.
    Regression guard for "seek back to an already-produced segment"."""
    producer = _make_producer(tmp_path)
    seg_dir = producer.session_dir
    # Simulate ffmpeg having already written segments 5 and 6.
    import os as _os

    with open(_os.path.join(seg_dir, "seg_000005.ts"), "wb") as f:
        f.write(b"five")
    with open(_os.path.join(seg_dir, "seg_000006.ts"), "wb") as f:
        f.write(b"six")

    with patch("resources.lib.stream_proxy.subprocess.Popen") as mock_popen:
        result = producer.wait_for_segment(5, timeout=2.0)

    assert result == _os.path.join(seg_dir, "seg_000005.ts")
    mock_popen.assert_not_called()


def test_hls_producer_detects_mtime_stable_final_segment(tmp_path):
    """A final segment with no successor file is considered complete
    once its mtime has been stable longer than the stability window."""
    producer = _make_producer(tmp_path)
    seg_dir = producer.session_dir
    import os as _os
    import time as _time

    final_path = _os.path.join(seg_dir, "seg_000019.ts")
    with open(final_path, "wb") as f:
        f.write(b"final")
    # Force mtime into the past so the stability check passes immediately.
    old = _time.time() - 60
    _os.utime(final_path, (old, old))

    # Mark this segment as the terminal one so the producer's
    # "proc-exited" branch isn't triggered.
    with patch("resources.lib.stream_proxy.subprocess.Popen") as mock_popen:
        result = producer.wait_for_segment(19, timeout=2.0)

    assert result == final_path
    mock_popen.assert_not_called()


def test_hls_producer_cmd_has_no_reset_timestamps(tmp_path):
    """The persistent ffmpeg must NOT pass -reset_timestamps 1.

    With -reset_timestamps the segment muxer normalizes each output
    segment's PTS to near-zero. Kodi's Amlogic HW decoder treats the
    resulting per-segment resets as non-monotonic PTS, flags ``messy
    timestamps``, and eventually stalls with
    ``CAMLCodec::GetPicture: decoder timeout - elf:[5021ms]`` errors
    until playback freezes. Regression guard for the 2026-04-13
    Shawshank playback freeze.
    """
    producer = _make_producer(tmp_path, duration=600.0, seg_dur=30.0)
    cmd = producer._build_cmd(start_time=0.0, start_segment=0)
    assert "-reset_timestamps" not in cmd
    # -copyts must be present so that on a seek-restart the new
    # ffmpeg's output PTS continues from the source-time position.
    assert "-copyts" in cmd


def test_hls_producer_fmp4_build_cmd_contains_hls_segment_type(tmp_path):
    """fmp4 branch emits -f hls + -hls_segment_type fmp4 +
    -hls_fmp4_init_filename, and has NO -f segment."""
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        cmd = producer._build_cmd(start_time=0.0, start_segment=0)
        assert "-f" in cmd
        hls_idx = cmd.index("-f")
        assert cmd[hls_idx + 1] == "hls"
        assert "-hls_segment_type" in cmd
        seg_type_idx = cmd.index("-hls_segment_type")
        assert cmd[seg_type_idx + 1] == "fmp4"
        assert "-hls_fmp4_init_filename" in cmd
        assert "-hls_playlist_type" in cmd
        # Must NOT have the mpegts segment muxer
        assert "segment" not in [
            cmd[cmd.index("-f") + 1],
        ]
        assert "-segment_format" not in cmd
    finally:
        producer.close()


def test_hls_producer_fmp4_build_cmd_uses_padded_filename_pattern(tmp_path):
    """fmp4 hls_segment_filename uses the zero-padded seg_%06d.m4s
    pattern to match the mpegts branch and keep parser lookups
    consistent."""
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        cmd = producer._build_cmd(start_time=0.0, start_segment=0)
        filename_idx = cmd.index("-hls_segment_filename")
        seg_pattern = cmd[filename_idx + 1]
        assert seg_pattern.endswith("seg_%06d.m4s")
    finally:
        producer.close()


def test_hls_producer_fmp4_build_cmd_drops_subtitles(tmp_path):
    """fmp4 branch uses -sn (subtitles dropped) — documented Non-Goal
    regression guard."""
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        cmd = producer._build_cmd(start_time=0.0, start_segment=0)
        assert "-sn" in cmd
        assert "-c:s" not in cmd
    finally:
        producer.close()


def test_hls_producer_mpegts_build_cmd_unchanged(tmp_path):
    """Regression guard: mpegts branch still contains -f segment and
    -segment_format mpegts (it's what existing linear-playback tests
    assume)."""
    producer = _make_producer(tmp_path)  # defaults to mpegts
    try:
        cmd = producer._build_cmd(start_time=0.0, start_segment=0)
        assert "-f" in cmd
        f_idx = cmd.index("-f")
        assert cmd[f_idx + 1] == "segment"
        assert "-segment_format" in cmd
        fmt_idx = cmd.index("-segment_format")
        assert cmd[fmt_idx + 1] == "mpegts"
        # Must NOT have the fmp4 flags
        assert "-hls_segment_type" not in cmd
        assert "-hls_fmp4_init_filename" not in cmd
    finally:
        producer.close()


def test_hls_producer_fmp4_segment_files_use_m4s_extension(tmp_path):
    """segment_path(N) returns .m4s for fmp4 producers, .ts for mpegts."""
    from resources.lib.stream_proxy import HlsProducer

    ctx_fmp4 = {
        "session_id": "sess_fmp4",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer_fmp4 = HlsProducer(ctx_fmp4, str(tmp_path))
    try:
        path = producer_fmp4.segment_path(5)
        assert path.endswith("seg_000005.m4s")
    finally:
        producer_fmp4.close()

    producer_ts = _make_producer(tmp_path)  # defaults to mpegts
    try:
        path = producer_ts.segment_path(5)
        assert path.endswith("seg_000005.ts")
    finally:
        producer_ts.close()


def test_hls_producer_starts_ffmpeg_when_no_file_exists(tmp_path):
    """If the requested segment doesn't exist on disk and no ffmpeg
    is running, the producer must spawn ffmpeg with -ss at the
    segment's start time and -segment_start_number matching the
    segment index."""
    producer = _make_producer(tmp_path, duration=600.0, seg_dur=30.0)

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # simulated running ffmpeg

    with patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        # Short timeout — we expect the timeout to expire because the
        # mocked ffmpeg never writes files. We're asserting ON the
        # spawn call, not on the return value.
        producer.wait_for_segment(3, timeout=0.5)

    mock_popen.assert_called()
    cmd = mock_popen.call_args[0][0]
    ss_idx = cmd.index("-ss")
    start_num_idx = cmd.index("-segment_start_number")
    # Segment 3 at 30 s each → -ss 90.000
    assert cmd[ss_idx + 1] == "90.000"
    assert cmd[start_num_idx + 1] == "3"
    # Segment muxer, MPEG-TS format.
    f_idx = cmd.index("-f")
    assert cmd[f_idx + 1] == "segment"
    sf_idx = cmd.index("-segment_format")
    assert cmd[sf_idx + 1] == "mpegts"
    # Output template must land in the producer's session dir.
    assert cmd[-1].startswith(producer.session_dir)
    assert cmd[-1].endswith("seg_%06d.ts")


def test_hls_producer_restarts_ffmpeg_on_backward_seek(tmp_path):
    """If ffmpeg is running at segment N but seg M < N is requested,
    the producer kills the current ffmpeg and starts a new one aimed
    at segment M."""
    producer = _make_producer(tmp_path)

    old_proc = MagicMock()
    old_proc.poll.return_value = None  # alive
    producer._proc = old_proc
    producer._start_segment = 50

    new_proc = MagicMock()
    new_proc.poll.return_value = None

    with patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=new_proc
    ) as mock_popen:
        producer.wait_for_segment(10, timeout=0.3)

    old_proc.kill.assert_called_once()
    mock_popen.assert_called()
    cmd = mock_popen.call_args[0][0]
    ss_idx = cmd.index("-ss")
    assert cmd[ss_idx + 1] == "300.000"  # 10 * 30 s


def test_hls_producer_does_not_restart_on_small_forward_seek(tmp_path):
    """If ffmpeg is running at segment N and seg N+5 is requested
    (small forward jump), the producer does NOT restart — it waits
    for ffmpeg to naturally produce the segment."""
    producer = _make_producer(tmp_path)

    alive_proc = MagicMock()
    alive_proc.poll.return_value = None
    producer._proc = alive_proc
    producer._start_segment = 10

    with patch("resources.lib.stream_proxy.subprocess.Popen") as mock_popen:
        producer.wait_for_segment(12, timeout=0.3)

    mock_popen.assert_not_called()
    alive_proc.kill.assert_not_called()


def test_hls_producer_preserves_init_across_respawn(tmp_path):
    """_ensure_ffmpeg_headed_for in fmp4 mode must NOT unlink
    init.mp4 on respawn. The canonical init bytes cache in the
    producer has already committed to serving the first generation's
    init to every Kodi fetch, so whatever ffmpeg writes to the disk
    file on subsequent generations is irrelevant. Unlinking would
    just race the on-disk overwrite and momentarily fail the
    _init_file_complete check for no gain. Regression guard for
    the rewrite that added the canonical cache."""
    import os as _os

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        init_path = _os.path.join(producer.session_dir, "init.mp4")
        with open(init_path, "wb") as f:
            f.write(b"GEN_0_INIT")

        init_existed_at_spawn = {"value": None}
        init_bytes_at_spawn = {"value": None}

        def spy_popen(*args, **kwargs):
            init_existed_at_spawn["value"] = _os.path.exists(init_path)
            if init_existed_at_spawn["value"]:
                with open(init_path, "rb") as f:
                    init_bytes_at_spawn["value"] = f.read()
            proc = MagicMock()
            proc.poll.return_value = None
            return proc

        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            side_effect=spy_popen,
        ):
            producer._ensure_ffmpeg_headed_for(40)

        assert init_existed_at_spawn["value"] is True
        assert init_bytes_at_spawn["value"] == b"GEN_0_INIT"
    finally:
        producer.close()


def test_hls_producer_segment_complete_rejects_stale_prior_generation_segment(
    tmp_path,
):
    """_segment_complete in fmp4 mode must NOT return True for a
    segment file whose mtime predates the current ffmpeg generation's
    spawn time, even if the mtime-stability fallback is satisfied.

    Regression for H1 from the branch review: a backward seek can
    leave a stale ``seg_n.m4s`` from a prior generation on disk
    (mtime far in the past). The mtime-stability path's
    ``(now - mtime) > 500ms`` check is trivially true for such a
    file, and without the generation guard ``_segment_complete``
    would return True. Kodi would then read that stale segment
    against the canonical (current-generation) init.mp4 — different
    edit list / timestamp base, decoder glitch or stall.
    """
    import os as _os
    import time as _time

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess-stale",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 6.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        # Simulate a stale segment from a prior generation: write
        # the file and backdate it well before the current spawn.
        seg_path = producer.segment_path(40)
        with open(seg_path, "wb") as f:
            f.write(b"STALE_GEN_BYTES")
        ancient_mtime = _time.time() - 3600  # 1 hour ago
        _os.utime(seg_path, (ancient_mtime, ancient_mtime))
        # Pretend ffmpeg respawned just now, AFTER the stale file
        # was written. The current generation has not produced
        # seg_40 yet.
        producer._spawn_time = _time.time()

        # _segment_complete(40) must return False — the stale file
        # belongs to a prior generation and must not be served.
        assert producer._segment_complete(40) is False

        # Sanity check: a freshly-written file that postdates
        # spawn_time should be considered complete via the mtime
        # fallback once it's been stable.
        with open(seg_path, "wb") as f:
            f.write(b"NEW_GEN_BYTES")
        # mtime is now — but we need it stable for 500 ms to trip
        # the fallback. Backdate to spawn_time + small offset so
        # mtime > spawn_time AND (now - mtime) > 500 ms.
        fresh_mtime = producer._spawn_time + 0.001
        _os.utime(seg_path, (fresh_mtime, fresh_mtime))
        # Sleep just past the stable window.
        _time.sleep(0.6)
        assert producer._segment_complete(40) is True
    finally:
        producer.close()


def test_hls_producer_unlinks_new_target_segment_before_respawn(tmp_path):
    """_ensure_ffmpeg_headed_for in fmp4 mode unlinks
    seg_<new_target>.m4s before Popen. OTHER stale segments at
    different indices must still be present (regression guard for
    the backward-seek cache)."""
    import os as _os

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        target_path = _os.path.join(producer.session_dir, "seg_000040.m4s")
        other_path = _os.path.join(producer.session_dir, "seg_000005.m4s")
        with open(target_path, "wb") as f:
            f.write(b"STALE TARGET")
        with open(other_path, "wb") as f:
            f.write(b"STALE OTHER")

        target_existed_at_spawn = {"value": None}
        other_existed_at_spawn = {"value": None}

        def spy_popen(*args, **kwargs):
            target_existed_at_spawn["value"] = _os.path.exists(target_path)
            other_existed_at_spawn["value"] = _os.path.exists(other_path)
            proc = MagicMock()
            proc.poll.return_value = None
            return proc

        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            side_effect=spy_popen,
        ):
            producer._ensure_ffmpeg_headed_for(40)

        assert target_existed_at_spawn["value"] is False
        assert other_existed_at_spawn["value"] is True
    finally:
        producer.close()


def test_hls_producer_unlink_does_not_run_for_mpegts_branch(tmp_path):
    """Regression guard: mpegts branch does NOT unlink anything
    before spawn (preserves existing behavior)."""
    import os as _os

    producer = _make_producer(tmp_path)  # defaults to mpegts
    try:
        stale_path = _os.path.join(producer.session_dir, "seg_000040.ts")
        with open(stale_path, "wb") as f:
            f.write(b"STALE")

        stale_existed_at_spawn = {"value": None}

        def spy_popen(*args, **kwargs):
            stale_existed_at_spawn["value"] = _os.path.exists(stale_path)
            proc = MagicMock()
            proc.poll.return_value = None
            return proc

        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            side_effect=spy_popen,
        ):
            producer._ensure_ffmpeg_headed_for(40)

        # mpegts branch must leave the stale file alone
        assert stale_existed_at_spawn["value"] is True
    finally:
        producer.close()


def test_hls_producer_close_kills_ffmpeg_and_removes_dir(tmp_path):
    """close() must kill ffmpeg and delete the session directory."""
    producer = _make_producer(tmp_path)
    seg_dir = producer.session_dir
    import os as _os

    # Drop a bogus file so we can verify the directory is removed.
    with open(_os.path.join(seg_dir, "seg_000000.ts"), "wb") as f:
        f.write(b"x")
    assert _os.path.isdir(seg_dir)

    alive_proc = MagicMock()
    alive_proc.poll.return_value = None
    producer._proc = alive_proc

    producer.close()

    alive_proc.kill.assert_called_once()
    assert not _os.path.exists(seg_dir)


def test_hls_producer_opens_ffmpeg_log_in_init(tmp_path):
    """HlsProducer.__init__ opens session_dir/ffmpeg.log for append
    writes and stores it on self._ffmpeg_log."""
    import os as _os

    producer = _make_producer(tmp_path)
    log_path = _os.path.join(producer.session_dir, "ffmpeg.log")
    assert _os.path.exists(log_path)
    assert hasattr(producer, "_ffmpeg_log")
    assert not producer._ffmpeg_log.closed
    producer.close()


def test_hls_producer_init_ready_initialized_to_false(tmp_path):
    """Fresh producer has _init_ready=False without any spawn.
    Regression guard for AttributeError if _init_ready were only
    assigned in the spawn path."""
    producer = _make_producer(tmp_path)
    assert hasattr(producer, "_init_ready")
    assert producer._init_ready is False
    producer.close()


def test_hls_producer_defaults_segment_format_to_mpegts(tmp_path):
    """When ctx does not set hls_segment_format, the producer defaults
    to mpegts so existing callers keep their behavior."""
    producer = _make_producer(tmp_path)
    assert producer.segment_format == "mpegts"
    producer.close()


def test_hls_producer_reads_fmp4_segment_format_from_ctx(tmp_path):
    """When ctx sets hls_segment_format=fmp4, the producer stores it."""
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    assert producer.segment_format == "fmp4"
    producer.close()


def test_hls_producer_close_closes_ffmpeg_log(tmp_path):
    """close() closes the session-wide ffmpeg.log file handle."""
    producer = _make_producer(tmp_path)
    log_handle = producer._ffmpeg_log
    producer.close()
    assert log_handle.closed


def test_hls_producer_spawns_ffmpeg_with_session_log_as_stderr(tmp_path):
    """_ensure_ffmpeg_headed_for spawns ffmpeg with stderr=the
    session-wide log handle, not subprocess.PIPE. Regression guard
    for the deadlock bug."""
    producer = _make_producer(tmp_path, duration=600.0, seg_dur=30.0)

    mock_proc = MagicMock()
    mock_proc.poll.return_value = None

    with patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ) as mock_popen:
        producer._ensure_ffmpeg_headed_for(0)

    assert mock_popen.called
    _, kwargs = mock_popen.call_args
    import subprocess as _sp

    assert kwargs.get("stderr") is producer._ffmpeg_log
    assert kwargs.get("stderr") is not _sp.PIPE
    producer.close()


def test_hls_producer_reuses_same_log_handle_across_restarts(tmp_path):
    """Both ffmpeg spawns across a kill-and-restart receive the
    same stderr object identity. Regression guard for the
    file-descriptor leak."""
    producer = _make_producer(tmp_path, duration=600.0, seg_dur=30.0)

    spawn1_proc = MagicMock()
    spawn1_proc.poll.return_value = None
    spawn2_proc = MagicMock()
    spawn2_proc.poll.return_value = None

    with patch(
        "resources.lib.stream_proxy.subprocess.Popen",
        side_effect=[spawn1_proc, spawn2_proc],
    ) as mock_popen:
        producer._ensure_ffmpeg_headed_for(0)
        # Now force a far-forward seek (triggers restart because
        # 100 - 0 > 60).
        producer._ensure_ffmpeg_headed_for(100)

    assert mock_popen.call_count == 2
    stderr1 = mock_popen.call_args_list[0].kwargs["stderr"]
    stderr2 = mock_popen.call_args_list[1].kwargs["stderr"]
    assert stderr1 is stderr2
    assert stderr1 is producer._ffmpeg_log
    producer.close()


def test_hls_producer_concurrent_seek_respawn_starts_single_ffmpeg(tmp_path):
    """Two simultaneous seek-driven respawn requests must not start two
    ffmpeg processes for the same target segment."""
    import time as _time

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        dead_proc = MagicMock()
        dead_proc.poll.return_value = 1
        producer._proc = dead_proc
        producer._start_segment = 80

        live_proc = MagicMock()
        live_proc.poll.return_value = None
        popen_calls = []

        def fake_popen(*args, **kwargs):
            popen_calls.append(args[0])
            _time.sleep(0.05)
            return live_proc

        threads = [
            threading.Thread(target=producer._ensure_ffmpeg_headed_for, args=(10,))
            for _ in range(2)
        ]
        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            side_effect=fake_popen,
        ):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert len(popen_calls) == 1
        assert producer._start_segment == 10
    finally:
        producer._proc = None
        producer.close()


def test_hls_producer_prepare_is_noop_for_mpegts(tmp_path):
    """mpegts producers stay lazy — prepare() does not spawn."""
    producer = _make_producer(tmp_path)  # defaults to mpegts
    try:
        with patch("resources.lib.stream_proxy.subprocess.Popen") as mock_popen:
            producer.prepare()
        assert not mock_popen.called
    finally:
        producer.close()


def test_hls_producer_prepare_returns_when_init_and_first_segment_appear(
    tmp_path,
):
    """fmp4 producer; Popen returns a mock whose poll() returns None
    (alive). prepare() must wait for init.mp4 + seg_000000.m4s on
    disk before returning. We simulate ffmpeg's output by writing
    those files mid-prepare via a side-effect on Popen."""
    import os as _os
    import threading as _threading

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        def write_files_after_delay():
            # Drop the files into the session dir 100 ms after Popen
            # to simulate ffmpeg producing its first output.
            import time as _time

            _time.sleep(0.1)
            with open(_os.path.join(producer.session_dir, "init.mp4"), "wb") as f:
                f.write(b"INIT")
            with open(_os.path.join(producer.session_dir, "seg_000000.m4s"), "wb") as f:
                f.write(b"SEG0")

        def spy_popen(*args, **kwargs):
            _threading.Thread(target=write_files_after_delay, daemon=True).start()
            return mock_proc

        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            side_effect=spy_popen,
        ):
            producer.prepare()  # must not raise
    finally:
        producer.close()


def test_hls_producer_prepare_raises_if_no_output_within_deadline(tmp_path):
    """fmp4 producer; Popen returns an alive mock but no files ever
    appear on disk. prepare() must raise after the production
    deadline so _register_session falls back to matroska. This is
    the runtime safety net for ffmpeg/source combos that spawn
    cleanly but never produce output (analysis hang, slow
    upstream, etc)."""
    import pytest
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    # Shrink the deadline for the test so we don't sit for 30 s.
    producer._PREPARE_PRODUCTION_TIMEOUT_SECONDS = 0.5
    try:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            return_value=mock_proc,
        ):
            with pytest.raises(RuntimeError, match="did not produce"):
                producer.prepare()
    finally:
        producer.close()


def test_hls_producer_prepare_raises_if_ffmpeg_dies_during_production_wait(
    tmp_path,
):
    """fmp4 producer; ffmpeg starts alive but exits non-zero before
    producing init.mp4. prepare() must raise immediately on the
    next poll cycle, not wait for the full 30 s production
    deadline."""
    import pytest
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        mock_proc = MagicMock()
        # First few poll() calls return None (alive — passes the
        # 500 ms argv-rejection window). Then return 1 (exited).
        mock_proc.poll.side_effect = [None] * 12 + [1] * 100  # ~600 ms alive, then exit
        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            return_value=mock_proc,
        ):
            with pytest.raises(RuntimeError, match="exited with code"):
                producer.prepare()
    finally:
        producer.close()


def test_hls_producer_prepare_raises_when_ffmpeg_exits_immediately(tmp_path):
    """fmp4 producer; Popen returns a mock whose poll() returns 1
    (exited). prepare() raises RuntimeError mentioning the exit code."""
    import pytest
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 1  # exited with non-zero
        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            return_value=mock_proc,
        ):
            with pytest.raises(RuntimeError, match="1"):
                producer.prepare()
    finally:
        producer._proc = None  # avoid close() trying to kill the mock
        producer.close()


def test_hls_producer_prepare_raises_when_popen_fails(tmp_path):
    """fmp4 producer; Popen raises OSError. The current
    _ensure_ffmpeg_headed_for swallows OSError and leaves _proc=None.
    prepare() should detect the None state and raise RuntimeError."""
    import pytest
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            side_effect=OSError("ffmpeg not found"),
        ):
            with pytest.raises(RuntimeError):
                producer.prepare()
    finally:
        producer.close()


def test_hls_producer_init_file_complete_requires_current_generation_segment(tmp_path):
    """_init_file_complete binds to seg_<start_segment>.m4s, not 'any
    segment'. Only init.mp4 on disk -> False. init + seg_000099.m4s
    (wrong index) -> still False. init + seg_000100.m4s at the
    current target -> True."""
    import os as _os

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        # Simulate a restart target at 100
        producer._start_segment = 100

        assert producer._init_file_complete() is False  # nothing on disk

        init_path = _os.path.join(producer.session_dir, "init.mp4")
        with open(init_path, "wb") as f:
            f.write(b"INIT")
        assert producer._init_file_complete() is False  # no segment

        wrong_seg = _os.path.join(producer.session_dir, "seg_000099.m4s")
        with open(wrong_seg, "wb") as f:
            f.write(b"WRONG")
        assert producer._init_file_complete() is False  # wrong index

        right_seg = _os.path.join(producer.session_dir, "seg_000100.m4s")
        with open(right_seg, "wb") as f:
            f.write(b"RIGHT")
        assert producer._init_file_complete() is True
    finally:
        producer.close()


def test_hls_producer_init_ready_ignores_stale_segments_from_prior_generation(tmp_path):
    """Pre-seed init.mp4 + seg_000005.m4s from a prior generation.
    Set _start_segment=100. _init_file_complete returns False —
    the stale seg_000005 is not the current generation's first
    segment. After creating seg_000100.m4s, it returns True."""
    import os as _os

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        init_path = _os.path.join(producer.session_dir, "init.mp4")
        with open(init_path, "wb") as f:
            f.write(b"INIT FROM CURRENT GEN")
        stale_seg = _os.path.join(producer.session_dir, "seg_000005.m4s")
        with open(stale_seg, "wb") as f:
            f.write(b"STALE")

        producer._start_segment = 100
        assert producer._init_file_complete() is False

        fresh_seg = _os.path.join(producer.session_dir, "seg_000100.m4s")
        with open(fresh_seg, "wb") as f:
            f.write(b"FRESH")
        assert producer._init_file_complete() is True
    finally:
        producer.close()


def test_hls_producer_init_file_complete_does_not_use_mtime_window(tmp_path):
    """An ancient-mtime init.mp4 alone (no matching current-generation
    segment) never satisfies _init_file_complete. Regression guard
    against reintroducing an mtime-stable window."""
    import os as _os
    import time as _time

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        init_path = _os.path.join(producer.session_dir, "init.mp4")
        with open(init_path, "wb") as f:
            f.write(b"INIT")
        # Mtime 10 seconds ago
        ancient = _time.time() - 10
        _os.utime(init_path, (ancient, ancient))

        producer._start_segment = 0
        # No seg_000000.m4s -> False, regardless of mtime stability
        assert producer._init_file_complete() is False
    finally:
        producer.close()


def test_hls_producer_init_file_complete_returns_false_for_mpegts_ctx(tmp_path):
    """mpegts producers never return True from _init_file_complete —
    the method is fmp4-only."""
    producer = _make_producer(tmp_path)  # mpegts
    try:
        assert producer._init_file_complete() is False
    finally:
        producer.close()


def test_hls_producer_wait_for_init_returns_path_when_current_target_segment_exists(  # noqa: E501
    tmp_path,
):
    """Producer at _start_segment=0 with init.mp4 and seg_000000.m4s
    on disk. wait_for_init returns the init path."""
    import os as _os

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        init_path = _os.path.join(producer.session_dir, "init.mp4")
        seg_path = _os.path.join(producer.session_dir, "seg_000000.m4s")
        with open(init_path, "wb") as f:
            f.write(b"INIT")
        with open(seg_path, "wb") as f:
            f.write(b"SEG0")

        # Patch Popen so no real ffmpeg is started. We expect
        # wait_for_init to see the existing files and return
        # without spawning.
        with patch("resources.lib.stream_proxy.subprocess.Popen"):
            result = producer.wait_for_init(timeout=2.0)

        assert result == init_path
    finally:
        producer.close()


def test_hls_producer_wait_for_init_returns_none_for_mpegts(tmp_path):
    """mpegts producers short-circuit wait_for_init to None (there
    is no init file)."""
    producer = _make_producer(tmp_path)
    try:
        result = producer.wait_for_init(timeout=0.5)
        assert result is None
    finally:
        producer.close()


def test_hls_producer_wait_for_init_spawns_ffmpeg_when_not_running(tmp_path):
    """Regression guard for the bootstrap deadlock bug. Before
    wait_for_init, _proc is None. After wait_for_init (even on
    timeout), Popen was called at least once."""
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # alive
        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            return_value=mock_proc,
        ) as mock_popen:
            producer.wait_for_init(timeout=0.5)
        assert mock_popen.called
    finally:
        producer.close()


def test_hls_producer_wait_for_init_does_not_rewind_live_producer(tmp_path):
    """Regression guard for the rewind bug. If _proc is already alive
    (simulating a running ffmpeg at seg 40), wait_for_init must NOT
    call Popen again."""
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 3600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        # Pre-install a fake live proc
        live_proc = MagicMock()
        live_proc.poll.return_value = None  # alive
        producer._proc = live_proc
        producer._start_segment = 40

        with patch("resources.lib.stream_proxy.subprocess.Popen") as mock_popen:
            producer.wait_for_init(timeout=0.5)

        assert not mock_popen.called
        # start_segment must still be 40 — no rewind
        assert producer._start_segment == 40
    finally:
        producer._proc = None  # avoid close() trying to kill the mock
        producer.close()


def test_hls_producer_wait_for_init_respawns_at_current_target_after_crash(tmp_path):
    """If _proc is dead (poll() returns non-None) and
    _start_segment=40, wait_for_init's respawn targets seg 40, not
    0. Regression guard for a crashed-mid-seek producer being
    accidentally rewound."""
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 3600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        dead_proc = MagicMock()
        dead_proc.poll.return_value = 1  # exited
        producer._proc = dead_proc
        producer._start_segment = 40

        new_proc = MagicMock()
        new_proc.poll.return_value = None
        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            return_value=new_proc,
        ) as mock_popen:
            producer.wait_for_init(timeout=0.5)

        assert mock_popen.called
        args, _kwargs = mock_popen.call_args
        cmd = args[0]
        # The new -ss value should be 40 * 30.0 = 1200.0 seconds.
        ss_idx = cmd.index("-ss")
        assert float(cmd[ss_idx + 1]) == 1200.0
        # And -start_number should be 40 (fmp4) or -segment_start_number
        # should be 40 (mpegts). This producer is fmp4.
        sn_idx = cmd.index("-start_number")
        assert cmd[sn_idx + 1] == "40"
    finally:
        producer._proc = None
        producer.close()


def test_hls_producer_wait_for_init_returns_none_on_timeout(tmp_path):
    """If no file ever appears, wait_for_init returns None within
    the test timeout."""
    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with patch(
            "resources.lib.stream_proxy.subprocess.Popen",
            return_value=mock_proc,
        ):
            result = producer.wait_for_init(timeout=0.5)
        assert result is None
    finally:
        producer.close()


def test_hls_producer_wait_for_segment_zero_blocks_until_init_ready(tmp_path):
    """In fmp4 mode, wait_for_segment(0) does not return even if
    seg_000000.m4s exists on disk, until init.mp4 is also present
    AND seg_<start_segment>.m4s exists (i.e. _init_file_complete
    returns True)."""
    import os as _os
    import threading as _threading
    import time as _time

    from resources.lib.stream_proxy import HlsProducer

    ctx = {
        "session_id": "sess1",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 600.0,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(tmp_path))
    try:
        # Pre-seed seg_000000.m4s WITHOUT init.mp4
        seg_path = _os.path.join(producer.session_dir, "seg_000000.m4s")
        with open(seg_path, "wb") as f:
            f.write(b"SEG0")

        # No init.mp4 on disk -> _init_file_complete returns False,
        # so wait_for_segment should not return. Patch Popen so any
        # ffmpeg start the loop triggers is a no-op.
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        # Create init.mp4 (and re-create seg_000000.m4s, which the
        # first ensure_ffmpeg_headed_for unlink will have wiped)
        # halfway through the wait so the gate eventually opens.
        def create_init_later():
            _time.sleep(0.5)
            init_path = _os.path.join(producer.session_dir, "init.mp4")
            with open(init_path, "wb") as f:
                f.write(b"INIT")
            with open(seg_path, "wb") as f:
                f.write(b"SEG0")

        t = _threading.Thread(target=create_init_later, daemon=True)
        t.start()
        try:
            with patch(
                "resources.lib.stream_proxy.subprocess.Popen",
                return_value=mock_proc,
            ):
                result = producer.wait_for_segment(0, timeout=3.0)
        finally:
            t.join()

        assert result == seg_path
    finally:
        producer.close()


def test_choose_hls_workdir_prefers_first_writable(tmp_path):
    """_choose_hls_workdir walks its candidate list in order and
    returns the first candidate whose parent is writable."""
    from resources.lib.stream_proxy import _choose_hls_workdir

    parent_a = tmp_path / "parent_a"
    parent_b = tmp_path / "parent_b"
    parent_a.mkdir()
    parent_b.mkdir()
    candidate_a = str(parent_a / "nzbdav-hls")
    candidate_b = str(parent_b / "nzbdav-hls")

    with patch(
        "resources.lib.stream_proxy._HLS_WORKDIR_CANDIDATES",
        (candidate_a, candidate_b),
    ):
        chosen = _choose_hls_workdir()

    assert chosen == candidate_a
    import os as _os

    assert _os.path.isdir(candidate_a)


def test_choose_hls_workdir_fallback_is_not_predictable(tmp_path):
    """Fallback workdir must not reuse a fixed shared temp path."""
    import os as _os

    from resources.lib.stream_proxy import _choose_hls_workdir

    predictable = str(tmp_path / "nzbdav-hls")

    with patch(
        "resources.lib.stream_proxy._HLS_WORKDIR_CANDIDATES",
        ("/missing-a", "/missing-b"),
    ):
        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            with patch(
                "resources.lib.stream_proxy._HLS_PRIVATE_TEMP_ROOT",
                None,
                create=True,
            ):
                chosen = _choose_hls_workdir()

    assert chosen.startswith(str(tmp_path) + _os.sep)
    assert chosen != predictable
    assert _os.path.isdir(chosen)


def test_register_session_hls_returns_playlist_url(tmp_path):
    """A force-remux / HLS session must register with a playlist URL
    and attach an HlsProducer pointing at the session's working
    directory on disk."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 12345

    ctx = {
        "mode": "hls",
        "remote_url": "http://host/film.mkv",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "duration_seconds": 60.0,
        "hls_segment_duration": 30.0,
    }
    import os as _os

    with patch(
        "resources.lib.stream_proxy._choose_hls_workdir",
        return_value=str(tmp_path),
    ):
        url = sp._register_session(ctx)

    assert url.startswith("http://127.0.0.1:12345/hls/")
    assert url.endswith("/playlist.m3u8")
    # Producer was attached and pointed at a per-session directory.
    producer = ctx.get("hls_producer")
    assert producer is not None
    assert _os.path.isdir(producer.session_dir)
    assert producer.session_dir.startswith(str(tmp_path))


def test_register_session_hls_producer_failure_rewrites_to_matroska():
    """When HlsProducer.__init__ raises, _register_session rewrites
    ctx in place to the matroska shape and returns a /stream/ URL
    (not /hls/...)."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    ctx = {
        "remote_url": "http://host/shawshank.mkv",
        "auth_header": None,
        "content_type": "application/vnd.apple.mpegurl",
        "mode": "hls",
        "remux": True,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 58 * 1024 * 1024 * 1024,
        "duration_seconds": 8532.0,
        "seekable": True,
        "hls_segment_duration": 30.0,
        "hls_segment_format": "fmp4",
    }

    with patch(
        "resources.lib.stream_proxy.HlsProducer",
        side_effect=OSError("workdir not writable"),
    ):
        url = sp._register_session(ctx)

    assert url.startswith("http://127.0.0.1:9999/stream/")
    assert "/hls/" not in url
    assert ctx.get("mode") is None
    assert "hls_segment_format" not in ctx
    assert "hls_producer" not in ctx
    assert ctx["content_type"] == "video/x-matroska"
    assert ctx["seekable"] is True


def test_register_session_hls_producer_failure_preserves_duration_and_seekable():
    """After the rewrite, duration_seconds and total_bytes are carried
    over from the original fmp4 ctx and seekable is recomputed via
    the matroska rule (duration not None AND total_bytes > 0)."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    ctx = {
        "remote_url": "http://host/shawshank.mkv",
        "auth_header": None,
        "content_type": "application/vnd.apple.mpegurl",
        "mode": "hls",
        "remux": True,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 123456789,
        "duration_seconds": 600.0,
        "seekable": True,
        "hls_segment_format": "fmp4",
    }

    with patch(
        "resources.lib.stream_proxy.HlsProducer",
        side_effect=OSError("boom"),
    ):
        sp._register_session(ctx)

    assert ctx["duration_seconds"] == 600.0
    assert ctx["total_bytes"] == 123456789
    assert ctx["seekable"] is True


def test_register_session_catches_non_oserror_exceptions():
    """HlsProducer.__init__ raising ValueError (or anything else)
    still produces the matroska rewrite, not an unhandled exception.

    Regression guard for the too-narrow except OSError in the
    pre-spike code."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    ctx = {
        "remote_url": "http://host/x.mkv",
        "auth_header": None,
        "content_type": "application/vnd.apple.mpegurl",
        "mode": "hls",
        "remux": True,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 100,
        "duration_seconds": 10.0,
        "seekable": True,
        "hls_segment_format": "fmp4",
    }

    with patch(
        "resources.lib.stream_proxy.HlsProducer",
        side_effect=ValueError("unexpected"),
    ):
        # Must NOT raise.
        url = sp._register_session(ctx)

    assert url.startswith("http://127.0.0.1:9999/stream/")
    assert ctx.get("mode") is None


def test_register_session_calls_producer_prepare():
    """Happy path: _register_session calls producer.prepare() exactly
    once after construction."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    ctx = {
        "remote_url": "http://host/x.mkv",
        "auth_header": None,
        "content_type": "application/vnd.apple.mpegurl",
        "mode": "hls",
        "remux": True,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 100,
        "duration_seconds": 10.0,
        "seekable": True,
        "hls_segment_format": "fmp4",
    }

    producer_mock = MagicMock()
    with patch("resources.lib.stream_proxy.HlsProducer", return_value=producer_mock):
        sp._register_session(ctx)

    producer_mock.prepare.assert_called_once_with()


def test_register_session_prepare_failure_rewrites_to_matroska():
    """If producer.prepare() raises (e.g. ffmpeg rejects fmp4 HLS),
    _register_session rewrites ctx in-place to matroska and returns
    a /stream/ URL. Regression guard for the spawn-time-validation
    safety property — without this, a deployed ffmpeg build that
    doesn't support fmp4 would surface as a 504 from
    /hls/<sess>/init.mp4 AFTER the URL had already been returned to
    Kodi."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    ctx = {
        "remote_url": "http://host/x.mkv",
        "auth_header": None,
        "content_type": "application/vnd.apple.mpegurl",
        "mode": "hls",
        "remux": True,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 100,
        "duration_seconds": 10.0,
        "seekable": True,
        "hls_segment_format": "fmp4",
    }

    producer_mock = MagicMock()
    producer_mock.prepare.side_effect = RuntimeError(
        "ffmpeg exited immediately with code 1 — fmp4 HLS unsupported"
    )
    with patch("resources.lib.stream_proxy.HlsProducer", return_value=producer_mock):
        url = sp._register_session(ctx)

    assert url.startswith("http://127.0.0.1:9999/stream/")
    assert "/hls/" not in url
    assert ctx.get("mode") is None
    assert ctx["content_type"] == "video/x-matroska"


def test_register_session_hls_success_unchanged():
    """Happy path regression: if HlsProducer.__init__ AND prepare
    both succeed, the returned URL is the HLS URL and ctx keeps
    mode=='hls' and hls_producer is set on the ctx."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    ctx = {
        "remote_url": "http://host/x.mkv",
        "auth_header": None,
        "content_type": "application/vnd.apple.mpegurl",
        "mode": "hls",
        "remux": True,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 100,
        "duration_seconds": 10.0,
        "seekable": True,
        "hls_segment_format": "fmp4",
    }

    producer_mock = MagicMock()
    # prepare is a no-op (MagicMock auto-returns None)
    with patch("resources.lib.stream_proxy.HlsProducer", return_value=producer_mock):
        url = sp._register_session(ctx)

    assert "/hls/" in url
    assert url.endswith("/playlist.m3u8")
    assert ctx["mode"] == "hls"
    assert ctx["hls_producer"] is producer_mock


def test_register_session_prepare_failure_closes_partially_initialized_producer():
    """Regression guard: when producer.prepare() raises, the
    partially initialized producer is close()'d before the matroska
    rewrite. Otherwise opt-in fmp4 plays against an unsupported
    ffmpeg build orphan session_dir + ffmpeg.log every time."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    ctx = {
        "remote_url": "http://host/x.mkv",
        "auth_header": None,
        "content_type": "application/vnd.apple.mpegurl",
        "mode": "hls",
        "remux": True,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 100,
        "duration_seconds": 10.0,
        "seekable": True,
        "hls_segment_format": "fmp4",
    }

    producer_mock = MagicMock()
    producer_mock.prepare.side_effect = RuntimeError("ffmpeg exited immediately")
    with patch("resources.lib.stream_proxy.HlsProducer", return_value=producer_mock):
        url = sp._register_session(ctx)

    producer_mock.close.assert_called_once_with()
    assert url.startswith("http://127.0.0.1:9999/stream/")
    assert ctx.get("mode") is None


def test_register_session_init_failure_does_not_call_close_on_undefined_producer():
    """Regression guard for the `producer = None` sentinel outside
    the try block: when HlsProducer.__init__ itself raises, no
    producer was ever constructed, so close() must not be called.
    The rewrite still happens and no AttributeError is raised."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_sessions = {}
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    ctx = {
        "remote_url": "http://host/x.mkv",
        "auth_header": None,
        "content_type": "application/vnd.apple.mpegurl",
        "mode": "hls",
        "remux": True,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "total_bytes": 100,
        "duration_seconds": 10.0,
        "seekable": True,
        "hls_segment_format": "fmp4",
    }

    with patch(
        "resources.lib.stream_proxy.HlsProducer",
        side_effect=OSError("workdir not writable"),
    ):
        # Must NOT raise AttributeError on a None producer.
        url = sp._register_session(ctx)

    assert url.startswith("http://127.0.0.1:9999/stream/")
    assert ctx.get("mode") is None


def test_serve_remux_matroska_keeps_accept_ranges_none():
    """The MP4-fallback matroska path must keep `Accept-Ranges: none`.

    Piped MKV has no Cues, so advertising bytes would disable Kodi's
    cache-based seek fallback without enabling real seek. Only the
    mpegts path flips to `Accept-Ranges: bytes`.
    """
    ctx = {
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "content_type": "video/x-matroska",
        # No output_format set → defaults to "matroska"
        "total_bytes": 5 * 1024 * 1024 * 1024,
        "duration_seconds": 3600.0,
        "seekable": True,
        "remux": True,
    }

    handler = _make_handler_with_server(ctx)

    mock_proc = MagicMock()
    mock_proc.stdout.read.return_value = b""
    mock_proc.stderr.read.return_value = b""

    with patch("resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc):
        handler._serve_remux(ctx)

    handler.send_response.assert_called_once_with(200)
    header_calls = {
        call.args[0]: call.args[1] for call in handler.send_header.call_args_list
    }
    assert header_calls["Accept-Ranges"] == "none"
    assert header_calls["Content-Type"] == "video/x-matroska"
    assert "Content-Length" not in header_calls


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


def test_head_seekable_remux_returns_accept_ranges_none():
    """HEAD on a seekable remux context currently returns Accept-Ranges:
    none. An experiment in v0.6.18 tried advertising bytes so Kodi would
    HTTP-seek past the cache window, but the pipe-output MKV has no Cues
    and Kodi's demuxer can't translate user seeks into byte offsets — the
    flag flip only disabled the working cache fallback. Keeping `none`
    until we can produce an MKV with a real seek index (Cues or fMP4)."""
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

    assert url.startswith("http://127.0.0.1:9999/stream/")
    ctx = sp._server.stream_context
    assert ctx["faststart"] is True
    assert ctx["remux"] is False
    assert info["seekable"] is True
    assert info["virtual_size"] == 5000000132


def test_prepare_stream_direct_redirect_for_already_faststart():
    """Already-faststart MP4 returns remote URL directly (no proxy hop)."""
    import threading

    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._server.stream_context = None
    sp._context_lock = threading.Lock()
    sp.port = 9999

    mock_layout = {
        "ftyp_data": b"\x00" * 16,
        "ftyp_end": 16,
        "moov_data": b"\x00" * 50,
        "mdat_offset": 66,
        "original_moov_offset": 16,
        "moov_before_mdat": True,
    }
    mock_faststart = {
        "header_data": b"\x00" * 66,
        "virtual_size": 566,
        "payload_remote_start": 66,
        "payload_remote_end": 67,
        "payload_size": 500,
        "already_faststart": True,
    }

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value=None
    ), patch.object(sp, "_get_content_length", return_value=566), patch(
        "resources.lib.stream_proxy.fetch_remote_mp4_layout",
        return_value=mock_layout,
    ), patch(
        "resources.lib.stream_proxy.build_faststart_layout",
        return_value=mock_faststart,
    ):
        url, info = sp.prepare_stream(
            "http://host/faststart.mp4", auth_header="Basic dXNlcjpwYXNz"
        )

    # Direct redirect: URL is the remote URL, not the proxy URL
    assert url == "http://host/faststart.mp4"
    assert info["direct"] is True
    assert info["seekable"] is True
    assert info["remux"] is False


# ---------------------------------------------------------------------------
# _notify_error — stream error notifications
# ---------------------------------------------------------------------------


def test_faststart_proxy_error_notifies_user():
    """_serve_mp4_faststart calls _notify on OSError."""
    ctx = {
        "remote_url": "http://host/movie.mp4",
        "auth_header": None,
        "faststart": True,
        "header_data": b"\x00" * 100,
        "virtual_size": 1000,
        "payload_remote_start": 100,
        "payload_size": 900,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=200-999")

    with patch(
        "resources.lib.stream_proxy.urlopen",
        side_effect=OSError("Connection reset"),
    ), patch("resources.lib.stream_proxy._notify") as mock_notify:
        handler._serve_mp4_faststart(ctx)

    mock_notify.assert_called_once()
    assert mock_notify.call_args[0][0] == "NZB-DAV"
    assert "Connection reset" in mock_notify.call_args[0][1]


def test_head_uses_session_path_context():
    ctx = {
        "remux": False,
        "content_type": "video/mp4",
        "content_length": 1000,
    }
    handler = _make_handler_with_server(ctx=None)
    handler.path = "/stream/session123"
    handler.server.stream_sessions = {"session123": ctx}

    handler.do_HEAD()

    handler.send_response.assert_called_once_with(200)
    assert ctx["last_access"] > 0


def test_get_stream_context_updates_last_access_under_context_lock():
    from types import SimpleNamespace

    class _TrackingLock:
        def __init__(self):
            self.held = False

        def __enter__(self):
            self.held = True
            return self

        def __exit__(self, exc_type, exc, tb):
            self.held = False
            return False

    class _LockAwareContext(dict):
        def __init__(self, *args, **kwargs):
            self.lock = kwargs.pop("lock")
            self.last_access_updates = []
            super().__init__(*args, **kwargs)

        def __setitem__(self, key, value):
            if key == "last_access":
                self.last_access_updates.append(self.lock.held)
            super().__setitem__(key, value)

    lock = _TrackingLock()
    ctx = _LockAwareContext(
        {
            "remux": False,
            "content_type": "video/mp4",
            "content_length": 1000,
        },
        lock=lock,
    )
    handler = _make_handler_with_server(ctx=None)
    handler.path = "/stream/session123"
    handler.server.owner_proxy = SimpleNamespace(_context_lock=lock)
    handler.server.stream_sessions = {"session123": ctx}

    resolved = handler._get_stream_context()

    assert resolved is ctx
    assert ctx.last_access_updates == [True]


# ---------------------------------------------------------------------------
# _serve_proxy — pass-through with zero-fill recovery for missing articles
# ---------------------------------------------------------------------------


def _mock_urlopen_response(chunks, status=206, headers=None):
    """Build a mock urlopen-returned object with given byte chunks."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    data = list(chunks) + [b""]
    resp.read = MagicMock(side_effect=data)
    resp.status = status
    resp.getcode = MagicMock(return_value=status)
    header_map = headers or {}
    resp.headers.get = MagicMock(
        side_effect=lambda key, default=None: header_map.get(key, default)
    )
    resp.close = MagicMock()
    return resp


def _collect_written(handler):
    """Return all bytes written to handler.wfile as a single bytes object."""
    total = b""
    for call in handler.wfile.write.call_args_list:
        arg = call[0][0]
        total += bytes(arg)
    return total


def test_serve_proxy_streams_happy_path():
    """Upstream delivers all bytes — client gets them verbatim."""
    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 2048,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=0-2047")

    payload = b"A" * 2048
    with patch(
        "resources.lib.stream_proxy.urlopen",
        return_value=_mock_urlopen_response([payload]),
    ):
        handler._serve_proxy(ctx)

    handler.send_response.assert_called_once_with(206)
    assert _collect_written(handler) == payload


@patch("resources.lib.stream_proxy.xbmc")
def test_serve_proxy_logs_terminal_summary_on_success(mock_xbmc):
    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 2048,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=0-2047")

    payload = b"A" * 2048
    with patch(
        "resources.lib.stream_proxy.urlopen",
        return_value=_mock_urlopen_response([payload]),
    ):
        handler._serve_proxy(ctx)

    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "Pass-through summary" in logged
    assert "reason=complete" in logged
    assert "streamed=2048" in logged
    assert "zero_fill=0" in logged
    assert "recoveries=0" in logged


def test_serve_proxy_zero_fills_on_upstream_failure():
    """Upstream cuts out mid-stream — proxy probes, zero-fills, resumes."""
    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 20 * 1048576,
    }
    handler = _make_handler_with_server(
        ctx, range_header="bytes=0-{}".format(20 * 1048576 - 1)
    )

    # Responses in order:
    # 1. Initial range 0..end: delivers 1 MB of real bytes then upstream closes
    # 2. Skip probe at +1 MB: success, returns 64 bytes
    # 3. Resume stream at offset 2M..end: delivers remaining 18 MB
    first_mb = b"X" * 1048576
    initial = _mock_urlopen_response([first_mb])
    probe_1mb = _mock_urlopen_response([b"Y" * 64])
    resume_payload = b"Z" * (18 * 1048576)
    resume = _mock_urlopen_response([resume_payload])

    responses = iter([initial, probe_1mb, resume])

    with patch(
        "resources.lib.stream_proxy.urlopen",
        side_effect=lambda *a, **kw: next(responses),
    ), patch("resources.lib.stream_proxy.time.sleep"):
        handler._serve_proxy(ctx)

    written = _collect_written(handler)
    assert len(written) == 20 * 1048576
    assert written[:1048576] == first_mb
    # Bytes 1M..2M are zero-fill (skip of 1 MB after the 1 MB already served).
    assert written[1048576 : 2 * 1048576] == bytes(1048576)
    assert written[2 * 1048576 :] == resume_payload


def test_serve_proxy_notifies_first_recovery_with_bytes_and_count():
    import sys

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 20 * 1048576,
    }
    handler = _make_handler_with_server(
        ctx, range_header="bytes=0-{}".format(20 * 1048576 - 1)
    )

    first_mb = b"X" * 1048576
    initial = _mock_urlopen_response([first_mb])
    probe_1mb = _mock_urlopen_response([b"Y" * 64])
    resume_payload = b"Z" * (18 * 1048576)
    resume = _mock_urlopen_response([resume_payload])
    responses = iter([initial, probe_1mb, resume])

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: {
        "strict_contract_mode": "warn",
        "density_breaker_enabled": "false",
        "zero_fill_budget_enabled": "true",
        "retry_ladder_enabled": "false",
    }.get(key, "")
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch(
            "resources.lib.stream_proxy.urlopen",
            side_effect=lambda *a, **kw: next(responses),
        ), patch("resources.lib.stream_proxy.time.sleep"), patch(
            "resources.lib.stream_proxy._notify"
        ) as mock_notify:
            handler._serve_proxy(ctx)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    mock_notify.assert_called_once()
    assert mock_notify.call_args[0][0] == "NZB-DAV"
    assert "1048576" in mock_notify.call_args[0][1]
    assert "1" in mock_notify.call_args[0][1]


def test_serve_proxy_retries_probes_when_upstream_briefly_down():
    """If all early probes fail fast, retry with backoff before giving up."""
    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 8 * 1048576,
    }
    handler = _make_handler_with_server(
        ctx, range_header="bytes=0-{}".format(8 * 1048576 - 1)
    )

    first_chunk = b"X" * 1048576
    initial = _mock_urlopen_response([first_chunk])
    # First two probe attempts raise ConnectionRefusedError (instant fail),
    # third attempt succeeds — simulates a brief upstream restart.
    probe_refused_1 = MagicMock()
    probe_refused_1.__enter__ = MagicMock(side_effect=ConnectionRefusedError())
    probe_refused_2 = MagicMock()
    probe_refused_2.__enter__ = MagicMock(side_effect=ConnectionRefusedError())
    probe_success = _mock_urlopen_response([b"Y" * 64])

    resume_payload = b"Z" * (6 * 1048576)
    resume = _mock_urlopen_response([resume_payload])

    responses = iter([initial, probe_refused_1, probe_refused_2, probe_success, resume])

    with patch(
        "resources.lib.stream_proxy.urlopen",
        side_effect=lambda *a, **kw: next(responses),
    ), patch("resources.lib.stream_proxy.time.sleep") as mock_sleep:
        handler._serve_proxy(ctx)

    # sleep was called at least twice (between retry attempts).
    assert mock_sleep.call_count >= 2
    written = _collect_written(handler)
    assert len(written) == 8 * 1048576
    assert written[:1048576] == first_chunk
    assert written[2 * 1048576 :] == resume_payload


def test_serve_proxy_debounces_recovery_notify_within_one_session():
    import sys

    from resources.lib.stream_proxy import (
        _UPSTREAM_RANGE_OK,
        _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
    )

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 6 * 1048576,
    }
    handler = _make_handler_with_server(
        ctx, range_header="bytes=0-{}".format(6 * 1048576 - 1)
    )

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: {
        "strict_contract_mode": "warn",
        "density_breaker_enabled": "false",
        "zero_fill_budget_enabled": "true",
        "retry_ladder_enabled": "false",
    }.get(key, "")
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch.object(
            handler,
            "_stream_upstream_range",
            side_effect=[
                (_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, 1048576),
                (_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, 1048576),
                (_UPSTREAM_RANGE_OK, 2 * 1048576),
            ],
        ), patch.object(
            handler, "_find_skip_offset", side_effect=[1048576, 1048576]
        ), patch.object(
            handler, "_write_zeros"
        ), patch(
            "resources.lib.stream_proxy._notify"
        ) as mock_notify:
            handler._serve_proxy(ctx)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    mock_notify.assert_called_once()


def test_serve_proxy_retries_original_range_before_skip_probe():
    import sys

    from resources.lib.stream_proxy import (
        _UPSTREAM_RANGE_OK,
        _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
        _UPSTREAM_RANGE_UPSTREAM_ERROR,
    )

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 4096,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=0-4095")

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: {
        "strict_contract_mode": "warn",
        "density_breaker_enabled": "false",
        "zero_fill_budget_enabled": "true",
        "retry_ladder_enabled": "true",
    }.get(key, "")
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch.object(
            handler,
            "_stream_upstream_range",
            side_effect=[
                (_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, 1024),
                (_UPSTREAM_RANGE_UPSTREAM_ERROR, 0),
                (_UPSTREAM_RANGE_OK, 3072),
            ],
        ) as mock_stream, patch.object(
            handler, "_find_skip_offset"
        ) as mock_find_skip_offset, patch(
            "resources.lib.stream_proxy.time.sleep"
        ) as mock_sleep:
            handler._serve_proxy(ctx)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    assert mock_stream.call_count == 3
    mock_find_skip_offset.assert_not_called()
    assert [call.args[0] for call in mock_sleep.call_args_list] == [2, 4]


def test_serve_proxy_falls_back_to_skip_probe_after_retry_ladder_exhausted():
    import sys

    from resources.lib.stream_proxy import (
        _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
        _UPSTREAM_RANGE_UPSTREAM_ERROR,
    )

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 4096,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=0-4095")

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: {
        "strict_contract_mode": "warn",
        "density_breaker_enabled": "false",
        "zero_fill_budget_enabled": "true",
        "retry_ladder_enabled": "true",
    }.get(key, "")
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch.object(
            handler,
            "_stream_upstream_range",
            side_effect=[
                (_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, 1024),
                (_UPSTREAM_RANGE_UPSTREAM_ERROR, 0),
                (_UPSTREAM_RANGE_UPSTREAM_ERROR, 0),
                (_UPSTREAM_RANGE_UPSTREAM_ERROR, 0),
            ],
        ), patch.object(
            handler, "_find_skip_offset", return_value=None
        ) as mock_find_skip_offset, patch.object(
            handler, "_write_zeros"
        ) as mock_write_zeros, patch(
            "resources.lib.stream_proxy.time.sleep"
        ) as mock_sleep:
            handler._serve_proxy(ctx)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    mock_find_skip_offset.assert_called_once_with(ctx, 1024, 4095)
    mock_write_zeros.assert_called_once_with(3072)
    assert [call.args[0] for call in mock_sleep.call_args_list] == [2, 4, 8]


def test_serve_proxy_retry_ladder_flag_skips_range_retries():
    import sys

    from resources.lib.stream_proxy import _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 4096,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=0-4095")

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: {
        "strict_contract_mode": "warn",
        "density_breaker_enabled": "false",
        "zero_fill_budget_enabled": "true",
        "retry_ladder_enabled": "false",
    }.get(key, "")
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch.object(
            handler,
            "_stream_upstream_range",
            return_value=(_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, 1024),
        ) as mock_stream, patch.object(
            handler, "_find_skip_offset", return_value=None
        ) as mock_find_skip_offset, patch.object(
            handler, "_write_zeros"
        ) as mock_write_zeros, patch(
            "resources.lib.stream_proxy.time.sleep"
        ) as mock_sleep:
            handler._serve_proxy(ctx)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    assert mock_stream.call_count == 1
    mock_find_skip_offset.assert_called_once_with(ctx, 1024, 4095)
    mock_write_zeros.assert_called_once_with(3072)
    assert mock_sleep.call_count == 0


def test_serve_proxy_zero_fills_remainder_when_recovery_exhausted():
    """All skip probes fail — zero-fill the rest of the committed response."""
    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 8 * 1048576,
    }
    handler = _make_handler_with_server(
        ctx, range_header="bytes=0-{}".format(8 * 1048576 - 1)
    )

    first_chunk = b"X" * 512000
    initial = _mock_urlopen_response([first_chunk])

    def _fail_probe(*args, **kwargs):
        raise OSError("article not found")

    responses = iter([initial])

    def _dispatch(*args, **kwargs):
        try:
            return next(responses)
        except StopIteration:
            return _fail_probe()

    with patch("resources.lib.stream_proxy.urlopen", side_effect=_dispatch), patch(
        "resources.lib.stream_proxy.time.sleep"
    ):
        handler._serve_proxy(ctx)

    written = _collect_written(handler)
    assert len(written) == 8 * 1048576
    assert written[:512000] == first_chunk
    # Everything after the real bytes is zero-filled.
    assert written[512000:] == bytes(8 * 1048576 - 512000)


@patch("resources.lib.stream_proxy.xbmc")
def test_serve_proxy_logs_terminal_summary_on_recovery_exhausted(mock_xbmc):
    from resources.lib.stream_proxy import _UPSTREAM_RANGE_UPSTREAM_ERROR

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 1024,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=0-1023")

    with patch.object(
        handler,
        "_stream_upstream_range",
        return_value=(_UPSTREAM_RANGE_UPSTREAM_ERROR, 256),
    ), patch.object(handler, "_find_skip_offset", return_value=None), patch.object(
        handler, "_write_zeros"
    ) as mock_write_zeros:
        handler._serve_proxy(ctx)

    mock_write_zeros.assert_called_once_with(768)
    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "Pass-through summary" in logged
    assert "reason=recovery_exhausted" in logged
    assert "streamed=256" in logged
    assert "zero_fill=768" in logged


@patch("resources.lib.stream_proxy.xbmc")
def test_serve_proxy_aborts_when_session_zero_fill_ratio_exceeds_cap(mock_xbmc):
    import sys

    from resources.lib.stream_proxy import _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 4096,
        "session_streamed_bytes": 4096,
        "session_zero_fill_bytes": 0,
        "session_recovery_count": 0,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=0-4095")

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: {
        "strict_contract_mode": "warn",
        "density_breaker_enabled": "false",
        "zero_fill_budget_enabled": "true",
        "retry_ladder_enabled": "false",
    }.get(key, "")
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch.object(
            handler,
            "_stream_upstream_range",
            return_value=(_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, 1024),
        ), patch.object(handler, "_find_skip_offset", return_value=600), patch.object(
            handler, "_write_zeros"
        ) as mock_write_zeros, patch(
            "resources.lib.stream_proxy._notify"
        ) as mock_notify:
            handler._serve_proxy(ctx)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    mock_write_zeros.assert_not_called()
    mock_notify.assert_called_once()
    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "reason=session_zero_fill_budget_exceeded" in logged


def test_serve_proxy_zero_fill_budget_flag_disables_session_ratio_abort():
    import sys

    from resources.lib.stream_proxy import (
        _UPSTREAM_RANGE_OK,
        _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE,
    )

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 4096,
        "session_streamed_bytes": 4096,
        "session_zero_fill_bytes": 0,
        "session_recovery_count": 0,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=0-4095")

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: {
        "strict_contract_mode": "warn",
        "density_breaker_enabled": "false",
        "zero_fill_budget_enabled": "false",
        "retry_ladder_enabled": "false",
    }.get(key, "")
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch.object(
            handler,
            "_stream_upstream_range",
            side_effect=[
                (_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, 1024),
                (_UPSTREAM_RANGE_OK, 2472),
            ],
        ), patch.object(handler, "_find_skip_offset", return_value=600), patch.object(
            handler, "_write_zeros"
        ) as mock_write_zeros, patch(
            "resources.lib.stream_proxy._notify"
        ) as mock_notify:
            handler._serve_proxy(ctx)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    mock_write_zeros.assert_called_once_with(600)
    mock_notify.assert_called_once()


def test_get_strict_contract_mode_maps_known_values_and_defaults_warn():
    import sys

    from resources.lib.stream_proxy import _get_strict_contract_mode

    mock_addon = MagicMock()
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        cases = {
            "": "warn",
            None: "warn",
            "0": "off",
            "off": "off",
            "1": "warn",
            "warn": "warn",
            "2": "enforce",
            "enforce": "enforce",
            "garbage": "warn",
        }
        for raw, expected in cases.items():
            mock_addon.getSetting.return_value = raw
            assert _get_strict_contract_mode() == expected
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original


@patch("resources.lib.stream_proxy.xbmc")
def test_stream_upstream_range_warn_mode_streams_on_soft_contract_mismatch(mock_xbmc):
    import sys

    from resources.lib.stream_proxy import (
        _UPSTREAM_RANGE_PROTOCOL_MISMATCH,
        _StreamHandler,
    )

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_length": 2048,
    }
    handler = _StreamHandler.__new__(_StreamHandler)
    handler.wfile = MagicMock()

    payload = b"A" * 1024
    response = _mock_urlopen_response(
        [payload],
        headers={
            "Content-Range": "bytes 0-1023/2048",
            "Content-Length": "2048",
        },
    )

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: (
        "warn" if key == "strict_contract_mode" else ""
    )
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch("resources.lib.stream_proxy.urlopen", return_value=response):
            result, written = handler._stream_upstream_range(ctx, 0, 1023)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    assert result == _UPSTREAM_RANGE_PROTOCOL_MISMATCH
    assert written == len(payload)
    assert _collect_written(handler) == payload
    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "reason=protocol_mismatch" in logged


@patch("resources.lib.stream_proxy.xbmc")
def test_stream_upstream_range_enforce_mode_rejects_soft_contract_mismatch(mock_xbmc):
    import sys

    from resources.lib.stream_proxy import (
        _UPSTREAM_RANGE_PROTOCOL_MISMATCH,
        _StreamHandler,
    )

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_length": 2048,
    }
    handler = _StreamHandler.__new__(_StreamHandler)
    handler.wfile = MagicMock()

    payload = b"A" * 1024
    response = _mock_urlopen_response(
        [payload],
        headers={
            "Content-Range": "bytes 0-1023/2048",
            "Content-Length": "2048",
        },
    )

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: (
        "enforce" if key == "strict_contract_mode" else ""
    )
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch("resources.lib.stream_proxy.urlopen", return_value=response):
            result, written = handler._stream_upstream_range(ctx, 0, 1023)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    assert result == _UPSTREAM_RANGE_PROTOCOL_MISMATCH
    assert written == 0
    handler.wfile.write.assert_not_called()
    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "reason=protocol_mismatch" in logged


@patch("resources.lib.stream_proxy.xbmc")
def test_stream_upstream_range_rejects_bad_content_range(mock_xbmc):
    import sys

    from resources.lib.stream_proxy import (
        _UPSTREAM_RANGE_PROTOCOL_MISMATCH,
        _StreamHandler,
    )

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_length": 2048,
    }
    handler = _StreamHandler.__new__(_StreamHandler)
    handler.wfile = MagicMock()

    response = _mock_urlopen_response(
        [b"A" * 1024],
        headers={
            "Content-Range": "bytes 256-1279/2048",
            "Content-Length": "1024",
        },
    )

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda key: (
        "warn" if key == "strict_contract_mode" else ""
    )
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch("resources.lib.stream_proxy.urlopen", return_value=response):
            result, written = handler._stream_upstream_range(ctx, 0, 1023)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    assert result == _UPSTREAM_RANGE_PROTOCOL_MISMATCH
    assert written == 0
    handler.wfile.write.assert_not_called()
    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "reason=protocol_mismatch" in logged
    assert "Content-Range" in logged


@patch("resources.lib.stream_proxy.xbmc")
def test_serve_proxy_density_breaker_aborts_and_notifies_once(mock_xbmc):
    import sys

    from resources.lib.stream_proxy import _UPSTREAM_RANGE_SHORT_READ_RECOVERABLE

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 8 * 1048576,
    }
    handler = _make_handler_with_server(
        ctx, range_header="bytes=0-{}".format(8 * 1048576 - 1)
    )

    def _get_setting(key):
        if key == "strict_contract_mode":
            return "warn"
        if key == "density_breaker_enabled":
            return "true"
        if key == "zero_fill_budget_enabled":
            return "false"
        if key == "retry_ladder_enabled":
            return "false"
        return ""

    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = _get_setting
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        with patch.object(
            handler,
            "_stream_upstream_range",
            return_value=(_UPSTREAM_RANGE_SHORT_READ_RECOVERABLE, 1048576),
        ), patch.object(
            handler, "_find_skip_offset", return_value=2 * 1048576
        ), patch.object(
            handler, "_write_zeros"
        ) as mock_write_zeros, patch(
            "resources.lib.stream_proxy._notify"
        ) as mock_notify:
            handler._serve_proxy(ctx)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    mock_write_zeros.assert_not_called()
    mock_notify.assert_called_once()
    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "reason=density_breaker_tripped" in logged


def test_serve_proxy_rejects_bad_range():
    """An unparseable Range header still returns 416 without emitting headers."""
    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 1000,
    }
    handler = _make_handler_with_server(ctx, range_header="bytes=banana")

    handler._serve_proxy(ctx)

    handler.send_error.assert_called_once_with(416)
    handler.send_response.assert_not_called()


def test_serve_proxy_no_range_defaults_to_206_partial_content():
    from resources.lib.stream_proxy import _UPSTREAM_RANGE_OK

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 2048,
    }
    handler = _make_handler_with_server(ctx)

    def fake_stream(_ctx, start, end, contract_mode=None):
        handler.wfile.write(b"x" * (end - start + 1))
        return _UPSTREAM_RANGE_OK, end - start + 1

    with patch.object(
        _StreamHandler, "_stream_upstream_range", side_effect=fake_stream
    ):
        handler._serve_proxy(ctx)

    handler.send_response.assert_called_once_with(206)
    handler.send_header.assert_any_call("Content-Length", "2048")
    handler.send_header.assert_any_call("Content-Range", "bytes 0-2047/2048")


def test_serve_proxy_no_range_can_send_200_without_content_range():
    from resources.lib.stream_proxy import _UPSTREAM_RANGE_OK

    ctx = {
        "remote_url": "http://host/movie.mkv",
        "auth_header": None,
        "content_type": "video/x-matroska",
        "content_length": 2048,
    }
    handler = _make_handler_with_server(ctx)

    def fake_stream(_ctx, start, end, contract_mode=None):
        handler.wfile.write(b"x" * (end - start + 1))
        return _UPSTREAM_RANGE_OK, end - start + 1

    with patch(
        "resources.lib.stream_proxy._get_addon_setting",
        side_effect=lambda key: "true" if key == "send_200_no_range" else None,
    ), patch.object(_StreamHandler, "_stream_upstream_range", side_effect=fake_stream):
        handler._serve_proxy(ctx)

    handler.send_response.assert_called_once_with(200)
    header_names = [call.args[0] for call in handler.send_header.call_args_list]
    assert "Content-Range" not in header_names


@pytest.mark.parametrize(
    ("factory", "call_name"),
    [
        (
            lambda tmp_path: (
                _make_handler_with_server(
                    {
                        "header_data": b"ftypmoov",
                        "virtual_size": 4096,
                        "payload_remote_start": 0,
                        "payload_size": 4088,
                        "remote_url": "http://host/movie.mp4",
                    },
                    range_header="bytes=9999-10000",
                ),
                "_serve_mp4_faststart",
            ),
            "faststart",
        ),
        (
            lambda tmp_path: (
                _make_handler_with_server(
                    {
                        "temp_path": str(tmp_path / "movie.mp4"),
                        "content_length": 4096,
                    },
                    range_header="bytes=10-5",
                ),
                "_serve_temp_faststart",
            ),
            "temp_faststart",
        ),
        (
            lambda tmp_path: (
                _make_handler_with_server(
                    {
                        "ffmpeg_path": "/usr/bin/ffmpeg",
                        "remote_url": "http://host/movie.mp4",
                        "auth_header": None,
                        "total_bytes": 4096,
                        "seekable": True,
                    },
                    range_header="bytes=-9999",
                ),
                "_serve_remux",
            ),
            "remux",
        ),
        (
            lambda tmp_path: (
                _make_handler_with_server(
                    {
                        "remote_url": "http://host/movie.mkv",
                        "auth_header": None,
                        "content_type": "video/x-matroska",
                        "content_length": 4096,
                    },
                    range_header="bytes=banana",
                ),
                "_serve_proxy",
            ),
            "pass_through",
        ),
    ],
)
def test_range_caller_matrix_returns_416_for_malformed_ranges(
    tmp_path, factory, call_name
):
    handler, method_name = factory(tmp_path)
    if call_name == "temp_faststart":
        tmp_path.joinpath("movie.mp4").write_bytes(b"x" * 32)

    getattr(handler, method_name)(handler.server.stream_context)

    handler.send_error.assert_called_once_with(416)
    handler.send_response.assert_not_called()
