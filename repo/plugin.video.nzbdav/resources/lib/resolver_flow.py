# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""resolve()/resolve_and_play() inner helpers.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import
from resources.lib import tmdbhelper_metadata
from resources.lib import tmdbhelper_scrobble


def _nzbget_enabled(settings_getter=None):
    """Return True when the NZBGet backend toggle is on.

    When the caller has an injected ``settings_getter`` (the handle-less
    ``resolve_and_play`` path passes one to avoid Kodi settings-API reads
    during RunScript/widget invocations), use it so the toggle is read the
    same way as the rest of that flow — otherwise an unavailable
    ``xbmcaddon.Addon().getSetting`` would silently disable NZBGet and fall
    back to nzbdav despite the user enabling it.

    Read defensively either way: ``Addon()`` can raise ``RuntimeError`` early
    in startup, an injected getter may raise, and ``getSetting`` can (rarely)
    return None, so any failure falls back to the nzbdav path instead of
    letting an exception escape ``resolve`` / ``resolve_and_play`` before a
    resolution call — the exact resolve-hang that TODO.md §H.2-H9 guards
    against.
    """
    try:
        if settings_getter is not None:
            value = settings_getter("nzbget_enabled", "")
        else:
            value = _resolver.xbmcaddon.Addon("plugin.video.nzbdav").getSetting(
                "nzbget_enabled"
            )
        return (value or "").strip().lower() == "true"
    except (RuntimeError, AttributeError, TypeError):
        return False


def _scrub_bookmark_for_nzbget(params):
    """Clear the TMDBHelper/plugin bookmark before an NZBGet handoff.

    NZBGet bypasses the nzbdav playback-state cleanup, so scrub the stale
    bookmark here or the next replay reopens plugin://... instead of the
    resolved stream (TODO.md §H.3). Guarded so a cleanup failure can't escape
    before the resolve completes; returns the scrubbed resume offset.
    """
    try:
        return _resolver._coerce_resume_seconds(
            _resolver._clear_kodi_playback_state(params)
        )
    except Exception as cleanup_error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: NZBGet pre-handoff bookmark cleanup failed: "
            "{}".format(cleanup_error),
            _resolver.xbmc.LOGWARNING,
        )
        return 0.0


def _reject_resolve_handle(handle, notify_message=None):
    """Fail the handle-based resolve: optional notify, then setResolvedUrl(False).

    Preserves the exact failure sequence shared across ``resolve`` and its
    helpers: an optional user-facing dialog, the ``setResolvedUrl(handle,
    False)`` completion signal Kodi waits on, then clearing the video playlist.
    """
    if notify_message is not None:
        # The notification is optional UI; if Dialog().ok() raises (e.g. during
        # shutdown) the handle-based resolve must still receive its False
        # resolution below or Kodi hangs (TODO.md §H.2-H9 no-hang guarantee).
        try:
            _resolver.xbmcgui.Dialog().ok(_resolver._addon_name(), notify_message)
        except (RuntimeError, OSError, TypeError) as error:
            _resolver.xbmc.log(
                "NZB-DAV: resolve rejection notification failed: {}".format(error),
                _resolver.xbmc.LOGWARNING,
            )
    _resolver.xbmcplugin.setResolvedUrl(handle, False, _resolver.xbmcgui.ListItem())
    _resolver.xbmc.PlayList(_resolver.xbmc.PLAYLIST_VIDEO).clear()


def _resolve_nzbget_delegate(handle, params):
    """Run the handle-based NZBGet delegation branch of ``resolve``.

    Extracted verbatim from ``resolve`` so the no-hang resolve contract is
    preserved: every path ends in a ``setResolvedUrl`` (True via
    ``resolve_and_play_nzbget`` on success / False on cancel or delegation
    failure). Guards the whole delegation because it runs before resolve()'s
    protected error path, so an import/call failure here must still end in a
    failure setResolvedUrl or the handle-based resolve contract hangs
    (TODO.md §H.2-H9 no-hang guarantee).
    """
    try:
        from resources.lib.nzbget_resolver import resolve_and_play_nzbget

        scrubbed_seconds = _scrub_bookmark_for_nzbget(params)
        # Resolve resume once here (release-identity keyed lookup + the
        # native resume prompt) so the NZBGet path honors the same
        # resume/start-over choice as the nzbdav path. A None choice means
        # the user cancelled the prompt; fail the resolve like any other.
        release_id, chosen = _resolver._resolve_resume_choice(params, scrubbed_seconds)
        if chosen is None:
            _resolver._preserve_resume_on_cancel(release_id, scrubbed_seconds)
            _reject_resolve_handle(handle)
            return
        # Carry the chosen resume position + release identity into NZBGet
        # playback so a replay resumes where the user left off (applied as
        # StartOffset) and the background monitor persists the next point
        # under the release identity.
        resolve_and_play_nzbget(
            handle, params, resume_seconds=chosen, resume_key=release_id
        )
    except Exception as nzbget_error:  # pylint: disable=broad-except
        from resources.lib.http_util import redact_text

        _resolver.xbmc.log(
            "NZB-DAV: NZBGet delegation failed: {}".format(
                redact_text(str(nzbget_error))
            ),
            _resolver.xbmc.LOGERROR,
        )
        _reject_resolve_handle(handle)


def _prepare_ready_stream_for_handoff(
    stream_url,
    stream_headers,
    fallback_state,
    dead,
    playback_cleanup_state,
    dialog,
):
    """Prepare the proxy + wait for the bookmark scrub for a ready stream.

    Shared verbatim by both resolve paths: snapshot the live fallback sources,
    start/await the proxy prepare, await the background bookmark-cleanup scrub,
    then close the progress dialog before the resume prompt so the
    Resume/Start-over contextmenu is never stacked behind the modal
    DialogProgress. Returns ``(prepared, scrubbed_seconds, dialog)`` where
    ``dialog`` is ``None`` once closed (the caller's ``finally`` stays a no-op).
    """
    fallback_sources = _resolver._playback_fallback_sources_for_stream(
        stream_url,
        _resolver._fallback_submit_jobs_snapshot(
            fallback_state,
            wait_seconds=_resolver._PLAYBACK_PREPARE_HANDOFF_GRACE_SECONDS,
        ),
        dead=dead,
    )
    playback_prepare_state = _resolver._start_direct_playback_prepare(
        stream_url,
        stream_headers,
        fallback_sources=fallback_sources,
        service_config_state=None,
    )
    prepared = _resolver._wait_direct_playback_prepare(playback_prepare_state)
    scrubbed_seconds = _resolver._wait_playback_state_cleanup(playback_cleanup_state)
    if dialog is not None:
        dialog.close()
        dialog = None
    return prepared, scrubbed_seconds, dialog


def _invoke_poll_until_ready(
    nzb_url,
    title,
    dialog,
    poll_interval,
    download_timeout,
    params_src,
    picker_completed_lookup_done,
    poll_ctx,
):
    """Verbatim ``_poll_until_ready`` invocation shared by both submit helpers.

    ``params_src`` is the params/resolve_params dict whose ``_completed_job`` /
    ``_download_pubdate`` / ``_download_size`` are folded into ``poll_ctx``, the
    ``PollContext`` bundling the hooks/hints constructed at the resolve entry.
    The resolve path leaves ``settings_getter`` at its ``None`` default, so this
    stays byte-identical to the original handle-based call. Returns
    ``(stream_url, stream_headers)``.
    """
    completed_job_hint = (
        None if picker_completed_lookup_done else params_src.get("_completed_job")
    )
    poll_ctx = poll_ctx._replace(
        completed_job_hint=completed_job_hint,
        completed_job_lookup_done=picker_completed_lookup_done,
        download_pubdate=params_src.get("_download_pubdate"),
        download_size=params_src.get("_download_size"),
    )
    return _resolver._poll_until_ready(
        nzb_url,
        title,
        dialog,
        poll_interval,
        download_timeout,
        poll_ctx=poll_ctx,
    )


def _resolve_submit_and_poll(
    nzb_url,
    title,
    params,
    picker_completed_lookup_done,
    poll_ctx,
):
    """Submit + poll for the handle-based ``resolve`` path (no completed hit).

    Extracted verbatim from the ``else`` branch of ``resolve``. ``poll_ctx`` is
    the ``PollContext`` bundling the hooks/hints built at the entry (its
    ``settings_getter`` stays ``None`` on this path). See
    ``_resolve_and_play_submit_and_poll`` for the bookmark-cleanup timing
    rationale. Returns ``(stream_url, stream_headers, dialog)``.
    """
    poll_interval, download_timeout = _resolver._get_poll_settings()
    # Offer the pre-submit queue clear before the dialog so the yes/no prompt
    # is never stacked behind a modal DialogProgress.
    _resolver._maybe_clear_queue_before_submit(
        title, completed_lookup_done=picker_completed_lookup_done
    )
    dialog = _resolver.xbmcgui.DialogProgress()
    dialog.create(_resolver._addon_name(), _resolver._string(30097))
    # Own the modal locally until we hand it back. A raise before the return
    # would otherwise leave the caller's dialog None, so its outer finally
    # could never close this modal (no-hang invariant; AGENTS.md).
    owner = dialog
    try:
        if not picker_completed_lookup_done:
            poll_ctx.on_existing_completed()
        stream_url, stream_headers = _invoke_poll_until_ready(
            nzb_url,
            title,
            dialog,
            poll_interval,
            download_timeout,
            params,
            picker_completed_lookup_done,
            poll_ctx,
        )
        owner = None
        return stream_url, stream_headers, dialog
    finally:
        if owner is not None:
            owner.close()


def _resolve_finish_or_reject(
    handle,
    params,
    stream,
    fallback,
    playback_cleanup_state,
    dialog,
):
    """Run the success/failure tail of the handle-based ``resolve`` path.

    Extracted verbatim from ``resolve``'s ``if stream_url: ... else: ...`` block.
    ``stream`` is ``(stream_url, stream_headers, dead)``; ``fallback`` is
    ``(fallback_state, start_fallback_after_primary)`` where the second item is
    the closure that lazily starts the fallback worker and assigns the outer
    ``fallback_state``. Returns the (possibly ``None``) progress dialog so the
    caller's ``finally`` close stays a no-op after this closed it.
    """
    stream_url, stream_headers, dead = stream
    fallback_state, start_fallback_after_primary = fallback
    if not stream_url:
        _resolver._stop_fallback_submit_worker(fallback_state, cancel_submitted=True)
        _reject_resolve_handle(handle)
        return dialog
    if fallback_state is None:
        fallback_state = start_fallback_after_primary(None)
    return _resolve_play_ready_stream(
        handle,
        params,
        stream_url,
        stream_headers,
        fallback_state,
        dead,
        playback_cleanup_state,
        dialog,
    )


def _resolve_play_ready_stream(
    handle,
    params,
    stream_url,
    stream_headers,
    fallback_state,
    dead,
    playback_cleanup_state,
    dialog,
):
    """Hand a ready stream off to playback on the handle-based ``resolve`` path.

    Extracted verbatim from the ``if stream_url:`` success block of
    ``resolve``: prepare the proxy, wait for the bookmark-cleanup scrub,
    resolve the resume choice, then finish with ``setResolvedUrl(handle, True)``
    — or, on a cancelled resume prompt, ``setResolvedUrl(handle, False)``.
    Returns the (possibly ``None``) progress dialog so the caller's ``finally``
    close stays a no-op after this closed it.
    """
    prepared, scrubbed_seconds, dialog = _prepare_ready_stream_for_handoff(
        stream_url,
        stream_headers,
        fallback_state,
        dead,
        playback_cleanup_state,
        dialog,
    )
    # Resolve resume once here (release-identity keyed lookup + the
    # native resume prompt). A None choice means the user cancelled the
    # prompt; treat it like any other resolve failure.
    release_id, chosen = _resolver._resolve_resume_choice(
        params, scrubbed_seconds, legacy_key=stream_url
    )
    if chosen is None:
        _resolver._preserve_resume_on_cancel(release_id, scrubbed_seconds)
        _resolver._stop_fallback_submit_worker(fallback_state, cancel_submitted=True)
        _reject_resolve_handle(handle)
        return dialog
    _resolver._arm_live_fallback_push(prepared, fallback_state, stream_url, dead=dead)
    # See the note in the script-play path above: identity goes out before the
    # handoff so the scrobbler sees it when playback starts.
    tmdbhelper_scrobble.publish_player_info(params)
    tmdbhelper_metadata.publish_params(params)
    _resolver._finish_direct_playback(
        handle, prepared, resume_key=release_id, resume_seconds=chosen
    )
    # Playback handed off to Kodi: start the fallback worker's "minutes
    # into playback" countdown now, not from the earlier primary submit.
    _resolver._signal_fallback_playback_started(fallback_state)
    return dialog


def _resolve_and_play_nzbget_delegate(nzb_url, title, params, resolve_params):
    """Run the handle-less NZBGet delegation branch of ``resolve_and_play``.

    Extracted verbatim from ``resolve_and_play``. There is no plugin handle on
    this path, so a cancelled resume prompt simply does not start playback (no
    setResolvedUrl, matching this path's contract). Same bookmark scrub as the
    handle-based resolve() NZBGet branch: the nzbdav playback-state cleanup is
    bypassed, so clear the stale TMDBHelper/plugin bookmark before handoff or
    the next replay resumes plugin://... instead of the resolved stream
    (TODO.md §H.3).
    """
    from resources.lib.nzbget_resolver import play_nzbget

    scrubbed_seconds = _scrub_bookmark_for_nzbget(params)
    # Resolve resume once here (release-identity keyed lookup + the native
    # resume prompt). There is no plugin handle on this path, so a cancelled
    # prompt simply does not start playback.
    release_id, chosen = _resolver._resolve_resume_choice(
        _resolver._resume_params_with_title(resolve_params, title), scrubbed_seconds
    )
    if chosen is None:
        _resolver._preserve_resume_on_cancel(release_id, scrubbed_seconds)
        return
    play_nzbget(
        nzb_url,
        title,
        params,
        resume_seconds=chosen,
        resume_key=release_id,
    )


def _resolve_and_play_submit_and_poll(
    nzb_url,
    title,
    resolve_params,
    picker_completed_lookup_done,
    poll_ctx,
):
    """Submit + poll (handle-less path); verbatim ``resolve_and_play`` ``else``.

    Mirrors ``_resolve_submit_and_poll`` with resolve-stage logging and a
    threaded ``settings_getter``. ``poll_ctx`` is the ``PollContext`` bundling
    the hooks/hints built at the entry (including its ``settings_getter``).
    """
    settings_getter = poll_ctx.settings_getter
    _resolver._resolve_stage("poll settings start")
    poll_interval, download_timeout = _resolver._get_poll_settings(
        settings_getter=settings_getter
    )
    _resolver._resolve_stage("poll settings done")
    _resolver._maybe_clear_queue_before_submit(
        title,
        settings_getter=settings_getter,
        completed_lookup_done=picker_completed_lookup_done,
    )
    _resolver._resolve_stage("progress create start")
    dialog = _resolver.xbmcgui.DialogProgress()
    dialog.create(_resolver._addon_name(), _resolver._string(30097))
    _resolver._resolve_stage("progress create done")
    # Own the modal locally until we hand it back; see _resolve_submit_and_poll.
    owner = dialog
    try:
        if not picker_completed_lookup_done:
            poll_ctx.on_existing_completed()
        _resolver._resolve_stage("poll until ready start")
        stream_url, stream_headers = _invoke_poll_until_ready(
            nzb_url,
            title,
            dialog,
            poll_interval,
            download_timeout,
            resolve_params,
            picker_completed_lookup_done,
            poll_ctx,
        )
        _resolver._resolve_stage(
            "poll until ready done stream={}".format(bool(stream_url))
        )
        owner = None
        return stream_url, stream_headers, dialog
    finally:
        if owner is not None:
            owner.close()


def _resolve_and_play_finish_or_stop(
    resume_params,
    stream,
    fallback,
    settings_getter,
    playback_cleanup_state,
    dialog,
):
    """Run the success/failure tail of the handle-less ``resolve_and_play`` path.

    Extracted verbatim from ``resolve_and_play``'s ``if stream_url: ... else:
    ...`` block. ``stream`` is ``(stream_url, stream_headers, dead)``;
    ``fallback`` is ``(fallback_state, start_fallback_after_primary)`` where the
    second item lazily starts the fallback worker and assigns the outer
    ``fallback_state``. Returns the (possibly ``None``) progress dialog so the
    caller's ``finally`` close stays a no-op after this closed it.
    """
    stream_url, stream_headers, dead = stream
    fallback_state, start_fallback_after_primary = fallback
    if not stream_url:
        _resolver._stop_fallback_submit_worker(fallback_state, cancel_submitted=True)
        return dialog
    if fallback_state is None:
        fallback_state = start_fallback_after_primary(None)
    return _resolve_and_play_ready_stream(
        resume_params,
        stream_url,
        stream_headers,
        fallback_state,
        dead,
        settings_getter,
        playback_cleanup_state,
        dialog,
    )


def _resolve_and_play_ready_stream(
    resume_params,
    stream_url,
    stream_headers,
    fallback_state,
    dead,
    settings_getter,
    playback_cleanup_state,
    dialog,
):
    """Hand a ready stream off to playback on the handle-less ``resolve_and_play``.

    Extracted verbatim from the ``if stream_url:`` success block of
    ``resolve_and_play`` (resolve-stage logging and the ``settings_getter``
    prepare kwarg included). There is no plugin handle on this path, so a
    cancelled resume prompt simply does not start playback (no setResolvedUrl,
    matching this path's contract). ``resume_params`` is the title-applied
    resume-lookup dict. Returns the (possibly ``None``) progress dialog so the
    caller's ``finally`` close stays a no-op after this closed it.
    """
    prepared, scrubbed_seconds, dialog = _prepare_player_ready_stream_for_handoff(
        stream_url,
        stream_headers,
        fallback_state,
        dead,
        settings_getter,
        playback_cleanup_state,
        dialog,
    )
    release_id, chosen = _resolver._resolve_resume_choice(
        resume_params,
        scrubbed_seconds,
        legacy_key=stream_url,
    )
    if chosen is None:
        _resolver._preserve_resume_on_cancel(release_id, scrubbed_seconds)
        _resolver._stop_fallback_submit_worker(fallback_state, cancel_submitted=True)
        return dialog
    _resolver._arm_live_fallback_push(prepared, fallback_state, stream_url, dead=dead)
    # Publish the TMDB identity before handing off so TMDb Helper's scrobbler
    # can attribute this playback to Trakt; the stream URL itself carries none.
    tmdbhelper_scrobble.publish_player_info(resume_params)
    tmdbhelper_metadata.publish_params(resume_params)
    _resolver._finish_player_playback(
        prepared, resume_key=release_id, resume_seconds=chosen
    )
    # Playback handed off to the player: start the fallback worker's
    # "minutes into playback" countdown now, not from the primary submit.
    _resolver._signal_fallback_playback_started(fallback_state)
    _resolver._resolve_stage("player playback started")
    return dialog


def _prepare_player_ready_stream_for_handoff(
    stream_url,
    stream_headers,
    fallback_state,
    dead,
    settings_getter,
    playback_cleanup_state,
    dialog,
):
    """Prepare the proxy + scrub wait for the handle-less ``resolve_and_play``.

    Same shape as ``_prepare_ready_stream_for_handoff`` but threads
    ``settings_getter`` into the prepare kwargs and keeps the resolve-stage
    logging woven in verbatim (this path emits stages; the handle-based path
    does not). Returns ``(prepared, scrubbed_seconds, dialog)`` with ``dialog``
    set to ``None`` once closed before the resume prompt.
    """
    fallback_sources = _resolver._playback_fallback_sources_for_stream(
        stream_url,
        _resolver._fallback_submit_jobs_snapshot(
            fallback_state,
            wait_seconds=_resolver._PLAYBACK_PREPARE_HANDOFF_GRACE_SECONDS,
        ),
        dead=dead,
    )
    _resolver._resolve_stage("prepare playback start")
    prepare_kwargs = {
        "fallback_sources": fallback_sources,
        "service_config_state": None,
    }
    prepare_kwargs.update(_resolver._settings_getter_kwargs(settings_getter))
    playback_prepare_state = _resolver._start_direct_playback_prepare(
        stream_url, stream_headers, **prepare_kwargs
    )
    _resolver._resolve_stage("finish playback start")
    _resolver._resolve_stage("prepare wait start")
    prepared = _resolver._wait_direct_playback_prepare(playback_prepare_state)
    _resolver._resolve_stage(
        "prepare wait done service_port={}".format(
            prepared.get("service_port") if prepared else ""
        )
    )
    _resolver._resolve_stage("cleanup wait start")
    scrubbed_seconds = _resolver._wait_playback_state_cleanup(playback_cleanup_state)
    _resolver._resolve_stage("cleanup wait done")
    if dialog is not None:
        dialog.close()
        dialog = None
    return prepared, scrubbed_seconds, dialog
