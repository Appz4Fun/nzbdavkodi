# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Result filtering and sorting using PTT for title parsing."""

import math
import time
from copy import deepcopy
from types import SimpleNamespace

import xbmc
import xbmcaddon
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
from resources.lib.filter_languages import language_filter_values
from resources.lib.filter_normalize import (
    _normalize_fallback_meta,
    _normalize_parsed_meta,
)
from resources.lib.filter_options import (
    AUDIO_SETTINGS as _AUDIO_SETTINGS,
)
from resources.lib.filter_options import (
    BOOLEAN_SETTINGS,
    UNKNOWN_SETTINGS,
    values_filter_pass,
)
from resources.lib.filter_options import (
    CODEC_SETTINGS as _CODEC_SETTINGS,
)
from resources.lib.filter_options import (
    HDR_SETTINGS as _HDR_SETTINGS,
)
from resources.lib.filter_options import (
    LANGUAGE_SETTINGS as _LANGUAGE_SETTINGS,
)
from resources.lib.filter_options import (
    RESOLUTION_SETTINGS as _RESOLUTION_SETTINGS,
)
from resources.lib.filter_remux import (
    DEFAULT_REMUX_GROUPS,
    compile_remux_tiers,
    read_remux_tiers,
    remux_group_rank,
    remux_priority,
)
from resources.lib.filter_tags import supplement_metadata

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
        if (addon.getSetting(setting_id) or "true").lower() == "true"
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
        addon = xbmcaddon.Addon("plugin.video.nzbdav")
    else:
        addon = SimpleNamespace(
            getSetting=lambda key: settings_getter(
                key, "true" if key in BOOLEAN_SETTINGS else ""
            )
        )

    resolutions = _collect_enabled(addon, _RESOLUTION_SETTINGS)
    hdr = _collect_enabled(addon, _HDR_SETTINGS)
    audio = _collect_enabled(addon, _AUDIO_SETTINGS)
    codecs = _collect_enabled(addon, _CODEC_SETTINGS)
    languages = _collect_enabled(addon, _LANGUAGE_SETTINGS)

    min_size, max_size = _resolve_size_bounds(addon)

    return {
        **{
            name: (addon.getSetting(key) or "true").lower() == "true"
            for name, key in UNKNOWN_SETTINGS.items()
        },
        "remux_tiers": read_remux_tiers(
            settings_getter or (lambda key, default: addon.getSetting(key))
        ),
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
        return supplement_metadata(title, _normalize_parsed_meta(parsed))
    except (TypeError, AttributeError, KeyError) as e:
        xbmc.log(
            "NZB-DAV: PTT metadata normalisation failed for '{}': {}; "
            "falling back to regex parse".format(title, e),
            xbmc.LOGWARNING,
        )
        return supplement_metadata(
            title, _normalize_fallback_meta(_fallback_parse(title))
        )


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
    return first_rejecting_filter(result, meta, settings) is None


def _group_filter_passes(meta, settings):
    excluded = settings["exclude_release_group"]
    return not (meta["group"] and meta["group"].lower() in excluded)


def first_rejecting_filter(result, meta, settings):
    """Name of the first filter that rejects this result, or ``None``.

    Checks run in ``matches_filters``' historical short-circuit order, so
    the reported reason is the filter that would have rejected first. The
    labels are technical tokens rendered verbatim in the picker's
    ``FILTERED:`` chip (not localized, mirroring the ASCII ``DL`` tag).

    A table of lazy checks (rather than a chain of ``if`` statements) keeps
    this function's own cyclomatic complexity low -- the branching lives in
    each named predicate, which is measured separately.
    """
    checks = (
        (lambda: _resolution_filter_passes(meta, settings), "resolution"),
        (lambda: _hdr_filter_passes(meta, settings), "HDR"),
        (lambda: _audio_filter_passes(meta, settings), "audio"),
        (lambda: _codec_filter_passes(meta, settings), "codec"),
        (lambda: _language_filter_passes(meta, settings), "language"),
        (lambda: _keyword_filters_pass(result["title"].lower(), settings), "keyword"),
        (lambda: _group_filter_passes(meta, settings), "group"),
        (lambda: _size_filter_passes(result, settings), "size"),
    )
    for passes, label in checks:
        if not passes():
            return label
    return None


def _resolution_filter_passes(meta, settings):
    return values_filter_pass(
        [meta["resolution"]] if meta["resolution"] else [],
        settings["resolutions"],
        _RESOLUTION_SETTINGS,
        settings.get("unknown_resolution", True),
    )


def _hdr_filter_passes(meta, settings):
    return values_filter_pass(
        meta["hdr"],
        settings["hdr"],
        _HDR_SETTINGS,
        settings.get("unknown_hdr", True),
    )


def _audio_filter_passes(meta, settings):
    return values_filter_pass(
        meta["audio"],
        settings["audio"],
        _AUDIO_SETTINGS,
        settings.get("unknown_audio", True),
    )


def _codec_filter_passes(meta, settings):
    return values_filter_pass(
        [meta["codec"]] if meta["codec"] else [],
        settings["codecs"],
        _CODEC_SETTINGS,
        settings.get("unknown_codec", True),
    )


def _language_filter_passes(meta, settings):
    return values_filter_pass(
        language_filter_values(meta["languages"]),
        settings["languages"],
        _LANGUAGE_SETTINGS,
        settings.get("unknown_language", True),
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

    Side effect: mutates each input dict by attaching ``_meta``
    (parsed-title metadata) and ``_filter_reject`` (the first rejecting
    filter's name, or ``None`` when the row passed). Callers that iterate
    ``results`` after this call will see the extra fields. ``all_parsed`` is the
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
        reject = first_rejecting_filter(result, meta, settings)
        result["_filter_reject"] = reject
        if reject is None:
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


# Resolution rank: highest first, from 8K down to SD, unknown worst.
_RES_RANK = {value: rank for rank, (_, value) in enumerate(_RESOLUTION_SETTINGS)}

# HDR rank: DV best (0), HDR10+ (1), HDR10 (2), HLG (3), SDR (4), none (5).
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
    "DTS-HD HR": 5,
    "FLAC": 4,
    "PCM": 4,
    "ALAC": 4,
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


def _make_relevance_key(remux_tiers=DEFAULT_REMUX_GROUPS):
    """Rank resolution and HDR first, then REMUX, groups, audio, and size."""
    tier_patterns = compile_remux_tiers(remux_tiers)

    def _relevance_key(r):
        meta = r.get("_meta", {})
        res_rank = _RES_RANK.get(meta.get("resolution", ""), len(_RES_RANK))
        size = -_size_sort_key(r)  # larger = better, negate for ascending sort
        priority = remux_priority(r)
        return (
            res_rank,
            _hdr_rank(meta.get("hdr", [])),
            priority,
            remux_group_rank(r, tier_patterns),
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
    return sorted(
        results,
        key=_make_relevance_key(settings.get("remux_tiers", DEFAULT_REMUX_GROUPS)),
    )
