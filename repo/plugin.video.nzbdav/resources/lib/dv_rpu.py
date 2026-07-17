"""Pure-Python Dolby Vision RPU parser and MEL/FEL classifier.

Ports the minimal subset of quietvoid/dovi_tool needed to detect DV profile
and distinguish profile 7 MEL from FEL by parsing the RPU header, mapping
data, and NLQ data. No external dependencies.

Reference (pinned):
    https://github.com/quietvoid/dovi_tool/tree/main/dolby_vision/src/rpu
    Last cross-verified against upstream main @ commit
    e7bef8d979a3a975a5eb6930c25e07e554cecee9 (2026-04-23).

    When the upstream parser changes, re-run ``dovi_tool info --frame 0``
    against ``tests/fixtures/dovi/*.bin`` and compare to
    ``parse_rpu_payload()`` output. Any divergence means this port needs
    updating; the fixtures are frozen and cannot drift.

Edge cases worth knowing about:

* ``use_prev_vdr_rpu_flag=True`` frames legitimately carry no NLQ data.
  ``parse_rpu_payload`` returns ``DolbyVisionRpuInfo(profile=7, el_type=None)``
  for them. Callers that sample only one frame may land on such a frame and
  fail to classify MEL/FEL — the classifier is frame-local, but el_type is
  not. Callers needing high-confidence classification should either probe
  multiple frames or accept the el_type=None result as "profile known,
  EL type unknown".
"""

from dataclasses import dataclass
from typing import Optional

_NUM_COMPONENTS = 3
_MMR_MAX_COEFFS = 7
_NLQ_NUM_PIVOTS = 2


@dataclass
class DolbyVisionRpuInfo:
    """Structured DV classification: detected profile and optional EL type."""

    profile: int
    el_type: Optional[str] = None


class _BitReader:
    # `read_ue` reads a leading-zero-then-one Exp-Golomb prefix. A truncated
    # or adversarial payload could yield a stream of zeros that never
    # terminates; this caps the prefix length so the loop can't run away.
    # The H.265 spec uses up to ue(31) (32-bit values) so this is an order
    # of magnitude above any legitimate input.
    _MAX_UE_PREFIX_BITS = 64

    def __init__(self, data):
        self.data = data
        self.bit_pos = 0

    def read_bit(self):
        """Read and return the next bit MSB-first; raise ValueError if truncated."""
        byte_index = self.bit_pos // 8
        if byte_index >= len(self.data):
            # Truncated payload — caller (`parse_unspec62_nalu`) wraps
            # ValueError into a soft "could not parse" return, so a raw
            # IndexError must not escape here.
            raise ValueError(
                "RPU bitstream truncated at bit {} (data is {} bytes)".format(
                    self.bit_pos, len(self.data)
                )
            )
        bit_index = 7 - (self.bit_pos % 8)
        self.bit_pos += 1
        return (self.data[byte_index] >> bit_index) & 1

    def read_bits(self, count):
        """Read ``count`` bits MSB-first and return them as an unsigned integer."""
        value = 0
        for _ in range(count):
            value = (value << 1) | self.read_bit()
        return value

    def read_ue(self):
        """Read and return an unsigned Exp-Golomb ``ue(v)`` value."""
        zeros = 0
        while self.read_bit() == 0:
            zeros += 1
            if zeros > self._MAX_UE_PREFIX_BITS:
                raise ValueError(
                    "RPU ue(v) prefix exceeded {} bits — payload likely "
                    "truncated or malformed".format(self._MAX_UE_PREFIX_BITS)
                )
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.read_bits(zeros)

    def read_se(self):
        """Read and return a signed Exp-Golomb ``se(v)`` value."""
        value = self.read_ue()
        if value % 2 == 0:
            return -(value // 2)
        return (value + 1) // 2

    def read_var(self, bit_count):
        """Read ``bit_count`` bits; alias for read_bits kept for dovi_tool parity."""
        # Alias for read_bits, preserved for grep parity with dovi_tool's
        # `read_var` naming in rpu_data_mapping.rs / rpu_data_nlq.rs.
        return self.read_bits(bit_count)


@dataclass
class _RpuHeader:
    rpu_format: int
    vdr_rpu_profile: int
    coefficient_data_type: int
    coefficient_log2_denom_length: int
    bl_bit_depth_minus8: int
    el_bit_depth_minus8: int
    vdr_bit_depth_minus8: int
    bl_video_full_range_flag: bool
    el_spatial_resampling_filter_flag: bool
    disable_residual_flag: bool
    vdr_dm_metadata_present_flag: bool
    use_prev_vdr_rpu_flag: bool

    def get_dovi_profile(self):
        """Return the Dolby Vision profile derived from the header fields."""
        if self.vdr_rpu_profile == 0:
            return 5 if self.bl_video_full_range_flag else 0
        if self.vdr_rpu_profile == 1:
            dual_layer = (
                self.el_spatial_resampling_filter_flag
                and not self.disable_residual_flag
            )
            if dual_layer:
                return 7 if self.vdr_bit_depth_minus8 == 4 else 4
            return 8
        return 0


@dataclass
class _RpuNlq:
    nlq_offset: list
    vdr_in_max_int: list
    vdr_in_max: list
    linear_deadzone_slope_int: list
    linear_deadzone_slope: list
    linear_deadzone_threshold_int: list
    linear_deadzone_threshold: list

    def is_mel(self):
        """Return True when the NLQ fields match the MEL pattern (not FEL)."""
        # MEL has every NLQ field zeroed except vdr_in_max_int, which is all
        # ones. Each (values, expected) pair must hold across all components.
        checks = (
            (self.nlq_offset, 0),
            (self.vdr_in_max_int, 1),
            (self.vdr_in_max, 0),
            (self.linear_deadzone_slope_int, 0),
            (self.linear_deadzone_slope, 0),
            (self.linear_deadzone_threshold_int, 0),
            (self.linear_deadzone_threshold, 0),
        )
        return all(all(v == expected for v in values) for values, expected in checks)

    def el_type(self):
        """Return ``"MEL"`` or ``"FEL"`` from the NLQ classification."""
        return "MEL" if self.is_mel() else "FEL"


def _validated_rpu_payload(data):
    """Strip known NAL/Annex-B wrappers until the stream starts at the 0x19 RPU prefix.

    Handles:
      - 4-byte Annex B start code + 0x19 (``00 00 00 01 19``)
      - 3-byte Annex B start code + 0x19 (``00 00 01 19``)
      - 2-byte HEVC UNSPEC62 NAL header (``7c 01`` or ``00 01``)
      - 1-byte wrapper (``01``)

    ``7c 01`` may wrap a payload that itself has an Annex B prefix, so the
    loop keeps stripping until it finds the 0x19 byte or runs out of patterns.
    """
    if len(data) < 7:
        raise ValueError("RPU data too short")

    while True:
        result, stripped = _strip_one_rpu_wrapper(data)
        if result is not None:
            return result
        if stripped is None:
            return data
        data = stripped


def _strip_one_rpu_wrapper(data):
    """Strip one RPU wrapper layer from ``data``.

    Returns ``(rpu_bytes, None)`` once the stream starts at the 0x19 RPU
    prefix (caller stops), ``(None, remaining)`` when a continuable wrapper
    (``7c 01`` / ``00 01`` / ``01``) was stripped, and ``(None, None)`` when
    no known wrapper matches (caller returns ``data`` unchanged).
    """
    if data[:5] == b"\x00\x00\x00\x01\x19":
        return data[4:], None
    if data[:4] == b"\x00\x00\x01\x19":
        return data[3:], None
    if data[:1] == b"\x19":
        return data, None
    if len(data) >= 2 and data[:2] in (b"\x7c\x01", b"\x00\x01"):
        return None, data[2:]
    if data[:1] == b"\x01":
        return None, data[1:]
    return None, None


def _clear_emulation_prevention_bytes(data):
    """Remove HEVC start-code emulation prevention bytes.

    The encoder inserts ``0x03`` after any ``0x00 0x00`` pair in the payload
    so the bit stream cannot accidentally produce a ``00 00 01`` start-code
    prefix. The decoder must strip those bytes before parsing.
    """
    out = bytearray()
    zero_run = 0
    for byte in data:
        if zero_run >= 2 and byte == 0x03:
            zero_run = 0
            continue
        out.append(byte)
        zero_run = zero_run + 1 if byte == 0x00 else 0
    return bytes(out)


def _default_seq_info():
    """Return the vdr_seq_info field defaults used when the block is absent."""
    return {
        "coefficient_data_type": 0,
        "coefficient_log2_denom_length": 0,
        "bl_bit_depth_minus8": 2,
        "el_bit_depth_minus8": 2,
        "vdr_bit_depth_minus8": 4,
        "bl_video_full_range_flag": False,
        "el_spatial_resampling_filter_flag": False,
        "disable_residual_flag": True,
    }


def _parse_bit_depths(reader, fields):
    """Read the bit-depth/resampling sub-block into ``fields`` in place."""
    fields["bl_bit_depth_minus8"] = reader.read_ue()
    # dovi_tool splits this ue into the low 8 bits (el_bit_depth_minus8) and
    # the next 8 bits (ext_mapping_idc). We only need the low 8 for MEL/FEL
    # classification, so the upper bits are discarded.
    fields["el_bit_depth_minus8"] = reader.read_ue() & 0xFF
    fields["vdr_bit_depth_minus8"] = reader.read_ue()
    reader.read_bit()  # spatial_resampling_filter_flag
    reader.read_bits(3)  # reserved_zero_3bits
    fields["el_spatial_resampling_filter_flag"] = bool(reader.read_bit())
    fields["disable_residual_flag"] = bool(reader.read_bit())


def _parse_seq_info(reader, rpu_format):
    """Parse the vdr_seq_info block, returning the derived header fields dict."""
    fields = _default_seq_info()
    reader.read_bit()  # chroma_resampling_explicit_filter_flag
    coefficient_data_type = reader.read_bits(2)
    fields["coefficient_data_type"] = coefficient_data_type
    coefficient_log2_denom = reader.read_ue() if coefficient_data_type == 0 else 0
    reader.read_bits(2)  # vdr_rpu_normalized_idc
    fields["bl_video_full_range_flag"] = bool(reader.read_bit())

    if rpu_format & 0x700 == 0:
        _parse_bit_depths(reader, fields)

    if coefficient_data_type == 0:
        fields["coefficient_log2_denom_length"] = coefficient_log2_denom
    elif coefficient_data_type == 1:
        fields["coefficient_log2_denom_length"] = 32
    else:
        raise ValueError("invalid coefficient_data_type")
    return fields


def _parse_header(reader):
    rpu_type = reader.read_bits(6)
    if rpu_type != 2:
        raise ValueError("rpu_type must be 2")

    rpu_format = reader.read_bits(11)
    vdr_rpu_profile = reader.read_bits(4)
    reader.read_bits(4)  # vdr_rpu_level
    vdr_seq_info_present_flag = bool(reader.read_bit())

    if vdr_seq_info_present_flag:
        fields = _parse_seq_info(reader, rpu_format)
    else:
        fields = _default_seq_info()

    vdr_dm_metadata_present_flag = bool(reader.read_bit())
    use_prev_vdr_rpu_flag = bool(reader.read_bit())
    if use_prev_vdr_rpu_flag:
        reader.read_ue()

    return _RpuHeader(
        rpu_format=rpu_format,
        vdr_rpu_profile=vdr_rpu_profile,
        vdr_dm_metadata_present_flag=vdr_dm_metadata_present_flag,
        use_prev_vdr_rpu_flag=use_prev_vdr_rpu_flag,
        **fields,
    )


def _parse_polynomial_curve(reader, header):
    poly_order_minus1 = reader.read_ue()
    if poly_order_minus1 > 1:
        raise ValueError("poly_order_minus1 must be <= 1")

    linear_interp_flag = False
    if poly_order_minus1 == 0:
        linear_interp_flag = bool(reader.read_bit())
    if linear_interp_flag:
        # dovi_tool has this branch unimplemented too; no public content uses it.
        raise NotImplementedError("polynomial linear interpolation not supported")

    poly_coef_count = poly_order_minus1 + 2
    for _ in range(poly_coef_count):
        if header.coefficient_data_type == 0:
            reader.read_se()
        reader.read_var(header.coefficient_log2_denom_length)


def _parse_mmr_curve(reader, header):
    mmr_order_minus1 = reader.read_bits(2)
    if mmr_order_minus1 > 2:
        raise ValueError("mmr_order_minus1 must be <= 2")

    if header.coefficient_data_type == 0:
        reader.read_se()
    reader.read_var(header.coefficient_log2_denom_length)

    for _ in range(mmr_order_minus1 + 1):
        for _ in range(_MMR_MAX_COEFFS):
            if header.coefficient_data_type == 0:
                reader.read_se()
            reader.read_var(header.coefficient_log2_denom_length)


def _parse_mapping_pivots(reader, bl_bit_depth):
    """Read each component's pivot values, returning the per-component piece count."""
    num_pieces_per_cmp = []
    for _ in range(_NUM_COMPONENTS):
        num_pivots_minus2 = reader.read_ue()
        num_pieces_per_cmp.append(num_pivots_minus2 + 1)
        for _ in range(num_pivots_minus2 + 2):
            reader.read_var(bl_bit_depth)
    return num_pieces_per_cmp


def _parse_mapping_curves(reader, header, num_pieces_per_cmp):
    """Parse each piece's polynomial/MMR mapping curve."""
    for num_pieces in num_pieces_per_cmp:
        for _ in range(num_pieces):
            mapping_idc = reader.read_ue()
            if mapping_idc == 0:
                _parse_polynomial_curve(reader, header)
            elif mapping_idc == 1:
                _parse_mmr_curve(reader, header)
            else:
                raise ValueError("unknown mapping_idc {}".format(mapping_idc))


def _parse_mapping(reader, header):
    """Parse rpu_data_mapping() and return whether NLQ data follows."""
    reader.read_ue()  # vdr_rpu_id
    reader.read_ue()  # mapping_color_space
    reader.read_ue()  # mapping_chroma_format_idc

    bl_bit_depth = header.bl_bit_depth_minus8 + 8
    num_pieces_per_cmp = _parse_mapping_pivots(reader, bl_bit_depth)

    has_nlq = (header.rpu_format & 0x700 == 0) and not header.disable_residual_flag
    if has_nlq:
        nlq_method_idc = reader.read_bits(3)
        if nlq_method_idc != 0:
            raise ValueError("nlq_method_idc must be 0 (LinearDeadzone)")
        for _ in range(_NLQ_NUM_PIVOTS):
            reader.read_var(bl_bit_depth)

    reader.read_ue()  # num_x_partitions_minus1
    reader.read_ue()  # num_y_partitions_minus1

    _parse_mapping_curves(reader, header, num_pieces_per_cmp)
    return has_nlq


def _parse_nlq(reader, header):
    """Parse rpu_data_nlq() — one iteration per component, one pivot each."""
    el_bit_depth = header.el_bit_depth_minus8 + 8
    coef_len = header.coefficient_log2_denom_length

    nlq_offset = []
    vdr_in_max_int = []
    vdr_in_max = []
    slope_int = []
    slope = []
    threshold_int = []
    threshold = []

    for _ in range(_NUM_COMPONENTS):
        nlq_offset.append(reader.read_var(el_bit_depth))

        if header.coefficient_data_type == 0:
            vdr_in_max_int.append(reader.read_ue())
        else:
            vdr_in_max_int.append(0)
        vdr_in_max.append(reader.read_var(coef_len))

        if header.coefficient_data_type == 0:
            slope_int.append(reader.read_ue())
        else:
            slope_int.append(0)
        slope.append(reader.read_var(coef_len))

        if header.coefficient_data_type == 0:
            threshold_int.append(reader.read_ue())
        else:
            threshold_int.append(0)
        threshold.append(reader.read_var(coef_len))

    return _RpuNlq(
        nlq_offset=nlq_offset,
        vdr_in_max_int=vdr_in_max_int,
        vdr_in_max=vdr_in_max,
        linear_deadzone_slope_int=slope_int,
        linear_deadzone_slope=slope,
        linear_deadzone_threshold_int=threshold_int,
        linear_deadzone_threshold=threshold,
    )


def parse_rpu_payload(data):
    """Parse a raw RPU byte stream and return the classification result.

    Args:
        data: Raw RPU bytes, optionally prefixed with Annex-B start codes
            (``00 00 00 01`` or ``00 00 01``), the HEVC UNSPEC62 NAL header
            (``7c 01``), or a single-byte wrapper. Emulation prevention
            ``0x03`` bytes are stripped before parsing.

    Returns:
        :class:`DolbyVisionRpuInfo` with the detected DV profile and, for
        profile 7 with decodable NLQ, the ``MEL``/``FEL`` EL type. Non-P7
        profiles or frames where ``use_prev_vdr_rpu_flag`` is set return
        ``el_type=None``.

    Raises:
        ValueError: malformed RPU (bad prefix, truncated header, wrong
            rpu_type, invalid coefficient_data_type).

    Note:
        If the polynomial mapping data uses linear interpolation, the RPU
        is considered successfully profile-detected but NLQ parsing is
        skipped — so MEL/FEL detection returns None for those (extremely
        rare) frames, rather than raising.
    """
    payload = _validated_rpu_payload(data)
    if not payload or payload[0] != 25:
        raise ValueError("Invalid RPU prefix")

    payload = _clear_emulation_prevention_bytes(payload)
    reader = _BitReader(payload[1:])
    header = _parse_header(reader)
    profile = header.get_dovi_profile()

    if profile != 7 or header.use_prev_vdr_rpu_flag:
        return DolbyVisionRpuInfo(profile=profile)

    try:
        has_nlq = _parse_mapping(reader, header)
    except NotImplementedError:
        # Polynomial linear interpolation isn't supported in dovi_tool either.
        # Profile detection already succeeded — degrade gracefully to
        # "profile known, EL type not classifiable".
        return DolbyVisionRpuInfo(profile=profile)

    if not has_nlq:
        return DolbyVisionRpuInfo(profile=profile)

    nlq = _parse_nlq(reader, header)
    return DolbyVisionRpuInfo(profile=profile, el_type=nlq.el_type())


def parse_unspec62_nalu(data):
    """Parse an HEVC UNSPEC62 NAL unit payload (with or without outer wrappers).

    Thin alias for :func:`parse_rpu_payload`; the wrapper-stripping logic
    accepts both raw RPU bytes and bytes prefixed with the UNSPEC62 NAL
    header, so callers can pass either shape.
    """
    return parse_rpu_payload(data)
