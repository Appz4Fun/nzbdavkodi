"""Remote container probe that extracts the first HEVC sample from an MP4 or
MKV/Matroska file via HTTP range requests, locates the first Dolby Vision
UNSPEC62 RPU NAL, and feeds it into dv_rpu for structured classification.

Returns a :class:`DolbyVisionSourceResult` that drives fMP4 vs Matroska
routing in :mod:`stream_proxy`. The routing matrix lives in stream_proxy
(see the comment block above the ``probe_dolby_vision_source`` call site);
this module only produces the structured classification.
"""

import struct
from dataclasses import dataclass
from typing import Optional
from urllib.request import Request, urlopen

try:
    import xbmc
except ImportError:  # pragma: no cover — tests inject the module via conftest
    xbmc = None  # type: ignore[assignment]

from resources.lib.dv_rpu import parse_unspec62_nalu
from resources.lib.mp4_parser import fetch_remote_mp4_layout, read_box_header

# Safety caps — apply at every I/O seam where an attacker-controlled field
# (stsz.first_sample_size, SimpleBlock frame size, an unbounded 200 OK
# response) could otherwise ask us to allocate gigabytes on 32-bit Kodi.
_HTTP_READ_CAP = 16 * 1024 * 1024  # 16 MiB — larger than any real HEVC AU.
_MAX_FIRST_SAMPLE_SIZE = 16 * 1024 * 1024
_MKV_HEAD_SIZE = 2 * 1024 * 1024


# EBML element IDs used during the MKV walk (include the length-descriptor
# bits — see Matroska spec / RFC 8794).
_EBML_ID_SEGMENT = 0x18538067
_EBML_ID_TRACKS = 0x1654AE6B
_EBML_ID_CLUSTER = 0x1F43B675
_EBML_ID_TRACK_ENTRY = 0xAE
_EBML_ID_TRACK_NUMBER = 0xD7
_EBML_ID_CODEC_ID = 0x86
_EBML_ID_SIMPLE_BLOCK = 0xA3
_EBML_ID_BLOCK_GROUP = 0xA0
_EBML_ID_BLOCK = 0xA1


@dataclass
class DolbyVisionSourceResult:
    classification: str
    reason: str
    profile: Optional[int] = None
    el_type: Optional[str] = None


def _log_debug(msg):
    if xbmc is not None:
        try:
            xbmc.log("NZB-DAV: " + msg, xbmc.LOGDEBUG)
        except Exception:  # pylint: disable=broad-except
            pass


def _sanitize_header_value(value):
    """Strip CRLF from a header value to defeat header-injection attempts."""
    if value is None:
        return None
    return value.replace("\r", "").replace("\n", "")


def _http_range(url, start, end, auth_header=None, max_bytes=_HTTP_READ_CAP):
    """Fetch bytes[start..end] from url, capped at max_bytes to protect
    against servers that ignore the Range header (return 200 OK + full body).
    """
    req = Request(url)
    req.add_header("Range", "bytes={}-{}".format(start, end))
    clean_auth = _sanitize_header_value(auth_header)
    if clean_auth:
        req.add_header("Authorization", clean_auth)
    # Caller validates stream URLs before probing; this range fetcher only
    # adds bounded reads and sanitized auth headers.
    # nosemgrep
    with urlopen(req, timeout=30) as resp:  # nosec B310
        return resp.read(max_bytes)


def _iter_boxes(data, start=0, end=None):
    if end is None:
        end = len(data)
    offset = start
    while offset + 8 <= end:
        parsed = read_box_header(data, offset)
        if parsed is None:
            return
        box_type, header_size, total_size = parsed
        if total_size < 8 or offset + total_size > end:
            return
        yield box_type, offset, offset + header_size, offset + total_size
        offset += total_size


def _find_child(data, parent_start, parent_end, box_type):
    for current_type, offset, body_start, box_end in _iter_boxes(
        data, parent_start, parent_end
    ):
        if current_type == box_type:
            return offset, body_start, box_end
    return None


def _split_length_prefixed_nals(sample, nal_length_size=4):
    # nal_length_size hardcoded to 4 at the only caller. hvcC's
    # lengthSizeMinusOne field can specify 1/2/4 bytes, but real DV muxes
    # (both MP4 and Matroska) always use 4. Left as a parameter so a future
    # hvcC-aware caller can pass 1 or 2 without a signature change.
    offset = 0
    while offset + nal_length_size <= len(sample):
        if nal_length_size == 4:
            size = struct.unpack_from(">I", sample, offset)[0]
        elif nal_length_size == 2:
            size = struct.unpack_from(">H", sample, offset)[0]
        else:
            size = sample[offset]
        offset += nal_length_size
        if size <= 0 or offset + size > len(sample):
            return
        yield sample[offset : offset + size]
        offset += size


def _find_unspec62_nal(sample):
    for nal in _split_length_prefixed_nals(sample, 4):
        if nal and ((nal[0] >> 1) & 0x3F) == 62:
            return nal
    return None


def _find_first_video_stbl(moov_data):
    """Walk moov → trak → mdia → minf → stbl for the first video track.

    Returns the (stbl_offset, stbl_body_start, stbl_end) tuple in
    moov_data coordinates, or None if no video track was found.
    """
    moov = _find_child(moov_data, 0, len(moov_data), b"moov")
    if moov is None:
        return None
    _, moov_body_start, moov_end = moov
    for _, trak_offset, trak_body_start, trak_end in _iter_boxes(
        moov_data, moov_body_start, moov_end
    ):
        if moov_data[trak_offset + 4 : trak_offset + 8] != b"trak":
            continue
        mdia = _find_child(moov_data, trak_body_start, trak_end, b"mdia")
        if mdia is None:
            continue
        _, mdia_body_start, mdia_end = mdia
        hdlr = _find_child(moov_data, mdia_body_start, mdia_end, b"hdlr")
        if hdlr is None:
            continue
        _, hdlr_body_start, _ = hdlr
        # hdlr body: 4 bytes version+flags, 4 bytes pre_defined, 4 bytes
        # handler_type ("vide" for video), then reserved/name.
        if moov_data[hdlr_body_start + 8 : hdlr_body_start + 12] != b"vide":
            continue
        minf = _find_child(moov_data, mdia_body_start, mdia_end, b"minf")
        if minf is None:
            continue
        _, minf_body_start, minf_end = minf
        stbl = _find_child(moov_data, minf_body_start, minf_end, b"stbl")
        if stbl is None:
            continue
        return stbl
    return None


def _read_chunk_offset(moov, stbl_body_start, stbl_end):
    """Return the first chunk's file offset, handling both stco (32-bit) and
    co64 (64-bit) atoms. Returns None if neither is present or the entry
    count is zero.
    """
    stco = _find_child(moov, stbl_body_start, stbl_end, b"stco")
    if stco is not None:
        _, body, end = stco
        if body + 12 > end:
            return None
        count = struct.unpack_from(">I", moov, body + 4)[0]
        if count < 1:
            return None
        return struct.unpack_from(">I", moov, body + 8)[0]
    co64 = _find_child(moov, stbl_body_start, stbl_end, b"co64")
    if co64 is not None:
        _, body, end = co64
        if body + 16 > end:
            return None
        count = struct.unpack_from(">I", moov, body + 4)[0]
        if count < 1:
            return None
        return struct.unpack_from(">Q", moov, body + 8)[0]
    return None


def _extract_mp4_first_sample(url, file_size, auth_header):
    layout = fetch_remote_mp4_layout(url, file_size, auth_header=auth_header)
    if layout is None:
        return None
    moov = layout["moov_data"]
    stbl = _find_first_video_stbl(moov)
    if stbl is None:
        return None
    _, stbl_body_start, stbl_end = stbl

    stsz = _find_child(moov, stbl_body_start, stbl_end, b"stsz")
    if stsz is None:
        return None

    _, stsz_body_start, stsz_body_end = stsz
    # stsz body: 4 bytes version+flags, 4 bytes sample_size, 4 bytes
    # sample_count, then (if sample_size==0) sample_count × 4-byte entries.
    # Each unpack must verify the read fits inside the stsz body, otherwise
    # a malformed moov could make struct read into adjacent box bytes
    # (or off the end of the buffer entirely).
    if stsz_body_end - stsz_body_start < 12:
        return None
    sample_size = struct.unpack_from(">I", moov, stsz_body_start + 4)[0]
    sample_count = struct.unpack_from(">I", moov, stsz_body_start + 8)[0]
    if sample_count < 1:
        return None
    if sample_size == 0:
        if stsz_body_end - stsz_body_start < 16:
            return None
        first_sample_size = struct.unpack_from(">I", moov, stsz_body_start + 12)[0]
    else:
        first_sample_size = sample_size
    if first_sample_size <= 0 or first_sample_size > _MAX_FIRST_SAMPLE_SIZE:
        # Clamp: a malicious moov could declare a 4 GiB first sample.
        return None

    chunk_offset = _read_chunk_offset(moov, stbl_body_start, stbl_end)
    if chunk_offset is None:
        return None

    return _http_range(
        url,
        chunk_offset,
        chunk_offset + first_sample_size - 1,
        auth_header,
        max_bytes=first_sample_size,
    )


def _classify_parsed_rpu(info):
    if info.profile == 7:
        if info.el_type == "MEL":
            return DolbyVisionSourceResult(
                "dv_allowed_for_fmp4", "p7_mel", profile=7, el_type="MEL"
            )
        if info.el_type == "FEL":
            return DolbyVisionSourceResult(
                "dv_profile_7_fel", "p7_fel", profile=7, el_type="FEL"
            )
        return DolbyVisionSourceResult("dv_unknown", "p7_mel_fel_unproven", profile=7)
    return DolbyVisionSourceResult(
        "dv_allowed_for_fmp4", "non_p7_dv_profile", profile=info.profile
    )


def _probe_mp4(url, auth_header, file_size=None):
    if file_size is None:
        file_size = 1 << 20
    try:
        sample = _extract_mp4_first_sample(url, file_size, auth_header)
    except (OSError, ValueError, struct.error, IndexError) as exc:
        _log_debug("DV probe MP4 extraction failed: {!r}".format(exc))
        return DolbyVisionSourceResult("dv_unknown", "mp4_sample_extraction_failed")
    if not sample:
        _log_debug("DV probe MP4 extraction: no sample data returned")
        return DolbyVisionSourceResult("dv_unknown", "mp4_sample_extraction_failed")
    nal = _find_unspec62_nal(sample)
    if nal is None:
        return DolbyVisionSourceResult("non_dv", "no_rpu_nal_found")
    try:
        info = parse_unspec62_nalu(nal)
    except (ValueError, IndexError, NotImplementedError, UnicodeDecodeError) as exc:
        _log_debug("DV probe RPU parse failed: {!r}".format(exc))
        return DolbyVisionSourceResult("dv_unknown", "rpu_parse_failed")
    return _classify_parsed_rpu(info)


def _vint_width(first_byte):
    """Find EBML VINT width from the first byte. Width is the position of the
    length-descriptor bit (MSB-first). Raises ValueError if no bit is set
    within the legal 1..8 byte range (malformed or zero-padded input).
    """
    mask = 0x80
    width = 1
    while width <= 8:
        if first_byte & mask:
            return width, mask
        mask >>= 1
        width += 1
    raise ValueError("invalid EBML VINT: no length-descriptor bit in first byte")


def _read_vint_size(data, offset):
    """Read an EBML variable-length size. Strips the length-descriptor bit."""
    width, mask = _vint_width(data[offset])
    if offset + width > len(data):
        raise ValueError("EBML VINT truncated")
    value = data[offset] & (mask - 1)
    for i in range(1, width):
        value = (value << 8) | data[offset + i]
    return value, width


def _read_element_id(data, offset):
    """Read an EBML Element ID. Keeps the length-descriptor bit as part of the ID."""
    width, _ = _vint_width(data[offset])
    if offset + width > len(data):
        raise ValueError("EBML Element ID truncated")
    value = 0
    for i in range(width):
        value = (value << 8) | data[offset + i]
    return value, width


def _iter_ebml(data, start=0, end=None):
    if end is None:
        end = len(data)
    offset = start
    while offset < end:
        elem_id, id_len = _read_element_id(data, offset)
        size, size_len = _read_vint_size(data, offset + id_len)
        payload_start = offset + id_len + size_len
        payload_end = payload_start + size
        # Clamp so a malformed child cannot overrun its parent. Without this
        # guard, a corrupt size field would yield offsets past end and later
        # cause IndexError in downstream iter calls.
        if payload_start > end:
            return
        payload_end = min(payload_end, end)
        if payload_end <= offset:
            # Zero-sized or negative-progress element — refuse to loop.
            return
        yield elem_id, payload_start, payload_end
        offset = payload_end


def _first_bytes(url, auth_header, size=_MKV_HEAD_SIZE):
    return _http_range(url, 0, size - 1, auth_header, max_bytes=size)


def _iter_block_frames(block_id, block, block_track, width):
    """Yield frame bytes from a Matroska (Simple)Block.

    Accepts both SimpleBlock (0xA3) and Block (0xA1). Returns nothing if
    lacing is enabled (Xiph/EBML/fixed) — lacing layouts prepend lace
    metadata before frame data, so reading `block[width+3:]` as a NAL stream
    would produce garbage. Real HEVC muxes never lace video.
    """
    # SimpleBlock: track(vint) + timecode(2) + flags(1) + frame(s)
    # Block:       identical on-the-wire shape for our purposes
    # (lacing flags sit in the same position).
    if width + 3 > len(block):
        return
    flags = block[width + 2]
    lacing = (flags >> 1) & 0x03
    if lacing != 0:
        return
    del block_id, block_track  # unused; kept for call-site clarity
    yield block[width + 3 :]


def _extract_mkv_first_sample(url, auth_header):
    data = _first_bytes(url, auth_header)
    for elem_id, payload_start, payload_end in _iter_ebml(data):
        if elem_id != _EBML_ID_SEGMENT:
            continue
        frame = _extract_mkv_frame_from_segment(data[payload_start:payload_end])
        if frame is not None:
            return frame
    return None


def _extract_mkv_frame_from_segment(segment):
    """Walk a Matroska Segment to find the first HEVC video frame.

    Matroska allows Tracks and Cluster to appear in either order, so this
    collects the HEVC track_number from Tracks while scanning, and returns
    the first frame once both a HEVC track and a matching Cluster have
    been seen.
    """
    track_number = None
    for sub_id, sub_start, sub_end in _iter_ebml(segment):
        if sub_id == _EBML_ID_TRACKS:
            track_number = _find_hevc_track_number(segment[sub_start:sub_end])
        elif sub_id == _EBML_ID_CLUSTER and track_number is not None:
            frame = _extract_mkv_block_frame(segment[sub_start:sub_end], track_number)
            if frame is not None:
                return frame
    return None


def _find_hevc_track_number(tracks):
    """Return the TrackNumber of the first V_MPEGH/ISO/HEVC track, or None."""
    for track_id, track_start, track_end in _iter_ebml(tracks):
        if track_id != _EBML_ID_TRACK_ENTRY:
            continue
        entry = tracks[track_start:track_end]
        current_track = None
        codec_id = None
        for field_id, field_start, field_end in _iter_ebml(entry):
            if field_id == _EBML_ID_TRACK_NUMBER:
                # TrackNumber is an EBML unsigned int (big-endian). Real
                # DV muxes always use track 1 (one byte), but decode
                # properly to cover future 2+ byte cases.
                current_track = int.from_bytes(entry[field_start:field_end], "big")
            elif field_id == _EBML_ID_CODEC_ID:
                codec_id = entry[field_start:field_end].decode(errors="ignore")
        if codec_id == "V_MPEGH/ISO/HEVC":
            return current_track
    return None


def _extract_mkv_block_frame(cluster, track_number):
    """Find the first HEVC frame in a Cluster. Walks both SimpleBlock
    (0xA3) and BlockGroup→Block (0xA0→0xA1). Returns frame bytes or None.
    """
    for block_id, block_start, block_end in _iter_ebml(cluster):
        if block_id == _EBML_ID_SIMPLE_BLOCK:
            frame = _try_read_block_frame(
                cluster[block_start:block_end], track_number, block_id
            )
            if frame is not None:
                return frame
        elif block_id == _EBML_ID_BLOCK_GROUP:
            group = cluster[block_start:block_end]
            for child_id, child_start, child_end in _iter_ebml(group):
                if child_id == _EBML_ID_BLOCK:
                    frame = _try_read_block_frame(
                        group[child_start:child_end], track_number, child_id
                    )
                    if frame is not None:
                        return frame
    return None


def _try_read_block_frame(block, track_number, block_id):
    parsed_track, width = _read_vint_size(block, 0)
    if parsed_track != track_number:
        return None
    frames = list(_iter_block_frames(block_id, block, track_number, width))
    return frames[0] if frames else None


def _probe_mkv(url, auth_header):
    try:
        sample = _extract_mkv_first_sample(url, auth_header)
    except (OSError, ValueError, IndexError, struct.error) as exc:
        _log_debug("DV probe MKV extraction failed: {!r}".format(exc))
        return DolbyVisionSourceResult("dv_unknown", "mkv_sample_extraction_failed")
    if not sample:
        _log_debug("DV probe MKV extraction: no sample data returned")
        return DolbyVisionSourceResult("dv_unknown", "mkv_sample_extraction_failed")
    nal = _find_unspec62_nal(sample)
    if nal is None:
        return DolbyVisionSourceResult("non_dv", "no_rpu_nal_found")
    try:
        info = parse_unspec62_nalu(nal)
    except (ValueError, IndexError, NotImplementedError, UnicodeDecodeError) as exc:
        _log_debug("DV probe RPU parse failed: {!r}".format(exc))
        return DolbyVisionSourceResult("dv_unknown", "rpu_parse_failed")
    return _classify_parsed_rpu(info)


def probe_dolby_vision_source(url, auth_header=None, file_size=None):
    """Probe a remote MP4 or MKV URL and classify its Dolby Vision source.

    Args:
        url: Remote HTTP URL. Expected to be validated by the caller
            (``stream_proxy._validate_url``) — no scheme/host check here.
        auth_header: Optional full ``Authorization`` header value. CR/LF
            bytes are stripped defensively.
        file_size: Optional total file size in bytes. Pass the real
            ``Content-Length`` in production so moov-at-tail MP4 files can be
            located. If omitted the probe falls back to a 1 MB ceiling
            which is only large enough for moov-at-front layouts.

    Returns:
        :class:`DolbyVisionSourceResult` with one of four classifications:
        ``"dv_profile_7_fel"``, ``"dv_allowed_for_fmp4"``, ``"non_dv"``,
        ``"dv_unknown"``. Never raises on I/O or parse errors — failures
        degrade to ``dv_unknown`` so the caller can fail safe to matroska.
    """
    # Strip query string AND fragment so a URL like ``foo.mkv#.mp4`` is
    # classified by the real path component, not the fragment label.
    lower = url.split("?", 1)[0].split("#", 1)[0].lower()
    if lower.endswith((".mp4", ".m4v")):
        return _probe_mp4(url, auth_header, file_size=file_size)
    if lower.endswith(".mkv"):
        return _probe_mkv(url, auth_header)
    return DolbyVisionSourceResult("dv_unknown", "unsupported_container")
