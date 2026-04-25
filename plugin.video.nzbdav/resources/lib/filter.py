# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Result filtering and sorting using PTT for title parsing."""

import xbmc

# ---------------------------------------------------------------------------
# Known release groups — master list for multiselect dialogs
# ---------------------------------------------------------------------------

ALL_RELEASE_GROUPS = [
    "4KDVS",
    "Amen",
    "AOC",
    "APEX",
    "B0MBARDiERS",
    "Ben The Men",
    "BHDstudio",
    "BiTOR",
    "BYNDR",
    "c0kE",
    "CiNEPHiLES",
    "CM",
    "CMRG",
    "DDR",
    "DEFLATE",
    "DirtyHippie",
    "DiscoD",
    "DON",
    "DreamHD",
    "DVSUX",
    "EDITH",
    "ENDSTATiON",
    "ETHEL",
    "EVO",
    "FETiSH",
    "FGT",
    "FLUX",
    "FraMeSToR",
    "FrameStor",
    "FW",
    "GalaxyRG",
    "GLHF",
    "Gungnir",
    "hallowed",
    "HDS",
    "HDT",
    "HHWEB",
    "HiDt",
    "HONE",
    "HSaber",
    "IAMABLE",
    "j3rico",
    "KC",
    "Kira",
    "Kitsune",
    "KOGi",
    "KTR",
    "LEGi0N",
    "MainFrame",
    "MgB",
    "MIXED",
    "mkv",
    "mp4",
    "MZABI",
    "NAHOM",
    "Narcos",
    "NBQ",
    "NHTFS",
    "NOGRP",
    "NTb",
    "NUXWIO",
    "P2P",
    "playWEB",
    "PSA",
    "R3MiX",
    "Ralphy",
    "RARBG",
    "SDH",
    "Sensei",
    "SESKAPiLE",
    "SEV",
    "SiC",
    "SMURF",
    "SPHD",
    "SPx",
    "STRiKES",
    "SuccessfulCrab",
    "SUPPLY",
    "SURCODE",
    "SWTYBLZ",
    "TERMiNAL",
    "TEPES",
    "TheBiscuitMan",
    "ToonsHub",
    "TrollUHD",
    "TW",
    "VSEX",
    "W4NK3R",
    "WADU",
    "WiKi",
    "WRB",
    "XEBEC",
    "XXX",
    "ZAX",
]

DEFAULT_PREFERRED_GROUPS = {
    "CiNEPHiLES",
    "DiscoD",
    "DON",
    "FrameStor",
    "hallowed",
    "HiDt",
    "HONE",
    "j3rico",
    "Kira",
    "MainFrame",
    "SEV",
    "SPHD",
    "W4NK3R",
}

DEFAULT_EXCLUDED_GROUPS = {
    "4KDVS",
    "B0MBARDiERS",
    "Ben The Men",
    "BHDstudio",
    "BiTOR",
    "c0kE",
    "ENDSTATiON",
    "Gungnir",
    "HDS",
    "HSaber",
    "NUXWIO",
    "Ralphy",
    "SESKAPiLE",
    "SPx",
    "STRiKES",
    "SURCODE",
    "TW",
    "WiKi",
    "ZAX",
}

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


def _collect_enabled(addon, pairs):
    """Return labels for settings that are enabled (true).

    Args:
        addon: Kodi addon instance
        pairs: list of (setting_id, label) tuples
    """
    return [
        label
        for setting_id, label in pairs
        if addon.getSetting(setting_id).lower() == "true"
    ]


def _csv_setting(addon, key):
    """Read a comma-separated setting into a stripped list."""
    val = addon.getSetting(key).strip()
    if not val:
        return []
    return [x.strip() for x in val.split(",") if x.strip()]


def _int_setting(addon, key, default):
    """Read an integer Kodi setting with a safe fallback."""
    raw = addon.getSetting(key)
    try:
        return int(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        return default


def _get_filter_settings():
    """Read filter settings from Kodi addon config."""
    import xbmcaddon

    addon = xbmcaddon.Addon()

    resolutions = _collect_enabled(
        addon,
        [
            ("filter_2160p", "2160p"),
            ("filter_1080p", "1080p"),
            ("filter_720p", "720p"),
            ("filter_480p", "480p"),
        ],
    )

    hdr = _collect_enabled(
        addon,
        [
            ("filter_hdr10", "HDR10"),
            ("filter_hdr10plus", "HDR10+"),
            ("filter_dolby_vision", "Dolby Vision"),
            ("filter_hlg", "HLG"),
            ("filter_sdr", "SDR"),
        ],
    )

    audio = _collect_enabled(
        addon,
        [
            ("filter_atmos", "Atmos"),
            ("filter_truehd", "TrueHD"),
            ("filter_dtshd_ma", "DTS-HD MA"),
            ("filter_dtsx", "DTS:X"),
            ("filter_ddplus", "DD+"),
            ("filter_dd", "DD"),
            ("filter_aac", "AAC"),
        ],
    )

    codecs = _collect_enabled(
        addon,
        [
            ("filter_hevc", "x265/HEVC"),
            ("filter_avc", "x264/AVC"),
            ("filter_av1", "AV1"),
            ("filter_vp9", "VP9"),
            ("filter_mpeg2", "MPEG-2"),
        ],
    )

    languages = _collect_enabled(
        addon,
        [
            ("filter_english", "English"),
            ("filter_spanish", "Spanish"),
            ("filter_french", "French"),
            ("filter_german", "German"),
            ("filter_italian", "Italian"),
            ("filter_portuguese", "Portuguese"),
            ("filter_dutch", "Dutch"),
            ("filter_russian", "Russian"),
            ("filter_japanese", "Japanese"),
            ("filter_korean", "Korean"),
            ("filter_chinese", "Chinese"),
            ("filter_arabic", "Arabic"),
            ("filter_hindi", "Hindi"),
        ],
    )

    return {
        "resolutions": resolutions,
        "hdr": hdr,
        "audio": audio,
        "codecs": codecs,
        "languages": languages,
        "exclude_keywords": [
            k.lower() for k in _csv_setting(addon, "filter_exclude_keywords")
        ],
        "require_keywords": [
            k.lower() for k in _csv_setting(addon, "filter_require_keywords")
        ],
        "release_group": [
            g.lower() for g in _csv_setting(addon, "filter_release_group")
        ],
        "exclude_release_group": [
            g.lower() for g in _csv_setting(addon, "filter_exclude_release_group")
        ],
        "min_size": _int_setting(addon, "filter_min_size", 0),
        "max_size": _int_setting(addon, "filter_max_size", 0),
        "sort_order": _int_setting(addon, "sort_order", 0),
        "max_results": _int_setting(addon, "max_results", 25),
    }


def parse_title_metadata(title):
    """Parse a scene title and return normalized metadata dict."""
    try:
        from resources.lib.ptt import parse_title

        parsed = parse_title(title)
    except Exception as e:
        xbmc.log(
            "NZB-DAV: PTT parse failed for '{}': {}".format(title, e), xbmc.LOGERROR
        )
        parsed = _fallback_parse(title)

    if not parsed.get("resolution") and not parsed.get("codec"):
        # PTT returned empty, try fallback
        fallback = _fallback_parse(title)
        if fallback.get("resolution") or fallback.get("codec"):
            parsed = fallback

    # The normalization block below assumes PTT returned typed data
    # matching its documented contract: strings for resolution/codec/group/
    # year, lists (or strings) for hdr/audio/languages/channels. If the
    # vendored PTT drifts from that contract (or a custom transformer
    # returns e.g. a dict for hdr), the comprehensions below explode with
    # TypeError. Catch that so a single bad release name doesn't kill the
    # whole search; fall back to the regex-only metadata extractor.
    try:
        raw_res = parsed.get("resolution", "") or ""
        resolution = _RESOLUTION_MAP.get(raw_res, raw_res)

        # Dedup HDR / audio lists. PTT can return duplicates when a release
        # name mentions the same token twice (e.g. "Atmos.TrueHD.Atmos");
        # the duplicates broke combo-rank logic that uses set-membership +
        # list-position cues (Atmos+TrueHD combo, language filter). Use a
        # dict-as-ordered-set to preserve PTT's first-occurrence order.
        # Closes TODO.md §H.3.
        raw_hdr = parsed.get("hdr", [])
        if isinstance(raw_hdr, str):
            raw_hdr = [raw_hdr]
        hdr_list = list(dict.fromkeys(_HDR_MAP.get(h, h) for h in raw_hdr if h))

        raw_audio = parsed.get("audio", [])
        if isinstance(raw_audio, str):
            raw_audio = [raw_audio]
        audio_list = list(dict.fromkeys(_AUDIO_MAP.get(a, a) for a in raw_audio if a))

        raw_codec = parsed.get("codec", "") or ""
        codec = _CODEC_MAP.get(raw_codec, raw_codec)

        raw_langs = parsed.get("languages", [])
        if isinstance(raw_langs, str):
            raw_langs = [raw_langs]
        raw_langs = list(dict.fromkeys(raw_langs))  # dedup, preserve order

        group = parsed.get("group", "") or ""
        quality = parsed.get("quality", "") or ""
        edition = parsed.get("edition", "") or ""
        year = parsed.get("year", 0) or 0
        upscaled = bool(parsed.get("upscaled", False))
        container = parsed.get("container", "") or ""

        raw_channels = parsed.get("channels", [])
        if isinstance(raw_channels, str):
            raw_channels = [raw_channels]
        channels = raw_channels[0] if raw_channels else ""
    except (TypeError, AttributeError, KeyError) as e:
        xbmc.log(
            "NZB-DAV: PTT metadata normalisation failed for '{}': {}; "
            "falling back to regex parse".format(title, e),
            xbmc.LOGWARNING,
        )
        parsed = _fallback_parse(title)
        raw_res = parsed.get("resolution", "") or ""
        resolution = _RESOLUTION_MAP.get(raw_res, raw_res)
        raw_hdr = parsed.get("hdr", []) or []
        if isinstance(raw_hdr, str):
            raw_hdr = [raw_hdr]
        hdr_list = [_HDR_MAP.get(h, h) for h in raw_hdr if isinstance(h, str)]
        raw_audio = parsed.get("audio", []) or []
        if isinstance(raw_audio, str):
            raw_audio = [raw_audio]
        audio_list = [_AUDIO_MAP.get(a, a) for a in raw_audio if isinstance(a, str)]
        codec = _CODEC_MAP.get(parsed.get("codec", ""), parsed.get("codec", ""))
        raw_langs = parsed.get("languages", []) or []
        if isinstance(raw_langs, str):
            raw_langs = [raw_langs]
        group = parsed.get("group", "") or ""
        quality = parsed.get("quality", "") or ""
        edition = parsed.get("edition", "") or ""
        year = parsed.get("year", 0) or 0
        upscaled = bool(parsed.get("upscaled", False))
        container = parsed.get("container", "") or ""
        channels = ""

    return {
        "resolution": resolution,
        "hdr": hdr_list,
        "audio": audio_list,
        "codec": codec,
        "languages": raw_langs,
        "group": group,
        "quality": quality,
        "edition": edition,
        "channels": channels,
        "year": year,
        "upscaled": upscaled,
        "container": container,
    }


def matches_filters(result, meta, settings):
    """True iff every configured filter accepts this result.

    Args:
        result: Indexer result dict with at least ``title`` and ``size``.
        meta: Parsed-metadata dict produced by ``parse_title_metadata``
            (``resolution``, ``hdr`` list, ``audio`` list, ``codec``,
            ``languages`` list).
        settings: Filter-settings dict produced by ``_get_filter_settings``
            (label lists, CSV-keyword lists, size bounds).

    Returns:
        ``True`` when the result satisfies every enabled filter,
        ``False`` the first time any filter excludes it. Pure function
        — does not mutate any input.
    """
    title_lower = result["title"].lower()

    if settings["resolutions"] and meta["resolution"]:
        if meta["resolution"] not in settings["resolutions"]:
            return False

    if settings["hdr"] and meta["hdr"]:
        if not any(h in settings["hdr"] for h in meta["hdr"]):
            return False
    if settings["hdr"] and not meta["hdr"] and "SDR" not in settings["hdr"]:
        return False

    if settings["audio"] and meta["audio"]:
        if not any(a in settings["audio"] for a in meta["audio"]):
            return False

    if settings["codecs"] and meta["codec"]:
        if meta["codec"] not in settings["codecs"]:
            return False

    if settings["languages"] and meta["languages"]:
        if not any(lang in settings["languages"] for lang in meta["languages"]):
            return False

    for kw in settings["exclude_keywords"]:
        if kw in title_lower:
            return False

    for kw in settings["require_keywords"]:
        if kw not in title_lower:
            return False

    if meta["group"] and meta["group"].lower() in [
        g.lower() for g in settings["exclude_release_group"]
    ]:
        return False

    # Size filter: a 0-byte / missing-size placeholder result used to
    # skip both bounds because `if result.get("size"):` is falsy for
    # "0" and "". That let unparseable / placeholder rows slip past
    # min_size when the user wanted to filter them out. Now: reach the
    # size check unconditionally; treat unparseable size as 0 MB so a
    # min_size>0 filter rejects it. Closes TODO.md §H.3.
    raw_size = result.get("size", "")
    try:
        size_mb = int(raw_size) / 1048576 if raw_size not in (None, "") else 0
    except (ValueError, TypeError):
        size_mb = 0
    if settings["min_size"] > 0 and size_mb < settings["min_size"]:
        return False
    if settings["max_size"] > 0 and size_mb > settings["max_size"]:
        return False

    return True


def filter_results(results):
    """Apply filters, sort, truncate. Returns (filtered, all_parsed).

    Side effect: mutates each input dict by attaching a ``_meta`` key
    holding the parsed-title metadata. Callers that iterate ``results``
    after this call will see the extra field. ``all_parsed`` is the
    same list of dicts (with ``_meta`` populated) in sorted order;
    ``filtered`` is the subset that passed every filter, truncated
    to ``settings["max_results"]`` if that is non-zero.
    """
    settings = _get_filter_settings()

    all_parsed = []
    filtered = []
    for result in results:
        meta = parse_title_metadata(result["title"])
        result["_meta"] = meta
        all_parsed.append(result)
        if matches_filters(result, meta, settings):
            filtered.append(result)

    filtered = _sort_results(filtered, settings)
    all_parsed = _sort_results(all_parsed, settings)

    max_results = settings["max_results"]
    if max_results > 0:
        filtered = filtered[:max_results]

    xbmc.log(
        "NZB-DAV: Filtered {} -> {} results".format(len(all_parsed), len(filtered)),
        xbmc.LOGDEBUG,
    )
    return filtered, all_parsed


def _pubdate_sort_key(result):
    """Return a sortable datetime-derived key for RFC-822 pubdate.

    Sorting results by raw ``r.get("pubdate", "")`` gives LEXICOGRAPHIC
    order over strings like ``"Mon, 02 Jan 2006 15:04:05 GMT"`` — which
    puts "Fri" < "Mon" < "Sun" < "Tue" chronologically wrong. Parse to
    a timestamp instead. Unparseable values sort at the epoch so
    malformed entries don't crash and don't jump to the top under
    descending sort.
    """
    from email.utils import parsedate_to_datetime

    raw = result.get("pubdate", "") or ""
    if not raw:
        return 0.0
    try:
        return parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _size_sort_key(result):
    """Return an int-valued size key, tolerating malformed size fields.

    Indexers occasionally return non-numeric ``size`` values (e.g. when
    the NZB's file list omitted byte totals). Previously ``int(...)``
    would crash the entire sort on a single bad entry. Return 0 for
    anything non-parseable so the rest of the list still sorts cleanly.
    """
    raw = result.get("size", 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _sort_results(results, settings):
    """Sort results by configured sort order, with preferred groups boosted.

    Sort orders:
        0 = Relevance (original order)
        1 = Size (largest first)
        2 = Size (smallest first)
        3 = Age (newest first) -- pubdate descending
        4 = Age (oldest first) -- pubdate ascending
    """
    preferred_lower = [g.lower() for g in settings["release_group"]]
    sort_order = settings["sort_order"]

    # Resolution rank: 4K best (0), then 1080p, 720p, 480p, unknown worst
    _RES_RANK = {"2160p": 0, "1080p": 1, "720p": 2, "480p": 3}

    # HDR rank: DV best (0), HDR10+ (1), HDR10 (2), HLG (3), none (4)
    _HDR_RANK = {
        "Dolby Vision": 0,
        "HDR10+": 1,
        "HDR10": 2,
        "HLG": 3,
    }

    # Audio rank: TrueHD+Atmos best, then Atmos DD+, TrueHD, DTS:X,
    # DTS-HD MA, DTS, DD+, DD, AAC, unknown
    _AUDIO_RANK = {
        "TrueHD": 1,
        "Atmos": 0,
        "DTS:X": 3,
        "DTS-HD MA": 4,
        "DTS": 5,
        "DD+": 6,
        "DD": 7,
        "AAC": 8,
    }

    def _relevance_key(r):
        meta = r.get("_meta", {})

        # 1. Resolution (4K first)
        res_rank = _RES_RANK.get(meta.get("resolution", ""), 4)

        # 2. Best HDR tier present
        hdr_list = meta.get("hdr", [])
        if hdr_list:
            hdr_rank = min(_HDR_RANK.get(h, 4) for h in hdr_list)
        else:
            hdr_rank = 5  # no HDR = worst

        # 3. Preferred release group
        is_preferred = 0 if meta.get("group", "").lower() in preferred_lower else 1

        # 4. Best audio tier (handle combos like Atmos + TrueHD)
        audio_list = meta.get("audio", [])
        if audio_list:
            ranks = [_AUDIO_RANK.get(a, 9) for a in audio_list]
            # Atmos + TrueHD combo = rank 0 (best)
            if 0 in ranks and 1 in ranks:
                audio_rank = -1
            else:
                audio_rank = min(ranks)
        else:
            audio_rank = 10

        # 5. Size (larger = better, negate for ascending sort)
        size = -int(r.get("size", 0) or 0)

        return (res_rank, hdr_rank, is_preferred, audio_rank, size)

    if sort_order == 1:
        return sorted(
            results,
            key=lambda r: -int(r.get("size", 0) or 0),
        )
    elif sort_order == 2:
        return sorted(
            results,
            key=_size_sort_key,
        )
    elif sort_order == 3:
        return sorted(
            results,
            key=_pubdate_sort_key,
            reverse=True,
        )
    elif sort_order == 4:
        return sorted(
            results,
            key=_pubdate_sort_key,
        )
    else:
        # Relevance: resolution > HDR > preferred group > audio > size
        return sorted(results, key=_relevance_key)


def _fallback_parse(title):
    """Simple regex fallback when PTT fails or returns empty."""
    import re

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

    # Resolution
    m = re.search(r"(?i)\b(2160p|1080p|1080i|720p|480p|4K)\b", t)
    if m:
        result["resolution"] = m.group(1)

    # Codec
    m = re.search(r"(?i)\b(x265|h\.?265|hevc|x264|h\.?264|avc|av1|vp9)\b", t)
    if m:
        result["codec"] = m.group(1).lower()

    # Audio
    audio = []
    if re.search(r"(?i)\batmos\b", t):
        audio.append("Atmos")
    if re.search(r"(?i)\btruehd\b", t):
        audio.append("TrueHD")
    if re.search(r"(?i)\bdts[-. ]?hd[-. ]?ma\b", t):
        audio.append("DTS-HD MA")
    if re.search(r"(?i)\bddp?5[. ]1|eac3|dd\+|dolby.digital.plus\b", t):
        audio.append("DD+")
    if re.search(r"(?i)\bac3|dd[. ]?5[. ]1|dolby.digital\b", t):
        audio.append("DD")
    if re.search(r"(?i)\baac\b", t):
        audio.append("AAC")
    if re.search(r"(?i)\bdts\b", t) and not audio:
        audio.append("DTS")
    result["audio"] = audio

    # HDR
    hdr = []
    if re.search(r"(?i)\b(dv|dovi|dolby[. ]?vision)\b", t):
        hdr.append("DV")
    # HDR10+ alternation needs both branches anchored to a leading word
    # boundary so we don't pick up substrings inside another token.
    if re.search(r"(?i)\b(hdr10\+|hdr10plus)\b", t):
        hdr.append("HDR10+")
    # `hdr10` without the optional `0` would match `hdr1`; require the digit.
    elif re.search(r"(?i)\bhdr10\b", t):
        hdr.append("HDR10")
    if re.search(r"(?i)\bhlg\b", t):
        hdr.append("HLG")
    result["hdr"] = hdr

    # Quality / Source
    m = re.search(
        r"(?i)\b(remux|blu[-. ]?ray|bdrip|web[-. ]?dl|webrip|hdtv|dvdrip|hdrip)\b", t
    )
    if m:
        raw_q = m.group(1).upper().replace(" ", "").replace(".", "").replace("-", "")
        if "REMUX" in raw_q:
            result["quality"] = "BluRay REMUX"
        elif "BLURAY" in raw_q or "BDRIP" in raw_q:
            result["quality"] = "BluRay"
        elif "WEBDL" in raw_q:
            result["quality"] = "WEB-DL"
        elif "WEBRIP" in raw_q:
            result["quality"] = "WEBRip"
        elif "HDTV" in raw_q:
            result["quality"] = "HDTV"
        else:
            result["quality"] = raw_q

    # Edition
    _ed_re = (
        r"(?i)\b(uncut|unrated|director'?s[. ]?cut|extended[. ]?cut"
        r"|recut|theatrical|imax|special[. ]?edition)\b"
    )
    m = re.search(_ed_re, t)
    if m:
        result["edition"] = m.group(1).replace(".", " ")

    # Channels
    m = re.search(r"\b(7\.1|5\.1|2\.0)\b", t)
    if m:
        result["channels"] = m.group(1)

    # Year
    # Range chosen broadly enough that this isn't a time bomb the next
    # time we forget to bump it (TODO.md §H.2-M44 was the previous
    # bump — 2030 turned out to be too tight). 2100 is well past any
    # plausible release window for content this addon would index.
    m = re.search(r"[. (](\d{4})[. )]", t)
    if m:
        yr = int(m.group(1))
        if 1920 <= yr <= 2100:
            result["year"] = yr

    # Upscaled
    if re.search(r"(?i)\bupscale[d]?\b", t):
        result["upscaled"] = True

    # Group (last segment after hyphen)
    m = re.search(r"-([A-Za-z0-9]+)(?:\.[a-z]{2,4})?$", title)
    if m:
        result["group"] = m.group(1)

    return result


def configure_groups_dialog(setting_id, title, default_set):
    """Show a multiselect dialog for release group configuration.

    Args:
        setting_id: Kodi setting ID to read/write (comma-separated string).
        title: Dialog title string.
        default_set: Set of group names to preselect when setting is empty.
    """
    import xbmcaddon
    import xbmcgui

    addon = xbmcaddon.Addon()
    current = _csv_setting(addon, setting_id)

    if current:
        selected = set(current)
    else:
        selected = set(default_set)

    preselect = [i for i, g in enumerate(ALL_RELEASE_GROUPS) if g in selected]

    dialog = xbmcgui.Dialog()
    result = dialog.multiselect(title, ALL_RELEASE_GROUPS, preselect=preselect)

    if result is None:
        return

    chosen = [ALL_RELEASE_GROUPS[i] for i in result]
    addon.setSetting(setting_id, ",".join(chosen))
