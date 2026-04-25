# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Pure-Python MP4 box parser for moov-relocation proxy.

Parses MP4 top-level boxes to locate ftyp, moov, and mdat atoms.
Rewrites stco/co64 chunk offsets in the moov for virtual faststart
layout (moov-before-mdat) without modifying the original file.
"""

import struct
import threading
from collections import OrderedDict
from urllib.request import Request, urlopen


def read_box_header(data, offset):
    """Read an MP4 box header at the given offset.

    Accepts any buffer (bytes, bytearray, memoryview) and always returns
    box_type as immutable, hashable ``bytes`` so callers can use it in
    ``set`` / ``dict`` lookups regardless of the input buffer's type.
    Closes TODO.md §H.2-H3b — eliminates per-call ``bytes(data)`` full
    buffer copies that the recursive walker used to do just to make box
    types hashable.

    Returns (box_type, header_size, total_size) or None if not enough data.
    """
    if offset + 8 > len(data):
        return None
    size = struct.unpack_from(">I", data, offset)[0]
    box_type = bytes(data[offset + 4 : offset + 8])
    header_size = 8
    if size == 1:
        # Extended 64-bit size.
        if offset + 16 > len(data):
            return None
        size = struct.unpack_from(">Q", data, offset + 8)[0]
        header_size = 16
        # Extended-size header is 16 bytes, so any payload requires
        # size >= 16. Smaller values are malformed.
        if size < 16:
            return None
    elif size == 0:
        # Box extends to end of data. Spec only allows this for mdat;
        # refuse it for other box types so we don't silently consume the
        # tail of the file as a non-mdat box.
        if box_type != b"mdat":
            return None
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
        # Reject malformed boxes (size smaller than the header itself, or
        # size==0 on a non-mdat box) BEFORE storing any offsets. Storing
        # then breaking left stale offsets pointing at truncated data.
        if total_size < 8:
            break
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
        offset += total_size

    if result["moov_offset"] >= 0 and result["mdat_offset"] >= 0:
        result["moov_before_mdat"] = result["moov_offset"] < result["mdat_offset"]
    return result


_MAX_STCO_OFFSET = 0xFFFFFFFF  # 2^32 - 1


def _bounded_chunk_count(count, body_start, body_end, entry_size):
    """Bound a stco/co64 entry count by the actual box body size.

    The on-wire `count` field is an attacker-controlled uint32. A
    `count = 0xFFFFFFFF` would otherwise produce a 4-billion-iteration
    loop in `_rewrite_stco` / `_rewrite_co64`, hanging the proxy.
    Clamp to whatever the box body can actually fit, plus return the
    clamped value so the caller doesn't have to recompute.
    Closes TODO.md §H.2-H3a.
    """
    body_remaining = max(0, body_end - (body_start + 8))  # 4 ver+flags + 4 count
    max_entries = body_remaining // entry_size
    return min(count, max_entries)


def _rewrite_stco(data, body_start, body_end, delta):
    """Rewrite 32-bit chunk offsets in an stco box. Returns False on overflow."""
    count_off = body_start + 4  # skip version+flags
    if count_off + 4 > body_end:
        return True
    count = struct.unpack_from(">I", data, count_off)[0]
    count = _bounded_chunk_count(count, body_start, body_end, 4)
    entry_off = count_off + 4
    for i in range(count):
        pos = entry_off + i * 4
        if pos + 4 > body_end:
            break
        old = struct.unpack_from(">I", data, pos)[0]
        new_val = old + delta
        # Python ints don't overflow, so the only real check is against the
        # uint32 ceiling. A negative delta that would push below 0 also
        # matters though — struct.pack_into >I on a negative int would
        # raise, so guard explicitly.
        if new_val > _MAX_STCO_OFFSET or new_val < 0:
            return False
        struct.pack_into(">I", data, pos, new_val)
    return True


def _rewrite_co64(data, body_start, body_end, delta):
    """Rewrite 64-bit chunk offsets in a co64 box.

    Returns True on success, False if the box is structurally invalid
    (truncated header). Mirrors the `_rewrite_stco` contract — the
    earlier silent-success behavior meant a malformed co64 made the
    whole rewrite report success while leaving offsets unchanged for
    that track. Closes TODO.md §H.3.
    """
    count_off = body_start + 4
    if count_off + 4 > body_end:
        return False
    count = struct.unpack_from(">I", data, count_off)[0]
    # Clamp to the actual body size — same DoS guard as _rewrite_stco.
    count = _bounded_chunk_count(count, body_start, body_end, 8)
    entry_off = count_off + 4
    for i in range(count):
        pos = entry_off + i * 8
        if pos + 8 > body_end:
            return False
        old = struct.unpack_from(">Q", data, pos)[0]
        struct.pack_into(">Q", data, pos, old + delta)
    return True


_CONTAINER_BOXES = frozenset(
    {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta"}
)


def _rewrite_offsets_recursive(data, delta, start=0, end=None):
    """Walk MP4 box tree, rewriting stco/co64 chunk offsets by delta.

    Operates on a mutable bytearray in-place using ``[start, end)``
    bounds rather than slicing a child buffer per container box. The
    previous slice-and-assign-back pattern was O(N²) in moov size — at
    each container level the body bytes were copied out, recursed into,
    then copied back, which on a deeply-nested 50 MB moov hung the
    proxy. Closes TODO.md §H.2-H3b.

    Returns True on success, False if stco overflow detected.
    """
    if end is None:
        end = len(data)
    offset = start
    while offset + 8 <= end:
        parsed = read_box_header(data, offset)
        if parsed is None:
            break
        box_type, header_size, total_size = parsed
        if total_size < 8:
            break
        # Bounds-check the box against the current parent's end (not the
        # whole buffer). A malformed file could declare a child whose
        # ``total_size`` extends past the parent container; without this
        # guard the walker would read sibling boxes' bytes as if they
        # were children. Closes TODO.md §H.2-H3c.
        if offset + total_size > end:
            break

        body_start = offset + header_size
        body_end = offset + total_size

        if box_type == b"stco":
            if not _rewrite_stco(data, body_start, body_end, delta):
                return False
        elif box_type == b"co64":
            if not _rewrite_co64(data, body_start, body_end, delta):
                return False
        elif box_type in _CONTAINER_BOXES:
            if not _rewrite_offsets_recursive(data, delta, body_start, body_end):
                return False

        offset += total_size

    return True


def rewrite_moov_offsets(moov_bytes, delta):
    """Rewrite all stco/co64 chunk offsets in a moov atom by delta.

    Args:
        moov_bytes: Raw bytes of the complete moov box (including header).
        delta: Integer to add to every chunk offset. Positive when moving
               moov before mdat (offsets increase by moov_size).

    Returns:
        New bytes with adjusted offsets, or None if stco overflow detected
        (caller should fall back to temp-file faststart or MKV remux).
    """
    data = bytearray(moov_bytes)
    parsed = read_box_header(data, 0)
    if parsed is None:
        return None
    _, header_size, _ = parsed
    if not _rewrite_offsets_recursive(data, delta, header_size, len(data)):
        return None
    return bytes(data)


_HEAD_PROBE_SIZE = 65536  # 64 KB — enough to find ftyp and moov-at-front
_TAIL_PROBE_SIZE = 524288  # 512 KB — initial tail probe for moov-at-end
_TAIL_PROBE_MAX = 8 * 1048576  # 8 MB — max tail probe before giving up
_MAX_MOOV_SIZE = 50 * 1048576  # 50 MB — safety cap for moov fetch


def _http_range(url, start, end, auth_header=None):
    """Fetch a byte range from a URL. Returns bytes."""
    req = Request(url)
    req.add_header("Range", "bytes={}-{}".format(start, end))
    if auth_header:
        req.add_header("Authorization", auth_header)
    # nosemgrep
    with urlopen(  # nosec B310 — URL from user-configured WebDAV
        req, timeout=30
    ) as resp:
        return resp.read()


def _fetch_and_validate_moov(url, moov_offset, moov_size, auth_header):
    """Fetch a moov box and validate it. Returns bytes or None."""
    if moov_size > _MAX_MOOV_SIZE or moov_size < 8:
        return None
    moov_data = _http_range(url, moov_offset, moov_offset + moov_size - 1, auth_header)
    verify = read_box_header(moov_data, 0)
    if verify is None or verify[0] != b"moov":
        return None
    return moov_data


def _make_layout(ftyp_data, ftyp_end, moov_data, mdat_offset, moov_offset, faststart):
    """Build the layout result dict."""
    return {
        "ftyp_data": ftyp_data,
        "ftyp_end": ftyp_end,
        "moov_data": moov_data,
        "mdat_offset": mdat_offset,
        "original_moov_offset": moov_offset,
        "moov_before_mdat": faststart,
    }


def _find_moov_after_mdat(url, file_size, mdat_offset, mdat_size, auth_header):
    """Find moov right after mdat using computed offset."""
    if mdat_offset < 0 or mdat_size <= 0:
        return None
    mdat_end = mdat_offset + mdat_size
    if mdat_end >= file_size:
        return None
    probe = _http_range(url, mdat_end, min(mdat_end + 15, file_size - 1), auth_header)
    hdr = read_box_header(probe, 0)
    if hdr is None or hdr[0] != b"moov":
        return None
    moov_size = hdr[2]
    if mdat_end + moov_size > file_size:
        return None
    return mdat_end, moov_size


def _find_moov_by_tail_probe(url, file_size, auth_header):
    """Find moov via progressive tail probing. Returns (offset, size) or None."""
    tail_probe_size = _TAIL_PROBE_SIZE
    while tail_probe_size <= _TAIL_PROBE_MAX:
        tail_start = max(0, file_size - tail_probe_size)
        tail_data = _http_range(url, tail_start, file_size - 1, auth_header)
        tail_layout = scan_top_level_boxes(tail_data)

        if tail_layout["moov_offset"] >= 0:
            moov_abs = tail_start + tail_layout["moov_offset"]
            moov_size = tail_layout["moov_size"]
            if moov_abs + moov_size <= file_size:
                return moov_abs, moov_size
            return None

        tail_probe_size *= 2
    return None


def fetch_remote_mp4_layout(url, file_size, auth_header=None):
    """Fetch MP4 layout info from a remote file using HTTP range requests.

    Fetches the file header to get ftyp, then locates moov either by
    computing its position from mdat size or by progressive tail probing.

    Args:
        url: Remote HTTP URL of the MP4 file.
        file_size: Total file size in bytes.
        auth_header: Optional 'Basic xxx' auth header string.

    Returns:
        Dict with keys: ftyp_data, ftyp_end, moov_data, mdat_offset,
        original_moov_offset, moov_before_mdat. Or None on failure.
    """
    # Reject empty/negative file sizes before they turn into malformed
    # ``Range: bytes=0--1`` headers downstream. Closes TODO.md §H.2-H3f.
    if file_size <= 0:
        return None
    # 1. Fetch the first 64KB to find ftyp and check for moov-at-front
    head_size = min(_HEAD_PROBE_SIZE, file_size)
    head_data = _http_range(url, 0, head_size - 1, auth_header)
    head_layout = scan_top_level_boxes(head_data)

    ftyp_data = b""
    ftyp_end = 0
    if head_layout["ftyp_offset"] >= 0:
        ftyp_end = head_layout["ftyp_offset"] + head_layout["ftyp_size"]
        if ftyp_end <= len(head_data):
            ftyp_data = head_data[head_layout["ftyp_offset"] : ftyp_end]

    # Check if moov is already at the front (faststart)
    if head_layout["moov_offset"] >= 0 and head_layout["moov_before_mdat"]:
        moov_offset = head_layout["moov_offset"]
        moov_size = head_layout["moov_size"]
        if moov_offset + moov_size <= len(head_data):
            moov_data = head_data[moov_offset : moov_offset + moov_size]
        else:
            moov_data = _fetch_and_validate_moov(
                url, moov_offset, moov_size, auth_header
            )
            if moov_data is None:
                return None
        return _make_layout(
            ftyp_data,
            ftyp_end,
            moov_data,
            head_layout["mdat_offset"],
            moov_offset,
            True,
        )

    # 2. Moov not in head — try computed location, then tail probe
    mdat_offset = head_layout["mdat_offset"]
    result = _find_moov_after_mdat(
        url, file_size, mdat_offset, head_layout["mdat_size"], auth_header
    )
    if result is None:
        result = _find_moov_by_tail_probe(url, file_size, auth_header)
    if result is None:
        return None

    moov_abs_offset, moov_size = result

    # Validate moov range doesn't overlap mdat. ``_find_moov_after_mdat``
    # places moov at ``mdat_offset + mdat_size`` so it's safe by
    # construction, but the tail-probe fallback could discover a forged
    # ``moov`` header inside mdat content. Faststart layout would then
    # serve mdat bytes as if they were moov, with corrupt offsets.
    # Closes TODO.md §H.2-H3e.
    if mdat_offset >= 0 and head_layout["mdat_size"] > 0:
        mdat_end = mdat_offset + head_layout["mdat_size"]
        moov_end = moov_abs_offset + moov_size
        if moov_abs_offset < mdat_end and mdat_offset < moov_end:
            return None

    moov_data = _fetch_and_validate_moov(url, moov_abs_offset, moov_size, auth_header)
    if moov_data is None:
        return None

    if mdat_offset < 0:
        mdat_offset = ftyp_end

    return _make_layout(
        ftyp_data,
        ftyp_end,
        moov_data,
        mdat_offset,
        moov_abs_offset,
        False,
    )


def build_faststart_layout(layout_info):
    """Build virtual faststart layout from fetched MP4 layout info.

    Takes the output of fetch_remote_mp4_layout() and produces a virtual
    file layout where moov comes right after ftyp, with rewritten chunk
    offsets. All atoms between ftyp and moov in the original file
    (free, wide, uuid, mdat, etc.) are preserved in their original order
    as the "payload" region.

    Virtual layout:
        [ftyp][rewritten moov][original bytes from ftyp_end to moov_start]

    Range mapping for payload region:
        remote_offset = payload_remote_start + (virtual_offset - header_len)

    Returns dict with:
        header_data: bytes (ftyp + rewritten moov) to serve first
        virtual_size: total virtual file size
        payload_remote_start: first byte in original file for payload region
        payload_remote_end: last byte + 1 in original file for payload region
        payload_size: payload_remote_end - payload_remote_start
    Or None if offset rewriting fails (stco overflow).
    """
    ftyp_data = layout_info["ftyp_data"]
    moov_data = layout_info["moov_data"]
    ftyp_end = layout_info["ftyp_end"]
    original_moov_offset = layout_info["original_moov_offset"]

    if layout_info["moov_before_mdat"]:
        # Already faststart — serve ftyp + moov as header, rest as payload.
        # moov is right after ftyp, so payload starts after moov.
        moov_end = original_moov_offset + len(moov_data)
        header_data = ftyp_data + moov_data
        # payload_remote_start = moov_end (first byte after moov in original)
        # payload_remote_end is unknown without file_size; use moov_end + 1 as
        # a sentinel so callers can detect "extends to EOF" and substitute file_size.
        return {
            "header_data": header_data,
            "virtual_size": -1,  # caller must use file_size for this case
            "payload_remote_start": moov_end,
            "payload_remote_end": moov_end + 1,  # sentinel for EOF
            "payload_size": -1,
            "already_faststart": True,
        }

    # Moov is after mdat — need to rewrite offsets.
    # Files >4GB typically use co64 (64-bit chunk offsets) which can handle
    # any size. The stco overflow check is in rewrite_moov_offsets() and only
    # triggers for files that actually use 32-bit stco with values near 2^32.

    # Virtual layout: ftyp + moov + original[ftyp_end:moov_start]
    moov_size = len(moov_data)
    delta = moov_size  # everything from ftyp_end shifts right by moov_size

    rewritten_moov = rewrite_moov_offsets(moov_data, delta)
    if rewritten_moov is None:
        return None  # stco overflow — caller uses fallback

    header_data = ftyp_data + rewritten_moov
    payload_size = original_moov_offset - ftyp_end

    return {
        "header_data": header_data,
        "virtual_size": len(header_data) + payload_size,
        "payload_remote_start": ftyp_end,
        "payload_remote_end": original_moov_offset,
        "payload_size": payload_size,
        "already_faststart": False,
    }


class RangeCache:
    """Simple LRU byte cache for proxied range requests.

    Stores (start_offset, data) pairs. Supports partial reads from
    cached ranges. Evicts oldest entries when total bytes exceed max.
    Thread-safe.
    """

    def __init__(self, max_bytes=8 * 1048576):
        self._entries = OrderedDict()  # key: start_offset, value: bytes
        self._total = 0
        self._max = max_bytes
        self._lock = threading.Lock()

    def put(self, start, data):
        """Cache a byte range."""
        with self._lock:
            if start in self._entries:
                self._total -= len(self._entries[start])
                del self._entries[start]
            self._entries[start] = data
            self._total += len(data)
            # Evict oldest until under budget
            while self._total > self._max and self._entries:
                _, old_data = self._entries.popitem(last=False)
                self._total -= len(old_data)

    def get(self, start, end):
        """Return bytes for [start, end) if fully cached, else None."""
        with self._lock:
            # Snapshot items before iterating — we may mutate _entries
            # inside the loop (del + re-insert for LRU ordering), and
            # some OrderedDict implementations raise RuntimeError on
            # concurrent structure change even from the same thread.
            items = list(self._entries.items())
            for entry_start, entry_data in items:
                entry_end = entry_start + len(entry_data)
                if entry_start <= start and end <= entry_end:
                    # Move to end (most recent) — re-insert instead of
                    # move_to_end to avoid pylint E1101 false positive.
                    del self._entries[entry_start]
                    self._entries[entry_start] = entry_data
                    offset = start - entry_start
                    length = end - start
                    return entry_data[offset : offset + length]
            return None
