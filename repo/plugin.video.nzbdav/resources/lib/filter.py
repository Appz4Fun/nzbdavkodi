# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Result filtering and sorting using PTT for title parsing."""

import math
import time
from copy import deepcopy
from types import SimpleNamespace

import xbmc
import xbmcaddon  # noqa: F401  pylint: disable=unused-import  (module-scope Kodi imports are the repo convention; tests patch <mod>.xbmcaddon and the thread-safety contract tests forbid its use on no-getter paths)
import xbmcgui  # re-exported via __all__ for callers/tests; not used here

from resources.lib import telemetry
from resources.lib.filter_fallback import (
    _fallback_audio,
    _fallback_hdr,
    _fallback_parse,
    _fallback_quality,
    _fallback_year,
)
from resources.lib.filter_groups import ALL_RELEASE_GROUPS, configure_groups_dialog
from resources.lib.filter_normalize import (
    _normalize_fallback_meta,
    _normalize_parsed_meta,
)

# Re-exported so ``resources.lib.filter.<name>`` keeps resolving for callers
# and tests after the fallback parser, groups dialog, and metadata
# normalization moved to sibling modules (``filter_fallback`` /
# ``filter_groups`` / ``filter_normalize``).
__all__ = [
    "xbmcgui",
    "ALL_RELEASE_GROUPS",
    "configure_groups_dialog",
    "_fallback_audio",
    "_fallback_hdr",
    "_fallback_parse",
    "_fallback_quality",
    "_fallback_year",
    "_normalize_fallback_meta",
    "_normalize_parsed_meta",
]

# The complete set of keys produced by ``parse_title_metadata``. A cached
# ``_meta`` dict is only safe to reuse (skipping a reparse) when it satisfies
# this FULL contract — downstream consumers such as
# ``fallback_streams_identity`` trust any dict found in ``_meta`` and never
# reparse, so a partial dict would silently drop quality/edition/year/
# upscaled/container/etc. from the fallback pipeline.
_FILTER_META_STR_KEYS = (
    "resolution",
    "codec",
    "group",
    "quality",
    "edition",
    "channels",
    "container",
)
_FILTER_META_LIST_KEYS = ("hdr", "audio", "languages")
_FILTER_META_BOOL_KEYS = ("proper", "repack", "upscaled")
_FILTER_META_INT_KEYS = ("year",)
_FILTER_META_KEYS = frozenset(
    _FILTER_META_STR_KEYS
    + _FILTER_META_LIST_KEYS
    + _FILTER_META_BOOL_KEYS
    + _FILTER_META_INT_KEYS
)

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
    """Read an integer Kodi setting with a safe fallback.

    Tries plain ``int()`` first so a clean integer string like "500"
    parses without floating-point noise; on failure (e.g. user typed
    "1.5" because the size field accepts decimals on some Kodi
    skins), falls through to ``int(float(raw))`` so the caller sees
    a clear truncated value (1) instead of the silent default (0).
    """
    raw = addon.getSetting(key)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        pass
    try:
        parsed = float(raw)
        if not math.isfinite(parsed):
            return default
        return int(parsed)
    except (OverflowError, TypeError, ValueError):
        return default


# Setting-key -> filter-value specs, consumed by _get_filter_settings via
# _collect_enabled. ISO 639-1 language codes match PTT's
# `parsed["languages"]` output (lowercase two-letter codes); `matches_filters`
# does a direct ``lang in settings["languages"]`` membership check against PTT
# output, so comparing "en" against "English" never matched and any enabled
# language filter rejected every result. Closes TODO.md §H.2-H11.
_RESOLUTION_SETTINGS = [
    ("filter_2160p", "2160p"),
    ("filter_1080p", "1080p"),
    ("filter_720p", "720p"),
    ("filter_480p", "480p"),
]
_HDR_SETTINGS = [
    ("filter_hdr10", "HDR10"),
    ("filter_hdr10plus", "HDR10+"),
    ("filter_dolby_vision", "Dolby Vision"),
    ("filter_hlg", "HLG"),
    ("filter_sdr", "SDR"),
]
_AUDIO_SETTINGS = [
    ("filter_atmos", "Atmos"),
    ("filter_truehd", "TrueHD"),
    ("filter_dtshd_ma", "DTS-HD MA"),
    ("filter_dtsx", "DTS:X"),
    ("filter_ddplus", "DD+"),
    ("filter_dd", "DD"),
    ("filter_aac", "AAC"),
]
_CODEC_SETTINGS = [
    ("filter_hevc", "x265/HEVC"),
    ("filter_avc", "x264/AVC"),
    ("filter_av1", "AV1"),
    ("filter_vp9", "VP9"),
    ("filter_mpeg2", "MPEG-2"),
]
_LANGUAGE_SETTINGS = [
    ("filter_english", "en"),
    ("filter_spanish", "es"),
    ("filter_french", "fr"),
    ("filter_german", "de"),
    ("filter_italian", "it"),
    ("filter_portuguese", "pt"),
    ("filter_dutch", "nl"),
    ("filter_russian", "ru"),
    ("filter_japanese", "ja"),
    ("filter_korean", "ko"),
    ("filter_chinese", "zh"),
    ("filter_arabic", "ar"),
    ("filter_hindi", "hi"),
]


def _resolve_size_bounds(addon):
    """Read min/max size, disabling the filter on an inverted range."""
    min_size = _int_setting(addon, "filter_min_size", 0)
    max_size = _int_setting(addon, "filter_max_size", 0)
    if 0 < max_size < min_size:
        xbmc.log(
            "NZB-DAV: filter_min_size={} exceeds filter_max_size={}; "
            "disabling size filter".format(min_size, max_size),
            xbmc.LOGWARNING,
        )
        min_size = 0
        max_size = 0
    return min_size, max_size


def _get_filter_settings(settings_getter=None):
    """Read filter settings from Kodi addon config."""
    if settings_getter is None:
        # Disk read, not the xbmcaddon binding: filter_results runs on
        # fallback-loader worker threads, where concurrent binding reads
        # race Kodi's lazy settings load (TinyXML SIGSEGV,
        # gdb-confirmed on the extreme harness).
        from resources.lib.router import _get_script_setting as settings_getter
    addon = SimpleNamespace(getSetting=lambda key: settings_getter(key, ""))

    resolutions = _collect_enabled(addon, _RESOLUTION_SETTINGS)
    hdr = _collect_enabled(addon, _HDR_SETTINGS)
    audio = _collect_enabled(addon, _AUDIO_SETTINGS)
    codecs = _collect_enabled(addon, _CODEC_SETTINGS)
    languages = _collect_enabled(addon, _LANGUAGE_SETTINGS)

    min_size, max_size = _resolve_size_bounds(addon)

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
        "min_size": min_size,
        "max_size": max_size,
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

    # The normalization assumes PTT returned typed data matching its
    # documented contract. If the vendored PTT drifts from that contract
    # (or a custom transformer returns e.g. a dict for hdr), the
    # comprehensions explode with TypeError. Catch that so a single bad
    # release name doesn't kill the whole search; fall back to the
    # regex-only metadata extractor.
    try:
        return _normalize_parsed_meta(parsed)
    except (TypeError, AttributeError, KeyError) as e:
        xbmc.log(
            "NZB-DAV: PTT metadata normalisation failed for '{}': {}; "
            "falling back to regex parse".format(title, e),
            xbmc.LOGWARNING,
        )
        return _normalize_fallback_meta(_fallback_parse(title))


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
    if not _meta_filters_pass(meta, settings):
        return False
    if not _keyword_filters_pass(result["title"].lower(), settings):
        return False
    if meta["group"] and meta["group"].lower() in settings["exclude_release_group"]:
        return False
    if not _size_filter_passes(result, settings):
        return False
    return True


def _resolution_filter_passes(meta, settings):
    if settings["resolutions"] and meta["resolution"]:
        return meta["resolution"] in settings["resolutions"]
    return True


def _hdr_filter_passes(meta, settings):
    wanted = settings["hdr"]
    if not wanted:
        return True
    if meta["hdr"]:
        return any(h in wanted for h in meta["hdr"])
    return "SDR" in wanted


def _audio_filter_passes(meta, settings):
    if settings["audio"] and meta["audio"]:
        return any(a in settings["audio"] for a in meta["audio"])
    return True


def _codec_filter_passes(meta, settings):
    if settings["codecs"] and meta["codec"]:
        return meta["codec"] in settings["codecs"]
    return True


def _language_filter_passes(meta, settings):
    if settings["languages"] and meta["languages"]:
        return any(lang in settings["languages"] for lang in meta["languages"])
    return True


def _meta_filters_pass(meta, settings):
    """True iff the resolution/HDR/audio/codec/language filters all accept."""
    return (
        _resolution_filter_passes(meta, settings)
        and _hdr_filter_passes(meta, settings)
        and _audio_filter_passes(meta, settings)
        and _codec_filter_passes(meta, settings)
        and _language_filter_passes(meta, settings)
    )


def _keyword_filters_pass(title_lower, settings):
    """True iff exclude/require keyword filters accept this title."""
    for kw in settings["exclude_keywords"]:
        if kw in title_lower:
            return False
    for kw in settings["require_keywords"]:
        if kw not in title_lower:
            return False
    return True


def _size_filter_passes(result, settings):
    """True iff the result's size is within the configured bounds.

    A 0-byte / missing-size placeholder result used to skip both bounds
    because `if result.get("size"):` is falsy for "0" and "". That let
    unparseable / placeholder rows slip past min_size when the user
    wanted to filter them out. Now: reach the size check unconditionally;
    treat unparseable size as 0 MB so a min_size>0 filter rejects it.
    Closes TODO.md §H.3.
    """
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


def _is_str_list(value):
    """True when value is a list whose every item is a str."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_str(value):
    """True when value is a str."""
    return isinstance(value, str)


def _is_bool(value):
    """True when value is a bool."""
    return isinstance(value, bool)


def _is_int_not_bool(value):
    """True when value is an int but not a bool (bool subclasses int)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _all_match(meta, keys, predicate):
    """True iff ``predicate`` accepts ``meta.get(key)`` for every key."""
    return all(predicate(meta.get(key)) for key in keys)


def _has_filter_metadata_shape(meta):
    """Return True when cached metadata satisfies the full parse contract.

    Validates the complete set of fields produced by
    ``parse_title_metadata`` (not just the ones ``filter_results`` indexes)
    so a partial cached ``_meta`` is reparsed instead of being blindly
    reused and propagated to ``fallback_streams_identity``.
    """
    if not isinstance(meta, dict) or not _FILTER_META_KEYS <= set(meta):
        return False
    # bool is a subclass of int; ``_is_int_not_bool`` rejects a stray bool year.
    return (
        _all_match(meta, _FILTER_META_STR_KEYS, _is_str)
        and _all_match(meta, _FILTER_META_LIST_KEYS, _is_str_list)
        and _all_match(meta, _FILTER_META_BOOL_KEYS, _is_bool)
        and _all_match(meta, _FILTER_META_INT_KEYS, _is_int_not_bool)
    )


def _resolve_result_meta(result, parsed_by_title):
    """Return the metadata for a result, reusing the per-title cache."""
    meta = result.get("_meta")
    title = result["title"]
    if _has_filter_metadata_shape(meta):
        if title not in parsed_by_title:
            parsed_by_title[title] = meta
        return meta
    cached_meta = parsed_by_title.get(title)
    if cached_meta is not None:
        return deepcopy(cached_meta)
    meta = parse_title_metadata(title)
    parsed_by_title[title] = meta
    return meta


def _log_filter_summary(total, matched_count, shown):
    """Log the filter result counts, noting truncation when it occurred."""
    if shown < matched_count:
        message = "NZB-DAV: Filtered {} -> {} results (showing {})".format(
            total, matched_count, shown
        )
    else:
        message = "NZB-DAV: Filtered {} -> {} results".format(total, shown)
    xbmc.log(message, xbmc.LOGDEBUG)


def filter_results(results, settings_getter=None):
    """Apply filters, sort, truncate. Returns (filtered, all_parsed).

    Side effect: mutates each input dict by attaching a ``_meta`` key
    holding the parsed-title metadata. Callers that iterate ``results``
    after this call will see the extra field. ``all_parsed`` is the
    same list of dicts (with ``_meta`` populated) in sorted order;
    ``filtered`` is the subset that passed every filter, truncated
    to ``settings["max_results"]`` if that is non-zero.
    """
    started = time.monotonic()
    settings = _get_filter_settings(settings_getter=settings_getter)

    parsed_by_title = {}

    all_parsed = []
    filtered = []
    for result in results:
        meta = _resolve_result_meta(result, parsed_by_title)
        result["_meta"] = meta
        all_parsed.append(result)
        if matches_filters(result, meta, settings):
            filtered.append(result)

    filtered = _sort_results(filtered, settings)
    all_parsed = _sort_results(all_parsed, settings)

    matched_count = len(filtered)
    max_results = settings["max_results"]
    if max_results > 0:
        filtered = filtered[:max_results]

    _log_filter_summary(len(all_parsed), matched_count, len(filtered))
    telemetry.log_timing(
        "filter_results",
        (time.monotonic() - started) * 1000.0,
        input=len(all_parsed),
        matched=matched_count,
        shown=len(filtered),
    )
    return filtered, all_parsed


def partition_series_rows(rows, requested_title):
    """Split rows into (matching, rest) by PTT-parsed show title.

    An episode search matches the query phrase ANYWHERE in the release
    name, so a series whose name collides with another show's EPISODE
    title pulls in that show's releases (live: searching the 2025 series
    "The Good the Bad and the Ugly" returned The.Rookie.S01E03.The.Good.
    the.Bad.and.the.Ugly...-NTb, and auto-select played The Rookie).
    A row matches when its normalized parsed show title equals, contains,
    or is contained in the normalized requested title. Rows whose title
    PTT cannot parse land in ``rest``.
    """
    if not requested_title:
        return list(rows), []
    # Lazy import, and through fallback_streams (not _identity directly):
    # the fallback modules import each other and this module's parse
    # helpers; fallback_streams is the only entry point that initializes
    # cleanly from a cold sys.modules, and it re-exports both helpers.
    from resources.lib.fallback_streams import (
        _normalize_title,
        _release_identity,
    )

    want = _normalize_title(requested_title)
    if not want:
        return list(rows), []
    matching = []
    rest = []
    for row in rows:
        got = _release_identity(row)[0]
        raw = row.get("title", "") if isinstance(row, dict) else ""
        if got == _normalize_title(raw) and got != want:
            # PTT could not isolate a show title and the identity fell
            # back to the WHOLE normalized release name — which contains
            # the searched phrase by construction (the indexer matched
            # it), so containment would pass every row. Only exact
            # equality counts for these.
            rest.append(row)
        elif got and (got == want or want in got or got in want):
            matching.append(row)
        else:
            rest.append(row)
    return matching, rest


def prefer_series_rows(rows, requested_title):
    """Order rows so parsed-show-title matches precede everything else.

    Auto-select takes ``rows[0]``, so this is the wrong-show guard for
    episode playback; manual pickers still see every row. When NOTHING
    matches (foreign titling, PTT quirks) the original order is kept
    rather than guessing.
    """
    matching, rest = partition_series_rows(rows, requested_title)
    if not matching or not rest:
        return list(rows)
    xbmc.log(
        "NZB-DAV: demoted {} result(s) whose parsed show title does not "
        "match '{}'".format(len(rest), requested_title),
        xbmc.LOGINFO,
    )
    return matching + rest


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


def _hdr_rank(hdr_list):
    """Best (lowest) HDR tier rank present, or 5 when none."""
    if not hdr_list:
        return 5  # no HDR = worst
    return min(_HDR_RANK.get(h, 4) for h in hdr_list)


def _audio_rank(audio_list):
    """Best audio tier rank; Atmos + TrueHD combo is -1 (best)."""
    if not audio_list:
        return 10
    ranks = [_AUDIO_RANK.get(a, 9) for a in audio_list]
    # Atmos + TrueHD combo = rank 0 (best)
    if 0 in ranks and 1 in ranks:
        return -1
    return min(ranks)


def _make_relevance_key(preferred_lower):
    """Build a sort key: resolution > HDR > preferred group > audio > size."""

    def _relevance_key(r):
        meta = r.get("_meta", {})
        res_rank = _RES_RANK.get(meta.get("resolution", ""), 4)
        is_preferred = 0 if meta.get("group", "").lower() in preferred_lower else 1
        size = -_size_sort_key(r)  # larger = better, negate for ascending sort
        return (
            res_rank,
            _hdr_rank(meta.get("hdr", [])),
            is_preferred,
            _audio_rank(meta.get("audio", [])),
            size,
        )

    return _relevance_key


def _neg_size_sort_key(result):
    """Negated size key: largest first, preserving stable tie order."""
    return -_size_sort_key(result)


# Non-relevance sort orders: (key function, reverse). Order 1 negates the
# key rather than using reverse=True so equal-size ties keep their original
# relative order (reverse=True would flip ties under Python's stable sort).
_SORT_SPECS = {
    1: (_neg_size_sort_key, False),
    2: (_size_sort_key, False),
    3: (_pubdate_sort_key, True),
    4: (_pubdate_sort_key, False),
}


def _sort_results(results, settings):
    """Sort results by configured sort order, with preferred groups boosted.

    Sort orders:
        0 = Relevance (original order)
        1 = Size (largest first)
        2 = Size (smallest first)
        3 = Age (newest first) -- pubdate descending
        4 = Age (oldest first) -- pubdate ascending
    """
    spec = _SORT_SPECS.get(settings["sort_order"])
    if spec is not None:
        key, reverse = spec
        return sorted(results, key=key, reverse=reverse)
    preferred_lower = [g.lower() for g in settings["release_group"]]
    return sorted(results, key=_make_relevance_key(preferred_lower))
