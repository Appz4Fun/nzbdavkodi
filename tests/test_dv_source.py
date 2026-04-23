import struct
from unittest.mock import patch

from resources.lib.dv_source import probe_dolby_vision_source


def _box(box_type, payload):
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def _fullbox(box_type, payload, version_flags=b"\x00\x00\x00\x00"):
    return _box(box_type, version_flags + payload)


def _sample_entry_with_hvcc():
    reserved = b"\x00" * 6 + struct.pack(">H", 1)
    visual = b"\x00" * 16 + struct.pack(">HH", 3840, 2160) + b"\x00" * 50
    hvcc = _box(b"hvcC", b"\x01" + b"\x00" * 21)
    return _box(b"hvc1", reserved + visual + hvcc)


def _minimal_mp4(sample_bytes):
    ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2")
    stsd = _fullbox(b"stsd", struct.pack(">I", 1) + _sample_entry_with_hvcc())
    stsz = _fullbox(
        b"stsz", struct.pack(">II", 0, 1) + struct.pack(">I", len(sample_bytes))
    )
    stsc = _fullbox(b"stsc", struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1))
    stco_placeholder = _fullbox(b"stco", struct.pack(">I", 1) + struct.pack(">I", 0))
    stbl = _box(b"stbl", stsd + stsz + stsc + stco_placeholder)
    minf = _box(b"minf", stbl)
    hdlr = _fullbox(b"hdlr", b"\x00" * 4 + b"vide" + b"\x00" * 12)
    mdia = _box(b"mdia", hdlr + minf)
    trak = _box(b"trak", mdia)
    moov = _box(b"moov", trak)
    mdat = _box(b"mdat", sample_bytes)

    file_bytes = bytearray(ftyp + moov + mdat)
    stco_pos = bytes(file_bytes).find(b"stco")
    chunk_offset = len(ftyp) + len(moov) + 8
    struct.pack_into(">I", file_bytes, stco_pos + 12, chunk_offset)
    return bytes(file_bytes)


class _MockResponse:
    def __init__(self, data):
        self._data = data

    def read(self, size=-1):
        if size is None or size < 0:
            return self._data
        return self._data[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _mock_urlopen_from_file(file_bytes):
    def _mock(req, timeout=None):
        range_header = req.get_header("Range") or ""
        if range_header.startswith("bytes="):
            start, end = range_header.replace("bytes=", "").split("-")
            start = int(start)
            end = int(end)
            data = file_bytes[start : end + 1]
        else:
            data = file_bytes
        return _MockResponse(data)

    return _mock


def _nal(body):
    """Build a length-prefixed NAL unit (4-byte size + body)."""
    return struct.pack(">I", len(body)) + body


def _unspec62_nal(rpu_bytes):
    """Build a UNSPEC62 NAL wrapping a raw RPU payload."""
    # NAL header: forbidden(0) | nal_unit_type(62) | layer_id_high(0) = 0x7c,
    # then layer_id_low(0) | temporal_id_plus1(1) = 0x01.
    return _nal(b"\x7c\x01" + rpu_bytes)


def test_probe_mp4_profile8_from_first_sample():
    from pathlib import Path

    rpu = Path("tests/fixtures/dovi/profile8.bin").read_bytes()
    # Prepend a non-DV NAL (e.g. trailing slice, type=0) so the parser must
    # walk past it to find the UNSPEC62 NAL.
    sample = _nal(b"\x02\x01framedata") + _unspec62_nal(rpu)
    mp4 = _minimal_mp4(sample)

    mock = _mock_urlopen_from_file(mp4)
    with patch("resources.lib.dv_source.urlopen", side_effect=mock), patch(
        "resources.lib.mp4_parser.urlopen", side_effect=mock
    ):
        result = probe_dolby_vision_source("http://host/file.mp4", auth_header=None)

    assert result.classification == "dv_allowed_for_fmp4"
    assert result.reason == "non_p7_dv_profile"
    assert result.profile == 8
    assert result.el_type is None


def test_probe_mp4_without_rpu_is_non_dv():
    sample = struct.pack(">I", 8) + b"\x26\x01\x02\x03\x04\x05\x06\x07"
    mp4 = _minimal_mp4(sample)

    mock = _mock_urlopen_from_file(mp4)
    with patch("resources.lib.dv_source.urlopen", side_effect=mock), patch(
        "resources.lib.mp4_parser.urlopen", side_effect=mock
    ):
        result = probe_dolby_vision_source("http://host/file.mp4", auth_header=None)

    assert result.classification == "non_dv"
    assert result.reason == "no_rpu_nal_found"


def _vint(value):
    if value < 0x7F:
        return bytes([0x80 | value])
    if value < 0x3FFF:
        return bytes([0x40 | (value >> 8), value & 0xFF])
    raise ValueError("fixture value too large")


def _elm(elem_id, payload):
    return elem_id + _vint(len(payload)) + payload


def _simpleblock(track_number, payload):
    return _vint(track_number) + struct.pack(">hB", 0, 0) + payload


def _minimal_mkv(sample_bytes):
    codec_id = _elm(b"\x86", b"V_MPEGH/ISO/HEVC")
    codec_private = _elm(b"\x63\xa2", b"\x01" + b"\x00" * 21)
    track_number = _elm(b"\xd7", b"\x01")
    track_type = _elm(b"\x83", b"\x01")
    track_entry = _elm(b"\xae", track_number + track_type + codec_id + codec_private)
    tracks = _elm(b"\x16\x54\xae\x6b", track_entry)
    cluster = _elm(b"\x1f\x43\xb6\x75", _elm(b"\xa3", _simpleblock(1, sample_bytes)))
    segment = _elm(b"\x18\x53\x80\x67", tracks + cluster)
    ebml = _elm(b"\x1a\x45\xdf\xa3", b"\x42\x86\x81\x01")
    return ebml + segment


def test_probe_mkv_profile7_mel_from_first_block():
    from pathlib import Path

    rpu = Path("tests/fixtures/dovi/mel_orig.bin").read_bytes()
    nal = b"\x7c\x01" + rpu
    sample = struct.pack(">I", len(nal)) + nal
    mkv = _minimal_mkv(sample)

    mock = _mock_urlopen_from_file(mkv)
    with patch("resources.lib.dv_source.urlopen", side_effect=mock):
        result = probe_dolby_vision_source("http://host/file.mkv", auth_header=None)

    assert result.classification == "dv_allowed_for_fmp4"
    assert result.reason == "p7_mel"
    assert result.profile == 7
    assert result.el_type == "MEL"


def test_probe_mkv_profile7_fel_from_first_block():
    from pathlib import Path

    rpu = Path("tests/fixtures/dovi/fel_orig.bin").read_bytes()
    nal = b"\x7c\x01" + rpu
    sample = struct.pack(">I", len(nal)) + nal
    mkv = _minimal_mkv(sample)

    mock = _mock_urlopen_from_file(mkv)
    with patch("resources.lib.dv_source.urlopen", side_effect=mock):
        result = probe_dolby_vision_source("http://host/file.mkv", auth_header=None)

    assert result.classification == "dv_profile_7_fel"
    assert result.reason == "p7_fel"
    assert result.profile == 7
    assert result.el_type == "FEL"


# --- P4 tests: containers, network, security, auth ------------------------


def _minimal_mp4_with_co64(sample_bytes):
    """Same layout as _minimal_mp4 but uses co64 (64-bit chunk offsets)."""
    ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2")
    stsd = _fullbox(b"stsd", struct.pack(">I", 1) + _sample_entry_with_hvcc())
    stsz = _fullbox(
        b"stsz", struct.pack(">II", 0, 1) + struct.pack(">I", len(sample_bytes))
    )
    stsc = _fullbox(b"stsc", struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1))
    # co64 entry is 8 bytes (vs stco's 4).
    co64_placeholder = _fullbox(b"co64", struct.pack(">I", 1) + struct.pack(">Q", 0))
    stbl = _box(b"stbl", stsd + stsz + stsc + co64_placeholder)
    minf = _box(b"minf", stbl)
    hdlr = _fullbox(b"hdlr", b"\x00" * 4 + b"vide" + b"\x00" * 12)
    mdia = _box(b"mdia", hdlr + minf)
    trak = _box(b"trak", mdia)
    moov = _box(b"moov", trak)
    mdat = _box(b"mdat", sample_bytes)

    file_bytes = bytearray(ftyp + moov + mdat)
    co64_pos = bytes(file_bytes).find(b"co64")
    chunk_offset = len(ftyp) + len(moov) + 8
    # co64_pos indexes the start of "co64" (the type field, AFTER the 4-byte
    # size). Body layout from there: type(4) + version+flags(4) +
    # entry_count(4) + entry(8). First entry lives at co64_pos + 12.
    struct.pack_into(">Q", file_bytes, co64_pos + 12, chunk_offset)
    return bytes(file_bytes)


def test_probe_mp4_with_co64_chunk_offsets():
    """Real DV UHD files >4 GiB use co64 instead of stco for chunk offsets.
    The probe must try co64 as a fallback when stco is absent."""
    from pathlib import Path

    rpu = Path("tests/fixtures/dovi/profile8.bin").read_bytes()
    sample = _nal(b"\x02\x01frame") + _unspec62_nal(rpu)
    mp4 = _minimal_mp4_with_co64(sample)

    mock = _mock_urlopen_from_file(mp4)
    with patch("resources.lib.dv_source.urlopen", side_effect=mock), patch(
        "resources.lib.mp4_parser.urlopen", side_effect=mock
    ):
        result = probe_dolby_vision_source("http://host/big.mp4", auth_header=None)

    assert result.classification == "dv_allowed_for_fmp4"
    assert result.profile == 8


def test_probe_mp4_clamps_unreasonable_first_sample_size():
    """A malicious moov declaring a 4 GiB first sample (uint32 max) must be
    rejected before we attempt the HTTP Range — otherwise a poisoned feed
    could OOM 32-bit Kodi via `resp.read()`."""
    ftyp = _box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2")
    stsd = _fullbox(b"stsd", struct.pack(">I", 1) + _sample_entry_with_hvcc())
    # Declare sample_size = 4 GiB.
    huge = 0xFFFFFFFF
    stsz = _fullbox(b"stsz", struct.pack(">I", huge) + struct.pack(">I", 1))
    stsc = _fullbox(b"stsc", struct.pack(">I", 1) + struct.pack(">III", 1, 1, 1))
    stco = _fullbox(b"stco", struct.pack(">I", 1) + struct.pack(">I", 512))
    stbl = _box(b"stbl", stsd + stsz + stsc + stco)
    minf = _box(b"minf", stbl)
    hdlr = _fullbox(b"hdlr", b"\x00" * 4 + b"vide" + b"\x00" * 12)
    mdia = _box(b"mdia", hdlr + minf)
    trak = _box(b"trak", mdia)
    moov = _box(b"moov", trak)
    mp4 = ftyp + moov + _box(b"mdat", b"x" * 16)

    mock = _mock_urlopen_from_file(mp4)
    with patch("resources.lib.dv_source.urlopen", side_effect=mock), patch(
        "resources.lib.mp4_parser.urlopen", side_effect=mock
    ):
        result = probe_dolby_vision_source("http://host/evil.mp4", auth_header=None)

    assert result.classification == "dv_unknown"
    assert result.reason == "mp4_sample_extraction_failed"


def test_probe_mkv_refuses_laced_simpleblock():
    """Lacing changes the on-wire layout of the frame region; the probe must
    bail out rather than read garbage as a NAL length prefix."""
    from pathlib import Path

    # Build a SimpleBlock with lacing flag set (Xiph lacing = bits 1..2 = 10).
    rpu = Path("tests/fixtures/dovi/mel_orig.bin").read_bytes()
    nal = b"\x7c\x01" + rpu
    sample = struct.pack(">I", len(nal)) + nal
    laced_simpleblock = _vint(1) + struct.pack(">hB", 0, 0b0000_0100) + sample

    codec_id = _elm(b"\x86", b"V_MPEGH/ISO/HEVC")
    codec_private = _elm(b"\x63\xa2", b"\x01" + b"\x00" * 21)
    track_number = _elm(b"\xd7", b"\x01")
    track_type = _elm(b"\x83", b"\x01")
    track_entry = _elm(b"\xae", track_number + track_type + codec_id + codec_private)
    tracks = _elm(b"\x16\x54\xae\x6b", track_entry)
    cluster = _elm(b"\x1f\x43\xb6\x75", _elm(b"\xa3", laced_simpleblock))
    segment = _elm(b"\x18\x53\x80\x67", tracks + cluster)
    ebml = _elm(b"\x1a\x45\xdf\xa3", b"\x42\x86\x81\x01")
    mkv = ebml + segment

    mock = _mock_urlopen_from_file(mkv)
    with patch("resources.lib.dv_source.urlopen", side_effect=mock):
        result = probe_dolby_vision_source("http://host/laced.mkv", auth_header=None)

    # Lacing refusal → no frame data → "no RPU NAL found" → non_dv.
    # Or (if the iter_ebml malformation short-circuits first) dv_unknown.
    # Either is an acceptable safe answer; what we must NOT see is a
    # misclassified "dv_profile_7_fel" from treating lace metadata as a NAL
    # length prefix.
    assert result.classification in ("non_dv", "dv_unknown")


def test_probe_mkv_extracts_from_blockgroup_wrapper():
    """Some muxers wrap video frames in a BlockGroup (0xA0) → Block (0xA1)
    instead of a SimpleBlock (0xA3). The probe must descend into BlockGroup
    to find the video frame."""
    from pathlib import Path

    rpu = Path("tests/fixtures/dovi/mel_orig.bin").read_bytes()
    nal = b"\x7c\x01" + rpu
    sample = struct.pack(">I", len(nal)) + nal
    # Block (0xA1) has the same on-wire shape as SimpleBlock for our purposes.
    block = _vint(1) + struct.pack(">hB", 0, 0) + sample
    block_group = _elm(b"\xa0", _elm(b"\xa1", block))

    codec_id = _elm(b"\x86", b"V_MPEGH/ISO/HEVC")
    codec_private = _elm(b"\x63\xa2", b"\x01" + b"\x00" * 21)
    track_number = _elm(b"\xd7", b"\x01")
    track_type = _elm(b"\x83", b"\x01")
    track_entry = _elm(b"\xae", track_number + track_type + codec_id + codec_private)
    tracks = _elm(b"\x16\x54\xae\x6b", track_entry)
    cluster = _elm(b"\x1f\x43\xb6\x75", block_group)
    segment = _elm(b"\x18\x53\x80\x67", tracks + cluster)
    ebml = _elm(b"\x1a\x45\xdf\xa3", b"\x42\x86\x81\x01")
    mkv = ebml + segment

    mock = _mock_urlopen_from_file(mkv)
    with patch("resources.lib.dv_source.urlopen", side_effect=mock):
        result = probe_dolby_vision_source(
            "http://host/blockgroup.mkv", auth_header=None
        )

    assert result.classification == "dv_allowed_for_fmp4"
    assert result.profile == 7
    assert result.el_type == "MEL"


def test_probe_degrades_to_dv_unknown_on_network_error():
    """urlopen raising URLError must not propagate — probe must return
    dv_unknown so the caller can fail safe to matroska."""
    from urllib.error import URLError

    def _raising_urlopen(*_args, **_kwargs):
        raise URLError("connection refused")

    with patch("resources.lib.dv_source.urlopen", side_effect=_raising_urlopen), patch(
        "resources.lib.mp4_parser.urlopen", side_effect=_raising_urlopen
    ):
        result = probe_dolby_vision_source(
            "http://host/unreachable.mp4", auth_header=None
        )

    assert result.classification == "dv_unknown"


def test_probe_unsupported_extension_returns_dv_unknown():
    """Extensions we don't know how to probe (webm, ts, avi) return
    dv_unknown with a specific reason."""
    result = probe_dolby_vision_source("http://host/unknown.ts", auth_header=None)
    assert result.classification == "dv_unknown"
    assert result.reason == "unsupported_container"


def test_http_range_applies_size_cap_via_resp_read_argument():
    """The size cap is enforced at `resp.read(max_bytes)` — a server that
    ignores the Range header cannot push more than max_bytes into memory."""
    from resources.lib.dv_source import _HTTP_READ_CAP, _http_range

    giant = b"x" * (_HTTP_READ_CAP * 4)  # 4× the cap
    captured_sizes = []

    def _mock(req, timeout=None):
        del req, timeout
        mock_resp = _MockResponse(giant)
        original_read = mock_resp.read

        def _read(size=-1):
            captured_sizes.append(size)
            return original_read(size)

        mock_resp.read = _read  # type: ignore[method-assign]
        return mock_resp

    with patch("resources.lib.dv_source.urlopen", side_effect=_mock):
        result = _http_range("http://host/x", 0, 100, auth_header=None)

    assert captured_sizes == [_HTTP_READ_CAP]
    assert len(result) == _HTTP_READ_CAP


def test_http_range_strips_crlf_from_auth_header():
    """Auth header CR/LF bytes are stripped before being passed to urllib —
    defeats header-injection attempts from a future untrusted caller."""
    captured_headers = {}

    def _mock(req, timeout=None):
        del timeout
        # Snapshot the headers that reached urlopen.
        captured_headers.update({k.lower(): v for k, v in req.header_items()})
        return _MockResponse(b"")

    from resources.lib.dv_source import _http_range

    with patch("resources.lib.dv_source.urlopen", side_effect=_mock):
        _http_range(
            "http://host/x",
            0,
            10,
            auth_header="Basic abc\r\nEvil-Header: pwn",
        )

    assert captured_headers.get("authorization") == "Basic abcEvil-Header: pwn"
