# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Resume-point handling and direct/proxy playback finishers.

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


def _show_cache_prompt_after_playback(stream_info):
    """Show the advisory cache prompt after Kodi has the playable URL."""
    try:
        from resources.lib.cache_prompt import maybe_show_cache_prompt

        maybe_show_cache_prompt(stream_info)
    except _resolver._RESOLVE_RUNTIME_ERRORS as error:
        _resolver.xbmc.log(
            "NZB-DAV: cache prompt skipped after playback handoff: {}".format(error),
            _resolver.xbmc.LOGWARNING,
        )


def _apply_resume_start_offset(li, resume_seconds):
    """Apply a consumed Kodi resume offset to a playable ListItem."""
    resume_seconds = _resolver._coerce_resume_seconds(resume_seconds)
    if resume_seconds <= 0.0:
        return
    li.setProperty("StartOffset", str(resume_seconds))


def _read_stored_resume(key):
    """Return the addon-owned stored resume offset for ``key`` (0.0 on miss)."""
    if not key:
        return 0.0
    try:
        return _resolver._coerce_resume_seconds(_resolver.resume_store.get_resume(key))
    except _resolver._RESOLVE_RUNTIME_ERRORS as error:
        _resolver.xbmc.log(
            "NZB-DAV: Failed to read resume state: {}".format(error),
            _resolver.xbmc.LOGWARNING,
        )
        return 0.0


def _preserve_resume_on_cancel(release_id, scrubbed_seconds):
    """Persist a scrubbed Kodi bookmark offset when the user cancels the prompt.

    ``_clear_kodi_playback_state`` has already deleted Kodi's bookmark by the
    time the resume menu is shown, so backing out would otherwise lose the only
    surviving offset (e.g. the first replay after upgrade, before the addon
    store has an entry). Save it under the release identity so the next replay
    still offers it. ``save_resume`` itself drops tiny / near-end positions.
    """
    if not release_id:
        return
    seconds = _resolver._coerce_resume_seconds(scrubbed_seconds)
    if seconds <= 0.0:
        return
    # Never downgrade an existing, larger stored point with an older Kodi
    # bookmark: the prompt was shown using the merged max, so a cancel from a
    # 1 h stored position with a stale 10 min bookmark must keep the 1 h.
    if _read_stored_resume(release_id) >= seconds:
        return
    try:
        _resolver.resume_store.save_resume(release_id, seconds)
    except _resolver._RESOLVE_RUNTIME_ERRORS as error:
        _resolver.xbmc.log(
            "NZB-DAV: Failed to preserve resume on cancel: {}".format(error),
            _resolver.xbmc.LOGWARNING,
        )


def _resume_params_with_title(params, title):
    """Return resume-lookup params with the selected NZB ``title`` applied.

    ``resolve_and_play`` receives the selected release title as a separate
    argument; ``params`` may omit it or still carry the original TMDBHelper
    show/movie title. Copy the selected title in (overriding any stale one) so
    ``release_identity`` keys on the actual release and does not collapse
    distinct releases -- or different episodes of a show -- onto one resume key.
    """
    resume_params = dict(params or {})
    if title:
        resume_params["title"] = title
    return resume_params


def _migrate_legacy_resume(release_id, legacy_key):
    """Read a pre-upgrade URL-keyed offset and migrate it onto the release id.

    During playback the service saves/clears only the release key, so a stale
    URL-keyed entry left behind would survive a natural-end clear of the
    release key and be resurrected on the next replay. Copy it onto the release
    identity and drop the old key once consumed. Returns the offset (0.0 on
    miss).
    """
    legacy_stored = _read_stored_resume(legacy_key)
    if legacy_stored <= 0.0 or not release_id:
        return legacy_stored
    try:
        _resolver.resume_store.save_resume(release_id, legacy_stored)
        _resolver.resume_store.clear_resume(legacy_key)
    except _resolver._RESOLVE_RUNTIME_ERRORS as error:
        _resolver.xbmc.log(
            "NZB-DAV: Failed to migrate legacy resume key: {}".format(error),
            _resolver.xbmc.LOGWARNING,
        )
    return legacy_stored


def _resolve_resume_choice(params, scrubbed_seconds, legacy_key=""):
    """Resolve the resume offset for a replay, prompting only when Kodi would.

    Keys resume on the release identity (title + size + pubdate) rather than
    the churning proxy/SMB/WebDAV URL, merges the scrubbed Kodi bookmark with
    the addon-owned stored offset, then defers to ``resume_choice`` (honoring
    Kodi's native play-action preference). Returns ``(release_id, chosen)``
    where ``chosen`` is the seconds to start at, ``0.0`` to start over, or
    ``None`` when the user cancelled the prompt. The lookup happens once here
    so the finish funcs never re-read ``resume_store``.

    ``legacy_key`` (the source stream URL, when known) is consulted only when
    the release-identity lookup misses, so resume points saved under the old
    URL-keyed scheme survive the upgrade to release-identity keys.
    """
    release_id = _resolver.resume_choice.release_identity(params)
    stored = _read_stored_resume(release_id) if release_id else 0.0
    if stored <= 0.0 and legacy_key:
        stored = _migrate_legacy_resume(release_id, legacy_key)
    merged = max(
        _resolver._coerce_resume_seconds(scrubbed_seconds),
        _resolver._coerce_resume_seconds(stored),
    )
    chosen = _resolver.resume_choice.choose_resume_seconds(release_id, merged)
    return release_id, chosen


def _set_playback_monitor_properties(
    home, play_url, stream_url, resume_key, resume_seconds=0.0
):
    """Signal the background service with playable and stable stream identities.

    ``play_url`` is the real play/proxy URL the service replays on retry;
    ``resume_key`` is the release identity the monitor persists the resume
    point under. ``nzbdav.stream_title`` is derived from the source
    ``stream_url`` (the human-meaningful filename) rather than the disposable
    play URL.
    """
    home.setProperty("nzbdav.stream_url", play_url)
    home.setProperty("nzbdav.resume_key", resume_key)
    home.setProperty(
        "nzbdav.resume_offset", str(_resolver._coerce_resume_seconds(resume_seconds))
    )
    home.setProperty("nzbdav.stream_title", stream_url.rsplit("/", 1)[-1])
    home.setProperty("nzbdav.active", "true")


def _resolve_direct_no_proxy(
    handle, stream_url, stream_headers, monitor_key, resume_seconds
):
    """Resolve a direct (no service-proxy) stream and start handle playback."""
    bust_url = _resolver._cache_bust_url(stream_url)
    play_url = _resolver._build_play_url(bust_url, stream_headers)
    _resolver.xbmc.log(
        "NZB-DAV: Playing direct (no proxy) (handle={}): {}".format(
            handle, _resolver._redact_log(bust_url)
        ),
        _resolver.xbmc.LOGINFO,
    )
    li = _resolver._make_playable_listitem(bust_url, stream_headers)
    _apply_resume_start_offset(li, resume_seconds)
    tmdbhelper_metadata.apply_from_published_params(li)
    home = _resolver.xbmcgui.Window(10000)
    _set_playback_monitor_properties(
        home, play_url, stream_url, monitor_key, resume_seconds
    )
    _resolver.xbmcplugin.setResolvedUrl(handle, True, li)


def _finish_direct_playback(handle, prepared, resume_key="", resume_seconds=0.0):
    """Finish resolver playback on the Kodi thread.

    ``resume_seconds`` is the already-chosen offset (the resume lookup/prompt
    happens once earlier in ``resolve``); ``resume_key`` is the release
    identity the background monitor persists the next resume point under.
    """
    _resolver._resolve_stage("finish_direct_playback_start")
    stream_url = prepared["stream_url"]
    safe_url = _resolver._redact_log(stream_url)
    stream_headers = prepared["stream_headers"]
    service_port = prepared.get("service_port")
    _resolver._resolve_stage(
        "finish_direct_playback_got_params service_port={}".format(service_port)
    )
    resume_seconds = _resolver._coerce_resume_seconds(resume_seconds)
    # The monitor persists the next resume point under the release identity;
    # fall back to the source URL for direct callers that thread no identity.
    monitor_key = resume_key or stream_url

    if service_port:
        proxy_url = prepared["proxy_url"]
        stream_info = prepared["stream_info"]

        # Window properties go DOWN before ``setResolvedUrl`` so the
        # service-side playback monitor sees them the instant Kodi
        # transitions into playback. ``setResolvedUrl`` is what triggers
        # Kodi to actually start the player; if the service's 1 Hz tick
        # fired between resolve-and-property writes, it would miss the
        # session entirely until the next tick. TODO.md §H.2-M47.
        home = _resolver.xbmcgui.Window(10000)
        if stream_info.get("direct"):
            _resolver.xbmc.log(
                "NZB-DAV: MP4 already faststart, direct play: {}".format(safe_url),
                _resolver.xbmc.LOGINFO,
            )
            bust_url = _resolver._cache_bust_url(stream_url)
            li = _resolver._make_playable_listitem(bust_url, stream_headers)
            _apply_resume_start_offset(li, resume_seconds)
            tmdbhelper_metadata.apply_from_published_params(li)
            play_url = _resolver._build_play_url(bust_url, stream_headers)
            _set_playback_monitor_properties(
                home, play_url, stream_url, monitor_key, resume_seconds
            )
            _resolver.xbmcplugin.setResolvedUrl(handle, True, li)
            return

        li = _resolver.xbmcgui.ListItem(path=proxy_url)
        li.setContentLookup(False)
        _resolver._apply_proxy_mime(li, stream_url, stream_info)
        _apply_resume_start_offset(li, resume_seconds)
        tmdbhelper_metadata.apply_from_published_params(li)

        _set_playback_monitor_properties(
            home, proxy_url, stream_url, monitor_key, resume_seconds
        )
        _resolver.xbmcplugin.setResolvedUrl(handle, True, li)
        return

    _resolve_direct_no_proxy(
        handle, stream_url, stream_headers, monitor_key, resume_seconds
    )


def _finish_player_playback(prepared, resume_key="", resume_seconds=0.0):
    """Finish service-side playback on the Kodi thread.

    ``resume_seconds`` is the already-chosen offset (the resume lookup/prompt
    happens once earlier in ``resolve_and_play``); ``resume_key`` is the
    release identity the background monitor persists the next resume point
    under.
    """
    stream_url = prepared["stream_url"]
    safe_url = _resolver._redact_log(stream_url)
    stream_headers = prepared["stream_headers"]
    service_port = prepared.get("service_port")
    home = _resolver.xbmcgui.Window(10000)
    resume_seconds = _resolver._coerce_resume_seconds(resume_seconds)
    # The monitor persists the next resume point under the release identity;
    # fall back to the source URL for direct callers that thread no identity.
    monitor_key = resume_key or stream_url

    if service_port:
        proxy_url = prepared["proxy_url"]
        stream_info = prepared["stream_info"]

        if stream_info.get("direct"):
            _resolver.xbmc.log(
                "NZB-DAV: MP4 already faststart, direct play: {}".format(safe_url),
                _resolver.xbmc.LOGINFO,
            )
            bust_url = _resolver._cache_bust_url(stream_url)
            li = _resolver._make_playable_listitem(bust_url, stream_headers)
            _apply_resume_start_offset(li, resume_seconds)
            tmdbhelper_metadata.apply_from_published_params(li)
            play_url = _resolver._build_play_url(bust_url, stream_headers)
            _set_playback_monitor_properties(
                home, play_url, stream_url, monitor_key, resume_seconds
            )
            _resolver.xbmc.Player().play(li.getPath(), li)
            return

        li = _resolver.xbmcgui.ListItem(path=proxy_url)
        li.setContentLookup(False)
        _resolver._apply_proxy_mime(li, stream_url, stream_info)
        _apply_resume_start_offset(li, resume_seconds)
        tmdbhelper_metadata.apply_from_published_params(li)
        _set_playback_monitor_properties(
            home, proxy_url, stream_url, monitor_key, resume_seconds
        )
        _resolver.xbmc.Player().play(proxy_url, li)
        _show_cache_prompt_after_playback(stream_info)
        return

    bust_url = _resolver._cache_bust_url(stream_url)
    li = _resolver._make_playable_listitem(bust_url, stream_headers)
    _apply_resume_start_offset(li, resume_seconds)
    tmdbhelper_metadata.apply_from_published_params(li)
    play_url = _resolver._build_play_url(bust_url, stream_headers)
    _resolver.xbmc.log(
        "NZB-DAV: Playing direct (no proxy): {}".format(safe_url),
        _resolver.xbmc.LOGINFO,
    )
    _set_playback_monitor_properties(
        home, play_url, stream_url, monitor_key, resume_seconds
    )
    _resolver.xbmc.Player().play(li.getPath(), li)


def _play_direct(handle, stream_url, stream_headers, fallback_sources=None):
    """Play a stream through the local service proxy.

    Every file type routes through the service proxy so Kodi never opens the
    remote WebDAV URL directly. This avoids Kodi's PROPFIND scan of the
    parent directory (nzbdav's WebDAV returns localhost:8080 hrefs that
    break Kodi's directory parser and cascade into an Open failure) and
    sidesteps pipe-header auth quirks on MKV.

    The proxy picks the right mode per file: MP4 gets Tier 1-3 faststart or
    MKV remux; MKV/AVI/other get a range-capable pass-through.
    """
    _resolver._finish_direct_playback(
        handle,
        _resolver._prepare_direct_playback(
            stream_url, stream_headers, fallback_sources=fallback_sources
        ),
    )


def _play_via_proxy(stream_url, stream_headers, fallback_sources=None):
    """Play a stream for the resolve_and_play (service-side) path.

    Routes everything through the service proxy for the same reasons as
    _play_direct — see that function's docstring.

    Each play branch also sets ``nzbdav.stream_url`` /
    ``nzbdav.stream_title`` / ``nzbdav.active`` on the Home window
    (window 10000). The service-side playback monitor (``service.py``)
    polls these to drive its retry / error-dialog state machine; the
    RunPlugin entrypoint used to skip them so a stream that died
    mid-playback never triggered the retry path. Closes
    TODO.md §H.2-H10.
    """
    _resolver._finish_player_playback(
        _resolver._prepare_direct_playback(
            stream_url, stream_headers, fallback_sources=fallback_sources
        )
    )
