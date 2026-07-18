# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Fallback peer profile/manifest matching and prefetch-gate helpers."""

import hashlib

import resources.lib.fallback_streams as _fs

_PREFETCH_PROOF_KEY = "_fallback_prefetch_gate_proof"


_SELECTION_POOL_FIRST_PEER_KEY = "_fallback_selection_pool_first_peer"


def _metadata_profile_signature(meta):
    """Return the title/profile fields covered by the fallback prefetch gate."""
    return (
        _fs._meta_value_from_meta(meta, "resolution"),
        _fs._meta_value_from_meta(meta, "codec"),
        _fs._meta_value_from_meta(meta, "container"),
        _fs._normalize_title(_fs._meta_value_from_meta(meta, "edition")),
        _fs._meta_bool_from_meta(meta, "proper"),
        _fs._meta_bool_from_meta(meta, "repack"),
        _fs._meta_bool_from_meta(meta, "upscaled"),
        _fs._quality_family(_fs._meta_value_from_meta(meta, "quality")),
        tuple(sorted(set(_fs._meta_values_from_meta(meta, "hdr")))),
        tuple(sorted(set(_fs._meta_values_from_meta(meta, "audio")))),
        _fs._meta_value_from_meta(meta, "channels"),
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
        _fs._metadata_profile_signature(primary_meta),
        _fs._metadata_profile_signature(candidate_meta),
    )


def _remember_prefetch_gate_match(
    primary, candidate, primary_meta=None, candidate_meta=None
):
    """Store proof that the candidate passed the fallback title/profile gate."""
    proof = _fs._prefetch_gate_proof(
        primary, candidate, primary_meta=primary_meta, candidate_meta=candidate_meta
    )
    if proof is not None:
        candidate[_fs._PREFETCH_PROOF_KEY] = proof


def _has_prefetch_gate_match(primary, candidate):
    """Return whether a candidate still matches a prior prefetch-gate proof."""
    if not isinstance(primary, dict) or not isinstance(candidate, dict):
        return False
    proof = candidate.get(_fs._PREFETCH_PROOF_KEY)
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
    return proof == _fs._prefetch_gate_proof(primary, candidate)


def _remember_selection_pool_first_peer(selected, results, peer):
    """Store the first distinct peer found during a selection-pool scan."""
    if isinstance(selected, dict) and isinstance(peer, dict):
        selected[_fs._SELECTION_POOL_FIRST_PEER_KEY] = (
            id(results),
            selected.get("link", ""),
            peer,
        )


def cached_selection_pool_first_peer(selected, results):
    """Return the first distinct peer found by a matching pool scan."""
    if not isinstance(selected, dict):
        return None
    cached = selected.get(_fs._SELECTION_POOL_FIRST_PEER_KEY)
    if not isinstance(cached, tuple) or len(cached) != 3:
        return None
    results_id, selected_link, peer = cached
    if results_id != id(results) or selected_link != selected.get("link", ""):
        return None
    if not isinstance(peer, dict):
        return None
    return peer


def _multi_result_pool_has_no_distinct_peer(selected, results):
    """Return True when a multi-result pool has no distinct fallback peer."""
    if not isinstance(selected, dict):
        return False
    selected_link = selected.get("link", "")
    try:
        for result in results:
            if result is selected or not isinstance(result, dict):
                continue
            result_link = result.get("link", "")
            if result_link and result_link != selected_link:
                _fs._remember_selection_pool_first_peer(selected, results, result)
                return False
    except TypeError:
        return False
    return True


def _single_result_pool_has_no_distinct_peer(selected, results):
    """Return True when a one-result pool has no distinct fallback peer."""
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
        _fs._remember_selection_pool_first_peer(selected, results, only_result)
        return False
    return True


def _sized_pool_has_no_distinct_peer(selected, results):
    """Return True when a sized pool cannot contain any fallback peer."""
    try:
        result_count = len(results)
    except TypeError:
        return False
    if result_count == 0:
        return True
    if result_count != 1:
        return _fs._multi_result_pool_has_no_distinct_peer(selected, results)
    return _fs._single_result_pool_has_no_distinct_peer(selected, results)


def _prefetch_peer_state(selected):
    """Return mutable [tokens, meta, meta_ready] scan state for a selected result."""
    selected_meta = (
        selected.get("_meta") if isinstance(selected.get("_meta"), dict) else None
    )
    return [None, selected_meta, selected_meta is not None]


def _prefetch_peer_tokens(selected, state):
    """Return cached selected title tokens, computing them once on demand."""
    if state[0] is None:
        state[0] = _fs._title_tokens(selected)
    return state[0]


def _prefetch_peer_meta(selected, state):
    """Return the selected metadata dict, deriving + caching it on demand."""
    if not state[2]:
        state[1] = _fs._result_meta(selected)
        state[2] = True
    return state[1]


def _prefetch_peer_match(selected, result, candidate_meta, state):
    """Return ``result`` when it passes the full prefetch gate, else ``None``.

    Mirrors the original per-candidate branch structure exactly: the cheap
    profile gate runs first only when both metadata dicts are ready; otherwise
    the title prefilter runs before the profile parse.
    """
    if state[2] and candidate_meta is not None:
        return _fs._prefetch_peer_match_meta_ready(
            selected, result, candidate_meta, state
        )

    tokens = _fs._prefetch_peer_tokens(selected, state)
    if not _fs._title_token_sets_look_related(tokens, _fs._title_tokens(result)):
        return None
    return _fs._prefetch_peer_match_title_first(selected, result, candidate_meta, state)


def _prefetch_peer_match_meta_ready(selected, result, candidate_meta, state):
    """Cheap-profile-first prefetch gate when both metadata dicts are ready."""
    if not _fs._metadata_profiles_match(
        selected,
        result,
        primary_meta=state[1],
        candidate_meta=candidate_meta,
        require_same_group=True,
    ):
        return None
    tokens = _fs._prefetch_peer_tokens(selected, state)
    if _fs._title_token_sets_look_related(
        tokens, _fs._title_tokens(result)
    ) and _fs._same_content(selected, result):
        _fs._remember_prefetch_gate_match(selected, result, state[1], candidate_meta)
        return result
    return None


def _prefetch_peer_match_title_first(selected, result, candidate_meta, state):
    """Prefetch gate tail after the title prefilter already passed."""
    selected_meta = _fs._prefetch_peer_meta(selected, state)
    if candidate_meta is None:
        resolved_meta = result.get("_meta")
        if not isinstance(resolved_meta, dict):
            resolved_meta = None
    else:
        resolved_meta = candidate_meta
    if _fs._metadata_profiles_match(
        selected,
        result,
        primary_meta=selected_meta,
        candidate_meta=candidate_meta,
        require_same_group=True,
    ) and _fs._same_content(selected, result):
        _fs._remember_prefetch_gate_match(
            selected, result, selected_meta, resolved_meta
        )
        return result
    return None


def first_prefetchable_fallback_peer(
    selected, results, distinct_peer_already_checked=False
):
    """Return the first distinct result that can pass the prefetch gate."""
    if not isinstance(selected, dict):
        return None
    if not distinct_peer_already_checked and _fs._sized_pool_has_no_distinct_peer(
        selected, results
    ):
        return None
    state = _fs._prefetch_peer_state(selected)
    seen_links = {selected.get("link", "")}
    for result in results or []:
        if not _fs._is_distinct_dict_peer(result, selected, seen_links):
            continue
        match = _fs._prefetch_peer_match(
            selected, result, _fs._result_meta_or_none(result), state
        )
        if match is not None:
            return match
    return None


def _result_meta_or_none(result):
    """Return a result's already-computed ``_meta`` dict, or None."""
    meta = result.get("_meta")
    return meta if isinstance(meta, dict) else None


def _is_distinct_dict_peer(result, selected, seen_links):
    """Return whether ``result`` is a distinct dict peer with a usable link."""
    if not isinstance(result, dict) or result is selected:
        return False
    candidate_link = result.get("link", "")
    return bool(candidate_link) and candidate_link not in seen_links


def _metadata_profiles_match(
    primary, candidate, primary_meta=None, candidate_meta=None, require_same_group=False
):
    """Return whether two releases are plausible same-file fallback peers.

    This is intentionally looser than manifest equality. The stream proxy still
    verifies content length and sampled byte fingerprints before switching to a
    fallback source, so this stage should gather plausible peers instead of
    rejecting reposts because their NZB subject used a different filename.

    ``require_same_group`` adds the user-requested same-release-group gate: a
    backup must come from the SAME group as the primary, because a different
    group's encode is a different file that can never byte-match for a seamless
    cutover. Both groups must be parsed and equal (fail closed on an unknown
    group). The check reuses the metadata already computed here, so it adds no
    extra title-metadata parses and runs only after the cheap title prefilter.
    """
    if primary_meta is None:
        primary_meta = _fs._result_meta(primary)
    if candidate_meta is None:
        candidate_meta = _fs._result_meta(candidate)
    if (
        require_same_group
        and not _fs._same_group_resolution_gate(primary_meta, candidate_meta)
        and not _fs._cross_group_mirror_exception(
            primary, candidate, primary_meta, candidate_meta
        )
    ):
        return False
    if not _fs._shared_profile_fields_match(primary_meta, candidate_meta):
        return False
    return _fs._hdr_audio_channels_match(primary_meta, candidate_meta)


_NEAR_EXACT_SIZE_TOLERANCE_FRACTION = 0.005
_NEAR_EXACT_SIZE_TOLERANCE_FLOOR_BYTES = 4 * 1024 * 1024


def _near_exact_size_match(primary, candidate):
    """Return whether indexer sizes are near-identical (repost territory).

    Cross-poster reposts of the SAME file report sizes differing only by
    posting overhead (par2 sets, yEnc framing) — a fraction of a percent
    at multi-GB scale — while distinct encodes essentially never land
    within 0.5% of each other. Unknown sizes fail closed; contrast with
    the ±25% _prefetch_size_gate_match, which fails open.
    """
    primary_size = _fs._result_indexer_size(primary)
    candidate_size = _fs._result_indexer_size(candidate)
    if primary_size <= 0 or candidate_size <= 0:
        return False
    tolerance = max(
        _NEAR_EXACT_SIZE_TOLERANCE_FLOOR_BYTES,
        int(primary_size * _NEAR_EXACT_SIZE_TOLERANCE_FRACTION),
    )
    return abs(primary_size - candidate_size) <= tolerance


def _is_webdl_quality(meta):
    """Return whether a parsed quality identifies a WEB-DL source."""
    quality = _fs._meta_value_from_meta(meta, "quality") or ""
    return quality.lower().replace("-", "").replace(" ", "") == "webdl"


def _cross_group_mirror_exception(primary, candidate, primary_meta, candidate_meta):
    """Allow a different-group WEB-DL standby for near-exact-size reposts.

    The same-group gate exists because a different group's ENCODE is a
    different file that can never byte-match — true for BluRay/REMUX,
    where even near-equal sizes mean distinct author-produced files.
    WEB-DL breaks that assumption: multiple posters ship the SAME
    service file under their own group tags, and those cross-group
    reposts are exactly the byte-identical mirrors the live-cutover
    standby pool needs (hit live: an HDSWEB-selected episode rejected
    its UBWEB repost 2.4 KB apart in size, leaving every session with
    zero standby sources). So the exception requires: BOTH sides parse
    as WEB-DL quality, near-exact indexer size, and parsed-equal
    resolutions (each failing closed). The proxy's content-length +
    fingerprint validation remains the authoritative check before any
    cutover.
    """
    if not _fs._is_webdl_quality(primary_meta) or not _fs._is_webdl_quality(
        candidate_meta
    ):
        return False
    if not _fs._near_exact_size_match(primary, candidate):
        return False
    left_res = _fs._meta_value_from_meta(primary_meta, "resolution")
    right_res = _fs._meta_value_from_meta(candidate_meta, "resolution")
    return bool(left_res) and left_res == right_res


def _same_group_resolution_gate(primary_meta, candidate_meta):
    """Return whether group + resolution fail-closed gates pass for both sides."""
    left_group = _fs._meta_value_from_meta(primary_meta, "group")
    right_group = _fs._meta_value_from_meta(candidate_meta, "group")
    if not left_group or left_group != right_group:
        return False
    # require same RESOLUTION too (user requirement): fail CLOSED like the
    # group gate -- reject when either side's resolution is unparsed or
    # differs, so an unparsed-resolution candidate can never slip a
    # different-resolution encode past the gate. The shared resolution
    # check below only fails OPEN when one side is unknown, so this stricter
    # gate is what enforces "same resolution as parsed by PTT".
    left_res = _fs._meta_value_from_meta(primary_meta, "resolution")
    right_res = _fs._meta_value_from_meta(candidate_meta, "resolution")
    if not left_res or left_res != right_res:
        return False
    return True


def _meta_string_fields_match(primary_meta, candidate_meta, keys):
    """Return whether each metadata string field matches when both are known."""
    for key in keys:
        left = _fs._meta_value_from_meta(primary_meta, key)
        right = _fs._meta_value_from_meta(candidate_meta, key)
        if left and right and left != right:
            return False
    return True


def _meta_bool_flags_match(primary_meta, candidate_meta, keys):
    """Return whether each metadata boolean flag is identical on both sides."""
    for key in keys:
        if _fs._meta_bool_from_meta(primary_meta, key) != _fs._meta_bool_from_meta(
            candidate_meta, key
        ):
            return False
    return True


def _shared_profile_fields_match(primary_meta, candidate_meta):
    """Return whether res/codec/container, edition, flags, and quality match."""
    if not _fs._meta_string_fields_match(
        primary_meta, candidate_meta, ("resolution", "codec", "container")
    ):
        return False

    left_edition = _fs._normalize_title(
        _fs._meta_value_from_meta(primary_meta, "edition")
    )
    right_edition = _fs._normalize_title(
        _fs._meta_value_from_meta(candidate_meta, "edition")
    )
    if left_edition != right_edition:
        return False

    if not _fs._meta_bool_flags_match(
        primary_meta, candidate_meta, ("proper", "repack", "upscaled")
    ):
        return False

    left_quality = _fs._quality_family(
        _fs._meta_value_from_meta(primary_meta, "quality")
    )
    right_quality = _fs._quality_family(
        _fs._meta_value_from_meta(candidate_meta, "quality")
    )
    if left_quality and right_quality and left_quality != right_quality:
        return False
    return True


def _hdr_audio_channels_match(primary_meta, candidate_meta):
    """Return whether HDR, audio, and channel metadata are compatible."""
    left_hdr = set(_fs._meta_values_from_meta(primary_meta, "hdr"))
    right_hdr = set(_fs._meta_values_from_meta(candidate_meta, "hdr"))
    if left_hdr != right_hdr:
        return False

    left_audio = set(_fs._meta_values_from_meta(primary_meta, "audio"))
    right_audio = set(_fs._meta_values_from_meta(candidate_meta, "audio"))
    if left_audio and right_audio and not left_audio.intersection(right_audio):
        return False

    left_channels = _fs._meta_value_from_meta(primary_meta, "channels")
    right_channels = _fs._meta_value_from_meta(candidate_meta, "channels")
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
    digest = manifest.get("article_digest", "")
    if not _fs._manifest_group_key_fields_present(kind, name, digest):
        return None
    # Parse group_bytes before the kind branches so an unparseable size
    # rejects every kind (incl. archive), matching the original ordering.
    size = _fs._manifest_group_size_bytes(manifest)
    if size < 0:
        return None
    if kind == "archive":
        return kind, name
    if kind == "video" and size > 0:
        return kind, name, size
    return None


def _manifest_group_key_fields_present(kind, name, digest):
    """Return whether all required manifest group-key fields are non-empty."""
    return bool(kind and name and digest)


def _manifest_group_size_bytes(manifest):
    """Return manifest group_bytes as a positive int, or -1 when unparseable."""
    try:
        return int(manifest.get("group_bytes", 0) or 0)
    except (TypeError, ValueError):
        return -1


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
    primary_reason = _fs._manifest_unsupported_reason(primary)
    candidate_reason = _fs._manifest_unsupported_reason(candidate)
    if not primary_reason and not candidate_reason:
        return False
    if primary_reason and primary_reason not in _fs._METADATA_ONLY_MANIFEST_REASONS:
        return False
    if candidate_reason and candidate_reason not in _fs._METADATA_ONLY_MANIFEST_REASONS:
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


def _manifest_normalized_video_name(result):
    """Return the candidate's normalized video filename from its manifest, else "".

    Used to PREFER an exact-same-filename repost (a different upload of the
    byte-identical file, already de-duplicated from the primary by article
    digest) ahead of the looser tier/size ranking, per the user requirement to
    try exact-filename matches first.
    """
    manifest = result.get("_fallback_manifest") if isinstance(result, dict) else None
    if not isinstance(manifest, dict):
        return ""
    name = manifest.get("normalized_video_name", "")
    return name.strip().lower() if isinstance(name, str) else ""


def _fallback_peer_matches(primary, candidate):
    """Return whether candidate should be submitted as a standby fallback."""
    primary_link = primary.get("link", "")
    candidate_link = candidate.get("link", "")
    if not candidate_link or candidate_link == primary_link:
        return False

    primary_digest = _fs._article_digest(primary)
    candidate_digest = _fs._article_digest(candidate)
    if primary_digest and candidate_digest and candidate_digest == primary_digest:
        return False

    # Authoritative content-identity gate (F2): never fall back to a different
    # movie part / year / episode / edition even when release tokens overlap.
    if not _fs._same_content(primary, candidate):
        return False

    if not _fs._title_profile_gate_passes(primary, candidate):
        return False

    return _fs._fallback_manifest_peer_matches(primary, candidate)


def _title_profile_gate_passes(primary, candidate):
    """Return whether the title + same-group profile gate accepts a peer.

    A prior prefetch-gate match short-circuits the recheck (the group was
    already validated there).
    """
    if _fs._has_prefetch_gate_match(primary, candidate):
        return True
    if not _fs._titles_look_related(primary, candidate):
        return False
    # require_same_group: a backup must come from the SAME release group as
    # the primary (user requirement) -- a different group's encode is a
    # different file that can never byte-match for a seamless cutover.
    return _fs._metadata_profiles_match(primary, candidate, require_same_group=True)


_TIER0_SIZE_FRACTION = 0.03


_PEER_BYTES_TOLERANCE_FRACTION = 0.10


_PREFETCH_INDEXER_SIZE_TOLERANCE_FRACTION = 0.25


def _result_indexer_size(result):
    """Return the indexer-provided result size in bytes, or zero."""
    if not isinstance(result, dict):
        return 0
    value = result.get("size")
    if isinstance(value, bool):
        return 0
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return 0
    try:
        size = int(value)
    except (TypeError, ValueError):
        return 0
    return size if size > 0 else 0


def _synthetic_indexer_size_manifest(result):
    """Return a video-kind manifest synthesized from indexer size metadata."""
    size = _fs._result_indexer_size(result)
    if size < _fs._INDEXER_SIZE_SYNTHETIC_MIN_BYTES:
        return None
    link = result.get("link", "") if isinstance(result, dict) else ""
    if not isinstance(link, str) or not link:
        return None
    digest = hashlib.sha256(link.encode("utf-8")).hexdigest()
    return {
        "payload_kind": "video",
        "group_name": "",
        "group_bytes": size,
        "video_name": "",
        "normalized_video_name": "",
        "video_bytes": size,
        "archive_base_name": "",
        "article_digest": digest,
        "article_count": 0,
        "skipped_candidate_count": 0,
        "skipped_candidates": [],
        "unsupported_reason": "",
    }


def _manifest_with_indexer_size_fallback(result, manifest):
    """Replace parser-unsupported manifests with indexer-size fallback evidence."""
    if not isinstance(manifest, dict):
        return manifest
    reason = manifest.get("unsupported_reason", "") or ""
    if reason not in _fs._INDEXER_SIZE_SYNTHETIC_MANIFEST_REASONS:
        return manifest
    synthetic = _fs._synthetic_indexer_size_manifest(result)
    return synthetic or manifest


def _prefetch_size_gate_match(primary, candidate):
    """Return whether indexer sizes are close enough for prefetching NZBs."""
    primary_size = _fs._result_indexer_size(primary)
    candidate_size = _fs._result_indexer_size(candidate)
    if primary_size <= 0 or candidate_size <= 0:
        return True
    tolerance = primary_size * _fs._PREFETCH_INDEXER_SIZE_TOLERANCE_FRACTION
    return abs(primary_size - candidate_size) <= tolerance


def _fallback_manifest_peer_matches(primary, candidate):
    """Return whether manifest evidence allows an already-prefiltered peer."""
    # Archive group keys are (kind, archive_base_name) without group_bytes, so
    # two archive manifests that share an archive_base short-circuit here and
    # bypass the +/-20% size gate below. That is intentional: a shared
    # archive_base is strong evidence of the same upload set. Distinct
    # archive_base names (Theatrical vs Extended packaging) fall through to
    # the byte-tolerance gate.
    primary_key = _fs._manifest_group_key(primary)
    candidate_key = _fs._manifest_group_key(candidate)
    if primary_key is not None and primary_key == candidate_key:
        return True

    if _fs._metadata_only_manifest_fallback_allowed(primary, candidate):
        return True

    primary_kind = _fs._manifest_payload_kind(primary)
    candidate_kind = _fs._manifest_payload_kind(candidate)
    if not primary_kind or not candidate_kind:
        return False
    video_kinds = ("video", "archive")
    if primary_kind not in video_kinds or candidate_kind not in video_kinds:
        return False
    return _fs._manifest_group_bytes_within_tolerance(primary, candidate)


def _manifest_group_bytes_within_tolerance(primary, candidate):
    """Return whether two manifests' group_bytes are within the peer tolerance.

    Both kinds are plausible video payloads (direct MKV or RAR archive). The
    content-identity gate (``_same_content``) and the same-resolution/codec
    profile gate already ran upstream, so two peers reaching here are the same
    content and the same encode; their group_bytes should differ only by yEnc
    segmentation noise (+/-10%), so a large gap (different tracks/runtime) is
    rejected.
    """
    primary_bytes = _fs._manifest_group_bytes(primary)
    candidate_bytes = _fs._manifest_group_bytes(candidate)
    if primary_bytes <= 0 or candidate_bytes <= 0:
        return False
    tolerance = primary_bytes * _fs._PEER_BYTES_TOLERANCE_FRACTION
    return abs(primary_bytes - candidate_bytes) <= tolerance


def _manifest_may_match_any_peer(result):
    """Return whether this manifest can still match any fetched candidate."""
    if _fs._manifest_group_key(result) is not None:
        return True
    if _fs._manifest_unsupported_reason(result) in _fs._METADATA_ONLY_MANIFEST_REASONS:
        return True
    return bool(_fs._manifest_payload_kind(result))


def _message_id_is_healthy(message_id):
    """Return whether one Message-ID is a usable, well-formed article id."""
    if not isinstance(message_id, str):
        return False
    clean = message_id.strip()
    if not clean or "@" not in clean:
        return False
    return not any(char.isspace() or ord(char) < 0x20 for char in clean)


def _manifest_candidate_message_ids_are_healthy(candidate):
    """Return whether a manifest candidate has usable article Message-IDs."""
    message_ids = candidate.get("message_ids") if isinstance(candidate, dict) else None
    if not isinstance(message_ids, list) or not message_ids:
        return False
    return all(_fs._message_id_is_healthy(message_id) for message_id in message_ids)


def _manifest_error(reason):
    """Return an unsupported manifest for fallback grouping errors."""
    return _fs.make_empty_manifest(reason)
