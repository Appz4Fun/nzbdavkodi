# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Candidate ranking, pubdate dedup, prefetch-match, and job-payload builders."""

import hashlib

import resources.lib.fallback_streams as _fs


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


def _safe_len(value):
    try:
        return len(value)
    except TypeError:
        return "unknown"


def _candidate_pubdate_epoch(candidate):
    """Return a candidate's Usenet post date as UTC epoch seconds, or None.

    None (missing or unparseable pubdate) means "always distinct": such a
    candidate is never collapsed against another and is never suppressed by the
    primary's date — we cannot prove two undated posts are the same upload.
    """
    if not isinstance(candidate, dict):
        return None
    pubdate = candidate.get("pubdate", "")
    if not isinstance(pubdate, str) or not pubdate.strip():
        return None
    return _fs.pubdate_to_epoch(pubdate)


def _best_ranked_in_cluster(cluster):
    """Return the best fallback tuple from a same-post-date cluster.

    ``cluster`` is a list of ``(order_index, item)`` where ``item`` is a
    ``(exact_name, tier, size_delta, candidate)`` ranking tuple. Best = lowest
    ``(exact_name, tier, size_delta)`` (highest similarity tier), with original
    arrival order as a deterministic final tie-break.
    """
    return min(
        cluster,
        key=lambda entry: (entry[1][0], entry[1][1], entry[1][2], entry[0]),
    )[1]


def _partition_ranked_by_pubdate(target, ranked):
    """Split ranked tuples into (undated, dated) dropping same-as-primary posts.

    ``dated`` entries are ``(epoch, order_index, item)``; candidates within the
    same-article window of ``target``'s own post date are dropped entirely.
    """
    primary_epoch = _fs._candidate_pubdate_epoch(target)
    undated = []
    dated = []
    for order_index, item in enumerate(ranked):
        epoch = _fs._candidate_pubdate_epoch(item[3])
        if epoch is None:
            undated.append(item)
            continue
        if (
            primary_epoch is not None
            and abs(epoch - primary_epoch) <= _fs._SAME_POST_WINDOW_SECONDS
        ):
            continue  # same upload as the primary -> not a real backup
        dated.append((epoch, order_index, item))
    return undated, dated


def _dedupe_candidates_by_pubdate(target, ranked):
    """Collapse ranked fallback tuples posted within the same-article window.

    ``ranked`` is a list of ``(exact_name, tier, size_delta, candidate)`` tuples.
    Candidates whose post dates fall within ``_SAME_POST_WINDOW_SECONDS`` of each
    other are the same upload re-listed; only the best-ranked member of each such
    cluster is kept. Candidates within the window of ``target``'s own post date
    are dropped (the primary cannot be its own backup). Candidates with no
    parseable post date are always kept.

    Clustering is anchor-based: a candidate joins a cluster only when it is within
    the window of that cluster's EARLIEST member, so a chain of near-posts does
    not transitively merge into one blob. The returned list is unordered with
    respect to rank; callers re-sort before clamping.
    """
    undated, dated = _fs._partition_ranked_by_pubdate(target, ranked)
    dated.sort(key=lambda entry: (entry[0], entry[1]))
    survivors = []
    cluster = []
    anchor_epoch = None
    for epoch, order_index, item in dated:
        if anchor_epoch is None or epoch - anchor_epoch > _fs._SAME_POST_WINDOW_SECONDS:
            if cluster:
                survivors.append(_fs._best_ranked_in_cluster(cluster))
            cluster = [(order_index, item)]
            anchor_epoch = epoch
        else:
            cluster.append((order_index, item))
    if cluster:
        survivors.append(_fs._best_ranked_in_cluster(cluster))

    return undated + survivors


def _candidate_is_fresh_peer(
    target,
    candidate,
    candidate_link,
    candidate_digest,
    seen_links,
    seen_article_digests,
):
    """Return whether a candidate is a new, distinct, matching fallback peer."""
    if not candidate_link or candidate_link in seen_links:
        return False
    if candidate_digest and candidate_digest in seen_article_digests:
        return False
    return _fs._fallback_peer_matches(target, candidate)


def _ranking_tuple_for_candidate(target, candidate, target_size, target_name):
    """Return the (exact_name, tier, size_delta, candidate) tuple, or None."""
    tier = _fs._release_similarity(target, candidate)
    if tier is None:
        return None
    candidate_size = _fs._release_size_bytes(candidate)
    size_delta = abs(target_size - candidate_size) if target_size else 0
    candidate_name = _fs._manifest_normalized_video_name(candidate)
    exact_name = 0 if target_name and candidate_name == target_name else 1
    return (exact_name, tier, size_delta, candidate)


def _attach_candidates_for_target(target, pool, max_candidates):
    matched = []
    seen_links = {target.get("link", "")}
    target_digest = _fs._article_digest(target)
    seen_article_digests = {target_digest} if target_digest else set()
    target_size = _fs._release_size_bytes(target)
    target_name = _fs._manifest_normalized_video_name(target)
    for candidate in pool:
        if candidate is target:
            continue
        candidate_link = candidate.get("link", "")
        candidate_digest = _fs._article_digest(candidate)
        if not _fs._candidate_is_fresh_peer(
            target,
            candidate,
            candidate_link,
            candidate_digest,
            seen_links,
            seen_article_digests,
        ):
            continue
        ranking_tuple = _fs._ranking_tuple_for_candidate(
            target, candidate, target_size, target_name
        )
        if ranking_tuple is None:
            continue
        matched.append(ranking_tuple)
        seen_links.add(candidate_link)
        if candidate_digest:
            seen_article_digests.add(candidate_digest)
    # Collapse same-post-date duplicates (same upload re-listed) before ranking
    # so the _MAX_FALLBACKS clamp keeps the best DISTINCT posts, not dupes.
    matched = _fs._dedupe_candidates_by_pubdate(target, matched)
    # Exact-same-filename first (0 before 1), then tiered ranking: most-similar
    # first (lower tier), then smallest size delta. Sort is stable so equal keys
    # keep pool order.
    matched.sort(key=lambda item: (item[0], item[1], item[2]))
    target["_fallback_candidates"] = [item[3] for item in matched[:max_candidates]]


def _prefetch_candidate_matches(
    target, candidate, seen_links, target_tokens=None, target_meta=None
):
    """Return whether a candidate is worth fetching manifest evidence for."""
    if not _fs._prefetch_candidate_passes_preconditions(target, candidate, seen_links):
        return False
    candidate_meta = candidate.get("_meta")
    if not isinstance(candidate_meta, dict):
        candidate_meta = None

    title_first = _fs._prefetch_title_first(target_tokens, target_meta, candidate_meta)
    if title_first:
        # Title prefilter precedes the profile parse when no cheap profile gate
        # is available (one side's metadata is not yet computed).
        if not _fs._prefetch_titles_match(target, candidate, target_tokens):
            return False
    if not _fs._prefetch_same_group_profile_match(
        target, candidate, target_meta, candidate_meta
    ):
        return False
    if not title_first and not _fs._prefetch_titles_match(
        target, candidate, target_tokens
    ):
        return False
    # Authoritative content-identity gate after the cheap profile/title checks.
    return _fs._same_content(target, candidate)


def _prefetch_title_first(target_tokens, target_meta, candidate_meta):
    """Return whether the title prefilter should run before the profile parse."""
    if target_tokens is None:
        return False
    return candidate_meta is None or target_meta is None


def _prefetch_candidate_passes_preconditions(target, candidate, seen_links):
    """Return whether a candidate clears the cheap distinctness + size gates."""
    if candidate is target:
        return False
    candidate_link = candidate.get("link", "")
    if not candidate_link or candidate_link in seen_links:
        return False
    return _fs._prefetch_size_gate_match(target, candidate)


def _prefetch_same_group_profile_match(target, candidate, target_meta, candidate_meta):
    """Return whether the same-group profile gate accepts a prefetch candidate."""
    return _fs._metadata_profiles_match(
        target,
        candidate,
        primary_meta=target_meta,
        candidate_meta=candidate_meta,
        require_same_group=True,
    )


def _prefetch_titles_match(target, candidate, target_tokens):
    """Return whether target/candidate titles look related for prefetch."""
    if target_tokens is None:
        return _fs._titles_look_related(target, candidate)
    return _fs._title_token_sets_look_related(
        target_tokens, _fs._title_tokens(candidate)
    )


def _rank_fallback_candidates(target, candidates):
    """Return candidates ordered best-first by fallback tier, then size delta.

    An exact-same-filename repost (a different upload of the byte-identical
    file) is preferred first; then tiered ranking (lower tier = tried first) so
    the most-similar release is submitted before a looser same-content peer.
    Sort is stable, preserving original arrival order within a bucket.
    """
    target_size = _fs._release_size_bytes(target)
    target_name = _fs._manifest_normalized_video_name(target)
    ranked = []
    for candidate in candidates:
        tier = _fs._release_similarity(target, candidate)
        if tier is None:
            # Content gate already ran upstream; keep as last-resort if it
            # somehow lacks a tier (defensive — should not happen).
            tier = 3
        candidate_size = _fs._release_size_bytes(candidate)
        size_delta = abs(target_size - candidate_size) if target_size else 0
        candidate_name = _fs._manifest_normalized_video_name(candidate)
        exact_name = 0 if target_name and candidate_name == target_name else 1
        ranked.append((exact_name, tier, size_delta, candidate))
    # Collapse same-post-date duplicates (same upload re-listed) before ordering.
    ranked = _fs._dedupe_candidates_by_pubdate(target, ranked)
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in ranked]


def build_fallback_job_name(title, nzb_url, index):
    """Return a stable, traceable nzbdav job name for a fallback candidate."""
    clean_title = title if isinstance(title, str) else ""
    clean_title = _fs._INVALID_TITLE_RE.sub(" ", clean_title)
    clean_title = " ".join(clean_title.split())[:180].strip()
    if not clean_title:
        clean_title = "fallback"

    digest = hashlib.sha256(str(nzb_url).encode("utf-8")).hexdigest()[:8]
    job_name = "{} [fallback-{}-{}]".format(clean_title, index, digest)
    if not _fs._SAFE_JOB_RE.match(job_name):
        job_name = _fs._INVALID_TITLE_RE.sub(" ", job_name)
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
