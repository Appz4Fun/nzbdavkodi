# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Conservative grouping for duplicate releases usable as fallback streams."""

import hashlib
import re
import threading
import time
from collections import namedtuple
from functools import lru_cache
from queue import Empty, Queue
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import xbmc
import xbmcaddon

from resources.lib.nzb_manifest import fetch_nzb_video_manifest, make_empty_manifest

_SAFE_JOB_RE = re.compile(r"^[A-Za-z0-9._ \[\]-]+$")
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")
_FINGERPRINT_SAMPLE_COUNT = 20
_FINGERPRINT_BYTES = 4096

_MAX_FALLBACKS = 5
_FALLBACK_MANIFEST_STALL_SPECULATION_SECONDS = 0.05
_FALLBACK_MANIFEST_OPTIONAL_TAIL_WAIT_SECONDS = 0.1
_ALLOWED_STREAM_SCHEMES = frozenset(("http", "https"))
_METADATA_ONLY_MANIFEST_REASONS = frozenset(("too_large",))
_PrecomputedProbeBase = namedtuple("_PrecomputedProbeBase", "parts origin path")


def _setting_bool(addon, key, default=False):
    """Read a Kodi boolean setting with a safe fallback."""
    raw = addon.getSetting(key)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized == "":
            return bool(default)
        if normalized in ("false", "0", "no", "off"):
            return False
    return bool(default)


def _setting_int(addon, key, default=0):
    """Read a Kodi integer setting with a safe fallback."""
    raw = addon.getSetting(key)
    try:
        return int(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        return int(default)


def _valid_stream_url(url):
    """Return True for HTTP(S) stream URLs that are safe to probe."""
    return _validated_probe_url(url) is not None


def _split_http_url(url):
    """Parse a URL and return parts only for simple HTTP(S) URLs."""
    if not isinstance(url, str) or any(ord(char) < 0x20 for char in url):
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in _ALLOWED_STREAM_SCHEMES:
            return False
        if not parts.netloc or not parts.hostname:
            return False
        if parts.username or parts.password:
            return False
        # Accessing .port validates that any explicit port is numeric/ranged.
        _port = parts.port
    except ValueError:
        return False
    return parts


def _origin_key(parts):
    """Return the normalized origin tuple for parsed URL parts."""
    scheme = parts.scheme.lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parts.hostname.lower(), port


def _path_is_under_base(path, base_path):
    """Return whether a URL path is within the configured base path."""
    prefix = (base_path or "").rstrip("/")
    if not prefix:
        return True
    return path == prefix or path.startswith(prefix + "/")


def _configured_stream_bases():
    """Return configured WebDAV/nzbdav bases that fallback probes may hit."""
    try:
        addon = xbmcaddon.Addon()
        raw_bases = (
            addon.getSetting("webdav_url"),
            addon.getSetting("nzbdav_url"),
        )
    except Exception:  # pylint: disable=broad-except
        raw_bases = ()

    bases = []
    for raw_base in raw_bases:
        parts = _split_http_url(str(raw_base or "").rstrip("/"))
        if parts:
            bases.append(parts)
    return bases


def configured_stream_bases():
    """Return configured stream bases for callers doing repeated probes."""
    return _configured_stream_bases()


def configured_stream_probe_bases():
    """Return configured stream bases with reusable origin/path checks."""
    return tuple(
        _PrecomputedProbeBase(parts, _origin_key(parts), parts.path or "/")
        for parts in _configured_stream_bases()
    )


def _probe_base_components(base):
    """Return parsed URL parts plus cached origin/path data for one base."""
    if isinstance(base, _PrecomputedProbeBase):
        return base.parts, base.origin, base.path
    return base, _origin_key(base), base.path or "/"


def _validated_probe_url(url, probe_bases=None):
    """Return a probe URL constrained to the configured WebDAV origin."""
    candidate = _split_http_url(url)
    if not candidate:
        return None
    bases = _configured_stream_bases() if probe_bases is None else probe_bases
    candidate_origin = _origin_key(candidate)
    for base in bases:
        base_parts, base_origin, base_path = _probe_base_components(base)
        if candidate_origin != base_origin:
            continue
        if not _path_is_under_base(candidate.path or "/", base_path):
            continue
        return urlunsplit(
            (
                base_parts.scheme.lower(),
                base_parts.netloc,
                candidate.path or "/",
                candidate.query,
                "",
            )
        )
    return None


@lru_cache(maxsize=256)
def _cached_validated_probe_url(url, probe_bases):
    """Return a cached configured-origin probe URL for immutable base snapshots."""
    return _validated_probe_url(url, probe_bases=probe_bases)


def _validated_probe_url_for_fetch(url, probe_bases=None):
    """Return a probe URL, caching validation when bases are immutable."""
    if probe_bases is None:
        return _validated_probe_url(url)
    try:
        return _cached_validated_probe_url(url, tuple(probe_bases))
    except TypeError:
        return _validated_probe_url(url, probe_bases=probe_bases)


def _normalize_title(value):
    """Normalize release titles for conservative duplicate grouping."""
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[\W_]+", " ", value.lower())
    return " ".join(normalized.split())


def _quality_key(result):
    """Return the conservative duplicate-grouping quality key for a result."""
    meta = _result_meta(result)
    return (
        _normalize_title(result.get("title", "") if isinstance(result, dict) else ""),
        str(meta.get("resolution", "") or "").strip().lower(),
        str(meta.get("quality", "") or "").strip().lower(),
        str(meta.get("codec", "") or "").strip().lower(),
        str(meta.get("group", "") or "").strip().lower(),
        str(meta.get("container", "") or "").strip().lower(),
    )


def _result_meta(result):
    """Return parsed title metadata, deriving it when the caller has raw results."""
    if not isinstance(result, dict):
        return {}
    meta = result.get("_meta")
    if isinstance(meta, dict):
        return meta
    try:
        from resources.lib.filter import parse_title_metadata

        meta = parse_title_metadata(result.get("title", ""))
    except Exception:  # pylint: disable=broad-except
        meta = {}
    if isinstance(meta, dict):
        result["_meta"] = meta
        return meta
    return {}


def _meta_value(result, key):
    """Return a normalized metadata string from a result."""
    return _meta_value_from_meta(_result_meta(result), key)


def _meta_value_from_meta(meta, key):
    """Return a normalized metadata string from an existing metadata dict."""
    if not isinstance(meta, dict):
        return ""
    value = meta.get(key, "")
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def _meta_values(result, key):
    """Return normalized metadata list values from a result."""
    return _meta_values_from_meta(_result_meta(result), key)


def _meta_values_from_meta(meta, key):
    """Return normalized metadata list values from an existing metadata dict."""
    if not isinstance(meta, dict):
        return []
    value = meta.get(key, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def _meta_bool(result, key):
    """Return a normalized boolean metadata flag from a result."""
    return _meta_bool_from_meta(_result_meta(result), key)


def _meta_bool_from_meta(meta, key):
    """Return a normalized boolean flag from an existing metadata dict."""
    if not isinstance(meta, dict):
        return False
    value = meta.get(key, False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _quality_family(value):
    """Collapse source labels that describe the same fallback-safe family."""
    text = _normalize_title(value)
    if "remux" in text:
        return "remux"
    if "web dl" in text or "webdl" in text:
        return "web-dl"
    if "webrip" in text or "web rip" in text:
        return "webrip"
    if "hdtv" in text:
        return "hdtv"
    if "bluray" in text or "blu ray" in text or "bdrip" in text or "uhd" in text:
        return "bluray"
    return text


_TITLE_STOP_TOKENS = frozenset(
    (
        "2160p",
        "1080p",
        "720p",
        "480p",
        "ac3",
        "a",
        "an",
        "and",
        "atmos",
        "avc",
        "bluray",
        "ddp",
        "dovi",
        "dts",
        "dv",
        "group",
        "grp",
        "hdr",
        "hdr10",
        "hevc",
        "h264",
        "h265",
        "remux",
        "the",
        "uhd",
        "web",
        "webdl",
        "x264",
        "x265",
    )
)
_TITLE_TOKEN_CACHE_TITLE_KEY = "_fallback_title_tokens_title"
_TITLE_TOKEN_CACHE_VALUE_KEY = "_fallback_title_tokens"
_PREFETCH_PROOF_KEY = "_fallback_prefetch_gate_proof"
_SELECTION_POOL_FIRST_PEER_KEY = "_fallback_selection_pool_first_peer"
FALLBACK_CANDIDATES_DISABLED = object()


def _title_tokens(result):
    """Return content-identifying title tokens for lenient fallback matching."""
    title = result.get("title", "") if isinstance(result, dict) else ""
    if isinstance(result, dict):
        cached_tokens = result.get(_TITLE_TOKEN_CACHE_VALUE_KEY)
        if result.get(_TITLE_TOKEN_CACHE_TITLE_KEY) == title and isinstance(
            cached_tokens, (frozenset, set)
        ):
            return cached_tokens

    tokens = []
    for token in _normalize_title(title).split():
        if token in _TITLE_STOP_TOKENS:
            continue
        if len(token) <= 1 and not token.isdigit():
            continue
        tokens.append(token)
    token_set = frozenset(tokens)
    if isinstance(result, dict):
        result[_TITLE_TOKEN_CACHE_TITLE_KEY] = title
        result[_TITLE_TOKEN_CACHE_VALUE_KEY] = token_set
    return token_set


def _cached_title_tokens(result):
    """Return already-computed title tokens for this exact title, if present."""
    if not isinstance(result, dict):
        return None
    title = result.get("title", "")
    cached_tokens = result.get(_TITLE_TOKEN_CACHE_VALUE_KEY)
    if result.get(_TITLE_TOKEN_CACHE_TITLE_KEY) == title and isinstance(
        cached_tokens, (frozenset, set)
    ):
        return cached_tokens
    return None


def _titles_look_related(primary, candidate):
    """Return whether release titles overlap enough to be plausible reposts."""
    left = _title_tokens(primary)
    right = _title_tokens(candidate)
    return _title_token_sets_look_related(left, right)


def _title_token_sets_look_related(left, right):
    """Return whether two precomputed title-token sets look related."""
    if not left or not right:
        return True
    overlap = left.intersection(right)
    needed = 1 if min(len(left), len(right)) <= 2 else 2
    return len(overlap) >= needed


def _metadata_profile_signature(meta):
    """Return the title/profile fields covered by the fallback prefetch gate."""
    return (
        _meta_value_from_meta(meta, "resolution"),
        _meta_value_from_meta(meta, "codec"),
        _meta_value_from_meta(meta, "container"),
        _normalize_title(_meta_value_from_meta(meta, "edition")),
        _meta_bool_from_meta(meta, "proper"),
        _meta_bool_from_meta(meta, "repack"),
        _meta_bool_from_meta(meta, "upscaled"),
        _quality_family(_meta_value_from_meta(meta, "quality")),
        tuple(sorted(set(_meta_values_from_meta(meta, "hdr")))),
        tuple(sorted(set(_meta_values_from_meta(meta, "audio")))),
        _meta_value_from_meta(meta, "channels"),
    )


def _prefetch_gate_proof(primary, candidate, primary_meta=None, candidate_meta=None):
    """Return a stable proof key for an already-passed title/profile gate."""
    if not isinstance(primary, dict) or not isinstance(candidate, dict):
        return None
    if primary_meta is None:
        primary_meta = primary.get("_meta")
    if candidate_meta is None:
        candidate_meta = candidate.get("_meta")
    if not isinstance(primary_meta, dict) or not isinstance(candidate_meta, dict):
        return None
    return (
        primary.get("link", ""),
        primary.get("title", ""),
        candidate.get("link", ""),
        candidate.get("title", ""),
        _metadata_profile_signature(primary_meta),
        _metadata_profile_signature(candidate_meta),
    )


def _remember_prefetch_gate_match(
    primary, candidate, primary_meta=None, candidate_meta=None
):
    """Store proof that the candidate passed the fallback title/profile gate."""
    proof = _prefetch_gate_proof(
        primary, candidate, primary_meta=primary_meta, candidate_meta=candidate_meta
    )
    if proof is not None:
        candidate[_PREFETCH_PROOF_KEY] = proof


def _has_prefetch_gate_match(primary, candidate):
    """Return whether a candidate still matches a prior prefetch-gate proof."""
    if not isinstance(primary, dict) or not isinstance(candidate, dict):
        return False
    proof = candidate.get(_PREFETCH_PROOF_KEY)
    if not isinstance(proof, tuple) or len(proof) != 6:
        return False
    current_identity = (
        primary.get("link", ""),
        primary.get("title", ""),
        candidate.get("link", ""),
        candidate.get("title", ""),
    )
    if proof[:4] != current_identity:
        return False
    return proof == _prefetch_gate_proof(primary, candidate)


def _remember_selection_pool_first_peer(selected, results, peer):
    """Store the first distinct peer found during a selection-pool scan."""
    if isinstance(selected, dict) and isinstance(peer, dict):
        selected[_SELECTION_POOL_FIRST_PEER_KEY] = (
            id(results),
            selected.get("link", ""),
            peer,
        )


def cached_selection_pool_first_peer(selected, results):
    """Return the first distinct peer found by a matching pool scan."""
    if not isinstance(selected, dict):
        return None
    cached = selected.get(_SELECTION_POOL_FIRST_PEER_KEY)
    if not isinstance(cached, tuple) or len(cached) != 3:
        return None
    results_id, selected_link, peer = cached
    if results_id != id(results) or selected_link != selected.get("link", ""):
        return None
    if not isinstance(peer, dict):
        return None
    return peer


def has_title_related_fallback_peer(selected, results):
    """Return whether any distinct result can pass the title fallback gate."""
    if not isinstance(selected, dict):
        return False
    selected_link = selected.get("link", "")
    selected_tokens = _title_tokens(selected)
    for result in results or []:
        if result is selected or not isinstance(result, dict):
            continue
        result_link = result.get("link", "")
        if not result_link or result_link == selected_link:
            continue
        if _title_token_sets_look_related(selected_tokens, _title_tokens(result)):
            return True
    return False


def has_prefetchable_fallback_peer(selected, results):
    """Return whether any distinct result can pass the fallback prefetch gate."""
    return first_prefetchable_fallback_peer(selected, results) is not None


def _sized_pool_has_no_distinct_peer(selected, results):
    """Return True when a sized pool cannot contain any fallback peer."""
    try:
        result_count = len(results)
    except TypeError:
        return False
    if result_count == 0:
        return True
    if result_count != 1:
        if not isinstance(selected, dict):
            return False
        selected_link = selected.get("link", "")
        try:
            for result in results:
                if result is selected or not isinstance(result, dict):
                    continue
                result_link = result.get("link", "")
                if result_link and result_link != selected_link:
                    _remember_selection_pool_first_peer(selected, results, result)
                    return False
        except TypeError:
            return False
        return True
    try:
        only_result = results[0]
    except (IndexError, KeyError, TypeError):
        return False
    if only_result is selected:
        return True
    if not isinstance(selected, dict) or not isinstance(only_result, dict):
        return False
    only_link = only_result.get("link", "")
    selected_link = selected.get("link", "")
    if only_link and (not selected_link or only_link != selected_link):
        _remember_selection_pool_first_peer(selected, results, only_result)
        return False
    return True


def first_prefetchable_fallback_peer(
    selected, results, distinct_peer_already_checked=False
):
    """Return the first distinct result that can pass the prefetch gate."""
    if not isinstance(selected, dict):
        return None
    if not distinct_peer_already_checked and _sized_pool_has_no_distinct_peer(
        selected, results
    ):
        return None
    selected_tokens = None
    selected_meta = (
        selected.get("_meta") if isinstance(selected.get("_meta"), dict) else None
    )
    selected_meta_ready = selected_meta is not None
    seen_links = {selected.get("link", "")}
    for result in results or []:
        if not isinstance(result, dict):
            continue
        candidate_link = result.get("link", "")
        if result is selected or not candidate_link or candidate_link in seen_links:
            continue
        candidate_meta = (
            result.get("_meta") if isinstance(result.get("_meta"), dict) else None
        )
        if selected_meta_ready and candidate_meta is not None:
            if not _metadata_profiles_match(
                selected,
                result,
                primary_meta=selected_meta,
                candidate_meta=candidate_meta,
            ):
                continue
            if selected_tokens is None:
                selected_tokens = _title_tokens(selected)
            if _title_token_sets_look_related(selected_tokens, _title_tokens(result)):
                _remember_prefetch_gate_match(
                    selected, result, selected_meta, candidate_meta
                )
                return result
            continue
        if selected_tokens is None:
            selected_tokens = _title_tokens(selected)
        if not _title_token_sets_look_related(selected_tokens, _title_tokens(result)):
            continue
        if candidate_meta is not None:
            if not selected_meta_ready:
                selected_meta = _result_meta(selected)
                selected_meta_ready = True
            if _metadata_profiles_match(
                selected,
                result,
                primary_meta=selected_meta,
                candidate_meta=candidate_meta,
            ):
                _remember_prefetch_gate_match(
                    selected, result, selected_meta, candidate_meta
                )
                return result
            continue
        if not selected_meta_ready:
            selected_meta = _result_meta(selected)
            selected_meta_ready = True
        if _metadata_profiles_match(selected, result, primary_meta=selected_meta):
            candidate_meta = result.get("_meta")
            if not isinstance(candidate_meta, dict):
                candidate_meta = None
            _remember_prefetch_gate_match(
                selected, result, selected_meta, candidate_meta
            )
            return result
    return None


def _metadata_profiles_match(
    primary, candidate, primary_meta=None, candidate_meta=None
):
    """Return whether two releases are plausible same-file fallback peers.

    This is intentionally looser than manifest equality. The stream proxy still
    verifies content length and sampled byte fingerprints before switching to a
    fallback source, so this stage should gather plausible peers instead of
    rejecting reposts because their NZB subject used a different filename/group.
    """
    if primary_meta is None:
        primary_meta = _result_meta(primary)
    if candidate_meta is None:
        candidate_meta = _result_meta(candidate)
    for key in ("resolution", "codec", "container"):
        left = _meta_value_from_meta(primary_meta, key)
        right = _meta_value_from_meta(candidate_meta, key)
        if left and right and left != right:
            return False

    left_edition = _normalize_title(_meta_value_from_meta(primary_meta, "edition"))
    right_edition = _normalize_title(_meta_value_from_meta(candidate_meta, "edition"))
    if left_edition != right_edition:
        return False

    for key in ("proper", "repack", "upscaled"):
        if _meta_bool_from_meta(primary_meta, key) != _meta_bool_from_meta(
            candidate_meta, key
        ):
            return False

    left_quality = _quality_family(_meta_value_from_meta(primary_meta, "quality"))
    right_quality = _quality_family(_meta_value_from_meta(candidate_meta, "quality"))
    if left_quality and right_quality and left_quality != right_quality:
        return False

    left_hdr = set(_meta_values_from_meta(primary_meta, "hdr"))
    right_hdr = set(_meta_values_from_meta(candidate_meta, "hdr"))
    if left_hdr != right_hdr:
        return False

    left_audio = set(_meta_values_from_meta(primary_meta, "audio"))
    right_audio = set(_meta_values_from_meta(candidate_meta, "audio"))
    if left_audio and right_audio and not left_audio.intersection(right_audio):
        return False

    left_channels = _meta_value_from_meta(primary_meta, "channels")
    right_channels = _meta_value_from_meta(candidate_meta, "channels")
    if left_channels and right_channels and left_channels != right_channels:
        return False

    return True


def _manifest_group_key(result):
    """Return the manifest grouping key used to find fallback peers."""
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return None
    kind = manifest.get("payload_kind", "")
    name = manifest.get("group_name", "")
    try:
        size = int(manifest.get("group_bytes", 0) or 0)
    except (TypeError, ValueError):
        return None
    digest = manifest.get("article_digest", "")
    if not kind or not name or not digest:
        return None
    if kind == "video":
        if size <= 0:
            return None
        return kind, name, size
    if kind == "archive":
        return kind, name
    return None


def _article_digest(result):
    """Return the manifest article digest attached to a result."""
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return ""
    return manifest.get("article_digest", "") or ""


def _manifest_unsupported_reason(result):
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return ""
    return manifest.get("unsupported_reason", "") or ""


def _metadata_only_manifest_fallback_allowed(primary, candidate):
    """Return whether strict metadata may stand in for an oversized manifest."""
    primary_reason = _manifest_unsupported_reason(primary)
    candidate_reason = _manifest_unsupported_reason(candidate)
    if not primary_reason and not candidate_reason:
        return False
    if primary_reason and primary_reason not in _METADATA_ONLY_MANIFEST_REASONS:
        return False
    if candidate_reason and candidate_reason not in _METADATA_ONLY_MANIFEST_REASONS:
        return False
    return True


def _manifest_payload_kind(result):
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return ""
    return manifest.get("payload_kind", "") or ""


def _manifest_group_bytes(result):
    manifest = result.get("_fallback_manifest")
    if not isinstance(manifest, dict):
        return 0
    try:
        return int(manifest.get("group_bytes", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _fallback_peer_matches(primary, candidate):
    """Return whether candidate should be submitted as a standby fallback."""
    primary_link = primary.get("link", "")
    candidate_link = candidate.get("link", "")
    if not candidate_link or candidate_link == primary_link:
        return False

    primary_digest = _article_digest(primary)
    candidate_digest = _article_digest(candidate)
    if primary_digest and candidate_digest and candidate_digest == primary_digest:
        return False

    if not _has_prefetch_gate_match(primary, candidate):
        if not _titles_look_related(primary, candidate):
            return False

        if not _metadata_profiles_match(primary, candidate):
            return False

    return _fallback_manifest_peer_matches(primary, candidate)


def _fallback_manifest_peer_matches(primary, candidate):
    """Return whether manifest evidence allows an already-prefiltered peer."""
    primary_key = _manifest_group_key(primary)
    candidate_key = _manifest_group_key(candidate)
    if primary_key is not None and primary_key == candidate_key:
        return True

    if _metadata_only_manifest_fallback_allowed(primary, candidate):
        return True

    primary_kind = _manifest_payload_kind(primary)
    candidate_kind = _manifest_payload_kind(candidate)
    if not primary_kind or primary_kind != candidate_kind:
        return False
    if primary_kind == "video":
        primary_bytes = _manifest_group_bytes(primary)
        candidate_bytes = _manifest_group_bytes(candidate)
        return primary_bytes > 0 and primary_bytes == candidate_bytes
    if primary_kind == "archive":
        return True
    return False


def _manifest_may_match_any_peer(result):
    """Return whether this manifest can still match any fetched candidate."""
    if _manifest_group_key(result) is not None:
        return True
    if _manifest_unsupported_reason(result) in _METADATA_ONLY_MANIFEST_REASONS:
        return True
    return bool(_manifest_payload_kind(result))


def _manifest_candidate_message_ids_are_healthy(candidate):
    """Return whether a manifest candidate has usable article Message-IDs."""
    message_ids = candidate.get("message_ids") if isinstance(candidate, dict) else None
    if not isinstance(message_ids, list) or not message_ids:
        return False
    for message_id in message_ids:
        if not isinstance(message_id, str):
            return False
        clean = message_id.strip()
        if not clean or "@" not in clean:
            return False
        if any(char.isspace() or ord(char) < 0x20 for char in clean):
            return False
    return True


def _manifest_error(reason):
    """Return an unsupported manifest for fallback grouping errors."""
    return make_empty_manifest(reason)


def _fallback_settings():
    """Return (enabled, max_candidates) from Kodi settings."""
    addon = xbmcaddon.Addon()
    enabled = _setting_bool(addon, "fallback_streams_enabled", True)
    max_candidates = _setting_int(addon, "fallback_streams_max", 5)
    if max_candidates < 0 or max_candidates > _MAX_FALLBACKS:
        xbmc.log(
            "NZB-DAV: fallback_streams_max={} clamped to 0..{}".format(
                max_candidates, _MAX_FALLBACKS
            ),
            xbmc.LOGWARNING,
        )
    return enabled, max(0, min(max_candidates, _MAX_FALLBACKS))


def fallback_candidate_prefetch_settings():
    """Return fallback discovery settings for picker prefetch callers."""
    return _fallback_settings()


def fallback_candidate_prefetch_enabled(settings=None):
    """Return whether fallback discovery should scan picker peers."""
    if settings is None:
        settings = fallback_candidate_prefetch_settings()
    enabled, max_candidates = settings
    return enabled and max_candidates > 0


def selection_pool_may_have_fallback_peer(selected, results):
    """Return whether a selection pool can contain a distinct fallback peer."""
    return not _sized_pool_has_no_distinct_peer(selected, results)


def selected_manifest_may_have_fallback_peer(selected):
    """Return whether a selected result's manifest still allows fallback peers."""
    if not isinstance(selected, dict):
        return False
    selected_manifest = selected.get("_fallback_manifest")
    return not (
        isinstance(selected_manifest, dict)
        and not _manifest_may_match_any_peer(selected)
    )


def _pool_has_distinct_nzb_links(results):
    """Return whether a full result pool has at least two usable NZB links."""
    seen_links = set()
    for result in results or []:
        if not isinstance(result, dict):
            continue
        link = result.get("link", "")
        if not link:
            continue
        if seen_links and link not in seen_links:
            return True
        seen_links.add(link)
    return False


def _ensure_fallback_manifests(results):
    """Fetch missing NZB manifests for fallback grouping."""
    manifest_cache = {}
    for result in results:
        _ensure_fallback_manifest(result, manifest_cache)
    return manifest_cache


def _ensure_fallback_manifest(result, manifest_cache):
    """Fetch one missing NZB manifest using the attach-call cache."""
    manifest = result.get("_fallback_manifest")
    if isinstance(manifest, dict):
        result["_fallback_manifest_error"] = manifest.get("unsupported_reason", "")
        return manifest
    link = result.get("link", "")
    if not isinstance(link, str) or not link.strip():
        result["_fallback_manifest_error"] = "missing_link"
        return None
    if link not in manifest_cache:
        try:
            manifest_cache[link] = fetch_nzb_video_manifest(
                link, health_check=_manifest_candidate_message_ids_are_healthy
            )
        except Exception:  # pylint: disable=broad-except
            manifest_cache[link] = _manifest_error("fetch_error")
    manifest = manifest_cache[link]
    if not isinstance(manifest, dict):
        manifest = _manifest_error("fetch_error")
        manifest_cache[link] = manifest
    result["_fallback_manifest"] = manifest
    result["_fallback_manifest_error"] = manifest.get("unsupported_reason", "")
    return manifest


def _attach_candidates_for_target(target, pool, max_candidates):
    candidates = []
    seen_links = {target.get("link", "")}
    target_digest = _article_digest(target)
    seen_article_digests = {target_digest} if target_digest else set()
    for candidate in pool:
        if candidate is target:
            continue
        candidate_link = candidate.get("link", "")
        candidate_digest = _article_digest(candidate)
        if (
            not candidate_link
            or candidate_link in seen_links
            or (candidate_digest and candidate_digest in seen_article_digests)
            or not _fallback_peer_matches(target, candidate)
        ):
            continue
        candidates.append(candidate)
        seen_links.add(candidate_link)
        if candidate_digest:
            seen_article_digests.add(candidate_digest)
        if len(candidates) >= max_candidates:
            break
    target["_fallback_candidates"] = candidates


def _attach_manifest_candidate_if_matching(
    selected, candidate, candidates, seen_candidate_links, seen_article_digests
):
    """Attach a fetched candidate when manifest evidence still matches."""
    candidate_link = candidate.get("link", "")
    candidate_digest = _article_digest(candidate)
    if (
        candidate_link in seen_candidate_links
        or (candidate_digest and candidate_digest in seen_article_digests)
        or not _fallback_manifest_peer_matches(selected, candidate)
    ):
        return False
    candidates.append(candidate)
    seen_candidate_links.add(candidate_link)
    if candidate_digest:
        seen_article_digests.add(candidate_digest)
    return True


def _fetch_selection_manifest_for_queue(kind, index, target, result_queue):
    """Fetch one selection manifest target and publish it to the collector."""
    try:
        _ensure_fallback_manifest(target, {})
    except Exception:  # pylint: disable=broad-except
        target["_fallback_manifest"] = _manifest_error("fetch_error")
        target["_fallback_manifest_error"] = "fetch_error"
    finally:
        result_queue.put((kind, index, target))


def _start_selection_manifest_fetch(kind, index, target, result_queue):
    """Start one daemon manifest fetch, falling back to inline execution."""
    thread = threading.Thread(
        target=_fetch_selection_manifest_for_queue,
        args=(kind, index, target, result_queue),
        name="nzbdav-fallback-manifest",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        _fetch_selection_manifest_for_queue(kind, index, target, result_queue)


def _attach_ready_selection_candidates(
    selected,
    completed,
    next_to_attach,
    candidates,
    seen_candidate_links,
    seen_article_digests,
    max_candidates,
    misses_seen,
    consumed_indices,
):
    """Attach completed candidate manifests in result order."""
    while next_to_attach[0] in consumed_indices:
        next_to_attach[0] += 1
    while next_to_attach[0] in completed:
        ready_index = next_to_attach[0]
        ready_candidate = completed.pop(ready_index)
        consumed_indices.add(ready_index)
        attached = _attach_manifest_candidate_if_matching(
            selected,
            ready_candidate,
            candidates,
            seen_candidate_links,
            seen_article_digests,
        )
        if not attached:
            misses_seen[0] += 1
        next_to_attach[0] += 1
        while next_to_attach[0] in consumed_indices:
            next_to_attach[0] += 1
        if len(candidates) >= max_candidates:
            return True
    remaining_slots = max_candidates - len(candidates)
    if len(completed) >= remaining_slots > 0:
        for ready_index in sorted(completed):
            ready_candidate = completed.pop(ready_index)
            consumed_indices.add(ready_index)
            attached = _attach_manifest_candidate_if_matching(
                selected,
                ready_candidate,
                candidates,
                seen_candidate_links,
                seen_article_digests,
            )
            if not attached:
                misses_seen[0] += 1
            if len(candidates) >= max_candidates:
                return True
    return False


def _attach_selection_candidates_streaming(
    selected,
    candidate_iter,
    candidates,
    seen_candidate_links,
    seen_article_digests,
    include_selected_manifest,
    max_candidates,
):
    """Fetch selected fallback manifests with a rolling ordered window."""
    result_queue = Queue()
    completed = {}
    next_candidate_index = [0]
    next_to_attach = [0]
    active = [0]
    active_candidates = [0]
    candidate_iter = iter(candidate_iter)
    candidate_exhausted = [False]
    pending_to_start = []
    misses_seen = [0]
    consumed_indices = set()
    selected_ready = [not include_selected_manifest]
    selected_can_match = [True]
    optional_tail_deadline = [None]
    max_workers = min(max_candidates, _MAX_FALLBACKS)

    def _start_candidate_fetch():
        if candidate_exhausted[0]:
            return False
        if pending_to_start:
            candidate = pending_to_start.pop(0)
        else:
            try:
                candidate = next(candidate_iter)
            except StopIteration:
                candidate_exhausted[0] = True
                return False
        index = next_candidate_index[0]
        next_candidate_index[0] += 1
        active[0] += 1
        active_candidates[0] += 1
        _start_selection_manifest_fetch("candidate", index, candidate, result_queue)
        return True

    def _fill_candidate_window():
        speculative_slots = min(misses_seen[0], max_candidates - len(candidates))
        while (
            selected_can_match[0]
            and len(candidates) < max_candidates
            and active_candidates[0] < max_workers
            and len(candidates) + active_candidates[0] + len(completed)
            < max_candidates + speculative_slots
            and _start_candidate_fetch()
        ):
            speculative_slots = min(misses_seen[0], max_candidates - len(candidates))

    def _can_start_stall_speculation():
        return (
            selected_ready[0]
            and selected_can_match[0]
            and len(candidates) < max_candidates
            and active_candidates[0] > 0
            and active_candidates[0] < max_workers
            and not candidate_exhausted[0]
        )

    def _optional_tail_wait_remaining():
        if not (
            selected_ready[0]
            and selected_can_match[0]
            and candidate_exhausted[0]
            and candidates
            and len(candidates) < max_candidates
            and active_candidates[0] > 0
        ):
            optional_tail_deadline[0] = None
            return None
        now = time.monotonic()
        if optional_tail_deadline[0] is None:
            optional_tail_deadline[0] = (
                now + _FALLBACK_MANIFEST_OPTIONAL_TAIL_WAIT_SECONDS
            )
        return max(0, optional_tail_deadline[0] - now)

    try:
        pending_to_start.append(next(candidate_iter))
    except StopIteration:
        candidate_exhausted[0] = True

    if not pending_to_start and candidate_exhausted[0]:
        return True

    if include_selected_manifest:
        active[0] += 1
        _start_selection_manifest_fetch("selected", -1, selected, result_queue)

    _fill_candidate_window()

    while active[0]:
        try:
            tail_wait = _optional_tail_wait_remaining()
            if tail_wait is not None:
                if tail_wait <= 0:
                    return True
                kind, index, target = result_queue.get(timeout=tail_wait)
            elif _can_start_stall_speculation():
                kind, index, target = result_queue.get(
                    timeout=_FALLBACK_MANIFEST_STALL_SPECULATION_SECONDS
                )
            else:
                kind, index, target = result_queue.get()
        except Empty:
            if _optional_tail_wait_remaining() is not None:
                return True
            _start_candidate_fetch()
            continue
        active[0] -= 1
        if kind == "candidate":
            active_candidates[0] -= 1
            completed[index] = target
        else:
            selected_ready[0] = True
            selected_digest = _article_digest(selected)
            if selected_digest:
                seen_article_digests.add(selected_digest)
            selected_can_match[0] = _manifest_may_match_any_peer(selected)

        if selected_ready[0] and not selected_can_match[0]:
            return False

        if selected_ready[0] and selected_can_match[0]:
            if _attach_ready_selection_candidates(
                selected,
                completed,
                next_to_attach,
                candidates,
                seen_candidate_links,
                seen_article_digests,
                max_candidates,
                misses_seen,
                consumed_indices,
            ):
                return True

        _fill_candidate_window()

    return selected_can_match[0]


def _prefetch_candidate_matches(
    target, candidate, seen_links, target_tokens=None, target_meta=None
):
    """Return whether a candidate is worth fetching manifest evidence for."""
    if candidate is target:
        return False
    candidate_link = candidate.get("link", "")
    if not candidate_link or candidate_link in seen_links:
        return False
    candidate_meta = candidate.get("_meta")
    if not isinstance(candidate_meta, dict):
        candidate_meta = None
    candidate_has_meta = candidate_meta is not None
    if target_tokens is not None and (not candidate_has_meta or target_meta is None):
        if not _title_token_sets_look_related(target_tokens, _title_tokens(candidate)):
            return False
        return _metadata_profiles_match(
            target, candidate, primary_meta=target_meta, candidate_meta=candidate_meta
        )
    if not _metadata_profiles_match(
        target, candidate, primary_meta=target_meta, candidate_meta=candidate_meta
    ):
        return False
    if target_tokens is None:
        titles_match = _titles_look_related(target, candidate)
    else:
        titles_match = _title_token_sets_look_related(
            target_tokens, _title_tokens(candidate)
        )
    if not titles_match:
        return False
    return True


def attach_fallback_candidates(results):
    """Attach duplicate fallback candidates to each result in-place.

    Every result receives ``_fallback_candidates``. When fallback streams are
    disabled, the cap is zero, or a result cannot be conservatively matched,
    the attached list is empty.
    """
    for result in results:
        result["_fallback_candidates"] = []

    if not _pool_has_distinct_nzb_links(results):
        return results

    enabled, max_candidates = _fallback_settings()
    if not enabled or max_candidates <= 0:
        return results

    prefetchable_results = []
    for result in results:
        if first_prefetchable_fallback_peer(
            result, results, distinct_peer_already_checked=True
        ):
            prefetchable_results.append(result)
    if not prefetchable_results:
        return results

    _ensure_fallback_manifests(prefetchable_results)
    for result in prefetchable_results:
        _attach_candidates_for_target(result, prefetchable_results, max_candidates)

    return results


def attach_fallback_candidates_for_selection(selected, results, fallback_settings=None):
    """Attach fallback candidates only for the result the user selected."""
    if selected:
        selected["_fallback_candidates"] = []

    if not selected:
        return selected
    if not selected_manifest_may_have_fallback_peer(selected):
        return selected
    if results is None or _sized_pool_has_no_distinct_peer(selected, results):
        return selected

    if fallback_settings is None:
        fallback_settings = _fallback_settings()
    enabled, max_candidates = fallback_settings
    if not enabled or max_candidates <= 0:
        return selected

    seen_prefetch_links = {selected.get("link", "")}
    selected_title_tokens = None
    selected_meta = selected.get("_meta")
    if not isinstance(selected_meta, dict):
        selected_meta = None
    candidates = []
    seen_candidate_links = {selected.get("link", "")}
    seen_article_digests = set()
    selected_manifest_ready = isinstance(selected.get("_fallback_manifest"), dict)
    if selected_manifest_ready:
        selected_digest = _article_digest(selected)
        if selected_digest:
            seen_article_digests.add(selected_digest)

    def _prefetch_candidates():
        # Keep prefiltering lazy so all-matching pools still stop after the cap
        # instead of fetching manifests for the rest of the result list.
        selected_title_tokens_ref = [selected_title_tokens]
        selected_meta_ref = [selected_meta]
        for candidate in results or []:
            if candidate is selected or not isinstance(candidate, dict):
                continue
            candidate_link = candidate.get("link", "")
            if not candidate_link or candidate_link in seen_prefetch_links:
                continue
            if _has_prefetch_gate_match(selected, candidate):
                prefetch_match = True
            else:
                candidate_meta = candidate.get("_meta")
                if not isinstance(candidate_meta, dict):
                    candidate_meta = None
                prefetch_tokens = selected_title_tokens_ref[0]
                if prefetch_tokens is None and (
                    selected_meta_ref[0] is None or candidate_meta is None
                ):
                    prefetch_tokens = _title_tokens(selected)
                    selected_title_tokens_ref[0] = prefetch_tokens
                prefetch_match = _prefetch_candidate_matches(
                    selected,
                    candidate,
                    seen_prefetch_links,
                    prefetch_tokens,
                    selected_meta_ref[0],
                )
                if selected_title_tokens_ref[0] is None:
                    selected_title_tokens_ref[0] = _cached_title_tokens(selected)
            if selected_meta_ref[0] is None:
                cached_selected_meta = selected.get("_meta")
                if isinstance(cached_selected_meta, dict):
                    selected_meta_ref[0] = cached_selected_meta
            if not prefetch_match:
                continue
            seen_prefetch_links.add(candidate_link)
            yield candidate

    _attach_selection_candidates_streaming(
        selected,
        _prefetch_candidates(),
        candidates,
        seen_candidate_links,
        seen_article_digests,
        include_selected_manifest=not selected_manifest_ready,
        max_candidates=max_candidates,
    )
    selected["_fallback_candidates"] = candidates
    return selected


def build_fallback_job_name(title, nzb_url, index):
    """Return a stable, traceable nzbdav job name for a fallback candidate."""
    clean_title = title if isinstance(title, str) else ""
    clean_title = re.sub(r"[^A-Za-z0-9._ -]+", " ", clean_title)
    clean_title = " ".join(clean_title.split())[:180].strip()
    if not clean_title:
        clean_title = "fallback"

    digest = hashlib.sha256(str(nzb_url).encode("utf-8")).hexdigest()[:8]
    job_name = "{} [fallback-{}-{}]".format(clean_title, index, digest)
    if not _SAFE_JOB_RE.match(job_name):
        job_name = re.sub(r"[^A-Za-z0-9._ -]+", " ", job_name)
        job_name = " ".join(job_name.split())
    return job_name


def build_prepare_fallback_payload(fallback_jobs):
    """Build the service prepare manifest payload for fallback jobs."""
    payload = []
    for job in fallback_jobs:
        nzo_id = job.get("nzo_id") if isinstance(job, dict) else None
        if not nzo_id:
            continue
        payload.append(
            {
                "title": job.get("title", ""),
                "nzb_url": job.get("nzb_url", ""),
                "job_name": job.get("job_name", ""),
                "nzo_id": nzo_id,
                "stream_url": job.get("stream_url") or "",
                "stream_headers": job.get("stream_headers") or {},
                "content_length": job.get("content_length") or 0,
            }
        )
    return payload


def fingerprint_ranges(content_length):
    """Return byte ranges used to prove two stream URLs expose the same file."""
    return list(_fingerprint_ranges_for_length(content_length))


@lru_cache(maxsize=512)
def _fingerprint_ranges_for_length(content_length):
    """Return cached immutable fingerprint ranges for a content length."""
    if content_length <= 0:
        return ()
    if content_length <= _FINGERPRINT_BYTES:
        return ((0, content_length - 1),)

    if content_length <= _FINGERPRINT_SAMPLE_COUNT * _FINGERPRINT_BYTES:
        ranges = []
        start = 0
        while start < content_length:
            end = min(content_length - 1, start + _FINGERPRINT_BYTES - 1)
            ranges.append((start, end))
            start += _FINGERPRINT_BYTES
        return tuple(ranges)

    max_start = content_length - _FINGERPRINT_BYTES
    starts = {0, max_start}
    counter = 0
    while len(starts) < _FINGERPRINT_SAMPLE_COUNT:
        digest = hashlib.sha256(
            "{}:{}".format(content_length, counter).encode("utf-8")
        ).digest()
        starts.add(int.from_bytes(digest[:8], "big") % (max_start + 1))
        counter += 1
    return tuple((start, start + _FINGERPRINT_BYTES - 1) for start in sorted(starts))


def fetch_content_length(url, auth_header, timeout=10, probe_bases=None):
    """Return Content-Length for a WebDAV stream URL, or 0."""
    probe_url = _validated_probe_url_for_fetch(url, probe_bases=probe_bases)
    if not probe_url:
        return 0
    req = Request(probe_url, method="HEAD")
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        # nosemgrep
        with urlopen(req, timeout=timeout) as resp:  # nosec B310
            return int(resp.headers.get("Content-Length", "0") or 0)
    except (HTTPError, URLError, OSError, TypeError, ValueError):
        return 0


def _content_range_matches_request(content_range, start, end, content_length=0):
    """Return whether a Content-Range header matches a requested range."""
    if not isinstance(content_range, str):
        return False
    match = _CONTENT_RANGE_RE.match(content_range.strip())
    if not match:
        return False
    try:
        if int(match.group(1)) != start or int(match.group(2)) != end:
            return False
        if content_length:
            total = match.group(3)
            return total != "*" and int(total) == int(content_length)
        return True
    except ValueError:
        return False


def fetch_range_bytes(
    url,
    auth_header,
    start,
    end,
    timeout=10,
    content_length=0,
    probe_bases=None,
):
    """Read a validated byte range from a configured WebDAV stream URL."""
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    try:
        content_length = int(content_length or 0)
    except (TypeError, ValueError):
        return None
    if start < 0 or end < start or content_length < 0:
        return None
    if content_length and end >= content_length:
        return None

    probe_url = _validated_probe_url_for_fetch(url, probe_bases=probe_bases)
    if not probe_url:
        return None
    req = Request(probe_url)
    if auth_header:
        req.add_header("Authorization", auth_header)
    req.add_header("Range", "bytes={}-{}".format(start, end))
    try:
        # nosemgrep
        with urlopen(req, timeout=timeout) as resp:  # nosec B310
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 206:
                return None
            if not _content_range_matches_request(
                resp.headers.get("Content-Range"), start, end, content_length
            ):
                return None
            body = resp.read(end - start + 1)
    except (HTTPError, URLError, OSError, ValueError):
        return None
    if len(body) != end - start + 1:
        return None
    return body


def fetch_range_digest(
    url,
    auth_header,
    start,
    end,
    timeout=10,
    content_length=0,
    probe_bases=None,
):
    """Read a byte range and return a SHA-256 digest of the returned bytes."""
    body = fetch_range_bytes(
        url,
        auth_header,
        start,
        end,
        timeout=timeout,
        content_length=content_length,
        probe_bases=probe_bases,
    )
    if body is None:
        return None
    return hashlib.sha256(body).hexdigest()
