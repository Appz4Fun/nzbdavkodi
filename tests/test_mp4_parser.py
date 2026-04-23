# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for mp4_parser.py -- MP4 box header parsing."""

import struct
from unittest.mock import MagicMock, patch


def test_read_box_header_standard():
    """Standard 8-byte box header: 4-byte size + 4-byte type."""
    from resources.lib.mp4_parser import read_box_header

    # Build a box: size=100, type='ftyp'
    data = struct.pack(">I", 100) + b"ftyp" + b"\x00" * 92
    box_type, header_size, total_size = read_box_header(data, 0)
    assert box_type == b"ftyp"
    assert header_size == 8
    assert total_size == 100


def test_read_box_header_extended_size():
    """Extended 16-byte header: size=1, then 8-byte extended size."""
    from resources.lib.mp4_parser import read_box_header

    data = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 5000000000)
    box_type, header_size, total_size = read_box_header(data, 0)
    assert box_type == b"mdat"
    assert header_size == 16
    assert total_size == 5000000000


def test_read_box_header_too_short():
    """Not enough data returns None."""
    from resources.lib.mp4_parser import read_box_header

    assert read_box_header(b"\x00\x00", 0) is None


def test_scan_top_level_boxes_finds_ftyp_mdat_moov():
    """Scan a synthetic MP4 with ftyp + mdat + moov layout."""
    from resources.lib.mp4_parser import scan_top_level_boxes

    ftyp = struct.pack(">I", 32) + b"ftyp" + b"\x00" * 24
    mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 1000) + b"\x00" * 984
    moov = struct.pack(">I", 200) + b"moov" + b"\x00" * 192

    data = ftyp + mdat + moov
    result = scan_top_level_boxes(data)

    assert result["ftyp_offset"] == 0
    assert result["ftyp_size"] == 32
    assert result["mdat_offset"] == 32
    assert result["mdat_size"] == 1000
    assert result["moov_offset"] == 1032
    assert result["moov_size"] == 200


def test_scan_top_level_boxes_moov_before_mdat():
    """Handle faststart layout (moov before mdat)."""
    from resources.lib.mp4_parser import scan_top_level_boxes

    ftyp = struct.pack(">I", 16) + b"ftyp" + b"\x00" * 8
    moov = struct.pack(">I", 100) + b"moov" + b"\x00" * 92
    mdat = struct.pack(">I", 500) + b"mdat" + b"\x00" * 492

    data = ftyp + moov + mdat
    result = scan_top_level_boxes(data)

    assert result["ftyp_offset"] == 0
    assert result["moov_offset"] == 16
    assert result["mdat_offset"] == 116
    assert result["moov_before_mdat"] is True


def test_scan_top_level_boxes_with_free_atoms():
    """Handle files with free/wide atoms between ftyp and mdat."""
    from resources.lib.mp4_parser import scan_top_level_boxes

    ftyp = struct.pack(">I", 16) + b"ftyp" + b"\x00" * 8
    free = struct.pack(">I", 24) + b"free" + b"\x00" * 16
    mdat = struct.pack(">I", 500) + b"mdat" + b"\x00" * 492
    moov = struct.pack(">I", 100) + b"moov" + b"\x00" * 92

    data = ftyp + free + mdat + moov
    result = scan_top_level_boxes(data)

    assert result["ftyp_offset"] == 0
    assert result["ftyp_size"] == 16
    assert result["mdat_offset"] == 40
    assert result["moov_offset"] == 540
    assert result["moov_before_mdat"] is False
    # Other atoms tracked for virtual layout
    assert len(result["other_atoms"]) == 1
    assert result["other_atoms"][0] == (16, 24, b"free")


def test_rewrite_stco_offsets():
    """Rewrite 32-bit chunk offsets in a moov atom."""
    from resources.lib.mp4_parser import rewrite_moov_offsets

    # Build minimal moov > trak > mdia > minf > stbl > stco
    # stco: version(1) + flags(3) + entry_count(4) + offsets(4 each)
    stco_body = struct.pack(">I", 0) + struct.pack(">I", 3)  # version+flags, count=3
    stco_body += struct.pack(">III", 1000, 2000, 3000)  # 3 offsets
    stco = struct.pack(">I", 8 + len(stco_body)) + b"stco" + stco_body

    stbl = struct.pack(">I", 8 + len(stco)) + b"stbl" + stco
    minf = struct.pack(">I", 8 + len(stbl)) + b"minf" + stbl
    mdia = struct.pack(">I", 8 + len(minf)) + b"mdia" + minf
    trak = struct.pack(">I", 8 + len(mdia)) + b"trak" + mdia
    moov_body = trak
    moov = struct.pack(">I", 8 + len(moov_body)) + b"moov" + moov_body

    result = rewrite_moov_offsets(moov, 500)

    stco_start = result.index(b"stco")
    off_start = stco_start + 4 + 4 + 4  # type + version+flags + count
    o1 = struct.unpack_from(">I", result, off_start)[0]
    o2 = struct.unpack_from(">I", result, off_start + 4)[0]
    o3 = struct.unpack_from(">I", result, off_start + 8)[0]
    assert o1 == 1500
    assert o2 == 2500
    assert o3 == 3500


def test_rewrite_co64_offsets():
    """Rewrite 64-bit chunk offsets in a moov atom."""
    from resources.lib.mp4_parser import rewrite_moov_offsets

    co64_body = struct.pack(">I", 0) + struct.pack(">I", 2)
    co64_body += struct.pack(">QQ", 5000000000, 6000000000)
    co64 = struct.pack(">I", 8 + len(co64_body)) + b"co64" + co64_body

    stbl = struct.pack(">I", 8 + len(co64)) + b"stbl" + co64
    minf = struct.pack(">I", 8 + len(stbl)) + b"minf" + stbl
    mdia = struct.pack(">I", 8 + len(minf)) + b"mdia" + minf
    trak = struct.pack(">I", 8 + len(mdia)) + b"trak" + mdia
    moov = struct.pack(">I", 8 + len(trak)) + b"moov" + trak

    result = rewrite_moov_offsets(moov, 1000)

    co64_start = result.index(b"co64")
    off_start = co64_start + 4 + 4 + 4
    o1 = struct.unpack_from(">Q", result, off_start)[0]
    o2 = struct.unpack_from(">Q", result, off_start + 8)[0]
    assert o1 == 5000001000
    assert o2 == 6000001000


def test_rewrite_stco_overflow_returns_none():
    """Rewriting stco that would overflow 32-bit returns None."""
    from resources.lib.mp4_parser import rewrite_moov_offsets

    stco_body = struct.pack(">I", 0) + struct.pack(">I", 1)
    stco_body += struct.pack(">I", 4294967000)  # near 2^32 limit
    stco = struct.pack(">I", 8 + len(stco_body)) + b"stco" + stco_body

    stbl = struct.pack(">I", 8 + len(stco)) + b"stbl" + stco
    minf = struct.pack(">I", 8 + len(stbl)) + b"minf" + stbl
    mdia = struct.pack(">I", 8 + len(minf)) + b"mdia" + minf
    trak = struct.pack(">I", 8 + len(mdia)) + b"trak" + mdia
    moov = struct.pack(">I", 8 + len(trak)) + b"moov" + trak

    result = rewrite_moov_offsets(moov, 1000)
    assert result is None  # overflow — caller should use fallback


def _make_mock_response(data, status=200, headers=None):
    """Create a mock HTTP response."""
    resp = MagicMock()
    resp.read.return_value = data
    resp.getcode.return_value = status
    resp.headers = headers or {}
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_fetch_moov_from_tail():
    """Fetch moov from the end of a remote MP4 file."""
    from resources.lib.mp4_parser import fetch_remote_mp4_layout

    ftyp = struct.pack(">I", 32) + b"ftyp" + b"\x00" * 24
    mdat = struct.pack(">I", 200) + b"mdat" + b"\x00" * 192
    moov = struct.pack(">I", 100) + b"moov" + b"\x00" * 92
    full_file = ftyp + mdat + moov

    file_size = len(full_file)

    def mock_urlopen(req, timeout=None):
        range_header = req.get_header("Range") or ""
        if range_header.startswith("bytes="):
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else file_size - 1
            return _make_mock_response(full_file[start : end + 1])
        return _make_mock_response(full_file)

    with patch("resources.lib.mp4_parser.urlopen", side_effect=mock_urlopen):
        layout = fetch_remote_mp4_layout(
            "http://host/file.mp4", file_size, auth_header=None
        )

    assert layout is not None
    assert layout["ftyp_data"] == ftyp
    assert layout["moov_data"] is not None
    assert len(layout["moov_data"]) == 100
    assert layout["mdat_offset"] == 32
    assert layout["moov_before_mdat"] is False
    assert layout["ftyp_end"] == 32
    assert layout["original_moov_offset"] == 232


def test_fetch_moov_already_faststart():
    """Moov at front should be detected from head probe."""
    from resources.lib.mp4_parser import fetch_remote_mp4_layout

    ftyp = struct.pack(">I", 16) + b"ftyp" + b"\x00" * 8
    moov = struct.pack(">I", 100) + b"moov" + b"\x00" * 92
    mdat = struct.pack(">I", 500) + b"mdat" + b"\x00" * 492
    full_file = ftyp + moov + mdat

    file_size = len(full_file)

    def mock_urlopen(req, timeout=None):
        range_header = req.get_header("Range") or ""
        parts = range_header.replace("bytes=", "").split("-")
        start = int(parts[0])
        end = int(parts[1]) if parts[1] else file_size - 1
        return _make_mock_response(full_file[start : end + 1])

    with patch("resources.lib.mp4_parser.urlopen", side_effect=mock_urlopen):
        layout = fetch_remote_mp4_layout(
            "http://host/file.mp4", file_size, auth_header=None
        )

    assert layout is not None
    assert layout["moov_before_mdat"] is True


def test_build_faststart_layout_moov_at_end():
    """Build virtual faststart layout with rewritten offsets."""
    from resources.lib.mp4_parser import build_faststart_layout

    ftyp = struct.pack(">I", 32) + b"ftyp" + b"\x00" * 24

    # Build moov with stco offsets pointing into mdat (original mdat at offset 32)
    stco_body = struct.pack(">I", 0) + struct.pack(">I", 2)
    stco_body += struct.pack(">II", 40, 100)  # offsets into mdat region
    stco = struct.pack(">I", 8 + len(stco_body)) + b"stco" + stco_body
    stbl = struct.pack(">I", 8 + len(stco)) + b"stbl" + stco
    minf = struct.pack(">I", 8 + len(stbl)) + b"minf" + stbl
    mdia = struct.pack(">I", 8 + len(minf)) + b"mdia" + minf
    trak = struct.pack(">I", 8 + len(mdia)) + b"trak" + mdia
    moov = struct.pack(">I", 8 + len(trak)) + b"moov" + trak

    moov_size = len(moov)

    layout_info = {
        "ftyp_data": ftyp,
        "ftyp_end": 32,
        "moov_data": moov,
        "mdat_offset": 32,
        "original_moov_offset": 1032,
        "moov_before_mdat": False,
    }

    layout = build_faststart_layout(layout_info)

    assert layout is not None
    # Virtual = ftyp(32) + moov(moov_size) + payload(1032-32=1000)
    assert layout["virtual_size"] == 32 + moov_size + 1000
    # Header = ftyp + rewritten moov
    assert len(layout["header_data"]) == 32 + moov_size
    # Payload maps to original[ftyp_end:moov_start]
    assert layout["payload_remote_start"] == 32  # ftyp_end
    assert layout["payload_remote_end"] == 1032  # original_moov_offset
    assert layout["payload_size"] == 1000

    # Verify moov offsets were adjusted by delta = moov_size
    header = layout["header_data"]
    stco_pos = header.index(b"stco")
    off_start = stco_pos + 4 + 4 + 4
    o1 = struct.unpack_from(">I", header, off_start)[0]
    o2 = struct.unpack_from(">I", header, off_start + 4)[0]
    assert o1 == 40 + moov_size  # original 40 + delta
    assert o2 == 100 + moov_size


def test_build_faststart_layout_already_faststart():
    """Already-faststart file returns passthrough layout."""
    from resources.lib.mp4_parser import build_faststart_layout

    ftyp = struct.pack(">I", 16) + b"ftyp" + b"\x00" * 8
    moov = struct.pack(">I", 50) + b"moov" + b"\x00" * 42

    layout_info = {
        "ftyp_data": ftyp,
        "ftyp_end": 16,
        "moov_data": moov,
        "mdat_offset": 66,
        "original_moov_offset": 16,
        "moov_before_mdat": True,
    }

    layout = build_faststart_layout(layout_info)

    assert layout is not None
    # For faststart files, we just pass through — no rewriting needed
    # But we still provide the layout for consistent proxy behavior
    assert layout["header_data"] == ftyp + moov
    assert layout["payload_remote_start"] == 66  # after moov
    assert layout["payload_remote_end"] > 66


def test_build_faststart_layout_stco_overflow_returns_none():
    """Layout returns None when stco overflow is detected."""
    from resources.lib.mp4_parser import build_faststart_layout

    ftyp = struct.pack(">I", 16) + b"ftyp" + b"\x00" * 8

    stco_body = struct.pack(">I", 0) + struct.pack(">I", 1)
    stco_body += struct.pack(">I", 0xFFFFFFFF)  # exactly at 2^32 - 1
    stco = struct.pack(">I", 8 + len(stco_body)) + b"stco" + stco_body
    stbl = struct.pack(">I", 8 + len(stco)) + b"stbl" + stco
    minf = struct.pack(">I", 8 + len(stbl)) + b"minf" + stbl
    mdia = struct.pack(">I", 8 + len(minf)) + b"mdia" + minf
    trak = struct.pack(">I", 8 + len(mdia)) + b"trak" + mdia
    moov = struct.pack(">I", 8 + len(trak)) + b"moov" + trak

    layout_info = {
        "ftyp_data": ftyp,
        "ftyp_end": 16,
        "moov_data": moov,
        "mdat_offset": 16,
        "original_moov_offset": 1016,
        "moov_before_mdat": False,
    }

    # delta = moov_size, and 0xFFFFFFFF + moov_size > 0xFFFFFFFF → overflow
    layout = build_faststart_layout(layout_info)
    assert layout is None  # stco overflow — caller uses fallback


def test_build_faststart_layout_with_free_atoms():
    """Layout preserves free/wide atoms between ftyp and mdat."""
    from resources.lib.mp4_parser import build_faststart_layout

    ftyp = struct.pack(">I", 16) + b"ftyp" + b"\x00" * 8
    # In original: ftyp(0..16) + free(16..40) + mdat(40..540) + moov(540..640)
    # ftyp_end=16, moov_start=540, payload=original[16:540]=524 bytes

    stco_body = struct.pack(">I", 0) + struct.pack(">I", 1)
    stco_body += struct.pack(">I", 48)  # offset into mdat data
    stco = struct.pack(">I", 8 + len(stco_body)) + b"stco" + stco_body
    stbl = struct.pack(">I", 8 + len(stco)) + b"stbl" + stco
    minf = struct.pack(">I", 8 + len(stbl)) + b"minf" + stbl
    mdia = struct.pack(">I", 8 + len(minf)) + b"mdia" + minf
    trak = struct.pack(">I", 8 + len(mdia)) + b"trak" + mdia
    moov = struct.pack(">I", 8 + len(trak)) + b"moov" + trak
    moov_size = len(moov)

    layout_info = {
        "ftyp_data": ftyp,
        "ftyp_end": 16,
        "moov_data": moov,
        "mdat_offset": 40,
        "original_moov_offset": 540,
        "moov_before_mdat": False,
    }

    layout = build_faststart_layout(layout_info)

    assert layout is not None
    # payload = original[16:540] = 524 bytes (includes free + mdat)
    assert layout["payload_size"] == 524
    assert layout["payload_remote_start"] == 16
    assert layout["payload_remote_end"] == 540
    # virtual = ftyp(16) + moov(moov_size) + 524
    assert layout["virtual_size"] == 16 + moov_size + 524

    # stco offset adjusted by moov_size
    header = layout["header_data"]
    stco_pos = header.index(b"stco")
    off_start = stco_pos + 4 + 4 + 4
    o1 = struct.unpack_from(">I", header, off_start)[0]
    assert o1 == 48 + moov_size


def test_range_cache_stores_and_retrieves():
    """Cache stores fetched ranges and serves overlapping requests."""
    from resources.lib.mp4_parser import RangeCache

    cache = RangeCache(max_bytes=1048576)
    cache.put(100, b"hello world")
    assert cache.get(100, 111) == b"hello world"
    assert cache.get(105, 111) == b" world"
    assert cache.get(100, 105) == b"hello"


def test_range_cache_miss_returns_none():
    """Cache returns None for ranges not stored."""
    from resources.lib.mp4_parser import RangeCache

    cache = RangeCache(max_bytes=1048576)
    assert cache.get(0, 100) is None


def test_range_cache_evicts_oldest():
    """Cache evicts oldest entries when over max_bytes."""
    from resources.lib.mp4_parser import RangeCache

    cache = RangeCache(max_bytes=20)
    cache.put(0, b"a" * 15)
    cache.put(100, b"b" * 10)  # triggers eviction of first entry
    assert cache.get(0, 15) is None
    assert cache.get(100, 110) == b"b" * 10


def test_range_cache_survives_concurrent_put_get_churn():
    import threading

    from resources.lib.mp4_parser import RangeCache

    cache = RangeCache(max_bytes=256)
    errors = []

    def worker(offset):
        try:
            payload = bytes([offset % 256]) * 32
            for _ in range(50):
                cache.put(offset, payload)
                cached = cache.get(offset, offset + len(payload))
                if cached not in (None, payload):
                    errors.append((offset, cached))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(idx * 32,)) for idx in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_range_cache_churn_keeps_recently_touched_entry_hot():
    from resources.lib.mp4_parser import RangeCache

    cache = RangeCache(max_bytes=96)
    cache.put(0, b"a" * 32)
    cache.put(32, b"b" * 32)
    assert cache.get(0, 32) == b"a" * 32  # move first entry to MRU position
    cache.put(64, b"c" * 32)
    cache.put(96, b"d" * 32)  # eviction should drop the older "b" entry first

    assert cache.get(0, 32) == b"a" * 32
    assert cache.get(32, 64) is None
    assert cache.get(64, 96) == b"c" * 32
    assert cache.get(96, 128) == b"d" * 32
