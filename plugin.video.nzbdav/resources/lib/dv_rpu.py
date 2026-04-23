"""Pure-Python Dolby Vision RPU parser and MEL/FEL classifier.

Ports the minimal subset of quietvoid/dovi_tool needed to detect DV profile
and distinguish profile 7 MEL from FEL by parsing the RPU header, mapping
data, and NLQ data. No external dependencies.

Reference:
    https://github.com/quietvoid/dovi_tool/tree/main/dolby_vision/src/rpu
"""

from dataclasses import dataclass


_NUM_COMPONENTS = 3
_MMR_MAX_COEFFS = 7
_NLQ_NUM_PIVOTS = 2


@dataclass
class DolbyVisionRpuInfo:
    profile: int
    el_type: str = None


class _BitReader:
    def __init__(self, data):
        self.data = data
        self.bit_pos = 0

    def read_bit(self):
        byte_index = self.bit_pos // 8
        bit_index = 7 - (self.bit_pos % 8)
        self.bit_pos += 1
        return (self.data[byte_index] >> bit_index) & 1

    def read_bits(self, count):
        value = 0
        for _ in range(count):
            value = (value << 1) | self.read_bit()
        return value

    def read_ue(self):
        zeros = 0
        while self.read_bit() == 0:
            zeros += 1
        if zeros == 0:
            return 0
        return (1 << zeros) - 1 + self.read_bits(zeros)

    def read_se(self):
        value = self.read_ue()
        if value % 2 == 0:
            return -(value // 2)
        return (value + 1) // 2

    def read_var(self, bit_count):
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
        if self.vdr_rpu_profile == 0:
            return 5 if self.bl_video_full_range_flag else 0
        if self.vdr_rpu_profile == 1:
            if self.el_spatial_resampling_filter_flag and not self.disable_residual_flag:
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
        return (
            all(v == 0 for v in self.nlq_offset)
            and all(v == 1 for v in self.vdr_in_max_int)
            and all(v == 0 for v in self.vdr_in_max)
            and all(v == 0 for v in self.linear_deadzone_slope_int)
            and all(v == 0 for v in self.linear_deadzone_slope)
            and all(v == 0 for v in self.linear_deadzone_threshold_int)
            and all(v == 0 for v in self.linear_deadzone_threshold)
        )

    def el_type(self):
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
        if data[:5] == b"\x00\x00\x00\x01\x19":
            return data[4:]
        if data[:4] == b"\x00\x00\x01\x19":
            return data[3:]
        if data[:1] == b"\x19":
            return data
        if len(data) >= 2 and data[:2] in (b"\x7c\x01", b"\x00\x01"):
            data = data[2:]
            continue
        if data[:1] == b"\x01":
            data = data[1:]
            continue
        return data


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


def _parse_header(reader):
    rpu_type = reader.read_bits(6)
    if rpu_type != 2:
        raise ValueError("rpu_type must be 2")

    rpu_format = reader.read_bits(11)
    vdr_rpu_profile = reader.read_bits(4)
    reader.read_bits(4)  # vdr_rpu_level
    vdr_seq_info_present_flag = bool(reader.read_bit())

    coefficient_data_type = 0
    coefficient_log2_denom_length = 0
    bl_bit_depth_minus8 = 2
    el_bit_depth_minus8 = 2
    vdr_bit_depth_minus8 = 4
    bl_video_full_range_flag = False
    el_spatial_resampling_filter_flag = False
    disable_residual_flag = True

    if vdr_seq_info_present_flag:
        reader.read_bit()  # chroma_resampling_explicit_filter_flag
        coefficient_data_type = reader.read_bits(2)
        coefficient_log2_denom = 0
        if coefficient_data_type == 0:
            coefficient_log2_denom = reader.read_ue()
        reader.read_bits(2)  # vdr_rpu_normalized_idc
        bl_video_full_range_flag = bool(reader.read_bit())

        if rpu_format & 0x700 == 0:
            bl_bit_depth_minus8 = reader.read_ue()
            el_bit_depth_and_ext = reader.read_ue()
            el_bit_depth_minus8 = el_bit_depth_and_ext & 0xFF
            vdr_bit_depth_minus8 = reader.read_ue()
            reader.read_bit()  # spatial_resampling_filter_flag
            reader.read_bits(3)  # reserved_zero_3bits
            el_spatial_resampling_filter_flag = bool(reader.read_bit())
            disable_residual_flag = bool(reader.read_bit())

        if coefficient_data_type == 0:
            coefficient_log2_denom_length = coefficient_log2_denom
        elif coefficient_data_type == 1:
            coefficient_log2_denom_length = 32
        else:
            raise ValueError("invalid coefficient_data_type")

    vdr_dm_metadata_present_flag = bool(reader.read_bit())
    use_prev_vdr_rpu_flag = bool(reader.read_bit())
    if use_prev_vdr_rpu_flag:
        reader.read_ue()

    return _RpuHeader(
        rpu_format=rpu_format,
        vdr_rpu_profile=vdr_rpu_profile,
        coefficient_data_type=coefficient_data_type,
        coefficient_log2_denom_length=coefficient_log2_denom_length,
        bl_bit_depth_minus8=bl_bit_depth_minus8,
        el_bit_depth_minus8=el_bit_depth_minus8,
        vdr_bit_depth_minus8=vdr_bit_depth_minus8,
        bl_video_full_range_flag=bl_video_full_range_flag,
        el_spatial_resampling_filter_flag=el_spatial_resampling_filter_flag,
        disable_residual_flag=disable_residual_flag,
        vdr_dm_metadata_present_flag=vdr_dm_metadata_present_flag,
        use_prev_vdr_rpu_flag=use_prev_vdr_rpu_flag,
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


def _parse_mapping(reader, header):
    """Parse rpu_data_mapping() and return whether NLQ data follows."""
    reader.read_ue()  # vdr_rpu_id
    reader.read_ue()  # mapping_color_space
    reader.read_ue()  # mapping_chroma_format_idc

    bl_bit_depth = header.bl_bit_depth_minus8 + 8
    num_pieces_per_cmp = []
    for _ in range(_NUM_COMPONENTS):
        num_pivots_minus2 = reader.read_ue()
        num_pieces_per_cmp.append(num_pivots_minus2 + 1)
        for _ in range(num_pivots_minus2 + 2):
            reader.read_var(bl_bit_depth)

    has_nlq = (header.rpu_format & 0x700 == 0) and not header.disable_residual_flag
    if has_nlq:
        nlq_method_idc = reader.read_bits(3)
        if nlq_method_idc != 0:
            raise ValueError("nlq_method_idc must be 0 (LinearDeadzone)")
        for _ in range(_NLQ_NUM_PIVOTS):
            reader.read_var(bl_bit_depth)

    reader.read_ue()  # num_x_partitions_minus1
    reader.read_ue()  # num_y_partitions_minus1

    for num_pieces in num_pieces_per_cmp:
        for _ in range(num_pieces):
            mapping_idc = reader.read_ue()
            if mapping_idc == 0:
                _parse_polynomial_curve(reader, header)
            elif mapping_idc == 1:
                _parse_mmr_curve(reader, header)
            else:
                raise ValueError("unknown mapping_idc {}".format(mapping_idc))

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
    """Parse a raw RPU byte stream and return the classification result."""
    payload = _validated_rpu_payload(data)
    if not payload or payload[0] != 25:
        raise ValueError("Invalid RPU prefix")

    payload = _clear_emulation_prevention_bytes(payload)
    reader = _BitReader(payload[1:])
    header = _parse_header(reader)
    profile = header.get_dovi_profile()

    if profile != 7 or header.use_prev_vdr_rpu_flag:
        return DolbyVisionRpuInfo(profile=profile)

    has_nlq = _parse_mapping(reader, header)
    if not has_nlq:
        return DolbyVisionRpuInfo(profile=profile)

    nlq = _parse_nlq(reader, header)
    return DolbyVisionRpuInfo(profile=profile, el_type=nlq.el_type())


def parse_unspec62_nalu(data):
    """Parse an HEVC UNSPEC62 NAL unit payload (with or without outer wrappers)."""
    return parse_rpu_payload(data)
