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
    stsz = _fullbox(b"stsz", struct.pack(">II", 0, 1) + struct.pack(">I", len(sample_bytes)))
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

    def read(self):
        return self._data

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
    codec_private = _elm(b"\x63\xA2", b"\x01" + b"\x00" * 21)
    track_number = _elm(b"\xD7", b"\x01")
    track_type = _elm(b"\x83", b"\x01")
    track_entry = _elm(b"\xAE", track_number + track_type + codec_id + codec_private)
    tracks = _elm(b"\x16\x54\xAE\x6B", track_entry)
    cluster = _elm(
        b"\x1F\x43\xB6\x75", _elm(b"\xA3", _simpleblock(1, sample_bytes))
    )
    segment = _elm(b"\x18\x53\x80\x67", tracks + cluster)
    ebml = _elm(b"\x1A\x45\xDF\xA3", b"\x42\x86\x81\x01")
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
