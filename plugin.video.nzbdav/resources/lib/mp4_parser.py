# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Pure-Python MP4 box parser for moov-relocation proxy.

Parses MP4 top-level boxes to locate ftyp, moov, and mdat atoms.
Rewrites stco/co64 chunk offsets in the moov for virtual faststart
layout (moov-before-mdat) without modifying the original file.
"""

import struct


def read_box_header(data, offset):
    """Read an MP4 box header at the given offset.

    Returns (box_type, header_size, total_size) or None if not enough data.
    box_type is a 4-byte bytes object (e.g. b'ftyp').
    """
    if offset + 8 > len(data):
        return None
    size = struct.unpack_from(">I", data, offset)[0]
    box_type = data[offset + 4 : offset + 8]
    header_size = 8
    if size == 1:
        # Extended 64-bit size
        if offset + 16 > len(data):
            return None
        size = struct.unpack_from(">Q", data, offset + 8)[0]
        header_size = 16
    elif size == 0:
        # Box extends to end of data
        size = len(data) - offset
    return box_type, header_size, size
