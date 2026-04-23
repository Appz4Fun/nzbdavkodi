"""Remote container probe that extracts the first HEVC sample from an MP4 or
MKV/Matroska file via HTTP range requests, locates the first Dolby Vision
UNSPEC62 RPU NAL, and feeds it into dv_rpu for structured classification.

Returns a :class:`DolbyVisionSourceResult` that drives fMP4 vs Matroska
routing in :mod:`stream_proxy` — profile 7 FEL and anything unrecognised
falls back to matroska so the proxy never shows Kodi a stream it can't
decode, while profile 8 / profile 5 / non-DV / profile 7 MEL stay on fmp4.
"""

from dataclasses import dataclass
import struct
from urllib.request import Request, urlopen

from resources.lib.dv_rpu import parse_unspec62_nalu
from resources.lib.mp4_parser import fetch_remote_mp4_layout, read_box_header


@dataclass
class DolbyVisionSourceResult:
    classification: str
    reason: str
    profile: int = None
    el_type: str = None


def _http_range(url, start, end, auth_header=None):
    req = Request(url)
    req.add_header("Range", "bytes={}-{}".format(start, end))
    if auth_header:
        req.add_header("Authorization", auth_header)
    with urlopen(req, timeout=30) as resp:  # nosec B310
        return resp.read()


def _iter_boxes(data, start=0, end=None):
    if end is None:
        end = len(data)
    offset = start
    while offset + 8 <= end:
        parsed = read_box_header(data, offset)
        if parsed is None:
            return
        box_type, header_size, total_size = parsed
        if total_size < 8:
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
        if nal and (nal[0] >> 1) == 62:
            return nal
    return None


def _find_first_video_track(moov_data):
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
        # hdlr body layout: 4 bytes version+flags, 4 bytes pre_defined,
        # 4 bytes handler_type, then reserved/name.
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


def _extract_mp4_first_sample(url, file_size, auth_header):
    layout = fetch_remote_mp4_layout(url, file_size, auth_header=auth_header)
    if layout is None:
        return None
    moov = layout["moov_data"]
    stbl = _find_first_video_track(moov)
    if stbl is None:
        return None
    _, stbl_body_start, stbl_end = stbl

    stsz = _find_child(moov, stbl_body_start, stbl_end, b"stsz")
    stco = _find_child(moov, stbl_body_start, stbl_end, b"stco")
    if stsz is None or stco is None:
        return None

    _, stsz_body_start, _ = stsz
    _, stco_body_start, _ = stco
    # stsz body: 4 bytes version+flags, 4 bytes sample_size, 4 bytes
    # sample_count, then (if sample_size==0) sample_count × 4-byte entries.
    sample_size = struct.unpack_from(">I", moov, stsz_body_start + 4)[0]
    sample_count = struct.unpack_from(">I", moov, stsz_body_start + 8)[0]
    if sample_count < 1:
        return None
    if sample_size == 0:
        first_sample_size = struct.unpack_from(">I", moov, stsz_body_start + 12)[0]
    else:
        first_sample_size = sample_size
    if first_sample_size <= 0:
        return None
    chunk_offset = struct.unpack_from(">I", moov, stco_body_start + 8)[0]
    return _http_range(
        url, chunk_offset, chunk_offset + first_sample_size - 1, auth_header
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
    except (OSError, ValueError, struct.error):
        return DolbyVisionSourceResult("dv_unknown", "mp4_sample_extraction_failed")
    if not sample:
        return DolbyVisionSourceResult("dv_unknown", "mp4_sample_extraction_failed")
    nal = _find_unspec62_nal(sample)
    if nal is None:
        return DolbyVisionSourceResult("non_dv", "no_rpu_nal_found")
    try:
        info = parse_unspec62_nalu(nal)
    except (ValueError, IndexError):
        return DolbyVisionSourceResult("dv_unknown", "rpu_parse_failed")
    return _classify_parsed_rpu(info)


def _vint_width(first_byte):
    mask = 0x80
    width = 1
    while width <= 8 and not (first_byte & mask):
        mask >>= 1
        width += 1
    return width, mask


def _read_vint_size(data, offset):
    """Read an EBML variable-length size. Strips the length-descriptor bit."""
    width, mask = _vint_width(data[offset])
    value = data[offset] & (mask - 1)
    for i in range(1, width):
        value = (value << 8) | data[offset + i]
    return value, width


def _read_element_id(data, offset):
    """Read an EBML Element ID. Keeps the length-descriptor bit as part of the ID."""
    width, _ = _vint_width(data[offset])
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
        yield elem_id, payload_start, payload_end
        offset = payload_end


def _first_bytes(url, auth_header, size=2 * 1024 * 1024):
    return _http_range(url, 0, size - 1, auth_header)


def _extract_mkv_first_sample(url, auth_header):
    data = _first_bytes(url, auth_header)
    track_number = None

    for elem_id, payload_start, payload_end in _iter_ebml(data):
        if elem_id != 0x18538067:  # Segment
            continue
        segment = data[payload_start:payload_end]
        for sub_id, sub_start, sub_end in _iter_ebml(segment):
            if sub_id == 0x1654AE6B:  # Tracks
                tracks = segment[sub_start:sub_end]
                for track_id, track_start, track_end in _iter_ebml(tracks):
                    if track_id != 0xAE:
                        continue
                    entry = tracks[track_start:track_end]
                    current_track = None
                    codec_id = None
                    for field_id, field_start, field_end in _iter_ebml(entry):
                        if field_id == 0xD7:
                            current_track = entry[field_start]
                        elif field_id == 0x86:
                            codec_id = entry[field_start:field_end].decode(
                                errors="ignore"
                            )
                    if codec_id == "V_MPEGH/ISO/HEVC":
                        track_number = current_track
            if sub_id == 0x1F43B675 and track_number is not None:  # Cluster
                cluster = segment[sub_start:sub_end]
                for block_id, block_start, block_end in _iter_ebml(cluster):
                    if block_id != 0xA3:  # SimpleBlock
                        continue
                    block = cluster[block_start:block_end]
                    parsed_track, width = _read_vint_size(block, 0)
                    if parsed_track != track_number:
                        continue
                    # SimpleBlock: track(vint) + timecode(2) + flags(1) + frame data
                    return block[width + 3 :]
    return None


def _probe_mkv(url, auth_header):
    try:
        sample = _extract_mkv_first_sample(url, auth_header)
    except (OSError, ValueError, IndexError, struct.error):
        return DolbyVisionSourceResult("dv_unknown", "mkv_sample_extraction_failed")
    if not sample:
        return DolbyVisionSourceResult("dv_unknown", "mkv_sample_extraction_failed")
    nal = _find_unspec62_nal(sample)
    if nal is None:
        return DolbyVisionSourceResult("non_dv", "no_rpu_nal_found")
    try:
        info = parse_unspec62_nalu(nal)
    except (ValueError, IndexError):
        return DolbyVisionSourceResult("dv_unknown", "rpu_parse_failed")
    return _classify_parsed_rpu(info)


def probe_dolby_vision_source(url, auth_header=None, file_size=None):
    """Probe a remote MP4 or MKV URL and classify its Dolby Vision source.

    Args:
        url: Remote HTTP URL.
        auth_header: Optional full ``Authorization`` header value.
        file_size: Optional total file size in bytes. If omitted the probe
            uses a 1 MB ceiling which is large enough for MP4 ``moov``-at-front
            layouts but will miss moov-at-tail in production-size files.
    """
    lower = url.split("?", 1)[0].lower()
    if lower.endswith((".mp4", ".m4v")):
        return _probe_mp4(url, auth_header, file_size=file_size)
    if lower.endswith(".mkv"):
        return _probe_mkv(url, auth_header)
    return DolbyVisionSourceResult("dv_unknown", "unsupported_container")
