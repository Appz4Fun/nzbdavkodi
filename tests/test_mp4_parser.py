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
