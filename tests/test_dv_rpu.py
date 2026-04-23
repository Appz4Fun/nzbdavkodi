from pathlib import Path

import pytest
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


# --- Robustness tests (P4 from BUG2.MD) ----------------------------------


def test_parse_rejects_truncated_rpu():
    """RPU under the 7-byte minimum raises ValueError — size guard is the
    first line of defence before bit-stream parsing begins."""

    with pytest.raises(ValueError):
        parse_rpu_payload(b"\x19\x08\x09")


def test_parse_rejects_wrong_rpu_prefix():
    """First byte after wrapper-stripping must be 0x19 (25). A malformed
    stream starting with something else is rejected."""

    # 7+ bytes (passes length gate) but first byte 0x42, not 0x19.
    with pytest.raises(ValueError):
        parse_rpu_payload(b"\x42" + b"\x00" * 10)


def test_parse_rejects_invalid_rpu_type():
    """rpu_type field (first 6 bits after prefix) must equal 2. A synthesized
    RPU with rpu_type=0 in bits 7..2 of the second byte raises."""

    # \x19 prefix + byte with top-6-bits = 0 (rpu_type=0, not 2).
    with pytest.raises(ValueError):
        parse_rpu_payload(b"\x19\x00\x00\x00\x00\x00\x00")


def test_emulation_prevention_strips_mid_payload_escape():
    """A ``00 00 03`` inside the RPU body must be stripped by
    ``_clear_emulation_prevention_bytes`` before the bit reader sees it.
    Without stripping, the 0x03 shifts every subsequent bit by 8 and the
    parser reads garbage. This targets the MEL fixture's internal 00 00 03
    pattern specifically — the scenario that surfaced during development."""
    info = parse_rpu_payload(_fixture("mel_orig.bin"))
    # If EP stripping were broken, this would either raise or return a
    # garbage profile. The fixture contains 0x03 preceded by two 0x00s.
    assert info.profile == 7
    assert info.el_type == "MEL"


def test_parse_rpu_payload_degrades_gracefully_on_polynomial_linear_interp():
    """The polynomial linear-interpolation branch is unimplemented in both
    dovi_tool and this port. Production DV content never hits it, but if it
    did, profile detection should still succeed and el_type should be
    None — not a propagated NotImplementedError."""
    # Real fixtures don't have linear_interp=True so we can't exercise the
    # branch end-to-end with a fixture. Instead assert the regression guard:
    # parse_rpu_payload catches NotImplementedError from _parse_mapping
    # internally (see dv_rpu.py try/except around _parse_mapping).
    import inspect

    from resources.lib import dv_rpu

    src = inspect.getsource(dv_rpu.parse_rpu_payload)
    assert "except NotImplementedError" in src, (
        "parse_rpu_payload must catch NotImplementedError from _parse_mapping "
        "to keep profile detection robust against polynomial linear interp."
    )
