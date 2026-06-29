# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Regex-based fallback title parsing for when PTT fails or returns empty.

Split out of :mod:`resources.lib.filter` to keep that module under the
file-size guard. These helpers are self-contained: they depend only on the
compiled regexes defined here and call no name that tests patch on
``resources.lib.filter``. ``filter`` re-exports them so existing
``resources.lib.filter.<name>`` references keep resolving.
"""

import re

# ---------------------------------------------------------------------------
# Fallback parse compiled regexes
# ---------------------------------------------------------------------------

_RE_RES = re.compile(r"(?i)\b(2160p|1080p|1080i|720p|480p|4K)\b")
_RE_CODEC = re.compile(r"(?i)\b(x265|h\.?265|hevc|x264|h\.?264|avc|av1|vp9)\b")
_RE_ATMOS = re.compile(r"(?i)\batmos\b")
_RE_TRUEHD = re.compile(r"(?i)\btruehd\b")
_RE_DTSHD = re.compile(r"(?i)\bdts[-. ]?hd[-. ]?ma\b")
_RE_DDPLUS = re.compile(r"(?i)\bddp?5[. ]1|eac3|dd\+|dolby.digital.plus\b")
_RE_DD = re.compile(r"(?i)\bac3|dd[. ]?5[. ]1|dolby.digital\b")
_RE_AAC = re.compile(r"(?i)\baac\b")
_RE_DTS = re.compile(r"(?i)\bdts\b")
_RE_DV = re.compile(r"(?i)\b(dv|dovi|dolby[. ]?vision)\b")
_RE_HDR10PLUS = re.compile(r"(?i)\b(hdr10\+|hdr10plus)\b")
_RE_HDR10 = re.compile(r"(?i)\bhdr10\b")
_RE_HLG = re.compile(r"(?i)\bhlg\b")
_RE_QUALITY = re.compile(
    r"(?i)\b(remux|blu[-. ]?ray|bdrip|web[-. ]?dl|webrip|hdtv|dvdrip|hdrip)\b"
)
_RE_EDITION = re.compile(
    r"(?i)\b(uncut|unrated|director'?s[. ]?cut|extended[. ]?cut"
    r"|recut|theatrical|imax|special[. ]?edition)\b"
)
_RE_CHANNELS = re.compile(r"\b(7\.1|5\.1|2\.0)\b")
_RE_YEAR = re.compile(r"[. (](\d{4})[. )]")
_RE_UPSCALED = re.compile(r"(?i)\bupscale[d]?\b")
_RE_GROUP = re.compile(r"-([A-Za-z0-9][A-Za-z0-9_-]*)(?:\.[a-z]{2,4})?$")

_FALLBACK_AUDIO_TAGS = (
    (_RE_ATMOS, "Atmos"),
    (_RE_TRUEHD, "TrueHD"),
    (_RE_DTSHD, "DTS-HD MA"),
    (_RE_DDPLUS, "DD+"),
    (_RE_DD, "DD"),
    (_RE_AAC, "AAC"),
)


def _fallback_audio(t):
    """Detect audio tags via regex; mirrors PTT's audio list ordering."""
    audio = [label for regex, label in _FALLBACK_AUDIO_TAGS if regex.search(t)]
    if _RE_DTS.search(t) and not audio:
        audio.append("DTS")
    return audio


def _fallback_hdr(t):
    """Detect HDR tags via regex."""
    hdr = []
    if _RE_DV.search(t):
        hdr.append("DV")
    # HDR10+ alternation needs both branches anchored to a leading word
    # boundary so we don't pick up substrings inside another token.
    if _RE_HDR10PLUS.search(t):
        hdr.append("HDR10+")
    # `hdr10` without the optional `0` would match `hdr1`; require the digit.
    elif _RE_HDR10.search(t):
        hdr.append("HDR10")
    if _RE_HLG.search(t):
        hdr.append("HLG")
    return hdr


def _fallback_quality(t):
    """Map a quality/source regex hit to a normalized label, or ""."""
    m = _RE_QUALITY.search(t)
    if not m:
        return ""
    raw_q = m.group(1).upper().replace(" ", "").replace(".", "").replace("-", "")
    if "REMUX" in raw_q:
        return "BluRay REMUX"
    if "BLURAY" in raw_q or "BDRIP" in raw_q:
        return "BluRay"
    if "WEBDL" in raw_q:
        return "WEB-DL"
    if "WEBRIP" in raw_q:
        return "WEBRip"
    if "HDTV" in raw_q:
        return "HDTV"
    return raw_q


def _fallback_year(t):
    """Parse a plausible release year from the title, or 0.

    Range chosen broadly enough that this isn't a time bomb the next
    time we forget to bump it (TODO.md §H.2-M44 was the previous
    bump — 2030 turned out to be too tight). 2100 is well past any
    plausible release window for content this addon would index.
    """
    m = _RE_YEAR.search(t)
    if not m:
        return 0
    yr = int(m.group(1))
    if 1920 <= yr <= 2100:
        return yr
    return 0


def _fallback_parse(title):
    """Simple regex fallback when PTT fails or returns empty."""
    result = {
        "resolution": "",
        "codec": "",
        "audio": [],
        "hdr": [],
        "languages": [],
        "group": "",
        "quality": "",
        "edition": "",
        "channels": "",
        "year": 0,
        "upscaled": False,
    }

    t = title.replace("[", ".").replace("]", ".").replace("(", ".").replace(")", ".")

    m = _RE_RES.search(t)
    if m:
        result["resolution"] = m.group(1)

    m = _RE_CODEC.search(t)
    if m:
        result["codec"] = m.group(1).lower()

    result["audio"] = _fallback_audio(t)
    result["hdr"] = _fallback_hdr(t)
    result["quality"] = _fallback_quality(t)

    m = _RE_EDITION.search(t)
    if m:
        result["edition"] = m.group(1).replace(".", " ")

    m = _RE_CHANNELS.search(t)
    if m:
        result["channels"] = m.group(1)

    result["year"] = _fallback_year(t)

    if _RE_UPSCALED.search(t):
        result["upscaled"] = True

    # Group (last segment after hyphen). Scene groups can contain hyphens and
    # underscores, e.g. GROUP-NAME or GROUP_NAME.
    m = _RE_GROUP.search(title)
    if m:
        result["group"] = m.group(1)

    return result
