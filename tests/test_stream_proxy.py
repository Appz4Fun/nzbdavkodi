# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Unit tests for stream_proxy.py MP4 faststart and range-serving logic."""

import struct
from unittest.mock import MagicMock, patch

from resources.lib.stream_proxy import (
    _adjust_moov_offsets,
    _read_box_header,
    _StreamHandler,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_box(box_type: bytes, body: bytes) -> bytes:
    """Build a minimal 8-byte-header MP4 box."""
    return struct.pack(">I", 8 + len(body)) + box_type + body


def _make_stco(*offsets):
    """Build a stco box with the given 32-bit chunk offsets."""
    body = b"\x00\x00\x00\x00"  # version + flags
    body += struct.pack(">I", len(offsets))
    for o in offsets:
        body += struct.pack(">I", o)
    return _make_box(b"stco", body)


def _make_co64(*offsets):
    """Build a co64 box with the given 64-bit chunk offsets."""
    body = b"\x00\x00\x00\x00"  # version + flags
    body += struct.pack(">I", len(offsets))
    for o in offsets:
        body += struct.pack(">Q", o)
    return _make_box(b"co64", body)


def _wrap_containers(inner: bytes, *container_types) -> bytes:
    """Wrap inner data in nested container boxes (innermost first)."""
    data = inner
    for ct in container_types:
        data = _make_box(ct, data)
    return data


def _parse_stco_entries(data: bytes):
    """Return the list of 32-bit chunk offsets from the first stco in data."""
    idx = data.find(b"stco")
    assert idx >= 4, "stco not found"
    content_start = idx + 4  # skip past the type field (size already before idx)
    # Skip version/flags (4 bytes)
    count = struct.unpack(">I", data[content_start + 4 : content_start + 8])[0]
    entries = []
    pos = content_start + 8
    for _ in range(count):
        entries.append(struct.unpack(">I", data[pos : pos + 4])[0])
        pos += 4
    return entries


def _parse_co64_entries(data: bytes):
    """Return the list of 64-bit chunk offsets from the first co64 in data."""
    idx = data.find(b"co64")
    assert idx >= 4, "co64 not found"
    content_start = idx + 4
    count = struct.unpack(">I", data[content_start + 4 : content_start + 8])[0]
    entries = []
    pos = content_start + 8
    for _ in range(count):
        entries.append(struct.unpack(">Q", data[pos : pos + 8])[0])
        pos += 8
    return entries


# ---------------------------------------------------------------------------
# _read_box_header — pure function tests
# ---------------------------------------------------------------------------


def test_read_box_header_standard():
    """Read a standard 8-byte box header."""
    data = struct.pack(">I", 100) + b"ftyp" + b"\x00" * 92
    box_type, size, header_size = _read_box_header(data, 0)
    assert box_type == b"ftyp"
    assert size == 100
    assert header_size == 8


def test_read_box_header_extended():
    """Read extended 16-byte box header (size field == 1)."""
    data = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 5_000_000_000)
    box_type, size, header_size = _read_box_header(data, 0)
    assert box_type == b"mdat"
    assert size == 5_000_000_000
    assert header_size == 16


def test_read_box_header_too_short_for_standard():
    """Return None when there are fewer than 8 bytes at offset."""
    data = b"\x00\x00"
    box_type, size, header_size = _read_box_header(data, 0)
    assert box_type is None
    assert size == 0
    assert header_size == 0


def test_read_box_header_too_short_for_extended():
    """Return None when size==1 but fewer than 16 bytes remain."""
    # 8-byte standard header with size==1 but no room for the 8-byte extended size
    data = struct.pack(">I", 1) + b"mdat" + b"\x00\x00"  # only 10 bytes total
    box_type, size, header_size = _read_box_header(data, 0)
    assert box_type is None


def test_read_box_header_at_nonzero_offset():
    """Read a box header at a non-zero offset into the buffer."""
    padding = b"\x00" * 32
    box_data = struct.pack(">I", 50) + b"moov"
    data = padding + box_data
    box_type, size, header_size = _read_box_header(data, 32)
    assert box_type == b"moov"
    assert size == 50
    assert header_size == 8


def test_read_box_header_offset_past_end():
    """Return None when offset is past the end of the buffer."""
    data = struct.pack(">I", 100) + b"ftyp" + b"\x00" * 92
    box_type, size, header_size = _read_box_header(data, 200)
    assert box_type is None


def test_read_box_header_various_types():
    """Parse multiple known box types correctly."""
    for box_name in [b"ftyp", b"moov", b"trak", b"mdat", b"stco", b"co64"]:
        data = struct.pack(">I", 8) + box_name
        box_type, size, _ = _read_box_header(data, 0)
        assert box_type == box_name
        assert size == 8


# ---------------------------------------------------------------------------
# _adjust_moov_offsets — stco adjustments
# ---------------------------------------------------------------------------


def _build_moov_with_stco(*offsets):
    """Build moov > trak > mdia > minf > stbl > stco with given offsets."""
    stco = _make_stco(*offsets)
    return _wrap_containers(stco, b"stbl", b"minf", b"mdia", b"trak", b"moov")


def test_adjust_moov_offsets_stco_single_entry():
    """A single stco entry is increased by delta."""
    moov = _build_moov_with_stco(100)
    adjusted = _adjust_moov_offsets(moov, 500)
    assert _parse_stco_entries(adjusted) == [600]


def test_adjust_moov_offsets_stco_multiple_entries():
    """All stco entries are increased by delta."""
    moov = _build_moov_with_stco(100, 200, 300)
    adjusted = _adjust_moov_offsets(moov, 1000)
    assert _parse_stco_entries(adjusted) == [1100, 1200, 1300]


def test_adjust_moov_offsets_stco_zero_delta():
    """With delta=0, stco entries are unchanged."""
    moov = _build_moov_with_stco(50, 150)
    adjusted = _adjust_moov_offsets(moov, 0)
    assert _parse_stco_entries(adjusted) == [50, 150]


def test_adjust_moov_offsets_stco_large_delta():
    """Large delta values are handled correctly."""
    moov = _build_moov_with_stco(0, 1)
    adjusted = _adjust_moov_offsets(moov, 2**31 - 1)
    entries = _parse_stco_entries(adjusted)
    assert entries[0] == 2**31 - 1
    assert entries[1] == 2**31


# ---------------------------------------------------------------------------
# _adjust_moov_offsets — co64 adjustments
# ---------------------------------------------------------------------------


def _build_moov_with_co64(*offsets):
    """Build moov > trak > mdia > minf > stbl > co64 with given offsets."""
    co64 = _make_co64(*offsets)
    return _wrap_containers(co64, b"stbl", b"minf", b"mdia", b"trak", b"moov")


def test_adjust_moov_offsets_co64_single_entry():
    """A single co64 entry is increased by delta."""
    moov = _build_moov_with_co64(5_000_000_000)
    adjusted = _adjust_moov_offsets(moov, 2000)
    assert _parse_co64_entries(adjusted) == [5_000_002_000]


def test_adjust_moov_offsets_co64_multiple_entries():
    """All co64 entries are increased by delta."""
    moov = _build_moov_with_co64(5_000_000_000, 6_000_000_000)
    adjusted = _adjust_moov_offsets(moov, 1000)
    assert _parse_co64_entries(adjusted) == [5_000_001_000, 6_000_001_000]


def test_adjust_moov_offsets_co64_zero_delta():
    """With delta=0, co64 entries are unchanged."""
    moov = _build_moov_with_co64(9_999_999_999)
    adjusted = _adjust_moov_offsets(moov, 0)
    assert _parse_co64_entries(adjusted) == [9_999_999_999]


# ---------------------------------------------------------------------------
# _adjust_moov_offsets — non-container boxes are not modified
# ---------------------------------------------------------------------------


def test_adjust_moov_offsets_ignores_non_container_body():
    """Non-container boxes (e.g. mvhd) inside moov are left unchanged."""
    mvhd_body = bytes(range(100))
    mvhd = _make_box(b"mvhd", mvhd_body)
    moov = _make_box(b"moov", mvhd)
    adjusted = _adjust_moov_offsets(moov, 500)
    # The mvhd body starts at byte 16 (8 moov header + 8 mvhd header)
    assert adjusted[16:] == mvhd_body


def test_adjust_moov_offsets_returns_bytes():
    """_adjust_moov_offsets always returns bytes, not bytearray."""
    moov = _build_moov_with_stco(100)
    result = _adjust_moov_offsets(moov, 0)
    assert isinstance(result, bytes)


def test_adjust_moov_offsets_does_not_mutate_input():
    """The input bytes object is not modified."""
    moov = _build_moov_with_stco(100)
    original = moov[:]
    _adjust_moov_offsets(moov, 999)
    assert moov == original


# ---------------------------------------------------------------------------
# _adjust_moov_offsets — container recursion
# ---------------------------------------------------------------------------


def test_adjust_moov_offsets_recurses_through_edts():
    """edts is a known container; stco nested inside it should be adjusted."""
    stco = _make_stco(77)
    # Nest inside edts instead of the usual stbl/minf/mdia/trak path
    inner = _wrap_containers(stco, b"edts")
    moov = _make_box(b"moov", inner)
    adjusted = _adjust_moov_offsets(moov, 23)
    assert _parse_stco_entries(adjusted) == [100]


def test_adjust_moov_offsets_recurses_through_udta():
    """udta is a known container; stco nested inside it should be adjusted."""
    stco = _make_stco(50)
    inner = _wrap_containers(stco, b"udta")
    moov = _make_box(b"moov", inner)
    adjusted = _adjust_moov_offsets(moov, 10)
    assert _parse_stco_entries(adjusted) == [60]


# ---------------------------------------------------------------------------
# _StreamHandler._parse_range — pure method tests (no server needed)
# ---------------------------------------------------------------------------


def _make_handler():
    """Return a bare _StreamHandler with no server or socket."""
    return _StreamHandler.__new__(_StreamHandler)


def test_parse_range_standard():
    """Parse a fully-specified byte range."""
    handler = _make_handler()
    start, end = handler._parse_range("bytes=0-999", 10000)
    assert start == 0
    assert end == 999


def test_parse_range_open_ended():
    """Parse an open-ended range (no end byte)."""
    handler = _make_handler()
    start, end = handler._parse_range("bytes=500-", 10000)
    assert start == 500
    assert end == 9999


def test_parse_range_suffix():
    """Parse a suffix range (last N bytes)."""
    handler = _make_handler()
    start, end = handler._parse_range("bytes=-100", 10000)
    assert start == 9900
    assert end == 9999


def test_parse_range_clamps_end_to_content_length():
    """End byte is clamped to content_length - 1 even if header says more."""
    handler = _make_handler()
    start, end = handler._parse_range("bytes=0-99999", 1000)
    assert start == 0
    assert end == 999


def test_parse_range_invalid_no_equals():
    """Non-parseable range header returns (None, None)."""
    handler = _make_handler()
    start, end = handler._parse_range("invalid", 10000)
    assert start is None
    assert end is None


def test_parse_range_invalid_non_numeric():
    """Non-numeric range values return (None, None)."""
    handler = _make_handler()
    start, end = handler._parse_range("bytes=abc-def", 10000)
    assert start is None
    assert end is None


def test_parse_range_zero_start():
    """bytes=0- returns full range from start."""
    handler = _make_handler()
    start, end = handler._parse_range("bytes=0-", 500)
    assert start == 0
    assert end == 499


def test_parse_range_exact_last_byte():
    """bytes=-1 requests only the very last byte."""
    handler = _make_handler()
    start, end = handler._parse_range("bytes=-1", 1000)
    assert start == 999
    assert end == 999


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


def test_detect_content_type_m4v():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    assert sp._detect_content_type("http://host/film.m4v") == "video/mp4"


def test_detect_content_type_avi():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    assert sp._detect_content_type("http://host/film.avi") == "video/x-msvideo"


def test_detect_content_type_unknown_defaults_to_mp4():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    assert sp._detect_content_type("http://host/file.ts") == "video/mp4"


def test_detect_content_type_uppercase_extension():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    # URL lowercased before matching, so MKV should resolve correctly
    assert sp._detect_content_type("http://host/file.MKV") == "video/x-matroska"


# ---------------------------------------------------------------------------
# StreamProxy.start / stop lifecycle
# ---------------------------------------------------------------------------


def test_stream_proxy_start_assigns_port():
    """start() binds to a random port > 0."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy()
    sp.start()
    try:
        assert sp.port > 0
    finally:
        sp.stop()


def test_stream_proxy_stop_clears_server():
    """stop() clears _server and _thread."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy()
    sp.start()
    sp.stop()
    assert sp._server is None
    assert sp._thread is None


def test_stream_proxy_stop_idempotent():
    """Calling stop() on an unstarted proxy does not raise."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy()
    sp.stop()  # should not raise


# ---------------------------------------------------------------------------
# StreamProxy._get_content_length
# ---------------------------------------------------------------------------


def test_get_content_length_from_head():
    """Returns Content-Length from a HEAD response."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.headers.get.return_value = "12345"

    with patch("resources.lib.stream_proxy.urlopen", return_value=mock_resp):
        length = sp._get_content_length("http://host/file.mp4", None)

    assert length == 12345


def test_get_content_length_falls_back_to_range_probe():
    """Falls back to a range probe when HEAD fails, parsing Content-Range."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.headers.get.return_value = "bytes 0-0/99999"

    call_count = [0]

    def fake_urlopen(req, timeout):
        if call_count[0] == 0:
            call_count[0] += 1
            raise OSError("HEAD failed")
        return mock_resp

    with patch("resources.lib.stream_proxy.urlopen", side_effect=fake_urlopen):
        length = sp._get_content_length("http://host/file.mp4", None)

    assert length == 99999


def test_get_content_length_returns_zero_on_complete_failure():
    """Returns 0 when both HEAD and range probe fail."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    with patch(
        "resources.lib.stream_proxy.urlopen", side_effect=Exception("all failed")
    ):
        length = sp._get_content_length("http://host/file.mp4", None)

    assert length == 0


# ---------------------------------------------------------------------------
# StreamProxy._range_read
# ---------------------------------------------------------------------------


def test_range_read_success():
    """_range_read returns the response body on success."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = b"hello"

    with patch("resources.lib.stream_proxy.urlopen", return_value=mock_resp):
        data = sp._range_read("http://host/file.mp4", None, 0, 4)

    assert data == b"hello"


def test_range_read_includes_auth_header():
    """_range_read adds Authorization header when auth_header is provided."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = b"data"

    captured = []

    def fake_urlopen(req, timeout):
        captured.append(req)
        return mock_resp

    with patch("resources.lib.stream_proxy.urlopen", side_effect=fake_urlopen):
        sp._range_read("http://host/file.mp4", "Basic abc123", 0, 99)

    req = captured[0]
    auth_val = req.get_header("Authorization")
    assert auth_val == "Basic abc123"


def test_range_read_returns_none_on_exception():
    """_range_read returns None when urlopen raises."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    with patch(
        "resources.lib.stream_proxy.urlopen", side_effect=Exception("network error")
    ):
        result = sp._range_read("http://host/file.mp4", None, 0, 100)

    assert result is None


# ---------------------------------------------------------------------------
# StreamProxy.prepare_stream — passthrough path
# ---------------------------------------------------------------------------


def _make_stream_proxy_with_server():
    """Return a started StreamProxy."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy()
    sp.start()
    return sp


def test_prepare_stream_passthrough_for_mkv():
    """MKV files use passthrough (faststart=False)."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    with patch.object(sp, "_get_content_length", return_value=100000), patch.object(
        sp, "_detect_content_type", return_value="video/x-matroska"
    ), patch.object(sp, "_prepare_faststart") as mock_fs:
        url = sp.prepare_stream("http://host/film.mkv")

    mock_fs.assert_not_called()
    assert url == "http://127.0.0.1:9999/stream"
    ctx = sp._server.stream_context
    assert ctx["faststart"] is False


def test_prepare_stream_skips_faststart_for_mp4():
    """MP4 files no longer trigger faststart — Kodi seeks to moov natively."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    with patch.object(sp, "_get_content_length", return_value=500000), patch.object(
        sp, "_detect_content_type", return_value="video/mp4"
    ), patch.object(sp, "_prepare_faststart") as mock_fs:
        sp.prepare_stream("http://host/film.mp4")

    mock_fs.assert_not_called()


def test_prepare_stream_skips_faststart_when_content_length_zero():
    """No faststart attempt when content_length is 0."""
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    with patch.object(sp, "_get_content_length", return_value=0), patch.object(
        sp, "_detect_content_type", return_value="video/mp4"
    ), patch.object(sp, "_prepare_faststart") as mock_fs:
        sp.prepare_stream("http://host/film.mp4")

    mock_fs.assert_not_called()
