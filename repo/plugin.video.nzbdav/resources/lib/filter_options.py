# SPDX-License-Identifier: GPL-3.0-or-later
"""Selectable media formats and shared Other/unknown filter semantics."""

from resources.lib.filter_languages import LANGUAGES

RESOLUTION_SETTINGS = [
    ("filter_4320p", "4320p"),
    ("filter_2160p", "2160p"),
    ("filter_1440p", "1440p"),
    ("filter_1080p", "1080p"),
    ("filter_720p", "720p"),
    ("filter_576p", "576p"),
    ("filter_540p", "540p"),
    ("filter_480p", "480p"),
    ("filter_360p", "360p"),
    ("filter_240p", "240p"),
]
HDR_SETTINGS = [
    ("filter_hdr10", "HDR10"),
    ("filter_hdr10plus", "HDR10+"),
    ("filter_dolby_vision", "Dolby Vision"),
    ("filter_hlg", "HLG"),
    ("filter_sdr", "SDR"),
]
AUDIO_SETTINGS = [
    ("filter_atmos", "Atmos"),
    ("filter_truehd", "TrueHD"),
    ("filter_dtshd_ma", "DTS-HD MA"),
    ("filter_dtshd_hr", "DTS-HD HR"),
    ("filter_dtsx", "DTS:X"),
    ("filter_dts", "DTS"),
    ("filter_ddplus", "DD+"),
    ("filter_dd", "DD"),
    ("filter_aac", "AAC"),
    ("filter_flac", "FLAC"),
    ("filter_pcm", "PCM"),
    ("filter_opus", "OPUS"),
    ("filter_mp3", "MP3"),
    ("filter_alac", "ALAC"),
    ("filter_vorbis", "Vorbis"),
    ("filter_mp2", "MP2"),
    ("filter_wma", "WMA"),
    ("filter_ac4", "AC-4"),
]
CODEC_SETTINGS = [
    ("filter_hevc", "x265/HEVC"),
    ("filter_avc", "x264/AVC"),
    ("filter_av1", "AV1"),
    ("filter_vp9", "VP9"),
    ("filter_mpeg2", "MPEG-2"),
    ("filter_mpeg4", "MPEG-4 ASP"),
    ("filter_vc1", "VC-1"),
    ("filter_mpeg1", "MPEG-1"),
    ("filter_vp8", "VP8"),
    ("filter_wmv", "WMV"),
    ("filter_h263", "H.263"),
    ("filter_vvc", "VVC"),
    ("filter_mjpeg", "MJPEG"),
    ("filter_theora", "Theora"),
]
LANGUAGE_SETTINGS = [("filter_" + suffix, code) for code, suffix, _, _ in LANGUAGES]

UNKNOWN_SETTINGS = {
    "unknown_resolution": "filter_unknown_resolution",
    "unknown_hdr": "filter_unknown_hdr",
    "unknown_audio": "filter_unknown_audio",
    "unknown_codec": "filter_unknown_codec",
    "unknown_language": "filter_unknown_language",
}
BOOLEAN_SETTINGS = frozenset(
    key
    for specs in (
        RESOLUTION_SETTINGS,
        HDR_SETTINGS,
        AUDIO_SETTINGS,
        CODEC_SETTINGS,
        LANGUAGE_SETTINGS,
    )
    for key, _ in specs
) | frozenset(UNKNOWN_SETTINGS.values())


def values_filter_pass(values, selected, specs, allow_unknown):
    """Keep explicit known formats separate from missing/unlisted metadata.

    An empty selection retains the existing no-restriction behavior for known
    formats. The unknown toggle still independently controls missing metadata.
    """
    known = {value for _, value in specs}
    if not values:
        return allow_unknown
    return any(
        (value in selected or not selected) if value in known else allow_unknown
        for value in values
    )
