# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for mp4_parser.py -- MP4 box header parsing."""

import struct


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
