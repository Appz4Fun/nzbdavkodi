# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Already-completed-copy discovery and submit-error dialogs.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

from typing import NamedTuple

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


class CompletedVideoProbe(NamedTuple):
    """Discovered completed-copy video plus the context needed to validate it."""

    video_path: str
    stream_url: str
    stream_headers: dict
    download_size: object
    webdav_folder: str


def _submit_error_is_too_many_requests(submit_error):
    message = str(submit_error.get("message", "") or "")
    normalized = message.replace(" ", "").replace("-", "").lower()
    return "toomanyrequests" in normalized or "429" in normalized


def _show_submit_error_dialog(submit_error):
    """Show a Kodi modal dialog reporting nzbdav's actual error message.

    Truncates the message to 200 chars (on top of the 500-char cap
    already applied in submit_nzb) and falls back to a clear placeholder
    when nzbdav returned an empty body.
    """
    if _submit_error_is_too_many_requests(submit_error):
        indexer = str(submit_error.get("indexer", "") or "").strip()
        message = (
            _resolver._fmt(30193, indexer) if indexer else _resolver._string(30194)
        )
        _resolver.xbmcgui.Dialog().notification(
            _resolver._addon_name(), message, "", 7000
        )
        return

    message = submit_error["message"][:200] or "(no error message)"
    _resolver.xbmcgui.Dialog().ok(
        _resolver._addon_name(),
        _resolver._fmt(30124, submit_error["status"], message),
    )


def _submit_error_with_indexer(submit_error, selected_indexer):
    if not selected_indexer:
        return submit_error
    enriched = dict(submit_error)
    enriched["indexer"] = selected_indexer
    return enriched


def _close_dialog_before_submit_error(dialog):
    """Close the progress dialog before displaying a terminal submit error."""
    try:
        dialog.close()
    except Exception as error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: progress dialog close before submit error failed: {}".format(
                error
            ),
            _resolver.xbmc.LOGDEBUG,
        )


def _start_existing_completed_cleanup(title, on_existing_completed):
    """Start existing-completed cleanup callback without failing resolve."""
    if on_existing_completed is None:
        return
    try:
        on_existing_completed()
    except Exception as error:  # pylint: disable=broad-except
        _resolver.xbmc.log(
            "NZB-DAV: Existing completed cleanup start failed for '{}': {}".format(
                title, error
            ),
            _resolver.xbmc.LOGWARNING,
        )


def _delegated_find_video_stream_for_folder(
    webdav_folder, settings_getter, title_hint, min_video_size
):
    """Return the webdav one-shot discovery tuple, or None when not delegable.

    Returns the ``(video_path, stream_url, stream_headers)`` tuple when the
    module-level webdav helpers are un-patched (so the single-call path is
    authoritative); ``None`` when delegation does not apply and the caller must
    fall back to the explicit find/url steps.
    """
    try:
        from resources.lib import webdav as _webdav

        if (
            _resolver.find_video_stream_for_folder
            is _webdav.find_video_stream_for_folder
            and _resolver.find_video_file is _webdav.find_video_file
            and _resolver.get_webdav_stream_url_for_path
            is _webdav.get_webdav_stream_url_for_path
        ):
            _resolver._resolve_stage("find_video_stream_for_folder_delegated")
            video_path, stream_url, stream_headers = (
                _resolver.find_video_stream_for_folder(
                    webdav_folder,
                    title_hint=title_hint,
                    min_video_size=min_video_size,
                    **_resolver._settings_getter_kwargs(settings_getter),
                )
            )
            if video_path:
                _resolver._remember_resolved_stream_content_length_hint(
                    video_path, stream_url, stream_headers
                )
            return video_path, stream_url, stream_headers
    except (AttributeError, ImportError):
        # Optional fast-path helper is absent/incompatible on this build;
        # fall through to the canonical find_video_file path below.
        pass
    return None


def _find_video_stream_for_folder(
    webdav_folder, settings_getter=None, title_hint=None, min_video_size=0
):
    """Return video path, URL, and headers for a completed WebDAV folder.

    ``title_hint`` is the requested release/episode title (e.g. the submitted
    scene name). It is threaded into webdav discovery so a multi-episode pack
    returns the requested SxxExx episode rather than whichever sibling file is
    largest. When ``None`` (movie / no identifiable episode) the historical
    largest-video-wins behavior is preserved unchanged.

    ``min_video_size`` is the precomputed advertised-size floor
    (``_stub_min_size_floor``, pack-agnostic); threading it into discovery lets a
    root-level job-start stub recurse into the subfolder holding the real file
    rather than being returned on every poll (#282 follow-up D). ``0`` (default)
    disables the floor, so the unknown-size path is unchanged.
    """
    delegated = _delegated_find_video_stream_for_folder(
        webdav_folder, settings_getter, title_hint, min_video_size
    )
    if delegated is not None:
        return delegated

    kwargs = _resolver._settings_getter_kwargs(settings_getter)
    _resolver._resolve_stage("find_video_file_start folder={}".format(webdav_folder))
    video_path = _resolver.find_video_file(
        webdav_folder,
        hints=_resolver.TitleHints(title_hint=title_hint),
        min_video_size=min_video_size,
        **kwargs,
    )
    _resolver._resolve_stage("find_video_file_done path={}".format(bool(video_path)))
    if not video_path:
        return None, None, None
    _resolver._resolve_stage("get_webdav_stream_url_start")
    stream_url, stream_headers = _resolver.get_webdav_stream_url_for_path(
        video_path, **kwargs
    )
    _resolver._resolve_stage("get_webdav_stream_url_done")
    _resolver._remember_resolved_stream_content_length_hint(
        video_path, stream_url, stream_headers
    )
    return video_path, stream_url, stream_headers


def _record_rejected_completed_id(completed_job, rejected_completed_ids):
    """Record a rejected Completed row's ``nzo_id`` so the submit / poll-loop
    by-name paths skip re-adopting the very row we just rejected.

    No-op when no set was provided or the row has no ``nzo_id``. Shared by the
    pre-submit shortcut's stub guard and its mid-file body probe so both
    rejection reasons feed the same skip set.
    """
    if rejected_completed_ids is None:
        return
    rejected_nzo_id = completed_job.get("nzo_id")
    if rejected_nzo_id:
        rejected_completed_ids.add(rejected_nzo_id)


def _completed_job_webdav_folder(title, completed_job):
    """Validate a completed history row and return its WebDAV folder, or None.

    Rejects non-dicts, rows whose status is set but not ``Completed``, rows whose
    name does not match ``title``, and rows lacking a storage path.
    """
    if not isinstance(completed_job, dict):
        return None
    status = completed_job.get("status", "")
    if status and status != "Completed":
        return None
    name = completed_job.get("name", "")
    if name and name != title:
        return None

    _resolver.xbmc.log(
        "NZB-DAV: '{}' already downloaded, streaming directly".format(title),
        _resolver.xbmc.LOGINFO,
    )
    storage = completed_job.get("storage")
    if not storage:
        _resolver.xbmc.log(
            "NZB-DAV: Completed history row for '{}' has no storage path".format(title),
            _resolver.xbmc.LOGWARNING,
        )
        return None
    return _resolver._storage_to_webdav_path(storage)


def _completed_job_stream(
    title,
    completed_job,
    on_existing_completed=None,
    settings_getter=None,
    rejected_completed_ids=None,
    download_size=None,
):
    """Return a WebDAV stream URL from a completed nzbdav history row.

    When the mid-file body probe rejects a row and ``rejected_completed_ids``
    is provided, the row's ``nzo_id`` is recorded into that set so the submit
    path that follows does not re-adopt the very row we just rejected.

    ``download_size`` is the indexer-advertised release size (bytes), threaded
    in from ``params['_download_size']``. It powers the same #282 job-start-stub
    guard the post-submit accept path applies (``_discovered_video_is_stub``):
    a stale stub left in a Completed row from a prior failed attempt has an
    available body and would otherwise pass the probe below. Defaults to
    ``None`` (guard fails open) so callers without a size are unaffected.
    """
    webdav_folder = _completed_job_webdav_folder(title, completed_job)
    if webdav_folder is None:
        return None
    _start_existing_completed_cleanup(title, on_existing_completed)
    # #282 follow-up D: thread the advertised-size floor into discovery so a
    # stale Completed row whose stub sits at the release root recurses into the
    # subfolder holding the real file rather than serving the stub. 0 for unknown
    # size. The stub guard below shares the same floor (folder-total comparison).
    min_video_size = _resolver._stub_min_size_floor(download_size)
    video_path, stream_url, stream_headers = _resolver._find_video_stream_for_folder(
        webdav_folder,
        settings_getter=settings_getter,
        title_hint=title,
        min_video_size=min_video_size,
    )
    if not video_path:
        return None
    if _completed_job_video_rejected(
        title,
        completed_job,
        CompletedVideoProbe(
            video_path=video_path,
            stream_url=stream_url,
            stream_headers=stream_headers,
            download_size=download_size,
            webdav_folder=webdav_folder,
        ),
        rejected_completed_ids,
        settings_getter,
    ):
        return None
    return stream_url, stream_headers


def _completed_job_video_rejected(
    title,
    completed_job,
    probe,
    rejected_completed_ids,
    settings_getter=None,
):
    """Return True if a discovered completed video is a stub or has no body.

    Rejects nzbdav's job-start stub before the body probe (a stale stub from a
    prior failed attempt has an available body and would otherwise be served --
    the same placeholder .mp4 the post-submit accept path rejects), then rejects
    a Completed row whose mid-file body is unavailable. Records the row's nzo_id
    on either rejection so the submit / poll paths skip it. Pack-agnostic: the
    stub check compares the folder's TOTAL video bytes against the advertised
    size, so a stub-only folder is rejected whether the release is a movie or a
    pack. Fails open on unknown size inside ``_discovered_video_is_stub``.
    """
    video_path = probe.video_path
    stream_url = probe.stream_url
    stream_headers = probe.stream_headers
    download_size = probe.download_size
    webdav_folder = probe.webdav_folder
    if _resolver._discovered_video_is_stub(
        webdav_folder, video_path, download_size, settings_getter
    ):
        _resolver.xbmc.log(
            "NZB-DAV: '{}' completed row exposes '{}' far smaller than the "
            "advertised release size; treating as nzbdav job-start stub and "
            "re-downloading instead of streaming directly".format(title, video_path),
            _resolver.xbmc.LOGWARNING,
        )
        _record_rejected_completed_id(completed_job, rejected_completed_ids)
        return True
    if not _resolver._completed_stream_body_available(stream_url, stream_headers):
        _resolver.xbmc.log(
            "NZB-DAV: '{}' is marked Completed but its mid-file body is "
            "unavailable; re-downloading instead of streaming directly".format(title),
            _resolver.xbmc.LOGWARNING,
        )
        _record_rejected_completed_id(completed_job, rejected_completed_ids)
        return True
    return False


def _existing_completed_stream(
    title,
    on_existing_completed=None,
    completed_job_hint=None,
    completed_job_lookup_done=False,
    settings_getter=None,
    rejected_completed_ids=None,
    download_size=None,
):
    """Return an already-downloaded stream URL when the title exists.

    ``download_size`` (indexer-advertised bytes) is threaded to
    ``_completed_job_stream`` so the pre-submit shortcut rejects the #282
    job-start stub just like the post-submit accept path. Defaults to ``None``
    (guard fails open).
    """
    hinted_stream = _completed_job_stream(
        title,
        completed_job_hint,
        on_existing_completed=on_existing_completed,
        settings_getter=settings_getter,
        rejected_completed_ids=rejected_completed_ids,
        download_size=download_size,
    )
    if hinted_stream is not None:
        return hinted_stream

    if completed_job_lookup_done:
        return None

    existing = _resolver.find_completed_by_name(
        title, **_resolver._settings_getter_kwargs(settings_getter)
    )
    return _completed_job_stream(
        title,
        existing,
        on_existing_completed=on_existing_completed,
        settings_getter=settings_getter,
        rejected_completed_ids=rejected_completed_ids,
        download_size=download_size,
    )


def _picker_completed_stream(
    title,
    params,
    on_existing_completed=None,
    settings_getter=None,
    rejected_completed_ids=None,
):
    """Return a picker-provided completed stream before opening progress UI.

    Shares ``rejected_completed_ids`` with the caller so a picker row the body
    probe rejects here is recorded for the submit/poll paths that follow — the
    picker hint is not re-probed inside ``_poll_until_ready`` once the picker
    has done the completed-history lookup.
    """
    if not params:
        return None
    has_hint = "_completed_job" in params
    lookup_done = _picker_completed_lookup_done(params)
    if not has_hint and not lookup_done:
        return None
    return _resolver._existing_completed_stream(
        title,
        on_existing_completed=on_existing_completed,
        completed_job_hint=params.get("_completed_job"),
        completed_job_lookup_done=lookup_done,
        settings_getter=settings_getter,
        rejected_completed_ids=rejected_completed_ids,
        # The indexer-advertised size rides along on the picker params; thread it
        # through so a picker-supplied Completed row that is the #282 job-start
        # stub is rejected before the progress UI opens, exactly as the
        # submit/poll paths do.
        download_size=params.get("_download_size"),
    )


def _picker_completed_lookup_done(params):
    """Return whether picker metadata already covered completed-history lookup."""
    if not params:
        return False
    return bool(params.get("_completed_job_lookup_done") or "_completed_job" in params)
