from pathlib import Path

from resources.lib.dv_rpu import parse_rpu_payload, parse_unspec62_nalu

FIXTURES = Path(__file__).parent / "fixtures" / "dovi"


def _fixture(name):
    return (FIXTURES / name).read_bytes()


def test_parse_profile7_fel_fixture():
    info = parse_rpu_payload(_fixture("fel_orig.bin"))
    assert info.profile == 7
    assert info.el_type == "FEL"


def test_parse_profile7_mel_fixture():
    info = parse_rpu_payload(_fixture("mel_orig.bin"))
    assert info.profile == 7
    assert info.el_type == "MEL"


def test_parse_profile8_fixture():
    info = parse_rpu_payload(_fixture("profile8.bin"))
    assert info.profile == 8
    assert info.el_type is None


def test_parse_unspec62_nalu_accepts_prefixed_payload():
    payload = _fixture("mel_orig.bin")
    info = parse_unspec62_nalu(b"\x7c\x01" + payload)
    assert info.profile == 7
    assert info.el_type == "MEL"
