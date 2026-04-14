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
    assert ctx.get("mode") != "hls"
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


def test_prepare_stream_rejects_invalid_scheme():
    import pytest
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    with pytest.raises(ValueError):
        sp.prepare_stream("file:///etc/passwd")


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


def test_build_ffmpeg_cmd_encodes_reserved_auth_chars():
    import base64

    handler = _make_handler()
    auth = "Basic " + base64.b64encode(b"user@domain:pa/ss?#word").decode()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": auth,
    }
    cmd = handler._build_ffmpeg_cmd(ctx)
    i_idx = cmd.index("-i")
    assert "user%40domain:pa%2Fss%3F%23word@host" in cmd[i_idx + 1]


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


# ---------------------------------------------------------------------------
# HLS playlist / segment handlers
# ---------------------------------------------------------------------------


def _make_hls_handler(ctx, request_path):
    """Construct a _StreamHandler for HLS path dispatch tests."""
    import threading

    from resources.lib.stream_proxy import _StreamHandler

    handler = _StreamHandler.__new__(_StreamHandler)
    handler.server = MagicMock()
    session_id = ctx.get("session_id", "abc123")
    handler.server.stream_sessions = {session_id: ctx}
    handler.server.stream_context = ctx
    handler.server.active_ffmpeg = None
    handler.server.current_byte_pos = 0
    handler.server.ffmpeg_lock = threading.Lock()
    handler.path = request_path
    handler.headers = {}
    handler.wfile = MagicMock()
    handler.connection = MagicMock()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.send_error = MagicMock()
    return handler


def test_parse_hls_resource_playlist():
    from resources.lib.stream_proxy import _StreamHandler

    assert _StreamHandler._parse_hls_resource("/hls/abc/playlist.m3u8") == (
        "abc",
        "playlist",
    )


def test_parse_hls_resource_segment():
    from resources.lib.stream_proxy import _StreamHandler

    assert _StreamHandler._parse_hls_resource("/hls/abc/seg_42.ts") == (
        "abc",
        ("segment", 42),
    )


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


def test_hls_segment_seconds_is_at_least_30():
    """The segment duration must be large enough to hide ffmpeg's
    cold-start time on the next segment.

    On the CoreELEC test box, each segment spawns a fresh ffmpeg
    that takes ~10-15 s to open the remote 58 GB MKV, parse the
    container, seek to the requested keyframe, and start emitting
    MPEG-TS packets. If segment duration is shorter than cold
    start, Kodi runs out of buffered data before the next segment
    is ready, and playback caches continuously. 30 s gives Kodi
    comfortable headroom. Regression test for the ``constant
    caching`` report during Shawshank playback.
    """
    from resources.lib.stream_proxy import _HLS_SEGMENT_SECONDS

    assert _HLS_SEGMENT_SECONDS >= 30.0


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


# ---------------------------------------------------------------------------
# _serve_proxy — pass-through with zero-fill recovery for missing articles
# ---------------------------------------------------------------------------


def _mock_urlopen_response(chunks, status=206):
    """Build a mock urlopen-returned object with given byte chunks."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    data = list(chunks) + [b""]
    resp.read = MagicMock(side_effect=data)
    resp.status = status
    resp.getcode = MagicMock(return_value=status)
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
