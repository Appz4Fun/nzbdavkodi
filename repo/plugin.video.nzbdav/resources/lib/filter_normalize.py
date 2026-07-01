# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Normalize PTT / regex-fallback parsed dicts into the metadata contract.

Split out of :mod:`resources.lib.filter` to keep that module under the
file-size guard. ``parse_title_metadata`` (kept in ``filter`` because tests
patch it) calls ``_normalize_parsed_meta`` / ``_normalize_fallback_meta`` from
here. These helpers call no name that tests patch on ``resources.lib.filter``;
this module must not import ``filter`` (no cycle).
"""

_RESOLUTION_MAP = {
    "2160p": "2160p",
    "4K": "2160p",
    "1080p": "1080p",
    "1080i": "1080p",
    "720p": "720p",
    "480p": "480p",
    "480i": "480p",
    "SD": "480p",
}

_HDR_MAP = {
    "HDR": "HDR10",
    "HDR10": "HDR10",
    "HDR10+": "HDR10+",
    "HDR10Plus": "HDR10+",
    "DV": "Dolby Vision",
    "Dolby Vision": "Dolby Vision",
    "DoVi": "Dolby Vision",
    "HLG": "HLG",
}

_AUDIO_MAP = {
    "Atmos": "Atmos",
    "TrueHD": "TrueHD",
    "DTS-HD MA": "DTS-HD MA",
    "DTS-HD": "DTS-HD MA",
    "DTS Lossless": "DTS-HD MA",
    "DTS:X": "DTS:X",
    "DTS-X": "DTS:X",
    "DD+": "DD+",
    "EAC3": "DD+",
    "E-AC-3": "DD+",
    "Dolby Digital Plus": "DD+",
    "DD": "DD",
    "AC3": "DD",
    "AC-3": "DD",
    "Dolby Digital": "DD",
    "DTS Lossy": "DD",
    "AAC": "AAC",
}

_CODEC_MAP = {
    "x265": "x265/HEVC",
    "HEVC": "x265/HEVC",
    "H.265": "x265/HEVC",
    "h265": "x265/HEVC",
    "hevc": "x265/HEVC",
    "x264": "x264/AVC",
    "AVC": "x264/AVC",
    "H.264": "x264/AVC",
    "h264": "x264/AVC",
    "avc": "x264/AVC",
    "AV1": "AV1",
    "av1": "AV1",
    "VP9": "VP9",
    "vp9": "VP9",
    "MPEG2": "MPEG-2",
    "MPEG-2": "MPEG-2",
    "mpeg2": "MPEG-2",
}


def _as_list(value):
    """Coerce a PTT field into a list (wrapping a bare string)."""
    if isinstance(value, str):
        return [value]
    return value


def _normalize_parsed_meta(parsed):
    """Normalize a PTT-style parsed dict into the metadata contract.

    Assumes PTT returned typed data matching its documented contract:
    strings for resolution/codec/group/year, lists (or strings) for
    hdr/audio/languages/channels. May raise TypeError/AttributeError/
    KeyError if PTT drifts from that contract; the caller catches that.
    """
    raw_res = parsed.get("resolution", "") or ""
    resolution = _RESOLUTION_MAP.get(raw_res, raw_res)

    # Dedup HDR / audio lists. PTT can return duplicates when a release
    # name mentions the same token twice (e.g. "Atmos.TrueHD.Atmos");
    # the duplicates broke combo-rank logic that uses set-membership +
    # list-position cues (Atmos+TrueHD combo, language filter). Use a
    # dict-as-ordered-set to preserve PTT's first-occurrence order.
    # Closes TODO.md §H.3.
    raw_hdr = _as_list(parsed.get("hdr", []))
    hdr_list = list(dict.fromkeys(_HDR_MAP.get(h, h) for h in raw_hdr if h))

    raw_audio = _as_list(parsed.get("audio", []))
    audio_list = list(dict.fromkeys(_AUDIO_MAP.get(a, a) for a in raw_audio if a))

    raw_codec = parsed.get("codec", "") or ""
    codec = _CODEC_MAP.get(raw_codec, raw_codec)

    raw_langs = _as_list(parsed.get("languages", []))
    raw_langs = list(dict.fromkeys(raw_langs))  # dedup, preserve order

    raw_channels = _as_list(parsed.get("channels", []))
    channels = raw_channels[0] if raw_channels else ""

    meta = _common_parsed_fields(parsed)
    meta.update(
        {
            "resolution": resolution,
            "hdr": hdr_list,
            "audio": audio_list,
            "codec": codec,
            "languages": raw_langs,
            "channels": channels,
        }
    )
    return meta


def _common_parsed_fields(parsed):
    """Extract the scalar metadata fields shared by both parse paths."""
    return {
        "group": parsed.get("group", "") or "",
        "quality": parsed.get("quality", "") or "",
        "edition": parsed.get("edition", "") or "",
        "proper": bool(parsed.get("proper", False)),
        "repack": bool(parsed.get("repack", False)),
        "year": parsed.get("year", 0) or 0,
        "upscaled": bool(parsed.get("upscaled", False)),
        "container": parsed.get("container", "") or "",
    }


def _mapped_str_list(parsed, key, mapping):
    """Map a PTT field's string items through a lookup table."""
    raw = _as_list(parsed.get(key, []) or [])
    return [mapping.get(item, item) for item in raw if isinstance(item, str)]


def _normalize_fallback_meta(parsed):
    """Normalize a regex-fallback parsed dict (string-only filtering)."""
    raw_res = parsed.get("resolution", "") or ""
    resolution = _RESOLUTION_MAP.get(raw_res, raw_res)
    hdr_list = _mapped_str_list(parsed, "hdr", _HDR_MAP)
    audio_list = _mapped_str_list(parsed, "audio", _AUDIO_MAP)
    codec = _CODEC_MAP.get(parsed.get("codec", ""), parsed.get("codec", ""))
    raw_langs = _as_list(parsed.get("languages", []) or [])
    raw_channels = _as_list(parsed.get("channels", []) or [])
    channels = raw_channels[0] if raw_channels else ""

    meta = _common_parsed_fields(parsed)
    meta.update(
        {
            "resolution": resolution,
            "hdr": hdr_list,
            "audio": audio_list,
            "codec": codec,
            "languages": raw_langs,
            "channels": channels,
        }
    )
    return meta
