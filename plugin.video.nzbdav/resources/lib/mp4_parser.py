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


# Box types that are not ftyp/moov/mdat but may appear at the top level
_KNOWN_PASSTHROUGH = {b"free", b"wide", b"uuid", b"skip", b"pdin"}


def scan_top_level_boxes(data):
    """Scan top-level MP4 boxes and return their locations.

    Returns a dict with keys: ftyp_offset, ftyp_size, moov_offset,
    moov_size, mdat_offset, mdat_size, moov_before_mdat, other_atoms.
    other_atoms is a list of (offset, size, type) for non-ftyp/moov/mdat boxes.
    Missing boxes have offset/size of -1/0.
    """
    result = {
        "ftyp_offset": -1,
        "ftyp_size": 0,
        "moov_offset": -1,
        "moov_size": 0,
        "mdat_offset": -1,
        "mdat_size": 0,
        "moov_before_mdat": False,
        "other_atoms": [],
    }
    offset = 0
    while offset < len(data):
        parsed = read_box_header(data, offset)
        if parsed is None:
            break
        box_type, header_size, total_size = parsed
        if box_type == b"ftyp":
            result["ftyp_offset"] = offset
            result["ftyp_size"] = total_size
        elif box_type == b"moov":
            result["moov_offset"] = offset
            result["moov_size"] = total_size
        elif box_type == b"mdat":
            result["mdat_offset"] = offset
            result["mdat_size"] = total_size
        elif box_type in _KNOWN_PASSTHROUGH:
            result["other_atoms"].append((offset, total_size, box_type))
        if total_size < 8:
            break
        offset += total_size

    if result["moov_offset"] >= 0 and result["mdat_offset"] >= 0:
        result["moov_before_mdat"] = result["moov_offset"] < result["mdat_offset"]
    return result
