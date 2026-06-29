# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Streaming fallback selection/attach engine and public entry points."""

import threading
import time
from queue import Empty, Queue

import resources.lib.fallback_streams as _fs
from resources.lib import telemetry


def _fallback_settings(settings_getter=None):
    """Return (enabled, max_candidates) from Kodi settings."""
    if settings_getter is None:
        addon = _fs.xbmcaddon.Addon("plugin.video.nzbdav")
    else:
        addon = _fs.SimpleNamespace(getSetting=lambda key: settings_getter(key, ""))
    enabled = _fs._setting_bool(addon, "fallback_streams_enabled", True)
    max_candidates = _fs._setting_int(addon, "fallback_streams_max", 5)
    if max_candidates < 0 or max_candidates > _fs._MAX_FALLBACKS:
        _fs.xbmc.log(
            "NZB-DAV: fallback_streams_max={} clamped to 0..{}".format(
                max_candidates, _fs._MAX_FALLBACKS
            ),
            _fs.xbmc.LOGWARNING,
        )
    return enabled, max(0, min(max_candidates, _fs._MAX_FALLBACKS))


def fallback_candidate_prefetch_settings(settings_getter=None):
    """Return fallback discovery settings for picker prefetch callers."""
    if settings_getter is None:
        return _fs._fallback_settings()
    return _fs._fallback_settings(settings_getter=settings_getter)


def fallback_candidate_prefetch_enabled(settings=None):
    """Return whether fallback discovery should scan picker peers."""
    if settings is None:
        settings = _fs.fallback_candidate_prefetch_settings()
    enabled, max_candidates = settings
    return enabled and max_candidates > 0


def selection_pool_may_have_fallback_peer(selected, results):
    """Return whether a selection pool can contain a distinct fallback peer."""
    return not _fs._sized_pool_has_no_distinct_peer(selected, results)


def selected_manifest_may_have_fallback_peer(selected):
    """Return whether a selected result's manifest still allows fallback peers."""
    if not isinstance(selected, dict):
        return False
    selected_manifest = selected.get("_fallback_manifest")
    if isinstance(selected_manifest, dict):
        selected_manifest = _fs._manifest_with_indexer_size_fallback(
            selected, selected_manifest
        )
        selected["_fallback_manifest"] = selected_manifest
        selected["_fallback_manifest_error"] = selected_manifest.get(
            "unsupported_reason", ""
        )
    return not (
        isinstance(selected_manifest, dict)
        and not _fs._manifest_may_match_any_peer(selected)
    )


def _ensure_fallback_manifests(results):
    """Fetch missing NZB manifests for fallback grouping."""
    started = time.monotonic()
    manifest_cache = {}
    input_count = 0
    for result in results:
        input_count += 1
        _fs._ensure_fallback_manifest(result, manifest_cache)
    telemetry.log_timing(
        "fallback_manifests",
        (time.monotonic() - started) * 1000.0,
        input=input_count,
        fetched=len(manifest_cache),
    )
    return manifest_cache


def _ensure_fallback_manifest(result, manifest_cache):
    """Fetch one missing NZB manifest using the attach-call cache."""
    manifest = result.get("_fallback_manifest")
    if isinstance(manifest, dict):
        manifest = _fs._manifest_with_indexer_size_fallback(result, manifest)
        result["_fallback_manifest"] = manifest
        result["_fallback_manifest_error"] = manifest.get("unsupported_reason", "")
        return manifest
    link = result.get("link", "")
    if not isinstance(link, str) or not link.strip():
        result["_fallback_manifest_error"] = "missing_link"
        return None
    if link not in manifest_cache:
        manifest_cache[link] = _fs._fetch_fallback_manifest(link)
    manifest = manifest_cache[link]
    if not isinstance(manifest, dict):
        manifest = _fs._manifest_error("fetch_error")
        manifest_cache[link] = manifest
    manifest = _fs._manifest_with_indexer_size_fallback(result, manifest)
    manifest_cache[link] = manifest
    result["_fallback_manifest"] = manifest
    result["_fallback_manifest_error"] = manifest.get("unsupported_reason", "")
    return manifest


def _attach_manifest_candidate_if_matching(
    selected, candidate, candidates, seen_candidate_links, seen_article_digests
):
    """Attach a fetched candidate when manifest evidence still matches."""
    candidate_link = candidate.get("link", "")
    candidate_digest = _fs._article_digest(candidate)
    if (
        candidate_link in seen_candidate_links
        or (candidate_digest and candidate_digest in seen_article_digests)
        or not _fs._fallback_manifest_peer_matches(selected, candidate)
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
        _fs._ensure_fallback_manifest(target, {})
    except Exception:  # pylint: disable=broad-except
        target["_fallback_manifest"] = _fs._manifest_error("fetch_error")
        target["_fallback_manifest_error"] = "fetch_error"
    finally:
        result_queue.put((kind, index, target))


def _start_selection_manifest_fetch(kind, index, target, result_queue):
    """Start one daemon manifest fetch, falling back to inline execution."""
    thread = threading.Thread(
        target=_fs._fetch_selection_manifest_for_queue,
        args=(kind, index, target, result_queue),
        name="nzbdav-fallback-manifest",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        _fs._fetch_selection_manifest_for_queue(kind, index, target, result_queue)


def _advance_past_consumed(next_to_attach, consumed_indices):
    """Advance the ordered cursor past every already-consumed index."""
    while next_to_attach[0] in consumed_indices:
        next_to_attach[0] += 1


def _consume_ready_candidate(
    selected,
    completed,
    ready_index,
    candidates,
    seen_candidate_links,
    seen_article_digests,
    misses_seen,
    consumed_indices,
):
    """Pop one ready candidate, attach it, and count a miss when it does not match."""
    ready_candidate = completed.pop(ready_index)
    consumed_indices.add(ready_index)
    attached = _fs._attach_manifest_candidate_if_matching(
        selected,
        ready_candidate,
        candidates,
        seen_candidate_links,
        seen_article_digests,
    )
    if not attached:
        misses_seen[0] += 1


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
    _fs._advance_past_consumed(next_to_attach, consumed_indices)
    while next_to_attach[0] in completed:
        _fs._consume_ready_candidate(
            selected,
            completed,
            next_to_attach[0],
            candidates,
            seen_candidate_links,
            seen_article_digests,
            misses_seen,
            consumed_indices,
        )
        next_to_attach[0] += 1
        _fs._advance_past_consumed(next_to_attach, consumed_indices)
        if len(candidates) >= max_candidates:
            return True
    remaining_slots = max_candidates - len(candidates)
    if len(completed) >= remaining_slots > 0:
        for ready_index in sorted(completed):
            _fs._consume_ready_candidate(
                selected,
                completed,
                ready_index,
                candidates,
                seen_candidate_links,
                seen_article_digests,
                misses_seen,
                consumed_indices,
            )
            if len(candidates) >= max_candidates:
                return True
    return False


def _prime_first_candidate(candidate_iter, pending_to_start, candidate_exhausted):
    """Queue the first candidate for fetch; report an empty-iterator early exit.

    Preserves the original priming order: the first ``next(candidate_iter)`` is
    buffered into ``pending_to_start`` so the window fill dispatches it before
    pulling further candidates, matching the streaming dispatch timing exactly.
    """
    try:
        pending_to_start.append(next(candidate_iter))
    except StopIteration:
        candidate_exhausted[0] = True
    return not pending_to_start and candidate_exhausted[0]


def _post_record_action(selected_ready, selected_can_match, attach_ready):
    """Return the loop action after recording one streamed manifest result.

    ``attach_ready`` is the zero-arg attach call run only when the selected
    manifest is ready and still able to match a peer; preserving that gate keeps
    strict candidate validation before any source switch.
    """
    if selected_ready and not selected_can_match:
        return "return_false"
    if selected_ready and selected_can_match and attach_ready():
        return "return_true"
    return "continue"


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
    max_workers = min(max_candidates, _fs._MAX_FALLBACKS)

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
        _fs._start_selection_manifest_fetch("candidate", index, candidate, result_queue)
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

    def _start_stall_speculation():
        active_before = active_candidates[0]
        while _can_start_stall_speculation() and _start_candidate_fetch():
            if active_candidates[0] == active_before:
                break
            active_before = active_candidates[0]

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
                now + _fs._FALLBACK_MANIFEST_OPTIONAL_TAIL_WAIT_SECONDS
            )
        return max(0, optional_tail_deadline[0] - now)

    if _fs._prime_first_candidate(
        candidate_iter, pending_to_start, candidate_exhausted
    ):
        return True

    if include_selected_manifest:
        active[0] += 1
        _fs._start_selection_manifest_fetch("selected", -1, selected, result_queue)

    def _receive_next():
        # Returns (action, message) where action is "got" (message is the
        # (kind, index, target) tuple), "return_true", or "continue".
        try:
            tail_wait = _optional_tail_wait_remaining()
            if tail_wait is not None:
                if tail_wait <= 0:
                    return "return_true", None
                return "got", result_queue.get(timeout=tail_wait)
            if _can_start_stall_speculation():
                return "got", result_queue.get(
                    timeout=_fs._FALLBACK_MANIFEST_STALL_SPECULATION_SECONDS
                )
            return "got", result_queue.get()
        except Empty:
            if _optional_tail_wait_remaining() is not None:
                return "return_true", None
            _start_stall_speculation()
            return "continue", None

    def _record_result(kind, index, target):
        active[0] -= 1
        if kind == "candidate":
            active_candidates[0] -= 1
            completed[index] = target
            return
        selected_ready[0] = True
        selected_digest = _fs._article_digest(selected)
        if selected_digest:
            seen_article_digests.add(selected_digest)
        selected_can_match[0] = _fs._manifest_may_match_any_peer(selected)

    def _attach_ready():
        return _fs._attach_ready_selection_candidates(
            selected,
            completed,
            next_to_attach,
            candidates,
            seen_candidate_links,
            seen_article_digests,
            max_candidates,
            misses_seen,
            consumed_indices,
        )

    _fill_candidate_window()

    while active[0]:
        action, message = _receive_next()
        if action == "return_true":
            return True
        if action == "continue":
            continue
        kind, index, target = message
        _record_result(kind, index, target)

        post_record = _fs._post_record_action(
            selected_ready[0], selected_can_match[0], _attach_ready
        )
        if post_record == "return_false":
            return False
        if post_record == "return_true":
            return True

        _fill_candidate_window()

    return selected_can_match[0]


def attach_fallback_candidates(results):
    """Attach duplicate fallback candidates to each result in-place.

    Every result receives ``_fallback_candidates``. When fallback streams are
    disabled, the cap is zero, or a result cannot be conservatively matched,
    the attached list is empty.
    """
    for result in results:
        result["_fallback_candidates"] = []

    if not _fs._pool_has_distinct_nzb_links(results):
        return results

    enabled, max_candidates = _fs._fallback_settings()
    if not enabled or max_candidates <= 0:
        return results

    prefetchable_results = _fs._prefetchable_results(results)
    if not prefetchable_results:
        return results

    _fs._ensure_fallback_manifests(prefetchable_results)
    for result in prefetchable_results:
        _fs._attach_candidates_for_target(result, prefetchable_results, max_candidates)

    return results


def _prefetchable_results(results):
    """Return the results that have at least one prefetchable fallback peer."""
    return [
        result
        for result in results
        if _fs.first_prefetchable_fallback_peer(
            result, results, distinct_peer_already_checked=True
        )
    ]


def _selection_prefetch_candidate_matches(
    selected, candidate, seen_prefetch_links, tokens_ref, meta_ref
):
    """Return whether a selection-pool candidate is worth fetching a manifest for.

    ``tokens_ref`` / ``meta_ref`` are single-element lists used to lazily cache
    the selected result's title tokens and metadata across candidates, exactly
    as the original inline generator did.
    """
    if _fs._has_prefetch_gate_match(selected, candidate):
        prefetch_match = True
    else:
        prefetch_match = _fs._selection_prefetch_uncached_match(
            selected, candidate, seen_prefetch_links, tokens_ref, meta_ref
        )
    if meta_ref[0] is None:
        cached_selected_meta = selected.get("_meta")
        if isinstance(cached_selected_meta, dict):
            meta_ref[0] = cached_selected_meta
    return prefetch_match


def _selection_prefetch_uncached_match(
    selected, candidate, seen_prefetch_links, tokens_ref, meta_ref
):
    """Run the non-cached prefetch gate, caching selected tokens lazily."""
    candidate_meta = candidate.get("_meta")
    if not isinstance(candidate_meta, dict):
        candidate_meta = None
    prefetch_tokens = tokens_ref[0]
    if prefetch_tokens is None and (meta_ref[0] is None or candidate_meta is None):
        prefetch_tokens = _fs._title_tokens(selected)
        tokens_ref[0] = prefetch_tokens
    prefetch_match = _fs._prefetch_candidate_matches(
        selected,
        candidate,
        seen_prefetch_links,
        prefetch_tokens,
        meta_ref[0],
    )
    if tokens_ref[0] is None:
        tokens_ref[0] = _fs._cached_title_tokens(selected)
    return prefetch_match


def _iter_selection_prefetch_candidates(
    selected, results, seen_prefetch_links, selected_meta
):
    """Yield selection-pool candidates worth fetching a manifest for.

    Prefiltering stays lazy so all-matching pools still stop after the cap
    instead of fetching manifests for the rest of the result list.
    """
    selected_title_tokens_ref = [None]
    selected_meta_ref = [selected_meta]
    for candidate in results or []:
        if not _fs._selection_prefetch_candidate_eligible(
            selected, candidate, seen_prefetch_links
        ):
            continue
        if _fs._selection_prefetch_candidate_matches(
            selected,
            candidate,
            seen_prefetch_links,
            selected_title_tokens_ref,
            selected_meta_ref,
        ):
            seen_prefetch_links.add(candidate.get("link", ""))
            yield candidate


def _selection_prefetch_candidate_eligible(selected, candidate, seen_prefetch_links):
    """Return whether a candidate clears the cheap distinctness + size gates."""
    if candidate is selected or not isinstance(candidate, dict):
        return False
    candidate_link = candidate.get("link", "")
    if not candidate_link or candidate_link in seen_prefetch_links:
        return False
    return _fs._prefetch_size_gate_match(selected, candidate)


def _selection_seed_article_digests(selected, selected_manifest_ready):
    """Return the initial seen-article-digest set for a selection scan."""
    seen_article_digests = set()
    if selected_manifest_ready:
        selected_digest = _fs._article_digest(selected)
        if selected_digest:
            seen_article_digests.add(selected_digest)
    return seen_article_digests


def _selection_pool_admits_fallback(selected, results):
    """Return whether a selection + pool can still yield a fallback peer."""
    if not selected:
        return False
    if not _fs.selected_manifest_may_have_fallback_peer(selected):
        return False
    if results is None or _fs._sized_pool_has_no_distinct_peer(selected, results):
        return False
    return True


def _resolve_fallback_settings(fallback_settings):
    """Return (enabled, max_candidates), loading from Kodi when not supplied."""
    if fallback_settings is None:
        return _fs._fallback_settings()
    return fallback_settings


def attach_fallback_candidates_for_selection(selected, results, fallback_settings=None):
    """Attach fallback candidates only for the result the user selected."""
    if selected:
        selected["_fallback_candidates"] = []

    if not _fs._selection_pool_admits_fallback(selected, results):
        return selected

    enabled, max_candidates = _fs._resolve_fallback_settings(fallback_settings)
    if not enabled or max_candidates <= 0:
        return selected

    seen_prefetch_links = {selected.get("link", "")}
    selected_meta = selected.get("_meta")
    if not isinstance(selected_meta, dict):
        selected_meta = None
    candidates = []
    seen_candidate_links = {selected.get("link", "")}
    selected_manifest_ready = isinstance(selected.get("_fallback_manifest"), dict)
    seen_article_digests = _fs._selection_seed_article_digests(
        selected, selected_manifest_ready
    )

    started = time.monotonic()
    selected_manifest_fetch = not selected_manifest_ready
    _fs._attach_selection_candidates_streaming(
        selected,
        _fs._iter_selection_prefetch_candidates(
            selected, results, seen_prefetch_links, selected_meta
        ),
        candidates,
        seen_candidate_links,
        seen_article_digests,
        include_selected_manifest=selected_manifest_fetch,
        max_candidates=max_candidates,
    )
    telemetry.log_timing(
        "fallback_selection_manifests",
        (time.monotonic() - started) * 1000.0,
        attached=len(candidates),
        pool=_fs._safe_len(results or []),
        selected_manifest_fetch=selected_manifest_fetch,
    )
    selected["_fallback_candidates"] = _fs._rank_fallback_candidates(
        selected, candidates
    )
    return selected
