# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Deferred fallback-candidate computation split out of ``router``.

``_fallback_candidate_loader_for_selection`` stays in ``router`` (the suite
imports it from there); it calls into this module's lazy distinct-peer scan.
The fallback_streams symbols the suite patches via ``resources.lib.router``
(``attach_fallback_candidates_for_selection``, ``fallback_candidate_prefetch_settings``,
``cached_selection_pool_first_peer``, ``selection_pool_may_have_fallback_peer``,
``fallback_candidate_prefetch_enabled``, ``FALLBACK_CANDIDATES_DISABLED``) are
reached at call time through ``import resources.lib.router as _router`` so those
``@patch`` decorators keep resolving.
"""

from itertools import chain

import xbmc


def _pool_has_no_fallback_peer(selected, results, result_count, known_first_peer):
    """Return whether a multi-result pool provably cannot host a fallback peer."""
    import resources.lib.router as _router

    return (
        known_first_peer is None
        and result_count is not None
        and result_count != 1
        and not _router.selection_pool_may_have_fallback_peer(selected, results)
    )


def _compute_fallback_candidates(selected, results, result_count, settings_getter):
    """Run the deferred fallback distinct-peer scan + attach for one selection.

    Extracted verbatim from the former ``_load_fallback_candidates`` closure;
    every call is preserved in order so the lazy distinct-peer-scan contract is
    unchanged.
    """
    import resources.lib.router as _router

    known_first_peer = _router.cached_selection_pool_first_peer(selected, results)
    if _pool_has_no_fallback_peer(selected, results, result_count, known_first_peer):
        return _router.FALLBACK_CANDIDATES_DISABLED
    if known_first_peer is None:
        known_first_peer = _router.cached_selection_pool_first_peer(selected, results)
    fallback_settings = _resolve_fallback_prefetch_settings(settings_getter)
    if not _router.fallback_candidate_prefetch_enabled(fallback_settings):
        return _router.FALLBACK_CANDIDATES_DISABLED

    augmented, known_first_peer, disabled = _augmented_pool_and_first_peer(
        selected, results, result_count, settings_getter, known_first_peer
    )
    if disabled:
        return _router.FALLBACK_CANDIDATES_DISABLED
    _router.attach_fallback_candidates_for_selection(
        selected,
        _selection_pool_with_peer_first(selected, augmented, known_first_peer),
        fallback_settings=fallback_settings,
    )
    return list(selected.get("_fallback_candidates", []) or [])


def _augmented_pool_and_first_peer(
    selected, results, result_count, settings_getter, known_first_peer
):
    """Build the Hydra-augmented pool and resolve the plausible first peer.

    Returns ``(augmented, known_first_peer, disabled)``; ``disabled`` is True
    when the augmented pool provably cannot host a fallback peer. Extracted
    verbatim — every call is preserved in order.
    """
    extra_uploads = _fetch_fallback_extra_uploads(selected, settings_getter)
    augmented = chain(results or [], extra_uploads or [])
    if known_first_peer is None:
        known_first_peer, disabled = _resolve_known_first_peer(
            selected, results, result_count, extra_uploads, augmented
        )
        return augmented, known_first_peer, disabled
    return augmented, known_first_peer, False


def _resolve_fallback_prefetch_settings(settings_getter):
    """Return the fallback prefetch settings for the active getter (or default)."""
    import resources.lib.router as _router

    if settings_getter is None:
        return _router.fallback_candidate_prefetch_settings()
    return _router.fallback_candidate_prefetch_settings(settings_getter=settings_getter)


def _fetch_fallback_extra_uploads(selected, settings_getter):
    """Fetch same-title alternate uploads from Hydra's duplicate API (fail-soft).

    The picker UX still shows one row per release for clean UI, but the
    fallback worker needs real same-release/different-upload peers — those are
    exactly what nzbdav-rs needs to swap to without interrupting playback when
    the primary stream's articles fail. Returns ``[]`` when the lookup is
    disabled or raises.
    """
    import resources.lib.router as _router

    if not _router._hydra_duplicate_lookup_enabled(
        selected, settings_getter=settings_getter
    ):
        return []
    from resources.lib.hydra import fetch_release_duplicate_uploads

    try:
        return fetch_release_duplicate_uploads(
            selected, settings_getter=settings_getter
        )
    except Exception as error:  # pylint: disable=broad-except
        from resources.lib.http_util import redact_text

        xbmc.log(
            "NZB-DAV: duplicate-uploads lookup raised: {}".format(
                redact_text(str(error))
            ),
            xbmc.LOGDEBUG,
        )
        return []


def _has_extra_uploads(extra_uploads):
    """Return whether ``extra_uploads`` is a non-empty sized collection."""
    try:
        len(extra_uploads)
    except TypeError:
        return False
    return bool(extra_uploads)


def _resolve_known_first_peer(
    selected, results, result_count, extra_uploads, augmented
):
    """Resolve the plausible first peer, returning ``(peer, disabled)``.

    ``disabled`` is True when the augmented pool cannot host a fallback peer
    and the loader should return ``FALLBACK_CANDIDATES_DISABLED``.
    """
    import resources.lib.router as _router

    if not _has_extra_uploads(extra_uploads):
        return _resolve_first_peer_no_extras(selected, results, result_count)
    if (
        isinstance(results, (list, tuple))
        and len(results) == 1
        and results[0] is selected
    ):
        return (extra_uploads[0] if extra_uploads else None), False
    if not _router.selection_pool_may_have_fallback_peer(selected, augmented):
        return None, True
    return _router.cached_selection_pool_first_peer(selected, augmented), False


def _resolve_first_peer_no_extras(selected, results, result_count):
    """Resolve ``(peer, disabled)`` when no extra Hydra uploads are available."""
    import resources.lib.router as _router

    try:
        len(results)
    except TypeError:
        return None, False
    if result_count == 1 or not _router.selection_pool_may_have_fallback_peer(
        selected, results
    ):
        return None, True
    return _router.cached_selection_pool_first_peer(selected, results), False


def _selection_pool_with_peer_first(selected, results, first_peer):
    """Return a selection pool that tries the known plausible peer first."""
    if isinstance(selected, dict):
        yield selected
    if isinstance(first_peer, dict) and first_peer is not selected:
        yield first_peer
    for result in results or []:
        if result is selected or result is first_peer:
            continue
        yield result
