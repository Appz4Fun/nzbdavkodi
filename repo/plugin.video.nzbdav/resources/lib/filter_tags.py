# SPDX-License-Identifier: GPL-3.0-or-later
"""Precise media-tag recognition shared by normal and fallback title parsing."""

import re

from resources.lib.filter_languages import title_languages


def _pattern(expression):
    return re.compile(r"(?<![a-z0-9])(?:" + expression + r")(?![a-z0-9])", re.I)


_HDR_PLUS = _pattern(r"hdr(?:10)?(?:[. _-]?plus|[. _-]?\+)|hdr10p")
_HDR = _pattern(r"hdr(?:10)?")
_DV = _pattern(r"dv|dovi|dolby[. _-]?vision")
_HLG = _pattern(r"hlg")
_SDR = _pattern(r"sdr")
_RESOLUTION = _pattern(r"\d{3,4}x\d{3,4}|\d{3,4}[pi]|[48]k|qhd")

_AUDIO_PATTERNS = tuple(
    (name, _pattern(expression))
    for name, expression in [
        ("Atmos", r"atmos"),
        ("TrueHD", r"true[. _-]?hd"),
        ("DTS:X", r"dts[. :_-]?x"),
        ("DTS-HD MA", r"dts[. _-]?hd[. _-]?(?:ma|master(?:[. _-]?audio)?)"),
        ("DTS-HD HR", r"dts[. _-]?hd[. _-]?(?:hra?|high[. _-]?resolution)"),
        ("DTS", r"dts(?![. _:-]?(?:hd|x)\b)"),
        (
            "DD+",
            r"e[. _-]?ac[. _-]?3(?:x\d+)?|dd2?(?:p|\+)(?:[1-9][. _]?\d)?"
            r"|dd[. _-]?plus"
            r"|dolby[. _-]?digital[. _-]?plus",
        ),
        (
            "DD",
            r"ac[. _-]?3(?:x\d+)?|dd(?:[1257][. _]?\d)?|dolby[. _-]?(?:digital|d)",
        ),
        ("AAC", r"(?:he[. _-]?|q{1,2})?aac(?:[1257][. _]?\d|2)?(?:x\d+)?"),
        ("FLAC", r"flac(?:[1257][. _]?\d)?(?:x\d+)?"),
        ("PCM", r"l?pcm(?:[1257][. _]?\d)?"),
        ("OPUS", r"opus(?:[1257][. _]?\d)?"),
        ("MP3", r"mp3"),
        ("ALAC", r"alac"),
        ("Vorbis", r"vorbis"),
        ("MP2", r"mp2"),
        ("WMA", r"wma(?:[. _-]?pro)?"),
        ("AC-4", r"ac[. _-]?4"),
    ]
)

_CODEC_PATTERNS = tuple(
    (name, _pattern(expression))
    for name, expression in [
        ("VVC", r"vvc|h[. _-]?266"),
        ("x265/HEVC", r"[xh][. _-]?265|hevc(?:10(?:bit)?)?"),
        ("x264/AVC", r"[xh][. _-]?264|avc"),
        ("AV1", r"av1"),
        ("VP9", r"vp[. _-]?9"),
        ("VP8", r"vp[. _-]?8"),
        ("VC-1", r"vc[. _-]?1|wvc1"),
        ("MPEG-4 ASP", r"xvid|divx|mpeg[. _-]?4(?:[. _-]?asp)?"),
        ("MPEG-2", r"mpeg[. _-]?2|h[. _-]?262"),
        ("MPEG-1", r"mpeg[. _-]?1"),
        ("WMV", r"wmv[139]?"),
        ("H.263", r"h[. _-]?263"),
        ("MJPEG", r"mjpeg|mjpg"),
        ("Theora", r"theora"),
    ]
)


def detect_hdr(title):
    """Only explicit title tags identify HDR or SDR; missing tags stay unknown."""
    values = ["Dolby Vision"] if _DV.search(title) else []
    plus = bool(_HDR_PLUS.search(title))
    if plus:
        values.append("HDR10+")
    # Remove HDR+ tokens before looking for a separately declared HDR base.
    if _HDR.search(_HDR_PLUS.sub("", title)):
        values.append("HDR10")
    if _HLG.search(title):
        values.append("HLG")
    if _SDR.search(title):
        values.append("SDR")
    return values


def detect_audio(title):
    """Consume specific tags before broad tags, avoiding DTS/DD mislabeling."""
    values = []
    remaining = title
    for name, pattern in _AUDIO_PATTERNS:
        if pattern.search(remaining):
            values.append(name)
            remaining = pattern.sub(" ", remaining)
    return values


def detect_codec(title):
    """Return the first explicit codec tag, retaining unknown when absent."""
    return next(
        (name for name, pattern in _CODEC_PATTERNS if pattern.search(title)), ""
    )


def normalize_resolution(value):
    """Normalize dimensions, 4K/8K, and interlaced aliases into filter buckets."""
    value = value.lower().rstrip("p") if "x" in value.lower() else value.lower()
    if "x" in value:
        width, _, height = value.partition("x")
        value = {
            "7680": "4320",
            "3840": "2160",
            "2560": "1440",
            "1920": "1080",
            "1280": "720",
        }.get(width, height) + "p"
    value = {"4k": "2160p", "8k": "4320p", "qhd": "1440p"}.get(value, value)
    return value[:-1] + "p" if value.endswith("i") else value


def supplement_metadata(title, meta):
    """Correct media metadata consistently without modifying vendored PTT."""
    match = _RESOLUTION.search(title)
    meta["resolution"] = normalize_resolution(
        match.group() if match else meta["resolution"]
    )
    meta["hdr"] = detect_hdr(title)
    audio = detect_audio(title)
    if audio:
        # Correct PTT's DTS/DD family labels, but retain other formats and
        # aliases that its parser recognizes beyond the supplemental patterns.
        replaced = set(audio)
        if any(value.startswith("DTS") for value in audio):
            replaced.update(("DTS", "DTS-HD MA", "DTS-HD HR", "DTS:X"))
        if replaced.intersection(("DD", "DD+")):
            replaced.update(("DD", "DD+"))
        meta["audio"] = audio + [
            value for value in meta["audio"] if value not in replaced
        ]
    meta["codec"] = detect_codec(title) or meta["codec"]
    meta["languages"] = title_languages(title, meta["languages"])
    return meta
