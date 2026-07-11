# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""resolve()/resolve_and_play() public entry points.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import
from resources.lib.season_pack import requested_episode


class _ResolveSideEffects:
    """Once-only playback-cleanup and fallback-worker starters shared by
    both resolve entry paths. Replaces per-function nonlocal closures."""

    def __init__(
        self,
        params,
        fallback_candidates,
        candidate_loader,
        nzb_url,
        dead,
        settings_getter=None,
    ):
        self._params = params
        self._candidates = fallback_candidates
        self._loader = candidate_loader
        self._nzb_url = nzb_url
        self._dead = dead
        self._settings_getter = settings_getter
        self.requested_episode = requested_episode(
            (params or {}).get("_episode_context") if isinstance(params, dict) else None
        )
        self.cleanup_state = None
        self.fallback_state = None

    def start_cleanup_once(self):
        if self.cleanup_state is None:
            self.cleanup_state = _resolver._start_playback_state_cleanup(self._params)

    def poll_context(self, selected_indexer, rejected_completed_ids):
        """Bundle the once-only hooks + per-attempt hints into a PollContext."""
        return _resolver.PollContext(
            on_primary_submitted=self.start_fallback_after_primary,
            on_existing_completed=self.start_cleanup_once,
            settings_getter=self._settings_getter,
            selected_indexer=selected_indexer,
            rejected_completed_ids=rejected_completed_ids,
            dead=self._dead,
            requested_episode=self.requested_episode,
        )

    def start_fallback_after_primary(self, _nzo_id):
        self.start_cleanup_once()
        if self.fallback_state is None:
            kwargs = {
                "candidate_loader": self._loader,
                "prewarm_delay": _resolver._get_fallback_submit_delay_seconds(
                    self._settings_getter
                ),
                "wait_for_playback": True,
                "dead": self._dead,
                "primary_nzb_url": self._nzb_url,
            }
            kwargs.update(_resolver._settings_getter_kwargs(self._settings_getter))
            self.fallback_state = _resolver._start_fallback_submit_worker(
                self._candidates, **kwargs
            )
        return self.fallback_state


def _resolve_acquire_stream(nzb_url, title, params, rejected_completed_ids, effects):
    """Acquire the stream for the handle-based ``resolve`` path.

    Returns ``(stream_url, stream_headers, dialog)``: the completed fast-path
    (``dialog`` is ``None``) or the submit+poll result. Extracted verbatim from
    ``resolve``."""
    selected_indexer = params.get("_selected_indexer", "")
    picker_completed_lookup_done = _resolver._picker_completed_lookup_done(params)
    picker_kwargs = {
        "on_existing_completed": effects.start_cleanup_once,
        "rejected_completed_ids": rejected_completed_ids,
    }
    if effects.requested_episode is not None:
        picker_kwargs["requested_episode"] = effects.requested_episode
    completed_stream = _resolver._picker_completed_stream(
        title, params, **picker_kwargs
    )
    if completed_stream is not None:
        stream_url, stream_headers = completed_stream
        return stream_url, stream_headers, None
    return _resolver._resolve_submit_and_poll(
        nzb_url,
        title,
        params,
        picker_completed_lookup_done,
        effects.poll_context(selected_indexer, rejected_completed_ids),
    )


def _resolve_and_play_make_effects(params, resolve_params, nzb_url, settings_getter):
    """Build the ``_ResolveSideEffects`` for the handle-less path.

    Prefetches the fallback candidate loader and emits the deferred-lookup
    resolve stages verbatim. Extracted from ``resolve_and_play``."""
    fallback_candidates = resolve_params.get("_fallback_candidates", [])
    fallback_candidate_loader = _resolver._prefetch_fallback_candidate_loader(
        resolve_params.get("_fallback_candidate_loader")
    )
    _resolver._resolve_stage("fallback lookup deferred")
    _resolver._resolve_stage("service config lookup deferred")
    return _ResolveSideEffects(
        params,
        fallback_candidates,
        fallback_candidate_loader,
        nzb_url,
        _resolver.DeadCandidates(),
        settings_getter=settings_getter,
    )


def _resolve_and_play_acquire_stream(
    nzb_url, title, resolve_params, settings_getter, effects
):
    """Acquire the stream for the handle-less ``resolve_and_play`` path.

    Returns ``(stream_url, stream_headers, dialog)``: the completed fast-path
    (``dialog`` is ``None``) or the submit+poll result, with the resolve-stage
    logging woven in verbatim. Extracted verbatim from ``resolve_and_play``."""
    selected_indexer = resolve_params.get("_selected_indexer", "")
    picker_completed_lookup_done = _resolver._picker_completed_lookup_done(
        resolve_params
    )
    # One rejected-id set per resolve attempt, shared so a Completed row the
    # picker body probe rejects is honored by the submit/poll paths.
    rejected_completed_ids = set()
    picker_kwargs = {
        "on_existing_completed": effects.start_cleanup_once,
        "settings_getter": settings_getter,
        "rejected_completed_ids": rejected_completed_ids,
    }
    if effects.requested_episode is not None:
        picker_kwargs["requested_episode"] = effects.requested_episode
    completed_stream = _resolver._picker_completed_stream(
        title, resolve_params, **picker_kwargs
    )
    _resolver._resolve_stage("picker completed stream checked")
    if completed_stream is not None:
        stream_url, stream_headers = completed_stream
        return stream_url, stream_headers, None
    return _resolver._resolve_and_play_submit_and_poll(
        nzb_url,
        title,
        resolve_params,
        picker_completed_lookup_done,
        effects.poll_context(selected_indexer, rejected_completed_ids),
    )


def resolve(handle, params):
    """Handle plugin:// URL resolution (TMDBHelper integration).

    Decodes parameters, polls until the stream is ready, then calls
    setResolvedUrl() — True on success, False on any failure — so Kodi
    always receives a resolution response and does not hang.

    Settings reads and the DialogProgress create call live inside the
    try block so that an exception from either still ends with
    `setResolvedUrl(handle, False)`. Without this, an unexpected raise
    from `_get_poll_settings()` (corrupt addon settings) or
    `dialog.create()` (rare Kodi UI failure) escaped before the try
    started and Kodi hung indefinitely waiting on resolve. Closes
    TODO.md §H.2-H9.
    """
    nzb_url = _resolver.unquote(params.get("nzburl", ""))
    title = _resolver.unquote(params.get("title", ""))
    effects = None

    if not nzb_url:
        _resolver._reject_resolve_handle(
            handle, notify_message=_resolver._string(30096)
        )
        return

    # NZBGet backend toggle: when enabled, the whole download+playback path
    # is handled by the NZBGet resolver (submit to NZBGet, wait, play from
    # SMB). The nzbdav streaming/fallback machinery below is bypassed. This
    # is the handle-based entry; setResolvedUrl is the completion signal.
    if _resolver._nzbget_enabled():
        _resolver._resolve_nzbget_delegate(handle, params)
        return

    dialog = None
    try:
        fallback_candidates = params.get("_fallback_candidates", [])
        fallback_candidate_loader = _resolver._prefetch_fallback_candidate_loader(
            params.get("_fallback_candidate_loader")
        )
        # One rejected-id set per resolve attempt, shared so a Completed row
        # the picker body probe rejects is honored by the submit/poll paths.
        rejected_completed_ids = set()
        dead = _resolver.DeadCandidates()
        effects = _ResolveSideEffects(
            params, fallback_candidates, fallback_candidate_loader, nzb_url, dead
        )
        stream_url, stream_headers, dialog = _resolve_acquire_stream(
            nzb_url, title, params, rejected_completed_ids, effects
        )
        dialog = _resolver._resolve_finish_or_reject(
            handle,
            params,
            (stream_url, stream_headers, dead),
            (effects.fallback_state, effects.start_fallback_after_primary),
            effects.cleanup_state,
            dialog,
        )
    except _resolver._RESOLVE_RUNTIME_ERRORS as error:
        _resolver._resolve_stage("resolve_exception {}".format(error))
        _resolver._stop_fallback_submit_worker(
            effects.fallback_state if effects is not None else None,
            cancel_submitted=True,
        )
        _resolver._handle_resolve_exception("resolve", error, handle=handle)
    finally:
        if dialog is not None:
            dialog.close()


def resolve_and_play(nzb_url, title, params=None):
    """Handle direct execution (executebuiltin://RunPlugin calls).

    Polls until the stream is ready, then plays via xbmc.Player().
    Unlike resolve(), there is no plugin handle so setResolvedUrl() is not
    called; playback simply does not start on failure.

    ``params`` (optional) carries the original plugin URL params dict
    (tmdb_id, imdb, season, episode, etc.) so `_clear_kodi_playback_state`
    can scrub the matching TMDBHelper bookmark row. Without it, the
    bookmark survives and the next replay of the same title resumes
    from the broken-stream offset (TODO.md §H.3).

    Settings reads and `dialog.create()` live inside the try block so
    a raise from either still routes through `_handle_resolve_exception`
    and lets the user see a notification rather than silently no-op'ing
    on the RunPlugin path. Same fix as `resolve()` — TODO.md §H.2-H9.
    """
    dialog = None
    effects = None
    try:
        _resolver._resolve_stage("enter resolve_and_play")
        # NZBGet backend toggle (handle-less path). resolve_and_play has no
        # plugin handle — TMDBHelper /resolve, the in-addon search picker,
        # and script-play all reach here — so play_nzbget starts playback
        # via xbmc.Player() rather than setResolvedUrl. The nzbdav
        # streaming/fallback machinery below is bypassed.
        resolve_params = params or {}
        settings_getter = resolve_params.get("_settings_getter")
        if _resolver._nzbget_enabled(settings_getter):
            _resolver._resolve_and_play_nzbget_delegate(
                nzb_url, title, params, resolve_params
            )
            return
        effects = _resolve_and_play_make_effects(
            params, resolve_params, nzb_url, settings_getter
        )
        stream_url, stream_headers, dialog = _resolve_and_play_acquire_stream(
            nzb_url, title, resolve_params, settings_getter, effects
        )
        dialog = _resolver._resolve_and_play_finish_or_stop(
            _resolver._resume_params_with_title(resolve_params, title),
            (stream_url, stream_headers, effects._dead),
            (effects.fallback_state, effects.start_fallback_after_primary),
            settings_getter,
            effects.cleanup_state,
            dialog,
        )
    except _resolver._RESOLVE_RUNTIME_ERRORS as error:
        _resolver._stop_fallback_submit_worker(
            effects.fallback_state if effects is not None else None,
            cancel_submitted=True,
        )
        _resolver._handle_resolve_exception("resolve_and_play", error)
    finally:
        if dialog is not None:
            dialog.close()
