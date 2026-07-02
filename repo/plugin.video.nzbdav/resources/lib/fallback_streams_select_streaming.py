# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Streaming fallback selection attach machinery.

Split out of ``fallback_streams_select`` to keep that module under the
file-size budget. These helpers are the rolling-window manifest-fetch engine
and its ordered/out-of-order attach bookkeeping. They are re-exported from
``fallback_streams_select`` (and thence ``fallback_streams``) so existing
patch targets and internal ``_fs.<name>`` call sites keep resolving unchanged.
"""

import threading
import time
from queue import Empty, Queue
from typing import NamedTuple

import resources.lib.fallback_streams as _fs


class SelectionAttachState(NamedTuple):
    """Mutable-collection bundle threaded through the streaming attach loop.

    The collections themselves are mutated in place; the tuple is only the
    handle that carries them (and the immutable cap) between the attach helpers.
    """

    candidates: list
    seen_candidate_links: set
    seen_article_digests: set
    max_candidates: int


def _already_attached(state, candidate):
    """Return the dedup gate for one candidate plus its cached link/digest.

    Yields ``(already_attached, candidate_link, candidate_digest)`` where
    ``already_attached`` is True when the candidate's link or (when present)
    article digest is already in ``state``'s seen-sets. Returning the computed
    link/digest lets the caller record them without re-deriving, preserving the
    original single ``_article_digest`` evaluation.
    """
    candidate_link = candidate.get("link", "")
    candidate_digest = _fs._article_digest(candidate)
    already_attached = candidate_link in state.seen_candidate_links or (
        candidate_digest and candidate_digest in state.seen_article_digests
    )
    return already_attached, candidate_link, candidate_digest


def _attach_manifest_candidate_if_matching(selected, candidate, state):
    """Attach a fetched candidate when manifest evidence still matches."""
    already_attached, candidate_link, candidate_digest = _fs._already_attached(
        state, candidate
    )
    if already_attached or not _fs._fallback_manifest_peer_matches(selected, candidate):
        return False
    state.candidates.append(candidate)
    state.seen_candidate_links.add(candidate_link)
    if candidate_digest:
        state.seen_article_digests.add(candidate_digest)
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
    selected, completed, ready_index, state, misses_seen, consumed_indices
):
    """Pop one ready candidate, attach it, and count a miss when it does not match."""
    ready_candidate = completed.pop(ready_index)
    consumed_indices.add(ready_index)
    attached = _fs._attach_manifest_candidate_if_matching(
        selected, ready_candidate, state
    )
    if not attached:
        misses_seen[0] += 1


def _attach_ready_selection_candidates(
    selected, completed, next_to_attach, state, misses_seen, consumed_indices
):
    """Attach completed candidate manifests strictly in result order.

    Stops at the first index that has not completed yet (a gap). Filling the cap
    from later out-of-order completions is handled separately by
    ``_fill_cap_from_completed`` once that gap has had its settle window, so an
    earlier (higher-priority) peer that is merely slow is never skipped here.
    """
    _fs._advance_past_consumed(next_to_attach, consumed_indices)
    while next_to_attach[0] in completed:
        _fs._consume_ready_candidate(
            selected, completed, next_to_attach[0], state, misses_seen, consumed_indices
        )
        next_to_attach[0] += 1
        _fs._advance_past_consumed(next_to_attach, consumed_indices)
        if len(state.candidates) >= state.max_candidates:
            return True
    return False


def _fill_cap_from_completed(selected, completed, state, misses_seen, consumed_indices):
    """Fill the remaining cap slots from out-of-order completions, lowest index
    first. Called only once the earlier in-flight gap has settled (completed or
    exceeded its settle window), so this no longer skips an earlier peer that is
    about to arrive. Returns True when the cap is reached."""
    remaining_slots = state.max_candidates - len(state.candidates)
    if len(completed) < remaining_slots or remaining_slots <= 0:
        return False
    for ready_index in sorted(completed):
        _fs._consume_ready_candidate(
            selected, completed, ready_index, state, misses_seen, consumed_indices
        )
        if len(state.candidates) >= state.max_candidates:
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


def _classify_stream_wait_outcome(
    settle_pending, optional_tail_wait_remaining, start_stall_speculation
):
    """Classify the loop action when the manifest queue wait times out (``Empty``).

    Mirrors the original ``_receive_next`` except-arm order exactly: a pending
    settle window drains first, then an expired optional-tail wait, otherwise a
    stall-speculation fetch is kicked off and the loop continues. ``settle_pending``
    is passed pre-evaluated so ``optional_tail_wait_remaining`` keeps its original
    short-circuit -- it is only invoked when no settle window is pending, which
    preserves the timing of its deadline side effect.
    """
    if settle_pending:
        return "settle_expired", None
    if optional_tail_wait_remaining() is not None:
        return "return_true", None
    start_stall_speculation()
    return "continue", None


def _attach_selection_candidates_streaming(
    selected, candidate_iter, state, include_selected_manifest
):
    """Fetch selected fallback manifests with a rolling ordered window."""
    candidates = state.candidates
    seen_article_digests = state.seen_article_digests
    max_candidates = state.max_candidates
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
    settle_pending = [False]
    settle_deadline = [None]
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
        # (kind, index, target) tuple), "return_true", "settle_expired", or
        # "continue".
        try:
            if settle_pending[0]:
                # A cap-fill is held pending an earlier in-flight peer; wait at
                # most the remaining settle window for it before filling from
                # later out-of-order completions.
                settle_remaining = settle_deadline[0] - time.monotonic()
                if settle_remaining <= 0:
                    return "settle_expired", None
                return "got", result_queue.get(timeout=settle_remaining)
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
            return _fs._classify_stream_wait_outcome(
                settle_pending[0],
                _optional_tail_wait_remaining,
                _start_stall_speculation,
            )

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
            selected, completed, next_to_attach, state, misses_seen, consumed_indices
        )

    def _maybe_fill_cap(force):
        # Fill remaining cap slots from out-of-order completions, but only once
        # the earlier in-flight gap has settled. Same selected-ready gate as the
        # in-order attach. ``force`` is set when the settle window has elapsed.
        if not (selected_ready[0] and selected_can_match[0]):
            settle_pending[0] = False
            settle_deadline[0] = None
            return False
        remaining_slots = max_candidates - len(candidates)
        if len(completed) < remaining_slots or remaining_slots <= 0:
            settle_pending[0] = False
            settle_deadline[0] = None
            return False
        gap_in_flight = next_to_attach[0] < next_candidate_index[0]
        if gap_in_flight and not force:
            if not settle_pending[0]:
                settle_pending[0] = True
                settle_deadline[0] = (
                    time.monotonic() + _fs._FALLBACK_MANIFEST_SETTLE_WINDOW_SECONDS
                )
            return False
        settle_pending[0] = False
        settle_deadline[0] = None
        return _fs._fill_cap_from_completed(
            selected, completed, state, misses_seen, consumed_indices
        )

    def _apply_recorded_result(kind, index, target):
        # Record one streamed manifest result and return the loop verdict:
        # ``False``/``True`` end the stream, ``None`` keeps draining. Preserves
        # the original in-loop order: record, post-record gate, then cap fill.
        _record_result(kind, index, target)
        post_record = _fs._post_record_action(
            selected_ready[0], selected_can_match[0], _attach_ready
        )
        if post_record == "return_false":
            return False
        if post_record == "return_true":
            return True
        if _maybe_fill_cap(force=False):
            return True
        _fill_candidate_window()
        return None

    def _drain_result_stream():
        while active[0]:
            action, message = _receive_next()
            if action == "return_true":
                return True
            if action == "settle_expired":
                if _maybe_fill_cap(force=True):
                    return True
                continue
            if action == "continue":
                continue
            kind, index, target = message
            outcome = _apply_recorded_result(kind, index, target)
            if outcome is not None:
                return outcome
        return selected_can_match[0]

    _fill_candidate_window()

    return _drain_result_stream()
