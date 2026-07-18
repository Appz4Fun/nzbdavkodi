# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import,unused-import

"""Conservative grouping for duplicate releases usable as fallback streams.

Cohesive helper groups are split into sibling modules (``fallback_streams_probe``,
``_identity``, ``_match``, ``_select``, ``_attach``) to keep every module below
Codacy's 500-NLOC file gate. Each is re-exported at the bottom so test imports
and ``@patch("resources.lib.fallback_streams.<name>")`` decorators keep
resolving; the moved helpers reach back via ``import
resources.lib.fallback_streams as _fs`` and call ``_fs.<name>`` so every patch on
this module is honored at call time and there is no top-level import cycle.

This module keeps only the redirect-refusing ``urlopen`` machinery (patched as
``fallback_streams.urlopen``), the small shared literals/regexes, the public
``FALLBACK_CANDIDATES_DISABLED`` sentinel, and the library symbols the siblings
reach through ``_fs`` (``xbmc``/``xbmcaddon``/``telemetry``/``pubdate_to_epoch``/
``fetch_nzb_video_manifest``/``make_empty_manifest``/``SimpleNamespace``).
"""

import re
from types import SimpleNamespace  # noqa: F401  (sibling _fs.SimpleNamespace access)
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, build_opener

import xbmc  # noqa: F401  (sibling _fs.xbmc access)
import xbmcaddon  # noqa: F401  (sibling _fs.xbmcaddon access)

from resources.lib import telemetry  # noqa: F401  (patched fallback_streams.telemetry)
from resources.lib.http_util import pubdate_to_epoch  # noqa: F401  (sibling _fs access)
from resources.lib.nzb_manifest import (  # noqa: F401  (sibling _fs access)
    fetch_nzb_video_manifest,
    make_empty_manifest,
)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse every HTTP redirect so origin pinning isn't bypassed.

    The fingerprint probes pin the request URL to the configured-origin
    allow-list via ``_validated_probe_url``, but a vanilla
    ``urlopen`` opener follows up to 10 redirects — a 302 to a
    different origin would silently bypass the allow-list, and on
    Python <3.11 the Authorization header even leaks across redirects.
    Raising ``HTTPError`` on the 3xx surfaces the redirect as a probe
    failure (None / 0 / empty digest) at every call site.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, msg, headers, fp)


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def _no_redirect_urlopen(req, timeout):
    """Open ``req`` without following any HTTP redirect.

    Centralized helper so the fingerprint probe path
    (``fetch_content_length`` / ``fetch_range_bytes`` /
    ``fetch_range_digest``) always uses the same allow-list-respecting
    opener instead of the redirect-following module-level ``urlopen``.

    Routes through the module-level ``urlopen`` symbol so existing
    tests that patch ``resources.lib.fallback_streams.urlopen`` keep
    working unchanged. In production the symbol is rebound below to
    the no-redirect opener's ``open`` method, so production traffic
    gets redirect rejection while tests intercept as before.
    """
    # nosemgrep
    return urlopen(req, timeout=timeout)  # nosec B310


urlopen = _NO_REDIRECT_OPENER.open  # noqa: F811
_SAFE_JOB_RE = re.compile(r"^[A-Za-z0-9._ \[\]-]+$")
_NON_WORD_RE = re.compile(r"[\W_]+")
_CONJUNCTION_TOKENS = frozenset(("and", "et", "und"))
_INVALID_TITLE_RE = re.compile(r"[^A-Za-z0-9._ -]+")
# Ceiling for fallback_streams_max (schema default stays 5). Raised to 10:
# episode WEB-DL repost pools legitimately offer ~10 same-file articles
# posted on different days, and repeated source failures should be able
# to consume them all as standbys.
_MAX_FALLBACKS = 10
_SAME_POST_WINDOW_SECONDS = 3600
_FALLBACK_MANIFEST_STALL_SPECULATION_SECONDS = 0.05
_FALLBACK_MANIFEST_OPTIONAL_TAIL_WAIT_SECONDS = 0.1
# Grace period to let an earlier-indexed candidate that is still in flight
# finish before the cap is filled from later out-of-order completions. Keeps
# the non-blocking "skip a genuinely slow gap" behavior while never skipping an
# earlier (higher-priority) peer that is about to arrive.
_FALLBACK_MANIFEST_SETTLE_WINDOW_SECONDS = 0.04
_METADATA_ONLY_MANIFEST_REASONS = frozenset(("too_large",))
_INDEXER_SIZE_SYNTHETIC_MANIFEST_REASONS = frozenset(("invalid_xml", "no_video_file"))
_INDEXER_SIZE_SYNTHETIC_MIN_BYTES = 100 * 1024 * 1024
FALLBACK_CANDIDATES_DISABLED = object()


# Cohesive helper groups split into sibling modules to keep this module
# below Codacy's 500-NLOC file gate. Re-exported here so test imports and
# ``@patch("resources.lib.fallback_streams.<name>")`` keep resolving; moved
# helpers reach back via a module-level ``import resources.lib.fallback_streams
# as _fs`` (call-time ``_fs.<name>`` resolution preserves patches; the import
# cycle is benign/latent).
from resources.lib.fallback_streams_attach import (  # noqa: F401,E402  pylint: disable=wrong-import-position
    _attach_candidates_for_target,
    _best_ranked_in_cluster,
    _candidate_is_fresh_peer,
    _candidate_pubdate_epoch,
    _dedupe_candidates_by_pubdate,
    _partition_ranked_by_pubdate,
    _pool_has_distinct_nzb_links,
    _prefetch_candidate_matches,
    _prefetch_candidate_passes_preconditions,
    _prefetch_same_group_profile_match,
    _prefetch_title_first,
    _prefetch_titles_match,
    _rank_fallback_candidates,
    _ranking_tuple_for_candidate,
    _safe_len,
    build_fallback_job_name,
    build_prepare_fallback_payload,
)
from resources.lib.fallback_streams_identity import (  # noqa: F401,E402  pylint: disable=wrong-import-position
    _PART_LABEL_RE,
    _PART_ORDINAL_WORDS,
    _QUALITY_FAMILY_MARKERS,
    _RELEASE_IDENTITY_CACHE_TITLE_KEY,
    _RELEASE_IDENTITY_CACHE_VALUE_KEY,
    _SEQUEL_TAIL_TOKENS,
    _TITLE_STOP_TOKENS,
    _TITLE_TOKEN_CACHE_TITLE_KEY,
    _TITLE_TOKEN_CACHE_VALUE_KEY,
    _cached_title_tokens,
    _collapse_phantom_season,
    _content_discriminators_match,
    _disjoint_titles_related,
    _episode_content_matches,
    _episode_set_pair_matches,
    _identity_corroborated,
    _identity_is_episodic,
    _is_content_title_token,
    _meta_bool_from_meta,
    _meta_value,
    _meta_value_from_meta,
    _meta_values_from_meta,
    _normalize_title,
    _parsed_title_fields,
    _part_number_from_title,
    _quality_family,
    _release_identity,
    _release_similarity,
    _release_size_bytes,
    _release_size_within,
    _result_meta,
    _same_content,
    _same_content_seasonal_tail,
    _same_meta_value,
    _sorted_int_tuple,
    _subset_titles_related,
    _title_token_sets_look_related,
    _title_tokens,
    _titles_core_related,
    _titles_look_related,
)
from resources.lib.fallback_streams_match import (  # noqa: F401,E402  pylint: disable=wrong-import-position
    _PEER_BYTES_TOLERANCE_FRACTION,
    _PREFETCH_INDEXER_SIZE_TOLERANCE_FRACTION,
    _PREFETCH_PROOF_KEY,
    _SELECTION_POOL_FIRST_PEER_KEY,
    _TIER0_SIZE_FRACTION,
    _article_digest,
    _cross_group_mirror_exception,
    _fallback_manifest_peer_matches,
    _fallback_peer_matches,
    _has_prefetch_gate_match,
    _hdr_audio_channels_match,
    _is_distinct_dict_peer,
    _is_webdl_quality,
    _manifest_candidate_message_ids_are_healthy,
    _manifest_error,
    _manifest_group_bytes,
    _manifest_group_bytes_within_tolerance,
    _manifest_group_key,
    _manifest_group_key_fields_present,
    _manifest_group_size_bytes,
    _manifest_may_match_any_peer,
    _manifest_normalized_video_name,
    _manifest_payload_kind,
    _manifest_unsupported_reason,
    _manifest_with_indexer_size_fallback,
    _message_id_is_healthy,
    _meta_bool_flags_match,
    _meta_string_fields_match,
    _metadata_only_manifest_fallback_allowed,
    _metadata_profile_signature,
    _metadata_profiles_match,
    _multi_result_pool_has_no_distinct_peer,
    _near_exact_size_match,
    _prefetch_gate_proof,
    _prefetch_peer_match,
    _prefetch_peer_match_meta_ready,
    _prefetch_peer_match_title_first,
    _prefetch_peer_meta,
    _prefetch_peer_state,
    _prefetch_peer_tokens,
    _prefetch_size_gate_match,
    _remember_prefetch_gate_match,
    _remember_selection_pool_first_peer,
    _result_indexer_size,
    _result_meta_or_none,
    _same_group_resolution_gate,
    _shared_profile_fields_match,
    _single_result_pool_has_no_distinct_peer,
    _sized_pool_has_no_distinct_peer,
    _synthetic_indexer_size_manifest,
    _title_profile_gate_passes,
    cached_selection_pool_first_peer,
    first_prefetchable_fallback_peer,
)
from resources.lib.fallback_streams_probe import (  # noqa: F401,E402  pylint: disable=wrong-import-position
    _ADDON_SETTINGS_SCHEMA,
    _ALLOWED_STREAM_SCHEMES,
    _CONTENT_RANGE_RE,
    _FALLBACK_MANIFEST_CACHE,
    _FALLBACK_MANIFEST_CACHE_LOCK,
    _FALLBACK_MANIFEST_CACHE_MAX_ENTRIES,
    _FALLBACK_MANIFEST_CACHE_TTL_SECONDS,
    _FINGERPRINT_BYTES,
    _FINGERPRINT_DENSE_SAMPLE_MIN_BYTES,
    _FINGERPRINT_SAMPLE_COUNT,
    _FINGERPRINT_SMALL_SAMPLE_COUNT,
    _cached_fallback_manifest,
    _cached_validated_probe_url,
    _configured_stream_bases,
    _content_range_matches_request,
    _copy_manifest,
    _fallback_manifest_cache_key,
    _fallback_manifest_cache_now,
    _fetch_fallback_manifest,
    _fingerprint_ranges_for_length,
    _fingerprint_sample_count,
    _http_url_parts_are_valid,
    _normalized_range_content_length,
    _origin_key,
    _path_is_under_base,
    _PrecomputedProbeBase,
    _probe_base_components,
    _range_bounds_valid,
    _read_validated_range,
    _schema_setting_default,
    _setting_bool,
    _setting_default_from_root,
    _setting_int,
    _split_http_url,
    _store_fallback_manifest,
    _validated_probe_url,
    _validated_probe_url_for_fetch,
    clear_fallback_manifest_cache,
    configured_stream_probe_bases,
    fetch_content_length,
    fetch_range_bytes,
    fetch_range_digest,
    fingerprint_ranges,
)
from resources.lib.fallback_streams_select import (  # noqa: F401,E402  pylint: disable=wrong-import-position
    SelectionAttachState,
    _advance_past_consumed,
    _already_attached,
    _attach_manifest_candidate_if_matching,
    _attach_ready_selection_candidates,
    _attach_selection_candidates_streaming,
    _classify_stream_wait_outcome,
    _consume_ready_candidate,
    _ensure_fallback_manifest,
    _ensure_fallback_manifests,
    _fallback_settings,
    _fetch_selection_manifest_for_queue,
    _fill_cap_from_completed,
    _iter_selection_prefetch_candidates,
    _post_record_action,
    _prefetchable_results,
    _prime_first_candidate,
    _resolve_fallback_settings,
    _selection_pool_admits_fallback,
    _selection_prefetch_candidate_eligible,
    _selection_prefetch_candidate_matches,
    _selection_prefetch_uncached_match,
    _selection_seed_article_digests,
    _start_selection_manifest_fetch,
    attach_fallback_candidates,
    attach_fallback_candidates_for_selection,
    fallback_candidate_prefetch_enabled,
    fallback_candidate_prefetch_settings,
    selected_manifest_may_have_fallback_peer,
    selection_pool_may_have_fallback_peer,
)
