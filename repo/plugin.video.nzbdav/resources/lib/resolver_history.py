# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""History/result handling and completed-video classification.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


def _abort_poll_before_fetch(
    iteration, elapsed, download_timeout, dialog, nzo_id, title
):
    """Handle the early-return poll abort conditions."""
    if iteration > _resolver.MAX_POLL_ITERATIONS:
        _resolver.xbmc.log(
            "NZB-DAV: Max poll iterations ({}) reached for nzo_id={}".format(
                _resolver.MAX_POLL_ITERATIONS, nzo_id
            ),
            _resolver.xbmc.LOGERROR,
        )
        # _fmt not _string: 30099 is "Download timed out after {} seconds"
        # — using _string() would render the literal "{}" to the user.
        _resolver.xbmcgui.Dialog().ok(
            _resolver._addon_name(), _resolver._fmt(30099, int(elapsed))
        )
        _resolver.cancel_job(nzo_id)
        return True

    if elapsed >= download_timeout:
        _resolver.xbmc.log(
            "NZB-DAV: Download timed out after {}s for nzo_id={} (title='{}'). "
            "Check the nzbdav queue for stalled jobs or increase the "
            "download timeout in addon settings.".format(int(elapsed), nzo_id, title),
            _resolver.xbmc.LOGERROR,
        )
        _resolver.xbmcgui.Dialog().ok(
            _resolver._addon_name(), _resolver._fmt(30099, int(elapsed))
        )
        _resolver.cancel_job(nzo_id)
        return True

    if dialog.iscanceled():
        _resolver.xbmc.log(
            "NZB-DAV: User cancelled resolve for nzo_id={}".format(nzo_id),
            _resolver.xbmc.LOGINFO,
        )
        _resolver.cancel_job(nzo_id)
        return True

    return False


def _status_dialog_message(status, percentage):
    """Return the progress-dialog text for a queue status update."""
    msg_id = _resolver._STATUS_MESSAGES.get(status)
    if not msg_id:
        return "Status: {}".format(status)
    if msg_id == 30105:
        return _resolver._fmt(msg_id, percentage)
    return _resolver._string(msg_id)


def _handle_job_status(job_status, nzo_id, dialog, last_status):
    """Apply queue-status updates and detect terminal failed states."""
    if not job_status:
        return False, last_status

    status = job_status.get("status", "Unknown")
    percentage = job_status.get("percentage", "0")

    if dialog.iscanceled():
        _resolver.xbmc.log(
            "NZB-DAV: User cancelled job {}".format(nzo_id),
            _resolver.xbmc.LOGINFO,
        )
        _resolver.cancel_job(nzo_id)
        return True, last_status

    if status != last_status:
        _resolver.xbmc.log(
            "NZB-DAV: Job {} status changed: {} -> {}".format(
                nzo_id, last_status, status
            ),
            _resolver.xbmc.LOGINFO,
        )
        last_status = status

    if status.lower() in ("failed", "deleted"):
        _resolver.xbmc.log(
            "NZB-DAV: Job {} failed/deleted (status={})".format(nzo_id, status),
            _resolver.xbmc.LOGERROR,
        )
        _resolver.xbmcgui.Dialog().ok(_resolver._addon_name(), _resolver._string(30100))
        return True, last_status

    try:
        progress = int(float(percentage or 0))
    except (TypeError, ValueError):
        progress = 0
    progress = max(0, min(progress, 100))
    _resolver._safe_dialog_update(
        dialog, progress, _status_dialog_message(status, percentage)
    )
    return False, last_status


def _find_completed_video_stream_with_rechecks(
    webdav_folder, monitor=None, settings_getter=None, title_hint=None, min_video_size=0
):
    """Return a completed WebDAV stream, briefly rechecking symlink visibility.

    ``title_hint`` (the requested release/episode title) is threaded into
    discovery so a multi-episode pack resolves to the requested episode; with
    ``None`` the historical largest-video behavior is preserved.

    ``min_video_size`` is the single-file advertised-size floor forwarded into
    discovery so a root-level job-start stub recurses into the subfolder holding
    the real file (#282 follow-up D). ``0`` (default) disables the floor.
    """
    _resolver._resolve_stage("find_video_stream_start")
    video_path, stream_url, stream_headers = _resolver._find_video_stream_for_folder(
        webdav_folder,
        settings_getter=settings_getter,
        title_hint=title_hint,
        min_video_size=min_video_size,
    )
    _resolver._resolve_stage("find_video_stream_done path={}".format(bool(video_path)))
    if video_path or monitor is None:
        return video_path, stream_url, stream_headers

    for delay_seconds in _resolver._COMPLETED_NO_VIDEO_RECHECK_DELAYS_SECONDS:
        if monitor.waitForAbort(delay_seconds):
            return None, None, None
        _resolver._resolve_stage(
            "find_video_stream_retry delay={}".format(delay_seconds)
        )
        video_path, stream_url, stream_headers = (
            _resolver._find_video_stream_for_folder(
                webdav_folder,
                settings_getter=settings_getter,
                title_hint=title_hint,
                min_video_size=min_video_size,
            )
        )
        _resolver._resolve_stage(
            "find_video_stream_retry_done path={}".format(bool(video_path))
        )
        if video_path:
            return video_path, stream_url, stream_headers
    _resolver._resolve_stage("find_video_stream_rechecks_exhausted")
    return None, None, None


def _advertised_size_bytes(download_size):
    """Best-effort parse of the indexer-advertised size (bytes) into an int.

    ``download_size`` is the selected result's ``size`` threaded through as
    ``params['_download_size']`` -- a digit string of bytes from Prowlarr /
    NZBHydra2, but tolerate ints/floats and stray formatting too. Returns 0
    (unknown) on anything unparseable so callers fail OPEN. Mirrors router's
    ``_result_size_bytes`` without importing it (avoids a resolver->router
    dependency).
    """
    if isinstance(download_size, bool):
        return 0
    if isinstance(download_size, (int, float)):
        return int(download_size) if download_size > 0 else 0
    if isinstance(download_size, str):
        text = download_size.strip().replace(",", "")
        if not text:
            return 0
        try:
            return max(0, int(float(text)))
        except (TypeError, ValueError, OverflowError):
            # `inf` / overflowing exponents (e.g. "1e10000") parse as float but
            # raise OverflowError on int(); treat as unknown (fail OPEN) like
            # every other unparseable size rather than letting it escape the
            # resolver (OverflowError is not in _RESOLVE_RUNTIME_ERRORS).
            return 0
    return 0


def _stub_min_size_floor(download_size, title):
    """Return the minimum plausible discovered-video size (bytes) for a
    single-file release, or 0 when no floor applies.

    The floor is ``advertised * _STUB_VIDEO_MIN_ADVERTISED_FRACTION`` -- exactly
    the threshold ``_discovered_video_is_stub`` rejects below. Returning it (not
    just a yes/no) lets webdav discovery treat a current-level candidate below
    the floor as nzbdav's job-start stub and recurse into subfolders for the
    real file before falling back to it (#282 follow-up D), so the accept-time
    guard and discovery agree on what "too small" means.

    Returns 0 (no floor) when the advertised size is unknown -- so the guard
    fails OPEN -- and for packs, whose advertised size spans every episode so a
    single picked episode is legitimately a fraction of it. May be fractional
    (the exact float threshold) so callers stay byte-consistent with the
    ``found < advertised * fraction`` comparison.
    """
    advertised = _advertised_size_bytes(download_size)
    if advertised <= 0:
        return 0
    try:
        from resources.lib.filter import release_is_pack

        if release_is_pack(title):
            return 0
    except Exception:  # pylint: disable=broad-except
        # Pack detection unavailable -> treat as single-file so the guard still
        # protects the reported (movie) case; the fraction floor is the net.
        pass
    return advertised * _resolver._STUB_VIDEO_MIN_ADVERTISED_FRACTION


def _discovered_video_is_stub(video_path, download_size, title):
    """Return True when a non-pack release's discovered video is implausibly
    small versus the indexer-advertised size (nzbdav's job-start stub, #282).

    nzbdav writes a small placeholder ``.mp4`` when a job starts; the completed
    WebDAV scan can return it seconds after submit, and streaming it plays ~30s
    of a stub instead of the feature. Compare the discovered file's size
    (captured by webdav discovery as a PROPFIND ``getcontentlength`` hint)
    against the advertised release size and reject anything far below it.

    Fails OPEN (returns ``False``) when either size is unknown, so a missing
    field never blocks a real stream, and is skipped for packs -- a pack's
    advertised size spans every episode, so one picked episode is legitimately
    a fraction of it. Shares ``_stub_min_size_floor`` with webdav discovery so
    the two never disagree on the threshold.
    """
    floor = _stub_min_size_floor(download_size, title)
    if floor <= 0:
        return False
    try:
        from resources.lib import webdav as _webdav

        found = _webdav.get_video_file_size_hint(video_path)
    except Exception:  # pylint: disable=broad-except
        return False
    if found <= 0:
        return False
    return found < floor


def _report_history_failed(history, title, modal_failures):
    """Log and surface a Failed history row to the user."""
    from resources.lib.http_util import redact_text

    fail_msg = redact_text(history.get("fail_message", "") or "")
    _resolver.xbmc.log(
        "NZB-DAV: Download failed for nzo_id={} (title='{}'): {}".format(
            history.get("nzo_id", "unknown"), title, fail_msg or "unknown reason"
        ),
        _resolver.xbmc.LOGERROR,
    )
    error_text = fail_msg if fail_msg else _resolver._string(30100)
    error_text = redact_text(error_text)
    if modal_failures:
        _resolver.xbmcgui.Dialog().ok(_resolver._addon_name(), error_text)
    else:
        _resolver._notify(_resolver._addon_name(), error_text, 5000)


def _report_no_video_exhaustion(
    title, storage, webdav_folder, no_video_retries, body_unavailable
):
    """Log and surface the terminal "no playable video" dialog after retries."""
    if body_unavailable:
        _resolver.xbmc.log(
            "NZB-DAV: '{}' completed but its mid-file body stayed "
            "unavailable after {} attempts (storage='{}')".format(
                title, no_video_retries, storage
            ),
            _resolver.xbmc.LOGERROR,
        )
        msg = (
            "Download completed but the video file is incomplete:\n{}\n\n"
            "The backend is missing or has not retained the middle "
            "articles. Check nzbdav retention / repair (PAR2) for this "
            "release."
        ).format(webdav_folder)
    else:
        _resolver.xbmc.log(
            "NZB-DAV: Download completed but no video file found "
            "at '{}' after {} attempts (storage='{}')".format(
                webdav_folder, no_video_retries, storage
            ),
            _resolver.xbmc.LOGERROR,
        )
        msg = (
            "Video file not found in WebDAV folder: {}\n\n"
            "Check WebDAV settings and ensure the download completed on nzbdav."
        ).format(webdav_folder)
    _resolver.xbmcgui.Dialog().ok(_resolver._addon_name(), msg)


def _handle_history_result(
    history,
    title,
    no_video_retries,
    max_no_video_retries,
    monitor=None,
    settings_getter=None,
    modal_failures=True,
    download_size=None,
):
    """Handle history-based completion and failure states.

    Use ``.get(...)`` for ``status`` and ``storage`` instead of bracket
    access. ``not history`` filters out None and empty dicts, but a
    history row with the keys *omitted* (server bug, partial response)
    would still pass that guard and KeyError on subscript access. The
    KeyError used to surface as a generic resolver crash; now a missing
    field falls through to the "not Completed" branch which returns
    cleanly. TODO.md §H.2-M41.
    """
    if not history:
        return False, None, None, no_video_retries

    status = history.get("status")
    if status == "Failed":
        _report_history_failed(history, title, modal_failures)
        return True, None, None, no_video_retries

    if status != "Completed":
        return False, None, None, no_video_retries

    return _handle_completed_history(
        history,
        title,
        no_video_retries,
        max_no_video_retries,
        monitor=monitor,
        settings_getter=settings_getter,
        download_size=download_size,
    )


def _handle_completed_history(
    history,
    title,
    no_video_retries,
    max_no_video_retries,
    monitor=None,
    settings_getter=None,
    download_size=None,
):
    """Handle a Completed history row: discover, stub/body-probe, or retry.

    Returns the same ``(should_stop, stream_url, stream_headers,
    no_video_retries)`` tuple as ``_handle_history_result``.
    """
    storage = history.get("storage")
    if not storage:
        return False, None, None, no_video_retries
    webdav_folder = _resolver._storage_to_webdav_path(storage)
    outcome, stream_url, stream_headers = _classify_completed_video(
        webdav_folder, title, monitor, settings_getter, download_size
    )
    if outcome == "stub":
        return False, None, None, no_video_retries
    if outcome == "available":
        return True, stream_url, stream_headers, no_video_retries

    # A truthy "unavailable" outcome means the happy-path return above was
    # skipped *because the body probe rejected a servable file*. Track that so
    # the exhaustion dialog explains the real failure (incomplete articles)
    # instead of misdirecting the user to WebDAV settings.
    body_unavailable = outcome == "unavailable"
    return _advance_no_video_retry(
        title,
        storage,
        webdav_folder,
        no_video_retries,
        max_no_video_retries,
        body_unavailable,
    )


def _discover_completed_video(
    webdav_folder, title, monitor, settings_getter, download_size
):
    """Run WebDAV discovery for a completed folder, returning the stream tuple."""
    # Thread the single-file advertised-size floor into discovery so a root-level
    # job-start stub recurses into the subfolder holding the real file instead of
    # being re-picked on every poll until download_timeout (#282 follow-up D).
    # 0 for packs / unknown size, so those paths are unchanged. The accept-time
    # guard shares the same floor via _stub_min_size_floor.
    min_video_size = _stub_min_size_floor(download_size, title)
    return _resolver._find_completed_video_stream_with_rechecks(
        webdav_folder,
        monitor=monitor,
        settings_getter=settings_getter,
        title_hint=title,
        min_video_size=min_video_size,
    )


def _classify_completed_video(
    webdav_folder, title, monitor, settings_getter, download_size
):
    """Discover the completed video and classify it for the poll loop.

    Returns ``(outcome, stream_url, stream_headers)`` where ``outcome`` is
    ``"stub"`` (job-start placeholder, keep polling), ``"available"`` (servable,
    play it), ``"unavailable"`` (Completed but mid-file body missing), or
    ``"missing"`` (no video discovered yet).
    """
    video_path, stream_url, stream_headers = _discover_completed_video(
        webdav_folder, title, monitor, settings_getter, download_size
    )
    if not video_path:
        return "missing", None, None
    # #282/#340: reject nzbdav's job-start stub before the body probe. A
    # single-file release whose discovered video is far below the advertised
    # size is the placeholder .mp4 nzbdav writes at job start, not the feature.
    # Return "stub" WITHOUT touching the no-video retry counter (the poll loop's
    # download_timeout is the stop authority), so the stub never plays and a
    # later genuine symlink-visibility gap keeps its retries. Packs / unknown
    # sizes are skipped inside _discovered_video_is_stub.
    if _discovered_video_is_stub(video_path, download_size, title):
        _resolver.xbmc.log(
            "NZB-DAV: '{}' discovered video '{}' is far smaller than the "
            "advertised release size; treating as nzbdav job-start stub and "
            "awaiting the real download".format(title, video_path),
            _resolver.xbmc.LOGWARNING,
        )
        return "stub", None, None
    if _resolver._completed_stream_body_available(stream_url, stream_headers):
        _resolver.xbmc.log(
            "NZB-DAV: File available, streaming '{}' via WebDAV".format(video_path),
            _resolver.xbmc.LOGINFO,
        )
        return "available", stream_url, stream_headers
    # nzbdav reports Completed and the container resolves, but the mid-file body
    # is unavailable (missing/unretained articles). Handing this to Kodi plays
    # an empty stream that EOFs the instant the demuxer reaches the body — the
    # missing-articles crash this guard prevents, mirroring the pre-submit
    # _completed_job_stream probe. Caller falls through to the retry budget.
    _resolver.xbmc.log(
        "NZB-DAV: '{}' is marked Completed but its mid-file body is "
        "unavailable; awaiting download instead of streaming".format(title),
        _resolver.xbmc.LOGWARNING,
    )
    return "unavailable", None, None


def _advance_no_video_retry(
    title,
    storage,
    webdav_folder,
    no_video_retries,
    max_no_video_retries,
    body_unavailable,
):
    """Bump the no-video retry counter, surfacing exhaustion when budget runs out."""
    no_video_retries += 1
    if no_video_retries >= max_no_video_retries:
        _report_no_video_exhaustion(
            title, storage, webdav_folder, no_video_retries, body_unavailable
        )
        return True, None, None, no_video_retries

    _resolver.xbmc.log(
        "NZB-DAV: Completed but no video found at '{}', "
        "retry {}/{} (storage='{}')...".format(
            webdav_folder,
            no_video_retries,
            max_no_video_retries,
            storage,
        ),
        _resolver.xbmc.LOGWARNING,
    )
    return False, None, None, no_video_retries


def _handle_webdav_error(nzo_id, webdav_error):
    """Handle terminal WebDAV auth failures and retryable server errors."""
    if webdav_error == "auth_failed":
        _resolver.xbmc.log(
            "NZB-DAV: WebDAV authentication failed for nzo_id={}. "
            "Check WebDAV username and password in addon settings.".format(nzo_id),
            _resolver.xbmc.LOGERROR,
        )
        _resolver.xbmcgui.Dialog().ok(
            _resolver._addon_name(),
            _resolver._string(_resolver._ERROR_MESSAGES["auth_failed"]),
        )
        return True

    if webdav_error == "server_error":
        _resolver.xbmc.log(
            "NZB-DAV: WebDAV server error, will retry on next poll",
            _resolver.xbmc.LOGWARNING,
        )
    return False


def _handle_resolve_exception(label, error, handle=None):
    """Log and surface a non-fatal resolve error to Kodi."""
    from resources.lib.http_util import redact_text

    message = redact_text(str(error))
    _resolver._resolve_stage("handle_resolve_exception {} {}".format(label, message))
    _resolver.xbmc.log(
        "NZB-DAV: Unexpected error in {}: {}".format(label, message),
        _resolver.xbmc.LOGERROR,
    )
    _resolver.xbmcgui.Dialog().ok(_resolver._addon_name(), "Error: {}".format(message))
    if handle is not None:
        _resolver.xbmcplugin.setResolvedUrl(handle, False, _resolver.xbmcgui.ListItem())
        _resolver.xbmc.PlayList(_resolver.xbmc.PLAYLIST_VIDEO).clear()
