# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=too-many-arguments,too-many-positional-arguments
# ^ 9-14-arg test signatures come from stacked @patch decorators; scheduled for
#   fixture consolidation in the complexity-reduction Phase C1 wave, after
#   which this module-level disable comes off.

import sys
import threading
import time as _time
from unittest.mock import ANY, MagicMock, call, patch

import pytest
from resources.lib.resolver import (
    _DOWNLOAD_TIMEOUT_MAX,
    _DOWNLOAD_TIMEOUT_MIN,
    _FALLBACK_PREWARM_DELAY_SECONDS,
    _POLL_INTERVAL_MAX,
    _POLL_INTERVAL_MIN,
    MAX_POLL_ITERATIONS,
    PollContext,
    _cache_bust_url,
    _clear_kodi_playback_state,
    _completed_job_stream,
    _direct_playback_service_config,
    _existing_completed_stream,
    _fallback_submit_jobs_snapshot,
    _finish_direct_playback,
    _finish_player_playback,
    _get_fallback_submit_delay_seconds,
    _get_poll_settings,
    _get_submit_timeout_seconds,
    _handle_history_result,
    _handle_job_status,
    _handle_resolve_exception,
    _kodi_video_db_version,
    _locate_kodi_video_db,
    _make_playable_listitem,
    _maybe_clear_queue_before_submit,
    _play_direct,
    _play_via_proxy,
    _poll_once,
    _poll_until_ready,
    _prefetch_fallback_candidate_loader,
    _prepare_direct_playback_with_service_config,
    _preserve_resume_on_cancel,
    _resolve_resume_choice,
    _resume_params_with_title,
    _show_submit_error_dialog,
    _start_direct_playback_prepare,
    _start_fallback_submit_worker,
    _stop_fallback_submit_worker,
    _storage_to_webdav_path,
    _submit_nzb_with_retries,
    _submit_nzb_with_ui_pump,
    _validate_stream_url,
    _wait_direct_playback_prepare,
    resolve,
    resolve_and_play,
)


@pytest.fixture(autouse=True)
def _no_resume_store_disk_writes():
    """Keep resolver tests hermetic.

    The real cancel path calls ``_preserve_resume_on_cancel`` ->
    ``resume_store.save_resume`` with the default path, which resolves through
    the mocked ``xbmcvfs.translatePath`` to a constant ``MagicMock/...`` path
    and writes ``resume.json`` there -- the same location ``cache`` later uses
    as its base dir, so the stray file makes ``cache.set_cached`` fail with
    ``NotADirectoryError`` in unrelated tests (e.g. test_router). The
    ``_preserve_resume_on_cancel`` behavior is asserted directly in its own
    unit tests, which mock ``resume_store`` explicitly.
    """
    with patch("resources.lib.resolver.resume_store.save_resume"), patch(
        "resources.lib.resolver.resume_store.clear_resume"
    ):
        yield


def _make_monitor(abort_after=None):
    """Make a mock xbmc.Monitor. Returns False until abort_after calls, then True."""
    monitor = MagicMock()
    if abort_after is None:
        monitor.waitForAbort.return_value = False
    else:
        side_effects = [False] * abort_after + [True]
        monitor.waitForAbort.side_effect = side_effects
    return monitor


# --- _storage_to_webdav_path tests ---


def test_max_poll_iterations_covers_max_timeout_at_min_interval():
    assert MAX_POLL_ITERATIONS >= _DOWNLOAD_TIMEOUT_MAX // _POLL_INTERVAL_MIN


def test_storage_to_webdav_path_standard():
    """Standard storage path converts to /content/ WebDAV path."""
    result = _storage_to_webdav_path(
        "/mnt/nzbdav/completed-symlinks/uncategorized/Send Help 2026 1080p"
    )
    assert result == "/content/uncategorized/Send Help 2026 1080p/"


def test_storage_to_webdav_path_mnt_data_variant():
    """Storage path with /mnt/data prefix also converts to /content/ WebDAV path."""
    result = _storage_to_webdav_path(
        "/mnt/data/completed-symlinks/uncategorized/Send Help 2026 1080p"
    )
    assert result == "/content/uncategorized/Send Help 2026 1080p/"


def test_storage_to_webdav_path_different_category():
    """Storage path with non-uncategorized category converts correctly."""
    result = _storage_to_webdav_path(
        "/mnt/nzbdav/completed-symlinks/movies/The Matrix 1999"
    )
    assert result == "/content/movies/The Matrix 1999/"


def test_storage_to_webdav_path_fallback():
    """Fallback for non-standard storage path uses last two components."""
    result = _storage_to_webdav_path("/some/other/path/category/name")
    assert result == "/content/category/name/"


def test_storage_to_webdav_path_trailing_slash():
    """Storage path with trailing slash is handled correctly."""
    result = _storage_to_webdav_path(
        "/mnt/nzbdav/completed-symlinks/uncategorized/Movie Name/"
    )
    assert result == "/content/uncategorized/Movie Name//"


def test_storage_to_webdav_path_nzbdav_rs_passthrough():
    """nzbdav-rs returns the WebDAV path directly — pass through with
    a trailing slash; do NOT re-root it under /content/ a second time."""
    result = _storage_to_webdav_path("/content/uncategorized/Movie Name")
    assert result == "/content/uncategorized/Movie Name/"


def test_storage_to_webdav_path_nzbdav_rs_passthrough_no_category():
    """nzbdav-rs with no category: storage is /content/Name/. The prior
    fallback-by-last-two-components would have produced
    /content/content/Name/ — the passthrough branch must win first."""
    result = _storage_to_webdav_path("/content/Movie Name/")
    assert result == "/content/Movie Name/"


@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmc")
def test_make_playable_listitem_redacts_logged_play_url(mock_xbmc, mock_gui):
    _make_playable_listitem(
        "http://webdav/movie.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )

    logged = mock_xbmc.log.call_args[0][0]
    assert "Basic dXNlcjpwYXNz" not in logged
    assert "redacted" in logged.lower()


@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmc")
def test_make_playable_listitem_detects_mime_with_fragment(mock_xbmc, mock_gui):
    """Mime detection must ignore ?query and #fragment on the URL."""
    mock_li = MagicMock()
    mock_gui.ListItem.return_value = mock_li

    _make_playable_listitem("http://webdav/movie.mkv#nzbdav_play=123", {})
    mock_li.setMimeType.assert_called_with("video/x-matroska")

    mock_li.reset_mock()
    _make_playable_listitem("http://webdav/movie.mp4?foo=bar", {})
    mock_li.setMimeType.assert_called_with("video/mp4")


@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmc")
def test_make_playable_listitem_detects_mpegts_mime(mock_xbmc, mock_gui):
    # Raw Blu-ray stream files (.m2ts) and .ts must not fall through to the
    # MKV default -- that mime mismatch confuses Kodi's demuxer selection.
    mock_li = MagicMock()
    mock_gui.ListItem.return_value = mock_li

    _make_playable_listitem("http://webdav/00000.m2ts", {})
    mock_li.setMimeType.assert_called_with("video/mp2t")

    mock_li.reset_mock()
    _make_playable_listitem("http://webdav/movie.ts", {})
    mock_li.setMimeType.assert_called_with("video/mp2t")


@patch("urllib.request.urlopen")
def test_validate_stream_url_catches_http_protocol_exception(mock_urlopen):
    from http.client import BadStatusLine

    mock_urlopen.side_effect = BadStatusLine("bad status line")

    assert _validate_stream_url("http://webdav/movie.mkv", {}) is False


def test_cache_bust_url_appends_query_param_and_is_unique():
    """Each call should produce a distinct query param so Kodi sees a new URL."""
    import time

    a = _cache_bust_url("http://webdav/movie.mkv")
    time.sleep(0.002)
    b = _cache_bust_url("http://webdav/movie.mkv")

    assert a.startswith("http://webdav/movie.mkv?nzbdav_play=")
    assert b.startswith("http://webdav/movie.mkv?nzbdav_play=")
    assert a != b


def test_cache_bust_url_preserves_existing_query():
    """If the URL already has a query string, append with &."""
    out = _cache_bust_url("http://webdav/movie.mkv?foo=bar")
    assert "?foo=bar&nzbdav_play=" in out


@patch("resources.lib.resolver.xbmc")
def test_get_poll_settings_clamps_too_low_and_logs(mock_xbmc):
    mock_addon = MagicMock()

    def get_setting(key):
        return {
            "poll_interval": "0",
            "download_timeout": "1",
        }[key]

    mock_addon.getSetting.side_effect = get_setting
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        assert _get_poll_settings() == (_POLL_INTERVAL_MIN, _DOWNLOAD_TIMEOUT_MIN)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "poll_interval" in logged
    assert "download_timeout" in logged


@patch("resources.lib.resolver.xbmc")
def test_get_poll_settings_clamps_typo_high_and_logs(mock_xbmc):
    mock_addon = MagicMock()

    def get_setting(key):
        return {
            "poll_interval": "6000",
            "download_timeout": "999999",
        }[key]

    mock_addon.getSetting.side_effect = get_setting
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        assert _get_poll_settings() == (_POLL_INTERVAL_MAX, _DOWNLOAD_TIMEOUT_MAX)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original

    logged = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "poll_interval" in logged
    assert "download_timeout" in logged


def test_get_poll_settings_uses_requested_defaults_for_empty_settings():
    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = lambda _key: ""
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        assert _get_poll_settings() == (1, 3600)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original


def test_get_poll_settings_uses_defaults_when_kodi_setting_raises_runtime():
    mock_addon = MagicMock()
    mock_addon.getSetting.side_effect = RuntimeError("Kodi settings unavailable")
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        assert _get_poll_settings() == (1, 3600)
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original


def test_get_poll_settings_uses_settings_getter_without_kodi_addon():
    original_addon = sys.modules["xbmcaddon"].Addon
    sys.modules["xbmcaddon"].Addon = MagicMock(
        side_effect=RuntimeError("Kodi settings unavailable")
    )

    def settings_getter(key, default=""):
        return {"poll_interval": "2", "download_timeout": "120"}.get(key, default)

    try:
        assert _get_poll_settings(settings_getter=settings_getter) == (2, 120)
    finally:
        sys.modules["xbmcaddon"].Addon = original_addon


def test_get_submit_timeout_seconds_uses_requested_default_for_empty_setting():
    mock_addon = MagicMock()
    mock_addon.getSetting.return_value = ""
    original = sys.modules["xbmcaddon"].Addon.return_value
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon
    try:
        assert _get_submit_timeout_seconds() == 300
    finally:
        sys.modules["xbmcaddon"].Addon.return_value = original


def test_get_fallback_submit_delay_seconds_defaults_to_prewarm_constant():
    """An empty setting falls back to the documented 120s prewarm default."""

    def settings_getter(_key, default=""):
        return ""

    assert (
        _get_fallback_submit_delay_seconds(settings_getter=settings_getter)
        == _FALLBACK_PREWARM_DELAY_SECONDS
    )


def test_get_fallback_submit_delay_seconds_uses_configured_value():
    """A user-configured defer is honored verbatim (the requirement)."""

    def settings_getter(key, default=""):
        return "45" if key == "fallback_submit_delay" else default

    assert _get_fallback_submit_delay_seconds(settings_getter=settings_getter) == 45


def test_get_fallback_submit_delay_seconds_allows_zero():
    """Zero means submit right at playback start — a valid configuration."""

    def settings_getter(key, default=""):
        return "0" if key == "fallback_submit_delay" else default

    assert _get_fallback_submit_delay_seconds(settings_getter=settings_getter) == 0


def test_get_fallback_submit_delay_seconds_rejects_garbage_and_negatives():
    """Non-numeric or negative values fall back to the safe default."""

    def garbage(key, default=""):
        return "soon" if key == "fallback_submit_delay" else default

    def negative(key, default=""):
        return "-30" if key == "fallback_submit_delay" else default

    assert (
        _get_fallback_submit_delay_seconds(settings_getter=garbage)
        == _FALLBACK_PREWARM_DELAY_SECONDS
    )
    assert (
        _get_fallback_submit_delay_seconds(settings_getter=negative)
        == _FALLBACK_PREWARM_DELAY_SECONDS
    )


def test_direct_playback_service_config_reads_proxy_window_once_for_fast_start():
    """Proxy port/token lookup should not duplicate Kodi window access."""

    window_calls = []
    home_window = MagicMock()
    home_window.getProperty.side_effect = lambda key: {
        "nzbdav.proxy_port": "57800",
        "nzbdav.proxy_token": "secret-token",
    }.get(key, "")

    def slow_window(_window_id):
        window_calls.append(_time.perf_counter())
        _time.sleep(0.06)
        return home_window

    with patch.object(sys.modules["xbmcgui"], "Window", side_effect=slow_window):
        started = _time.perf_counter()
        service_port, prepare_token = _direct_playback_service_config()
        elapsed = _time.perf_counter() - started

    assert (service_port, prepare_token) == (57800, "secret-token")
    assert len(window_calls) == 1
    assert elapsed < 0.5, "proxy config lookup took {:.3f}s".format(elapsed)


def test_handle_job_status_accepts_fractional_percentage():
    dialog = MagicMock()
    dialog.iscanceled.return_value = False

    should_stop, last_status = _handle_job_status(
        {"status": "Downloading", "percentage": "45.5"},
        "nzo_fractional",
        dialog,
        None,
    )

    assert should_stop is False
    assert last_status == "Downloading"
    for _ in range(20):
        if dialog.update.call_args_list:
            break
        _time.sleep(0.01)
    dialog.update.assert_called_once()
    assert dialog.update.call_args[0][0] == 45


def test_handle_job_status_does_not_block_on_stuck_dialog_update():
    update_started = threading.Event()
    release_update = threading.Event()
    update_finished = threading.Event()

    def stuck_update(*_args):
        update_started.set()
        release_update.wait(5)
        update_finished.set()

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    dialog.update.side_effect = stuck_update

    try:
        should_stop, last_status = _handle_job_status(
            {"status": "Downloading", "percentage": "99"},
            "nzo_stuck_dialog",
            dialog,
            None,
        )

        # The call returns while the (blocked) dialog.update is still in flight:
        # proves the update was dispatched to a background thread, not awaited.
        assert update_started.wait(1.0)
        assert not update_finished.is_set()
        assert should_stop is False
        assert last_status == "Downloading"
    finally:
        release_update.set()
        update_finished.wait(1.0)


@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.find_completed_by_name")
def test_existing_completed_stream_ignores_partial_history_row(
    mock_find_completed, mock_find_video
):
    mock_find_completed.return_value = {"status": "Completed"}

    assert _existing_completed_stream("movie.mkv") is None
    mock_find_video.assert_not_called()


def test_existing_completed_stream_rejects_context_with_legacy_options():
    from resources.lib.resolver_completed import CompletedStreamContext

    context = CompletedStreamContext(completed_job_lookup_done=True)
    with pytest.raises(TypeError, match="context.*options"):
        _existing_completed_stream(
            "movie.mkv", context=context, completed_job_lookup_done=False
        )


def _probe_response(content_length=None, code=206, body=b"\x00"):
    """Mock urllib response for the completed-stream body probe (HEAD/GET)."""
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.getcode.return_value = code
    hdrs = {}
    if content_length is not None:
        hdrs["Content-Length"] = str(content_length)
    resp.headers.get = MagicMock(side_effect=lambda k, d=None: hdrs.get(k, d))
    resp.read = MagicMock(return_value=body)
    return resp


_COMPLETED_JOB = {
    "status": "Completed",
    "name": "movie.mkv",
    "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
}


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_passes_settings_getter_to_webdav_lookup(mock_find_stream):
    def settings_getter(_key, default=""):
        return default

    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    # Body probe sees a large, fully-served file (HEAD length + mid-file bytes).
    with patch(
        "urllib.request.urlopen",
        return_value=_probe_response(content_length=85_000_000, body=b"\x00"),
    ):
        stream = _completed_job_stream(
            "movie.mkv",
            _COMPLETED_JOB,
            settings_getter=settings_getter,
        )

    assert stream == ("http://webdav/movie.mkv", {"Authorization": "Basic x"})
    mock_find_stream.assert_called_once_with(
        "/content/uncategorized/movie/",
        settings_getter=settings_getter,
        title_hint="movie.mkv",
        min_video_size=0,  # no download_size -> floor disabled
    )


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_threads_title_as_episode_hint(mock_find_stream):
    """The requested release title is threaded into webdav as ``title_hint`` so a
    multi-episode pack returns the requested episode, not the largest file."""
    mock_find_stream.return_value = (
        "/content/uncategorized/Show/Show.S02E05.mkv",
        "http://webdav/Show.S02E05.mkv",
        {"Authorization": "Basic x"},
    )

    job = {
        "status": "Completed",
        "name": "Show.S02E05.1080p.WEB-DL",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Show",
    }
    with patch(
        "urllib.request.urlopen",
        return_value=_probe_response(content_length=85_000_000, body=b"\x00"),
    ):
        _completed_job_stream("Show.S02E05.1080p.WEB-DL", job)

    assert mock_find_stream.call_args.kwargs["title_hint"] == "Show.S02E05.1080p.WEB-DL"


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_threads_explicit_episode_request(mock_find_stream):
    mock_find_stream.return_value = (
        "/content/uncategorized/Show/Show.S01E01.mkv",
        "http://webdav/Show.S01E01.mkv",
        {},
    )
    job = {
        "status": "Completed",
        "name": "Show.S01.2160p.WEB-DL",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Show",
    }

    with patch(
        "resources.lib.resolver._completed_stream_body_available", return_value=True
    ):
        _completed_job_stream("Show.S01.2160p.WEB-DL", job, requested_episode=(1, 1))

    assert mock_find_stream.call_args.kwargs["requested_episode"] == (1, 1)


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_preserves_full_episode_context(mock_find_stream):
    episode_context = {
        "type": "episode",
        "title": "Show",
        "imdb": "tt1234567",
        "tvdb": "7654",
        "tmdb_id": "987",
        "season": 1,
        "episode": 1,
    }
    mock_find_stream.return_value = (
        "/content/Show/Show.S01E01.mkv",
        "http://webdav/Show.S01E01.mkv",
        {},
    )
    job = {
        "status": "Completed",
        "name": "Show.S01.2160p.WEB-DL",
        "storage": "/mnt/data/completed-symlinks/tv/Show",
    }

    with patch(
        "resources.lib.resolver._completed_stream_body_available", return_value=True
    ):
        _completed_job_stream(
            "Show.S01.2160p.WEB-DL", job, episode_context=episode_context
        )

    assert mock_find_stream.call_args.kwargs["episode_context"] == episode_context


@patch("resources.lib.resolver_completed._delegated_find_video_stream_for_folder")
def test_webdav_boundary_converts_full_context_to_requested_episode(mock_delegated):
    from resources.lib.resolver import _find_video_stream_for_folder

    mock_delegated.return_value = (
        "/content/Show/Show.S01E01.mkv",
        "http://webdav/Show.S01E01.mkv",
        {},
    )
    episode_context = {
        "type": "episode",
        "title": "Show",
        "season": 1,
        "episode": 1,
    }

    _find_video_stream_for_folder("/content/Show/", episode_context=episode_context)

    assert mock_delegated.call_args.kwargs["requested_episode"] == (1, 1)


def test_resolve_and_play_episode_threads_request_through_poll_context():
    episode_context = {
        "type": "episode",
        "title": "Show",
        "imdb": "tt1234567",
        "tvdb": "7654",
        "tmdb_id": "987",
        "season": 1,
        "episode": 1,
    }
    params = {
        "_episode_context": episode_context,
        "_completed_job_lookup_done": True,
    }
    with patch("resources.lib.resolver._nzbget_enabled", return_value=False), patch(
        "resources.lib.resolver._picker_completed_stream", return_value=None
    ) as picker, patch(
        "resources.lib.resolver._resolve_and_play_submit_and_poll",
        return_value=(None, None, None),
    ) as submit_poll, patch(
        "resources.lib.resolver._stop_fallback_submit_worker"
    ):
        resolve_and_play("http://i/pack.nzb", "Show.S01.2160p", params=params)

    assert picker.call_args.kwargs["episode_context"] == episode_context
    poll_ctx = submit_poll.call_args.args[-1]
    assert poll_ctx.episode_context == episode_context


def test_resolve_side_effects_threads_full_context_into_fallback_worker():
    from resources.lib.resolver_entry import _ResolveSideEffects

    episode_context = {
        "type": "episode",
        "title": "Show",
        "imdb": "tt1234567",
        "tvdb": "7654",
        "tmdb_id": "987",
        "season": 1,
        "episode": 1,
    }
    effects = _ResolveSideEffects(
        {"_episode_context": episode_context},
        [],
        None,
        "http://i/primary.nzb",
        MagicMock(),
    )

    with patch("resources.lib.resolver._start_playback_state_cleanup"), patch(
        "resources.lib.resolver._get_fallback_submit_delay_seconds", return_value=0
    ), patch(
        "resources.lib.resolver._start_fallback_submit_worker", return_value={}
    ) as start:
        effects.start_fallback_after_primary("nzo-primary")

    assert start.call_args.kwargs["episode_context"] == episode_context


@patch("resources.lib.resolver._handle_history_result")
@patch("resources.lib.resolver._handle_job_status", return_value=(False, None))
@patch("resources.lib.resolver._poll_once", return_value=({}, {}, None))
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value="nzo-1")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_keeps_episode_request_for_completed_fallback(
    mock_xbmc,
    _mock_existing,
    _mock_submit,
    _mock_poll,
    _mock_status,
    mock_history,
):
    """Every completed history candidate is resolved for the same episode."""
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_history.return_value = (True, None, None, 0)

    episode_context = {
        "type": "episode",
        "title": "Show",
        "imdb": "tt1234567",
        "tvdb": "7654",
        "tmdb_id": "987",
        "season": 1,
        "episode": 1,
    }
    _poll_until_ready(
        "http://i/pack.nzb",
        "Show.S01.2160p",
        _make_dialog(),
        1,
        60,
        poll_ctx=PollContext(episode_context=episode_context),
    )

    assert mock_history.call_args.kwargs["episode_context"] == episode_context


def test_completed_history_records_pack_under_exact_history_nzo_id():
    from resources.lib.episode_inventory import build_video_inventory

    episode_context = {
        "type": "episode",
        "title": "Spider-Noir",
        "imdb": "tt123",
        "tvdb": "456",
        "tmdb_id": "789",
        "season": 1,
        "episode": 1,
    }
    inventory = build_video_inventory(
        [
            ("/content/tv/exact/Spider-Noir.S01E01.mkv", 6000),
            ("/content/tv/exact/Spider-Noir.S01E02.mkv", 7000),
        ],
        requested=(1, 1),
    )
    history = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/tv/exact",
        "name": "Spider-Noir.S01.2160p",
        "nzo_id": "SABnzbd_nzo_exact",
    }

    def discover(_folder, **kwargs):
        kwargs["on_inventory"](inventory)
        return (
            "/content/tv/exact/Spider-Noir.S01E01.mkv",
            "http://webdav/content/tv/exact/Spider-Noir.S01E01.mkv",
            {},
        )

    with patch(
        "resources.lib.resolver._find_completed_video_stream_with_rechecks",
        side_effect=discover,
    ), patch(
        "resources.lib.resolver._discovered_video_is_stub", return_value=False
    ), patch(
        "resources.lib.resolver._completed_stream_body_available", return_value=True
    ), patch(
        "resources.lib.season_pack_recording.season_pack.upsert"
    ) as upsert:
        result = _handle_history_result(
            history,
            "Spider-Noir.S01.2160p",
            0,
            5,
            episode_context=episode_context,
        )

    assert result[:3] == (
        True,
        "http://webdav/content/tv/exact/Spider-Noir.S01E01.mkv",
        {},
    )
    record = upsert.call_args.args[0]
    assert (record["backend"], record["job_id"], record["job_name"]) == (
        "nzbdav",
        "SABnzbd_nzo_exact",
        "Spider-Noir.S01.2160p",
    )
    assert record["folder"] == history["storage"]


def test_completed_history_catalog_write_failure_does_not_block_playback():
    from resources.lib.episode_inventory import build_video_inventory

    context = {
        "type": "episode",
        "title": "Show",
        "season": 1,
        "episode": 1,
    }
    inventory = build_video_inventory(
        [("/p/Show.S01E01.mkv", 1), ("/p/Show.S01E02.mkv", 2)],
        requested=(1, 1),
    )

    def discover(_folder, **kwargs):
        kwargs["on_inventory"](inventory)
        return "/p/Show.S01E01.mkv", "http://webdav/p/Show.S01E01.mkv", {}

    with patch(
        "resources.lib.resolver._find_completed_video_stream_with_rechecks",
        side_effect=discover,
    ), patch(
        "resources.lib.resolver._discovered_video_is_stub", return_value=False
    ), patch(
        "resources.lib.resolver._completed_stream_body_available", return_value=True
    ), patch(
        "resources.lib.season_pack_recording.season_pack.upsert",
        side_effect=OSError("disk full"),
    ):
        result = _handle_history_result(
            {
                "status": "Completed",
                "storage": "/mnt/nzbdav/completed-symlinks/tv/pack",
                "name": "Show.S01",
                "nzo_id": "exact",
            },
            "Show.S01",
            0,
            5,
            episode_context=context,
        )

    assert result[1] == "http://webdav/p/Show.S01E01.mkv"


def test_completed_history_does_not_record_pack_until_body_is_available():
    from resources.lib.episode_inventory import build_video_inventory

    context = {
        "type": "episode",
        "title": "Show",
        "season": 1,
        "episode": 1,
    }
    inventory = build_video_inventory(
        [("/p/Show.S01E01.mkv", 1), ("/p/Show.S01E02.mkv", 2)],
        requested=(1, 1),
    )

    def discover(_folder, **kwargs):
        kwargs["on_inventory"](inventory)
        return "/p/Show.S01E01.mkv", "http://webdav/p/Show.S01E01.mkv", {}

    with patch(
        "resources.lib.resolver._find_completed_video_stream_with_rechecks",
        side_effect=discover,
    ), patch(
        "resources.lib.resolver._discovered_video_is_stub", return_value=False
    ), patch(
        "resources.lib.resolver._completed_stream_body_available", return_value=False
    ), patch(
        "resources.lib.season_pack_recording.season_pack.upsert"
    ) as upsert:
        result = _handle_history_result(
            {
                "status": "Completed",
                "storage": "/mnt/nzbdav/completed-symlinks/tv/pack",
                "name": "Show.S01",
                "nzo_id": "exact",
            },
            "Show.S01",
            0,
            5,
            episode_context=context,
        )

    assert result[1] is None
    upsert.assert_not_called()


def test_real_nzbdav_remapped_poll_history_id_reaches_pack_recorder():
    import json

    from resources.lib.episode_inventory import build_video_inventory

    context = {
        "type": "episode",
        "title": "Show",
        "tvdb": "123",
        "season": 1,
        "episode": 1,
    }
    inventory = build_video_inventory(
        [("/p/Show.S01E01.mkv", 1), ("/p/Show.S01E02.mkv", 2)],
        requested=(1, 1),
    )
    empty_api_body = json.dumps({"history": {"slots": []}})
    remapped_api_body = json.dumps(
        {
            "history": {
                "slots": [
                    {
                        "status": "Completed",
                        "storage": "/mnt/nzbdav/completed-symlinks/tv/exact",
                        "name": "Show.S01",
                        "nzo_id": "SABnzbd_nzo_history_remapped",
                        "completed": 1000,
                    }
                ]
            }
        }
    )

    def discover(_folder, **kwargs):
        kwargs["on_inventory"](inventory)
        return "/p/Show.S01E01.mkv", "http://webdav/p/Show.S01E01.mkv", {}

    def history_response(url, **_kwargs):
        return remapped_api_body if "search=Show" in url else empty_api_body

    with patch("resources.lib.resolver.get_job_status", return_value=None), patch(
        "resources.lib.nzbdav_api._get_settings",
        return_value=("http://nzbdav:3000", "key"),
    ), patch("resources.lib.nzbdav_api._http_get", side_effect=history_response), patch(
        "resources.lib.resolver._find_completed_video_stream_with_rechecks",
        side_effect=discover,
    ), patch(
        "resources.lib.resolver._discovered_video_is_stub", return_value=False
    ), patch(
        "resources.lib.resolver._completed_stream_body_available", return_value=True
    ), patch(
        "resources.lib.season_pack_recording.season_pack.upsert"
    ) as upsert:
        _job, history, error = _poll_once(
            "SABnzbd_nzo_submitted",
            "Show.S01",
            MagicMock(),
            submit_started_wall=1000,
        )
        result = _handle_history_result(
            history, "Show.S01", 0, 5, episode_context=context
        )

    assert error is None
    assert result[1] == "http://webdav/p/Show.S01E01.mkv"
    assert upsert.call_args.args[0]["job_id"] == "SABnzbd_nzo_history_remapped"


def test_existing_completed_body_rejection_does_not_record_discovered_inventory():
    from resources.lib.episode_inventory import build_video_inventory

    context = {
        "type": "episode",
        "title": "Show",
        "season": 1,
        "episode": 1,
    }
    inventory = build_video_inventory(
        [("/p/Show.S01E01.mkv", 1), ("/p/Show.S01E02.mkv", 2)],
        requested=(1, 1),
    )

    def discover(_folder, **kwargs):
        kwargs["on_inventory"](inventory)
        return "/p/Show.S01E01.mkv", "http://webdav/p/Show.S01E01.mkv", {}

    with patch(
        "resources.lib.resolver._find_video_stream_for_folder",
        side_effect=discover,
    ), patch(
        "resources.lib.resolver._discovered_video_is_stub", return_value=False
    ), patch(
        "resources.lib.resolver._completed_stream_body_available", return_value=False
    ), patch(
        "resources.lib.season_pack_recording.season_pack.upsert"
    ) as upsert:
        stream = _completed_job_stream(
            "Show.S01",
            {
                "status": "Completed",
                "storage": "/mnt/nzbdav/completed-symlinks/tv/pack",
                "name": "Show.S01",
                "nzo_id": "exact",
            },
            episode_context=context,
        )

    assert stream is None
    upsert.assert_not_called()


def test_existing_completed_records_backend_native_storage_after_validation():
    from resources.lib.episode_inventory import build_video_inventory

    context = {
        "type": "episode",
        "title": "Show",
        "season": 1,
        "episode": 1,
    }
    inventory = build_video_inventory(
        [("/p/Show.S01E01.mkv", 1), ("/p/Show.S01E02.mkv", 2)],
        requested=(1, 1),
    )
    completed = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/tv/pack",
        "name": "Show.S01",
        "nzo_id": "exact",
    }

    def discover(_folder, **kwargs):
        kwargs["on_inventory"](inventory)
        return "/p/Show.S01E01.mkv", "http://webdav/p/Show.S01E01.mkv", {}

    with patch(
        "resources.lib.resolver._find_video_stream_for_folder",
        side_effect=discover,
    ), patch(
        "resources.lib.resolver._discovered_video_is_stub", return_value=False
    ), patch(
        "resources.lib.resolver._completed_stream_body_available", return_value=True
    ), patch(
        "resources.lib.season_pack_recording.season_pack.upsert"
    ) as upsert:
        stream = _completed_job_stream("Show.S01", completed, episode_context=context)

    assert stream is not None
    assert upsert.call_args.args[0]["folder"] == completed["storage"]


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_rejects_when_midfile_body_unavailable(mock_find_stream):
    """nzbdav says Completed but the mid-file articles are gone (the
    Good/Bad/Ugly failure): the byte-0 header reads, but a mid-file range
    GET fails. The stream must be rejected (return None) so the resolver
    re-downloads instead of handing Kodi an empty stream that EOFs at once.
    """
    from urllib.error import HTTPError

    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )
    head = _probe_response(content_length=85_000_000)
    midfile_500 = HTTPError(
        "http://webdav/movie.mkv", 500, "Internal Server Error", {}, None
    )

    with patch("urllib.request.urlopen", side_effect=[head, midfile_500]):
        stream = _completed_job_stream("movie.mkv", _COMPLETED_JOB)

    assert stream is None


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_streams_when_midfile_body_available(mock_find_stream):
    """When the mid-file range GET returns real body bytes, stream directly."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )
    head = _probe_response(content_length=85_000_000)
    midfile_ok = _probe_response(code=206, body=b"\x00")

    with patch("urllib.request.urlopen", side_effect=[head, midfile_ok]):
        stream = _completed_job_stream("movie.mkv", _COMPLETED_JOB)

    assert stream == ("http://webdav/movie.mkv", {"Authorization": "Basic x"})


# ---------------------------------------------------------------------------
# #282 follow-up: the pre-submit cache-hit shortcuts (_completed_job_stream /
# _existing_completed_stream / _picker_completed_stream) must reject the same
# job-start stub the post-submit accept path does. A stale stub left in a
# Completed history row from a prior failed attempt -- whose tiny body IS
# available -- would otherwise pass the body probe and be served, bypassing the
# #282 guard. Same advertised-size sanity check, threaded via download_size /
# params["_download_size"]. Fails OPEN / pack-exempt exactly as the post-submit
# guard does (see the _handle_history_result tests below).
# ---------------------------------------------------------------------------


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=362_076_665)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_rejects_stub_far_below_advertised(
    mock_find_stream, mock_probe, _mock_size
):
    """A Completed row whose discovered video (362 MB) is a tiny fraction of the
    advertised size (~81 GB) is nzbdav's job-start stub. The pre-submit shortcut
    must reject it (return None) and record its nzo_id, even though its body IS
    available -- the body probe is never reached."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/UNDERTAKERS.mp4",
        "http://webdav/UNDERTAKERS.mp4",
        {"Authorization": "Basic x"},
    )
    rejected = set()
    job = {
        "status": "Completed",
        "name": "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "nzo_id": "stub_completed",
    }

    stream = _completed_job_stream(
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        job,
        rejected_completed_ids=rejected,
        download_size="81610612736",  # ~81 GB advertised
    )

    assert stream is None
    assert "stub_completed" in rejected
    mock_probe.assert_not_called()  # rejected on size before any body probe


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=80_000_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_streams_single_file_matching_advertised(
    mock_find_stream, mock_probe, _mock_size
):
    """A single-file release whose discovered video (~80 GB) is close to the
    advertised size (~81 GB) is the real feature and streams normally."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    stream = _completed_job_stream(
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        {
            "status": "Completed",
            "name": "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "good_completed",
        },
        download_size="81610612736",
    )

    assert stream == ("http://webdav/movie.mkv", {"Authorization": "Basic x"})


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=30_000_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_streams_pack_episode_below_advertised(
    mock_find_stream, mock_probe, _mock_size
):
    """A season pack's advertised size covers every episode, so a single picked
    episode (3 GB) far below the pack size (30 GB) must NOT be rejected as a
    stub -- the guard is skipped for packs."""
    mock_find_stream.return_value = (
        "/content/uncategorized/show/show.s01e03.mkv",
        "http://webdav/show.s01e03.mkv",
        {"Authorization": "Basic x"},
    )

    stream = _completed_job_stream(
        "Some.Show.S01.1080p.WEB-DL.x264-GROUP",  # whole-season pack
        {
            "status": "Completed",
            "name": "Some.Show.S01.1080p.WEB-DL.x264-GROUP",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/show",
            "nzo_id": "pack_completed",
        },
        download_size="30000000000",  # 30 GB whole pack
    )

    assert stream == ("http://webdav/show.s01e03.mkv", {"Authorization": "Basic x"})


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=1_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_streams_when_advertised_size_unknown(
    mock_find_stream, mock_probe, _mock_size
):
    """Fail OPEN: with no advertised size the guard cannot judge plausibility,
    so a tiny discovered file streams exactly as before (no regression)."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    stream = _completed_job_stream(
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        {
            "status": "Completed",
            "name": "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "no_advertised",
        },
        download_size=None,
    )

    assert stream == ("http://webdav/movie.mkv", {"Authorization": "Basic x"})


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=80_000_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_threads_stub_floor_into_discovery(
    mock_find_stream, _mock_probe, _mock_size
):
    """#282 follow-up D: the pre-submit cache-hit path also threads the
    advertised-size floor into discovery, so a stale Completed row whose stub
    sits at the release root recurses into the subfolder holding the real file
    instead of serving the stub. Only an unknown advertised size passes a 0
    floor (the floor is now pack-agnostic -- advertised*0.5 for any known size)."""
    from resources.lib.resolver import _STUB_VIDEO_MIN_ADVERTISED_FRACTION

    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    _completed_job_stream(
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        {
            "status": "Completed",
            "name": "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "good_completed",
        },
        download_size="81610612736",
    )

    assert mock_find_stream.call_args.kwargs["min_video_size"] == (
        81610612736 * _STUB_VIDEO_MIN_ADVERTISED_FRACTION
    )


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=30_000_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_threads_advertised_floor_for_pack(
    mock_find_stream, _mock_probe, _mock_size
):
    """PACK-AGNOSTIC: a pack threads the SAME advertised*0.5 floor into discovery
    as a single file (no more pack-zero special case). The accept guard compares
    the folder's total video bytes against it, so a real pack (episodes sum to
    ~advertised) still streams while a stub-only folder is rejected."""
    from resources.lib.resolver import _STUB_VIDEO_MIN_ADVERTISED_FRACTION

    mock_find_stream.return_value = (
        "/content/uncategorized/show/show.s01e03.mkv",
        "http://webdav/show.s01e03.mkv",
        {"Authorization": "Basic x"},
    )

    _completed_job_stream(
        "Some.Show.S01.1080p.WEB-DL.x264-GROUP",
        {
            "status": "Completed",
            "name": "Some.Show.S01.1080p.WEB-DL.x264-GROUP",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/show",
            "nzo_id": "pack_completed",
        },
        download_size="30000000000",
    )

    assert mock_find_stream.call_args.kwargs["min_video_size"] == (
        30000000000 * _STUB_VIDEO_MIN_ADVERTISED_FRACTION
    )


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=362_076_665)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_existing_completed_stream_rejects_stub_via_download_size(
    mock_find_stream, mock_probe, _mock_size
):
    """_existing_completed_stream threads download_size down to the stub guard,
    so a hinted Completed row that is actually the job-start stub is rejected
    before its body is probed."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/UNDERTAKERS.mp4",
        "http://webdav/UNDERTAKERS.mp4",
        {"Authorization": "Basic x"},
    )
    job = {
        "status": "Completed",
        "name": "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "nzo_id": "stub_hint",
    }

    stream = _existing_completed_stream(
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        completed_job_hint=job,
        completed_job_lookup_done=True,
        download_size="81610612736",
    )

    assert stream is None
    mock_probe.assert_not_called()


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=362_076_665)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_picker_completed_stream_rejects_stub_via_params_download_size(
    mock_find_stream, mock_probe, _mock_size
):
    """The picker pre-submit shortcut reads params['_download_size'] and rejects
    the job-start stub before the progress UI opens, recording its nzo_id so the
    submit/poll paths skip it too."""
    from resources.lib.resolver import _picker_completed_stream

    mock_find_stream.return_value = (
        "/content/uncategorized/movie/UNDERTAKERS.mp4",
        "http://webdav/UNDERTAKERS.mp4",
        {"Authorization": "Basic x"},
    )
    rejected = set()
    params = {
        "_completed_job": {
            "status": "Completed",
            "name": "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "stub_picker",
        },
        "_download_size": "81610612736",
    }

    stream = _picker_completed_stream(
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        params,
        rejected_completed_ids=rejected,
    )

    assert stream is None
    assert "stub_picker" in rejected
    mock_probe.assert_not_called()


def test_fallback_worker_append_invokes_on_append_hook():
    """When a live-push hook is armed, each job the worker adopts fires it so
    late-adopted fallbacks get pushed to the proxy session."""
    from resources.lib.resolver import _start_fallback_submit_worker

    captured = {}

    def fake_submit(
        cands,
        monitor,
        stop_event=None,
        on_job=None,
        settings_getter=None,
        dead=None,
        primary_nzb_url=None,
    ):
        captured["on_job"] = on_job

    with patch(
        "resources.lib.resolver._submit_fallback_candidates", side_effect=fake_submit
    ):
        state = _start_fallback_submit_worker(candidates=[{"nzb_url": "x"}])
        state["thread"].join(timeout=2)

    on_append = MagicMock()
    state["on_append"] = on_append
    captured["on_job"]({"nzo_id": "a", "job_name": "A"})

    on_append.assert_called_once()
    assert any(j.get("nzo_id") == "a" for j in state["jobs"])


def test_arm_live_fallback_push_sets_hook_and_flushes_current_jobs():
    """Arming installs the on_append hook and immediately pushes whatever the
    worker has already adopted (between the prepare snapshot and now)."""
    from resources.lib.resolver import _arm_live_fallback_push

    prepared = {
        "service_port": 8080,
        "proxy_url": "http://127.0.0.1:8080/stream/abc123",
    }
    fallback_state = {"lock": threading.Lock(), "jobs": [{"nzo_id": "a"}]}

    with patch(
        "resources.lib.resolver._direct_playback_service_config",
        return_value=(8080, "tok"),
    ), patch(
        "resources.lib.resolver._playback_fallback_sources_for_stream",
        return_value=[{"nzo_id": "a", "stream_url": "http://h/a.mkv"}],
    ), patch(
        "resources.lib.stream_proxy.update_stream_fallbacks_via_service"
    ) as mock_update:
        _arm_live_fallback_push(prepared, fallback_state, "http://primary/movie.mkv")

    assert callable(fallback_state.get("on_append"))
    mock_update.assert_called_once_with(
        8080,
        "abc123",
        [{"nzo_id": "a", "stream_url": "http://h/a.mkv"}],
        prepare_token="tok",
    )


def test_arm_live_fallback_push_swallows_update_errors():
    """A push failure (service slow/unreachable) must never propagate out of
    the resolver flow and break playback handoff."""
    from resources.lib.resolver import _arm_live_fallback_push

    prepared = {
        "service_port": 8080,
        "proxy_url": "http://127.0.0.1:8080/stream/abc123",
    }
    fallback_state = {"lock": threading.Lock(), "jobs": [{"nzo_id": "a"}]}

    with patch(
        "resources.lib.resolver._direct_playback_service_config",
        return_value=(8080, "tok"),
    ), patch(
        "resources.lib.resolver._playback_fallback_sources_for_stream",
        return_value=[{"nzo_id": "a", "stream_url": ""}],
    ), patch(
        "resources.lib.stream_proxy.update_stream_fallbacks_via_service",
        side_effect=OSError("service unreachable"),
    ):
        # Must not raise.
        _arm_live_fallback_push(prepared, fallback_state, "http://primary/movie.mkv")

    assert callable(fallback_state.get("on_append"))


def test_fallback_worker_append_swallows_on_append_errors():
    """A throwing on_append hook must not lose the job or break submission."""
    from resources.lib.resolver import _start_fallback_submit_worker

    captured = {}

    def fake_submit(
        cands,
        monitor,
        stop_event=None,
        on_job=None,
        settings_getter=None,
        dead=None,
        primary_nzb_url=None,
    ):
        captured["on_job"] = on_job

    with patch(
        "resources.lib.resolver._submit_fallback_candidates", side_effect=fake_submit
    ):
        state = _start_fallback_submit_worker(candidates=[{"nzb_url": "x"}])
        state["thread"].join(timeout=2)

    state["on_append"] = MagicMock(side_effect=RuntimeError("push boom"))
    # Must not raise despite the hook throwing.
    captured["on_job"]({"nzo_id": "a"})
    assert any(j.get("nzo_id") == "a" for j in state["jobs"])


def test_fallback_submit_jobs_snapshot_skips_wait_when_gated_on_playback_start():
    """A ``wait_for_playback`` worker gates ALL submission behind
    ``state["playback_started"]``, which the caller of this exact snapshot
    only sets afterward (see ``_signal_fallback_playback_started`` in
    ``_resolve_and_play_ready_stream``, called strictly later in the same
    synchronous flow). Waiting the full grace period here can never observe
    a job -- it was a guaranteed ~8s no-op stall before every completed-job
    fast-path handoff. Confirms the wait is skipped (0s) in that case."""
    from resources.lib.resolver import _fallback_submit_jobs_snapshot

    state = {
        "lock": threading.Lock(),
        "jobs": [],
        "thread": MagicMock(is_alive=MagicMock(return_value=True)),
        "finished": threading.Event(),
        "playback_started": threading.Event(),  # not set
        "wait_for_playback": True,
    }

    with patch(
        "resources.lib.resolver_fallback_jobs._await_fallback_worker_finish"
    ) as mock_wait:
        _fallback_submit_jobs_snapshot(state, wait_seconds=8.0)

    mock_wait.assert_called_once_with(state["thread"], state["finished"], 0)


def test_fallback_submit_jobs_snapshot_waits_when_not_gated_on_playback():
    """A worker that does NOT require ``wait_for_playback`` (the download
    submit-and-poll path) starts submitting immediately, so the full grace
    period genuinely lets it populate jobs -- must not be shortened."""
    from resources.lib.resolver import _fallback_submit_jobs_snapshot

    state = {
        "lock": threading.Lock(),
        "jobs": [],
        "thread": MagicMock(is_alive=MagicMock(return_value=True)),
        "finished": threading.Event(),
        "playback_started": threading.Event(),
        "wait_for_playback": False,
    }

    with patch(
        "resources.lib.resolver_fallback_jobs._await_fallback_worker_finish"
    ) as mock_wait:
        _fallback_submit_jobs_snapshot(state, wait_seconds=8.0)

    mock_wait.assert_called_once_with(state["thread"], state["finished"], 8.0)


def test_fallback_submit_jobs_snapshot_waits_full_once_playback_already_started():
    """Once playback has actually started, a ``wait_for_playback`` worker is
    unblocked and may be mid-submission -- the full wait is useful again."""
    from resources.lib.resolver import _fallback_submit_jobs_snapshot

    playback_started = threading.Event()
    playback_started.set()
    state = {
        "lock": threading.Lock(),
        "jobs": [],
        "thread": MagicMock(is_alive=MagicMock(return_value=True)),
        "finished": threading.Event(),
        "playback_started": playback_started,
        "wait_for_playback": True,
    }

    with patch(
        "resources.lib.resolver_fallback_jobs._await_fallback_worker_finish"
    ) as mock_wait:
        _fallback_submit_jobs_snapshot(state, wait_seconds=8.0)

    mock_wait.assert_called_once_with(state["thread"], state["finished"], 8.0)


def test_start_fallback_submit_worker_records_wait_for_playback_flag():
    """The snapshot skip above relies on this flag being recorded at
    creation time; guard against it silently disappearing in a refactor."""
    from resources.lib.resolver import _start_fallback_submit_worker

    with patch("resources.lib.resolver._submit_fallback_candidates"):
        state = _start_fallback_submit_worker(
            candidates=[{"nzb_url": "x"}], wait_for_playback=True
        )
        state["thread"].join(timeout=2)

    assert state["wait_for_playback"] is True


def test_arm_live_fallback_push_noops_without_service_port():
    from resources.lib.resolver import _arm_live_fallback_push

    fallback_state = {"lock": threading.Lock(), "jobs": []}
    # No service_port (direct/non-service playback) -> nothing to push to.
    _arm_live_fallback_push(
        {"proxy_url": ""}, fallback_state, "http://primary/movie.mkv"
    )
    assert "on_append" not in fallback_state


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_rejected_by_fault_env(mock_find_stream):
    """NZBDAV_FAULT_REJECT_COMPLETED forces the 'already downloaded' path to be
    rejected (return None -> re-download), so a live fallback cutover can be
    staged: the re-download path attaches validated fallback sources that the
    primary-fault hook can then cut over to. Inert unless the env var is set.
    """
    import os

    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    # No urlopen patch: the env var must short-circuit before any network probe.
    with patch.dict(os.environ, {"NZBDAV_FAULT_REJECT_COMPLETED": "1"}):
        stream = _completed_job_stream("movie.mkv", _COMPLETED_JOB)

    assert stream is None


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_fails_open_when_probe_inconclusive(mock_find_stream):
    """A timeout / network error during the probe is ambiguous, not proof of
    a bad file — fail open and stream rather than block a slow-but-valid file.
    """
    from urllib.error import URLError

    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    with patch("urllib.request.urlopen", side_effect=URLError("timed out")):
        stream = _completed_job_stream("movie.mkv", _COMPLETED_JOB)

    assert stream == ("http://webdav/movie.mkv", {"Authorization": "Basic x"})


@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmc")
def test_handle_resolve_exception_redacts_credentials_in_log_and_dialog(
    mock_xbmc, mock_gui
):
    error = RuntimeError(
        "failed URL http://nzbdav/api?apikey=supersecret&password=hunter2"
    )

    _handle_resolve_exception("resolve", error)

    dialog_text = mock_gui.Dialog.return_value.ok.call_args.args[1]
    log_text = "\n".join(call.args[0] for call in mock_xbmc.log.call_args_list)
    assert "supersecret" not in dialog_text
    assert "hunter2" not in dialog_text
    assert "supersecret" not in log_text
    assert "hunter2" not in log_text
    assert "apikey=REDACTED" in dialog_text


# --- proxy-routing tests ---
#
# MKV and other non-MP4 files must route through the local stream proxy, not
# play the WebDAV URL directly. If they go direct, Kodi 21 runs a PROPFIND
# scan of the parent directory before Open; nzbdav's WebDAV returns
# localhost:8080 hrefs which break Kodi's directory parser and cascade into
# an "Unhandled exception" on Open.


@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_port")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmc")
def test_play_direct_routes_mkv_through_proxy(
    mock_xbmc, mock_gui, mock_plugin, mock_get_port, mock_prepare
):
    """MKV files must go through the stream proxy, not direct WebDAV."""
    mock_get_port.return_value = 57800
    mock_prepare.return_value = (
        "http://127.0.0.1:57800/stream/abc",
        {"remux": False, "faststart": False, "direct": False},
    )

    _play_direct(
        1,
        "http://webdav:8080/content/movie/movie.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )

    mock_prepare.assert_called_once()
    args = mock_prepare.call_args[0]
    assert args[0] == 57800
    assert args[1] == "http://webdav:8080/content/movie/movie.mkv"
    mock_plugin.setResolvedUrl.assert_called_once()
    # ListItem must be constructed with the proxy URL, not the WebDAV URL.
    mock_gui.ListItem.assert_called_with(path="http://127.0.0.1:57800/stream/abc")


@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_port")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmc")
def test_play_direct_mkv_sets_matroska_mime_on_passthrough(
    mock_xbmc, mock_gui, mock_plugin, mock_get_port, mock_prepare
):
    """Pass-through proxy for MKV must advertise video/x-matroska to Kodi."""
    mock_get_port.return_value = 57800
    mock_prepare.return_value = (
        "http://127.0.0.1:57800/stream/abc",
        {"remux": False, "faststart": False, "direct": False},
    )
    listitem = MagicMock()
    mock_gui.ListItem.return_value = listitem

    _play_direct(1, "http://webdav:8080/content/movie/movie.mkv", None)

    listitem.setMimeType.assert_called_with("video/x-matroska")


@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_port")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmc")
def test_play_direct_hls_proxy_sets_playlist_mime(
    mock_xbmc, mock_gui, mock_plugin, mock_get_port, mock_prepare
):
    mock_get_port.return_value = 57800
    mock_prepare.return_value = (
        "http://127.0.0.1:57800/hls/abc/playlist.m3u8",
        {
            "remux": True,
            "faststart": False,
            "direct": False,
            "mode": "hls",
            "content_type": "application/vnd.apple.mpegurl",
        },
    )
    listitem = MagicMock()
    mock_gui.ListItem.return_value = listitem

    _play_direct(1, "http://webdav:8080/content/movie/movie.mkv", None)

    listitem.setMimeType.assert_called_with("application/vnd.apple.mpegurl")


def test_apply_proxy_mime_matroska_remux_still_sets_matroska():
    from resources.lib.resolver import _apply_proxy_mime

    li = MagicMock()
    li.getPath.return_value = "http://127.0.0.1:57800/stream/abc"
    stream_info = {"remux": True, "content_type": "video/x-matroska"}

    _apply_proxy_mime(li, "http://webdav/movie.mkv", stream_info)

    li.setMimeType.assert_called_with("video/x-matroska")


@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_port")
@patch("resources.lib.resolver.xbmc")
def test_play_via_proxy_routes_mkv_through_proxy(
    mock_xbmc, mock_get_port, mock_prepare
):
    """Service-side (resolve_and_play) path also routes MKV through proxy."""
    mock_get_port.return_value = 57800
    mock_prepare.return_value = (
        "http://127.0.0.1:57800/stream/abc",
        {"remux": False, "faststart": False, "direct": False},
    )
    player = MagicMock()
    mock_xbmc.Player.return_value = player

    with patch("resources.lib.resolver.xbmcgui"):
        _play_via_proxy("http://webdav:8080/content/movie/movie.mkv", None)

    mock_prepare.assert_called_once()
    # Player must be given the proxy URL, not the WebDAV URL.
    player.play.assert_called_once()
    assert player.play.call_args[0][0] == "http://127.0.0.1:57800/stream/abc"


@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.resume_store")
def test_finish_direct_playback_applies_resume_start_offset(
    mock_resume_store, mock_gui, mock_plugin
):
    """The already-chosen resume offset/key are applied to resolver handoff.

    The resume lookup happens once earlier (``_resolve_resume_choice``), so the
    finish func must NOT re-read ``resume_store`` and must write the supplied
    release identity as ``nzbdav.resume_key``.
    """
    li = MagicMock()
    mock_gui.ListItem.return_value = li

    _finish_direct_playback(
        7,
        {
            "service_port": 57800,
            "stream_url": "http://webdav/content/movie/movie.mkv",
            "stream_headers": {},
            "proxy_url": "http://127.0.0.1:57800/stream/abc",
            "stream_info": {"remux": False, "faststart": False, "direct": False},
        },
        resume_key="Movie|123|2026",
        resume_seconds=123.0,
    )

    li.setProperty.assert_called_with("StartOffset", "123.0")
    mock_gui.Window.return_value.setProperty.assert_any_call(
        "nzbdav.resume_key", "Movie|123|2026"
    )
    mock_gui.Window.return_value.setProperty.assert_any_call(
        "nzbdav.resume_offset", "123.0"
    )
    # The lookup moved earlier; the finish func must not touch resume_store.
    mock_resume_store.get_resume.assert_not_called()
    mock_plugin.setResolvedUrl.assert_called_once_with(7, True, li)


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.resume_store")
def test_finish_player_playback_applies_resume_start_offset(
    mock_resume_store, mock_gui, mock_xbmc
):
    """RunPlugin playback should also apply the already-chosen resume offset/key."""
    li = MagicMock()
    mock_gui.ListItem.return_value = li
    player = MagicMock()
    mock_xbmc.Player.return_value = player

    _finish_player_playback(
        {
            "service_port": 57800,
            "stream_url": "http://webdav/content/movie/movie.mkv",
            "stream_headers": {},
            "proxy_url": "http://127.0.0.1:57800/stream/abc",
            "stream_info": {"remux": False, "faststart": False, "direct": False},
        },
        resume_key="Movie|456|2026",
        resume_seconds=456.0,
    )

    li.setProperty.assert_called_with("StartOffset", "456.0")
    mock_gui.Window.return_value.setProperty.assert_any_call(
        "nzbdav.resume_key", "Movie|456|2026"
    )
    mock_gui.Window.return_value.setProperty.assert_any_call(
        "nzbdav.resume_offset", "456.0"
    )
    mock_resume_store.get_resume.assert_not_called()
    player.play.assert_called_once_with("http://127.0.0.1:57800/stream/abc", li)


@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.resume_store")
def test_finish_direct_playback_writes_release_id_and_chosen_offset(
    mock_resume_store, mock_gui, mock_plugin
):
    """The monitor keys on the release identity, not the disposable proxy URL.

    The finish func receives the already-resolved ``(release_id, chosen)`` pair
    and writes them verbatim, so a churning proxy URL can change between plays
    while the resume point stays keyed on the stable release identity.
    """
    li = MagicMock()
    mock_gui.ListItem.return_value = li

    _finish_direct_playback(
        7,
        {
            "service_port": 57800,
            "stream_url": "http://webdav/content/movie/movie.mkv",
            "stream_headers": {},
            "proxy_url": "http://127.0.0.1:57800/stream/new-session",
            "stream_info": {"remux": False, "faststart": False, "direct": False},
        },
        resume_key="The Movie|734003200|Tue, 01 Jan 2026",
        resume_seconds=1565.8,
    )

    # Lookup happens once earlier; the finish func never re-reads resume_store.
    mock_resume_store.get_resume.assert_not_called()
    li.setProperty.assert_called_with("StartOffset", "1565.8")
    mock_gui.Window.return_value.setProperty.assert_any_call(
        "nzbdav.resume_key", "The Movie|734003200|Tue, 01 Jan 2026"
    )
    # stream_title is derived from the source URL, not the disposable play URL.
    mock_gui.Window.return_value.setProperty.assert_any_call(
        "nzbdav.stream_title", "movie.mkv"
    )
    mock_gui.Window.return_value.setProperty.assert_any_call(
        "nzbdav.resume_offset", "1565.8"
    )
    mock_plugin.setResolvedUrl.assert_called_once_with(7, True, li)


@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.resume_store")
def test_finish_direct_playback_never_played_sets_no_start_offset(
    mock_resume_store, mock_gui, mock_plugin
):
    """A never-played item (no stored point, no bookmark) gets no StartOffset.

    With ``resume_seconds=0.0`` the finish func must leave the ListItem free of
    a StartOffset property so Kodi starts from the beginning, and it must not
    consult resume_store (the lookup already ran once earlier).
    """
    li = MagicMock()
    mock_gui.ListItem.return_value = li

    _finish_direct_playback(
        7,
        {
            "service_port": 57800,
            "stream_url": "http://webdav/content/movie/movie.mkv",
            "stream_headers": {},
            "proxy_url": "http://127.0.0.1:57800/stream/abc",
            "stream_info": {"remux": False, "faststart": False, "direct": False},
        },
        resume_key="Movie|123|2026",
        resume_seconds=0.0,
    )

    mock_resume_store.get_resume.assert_not_called()
    start_offset_calls = [
        call
        for call in li.setProperty.call_args_list
        if call.args and call.args[0] == "StartOffset"
    ]
    assert start_offset_calls == []
    mock_gui.Window.return_value.setProperty.assert_any_call(
        "nzbdav.resume_offset", "0.0"
    )
    mock_plugin.setResolvedUrl.assert_called_once_with(7, True, li)


# --- _resolve_resume_choice tests ---


@patch("resources.lib.resolver.resume_choice")
@patch("resources.lib.resolver.resume_store")
def test_resolve_resume_choice_merges_store_and_bookmark(
    mock_resume_store, mock_resume_choice
):
    """The stored offset and scrubbed bookmark merge to the larger position."""
    mock_resume_choice.release_identity.return_value = "Movie|123|2026"
    mock_resume_store.get_resume.return_value = 1565.8
    mock_resume_choice.choose_resume_seconds.return_value = 1565.8

    params = {"title": "Movie", "_download_size": "123", "_download_pubdate": "2026"}
    release_id, chosen = _resolve_resume_choice(params, 600.0)

    assert release_id == "Movie|123|2026"
    assert chosen == 1565.8
    mock_resume_store.get_resume.assert_called_once_with("Movie|123|2026")
    # Merged = max(scrubbed 600.0, stored 1565.8) is handed to the chooser.
    mock_resume_choice.choose_resume_seconds.assert_called_once_with(
        "Movie|123|2026", 1565.8
    )


@patch("resources.lib.resolver.resume_choice")
@patch("resources.lib.resolver.resume_store")
def test_resolve_resume_choice_empty_id_skips_store_lookup(
    mock_resume_store, mock_resume_choice
):
    """An empty release identity must not touch resume_store (caller skips)."""
    mock_resume_choice.release_identity.return_value = ""
    mock_resume_choice.choose_resume_seconds.return_value = 0.0

    release_id, chosen = _resolve_resume_choice({}, 0.0)

    assert release_id == ""
    assert chosen == 0.0
    mock_resume_store.get_resume.assert_not_called()
    mock_resume_choice.choose_resume_seconds.assert_called_once_with("", 0.0)


@patch("resources.lib.resolver.resume_choice")
@patch("resources.lib.resolver.resume_store")
def test_resolve_resume_choice_swallows_store_errors(
    mock_resume_store, mock_resume_choice
):
    """A resume_store read failure degrades to the scrubbed bookmark, no raise."""
    mock_resume_choice.release_identity.return_value = "Movie|123|2026"
    mock_resume_store.get_resume.side_effect = KeyError("boom")
    mock_resume_choice.choose_resume_seconds.return_value = 42.0

    release_id, chosen = _resolve_resume_choice({"title": "Movie"}, 42.0)

    assert release_id == "Movie|123|2026"
    assert chosen == 42.0
    # Merged falls back to just the scrubbed bookmark when the store read raises.
    mock_resume_choice.choose_resume_seconds.assert_called_once_with(
        "Movie|123|2026", 42.0
    )


@patch("resources.lib.resolver.resume_store")
def test_resolve_resume_choice_never_played_does_not_prompt(mock_resume_store):
    """No stored point and no bookmark -> no native prompt, chosen is 0.0.

    Exercises the real ``resume_choice.choose_resume_seconds`` short-circuit
    (``seconds <= 0`` returns 0.0 before reading the native action or showing a
    dialog), so a never-played item is never prompted and gets no resume.
    """
    mock_resume_store.get_resume.return_value = 0.0
    dialog = MagicMock()

    with patch(
        "resources.lib.resume_choice.native_resume_action"
    ) as native_action, patch("resources.lib.resume_choice.xbmcgui") as mock_choice_gui:
        mock_choice_gui.Dialog.return_value = dialog
        params = {"title": "Movie", "_download_size": "9", "_download_pubdate": "2026"}
        release_id, chosen = _resolve_resume_choice(params, 0.0)

    assert release_id == "Movie|9|2026"
    assert chosen == 0.0
    native_action.assert_not_called()
    dialog.contextmenu.assert_not_called()


@patch("resources.lib.resolver.resume_choice")
@patch("resources.lib.resolver.resume_store")
def test_resolve_resume_choice_legacy_key_fallback_on_release_miss(
    mock_resume_store, mock_resume_choice
):
    """A release-identity miss falls back to the legacy URL-keyed offset.

    Resume points saved under the old stream-URL scheme survive the upgrade to
    release-identity keys.
    """
    mock_resume_choice.release_identity.return_value = "Movie|123|2026"
    mock_resume_store.get_resume.side_effect = lambda key: (
        900.0 if key == "http://webdav/movie.mkv" else 0.0
    )
    mock_resume_choice.choose_resume_seconds.return_value = 900.0

    release_id, chosen = _resolve_resume_choice(
        {"title": "Movie"}, 0.0, legacy_key="http://webdav/movie.mkv"
    )

    assert release_id == "Movie|123|2026"
    assert chosen == 900.0
    # Release id is read first; the legacy URL is consulted only on the miss.
    assert mock_resume_store.get_resume.call_args_list == [
        call("Movie|123|2026"),
        call("http://webdav/movie.mkv"),
    ]
    # The consumed legacy entry is migrated onto the release id and dropped, so
    # a later natural-end clear of the release key can't be resurrected by it.
    mock_resume_store.save_resume.assert_called_once_with("Movie|123|2026", 900.0)
    mock_resume_store.clear_resume.assert_called_once_with("http://webdav/movie.mkv")
    mock_resume_choice.choose_resume_seconds.assert_called_once_with(
        "Movie|123|2026", 900.0
    )


@patch("resources.lib.resolver.resume_choice")
@patch("resources.lib.resolver.resume_store")
def test_resolve_resume_choice_legacy_key_skipped_when_release_hits(
    mock_resume_store, mock_resume_choice
):
    """When the release identity already has an offset, the legacy URL is not read."""
    mock_resume_choice.release_identity.return_value = "Movie|123|2026"
    mock_resume_store.get_resume.return_value = 1200.0
    mock_resume_choice.choose_resume_seconds.return_value = 1200.0

    _resolve_resume_choice(
        {"title": "Movie"}, 0.0, legacy_key="http://webdav/movie.mkv"
    )

    mock_resume_store.get_resume.assert_called_once_with("Movie|123|2026")


def test_resume_params_with_title_adds_missing_title():
    assert _resume_params_with_title({"_download_size": "9"}, "Release.Name") == {
        "_download_size": "9",
        "title": "Release.Name",
    }


def test_resume_params_with_title_overrides_stale_tmdb_title():
    """The selected NZB title wins over a stale TMDBHelper title in params."""
    assert _resume_params_with_title({"title": "Show Name"}, "Show.S01E02.1080p") == {
        "title": "Show.S01E02.1080p"
    }


def test_resume_params_with_title_keeps_params_when_title_empty():
    assert _resume_params_with_title({"title": "Existing"}, "") == {"title": "Existing"}


@patch("resources.lib.resolver.resume_store")
def test_preserve_resume_on_cancel_persists_scrubbed_offset(mock_resume_store):
    """Cancelling the prompt saves the scrubbed bookmark under the release id."""
    mock_resume_store.get_resume.return_value = 0.0
    _preserve_resume_on_cancel("Movie|123|2026", 754.0)
    mock_resume_store.save_resume.assert_called_once_with("Movie|123|2026", 754.0)


@patch("resources.lib.resolver.resume_store")
def test_preserve_resume_on_cancel_does_not_downgrade_larger_stored(mock_resume_store):
    """A larger stored point is not overwritten by an older, smaller bookmark."""
    mock_resume_store.get_resume.return_value = 3600.0
    _preserve_resume_on_cancel("Movie|123|2026", 600.0)
    mock_resume_store.save_resume.assert_not_called()


@patch("resources.lib.resolver.resume_store")
def test_preserve_resume_on_cancel_skips_without_id_or_offset(mock_resume_store):
    """Nothing is persisted without a release id or a positive offset."""
    _preserve_resume_on_cancel("", 754.0)
    _preserve_resume_on_cancel("Movie|123|2026", 0.0)
    mock_resume_store.save_resume.assert_not_called()


# --- _clear_kodi_playback_state tests ---


_FAKE_VIDEOS_DB_SCHEMA = """
CREATE TABLE files (
    idFile INTEGER PRIMARY KEY,
    idPath INTEGER,
    strFilename TEXT
);
CREATE TABLE bookmark (
    idBookmark INTEGER PRIMARY KEY,
    idFile INTEGER,
    timeInSeconds REAL,
    totalTimeInSeconds REAL
);
CREATE TABLE settings (
    idFile INTEGER PRIMARY KEY,
    ResumeTime INTEGER
);
CREATE TABLE streamdetails (
    idFile INTEGER,
    iStreamType INTEGER,
    strVideoCodec TEXT
);
"""


def _build_fake_videos_db(tmp_path):
    """Build a minimal MyVideos131.db matching Kodi's schema."""
    import sqlite3

    db = tmp_path / "MyVideos131.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.executescript(_FAKE_VIDEOS_DB_SCHEMA)
    conn.commit()
    conn.close()
    return db


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_deletes_tmdb_helper_url(mock_xbmc, tmp_path):
    """Clearing with tmdb_id deletes bookmarks for matching TMDBHelper URLs.

    Only ``bookmark`` rows are removed; the ``files`` rows themselves must
    stay intact so the mutation to Kodi's primary DB is as narrow as
    possible. Regression test for TODO.md §H.2 C5 (was ISSUE_REPORT.md C5 before merge).
    """
    import sqlite3

    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    db = _build_fake_videos_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    tmdb_base = "plugin://plugin.video.themoviedb.helper/?info=play"
    urls = [
        tmdb_base + "&tmdb_type=movie&tmdb_id=389",
        tmdb_base + "&tmdb_id=389&tmdb_type=movie",
        tmdb_base + "&tmdb_type=movie&tmdb_id=3891",  # different id — keep
        "plugin://plugin.video.nzbdav/play?type=movie&title=Other",  # unrelated
    ]
    for i, url in enumerate(urls, start=1):
        cur.execute(
            "INSERT INTO files (idFile, idPath, strFilename) VALUES (?, 1, ?)",
            (i, url),
        )
        cur.execute(
            "INSERT INTO bookmark (idFile, timeInSeconds) VALUES (?, 100.0)", (i,)
        )
        cur.execute("INSERT INTO settings (idFile, ResumeTime) VALUES (?, 100)", (i,))
        cur.execute(
            "INSERT INTO streamdetails (idFile, iStreamType, strVideoCodec) "
            "VALUES (?, 0, 'h264')",
            (i,),
        )
    conn.commit()
    conn.close()

    fake_argv = [
        "plugin://plugin.video.nzbdav/play",
        "1",
        "?type=movie&tmdb_id=389",
    ]
    with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
        mock_vfs.translatePath.return_value = str(tmp_path) + "/"
        with patch.object(sys, "argv", fake_argv):
            resume_seconds = _clear_kodi_playback_state(
                {"tmdb_id": "389", "type": "movie"}
            )

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    # files rows must all still be present — we only touch bookmark.
    cur.execute("SELECT strFilename FROM files ORDER BY idFile")
    remaining = [row[0] for row in cur.fetchall()]
    assert set(remaining) == set(
        urls
    ), "files table must not be mutated — only bookmark rows should be removed"
    # settings / streamdetails rows must also be preserved.
    cur.execute("SELECT COUNT(*) FROM settings")
    assert cur.fetchone()[0] == len(urls), "settings table must not be mutated"
    cur.execute("SELECT COUNT(*) FROM streamdetails")
    assert cur.fetchone()[0] == len(urls), "streamdetails table must not be mutated"
    # The two matching TMDBHelper URLs must have their bookmark rows gone;
    # the 3891-id row and the unrelated nzbdav row must keep theirs.
    cur.execute("SELECT idFile FROM bookmark ORDER BY idFile")
    remaining_bookmarks = {row[0] for row in cur.fetchall()}
    conn.close()
    assert 1 not in remaining_bookmarks, "bookmark for tmdb_id=389 (v1) should be gone"
    assert 2 not in remaining_bookmarks, "bookmark for tmdb_id=389 (v2) should be gone"
    assert 3 in remaining_bookmarks, "bookmark for tmdb_id=3891 should remain"
    assert 4 in remaining_bookmarks, "bookmark for unrelated URL should remain"
    assert resume_seconds == 100.0


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_ignores_near_end_bookmark(mock_xbmc, tmp_path):
    """Near-end Kodi bookmarks should be cleared without being replayed."""
    import sqlite3

    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    db = _build_fake_videos_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    url = (
        "plugin://plugin.video.themoviedb.helper/?info=play&tmdb_type=movie&tmdb_id=389"
    )
    cur.execute(
        "INSERT INTO files (idFile, idPath, strFilename) VALUES (1, 1, ?)",
        (url,),
    )
    cur.execute(
        "INSERT INTO bookmark (idFile, timeInSeconds, totalTimeInSeconds) "
        "VALUES (1, 7090.0, 7200.0)"
    )
    conn.commit()
    conn.close()

    with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
        mock_vfs.translatePath.return_value = str(tmp_path) + "/"
        resume_seconds = _clear_kodi_playback_state({"tmdb_id": "389", "type": "movie"})

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookmark")
    bookmark_count = cur.fetchone()[0]
    conn.close()
    assert bookmark_count == 0
    assert resume_seconds == 0.0


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_captures_only_resume_bookmarks(mock_xbmc, tmp_path):
    """Manual Kodi bookmarks must not be replayed as resume offsets."""
    import sqlite3

    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    db = _build_fake_videos_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("ALTER TABLE bookmark ADD COLUMN type INTEGER")
    url = (
        "plugin://plugin.video.themoviedb.helper/?info=play&tmdb_type=movie&tmdb_id=389"
    )
    cur.execute(
        "INSERT INTO files (idFile, idPath, strFilename) VALUES (1, 1, ?)",
        (url,),
    )
    cur.execute(
        "INSERT INTO bookmark (idFile, timeInSeconds, totalTimeInSeconds, type) "
        "VALUES (1, 600.0, 7200.0, 1)"
    )
    cur.execute(
        "INSERT INTO bookmark (idFile, timeInSeconds, totalTimeInSeconds, type) "
        "VALUES (1, 3600.0, 7200.0, 2)"
    )
    conn.commit()
    conn.close()

    with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
        mock_vfs.translatePath.return_value = str(tmp_path) + "/"
        resume_seconds = _clear_kodi_playback_state({"tmdb_id": "389", "type": "movie"})

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookmark")
    bookmark_count = cur.fetchone()[0]
    conn.close()
    assert bookmark_count == 0
    assert resume_seconds == 600.0


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_limits_tmdb_episode_match(mock_xbmc, tmp_path):
    """TV resume harvesting should not borrow offsets from sibling episodes."""
    import sqlite3

    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    db = _build_fake_videos_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    tmdb_base = (
        "plugin://plugin.video.themoviedb.helper/?info=play&tmdb_type=tv&tmdb_id=42"
    )
    urls = [
        tmdb_base + "&season=1&episode=1",
        tmdb_base + "&season=1&episode=2",
    ]
    for i, url in enumerate(urls, start=1):
        cur.execute(
            "INSERT INTO files (idFile, idPath, strFilename) VALUES (?, 1, ?)",
            (i, url),
        )
    cur.execute(
        "INSERT INTO bookmark (idFile, timeInSeconds, totalTimeInSeconds) "
        "VALUES (1, 600.0, 3600.0)"
    )
    cur.execute(
        "INSERT INTO bookmark (idFile, timeInSeconds, totalTimeInSeconds) "
        "VALUES (2, 1800.0, 3600.0)"
    )
    conn.commit()
    conn.close()

    fake_argv = [
        "plugin://plugin.video.nzbdav/play",
        "1",
        "?type=episode&tmdb_id=42&season=1&episode=1",
    ]
    with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
        mock_vfs.translatePath.return_value = str(tmp_path) + "/"
        with patch.object(sys, "argv", fake_argv):
            resume_seconds = _clear_kodi_playback_state(
                {
                    "tmdb_id": "42",
                    "type": "episode",
                    "season": "1",
                    "episode": "1",
                }
            )

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT idFile FROM bookmark ORDER BY idFile")
    remaining_bookmarks = [row[0] for row in cur.fetchall()]
    conn.close()
    assert remaining_bookmarks == [2]
    assert resume_seconds == 600.0


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_movie_does_not_match_episode_url(
    mock_xbmc, tmp_path
):
    """Movie resume harvesting should not borrow offsets from episode URLs."""
    import sqlite3

    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    db = _build_fake_videos_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    tmdb_base = "plugin://plugin.video.themoviedb.helper/?info=play&tmdb_id=42"
    urls = [
        tmdb_base + "&tmdb_type=movie",
        tmdb_base + "&tmdb_type=tv&season=1&episode=1",
    ]
    for i, url in enumerate(urls, start=1):
        cur.execute(
            "INSERT INTO files (idFile, idPath, strFilename) VALUES (?, 1, ?)",
            (i, url),
        )
    cur.execute(
        "INSERT INTO bookmark (idFile, timeInSeconds, totalTimeInSeconds) "
        "VALUES (1, 600.0, 7200.0)"
    )
    cur.execute(
        "INSERT INTO bookmark (idFile, timeInSeconds, totalTimeInSeconds) "
        "VALUES (2, 1800.0, 3600.0)"
    )
    conn.commit()
    conn.close()

    fake_argv = [
        "plugin://plugin.video.nzbdav/play",
        "1",
        "?type=movie&tmdb_id=42",
    ]
    with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
        mock_vfs.translatePath.return_value = str(tmp_path) + "/"
        with patch.object(sys, "argv", fake_argv):
            resume_seconds = _clear_kodi_playback_state(
                {"tmdb_id": "42", "type": "movie"}
            )

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT idFile FROM bookmark ORDER BY idFile")
    remaining_bookmarks = [row[0] for row in cur.fetchall()]
    conn.close()
    assert remaining_bookmarks == [2]
    assert resume_seconds == 600.0


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_deletes_own_plugin_url(mock_xbmc, tmp_path):
    """Clearing without tmdb_id deletes the bookmark for our own plugin URL.

    The ``files`` row is preserved; only the ``bookmark`` row is removed.
    Regression test for TODO.md §H.2 C5 (was ISSUE_REPORT.md C5 before merge).
    """
    import sqlite3

    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    db = _build_fake_videos_db(tmp_path)
    own_url = "plugin://plugin.video.nzbdav/play?type=movie&title=Test&year=2025"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO files (idFile, idPath, strFilename) VALUES (1, 1, ?)", (own_url,)
    )
    cur.execute("INSERT INTO bookmark (idFile, timeInSeconds) VALUES (1, 50.0)")
    cur.execute("INSERT INTO settings (idFile, ResumeTime) VALUES (1, 50)")
    cur.execute(
        "INSERT INTO streamdetails (idFile, iStreamType, strVideoCodec) "
        "VALUES (1, 0, 'h264')"
    )
    conn.commit()
    conn.close()

    with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
        mock_vfs.translatePath.return_value = str(tmp_path) + "/"
        with patch.object(
            sys,
            "argv",
            [
                "plugin://plugin.video.nzbdav/play",
                "1",
                "?type=movie&title=Test&year=2025",
            ],
        ):
            resume_seconds = _clear_kodi_playback_state()

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM files")
    file_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM bookmark")
    bookmark_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM settings")
    settings_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM streamdetails")
    streamdetails_count = cur.fetchone()[0]
    conn.close()

    assert file_count == 1, "files row must be preserved"
    assert bookmark_count == 0, "bookmark row must be deleted"
    assert settings_count == 1, "settings row must be preserved"
    assert streamdetails_count == 1, "streamdetails row must be preserved"
    assert resume_seconds == 50.0


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_escapes_like_wildcards(mock_xbmc, tmp_path):
    """tmdb_id containing LIKE wildcards must not match unrelated rows.

    A raw LIKE pattern with % or _ in user-controlled tmdb_id would match
    arbitrary TMDBHelper rows. Regression test for TODO.md §H.2 M5 / C5
    (was ISSUE_REPORT.md M5 / C5 before audit-file merge on 2026-04-24).
    """
    import sqlite3

    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    db = _build_fake_videos_db(tmp_path)
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    tmdb_base = "plugin://plugin.video.themoviedb.helper/?info=play"
    urls = [
        tmdb_base + "&tmdb_id=12345",  # would match LIKE '%tmdb_id=%%'
        tmdb_base + "&tmdb_id=99999",
    ]
    for i, url in enumerate(urls, start=1):
        cur.execute(
            "INSERT INTO files (idFile, idPath, strFilename) VALUES (?, 1, ?)",
            (i, url),
        )
        cur.execute(
            "INSERT INTO bookmark (idFile, timeInSeconds) VALUES (?, 100.0)", (i,)
        )
    conn.commit()
    conn.close()

    fake_argv = ["plugin://plugin.video.nzbdav/play", "1", "?tmdb_id=%"]
    with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
        mock_vfs.translatePath.return_value = str(tmp_path) + "/"
        with patch.object(sys, "argv", fake_argv):
            # tmdb_id='%' must not match any row.
            _clear_kodi_playback_state({"tmdb_id": "%"})

    conn = sqlite3.connect(str(db))
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM bookmark")
    remaining = cur.fetchone()[0]
    conn.close()
    assert remaining == 2, (
        "LIKE wildcard in tmdb_id must be escaped — "
        "no unrelated bookmarks should be deleted"
    )


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_handles_db_busy(mock_xbmc, tmp_path):
    """A sqlite3.OperationalError (DB locked) must be caught, not propagated."""
    import sqlite3

    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    db = _build_fake_videos_db(tmp_path)

    # Hold an exclusive lock on the DB so our short-timeout connection
    # hits OperationalError.
    blocker = sqlite3.connect(str(db), isolation_level=None)
    blocker_cur = blocker.cursor()
    blocker_cur.execute("BEGIN EXCLUSIVE")
    try:
        with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
            mock_vfs.translatePath.return_value = str(tmp_path) + "/"
            # Should not raise.
            _clear_kodi_playback_state({"tmdb_id": "1"})
    finally:
        blocker_cur.execute("ROLLBACK")
        blocker.close()

    # The DEBUG "DB busy" log line should have been emitted.
    log_calls = [c[0][0] for c in mock_xbmc.log.call_args_list]
    assert any(
        "busy" in c.lower() for c in log_calls
    ), "Expected a log entry mentioning the DB was busy"


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_no_db_no_crash(mock_xbmc, tmp_path):
    """If no MyVideos*.db exists, the function should silently return."""
    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
        mock_vfs.translatePath.return_value = str(tmp_path) + "/"
        _clear_kodi_playback_state({"tmdb_id": "1"})


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_skips_when_video_playing(mock_xbmc, tmp_path):
    """If a video is playing, skip DB cleanup to avoid vacuum contention."""
    mock_xbmc.Player.return_value.isPlayingVideo.return_value = True
    _clear_kodi_playback_state()
    # Should have checked isPlayingVideo and returned early — no DB access.
    mock_xbmc.Player.return_value.isPlayingVideo.assert_called_once()
    mock_xbmc.log.assert_called()
    log_calls = [c[0][0] for c in mock_xbmc.log.call_args_list]
    assert any("Skipping playback-state cleanup" in c for c in log_calls)


@patch("resources.lib.resolver.xbmc")
def test_clear_kodi_playback_state_swallows_db_errors(mock_xbmc, tmp_path):
    """An exception inside the function should be logged, not propagated."""
    mock_xbmc.Player.return_value.isPlayingVideo.return_value = False
    with patch("resources.lib.resolver.xbmcvfs") as mock_vfs:
        mock_vfs.translatePath.side_effect = RuntimeError("boom")
        _clear_kodi_playback_state()
    # Verify we logged a warning (via xbmc.log).
    mock_xbmc.log.assert_called()


# --- resolve() tests ---


@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=0)
@patch("resources.lib.stream_proxy.get_proxy")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_success(
    mock_poll,
    mock_validate,
    mock_stream_url,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_get_proxy,
    mock_service_port,
):
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = ("SABnzbd_nzo_abc123", None)
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "name": "movie",
    }
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )
    mock_validate.return_value = True
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_proxy = MagicMock()
    mock_proxy.prepare_stream.return_value = "http://127.0.0.1:57800/stream"
    mock_get_proxy.return_value = mock_proxy

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_submit.assert_called_once()
    mock_plugin.setResolvedUrl.assert_called_once()
    resolve_call = mock_plugin.setResolvedUrl.call_args
    assert resolve_call[0][1] is True


@patch("resources.lib.resolver._stop_fallback_submit_worker")
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot")
@patch("resources.lib.resolver._resolve_resume_choice")
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_starts_fallback_worker_after_primary_submit_and_uses_snapshot(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    mock_resume_choice,
    mock_snapshot,
    mock_stop_fallback,
):
    mock_poll_settings.return_value = (2, 60)
    fallback_candidates = [
        {
            "title": "Fallback A 2026 1080p WEB-DL",
            "link": "http://hydra/getnzb/fallback-a",
        }
    ]
    fallback_state = {"state": "fallback"}
    prepare_state = {"state": "prepare"}
    prepared_playback = {"state": "prepared"}
    mock_start_fallback.return_value = fallback_state
    mock_start_prepare.return_value = prepare_state
    mock_wait_prepare.return_value = prepared_playback
    mock_clear_state.return_value = 321.0
    # Resume resolution is exercised separately; here verify the scrubbed
    # bookmark position flows through into the finish handoff (release id +
    # chosen offset). The helper echoes the scrubbed seconds it is handed.
    mock_resume_choice.side_effect = lambda params, scrubbed, legacy_key="": (
        "rel-id",
        scrubbed,
    )
    call_order = []

    def poll_ready(*args, **kwargs):
        call_order.append("poll")
        assert mock_start_fallback.call_count == 0
        kwargs["poll_ctx"].on_primary_submitted("SABnzbd_nzo_primary")
        mock_start_fallback.assert_called_once_with(
            fallback_candidates,
            candidate_loader=None,
            prewarm_delay=_FALLBACK_PREWARM_DELAY_SECONDS,
            wait_for_playback=True,
            dead=ANY,
            primary_nzb_url="http://hydra/getnzb/primary",
        )
        return (
            "http://webdav/content/primary/movie.mp4",
            {"Authorization": "Basic primary"},
        )

    mock_poll_until_ready.side_effect = poll_ready
    mock_snapshot.return_value = [
        {
            "title": "Fallback A 2026 1080p WEB-DL",
            "nzb_url": "http://hydra/getnzb/fallback-a",
            "job_name": "Fallback A 2026 1080p WEB-DL [fallback-1-5c5fd5e4]",
            "nzo_id": "SABnzbd_nzo_done",
            "stream_url": "",
            "stream_headers": {},
            "content_length": 0,
        }
    ]
    mock_xbmc.Monitor.return_value = _make_monitor()
    dialog = MagicMock()
    mock_gui.DialogProgress.return_value = dialog

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mp4",
            "_fallback_candidates": fallback_candidates,
        },
    )

    assert call_order == ["poll"]
    mock_snapshot.assert_called_once_with(fallback_state, wait_seconds=8.0)
    mock_start_fallback.assert_called_once_with(
        fallback_candidates,
        candidate_loader=None,
        prewarm_delay=_FALLBACK_PREWARM_DELAY_SECONDS,
        wait_for_playback=True,
        dead=ANY,
        primary_nzb_url="http://hydra/getnzb/primary",
    )
    mock_start_prepare.assert_called_once_with(
        "http://webdav/content/primary/movie.mp4",
        {"Authorization": "Basic primary"},
        fallback_sources=[
            {
                "title": "Fallback A 2026 1080p WEB-DL",
                "nzb_url": "http://hydra/getnzb/fallback-a",
                "job_name": "Fallback A 2026 1080p WEB-DL [fallback-1-5c5fd5e4]",
                "nzo_id": "SABnzbd_nzo_done",
                "stream_url": "",
                "stream_headers": {},
                "content_length": 0,
            },
        ],
        service_config_state=None,
    )
    mock_wait_prepare.assert_called_once_with(prepare_state)
    mock_resume_choice.assert_called_once_with(
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mp4",
            "_fallback_candidates": fallback_candidates,
        },
        321.0,
        legacy_key="http://webdav/content/primary/movie.mp4",
    )
    mock_finish_playback.assert_called_once_with(
        1, prepared_playback, resume_key="rel-id", resume_seconds=321.0
    )
    mock_stop_fallback.assert_not_called()


@patch("resources.lib.resolver._resolve_resume_choice", return_value=("", 0.0))
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot")
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_attaches_fallback_handoff_for_mkv_streams(
    mock_poll_settings,
    _mock_plugin,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    mock_snapshot,
    _mock_resume_choice,
):
    mock_poll_settings.return_value = (2, 60)
    fallback_state = {"state": "fallback"}
    mock_start_fallback.return_value = fallback_state
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )
    mock_snapshot.return_value = [
        {
            "title": "Fallback A 2026 1080p WEB-DL",
            "nzb_url": "http://hydra/getnzb/fallback-a",
            "job_name": "Fallback A 2026 1080p WEB-DL [fallback-1-5c5fd5e4]",
            "nzo_id": "SABnzbd_nzo_done",
            "stream_url": "",
            "stream_headers": {},
            "content_length": 0,
        }
    ]

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [{"title": "Fallback A"}],
        },
    )

    mock_snapshot.assert_called_once_with(fallback_state, wait_seconds=8.0)
    mock_start_prepare.assert_called_once_with(
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
        fallback_sources=[
            {
                "title": "Fallback A 2026 1080p WEB-DL",
                "nzb_url": "http://hydra/getnzb/fallback-a",
                "job_name": "Fallback A 2026 1080p WEB-DL [fallback-1-5c5fd5e4]",
                "nzo_id": "SABnzbd_nzo_done",
                "stream_url": "",
                "stream_headers": {},
                "content_length": 0,
            },
        ],
        service_config_state=None,
    )
    mock_wait_prepare.assert_called_once_with({"state": "prepare"})
    mock_finish_playback.assert_called_once_with(
        1, {"state": "prepared"}, resume_key="", resume_seconds=0.0
    )


@patch("resources.lib.resolver._resolve_resume_choice", return_value=("", 0.0))
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_routes_plain_mkv_through_proxy_without_fallbacks(
    mock_poll_settings,
    _mock_plugin,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
    _mock_resume_choice,
):
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [],
        },
    )

    mock_start_prepare.assert_called_once_with(
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
        fallback_sources=[],
        service_config_state=None,
    )
    mock_wait_prepare.assert_called_once_with({"state": "prepare"})
    mock_finish_playback.assert_called_once_with(
        1, {"state": "prepared"}, resume_key="", resume_seconds=0.0
    )


@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_prefetches_fallback_loader_before_primary_submit(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    _mock_finish_playback,
    _mock_snapshot,
):
    mock_poll_settings.return_value = (2, 60)
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    loader_started = threading.Event()
    loader_can_finish = threading.Event()
    candidates = [{"title": "Fallback A", "link": "http://hydra/fallback-a"}]

    def slow_loader():
        loader_started.set()
        assert loader_can_finish.wait(timeout=1)
        return list(candidates)

    def poll_ready(*_args, **kwargs):
        assert loader_started.wait(timeout=1)
        kwargs["poll_ctx"].on_primary_submitted("SABnzbd_nzo_primary")
        assert mock_start_fallback.call_args.args == ([],)
        loader_kwarg = mock_start_fallback.call_args.kwargs["candidate_loader"]
        assert loader_kwarg is not slow_loader
        loader_can_finish.set()
        assert loader_kwarg() == candidates
        return (
            "http://webdav/content/primary/movie.mkv",
            {"Authorization": "Basic primary"},
        )

    mock_poll_until_ready.side_effect = poll_ready
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [],
            "_fallback_candidate_loader": slow_loader,
        },
    )


@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_overlaps_bookmark_cleanup_with_post_submit_poll(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
):
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    timing = {}

    def cleanup(_params):
        timing["cleanup_start"] = _time.perf_counter()
        _time.sleep(0.2)
        timing["cleanup_end"] = _time.perf_counter()

    def poll_ready(*_args, **kwargs):
        timing["poll_start"] = _time.perf_counter()
        kwargs["poll_ctx"].on_primary_submitted("SABnzbd_nzo_primary")
        _time.sleep(0.2)
        timing["poll_end"] = _time.perf_counter()
        return (
            "http://webdav/content/primary/movie.mkv",
            {"Authorization": "Basic primary"},
        )

    def finish_playback(*_args, **_kwargs):
        timing["play"] = _time.perf_counter()

    mock_clear_state.side_effect = cleanup
    mock_poll_until_ready.side_effect = poll_ready
    mock_finish_playback.side_effect = finish_playback

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mp4",
            "_fallback_candidates": [],
        },
    )

    elapsed = timing["play"] - timing["poll_start"]
    after_ready_cleanup = timing["play"] - timing["poll_end"]
    assert timing["cleanup_start"] < timing["poll_end"], (
        "bookmark cleanup started after stream readiness; elapsed={:.3f}s "
        "after_ready_cleanup={:.3f}s".format(elapsed, after_ready_cleanup)
    )
    assert timing["cleanup_end"] <= timing["play"]
    # Intervals [cleanup_start, cleanup_end] and [poll_start, poll_end] intersect
    # => cleanup ran in parallel with the post-submit poll (serial would not overlap).
    # cleanup_start < poll_end is already asserted (with a message) above.
    assert timing["poll_start"] < timing["cleanup_end"]


@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_starts_bookmark_cleanup_before_primary_submit_wait(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
):
    """Submit/adoption latency should hide bookmark cleanup after selection."""
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    timing = {}

    def cleanup(_params):
        timing["cleanup_start"] = _time.perf_counter()
        _time.sleep(0.16)
        timing["cleanup_end"] = _time.perf_counter()

    def poll_ready(*_args, **kwargs):
        timing["poll_start"] = _time.perf_counter()
        _time.sleep(0.16)
        timing["primary_submitted"] = _time.perf_counter()
        kwargs["poll_ctx"].on_primary_submitted("SABnzbd_nzo_primary")
        timing["ready"] = _time.perf_counter()
        return (
            "http://webdav/content/primary/movie.mkv",
            {"Authorization": "Basic primary"},
        )

    def finish_playback(*_args, **_kwargs):
        timing["play"] = _time.perf_counter()

    mock_clear_state.side_effect = cleanup
    mock_poll_until_ready.side_effect = poll_ready
    mock_finish_playback.side_effect = finish_playback

    started = _time.perf_counter()
    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [],
        },
    )

    cleanup_start_delay = timing["cleanup_start"] - started
    ready_to_play = timing["play"] - timing["ready"]
    assert timing["cleanup_start"] < timing["poll_start"], (
        "bookmark cleanup started after submit/poll wait; "
        "cleanup_start_delay={:.3f}s ready_to_play={:.3f}s".format(
            cleanup_start_delay,
            ready_to_play,
        )
    )
    assert ready_to_play < 0.5, (
        "bookmark cleanup was not hidden by submit/adoption latency; "
        "ready_to_play={:.3f}s".format(ready_to_play)
    )


@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value=None)
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings", return_value=(1, 60))
def test_resolve_skips_completed_lookup_after_picker_snapshot_miss(
    _mock_poll_settings,
    mock_gui,
    mock_xbmc,
    mock_submit,
    mock_find_completed,
    mock_plugin,
):
    """A picker-time completed-history miss should go straight to submit."""

    def slow_completed_lookup(_title):
        _time.sleep(0.12)

    submit_started = []
    mock_find_completed.side_effect = slow_completed_lookup
    mock_submit.side_effect = (
        lambda *_args, **_kwargs: submit_started.append(_time.perf_counter()) or None
    )
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_gui.ListItem.return_value = "li"

    started = _time.perf_counter()
    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_completed_job_lookup_done": True,
        },
    )
    elapsed_to_submit = submit_started[0] - started

    assert (
        elapsed_to_submit < 0.5
    ), "completed miss lookup delayed submit by {:.3f}s".format(elapsed_to_submit)
    mock_find_completed.assert_not_called()
    mock_submit.assert_called_once()
    mock_plugin.setResolvedUrl.assert_called_once()


@patch("resources.lib.cache_prompt.maybe_show_cache_prompt")
@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_token", return_value="token")
@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=57800)
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_overlaps_proxy_prepare_with_bookmark_cleanup_after_ready(
    mock_poll_settings,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    _mock_snapshot,
    _mock_get_port,
    _mock_get_token,
    mock_prepare,
    _mock_cache_prompt,
):
    """Proxy prepare should overlap cleanup without resolving before cleanup."""
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_gui.ListItem.return_value = MagicMock()
    timing = {}

    def cleanup(_params):
        timing["cleanup_start"] = _time.perf_counter()
        _time.sleep(0.2)
        timing["cleanup_end"] = _time.perf_counter()

    def poll_ready(*_args, **kwargs):
        timing["poll_start"] = _time.perf_counter()
        kwargs["poll_ctx"].on_primary_submitted("SABnzbd_nzo_primary")
        _time.sleep(0.02)
        timing["ready"] = _time.perf_counter()
        return (
            "http://webdav/content/primary/movie.mp4",
            {"Authorization": "Basic primary"},
        )

    def prepare(*_args, **_kwargs):
        timing["prepare_start"] = _time.perf_counter()
        _time.sleep(0.2)
        timing["prepare_end"] = _time.perf_counter()
        return (
            "http://127.0.0.1:57800/stream/primary",
            {"remux": False, "faststart": False, "direct": False},
        )

    def set_resolved(*_args):
        timing["resolved"] = _time.perf_counter()

    mock_clear_state.side_effect = cleanup
    mock_poll_until_ready.side_effect = poll_ready
    mock_prepare.side_effect = prepare
    mock_plugin.setResolvedUrl.side_effect = set_resolved

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [],
        },
    )

    assert timing["prepare_start"] < timing["cleanup_end"]
    assert timing["cleanup_end"] <= timing["resolved"]
    # Intervals [prepare_start, prepare_end] and [cleanup_start, cleanup_end]
    # intersect => proxy prepare overlapped the in-flight cleanup (not serial).
    # prepare_start < cleanup_end is already asserted above.
    assert timing["cleanup_start"] < timing["prepare_end"]


@patch("resources.lib.cache_prompt.maybe_show_cache_prompt")
@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_token", return_value="token")
@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=57800)
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_sets_resolved_url_before_remux_cache_prompt(
    mock_poll_settings,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    _mock_snapshot,
    _mock_get_port,
    _mock_get_token,
    mock_prepare,
    mock_cache_prompt,
):
    """The direct resolver path must not show a cache prompt before playback."""
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_gui.ListItem.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mp4",
        {"Authorization": "Basic primary"},
    )
    mock_prepare.return_value = (
        "http://127.0.0.1:57800/stream/primary",
        {
            "remux": True,
            "faststart": False,
            "direct": False,
            "content_type": "video/x-matroska",
            "total_bytes": 58 * 1024**3,
        },
    )
    timing = {}

    def set_resolved(*_args):
        timing["resolved"] = _time.perf_counter()

    mock_plugin.setResolvedUrl.side_effect = set_resolved

    started = _time.perf_counter()
    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [],
        },
    )

    elapsed_to_resolved = timing["resolved"] - started
    assert (
        elapsed_to_resolved < 0.5
    ), "remux cache prompt delayed setResolvedUrl by {:.3f}s".format(
        elapsed_to_resolved
    )
    mock_cache_prompt.assert_not_called()


@patch("resources.lib.resolver._resolve_resume_choice", return_value=("", 0.0))
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._start_direct_playback_service_config_lookup")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_keeps_service_config_lookup_on_resolver_thread(
    mock_poll_settings,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_service_config_lookup,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
    _mock_resume_choice,
):
    """Do not call Kodi Window APIs from a background lookup thread."""
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mp4",
        {"Authorization": "Basic primary"},
    )

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mp4",
            "_fallback_candidates": [],
        },
    )

    mock_service_config_lookup.assert_not_called()
    mock_start_prepare.assert_called_once_with(
        "http://webdav/content/primary/movie.mp4",
        {"Authorization": "Basic primary"},
        fallback_sources=[],
        service_config_state=None,
    )
    mock_wait_prepare.assert_called_once_with({"state": "prepare"})
    mock_finish_playback.assert_called_once_with(
        1, {"state": "prepared"}, resume_key="", resume_seconds=0.0
    )
    mock_clear_state.assert_called_once()


@patch("resources.lib.resolver._resolve_resume_choice", return_value=("rel-id", None))
@patch("resources.lib.resolver._stop_fallback_submit_worker")
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_cancel_prompt_resolves_false_and_does_not_finish(
    mock_poll_settings,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
    mock_stop_fallback,
    _mock_resume_choice,
):
    """Cancelling the resume prompt satisfies the setResolvedUrl(False) contract.

    On the handle-based nzbdav path a None choice must fail the resolve (False),
    clear the video playlist, stop the fallback worker, and never reach the
    playback finish handoff.
    """
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [],
        },
    )

    mock_finish_playback.assert_not_called()
    assert mock_plugin.setResolvedUrl.call_args[0][:2] == (1, False)
    mock_xbmc.PlayList.return_value.clear.assert_called_once()
    mock_stop_fallback.assert_called_once_with(
        {"state": "fallback"}, cancel_submitted=True
    )


@patch("resources.lib.resolver._resolve_resume_choice", return_value=("rel-id", None))
@patch("resources.lib.resolver._stop_fallback_submit_worker")
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_player_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_cancel_prompt_does_not_play_or_resolve(
    mock_poll_settings,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
    mock_stop_fallback,
    _mock_resume_choice,
):
    """Cancelling the resume prompt on the handle-less path simply stops.

    There is no plugin handle here, so a None choice must NOT call
    setResolvedUrl (matching this path's contract), must not start playback,
    and must stop the fallback worker.
    """
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )

    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        params={"_fallback_candidates": []},
    )

    mock_finish_playback.assert_not_called()
    mock_plugin.setResolvedUrl.assert_not_called()
    mock_stop_fallback.assert_called_once_with(
        {"state": "fallback"}, cancel_submitted=True
    )


@patch("resources.lib.cache_prompt.maybe_show_cache_prompt")
@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_token", return_value="token")
@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=57800)
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_overlaps_proxy_prepare_with_bookmark_cleanup_after_ready(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    _mock_snapshot,
    _mock_get_port,
    _mock_get_token,
    mock_prepare,
    _mock_cache_prompt,
):
    """The result-dialog RunPlugin path should overlap cleanup and proxy prepare."""
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    player = MagicMock()
    mock_xbmc.Player.return_value = player
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_gui.ListItem.return_value = MagicMock()
    timing = {}

    def cleanup(_params):
        timing["cleanup_start"] = _time.perf_counter()
        _time.sleep(0.2)
        timing["cleanup_end"] = _time.perf_counter()

    def poll_ready(*_args, **kwargs):
        timing["poll_start"] = _time.perf_counter()
        kwargs["poll_ctx"].on_primary_submitted("SABnzbd_nzo_primary")
        _time.sleep(0.02)
        timing["ready"] = _time.perf_counter()
        return (
            "http://webdav/content/primary/movie.mp4",
            {"Authorization": "Basic primary"},
        )

    def prepare(*_args, **_kwargs):
        timing["prepare_start"] = _time.perf_counter()
        _time.sleep(0.2)
        timing["prepare_end"] = _time.perf_counter()
        return (
            "http://127.0.0.1:57800/stream/primary",
            {"remux": False, "faststart": False, "direct": False},
        )

    def play(*_args):
        timing["played"] = _time.perf_counter()

    mock_clear_state.side_effect = cleanup
    mock_poll_until_ready.side_effect = poll_ready
    mock_prepare.side_effect = prepare
    player.play.side_effect = play

    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        params={"_fallback_candidates": []},
    )

    assert timing["prepare_start"] < timing["cleanup_end"], (
        "resolve_and_play proxy prepare started after cleanup; "
        "ready_to_play={:.3f}s cleanup_wait={:.3f}s".format(
            timing["played"] - timing["ready"],
            timing["cleanup_end"] - timing["ready"],
        )
    )
    assert timing["cleanup_end"] <= timing["played"]
    elapsed = timing["played"] - timing["ready"]
    assert (
        elapsed < 0.32
    ), "resolve_and_play ready-to-play stayed serial at {:.3f}s".format(elapsed)


@patch("resources.lib.resolver._resolve_resume_choice", return_value=("", 0.0))
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_player_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_does_not_wait_forever_for_stuck_bookmark_cleanup(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
    _mock_resume_choice,
):
    """A stuck noncritical bookmark cleanup must not block Player.play()."""
    cleanup_started = threading.Event()
    cleanup_can_finish = threading.Event()
    resolve_finished = threading.Event()

    def stuck_cleanup(_params):
        cleanup_started.set()
        cleanup_can_finish.wait()

    def run_resolve():
        resolve_and_play(
            "http://hydra/getnzb/primary",
            "movie.mkv",
            params={"_fallback_candidates": []},
        )
        resolve_finished.set()

    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )
    mock_clear_state.side_effect = stuck_cleanup

    thread = threading.Thread(target=run_resolve, daemon=True)
    thread.start()

    assert cleanup_started.wait(timeout=1)
    assert resolve_finished.wait(
        timeout=0.35
    ), "resolve_and_play blocked playback on a stuck bookmark cleanup worker"
    mock_start_prepare.assert_called_once()
    mock_wait_prepare.assert_called_once_with({"state": "prepare"})
    mock_finish_playback.assert_called_once_with(
        {"state": "prepared"}, resume_key="", resume_seconds=0.0
    )
    cleanup_can_finish.set()


@patch("resources.lib.resolver._resolve_resume_choice")
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_player_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_routes_plain_mkv_through_proxy_without_fallbacks(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
    mock_resume_choice,
):
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_clear_state.return_value = 456.0
    # Echo the scrubbed bookmark position so the carry-through into the
    # handle-less finish handoff (release id + chosen offset) is verified.
    mock_resume_choice.side_effect = lambda params, scrubbed, legacy_key="": (
        "rel-id",
        scrubbed,
    )
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )

    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mp4",
        params={"_fallback_candidates": []},
    )

    mock_start_prepare.assert_called_once_with(
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
        fallback_sources=[],
        service_config_state=None,
    )
    mock_wait_prepare.assert_called_once_with({"state": "prepare"})
    mock_resume_choice.assert_called_once_with(
        {"title": "movie.mp4", "_fallback_candidates": []},
        456.0,
        legacy_key="http://webdav/content/primary/movie.mkv",
    )
    mock_finish_playback.assert_called_once_with(
        {"state": "prepared"}, resume_key="rel-id", resume_seconds=456.0
    )


@patch("resources.lib.resolver._resolve_resume_choice", return_value=("", 0.0))
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot")
@patch("resources.lib.resolver._finish_player_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_attaches_fallback_handoff_for_mkv_streams(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    mock_snapshot,
    _mock_resume_choice,
):
    mock_poll_settings.return_value = (2, 60)
    fallback_state = {"state": "fallback"}
    mock_start_fallback.return_value = fallback_state
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )
    mock_snapshot.return_value = [
        {
            "title": "Fallback A 2026 1080p WEB-DL",
            "nzb_url": "http://hydra/getnzb/fallback-a",
            "job_name": "Fallback A 2026 1080p WEB-DL [fallback-1-5c5fd5e4]",
            "nzo_id": "SABnzbd_nzo_done",
            "stream_url": "",
            "stream_headers": {},
            "content_length": 0,
        }
    ]

    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        params={"_fallback_candidates": [{"title": "Fallback A"}]},
    )

    mock_snapshot.assert_called_once_with(fallback_state, wait_seconds=8.0)
    mock_start_prepare.assert_called_once_with(
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
        fallback_sources=[
            {
                "title": "Fallback A 2026 1080p WEB-DL",
                "nzb_url": "http://hydra/getnzb/fallback-a",
                "job_name": "Fallback A 2026 1080p WEB-DL [fallback-1-5c5fd5e4]",
                "nzo_id": "SABnzbd_nzo_done",
                "stream_url": "",
                "stream_headers": {},
                "content_length": 0,
            },
        ],
        service_config_state=None,
    )
    mock_wait_prepare.assert_called_once_with({"state": "prepare"})
    mock_finish_playback.assert_called_once_with(
        {"state": "prepared"}, resume_key="", resume_seconds=0.0
    )


@patch("resources.lib.cache_prompt.maybe_show_cache_prompt")
@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_token", return_value="token")
@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=57800)
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_starts_player_before_remux_cache_prompt(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    _mock_snapshot,
    _mock_get_port,
    _mock_get_token,
    mock_prepare,
    mock_cache_prompt,
):
    """The RunPlugin path should not block first playback on an advisory prompt."""
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    player = MagicMock()
    mock_xbmc.Player.return_value = player
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_gui.ListItem.return_value = MagicMock()
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mp4",
        {"Authorization": "Basic primary"},
    )
    mock_prepare.return_value = (
        "http://127.0.0.1:57800/stream/primary",
        {
            "remux": True,
            "faststart": False,
            "direct": False,
            "content_type": "video/x-matroska",
            "total_bytes": 58 * 1024**3,
        },
    )
    timing = {}

    def slow_cache_prompt(_stream_info):
        timing["cache_prompt_start"] = _time.perf_counter()
        _time.sleep(0.12)
        timing["cache_prompt_end"] = _time.perf_counter()

    def play(*_args):
        timing["played"] = _time.perf_counter()

    mock_cache_prompt.side_effect = slow_cache_prompt
    player.play.side_effect = play

    started = _time.perf_counter()
    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mp4",
        params={"_fallback_candidates": []},
    )

    elapsed_to_play = timing["played"] - started
    assert timing["played"] <= timing["cache_prompt_start"], (
        "cache prompt started before Player.play; "
        "selected-to-play={:.3f}s prompt_delay={:.3f}s".format(
            elapsed_to_play,
            timing["cache_prompt_end"] - timing["cache_prompt_start"],
        )
    )
    assert (
        elapsed_to_play < 0.5
    ), "remux cache prompt delayed Player.play by {:.3f}s".format(elapsed_to_play)
    mock_cache_prompt.assert_called_once()


@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._submit_nzb_with_retries")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_overlaps_bookmark_cleanup_with_existing_completed_fast_path(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_find_completed,
    mock_find_video,
    mock_stream_url,
    mock_submit,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
):
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_submit.side_effect = AssertionError(
        "existing completed stream should not submit"
    )
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_find_completed.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }
    mock_stream_url.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )
    timing = {}

    def cleanup(_params):
        timing["cleanup_start"] = _time.perf_counter()
        _time.sleep(0.2)
        timing["cleanup_end"] = _time.perf_counter()

    def find_video(_path, **_kwargs):
        timing["video_scan_start"] = _time.perf_counter()
        _time.sleep(0.2)
        timing["video_scan_end"] = _time.perf_counter()
        return "/content/uncategorized/movie/movie.mkv"

    def finish_playback(*_args, **_kwargs):
        timing["play"] = _time.perf_counter()

    mock_clear_state.side_effect = cleanup
    mock_find_video.side_effect = find_video
    mock_finish_playback.side_effect = finish_playback

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [],
        },
    )

    elapsed = timing["play"] - timing["video_scan_start"]
    after_ready_cleanup = timing["play"] - timing["video_scan_end"]
    assert timing["cleanup_start"] < timing["video_scan_end"], (
        "bookmark cleanup started after existing-completed stream readiness; "
        "elapsed={:.3f}s after_ready_cleanup={:.3f}s".format(
            elapsed, after_ready_cleanup
        )
    )
    assert timing["cleanup_end"] <= timing["play"]
    # Intervals [cleanup_start, cleanup_end] and [video_scan_start, video_scan_end]
    # intersect => cleanup overlapped the completed-path video scan (serial would not).
    assert timing["cleanup_start"] < timing["video_scan_end"]
    assert timing["video_scan_start"] < timing["cleanup_end"]


@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._submit_nzb_with_retries")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_uses_picker_completed_job_hint_without_history_lookup(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    mock_clear_state,
    mock_find_completed,
    mock_find_video,
    mock_stream_url,
    mock_submit,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
):
    mock_poll_settings.return_value = (2, 60)
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_submit.side_effect = AssertionError("completed picker hint should skip submit")
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_find_video.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )

    def slow_history_lookup(_title):
        _time.sleep(0.18)
        return {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        }

    mock_find_completed.side_effect = slow_history_lookup

    started = _time.perf_counter()
    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [],
            "_completed_job": {
                "status": "Completed",
                "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
                "name": "movie.mkv",
                "nzo_id": "SABnzbd_nzo_done",
            },
        },
    )
    elapsed = _time.perf_counter() - started

    mock_find_completed.assert_not_called()
    mock_finish_playback.assert_called_once()
    assert (
        elapsed < 0.5
    ), "picker completed hint still paid history lookup {:.3f}s".format(elapsed)


@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_direct_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._submit_nzb_with_retries")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_picker_completed_hint_skips_progress_dialog_startup_latency(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_find_completed,
    mock_find_video,
    mock_stream_url,
    mock_submit,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    mock_finish_playback,
    _mock_snapshot,
):
    mock_poll_settings.return_value = (2, 60)
    mock_submit.side_effect = AssertionError("completed picker hint should skip submit")
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {"state": "prepared"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_find_video.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )

    def slow_dialog_progress():
        _time.sleep(0.12)
        return MagicMock()

    mock_gui.DialogProgress.side_effect = slow_dialog_progress

    started = _time.perf_counter()
    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [],
            "_completed_job": {
                "status": "Completed",
                "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
                "name": "movie.mkv",
                "nzo_id": "SABnzbd_nzo_done",
            },
        },
    )
    elapsed = _time.perf_counter() - started

    mock_find_completed.assert_not_called()
    mock_finish_playback.assert_called_once()
    assert elapsed < 0.5, "completed hint path stalled for {:.3f}s".format(elapsed)
    mock_gui.DialogProgress.assert_not_called()


@patch("resources.lib.resolver._start_direct_playback_service_config_lookup")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_once")
@patch("resources.lib.resolver._submit_nzb_with_retries")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_skips_duplicate_stale_picker_completed_probe_before_submit(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_find_completed,
    mock_find_video,
    mock_stream_url,
    mock_submit,
    mock_poll_once,
    mock_start_fallback,
    _mock_snapshot,
    mock_start_prepare,
    mock_wait_prepare,
    mock_service_config,
):
    """A stale picker completed hint should not delay the primary submit twice."""
    mock_poll_settings.return_value = (1, 60)
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_xbmc.Player.return_value = MagicMock()
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {
        "stream_url": "http://webdav/content/ready/movie.mkv",
        "stream_headers": {},
        "service_port": 0,
    }
    service_config_done = threading.Event()
    service_config_done.set()
    mock_service_config.return_value = {"done": service_config_done}
    mock_poll_once.return_value = (
        None,
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/ready",
        },
        None,
    )
    mock_stream_url.return_value = (
        "http://webdav/content/ready/movie.mkv",
        {"Authorization": "Basic primary"},
    )
    timing = {}
    scanned_folders = []

    def slow_find_video(folder, **_kwargs):
        scanned_folders.append(folder)
        _time.sleep(0.09)
        if folder.endswith("/ready/"):
            return "/content/uncategorized/ready/movie.mkv"
        return None

    def slow_completed_lookup(_title):
        _time.sleep(0.07)

    def submit(*_args, **_kwargs):
        timing["submit_start"] = _time.perf_counter()
        return "SABnzbd_nzo_primary"

    mock_find_video.side_effect = slow_find_video
    mock_find_completed.side_effect = slow_completed_lookup
    mock_submit.side_effect = submit

    started = _time.perf_counter()
    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        params={
            "_fallback_candidates": [],
            "_completed_job": {
                "status": "Completed",
                "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/stale",
                "name": "movie.mkv",
                "nzo_id": "SABnzbd_nzo_stale",
            },
        },
    )

    elapsed_to_submit = timing["submit_start"] - started
    assert (
        elapsed_to_submit < 0.5
    ), "stale completed hint delayed primary submit by {:.3f}s".format(
        elapsed_to_submit
    )
    assert scanned_folders.count("/content/uncategorized/stale/") == 1
    mock_find_completed.assert_not_called()


@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_player_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_defers_fallback_loader_until_primary_accept(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    _mock_finish_playback,
    _mock_snapshot,
):
    """RunScript playback should prefetch discovery but defer standby submits."""
    mock_poll_settings.return_value = (1, 60)
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {
        "stream_url": "http://webdav/content/primary/movie.mkv",
        "stream_headers": {},
        "service_port": 0,
    }

    def fallback_loader():
        return [{"title": "Fallback A", "link": "http://hydra/getnzb/fallback"}]

    def poll_ready(*_args, **kwargs):
        assert mock_start_fallback.call_count == 0
        kwargs["poll_ctx"].on_primary_submitted("SABnzbd_nzo_primary")
        return (
            "http://webdav/content/primary/movie.mkv",
            {"Authorization": "Basic primary"},
        )

    mock_poll_until_ready.side_effect = poll_ready

    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        params={
            "_fallback_candidates": [],
            "_fallback_candidate_loader": fallback_loader,
        },
    )

    assert mock_start_fallback.call_count == 1
    loader_kwarg = mock_start_fallback.call_args.kwargs["candidate_loader"]
    assert loader_kwarg is not fallback_loader
    assert loader_kwarg() == [
        {"title": "Fallback A", "link": "http://hydra/getnzb/fallback"}
    ]
    mock_start_fallback.assert_called_once_with(
        [],
        candidate_loader=loader_kwarg,
        prewarm_delay=_FALLBACK_PREWARM_DELAY_SECONDS,
        wait_for_playback=True,
        dead=ANY,
        primary_nzb_url="http://hydra/getnzb/primary",
    )


def test_fallback_submit_jobs_snapshot_waits_briefly_for_active_worker_jobs():
    """Playback handoff should include fallback jobs that finish inside grace."""
    finished = threading.Event()
    release_job = threading.Event()
    lock = threading.Lock()
    jobs = []
    job = {
        "title": "Fallback A",
        "nzb_url": "http://hydra/fallback-a",
        "job_name": "Fallback A [fallback-1-5c5fd5e4]",
        "nzo_id": "SABnzbd_nzo_fallback",
    }

    def worker_target():
        release_job.wait(timeout=1)
        with lock:
            jobs.append(job)
        finished.set()

    worker = threading.Thread(target=worker_target)
    state = {
        "lock": lock,
        "jobs": jobs,
        "thread": worker,
        "stop": threading.Event(),
        "finished": finished,
    }
    worker.start()
    timer = threading.Timer(0.05, release_job.set)
    timer.start()
    try:
        assert _fallback_submit_jobs_snapshot(state, wait_seconds=0.5) == [job]
    finally:
        release_job.set()
        timer.cancel()
        worker.join(timeout=1)


@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._finish_player_playback")
@patch("resources.lib.resolver._wait_direct_playback_prepare")
@patch("resources.lib.resolver._start_direct_playback_prepare")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_passes_settings_getter_to_fallback_worker(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_start_prepare,
    mock_wait_prepare,
    _mock_finish_playback,
    _mock_snapshot,
):
    """RunScript fallback worker should reuse the thread-safe settings getter."""
    mock_poll_settings.return_value = (1, 60)
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_start_prepare.return_value = {"state": "prepare"}
    mock_wait_prepare.return_value = {
        "stream_url": "http://webdav/content/primary/movie.mkv",
        "stream_headers": {},
        "service_port": 0,
    }

    def settings_getter(_key, default=""):
        return default

    def fallback_loader():
        return [{"title": "Fallback A", "link": "http://hydra/getnzb/fallback"}]

    def poll_ready(*_args, **kwargs):
        kwargs["poll_ctx"].on_primary_submitted("SABnzbd_nzo_primary")
        return (
            "http://webdav/content/primary/movie.mkv",
            {"Authorization": "Basic primary"},
        )

    mock_poll_until_ready.side_effect = poll_ready

    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        params={
            "_fallback_candidates": [],
            "_fallback_candidate_loader": fallback_loader,
            "_settings_getter": settings_getter,
        },
    )

    assert mock_start_fallback.call_count == 1
    assert mock_start_fallback.call_args.args == ([],)
    assert mock_start_fallback.call_args.kwargs["settings_getter"] is settings_getter
    assert "candidate_loader" in mock_start_fallback.call_args.kwargs


@patch("resources.lib.cache_prompt.maybe_show_cache_prompt")
@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_token", return_value="token")
@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=57800)
@patch("resources.lib.resolver._fallback_submit_jobs_snapshot", return_value=[])
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._clear_kodi_playback_state")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_passes_settings_snapshot_to_proxy_prepare(
    mock_poll_settings,
    mock_gui,
    mock_xbmc,
    _mock_clear_state,
    mock_poll_until_ready,
    mock_start_fallback,
    _mock_snapshot,
    _mock_get_port,
    _mock_get_token,
    mock_prepare,
    _mock_cache_prompt,
):
    mock_poll_settings.return_value = (1, 60)
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()
    mock_start_fallback.return_value = {"state": "fallback"}
    mock_prepare.return_value = (
        "http://127.0.0.1:57800/stream/abc",
        {"remux": False, "faststart": False, "direct": False},
    )
    mock_poll_until_ready.return_value = (
        "http://webdav/content/primary/movie.mkv",
        {"Authorization": "Basic primary"},
    )

    values = {
        "force_remux_threshold_mb": "15000",
        "force_remux_mode": "0",
        "force_remux_mode_v2_migrated": "false",
        "strict_contract_mode": "1",
        "density_breaker_enabled": "false",
        "zero_fill_budget_enabled": "true",
        "retry_ladder_enabled": "true",
        "send_200_no_range": "false",
        "proxy_convert_subs": "true",
        "readahead_buffer_mb": "256",
        "passthrough_stall_wait": "120",
    }

    def settings_getter(key, default=""):
        return values.get(key, default)

    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        params={"_settings_getter": settings_getter},
    )

    assert mock_prepare.call_args.kwargs["settings_snapshot"] == values


def test_prepare_direct_playback_retry_reuses_settings_snapshot():
    from resources.lib.stream_proxy import ServiceProxyUnavailableError

    service_config_state = {"done": None, "service_port": 57800, "prepare_token": "old"}
    values = {
        "force_remux_threshold_mb": "15000",
        "force_remux_mode": "0",
        "force_remux_mode_v2_migrated": "true",
        "strict_contract_mode": "1",
        "density_breaker_enabled": "false",
        "zero_fill_budget_enabled": "true",
        "retry_ladder_enabled": "true",
        "send_200_no_range": "false",
        "proxy_convert_subs": "true",
        "readahead_buffer_mb": "256",
        "passthrough_stall_wait": "120",
    }
    settings_getter = MagicMock(
        side_effect=lambda key, default="": values.get(key, default)
    )

    with patch(
        "resources.lib.stream_proxy.prepare_stream_via_service",
        side_effect=[
            ServiceProxyUnavailableError("stale port"),
            ("http://127.0.0.1:57801/stream/abc", {"direct": False}),
        ],
    ) as mock_prepare, patch(
        "resources.lib.resolver._direct_playback_service_config",
        return_value=(57801, "new"),
    ):
        prepared = _prepare_direct_playback_with_service_config(
            "http://webdav/content/movie.mkv",
            {"Authorization": "Basic abc"},
            [],
            service_config_state,
            settings_getter=settings_getter,
        )

    assert prepared["proxy_url"] == "http://127.0.0.1:57801/stream/abc"
    assert settings_getter.call_count == len(values)
    assert mock_prepare.call_args_list[0].kwargs["settings_snapshot"] == values
    assert mock_prepare.call_args_list[1].kwargs["settings_snapshot"] == values


@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.resolver._direct_playback_service_config")
def test_start_direct_playback_prepare_snapshots_settings_in_worker(
    mock_service_config, mock_prepare
):
    mock_service_config.return_value = (57800, "token")
    mock_prepare.return_value = (
        "http://127.0.0.1:57800/stream/abc",
        {"direct": False},
    )
    slow_started = threading.Event()
    release_settings = threading.Event()
    calls = []

    def settings_getter(key, default=""):
        calls.append(key)
        if len(calls) == 1:
            slow_started.set()
            # Block on a test-controlled gate released ONLY in the finally below
            # (AFTER the snapshot), NOT a self-releasing timer: the worker stays
            # in-flight until then, so ``done`` cannot be set before the snapshot.
            # A self-releasing wait here would let the worker finish on its own
            # under load and false-fail the snapshot.
            release_settings.wait(timeout=2)
        return default

    state = _start_direct_playback_prepare(
        "http://webdav/content/movie.mkv",
        {"Authorization": "Basic abc"},
        fallback_sources=[],
        settings_getter=settings_getter,
    )

    # Load-independent thread-handle proof: the returned state must carry the
    # live worker thread. If thread.start() raised and prepare ran synchronously
    # (or a refactor dropped the handle / took the ready-state path), state
    # carries "thread": None. This goes red even when the in-flight gate timing
    # below stays green (e.g. a dropped ``state["thread"] = thread`` assignment).
    assert state["thread"] is not None

    try:
        # The worker is genuinely in-flight (it has entered the blocked settings
        # read) but cannot have finished prepare while the gate above is closed
        # => structural proof prepare runs off the caller's thread. A
        # synchronous-prepare regression sets ``done`` before this point.
        assert slow_started.wait(2)
        assert not state["done"].is_set()
        release_settings.set()
        prepared = _wait_direct_playback_prepare(state)
    finally:
        release_settings.set()

    assert prepared["proxy_url"] == "http://127.0.0.1:57800/stream/abc"
    assert len(calls) == 11


@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.webdav.urlopen")
@patch("resources.lib.webdav._get_settings")
def test_completed_history_reuses_webdav_settings_for_stream_url(
    mock_settings, mock_urlopen, mock_body_probe
):
    """Completed history -> playable URL should not re-read Kodi settings."""
    settings = {
        "webdav_url": "",
        "nzbdav_url": "http://nzbdav:3000",
        "username": "user",
        "password": "pass",
    }

    def slow_settings():
        _time.sleep(0.04)
        return settings

    propfind_body = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:">
  <D:response>
    <D:href>/content/uncategorized/Movie/</D:href>
    <D:propstat>
      <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
  <D:response>
    <D:href>/content/uncategorized/Movie/Movie.mkv</D:href>
    <D:propstat>
      <D:prop>
        <D:getcontentlength>123456789</D:getcontentlength>
        <D:resourcetype/>
      </D:prop>
      <D:status>HTTP/1.1 200 OK</D:status>
    </D:propstat>
  </D:response>
</D:multistatus>"""
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read.return_value = propfind_body.encode("utf-8")
    mock_urlopen.return_value = mock_resp
    mock_settings.side_effect = slow_settings

    started = _time.perf_counter()
    should_stop, stream_url, stream_headers, no_video_retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/Movie",
        },
        "Movie",
        no_video_retries=0,
        max_no_video_retries=5,
    )
    elapsed = _time.perf_counter() - started

    assert should_stop is True
    assert stream_url == "http://nzbdav:3000/content/uncategorized/Movie/Movie.mkv"
    assert stream_headers.get("Authorization", "").startswith("Basic ")
    assert no_video_retries == 0
    assert mock_settings.call_count == 1
    assert elapsed < 0.5, "completed-history stream URL took {:.3f}s".format(elapsed)


@patch("resources.lib.resolver._notify")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmc")
def test_script_history_failure_uses_notification_not_modal(
    mock_xbmc, mock_gui, mock_notify
):
    settings_getter = MagicMock(return_value="")
    history = {
        "status": "Failed",
        "nzo_id": "nzo_failed",
        "fail_message": "CRC error in article",
    }

    should_stop, stream_url, stream_headers, retries = _handle_history_result(
        history,
        "movie.mkv",
        no_video_retries=0,
        max_no_video_retries=1,
        settings_getter=settings_getter,
        modal_failures=False,
    )

    assert (should_stop, stream_url, stream_headers, retries) == (True, None, None, 0)
    mock_gui.Dialog.return_value.ok.assert_not_called()
    mock_notify.assert_called_once()
    assert "CRC error in article" in mock_notify.call_args.args[1]
    mock_xbmc.log.assert_called()


@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.webdav.get_video_file_size_hint")
@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
def test_completed_history_propfind_length_hint_is_passed_to_proxy_prepare(
    mock_find_video,
    mock_stream_url,
    mock_prepare_via_service,
    mock_size_hint,
    mock_body_probe,
):
    """Reuse the PROPFIND getcontentlength so proxy prepare can skip stream HEAD."""
    from resources.lib.resolver import _handle_history_result, _prepare_direct_playback

    mock_find_video.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_size_hint.return_value = 4294967296
    mock_stream_url.return_value = (
        "http://webdav/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic primary"},
    )
    mock_prepare_via_service.return_value = (
        "http://127.0.0.1:57800/stream/abc",
        {"remux": False},
    )

    should_stop, stream_url, stream_headers, _retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        },
        "movie.mkv",
        0,
        5,
    )
    assert should_stop is True

    _prepare_direct_playback(
        stream_url,
        stream_headers,
        service_port=57800,
        prepare_token="token",
    )

    mock_prepare_via_service.assert_called_once_with(
        57800,
        "http://webdav/content/uncategorized/movie/movie.mkv",
        "Basic primary",
        fallback_sources=None,
        content_length_hint=4294967296,
        prepare_token="token",
    )


@patch("resources.lib.stream_proxy.prepare_stream_via_service")
def test_content_length_hint_is_scoped_to_stream_auth(mock_prepare_via_service):
    from resources.lib import resolver
    from resources.lib.resolver import _prepare_direct_playback

    with resolver._STREAM_CONTENT_LENGTH_HINTS_LOCK:
        resolver._STREAM_CONTENT_LENGTH_HINTS.clear()
    resolver._remember_stream_content_length_hint(
        "http://webdav/content/movie.mkv", "Basic primary", 4294967296
    )
    mock_prepare_via_service.return_value = (
        "http://127.0.0.1:57800/stream/abc",
        {"remux": False},
    )

    _prepare_direct_playback(
        "http://webdav/content/movie.mkv",
        {"Authorization": "Basic other"},
        service_port=57800,
        prepare_token="token",
    )

    assert "content_length_hint" not in mock_prepare_via_service.call_args.kwargs


@patch("resources.lib.webdav.get_video_file_size_hint", return_value=4294967296)
def test_delegated_find_video_stream_remembers_content_length_hint(mock_size_hint):
    from resources.lib import resolver
    from resources.lib.resolver import _find_video_stream_for_folder

    with resolver._STREAM_CONTENT_LENGTH_HINTS_LOCK:
        resolver._STREAM_CONTENT_LENGTH_HINTS.clear()

    delegated = MagicMock(
        return_value=(
            "/content/uncategorized/movie/movie.mkv",
            "http://webdav/content/uncategorized/movie/movie.mkv",
            {"Authorization": "Basic delegated"},
        )
    )

    with patch("resources.lib.webdav.find_video_stream_for_folder", delegated):
        with patch.object(resolver, "find_video_stream_for_folder", delegated):
            video_path, stream_url, stream_headers = _find_video_stream_for_folder(
                "/content/uncategorized/movie"
            )

    assert video_path == "/content/uncategorized/movie/movie.mkv"
    assert stream_url == "http://webdav/content/uncategorized/movie/movie.mkv"
    assert stream_headers == {"Authorization": "Basic delegated"}
    mock_size_hint.assert_called_once_with("/content/uncategorized/movie/movie.mkv")
    assert (
        resolver._get_stream_content_length_hint(stream_url, "Basic delegated")
        == 4294967296
    )


def test_find_video_stream_for_folder_threads_title_hint_to_webdav():
    """``_find_video_stream_for_folder`` forwards ``title_hint`` to the webdav
    delegate so the episode-pack preference reaches discovery at runtime."""
    from resources.lib import resolver
    from resources.lib.resolver import _find_video_stream_for_folder

    delegated = MagicMock(
        return_value=(
            "/content/Show/Show.S02E05.mkv",
            "http://webdav/content/Show/Show.S02E05.mkv",
            {"Authorization": "Basic x"},
        )
    )

    with patch("resources.lib.webdav.find_video_stream_for_folder", delegated):
        with patch.object(resolver, "find_video_stream_for_folder", delegated):
            _find_video_stream_for_folder(
                "/content/Show/", title_hint="Show.S02E05.1080p.WEB-DL"
            )

    assert delegated.call_args.kwargs["title_hint"] == "Show.S02E05.1080p.WEB-DL"


@patch("resources.lib.resolver._stop_fallback_submit_worker")
@patch("resources.lib.resolver._start_fallback_submit_worker")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_cancels_fallback_worker_jobs_when_primary_fails(
    mock_poll_settings,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_poll_until_ready,
    mock_start_fallback,
    mock_stop_fallback,
):
    mock_poll_settings.return_value = (2, 60)
    fallback_state = {"state": "fallback"}
    mock_start_fallback.return_value = fallback_state

    def poll_not_ready(*args, **kwargs):
        kwargs["poll_ctx"].on_primary_submitted("SABnzbd_nzo_primary")
        return None, {}

    mock_poll_until_ready.side_effect = poll_not_ready
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_gui.DialogProgress.return_value = MagicMock()

    resolve(
        1,
        {
            "nzburl": "http://hydra/getnzb/primary",
            "title": "movie.mkv",
            "_fallback_candidates": [{"link": "http://hydra/getnzb/fallback"}],
        },
    )

    mock_stop_fallback.assert_called_once_with(fallback_state, cancel_submitted=True)
    mock_plugin.setResolvedUrl.assert_called_once()


def test_fallback_submit_jobs_snapshot_does_not_wait_for_worker():
    """Ready primary playback must not block on slow standby submissions."""
    worker = MagicMock()
    worker.is_alive.return_value = True
    stop_event = threading.Event()
    stop_event.set()
    job = {
        "title": "Fallback A",
        "nzb_url": "http://hydra/fallback-a",
        "job_name": "Fallback A [fallback-1-5c5fd5e4]",
        "nzo_id": "SABnzbd_nzo_fallback",
    }
    state = {
        "lock": threading.Lock(),
        "jobs": [job],
        "thread": worker,
        "stop": stop_event,
        "finished": threading.Event(),
    }

    assert _fallback_submit_jobs_snapshot(state) == [job]
    worker.join.assert_not_called()


def test_fallback_submit_jobs_snapshot_does_not_wait_for_active_worker():
    """A zero-grace snapshot remains nonblocking for shutdown/cleanup paths."""
    worker = MagicMock()
    worker.is_alive.return_value = True
    worker.join.side_effect = AssertionError("snapshot blocked on active worker")
    finished = MagicMock()
    finished.wait.side_effect = AssertionError("snapshot waited on active worker")
    job = {
        "title": "Fallback A",
        "nzb_url": "http://hydra/fallback-a",
        "job_name": "Fallback A [fallback-1-5c5fd5e4]",
        "nzo_id": "SABnzbd_nzo_fallback",
    }
    state = {
        "lock": threading.Lock(),
        "jobs": [job],
        "thread": worker,
        "stop": threading.Event(),
        "finished": finished,
    }

    assert _fallback_submit_jobs_snapshot(state, wait_seconds=0) == [job]
    worker.join.assert_not_called()
    finished.wait.assert_not_called()


@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_fallback_submit_worker_loads_candidates_in_background(mock_xbmc, mock_submit):
    """Slow fallback discovery must not block the caller starting playback."""
    release_loader = threading.Event()

    def load_candidates():
        release_loader.wait(timeout=1)
        return [
            {
                "title": "Fallback A 2026 1080p WEB-DL",
                "link": "http://hydra/getnzb/fallback-a",
            }
        ]

    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_submit.return_value = ("SABnzbd_nzo_fallback", None)

    state = _start_fallback_submit_worker([], candidate_loader=load_candidates)

    assert _fallback_submit_jobs_snapshot(state) == []
    mock_submit.assert_not_called()

    release_loader.set()
    assert state["finished"].wait(timeout=1)
    assert _fallback_submit_jobs_snapshot(state) == [
        {
            "title": "Fallback A 2026 1080p WEB-DL",
            "nzb_url": "http://hydra/getnzb/fallback-a",
            "job_name": "Fallback A 2026 1080p WEB-DL [fallback-1-253ccd06]",
            "nzo_id": "SABnzbd_nzo_fallback",
            "stream_url": "",
            "stream_headers": {},
            "content_length": 0,
        }
    ]


@patch("resources.lib.resolver._submit_fallback_candidates")
def test_fallback_submit_worker_defers_prewarm_burst(mock_submit):
    """The fallback prewarm must wait ``prewarm_delay`` before submitting, so the
    multi-source burst doesn't pile concurrent nzbdav connections onto a
    just-started playback stream (the live black-screen freezes, 2026-05-31).

    A stop during the deferral window cancels the burst with no submission.
    """
    mock_submit.return_value = ("SABnzbd_nzo_fallback", None)

    state = _start_fallback_submit_worker(
        candidates=[{"title": "Fallback A", "link": "http://hydra/getnzb/a"}],
        prewarm_delay=30.0,
    )
    # Stop during the deferral window → burst cancelled, nothing submitted.
    state["stop"].set()
    assert state["finished"].wait(timeout=2)
    mock_submit.assert_not_called()


@patch("resources.lib.resolver._submit_fallback_candidates")
def test_fallback_submit_worker_waits_for_playback_start_before_prewarm(mock_submit):
    """With ``wait_for_playback``, the burst is held until playback is signaled,
    then for ``prewarm_delay`` -- so backups submit INTO playback, never during
    the pre-playback download. Nothing is submitted before the signal; after it
    (with prewarm_delay=0) the worker submits."""
    from resources.lib.resolver import _signal_fallback_playback_started

    mock_submit.return_value = ("SABnzbd_nzo_fallback", None)

    state = _start_fallback_submit_worker(
        candidates=[{"title": "Fallback A", "link": "http://hydra/getnzb/a"}],
        prewarm_delay=0,
        wait_for_playback=True,
    )
    # Playback not signaled yet → worker parked, nothing submitted.
    assert not state["finished"].wait(timeout=0.3)
    mock_submit.assert_not_called()
    # Signal playback start → worker proceeds (prewarm_delay=0) and submits.
    _signal_fallback_playback_started(state)
    assert state["finished"].wait(timeout=2)
    mock_submit.assert_called_once()


@patch("resources.lib.resolver._submit_fallback_candidates")
def test_fallback_submit_worker_playback_wait_is_cancellable(mock_submit):
    """A stop during the pre-playback wait aborts the burst with no submission."""
    state = _start_fallback_submit_worker(
        candidates=[{"title": "Fallback A", "link": "http://hydra/getnzb/a"}],
        prewarm_delay=0,
        wait_for_playback=True,
    )
    state["stop"].set()
    assert state["finished"].wait(timeout=2)
    mock_submit.assert_not_called()


@patch("resources.lib.resolver._submit_fallback_candidates")
def test_fallback_submit_worker_passes_settings_getter_to_submit_candidates(
    mock_submit_fallbacks,
):
    from resources.lib.resolver import _start_fallback_submit_worker

    def settings_getter(_key, default=""):
        return default

    state = _start_fallback_submit_worker(
        [{"title": "Fallback A", "link": "http://hydra/getnzb/fallback-a"}],
        settings_getter=settings_getter,
    )

    assert state["finished"].wait(timeout=2)
    assert mock_submit_fallbacks.call_args.kwargs["settings_getter"] is settings_getter


def test_prefetch_fallback_candidate_loader_starts_immediately_and_caches_result():
    """Selection manifest discovery should overlap primary submit latency."""
    loader_started = threading.Event()
    loader_can_finish = threading.Event()
    calls = []
    candidates = [{"title": "Fallback A", "link": "http://hydra/fallback-a"}]

    def slow_loader():
        calls.append("load")
        loader_started.set()
        assert loader_can_finish.wait(timeout=1)
        return list(candidates)

    wrapped = _prefetch_fallback_candidate_loader(slow_loader)

    assert callable(wrapped)
    assert loader_started.wait(timeout=1)

    loader_can_finish.set()

    assert wrapped() == candidates
    assert wrapped() == candidates
    assert calls == ["load"]


def test_prefetch_fallback_candidate_loader_preserves_disabled_sentinel():
    from resources.lib.fallback_streams import FALLBACK_CANDIDATES_DISABLED

    wrapped = _prefetch_fallback_candidate_loader(lambda: FALLBACK_CANDIDATES_DISABLED)

    assert wrapped() is FALLBACK_CANDIDATES_DISABLED


def test_fallback_submit_jobs_snapshot_waits_for_stopping_worker_final_jobs():
    """Shutdown snapshots should include jobs recorded while worker exits."""
    stop_event = threading.Event()
    stop_event.set()
    finished = threading.Event()
    allow_finish = threading.Event()
    lock = threading.Lock()
    jobs = []
    final_job = {
        "title": "Fallback B",
        "nzb_url": "http://hydra/fallback-b",
        "job_name": "Fallback B [fallback-2-5c5fd5e4]",
        "nzo_id": "SABnzbd_nzo_final",
    }

    def worker_target():
        allow_finish.wait(timeout=1)
        with lock:
            jobs.append(final_job)
        finished.set()

    worker = threading.Thread(target=worker_target)
    state = {
        "lock": lock,
        "jobs": jobs,
        "thread": worker,
        "stop": stop_event,
        "finished": finished,
    }
    worker.start()

    timer = threading.Timer(0.05, allow_finish.set)
    timer.start()
    try:
        assert _fallback_submit_jobs_snapshot(state) == [final_job]
    finally:
        allow_finish.set()
        timer.cancel()
        worker.join(timeout=1)


def test_stop_fallback_submit_worker_uses_bounded_join_for_active_worker():
    worker = MagicMock()
    worker.is_alive.return_value = True
    join_timeouts = []

    def record_join(timeout=None):
        if timeout is None:
            raise AssertionError("fallback shutdown used an unbounded join")
        join_timeouts.append(timeout)

    worker.join.side_effect = record_join
    cancelled = []
    stop_event = threading.Event()
    job = {
        "title": "Fallback A",
        "nzo_id": "SABnzbd_nzo_fallback",
        "status": "Downloading",
    }
    state = {
        "lock": threading.Lock(),
        "jobs": [job],
        "thread": worker,
        "stop": stop_event,
        "finished": threading.Event(),
        "cancel_job": cancelled.append,
    }

    assert _stop_fallback_submit_worker(
        state, cancel_submitted=True, join_timeout=0.05
    ) == [job]

    assert stop_event.is_set()
    assert join_timeouts == [0.05]
    assert cancelled == ["SABnzbd_nzo_fallback"]
    assert state["thread"] is worker


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_stop_fallback_submit_worker_cancels_job_finished_after_shutdown(
    mock_xbmc, mock_submit, mock_cancel_job
):
    submit_started = threading.Event()
    release_submit = threading.Event()

    def delayed_submit(*args):
        submit_started.set()
        assert release_submit.wait(timeout=1)
        return "SABnzbd_nzo_late", None

    mock_submit.side_effect = delayed_submit
    mock_xbmc.Monitor.return_value = _make_monitor()
    state = _start_fallback_submit_worker(
        [
            {
                "title": "Fallback A 2026 1080p WEB-DL",
                "link": "http://hydra/getnzb/fallback-a",
            }
        ]
    )

    assert submit_started.wait(timeout=1)
    assert (
        _stop_fallback_submit_worker(state, cancel_submitted=True, join_timeout=0.01)
        == []
    )

    release_submit.set()
    assert state["finished"].wait(timeout=1)
    mock_cancel_job.assert_called_once_with("SABnzbd_nzo_late")
    assert _fallback_submit_jobs_snapshot(state) == []


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_stop_fallback_submit_worker_cancels_late_job_with_settings_getter(
    mock_xbmc, mock_submit, mock_cancel_job
):
    submit_started = threading.Event()
    release_submit = threading.Event()
    settings = {
        "nzbdav_url": "http://nzbdav.example",
        "nzbdav_api_key": "secret",
    }

    def delayed_submit(*args, **kwargs):
        submit_started.set()
        assert kwargs["settings_getter"]("nzbdav_url") == settings["nzbdav_url"]
        assert release_submit.wait(timeout=1)
        return "SABnzbd_nzo_late", None

    mock_submit.side_effect = delayed_submit
    mock_xbmc.Monitor.return_value = _make_monitor()
    state = _start_fallback_submit_worker(
        [
            {
                "title": "Fallback A 2026 1080p WEB-DL",
                "link": "http://hydra/getnzb/fallback-a",
            }
        ],
        settings_getter=lambda key, default="": settings.get(key, default),
    )

    assert submit_started.wait(timeout=1)
    assert (
        _stop_fallback_submit_worker(state, cancel_submitted=True, join_timeout=0.01)
        == []
    )

    release_submit.set()
    assert state["finished"].wait(timeout=1)
    mock_cancel_job.assert_called_once()
    assert mock_cancel_job.call_args.args == ("SABnzbd_nzo_late",)
    settings_getter = mock_cancel_job.call_args.kwargs["settings_getter"]
    assert settings_getter("nzbdav_url") == settings["nzbdav_url"]
    assert _fallback_submit_jobs_snapshot(state) == []


def test_stop_fallback_submit_worker_cancels_running_jobs_when_requested():
    worker = MagicMock()
    worker.is_alive.return_value = False
    cancelled = []
    stop_event = threading.Event()
    job = {
        "title": "Fallback A",
        "nzo_id": "SABnzbd_nzo_fallback",
        "status": "Downloading",
    }
    state = {
        "lock": threading.Lock(),
        "jobs": [job],
        "thread": worker,
        "stop": stop_event,
        "finished": threading.Event(),
        "cancel_job": cancelled.append,
    }

    assert _stop_fallback_submit_worker(state, cancel_submitted=True) == [job]

    assert stop_event.is_set()
    worker.join.assert_called_once_with(timeout=10)
    assert cancelled == ["SABnzbd_nzo_fallback"]
    assert state["thread"] is None


@patch("resources.lib.resolver._notify", create=True)
@patch("resources.lib.resolver._submit_fallback_candidates")
def test_fallback_submit_worker_notifies_when_loader_finds_no_candidates(
    mock_submit_fallbacks, mock_notify
):
    from resources.lib.resolver import _start_fallback_submit_worker

    state = _start_fallback_submit_worker(candidate_loader=lambda: [])

    assert state["finished"].wait(timeout=2)
    mock_submit_fallbacks.assert_not_called()
    mock_notify.assert_called_once()
    assert "No known fallback matches" in mock_notify.call_args[0][1]


@patch("resources.lib.resolver.xbmcaddon", create=True)
@patch("resources.lib.resolver._notify")
@patch("resources.lib.resolver._submit_fallback_candidates")
def test_fallback_submit_worker_does_not_notify_no_candidates_when_disabled(
    mock_submit_fallbacks, mock_notify, mock_xbmcaddon
):
    from resources.lib.resolver import _start_fallback_submit_worker

    mock_xbmcaddon.Addon.return_value.getSetting.return_value = "false"

    state = _start_fallback_submit_worker(candidate_loader=lambda: [])

    assert state["finished"].wait(timeout=2)
    mock_submit_fallbacks.assert_not_called()
    mock_notify.assert_not_called()


@patch(
    "resources.lib.resolver._fallback_streams_enabled",
    side_effect=AssertionError("disabled loader should not re-check settings"),
)
@patch("resources.lib.resolver._notify")
@patch("resources.lib.resolver._submit_fallback_candidates")
def test_fallback_submit_worker_does_not_notify_when_loader_reports_disabled(
    mock_submit_fallbacks, mock_notify, _mock_enabled
):
    from resources.lib.fallback_streams import FALLBACK_CANDIDATES_DISABLED
    from resources.lib.resolver import _start_fallback_submit_worker

    state = _start_fallback_submit_worker(
        candidate_loader=lambda: FALLBACK_CANDIDATES_DISABLED
    )

    assert state["finished"].wait(timeout=2)
    mock_submit_fallbacks.assert_not_called()
    mock_notify.assert_not_called()


def test_stop_fallback_submit_worker_skips_completed_jobs_when_cancelling():
    worker = MagicMock()
    cancelled = []
    stop_event = threading.Event()
    state = {
        "lock": threading.Lock(),
        "jobs": [{"nzo_id": "SABnzbd_nzo_done", "status": "Completed"}],
        "thread": worker,
        "stop": stop_event,
        "finished": threading.Event(),
        "cancel_job": cancelled.append,
    }

    _stop_fallback_submit_worker(state, cancel_submitted=True)

    assert not cancelled


@patch("resources.lib.resolver._show_submit_error_dialog")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_submit_fallback_candidates_rejection_logs_without_dialog(
    mock_xbmc, mock_submit, mock_show_dialog
):
    from resources.lib.resolver import _submit_fallback_candidates

    mock_submit.return_value = (
        None,
        {"status": "rejected", "message": "bad fallback nzb"},
    )
    monitor = _make_monitor()

    jobs = _submit_fallback_candidates(
        [
            {
                "title": "Fallback A 2026 1080p WEB-DL",
                "link": "http://hydra/getnzb/fallback-a",
            }
        ],
        monitor,
    )

    assert not jobs
    mock_submit.assert_called_once_with(
        "http://hydra/getnzb/fallback-a",
        "Fallback A 2026 1080p WEB-DL [fallback-1-253ccd06]",
    )
    mock_show_dialog.assert_not_called()
    mock_xbmc.log.assert_called()


@patch("resources.lib.resolver.find_queued_by_names")
@patch("resources.lib.resolver.find_completed_by_names")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_fallback_candidates_adopts_existing_completed_before_submit(
    mock_submit, mock_find_completed, mock_find_queued
):
    from resources.lib.resolver import _submit_fallback_candidates

    expected_job_name = "Fallback A 2026 1080p WEB-DL [fallback-1-253ccd06]"
    mock_find_completed.return_value = {
        expected_job_name: {
            "nzo_id": "SABnzbd_nzo_existing",
            "status": "Completed",
        }
    }
    mock_submit.side_effect = AssertionError("existing fallback should be adopted")
    observed_jobs = []

    jobs = _submit_fallback_candidates(
        [
            {
                "title": "Fallback A 2026 1080p WEB-DL",
                "link": "http://hydra/getnzb/fallback-a",
            }
        ],
        _make_monitor(),
        on_job=observed_jobs.append,
    )

    expected = {
        "title": "Fallback A 2026 1080p WEB-DL",
        "nzb_url": "http://hydra/getnzb/fallback-a",
        "job_name": expected_job_name,
        "nzo_id": "SABnzbd_nzo_existing",
        "stream_url": "",
        "stream_headers": {},
        "content_length": 0,
        "status": "Completed",
    }
    assert jobs == [expected]
    assert observed_jobs == [expected]
    mock_find_completed.assert_called_once_with([expected_job_name])
    mock_find_queued.assert_called_once_with([])
    mock_submit.assert_not_called()


@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_names", create=True, return_value={})
@patch("resources.lib.resolver.find_completed_by_names", create=True, return_value={})
@patch("resources.lib.resolver.submit_nzb")
def test_submit_fallback_candidates_batches_existing_job_probes(
    mock_submit,
    mock_find_completed_batch,
    mock_find_queued_batch,
    mock_find_completed,
    mock_find_queued,
):
    from resources.lib.resolver import _submit_fallback_candidates

    candidates = [
        {"title": "Fallback A", "link": "http://hydra/getnzb/fallback-a"},
        {"title": "Fallback B", "link": "http://hydra/getnzb/fallback-b"},
    ]
    mock_submit.side_effect = [
        ("SABnzbd_nzo_submitted_a", None),
        ("SABnzbd_nzo_submitted_b", None),
    ]

    jobs = _submit_fallback_candidates(candidates, _make_monitor())

    expected_names = [
        "Fallback A [fallback-1-253ccd06]",
        "Fallback B [fallback-2-15dc370d]",
    ]
    mock_find_completed_batch.assert_called_once_with(expected_names)
    mock_find_queued_batch.assert_called_once_with(expected_names)
    mock_find_completed.assert_not_called()
    mock_find_queued.assert_not_called()
    assert [job["nzo_id"] for job in jobs] == [
        "SABnzbd_nzo_submitted_a",
        "SABnzbd_nzo_submitted_b",
    ]


@patch("resources.lib.resolver.find_queued_by_names", return_value={})
@patch("resources.lib.resolver.find_completed_by_names", return_value={})
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo-fallback", None))
def test_fallback_job_records_preserve_full_episode_context(
    _mock_submit, _mock_completed, _mock_queued
):
    from resources.lib.resolver import _submit_fallback_candidates

    episode_context = {
        "type": "episode",
        "title": "Spider-Noir",
        "imdb": "tt1234567",
        "tvdb": "451234",
        "tmdb_id": "987",
        "season": 1,
        "episode": 1,
    }
    jobs = _submit_fallback_candidates(
        [{"title": "Spider-Noir.S01", "link": "http://i/fallback.nzb"}],
        _make_monitor(),
        episode_context=episode_context,
    )

    assert jobs[0]["episode_context"] == episode_context


@patch("resources.lib.resolver.find_queued_by_names", return_value={})
@patch("resources.lib.resolver.find_completed_by_names", return_value={})
@patch("resources.lib.resolver.submit_nzb")
def test_submit_fallback_candidates_passes_settings_getter_to_nzbdav_calls(
    mock_submit,
    mock_find_completed,
    mock_find_queued,
):
    from resources.lib.resolver import _submit_fallback_candidates

    def settings_getter(_key, default=""):
        return default

    mock_submit.return_value = ("SABnzbd_nzo_fallback", None)

    jobs = _submit_fallback_candidates(
        [{"title": "Fallback A", "link": "http://hydra/getnzb/fallback-a"}],
        _make_monitor(),
        settings_getter=settings_getter,
    )

    expected_job_name = "Fallback A [fallback-1-253ccd06]"
    mock_find_completed.assert_called_once_with(
        [expected_job_name], settings_getter=settings_getter
    )
    mock_find_queued.assert_called_once_with(
        [expected_job_name], settings_getter=settings_getter
    )
    mock_submit.assert_called_once_with(
        "http://hydra/getnzb/fallback-a",
        expected_job_name,
        settings_getter=settings_getter,
    )
    assert jobs[0]["nzo_id"] == "SABnzbd_nzo_fallback"


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_submit_failure(
    mock_poll, mock_submit, mock_plugin, mock_gui, mock_xbmc, mock_find_completed
):
    """All submit retries fail — setResolvedUrl called with False."""
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = (None, None)
    mock_find_completed.return_value = None
    mock_xbmc.Monitor.return_value = MagicMock()
    mock_xbmc.Monitor.return_value.waitForAbort.return_value = False

    dialog = MagicMock()
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())
    assert mock_submit.call_count == 3


@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_probes_queue_without_two_second_startup_delay(
    mock_submit, mock_find_queued
):
    """Queue adoption should not wait two seconds before its first probe."""
    queue_seen = threading.Event()

    def delayed_submit(_nzb_url, _title):
        assert queue_seen.wait(timeout=1.5)
        return None, {"status": "timeout", "message": "Timed out"}

    def queued_job(_title):
        queue_seen.set()
        return {
            "nzo_id": "SABnzbd_nzo_queue_probe",
            "name": "movie.mkv",
            "status": "Downloading",
        }

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = queued_job
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = _make_monitor()

    started = _time.monotonic()
    nzo_id, submit_error = _submit_nzb_with_ui_pump(
        "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
    )
    elapsed = _time.monotonic() - started

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_queue_probe", None)
    assert elapsed < 1.5


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.find_queued_by_name", return_value=None)
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_passes_settings_getter_to_submit_worker(
    mock_submit, mock_find_queued, mock_find_completed
):
    def settings_getter(_key, default=""):
        return default

    seen = {}

    def submit(_nzb_url, _title, **kwargs):
        seen.update(kwargs)
        return "SABnzbd_nzo_script", None

    mock_submit.side_effect = submit
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = _make_monitor()

    nzo_id, submit_error = _submit_nzb_with_ui_pump(
        "http://hydra/getnzb/abc",
        "movie.mkv",
        dialog,
        monitor,
        settings_getter=settings_getter,
    )

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_script", None)
    assert seen["settings_getter"] is settings_getter
    assert seen["submit_timeout"] == 300
    if mock_find_queued.called:
        assert mock_find_queued.call_args.kwargs["settings_getter"] is settings_getter
    if mock_find_completed.called:
        assert (
            mock_find_completed.call_args.kwargs["settings_getter"] is settings_getter
        )


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.find_queued_by_name", return_value=None)
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_uses_nonblocking_abort_check_after_submit_result(
    mock_submit, _mock_find_queued, _mock_find_completed
):
    def delayed_terminal_submit(_nzb_url, _title, **_kwargs):
        _time.sleep(0.03)
        return None, {"status": 400, "message": "TooManyRequests"}

    def wait_for_abort(seconds):
        if seconds == 0:
            _time.sleep(0.25)
        return False

    mock_submit.side_effect = delayed_terminal_submit
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = wait_for_abort
    monitor.abortRequested.return_value = False

    started = _time.monotonic()
    nzo_id, submit_error = _submit_nzb_with_ui_pump(
        "http://hydra/getnzb/rate-limited",
        "movie.mkv",
        dialog,
        monitor,
    )
    elapsed = _time.monotonic() - started

    assert nzo_id is None
    assert submit_error == {"status": 400, "message": "TooManyRequests"}
    assert elapsed < 0.5
    assert 0 not in [call.args[0] for call in monitor.waitForAbort.call_args_list]


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.find_queued_by_name", return_value=None)
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_continues_when_probe_threads_cannot_start(
    mock_submit, _mock_find_queued, _mock_find_completed
):
    """Thread exhaustion in optional probes must not abort an active submit."""
    real_thread = threading.Thread
    allow_submit = threading.Event()

    def submit(_nzb_url, _title, **_kwargs):
        assert allow_submit.wait(timeout=1)
        return "SABnzbd_nzo_script", None

    class FakeThread:
        def __init__(self, target, name=None, daemon=None):
            self.target = target
            self.name = name
            self.daemon = daemon
            self._thread = None

        def start(self):
            if self.name == "nzbdav-submit":
                self._thread = real_thread(target=self.target, daemon=self.daemon)
                self._thread.start()
                return
            allow_submit.set()
            raise RuntimeError("can't start new thread")

        def join(self, timeout=None):
            if self._thread is not None:
                self._thread.join(timeout=timeout)

    mock_submit.side_effect = submit
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = _make_monitor()

    with patch("resources.lib.resolver.threading.Thread", FakeThread):
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_script", None)


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_terminal_error_does_not_wait_for_probe_cleanup(
    mock_submit, mock_find_queued, mock_find_completed
):
    probe_in_flight = threading.Event()
    release_probe = threading.Event()
    probe_completed = []

    def terminal_submit(_nzb_url, _title, **_kwargs):
        # Hold the terminal error until a probe is genuinely MID-FLIGHT, so the
        # snapshot below is meaningful: the terminal path must return while
        # slow_probe is still blocked. A regression that joined/awaited probe
        # cleanup on the terminal path would block on this in-flight probe.
        assert probe_in_flight.wait(timeout=2)
        return None, {"status": 400, "message": "TooManyRequests"}

    def slow_probe(*_args, **_kwargs):
        # Mark in-flight, then block until the test releases us in its finally
        # (AFTER the snapshot), with no timer -- load-independent. The bounded
        # wait caps the regression case (a terminal path that DOES await probe
        # cleanup) so the test cannot hang.
        probe_in_flight.set()
        release_probe.wait(timeout=2)
        probe_completed.append(True)

    mock_submit.side_effect = terminal_submit
    mock_find_queued.side_effect = slow_probe
    mock_find_completed.side_effect = slow_probe
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.abortRequested.return_value = False

    try:
        started = _time.monotonic()
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/rate-limited",
            "movie.mkv",
            dialog,
            monitor,
        )
        elapsed = _time.monotonic() - started
        # Load-independent proof: the terminal error returned WHILE the probe is
        # still mid-flight (blocked) -- it was NOT awaited, so no slow_probe ran
        # past its block and the completed list is still empty at return. Catches
        # an UNBOUNDED await of the still-parked probe threads.
        assert not probe_completed
        # Bounded-join guard (test-analyzer): dropping the terminal-error
        # early-skip would let cleanup fall through to t.join(timeout=1) on each
        # still-parked probe, pinning the return at ~1-2s. The healthy terminal
        # path returns in ~ms (>=10x margin); this generous 0.5s ceiling sits
        # well below the ~1s+ regression, staying load-independent while still
        # going red on the bounded join that `not probe_completed` alone misses.
        assert elapsed < 0.5
        assert nzo_id is None
        assert submit_error == {"status": 400, "message": "TooManyRequests"}
    finally:
        release_probe.set()


@patch("resources.lib.resolver._show_submit_error_dialog")
@patch("resources.lib.resolver._adopt_queued_or_completed_job")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_retries_http_4xx_closes_progress_and_skips_adoption(
    mock_submit, mock_adopt, mock_show_error
):
    submit_error = {
        "status": 400,
        "message": "Failed to fetch nzb-file url: TooManyRequests",
    }
    mock_submit.return_value = (None, submit_error)
    dialog = MagicMock()
    monitor = _make_monitor()

    result = _submit_nzb_with_retries(
        "http://hydra/getnzb/rate-limited",
        "movie.mkv",
        dialog,
        monitor,
    )

    assert result is None
    mock_submit.assert_called_once()
    mock_adopt.assert_not_called()
    dialog.close.assert_called_once()
    mock_show_error.assert_called_once_with(submit_error)


@patch("resources.lib.resolver.xbmcgui")
def test_show_submit_error_dialog_uses_nonblocking_rate_limit_notification(mock_gui):
    submit_error = {
        "status": 400,
        "indexer": "DrunkenSlug",
        "message": (
            "Failed to fetch nzb-file url "
            "`http://hydra/getnzb/abc?apikey=secret` Received status code "
            "TooManyRequests."
        ),
    }

    _show_submit_error_dialog(submit_error)

    dialog = mock_gui.Dialog.return_value
    dialog.ok.assert_not_called()
    dialog.notification.assert_called_once()
    message = dialog.notification.call_args.args[1]
    assert "DrunkenSlug" in message
    assert "too many requests" in message.lower()
    assert "http://" not in message
    assert "getnzb" not in message
    assert "TooManyRequests" not in message


@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.stream_proxy.get_service_proxy_token", return_value="token")
@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=57800)
def test_wait_direct_playback_prepare_waits_for_local_proxy_when_prepare_stalls(
    _mock_get_port, _mock_get_token, mock_prepare
):
    def slow_prepare(*_args, **_kwargs):
        _time.sleep(0.04)
        return (
            "http://127.0.0.1:57800/stream/slow",
            {"remux": False, "faststart": False, "direct": False},
        )

    stream_url = "http://webdav/content/movie.mkv"
    stream_headers = {"Authorization": "Basic x"}
    mock_prepare.side_effect = slow_prepare

    state = _start_direct_playback_prepare(stream_url, stream_headers)
    prepared = _wait_direct_playback_prepare(state, wait_seconds=0.01)

    # The returned proxy_url is the SLOW prepare's own output (".../stream/slow"),
    # which only exists once the stalled 0.04s prepare ran to completion -- so a
    # correct result proves _wait_direct_playback_prepare waited for it. The old
    # `elapsed >= 0.03` lower bound was redundant with that AND flaked under load
    # (the worker's 0.04s sleep can begin before `started` is captured, so the
    # measured span dips below 0.03 even though the wait happened).
    assert prepared["service_port"] == 57800
    assert prepared["stream_url"] == stream_url
    assert prepared["stream_headers"] == stream_headers
    assert prepared["proxy_url"] == "http://127.0.0.1:57800/stream/slow"


@patch("resources.lib.resolver.probe_webdav_reachable", return_value=(False, None))
@patch("resources.lib.resolver.get_job_history", return_value=None)
@patch("resources.lib.resolver.get_job_status", return_value=None)
def test_poll_once_passes_settings_getter_to_queue_history_workers(
    mock_status, mock_history, _mock_probe
):
    def settings_getter(_key, default=""):
        return default

    _poll_once("SABnzbd_nzo_script", "movie.mkv", _make_monitor(), settings_getter)

    assert mock_status.call_args.kwargs["settings_getter"] is settings_getter
    assert mock_history.call_args.kwargs["settings_getter"] is settings_getter


@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_starts_queue_probe_within_short_grace_window(
    mock_submit, mock_find_queued
):
    """Fast queue adoption should not spend a quarter second in the grace window."""
    queue_seen = threading.Event()

    def delayed_submit(_nzb_url, _title):
        assert queue_seen.wait(timeout=1)
        return None, {"status": "timeout", "message": "Timed out"}

    def queued_job(_title):
        queue_seen.set()
        return {
            "nzo_id": "SABnzbd_nzo_fast_queue_probe",
            "name": "movie.mkv",
            "status": "Downloading",
        }

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = queued_job
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    started = _time.monotonic()
    nzo_id, submit_error = _submit_nzb_with_ui_pump(
        "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
    )
    elapsed = _time.monotonic() - started

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_fast_queue_probe", None)
    assert elapsed < 0.2


@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_adopts_existing_queue_without_initial_probe_delay(
    mock_submit, mock_find_queued
):
    """Existing nzbdav queue jobs should be adopted without a fixed grace wait."""
    queue_seen = threading.Event()
    submit_can_finish = threading.Event()
    submit_completed = [False]
    first_probe_at = []

    def delayed_submit(_nzb_url, _title):
        assert queue_seen.wait(timeout=1)
        # Released only in the finally below (AFTER the snapshot), with no
        # early timer, so the slow submit worker cannot finish before we
        # record whether adoption waited for it -- load-independent. The
        # 0.75s cap only bounds a regression that blocks on this submit.
        submit_can_finish.wait(timeout=0.75)
        submit_completed[0] = True
        return "SABnzbd_nzo_submitted_late", None

    def queued_job(_title):
        if not first_probe_at:
            first_probe_at.append(_time.perf_counter())
        queue_seen.set()
        return {
            "nzo_id": "SABnzbd_nzo_existing_queue",
            "name": "movie.mkv",
            "status": "Downloading",
        }

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = queued_job
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    try:
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
        submit_completed_at_return = submit_completed[0]
    finally:
        submit_can_finish.set()

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_existing_queue", None)
    monitor.waitForAbort.assert_not_called()
    # Structural proof (load-independent): the existing-queue hit must be
    # adopted before the slow submit worker is released (only the finally
    # above releases it, after the snapshot). If adoption wrongly waited out
    # a fixed grace, delayed_submit would finish its bounded wait and set
    # submit_completed -> submit_completed_at_return True.
    assert submit_completed_at_return is False
    # No-initial-probe-delay guard (CodeRabbit): the load-independent PRIMARY
    # check is that the production constant is still zero -- it catches ANY
    # reintroduced non-zero initial grace, including a small one (e.g. 0.05) that
    # the coarse runtime ceiling below would let slip. The worker does
    # `queue_stop.wait(_SUBMIT_QUEUE_PROBE_INITIAL_DELAY_SECONDS)` before its
    # first find_queued_by_name, so a non-zero value delays the FIRST probe --
    # which the submit_completed snapshot above cannot see.
    from resources.lib.resolver import (  # pylint: disable=import-outside-toplevel
        _SUBMIT_QUEUE_PROBE_INITIAL_DELAY_SECONDS,
    )

    assert _SUBMIT_QUEUE_PROBE_INITIAL_DELAY_SECONDS == 0
    # Non-vacuity: the queue probe actually ran, so the adoption above was
    # genuinely exercised. The previous 0.1s wall-clock ceiling on the first
    # probe was removed (Codex P2): it measured thread creation + worker
    # scheduling, so a loaded runner could exceed it even with a healthy zero
    # initial delay -- the constant pin above is the load-independent guard.
    assert first_probe_at, "queue probe never ran"


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_rechecks_queue_quickly_after_initial_fast_miss(
    mock_submit, mock_find_queued, _mock_find_completed
):
    """A queue hit just after the first probe should not wait 200 ms."""
    submit_can_finish = threading.Event()
    queue_probe_times = []
    submit_completed = [False]

    def delayed_submit(_nzb_url, _title):
        # Slow op: blocks until the test releases submit_can_finish in its
        # finally (AFTER snapshotting the flag below). submit_completed flips
        # True only once this wait returns, so a fast path that adopts the
        # queue hit without awaiting the worker observes it still False --
        # load-independent. The 0.75s cap only bounds a regression that waits.
        submit_can_finish.wait(timeout=0.75)
        submit_completed[0] = True
        return "SABnzbd_nzo_submitted", None

    def queued_job(_title):
        queue_probe_times.append(_time.perf_counter())
        if len(queue_probe_times) == 1:
            return None
        return {
            "nzo_id": "SABnzbd_nzo_second_fast_probe",
            "name": "movie.mkv",
            "status": "Downloading",
        }

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = queued_job
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    # Record the actual interval the probe worker waits between probes by
    # capturing queue_stop.wait(interval) directly (Codex P2) -- a load-robust
    # signal that does not depend on scheduler-delayed wall-clock gaps.
    recorded_probe_waits = []

    class _RecordingEvent(threading.Event):
        def wait(self, timeout=None):
            recorded_probe_waits.append(timeout)
            return super().wait(timeout)

    try:
        with patch("resources.lib.resolver.threading.Event", _RecordingEvent):
            nzo_id, submit_error = _submit_nzb_with_ui_pump(
                "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
            )
        submit_completed_at_return = submit_completed[0]
    finally:
        submit_can_finish.set()

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_second_fast_probe", None)
    assert len(queue_probe_times) == 2
    # Cadence guard (CodeRabbit Major + Codex P2): two probes happening is not
    # enough -- the second probe must have been paced by the FAST retry interval,
    # not the normal slow poll. A regression dropping the recheck to the slow poll
    # (still < the 0.75s submit timeout) keeps len == 2 and the submit-not-
    # completed snapshot green. Instead of measuring the scheduler-delayed inter-
    # probe wall-clock gap (which flakes under load), the recording Event above
    # captured the actual interval the worker passed to queue_stop.wait(); assert
    # the slow interval was never used to pace a reprobe.
    from resources.lib.resolver import (  # pylint: disable=import-outside-toplevel
        _SUBMIT_QUEUE_PROBE_FAST_INTERVAL_SECONDS,
        _SUBMIT_QUEUE_PROBE_FAST_WINDOW_SECONDS,
        _SUBMIT_QUEUE_PROBE_INTERVAL_SECONDS,
    )

    # Premise pin (load-independent): the fast cadence only applies while elapsed
    # < the fast window; shrinking the window to an intermediate value would end
    # the fast cadence prematurely. Pin the documented window.
    assert _SUBMIT_QUEUE_PROBE_FAST_WINDOW_SECONDS >= 2.0

    # Every reprobe-pacing wait must be the fast interval. Waits below it (the
    # 0.0 initial-delay wait and the <=0.01 history-probe coordination polls) are
    # not cadence; any wait at or above the fast interval that isn't exactly the
    # fast interval -- the slow poll OR an intermediate value like 0.1 -- is a
    # cadence regression (CodeRabbit). The healthy run records only [~0.003, 0.01,
    # 0.05], so the fast interval is the sole >= -fast value.
    reprobe_interval_waits = [
        timeout
        for timeout in recorded_probe_waits
        if timeout and timeout >= _SUBMIT_QUEUE_PROBE_FAST_INTERVAL_SECONDS
    ]
    assert reprobe_interval_waits, "probe worker never paced a reprobe"
    assert all(
        timeout == _SUBMIT_QUEUE_PROBE_FAST_INTERVAL_SECONDS
        for timeout in reprobe_interval_waits
    ), (
        "reprobe used a non-fast interval (slow poll or intermediate value); "
        "intervals >= fast were {} (fast={:.3f}s, slow={:.3f}s)".format(
            reprobe_interval_waits,
            _SUBMIT_QUEUE_PROBE_FAST_INTERVAL_SECONDS,
            _SUBMIT_QUEUE_PROBE_INTERVAL_SECONDS,
        )
    )
    # Structural proof (load-independent): the second queue probe's hit is
    # adopted and returned while the submit worker is still blocked on
    # submit_can_finish (released only in the finally above, after this
    # snapshot). A regression that waits for the submit worker would let it
    # complete first -> submit_completed_at_return True.
    assert submit_completed_at_return is False


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_starts_history_probe_after_fast_queue_miss(
    mock_submit, mock_find_queued, mock_find_completed
):
    """A fast queue miss should let completed-history adoption skip the grace."""
    submit_can_finish = threading.Event()
    queue_probe_times = []
    history_probe_times = []

    def delayed_submit(_nzb_url, _title):
        submit_can_finish.wait(timeout=0.75)
        return "SABnzbd_nzo_submitted", None

    def fast_queue_miss(_title):
        queue_probe_times.append(_time.perf_counter())

    def completed_history(_title):
        history_probe_times.append(_time.perf_counter())
        return {
            "nzo_id": "SABnzbd_nzo_completed_fast_history",
            "name": "movie.mkv",
            "status": "Completed",
        }

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = fast_queue_miss
    mock_find_completed.side_effect = completed_history
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    try:
        # Push the parallel grace far above the submit worker's 0.75s timeout
        # so the completed-history probe cannot adopt by waiting the grace out:
        # it can only fire (and win adoption) via the fast queue-miss handoff
        # (first_queue_probe_done). A regression that waits the grace would
        # never reach the history probe before the submit worker returns, so
        # adoption falls through to the submitted nzo_id and history never runs.
        with patch(
            "resources.lib.resolver._SUBMIT_HISTORY_PROBE_PARALLEL_GRACE_SECONDS",
            5.0,
        ):
            nzo_id, submit_error = _submit_nzb_with_ui_pump(
                "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
            )
    finally:
        submit_can_finish.set()

    # Structural handoff guard (replaces wall-clock bounds that sat only ~0.02s
    # below the grace): with the grace disabled, history can only have fired —
    # and adoption can only resolve to the completed row — via the queue-miss
    # handoff, not by waiting out the grace.
    assert history_probe_times, (
        "completed-history probe never fired; it waited out the grace instead "
        "of starting on the fast queue-miss handoff"
    )
    assert (nzo_id, submit_error) == ("SABnzbd_nzo_completed_fast_history", None)


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name", return_value=None)
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_rechecks_completed_history_quickly_after_initial_miss(
    mock_submit, _mock_find_queued, mock_find_completed
):
    """Completed jobs appearing after the first miss should use the fast cadence."""
    submit_can_finish = threading.Event()
    history_probe_times = []

    def delayed_submit(_nzb_url, _title):
        submit_can_finish.wait(timeout=0.75)
        return "SABnzbd_nzo_submitted", None

    def completed_history_on_second_probe(_title):
        history_probe_times.append(_time.perf_counter())
        if len(history_probe_times) == 1:
            return None
        return {
            "nzo_id": "SABnzbd_nzo_second_history_probe",
            "name": "movie.mkv",
            "status": "Completed",
        }

    mock_submit.side_effect = delayed_submit
    mock_find_completed.side_effect = completed_history_on_second_probe
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    started = _time.perf_counter()
    try:
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
    finally:
        submit_can_finish.set()
    elapsed = _time.perf_counter() - started
    history_gap = history_probe_times[1] - history_probe_times[0]

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_second_history_probe", None)
    assert (
        elapsed < 0.2
    ), "second completed-history probe took {:.3f}s; gap was {:.3f}s".format(
        elapsed, history_gap
    )


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name")
def test_timeout_adoption_overlaps_completed_history_with_slow_queue_miss(
    mock_find_queued, mock_find_completed
):
    """Submit-timeout adoption should not serialize history behind queue miss."""
    from resources.lib.resolver import _adopt_queued_or_completed_job

    queue_running = threading.Event()
    queue_finished = threading.Event()
    history_during_queue = []

    def slow_queue_miss(_title):
        queue_running.set()
        _time.sleep(0.12)
        queue_finished.set()

    def completed_history(_title):
        # Record whether the slow queue probe was still in flight when the
        # history probe ran. Parallel adoption => queue running, not finished;
        # a serialized path would only reach history after the queue miss
        # returned (queue_finished set).
        history_during_queue.append(
            queue_running.is_set() and not queue_finished.is_set()
        )
        return {
            "nzo_id": "SABnzbd_nzo_completed_timeout",
            "name": "movie.mkv",
            "status": "Completed",
        }

    mock_find_queued.side_effect = slow_queue_miss
    mock_find_completed.side_effect = completed_history
    monitor = _make_monitor()

    nzo_id = _adopt_queued_or_completed_job("movie.mkv", monitor)

    assert nzo_id == "SABnzbd_nzo_completed_timeout"
    # Structural overlap guard (replaces a flake-prone wall-clock bound: the
    # 0.12s queue miss sits below the ~0.09-0.15s jitter floor). History runs
    # concurrently with the in-flight queue miss, not serialized after it.
    assert history_during_queue == [True], (
        "completed-history probe ran only after the slow queue miss finished "
        "(serialized adoption): {}".format(history_during_queue)
    )


@patch("resources.lib.resolver.probe_webdav_reachable")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
def test_poll_once_catches_late_active_queue_completed_history(
    mock_status, mock_history, mock_probe_webdav
):
    """A stale late-stage queue row should not cost a full poll interval."""
    history_started = threading.Event()
    history_may_finish = threading.Event()

    def late_active_status(_nzo_id):
        assert history_started.wait(timeout=1)
        history_may_finish.set()
        return {"status": "Downloading", "percentage": "96.0"}

    def completed_history(_nzo_id):
        history_started.set()
        assert history_may_finish.wait(timeout=1)
        _time.sleep(0.015)
        return {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        }

    mock_status.side_effect = late_active_status
    mock_history.side_effect = completed_history

    started = _time.perf_counter()
    job_status, history, error = _poll_once(
        "SABnzbd_nzo_primary", "movie.mkv", _make_monitor()
    )
    elapsed = _time.perf_counter() - started

    assert error is None
    assert job_status["status"] == "Downloading"
    assert history and history["status"] == "Completed", (
        "late completed history missed after {:.3f}s; resolver would wait for "
        "the next poll interval".format(elapsed)
    )
    assert elapsed < 0.5, "late-history catch took {:.3f}s".format(elapsed)
    mock_probe_webdav.assert_not_called()


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_rechecks_queue_while_history_miss_is_slow(
    mock_submit, mock_find_queued, mock_find_completed
):
    """A slow history miss must not block the fast queue adoption cadence."""
    submit_can_finish = threading.Event()
    queue_probe_times = []
    history_running = threading.Event()
    history_finished = threading.Event()
    second_probe_during_history = []

    def delayed_submit(_nzb_url, _title):
        submit_can_finish.wait(timeout=0.75)
        return "SABnzbd_nzo_submitted", None

    def queued_job(_title):
        queue_probe_times.append(_time.perf_counter())
        if len(queue_probe_times) == 1:
            return None
        # The second queue probe must fire while the slow history miss is
        # still running; a cadence serialized behind history could only run
        # after history returned (history_finished set).
        second_probe_during_history.append(
            history_running.is_set() and not history_finished.is_set()
        )
        return {
            "nzo_id": "SABnzbd_nzo_second_fast_probe",
            "name": "movie.mkv",
            "status": "Downloading",
        }

    def slow_history_miss(_title):
        history_running.set()
        _time.sleep(0.18)
        history_finished.set()

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = queued_job
    mock_find_completed.side_effect = slow_history_miss
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    try:
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
    finally:
        submit_can_finish.set()

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_second_fast_probe", None)
    assert len(queue_probe_times) == 2
    # Structural cadence guard (replaces a flake-prone wall-clock bound: the
    # 0.05s fast interval and the 0.18s history miss are too close given
    # ~0.09s jitter). The second queue probe runs while the history miss is
    # still in flight, proving the queue cadence is not serialized behind it.
    assert second_probe_during_history == [
        True
    ], "second queue probe was serialized behind the slow history miss: " "{}".format(
        second_probe_during_history
    )


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.find_queued_by_name", return_value=None)
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_wakes_when_submit_finishes_before_adoption_tick(
    mock_submit, _mock_find_queued, _mock_find_completed
):
    """Fast addurl success should not wait for the next 50 ms adoption tick."""

    def fast_submit(_nzb_url, _title):
        _time.sleep(0.02)
        return "SABnzbd_nzo_fast_submit", None

    mock_submit.side_effect = fast_submit
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    # Blow up the adoption-check interval so that, *if* the pump waited a full
    # adoption tick instead of waking on submit completion, this call would
    # take ~5 s. Waking on submit_done returns within one 0.01 s re-check
    # regardless of the interval, so the generous < 1.0 s bound cannot flake.
    started = _time.perf_counter()
    with patch("resources.lib.resolver._SUBMIT_ADOPTION_CHECK_INTERVAL_SECONDS", 5.0):
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
    elapsed = _time.perf_counter() - started

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_fast_submit", None)
    # The pump must wake on submit_done via the activity event, never on a
    # blocking Kodi wait tick.
    monitor.waitForAbort.assert_not_called()
    assert elapsed < 1.0, (
        "fast submit did not wake on submit completion (waited the adoption "
        "tick); elapsed={:.3f}s".format(elapsed)
    )


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_fast_submit_skips_slow_probe_cleanup_wait(
    mock_submit, mock_find_queued, _mock_find_completed
):
    """Fast addurl success should not wait for a stale slow adoption probe."""
    queue_probe_started = threading.Event()
    release_queue_probe = threading.Event()
    queue_probe_finished = threading.Event()

    def slow_queue_miss(_title):
        try:
            queue_probe_started.set()
            release_queue_probe.wait(timeout=0.25)
            return None
        finally:
            queue_probe_finished.set()

    def fast_submit(_nzb_url, _title):
        assert queue_probe_started.wait(timeout=1)
        _time.sleep(0.02)
        return "SABnzbd_nzo_fast_submit", None

    mock_find_queued.side_effect = slow_queue_miss
    mock_submit.side_effect = fast_submit
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    started = _time.perf_counter()
    try:
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
        elapsed = _time.perf_counter() - started
    finally:
        release_queue_probe.set()
        queue_probe_finished.wait(timeout=1)

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_fast_submit", None)
    assert (
        elapsed < 0.2
    ), "fast submit waited for slow adoption probe cleanup; elapsed={:.3f}s".format(
        elapsed
    )


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name", return_value=None)
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_adopts_completed_history_without_submit_join_delay(
    mock_submit, _mock_find_queued, mock_find_completed
):
    """Completed history adoption should not wait on the slow addurl response."""
    submit_can_finish = threading.Event()

    def delayed_submit(_nzb_url, _title):
        submit_can_finish.wait(timeout=0.75)
        return "SABnzbd_nzo_submitted", None

    mock_submit.side_effect = delayed_submit
    mock_find_completed.return_value = {
        "nzo_id": "SABnzbd_nzo_completed_probe",
        "name": "movie.mkv",
        "status": "Completed",
    }
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = _make_monitor()

    started = _time.monotonic()
    try:
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
    finally:
        submit_can_finish.set()
    elapsed = _time.monotonic() - started

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_completed_probe", None)
    assert elapsed < 0.6
    for _ in range(20):
        if dialog.update.call_args_list:
            break
        _time.sleep(0.01)
    dialog.update.assert_any_call(
        100,
        "Already completed in nzbdav\nPreparing stream: movie.mkv",
    )


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_overlaps_completed_history_probe_with_slow_queue_miss(
    mock_submit, mock_find_queued, mock_find_completed
):
    """A slow queue miss should not block an already-visible history hit."""
    submit_can_finish = threading.Event()
    queue_running = threading.Event()
    queue_finished = threading.Event()
    history_during_queue = []

    def delayed_submit(_nzb_url, _title):
        submit_can_finish.wait(timeout=0.75)
        return "SABnzbd_nzo_submitted", None

    def slow_queue_miss(_title):
        queue_running.set()
        _time.sleep(0.14)
        queue_finished.set()

    def completed_history(_title):
        # The visible history hit must be adopted while the slow queue miss is
        # still in flight; a serialized path would only reach it after the
        # queue miss returned (queue_finished set).
        history_during_queue.append(
            queue_running.is_set() and not queue_finished.is_set()
        )
        return {
            "nzo_id": "SABnzbd_nzo_completed_probe",
            "name": "movie.mkv",
            "status": "Completed",
        }

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = slow_queue_miss
    mock_find_completed.side_effect = completed_history
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = _make_monitor()

    try:
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
    finally:
        submit_can_finish.set()

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_completed_probe", None)
    # Structural overlap guard (replaces a flake-prone wall-clock bound: the
    # 0.14s queue miss sits below the ~0.09-0.15s jitter floor). The visible
    # history hit is adopted while the queue miss is still running.
    assert history_during_queue == [True], (
        "completed-history probe ran only after the slow queue miss finished "
        "(serialized adoption): {}".format(history_during_queue)
    )


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_retries_queue_probe_quickly_after_initial_miss(
    mock_submit, mock_find_queued, _mock_find_completed
):
    """Jobs appearing just after the first probe should not wait one second."""
    second_probe_seen = threading.Event()

    def delayed_submit(_nzb_url, _title):
        assert second_probe_seen.wait(timeout=2)
        return None, {"status": "timeout", "message": "Timed out"}

    def queued_job(_title):
        if mock_find_queued.call_count == 1:
            return None
        second_probe_seen.set()
        return {
            "nzo_id": "SABnzbd_nzo_second_probe",
            "name": "movie.mkv",
            "status": "Downloading",
        }

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = queued_job
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    started = _time.monotonic()
    nzo_id, submit_error = _submit_nzb_with_ui_pump(
        "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
    )
    elapsed = _time.monotonic() - started

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_second_probe", None)
    assert elapsed < 0.6


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_keeps_late_queue_probe_cadence_subsecond(
    mock_submit, mock_find_queued, _mock_find_completed
):
    """Late queue adoption should not pay a one-second post-picker probe gap."""
    second_probe_seen = threading.Event()
    submit_can_finish = threading.Event()
    queue_probe_times = []

    def delayed_submit(_nzb_url, _title):
        second_probe_seen.wait(timeout=2)
        submit_can_finish.wait(timeout=0.75)
        return "SABnzbd_nzo_submitted_late", None

    def queued_job(_title):
        queue_probe_times.append(_time.perf_counter())
        if len(queue_probe_times) == 1:
            return None
        second_probe_seen.set()
        return {
            "nzo_id": "SABnzbd_nzo_late_probe",
            "name": "movie.mkv",
            "status": "Downloading",
        }

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = queued_job
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)

    started = _time.perf_counter()
    try:
        with patch(
            "resources.lib.resolver._SUBMIT_QUEUE_PROBE_FAST_WINDOW_SECONDS", 0.0
        ):
            nzo_id, submit_error = _submit_nzb_with_ui_pump(
                "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
            )
    finally:
        submit_can_finish.set()
    elapsed = _time.perf_counter() - started
    probe_gap = queue_probe_times[1] - queue_probe_times[0]

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_late_probe", None)
    assert (
        elapsed < 0.5
    ), "late queue adoption took {:.3f}s; probe gap was {:.3f}s".format(
        elapsed, probe_gap
    )


@patch("resources.lib.resolver._get_submit_timeout_seconds", return_value=300)
@patch("resources.lib.resolver.find_queued_by_name", return_value=None)
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_reads_submit_timeout_once_per_attempt(
    mock_submit, _mock_find_queued, mock_submit_timeout
):
    submit_started = threading.Event()

    def delayed_submit(_nzb_url, _title):
        submit_started.set()
        _time.sleep(0.03)
        return "SABnzbd_nzo_submitted", None

    mock_submit.side_effect = delayed_submit
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.abortRequested.return_value = False

    nzo_id, submit_error = _submit_nzb_with_ui_pump(
        "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
    )

    assert submit_started.is_set()
    assert (nzo_id, submit_error) == ("SABnzbd_nzo_submitted", None)
    mock_submit_timeout.assert_called_once_with()


@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=0)
@patch("resources.lib.stream_proxy.get_proxy")
@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_submit_timeout_adopts_queued_nzo_id(
    mock_poll,
    mock_find_video,
    mock_validate,
    mock_stream_url,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_find_queued,
    mock_find_completed,
    mock_get_proxy,
    mock_service_port,
):
    """When submit_nzb returns a timeout sentinel, the resolver probes
    nzbdav's queue and adopts the existing nzo_id instead of retrying
    the submit. This is the fix for the observed bug where a big NZB
    that nzbdav had already accepted would be re-submitted and either
    bounce as a duplicate or orphan the first job."""
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = (None, {"status": "timeout", "message": "Timed out"})
    # First call: pre-submit "already completed" check — nothing there.
    # Subsequent calls from the adopt helper also return None, so the
    # queue hit is what ends up winning.
    mock_find_completed.return_value = None
    mock_find_queued.return_value = {
        "nzo_id": "SABnzbd_nzo_already_queued",
        "name": "movie.mkv",
        "status": "Downloading",
    }
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "name": "movie",
    }
    mock_find_video.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )
    mock_validate.return_value = True
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_proxy = MagicMock()
    mock_proxy.prepare_stream.return_value = "http://127.0.0.1:57800/stream"
    mock_get_proxy.return_value = mock_proxy

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    # Only ONE submit — the adoption path must prevent further retries.
    assert mock_submit.call_count == 1
    # The queue probe fires at least once with the title as its argument.
    assert mock_find_queued.called
    assert mock_find_queued.call_args[0][0] == "movie.mkv"
    # Playback was resolved successfully (True) because the polling
    # loop proceeded against the adopted nzo_id.
    mock_plugin.setResolvedUrl.assert_called()
    resolve_call = mock_plugin.setResolvedUrl.call_args
    assert resolve_call[0][1] is True


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_submit_timeout_retries_when_queue_empty(
    mock_poll,
    mock_submit,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_find_queued,
    mock_find_completed,
):
    """If the queue probe comes up empty after a submit timeout, the
    resolver falls through to a genuine retry of submit_nzb — the
    first submit may have actually failed at the network level."""
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = (None, {"status": "timeout", "message": "Timed out"})
    mock_find_queued.return_value = None
    mock_find_completed.return_value = None
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    # Three submits (the normal max_submit_retries) because every one
    # timed out, and no queue/history match was ever found to adopt.
    assert mock_submit.call_count == 3
    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_job_failed(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = ("SABnzbd_nzo_abc123", None)
    mock_status.return_value = {"status": "Failed", "percentage": "0"}
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_user_cancels(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = ("SABnzbd_nzo_abc123", None)
    mock_status.return_value = {"status": "Downloading", "percentage": "50"}
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    dialog.iscanceled.return_value = True
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())


@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_no_nzb_url(mock_poll, mock_plugin, mock_gui):
    """Resolve with no NZB URL should fail immediately."""
    mock_poll.return_value = (2, 60)

    resolve(1, {"nzburl": "", "title": "movie.mkv"})

    mock_gui.Dialog.return_value.ok.assert_called_once()
    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.time")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_timeout(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_time,
    mock_xbmc,
):
    """Resolve should time out after download_timeout seconds."""
    mock_poll.return_value = (0.01, 5)  # 5 second timeout
    mock_submit.return_value = ("SABnzbd_nzo_abc123", None)
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = _make_monitor()
    poll_started = [False]

    def status_downloading(_nzo_id):
        poll_started[0] = True
        return {"status": "Downloading", "percentage": "10"}

    mock_status.side_effect = status_downloading

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    # Simulate time passing beyond timeout
    mock_time.time.side_effect = [0.0, 10.0]

    def _fake_monotonic():
        return 10.0 if poll_started[0] else 0.0

    mock_time.monotonic.side_effect = _fake_monotonic

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())
    # Check timeout dialog was shown
    mock_gui.Dialog.return_value.ok.assert_called()


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_deleted_status(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    """'Deleted' status should be treated as failure."""
    mock_poll.return_value = (2, 60)
    mock_submit.return_value = ("SABnzbd_nzo_abc123", None)
    mock_status.return_value = {"status": "Deleted", "percentage": "0"}
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())


# --- New tests ---


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_url_encoded_special_characters(
    mock_poll,
    mock_validate,
    mock_stream_url,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    """resolve() URL-decodes nzburl and title before passing to submit_nzb."""
    from urllib.parse import quote

    mock_poll.return_value = (2, 60)
    mock_submit.return_value = ("SABnzbd_nzo_xyz789", None)
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "name": "movie",
    }
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )
    mock_validate.return_value = True
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    raw_url = "http://hydra:5076/getnzb/abc?apikey=testkey&extra=foo bar"
    raw_title = "Spider-Man: No Way Home (2021) 1080p"
    encoded_url = quote(raw_url, safe="")
    encoded_title = quote(raw_title, safe="")

    resolve(1, {"nzburl": encoded_url, "title": encoded_title})

    submit_call_args = mock_submit.call_args[0]
    assert (
        "hydra:5076" in submit_call_args[0]
    ), "NZB URL should be decoded before submit"
    assert "Spider-Man" in submit_call_args[1], "Title should be decoded before submit"
    mock_plugin.setResolvedUrl.assert_called_once()


@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._wait_for_abort_or_timeout", return_value=False)
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_poll_interval_respected(
    mock_poll,
    mock_wait,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
):
    """resolve() waits between polls with the configured poll_interval."""
    poll_interval = 7
    # Tiny download_timeout. status stays "Downloading" forever here, so resolve()
    # only stops at its deadline. Its per-poll wait (_wait_for_abort_or_timeout)
    # is mocked instant, so the loop does NOT pace on poll_interval — it busy-spins
    # until the real-monotonic download_timeout deadline (or MAX_POLL_ITERATIONS).
    # 3600s meant ~17s of spinning before the iteration cap; a fractional deadline
    # bounds the wall clock directly. resolve() still calls the wait with
    # poll_interval each pass, so assert_called_with(monitor, poll_interval) is
    # unchanged. The wall time is the deadline, not the (machine-dependent)
    # iteration count, so this stays deterministic.
    mock_poll.return_value = (poll_interval, 0.1)
    mock_submit.return_value = ("SABnzbd_nzo_poll123", None)
    mock_status.return_value = {"status": "Downloading", "percentage": "50"}
    mock_history.return_value = None

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    mock_xbmc.Monitor.return_value = monitor

    resolve(1, {"nzburl": "http://hydra/getnzb/poll", "title": "polltest.mkv"})

    mock_wait.assert_called_with(monitor, poll_interval)
    monitor.waitForAbort.assert_not_called()


@patch("resources.lib.stream_proxy.get_service_proxy_port", return_value=0)
@patch("resources.lib.stream_proxy.get_proxy")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_status_transitions_queued_to_downloading_to_completed(
    mock_poll,
    mock_validate,
    mock_stream_url,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_get_proxy,
    mock_service_port,
):
    """resolve() handles Queued -> Downloading -> Completed via history."""
    mock_poll.return_value = (1, 3600)
    mock_submit.return_value = ("SABnzbd_nzo_trans456", None)
    mock_status.side_effect = [
        {"status": "Queued", "percentage": "0"},
        {"status": "Downloading", "percentage": "50"},
        None,  # No longer in queue when completed
    ]
    mock_history.side_effect = [
        None,
        None,
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/downloaded",
            "name": "downloaded",
        },
    ]
    mock_find.return_value = "/content/uncategorized/downloaded/downloaded.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/downloaded/downloaded.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )
    mock_validate.return_value = True
    mock_proxy = MagicMock()
    mock_proxy.prepare_stream.return_value = "http://127.0.0.1:57800/stream"
    mock_get_proxy.return_value = mock_proxy

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    mock_xbmc.Monitor.return_value = _make_monitor()

    resolve(1, {"nzburl": "http://hydra/getnzb/trans", "title": "downloaded.mkv"})

    assert (
        mock_history.call_count == 3
    ), "get_job_history should be polled three times before completing"
    mock_plugin.setResolvedUrl.assert_called_once()
    resolve_call = mock_plugin.setResolvedUrl.call_args
    assert resolve_call[0][1] is True


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_dialog_closed_on_submit_exception(
    mock_poll, mock_submit, mock_plugin, mock_gui, mock_xbmc, mock_find
):
    """A crashed submit_nzb must not leave the progress dialog open and
    must not strand Kodi on the plugin handle. The worker-thread
    isolation added with the UI-pump helper now catches the exception
    inside the worker, logs it, and surfaces as a normal submit
    failure — so the specific 'Error: <message>' dialog that the
    old propagate-to-outer-try path produced no longer fires.
    What's still asserted: dialog.close, handle resolved False, and
    the final failure dialog (string 30098) did fire."""
    mock_poll.return_value = (2, 60)
    mock_find.return_value = None
    mock_submit.side_effect = RuntimeError("unexpected crash")
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = MagicMock()
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    dialog.close.assert_called()
    mock_plugin.setResolvedUrl.assert_called_once_with(1, False, mock_gui.ListItem())
    # The three-retry submit loop fired the terminal failure dialog.
    assert mock_gui.Dialog.return_value.ok.called


@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._poll_until_ready")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_and_play_exception_dialog_preserves_long_message(
    mock_poll, mock_poll_until_ready, mock_gui
):
    """Unexpected direct-play errors should show the full dialog message."""
    mock_poll.return_value = (2, 60)
    error_message = "direct playback crash " + ("details " * 20)
    mock_poll_until_ready.side_effect = RuntimeError(error_message)

    resolve_and_play("http://hydra/getnzb/abc", "movie.mkv")

    mock_gui.Dialog.return_value.ok.assert_called_once_with(
        "NZB-DAV", "Error: {}".format(error_message)
    )


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_max_iterations_safeguard(
    mock_poll,
    mock_submit,
    mock_status,
    mock_history,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_find,
):
    """Resolve loop exits after MAX_POLL_ITERATIONS even without timeout."""
    mock_poll.return_value = (0, 999999)  # Very long timeout, 0s interval
    mock_find.return_value = None
    mock_submit.return_value = ("SABnzbd_nzo_stuck", None)
    mock_status.return_value = {"status": "Queued", "percentage": "0"}
    mock_history.return_value = None
    mock_xbmc.Monitor.return_value = MagicMock()
    mock_xbmc.Monitor.return_value.waitForAbort.return_value = False

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    with patch("resources.lib.resolver.MAX_POLL_ITERATIONS", 2):
        resolve(1, {"nzburl": "http://hydra/getnzb/stuck", "title": "stuck.mkv"})

    mock_plugin.setResolvedUrl.assert_called_once()
    assert mock_plugin.setResolvedUrl.call_args[0][1] is False
    assert mock_status.call_count <= 2


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.xbmc")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.xbmcplugin")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver._get_poll_settings")
def test_resolve_retries_submit_on_transient_failure(
    mock_poll,
    mock_validate,
    mock_stream_url,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_plugin,
    mock_gui,
    mock_xbmc,
    mock_find_completed,
):
    """resolve should retry submit_nzb if it fails the first time."""
    mock_poll.return_value = (2, 60)
    mock_find_completed.return_value = None
    # First call fails, second succeeds
    mock_submit.side_effect = [(None, None), ("SABnzbd_nzo_retry123", None)]
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "name": "movie",
    }
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = (
        "http://webdav:8080/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic dXNlcjpwYXNz"},
    )
    mock_validate.return_value = True
    mock_xbmc.Monitor.return_value = MagicMock()
    mock_xbmc.Monitor.return_value.waitForAbort.return_value = False

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    mock_gui.DialogProgress.return_value = dialog

    resolve(1, {"nzburl": "http://hydra/getnzb/abc", "title": "movie.mkv"})

    assert mock_submit.call_count == 2


# --- _poll_until_ready() tests ---


def _make_dialog(canceled=False):
    """Return a mock DialogProgress with iscanceled set."""
    dialog = MagicMock()
    dialog.iscanceled.return_value = canceled
    return dialog


@patch("resources.lib.resolver.probe_webdav_reachable")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
def test_poll_once_returns_completed_history_before_slow_queue(
    mock_status, mock_history, mock_probe
):
    """Completed history is enough to continue resolving; do not wait on queue."""

    def slow_status(_nzo_id):
        _time.sleep(0.25)
        return {"status": "Downloading", "percentage": "99"}

    mock_status.side_effect = slow_status
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }

    started = _time.monotonic()
    job_status, history, webdav_error = _poll_once("nzo_done", "movie", _make_monitor())
    elapsed = _time.monotonic() - started

    assert elapsed < 0.2
    assert job_status is None
    assert history == mock_history.return_value
    assert webdav_error is None
    mock_probe.assert_not_called()


@patch("resources.lib.resolver.probe_webdav_reachable")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
def test_poll_once_returns_active_queue_before_slow_history(
    mock_status, mock_history, mock_probe
):
    """An active queue row is enough to update progress and start the next poll."""

    # Load-independent structural guard: the history fetch blocks on an event
    # the test releases only in finally, AFTER the fast-path result is captured.
    # If _poll_once returns without awaiting history, history_completed stays
    # False at the snapshot. A regression that blocks on history would run
    # slow_history to completion (setting the flag) before returning -> red.
    release = threading.Event()
    history_completed = {"done": False}

    def slow_history(_nzo_id):
        release.wait(timeout=2)
        history_completed["done"] = True

    mock_status.return_value = {"status": "Downloading", "percentage": "50"}
    mock_history.side_effect = slow_history

    try:
        job_status, history, webdav_error = _poll_once(
            "nzo_active", "movie", _make_monitor()
        )
        # The fast active-queue path must return before the slow history fetch
        # has completed.
        assert not history_completed["done"]
    finally:
        release.set()

    assert job_status == mock_status.return_value
    assert history is None
    assert webdav_error is None
    mock_probe.assert_not_called()


@patch("resources.lib.resolver.probe_webdav_reachable")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
def test_poll_once_failed_history_wins_over_fast_stale_active_queue(
    mock_status, mock_history, mock_probe
):
    """A stale active queue row must not hide a terminal failed history row."""

    def slightly_slow_failed_history(_nzo_id):
        _time.sleep(0.01)
        return {
            "status": "Failed",
            "fail_message": "article not found",
        }

    mock_status.return_value = {"status": "Downloading", "percentage": "50"}
    mock_history.side_effect = slightly_slow_failed_history

    job_status, history, webdav_error = _poll_once(
        "nzo_failed", "movie", _make_monitor()
    )

    assert job_status == mock_status.return_value
    assert history == {
        "status": "Failed",
        "fail_message": "article not found",
    }
    assert webdav_error is None
    mock_probe.assert_not_called()


@patch("resources.lib.resolver.probe_webdav_reachable")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
def test_poll_once_returns_full_progress_queue_before_slow_history(
    mock_status, mock_history, mock_probe
):
    """A 100% queue row should not block behind a slow history request."""

    # Slow path is pushed far above the bound so the assertion stays
    # red-on-regression: the legitimate path returns after the ~0.14s
    # full-progress grace, while a regression that blocks on history would
    # take ~2s and fail the widened bound. The wide gap (0.14s grace vs 2.0s
    # block, bound 1.0s) makes the wall-clock check load-robust.
    def slow_history(_nzo_id):
        _time.sleep(2.0)

    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.side_effect = slow_history

    started = _time.monotonic()
    job_status, history, webdav_error = _poll_once("nzo_full", "movie", _make_monitor())
    elapsed = _time.monotonic() - started

    assert elapsed < 1.0, "100% queue row waited on history for {:.3f}s".format(elapsed)
    assert job_status == mock_status.return_value
    assert history is None
    assert webdav_error is None
    mock_probe.assert_not_called()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver._validate_stream_url", return_value=True)
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_success(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_stream_url,
    mock_validate,
    mock_find_completed,
):
    """_poll_until_ready returns (url, headers) when download completes."""
    mock_submit.return_value = ("nzo_abc", None)
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = ("http://webdav/movie.mkv", {"Authorization": "x"})
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url == "http://webdav/movie.mkv"
    assert headers == {"Authorization": "x"}


@patch("resources.lib.resolver.record_download")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver._validate_stream_url", return_value=True)
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_records_pubdate_on_submit_success(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_stream_url,
    mock_validate,
    mock_find_completed,
    mock_record,
):
    """On a fresh submit, the selected result's pubdate is recorded against
    the title so the picker can later tell this download apart from a
    same-name repost posted on a different day."""
    mock_submit.return_value = ("nzo_abc", None)
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = ("http://webdav/movie.mkv", {"Authorization": "x"})
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, _ = _poll_until_ready(
        "http://hydra/nzb",
        "movie",
        _make_dialog(),
        2,
        3600,
        poll_ctx=PollContext(
            download_pubdate="Wed, 15 Dec 2021 12:00:00 +0000",
            download_size="1000",
        ),
    )

    assert url == "http://webdav/movie.mkv"
    mock_record.assert_called_once_with(
        "movie", "Wed, 15 Dec 2021 12:00:00 +0000", "1000"
    )


@patch("resources.lib.resolver.record_download")
@patch(
    "resources.lib.resolver._existing_completed_stream",
    return_value=("http://webdav/cached.mkv", {"Authorization": "x"}),
)
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_cache_hit_does_not_record(
    mock_xbmc, mock_submit, mock_existing, mock_record
):
    """A cache hit (already-completed row) returns before any submit, so it
    must not record a pubdate (nothing was downloaded this time)."""
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, _ = _poll_until_ready(
        "http://hydra/nzb",
        "movie",
        _make_dialog(),
        2,
        3600,
        poll_ctx=PollContext(
            download_pubdate="Wed, 15 Dec 2021 12:00:00 +0000",
        ),
    )

    assert url == "http://webdav/cached.mkv"
    mock_record.assert_not_called()
    mock_submit.assert_not_called()


@patch("resources.lib.resolver._handle_history_result")
@patch("resources.lib.resolver._poll_once")
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value="nzo_abc")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_uses_nonblocking_abort_check_between_polls(
    mock_xbmc,
    _mock_submit,
    mock_poll_once,
    mock_handle_history,
):
    def blocking_wait_for_abort(_seconds):
        _time.sleep(0.25)
        return False

    monitor = MagicMock()
    monitor.abortRequested.return_value = False
    monitor.waitForAbort.side_effect = blocking_wait_for_abort
    mock_xbmc.Monitor.return_value = monitor
    mock_poll_once.side_effect = [
        ({"status": "Downloading", "percentage": "1"}, None, None),
        (None, {"status": "Completed"}, None),
    ]
    mock_handle_history.side_effect = [
        (False, None, None, 0),
        (False, "http://webdav/movie.mkv", {"Authorization": "x"}, 0),
    ]

    started = _time.monotonic()
    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 0.01, 3600
    )
    elapsed = _time.monotonic() - started

    assert url == "http://webdav/movie.mkv"
    assert headers == {"Authorization": "x"}
    assert elapsed < 0.5
    monitor.waitForAbort.assert_not_called()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver._validate_stream_url")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_skips_non_gate_stream_validation(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_find,
    mock_stream_url,
    mock_validate,
    mock_find_completed,
):
    """Completed-history playback should not wait on an advisory HEAD probe."""
    mock_submit.return_value = ("nzo_abc", None)
    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.return_value = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = ("http://webdav/movie.mkv", {"Authorization": "x"})
    mock_validate.side_effect = AssertionError("validation should be skipped")
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url == "http://webdav/movie.mkv"
    assert headers == {"Authorization": "x"}
    mock_validate.assert_not_called()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value="nzo_abc")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_graces_nearly_complete_queue_for_history(
    mock_xbmc,
    mock_status,
    mock_history,
    mock_find,
    mock_stream_url,
    mock_submit,
    mock_find_completed,
):
    """A 99% queue row should not force a full extra poll before WebDAV discovery."""
    completed_history = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }
    status_calls = []
    history_calls = []

    def get_status(_nzo_id):
        status_calls.append(_time.perf_counter())
        if len(status_calls) > 1:
            _time.sleep(0.05)
        return {"status": "Downloading", "percentage": "99"}

    def get_history(_nzo_id):
        history_calls.append(_time.perf_counter())
        if len(history_calls) == 1:
            _time.sleep(0.05)
        return completed_history

    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)
    mock_xbmc.Monitor.return_value = monitor
    mock_status.side_effect = get_status
    mock_history.side_effect = get_history
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = ("http://webdav/movie.mkv", {"Authorization": "x"})

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 0.2, 3600
    )

    assert url == "http://webdav/movie.mkv"
    assert headers == {"Authorization": "x"}
    # Grace-hit-on-first-poll (replaces a flake-prone wall-clock bound: the
    # ~0.05s grace hit and the ~0.15s extra-poll regression are too close given
    # ~0.09s jitter). The completed history is caught within the nearly-complete
    # grace on the FIRST poll, so neither API is polled twice; a missed grace
    # forces a second poll (2 calls each) before WebDAV discovery.
    assert (
        len(status_calls) == 1
    ), "nearly-complete grace missed; status polled {} times".format(len(status_calls))
    assert (
        len(history_calls) == 1
    ), "nearly-complete grace missed; history polled {} times".format(
        len(history_calls)
    )
    # The count asserts alone cannot see a regression that poll-waits the full
    # 0.2s interval and then reads history in-place (counts stay 1 while the
    # forbidden startup delay returns, since waitForAbort sleeps in-band). Assert
    # no full poll-interval wait happened: the grace path reaches history via the
    # 0.1s grace / fast-repoll, never the 0.2s poll passed to _poll_until_ready.
    poll_waits = [c.args[0] for c in monitor.waitForAbort.call_args_list if c.args]
    assert 0.2 not in poll_waits, (
        "nearly-complete grace waited a full 0.2s poll before history; "
        "waitForAbort delays={}".format(poll_waits)
    )


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value="nzo_abc")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_waits_for_full_progress_history_before_poll_tick(
    mock_xbmc,
    mock_status,
    mock_history,
    mock_find,
    mock_stream_url,
    mock_submit,
    mock_find_completed,
):
    """A 100% queue row should not miss history and sleep a full poll tick."""
    completed_history = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }
    history_calls = []

    mock_status.return_value = {"status": "Downloading", "percentage": "100"}

    def get_history(_nzo_id):
        history_calls.append(_time.perf_counter())
        # Simulate a small history-arrival latency that lands comfortably inside
        # _POLL_FULL_PROGRESS_HISTORY_GRACE_SECONDS (0.14). The earlier 0.12 sleep
        # sat only 0.02s under the grace, so under CPU load it stretched past 0.14
        # and forced a false 2nd poll. ~0.02 keeps a wide margin while still
        # exercising the grace-wait path.
        if len(history_calls) == 1:
            _time.sleep(0.02)
        return completed_history

    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)
    mock_xbmc.Monitor.return_value = monitor
    mock_history.side_effect = get_history
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = ("http://webdav/movie.mkv", {"Authorization": "x"})

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 0.25, 3600
    )

    assert url == "http://webdav/movie.mkv"
    assert headers == {"Authorization": "x"}
    # Structural red-on-regression signal (load-independent): the 100% grace
    # caught completed history on the FIRST poll, so history was fetched once and
    # no poll-tick / fast-repoll waitForAbort ran before resolving. If the
    # full-progress grace regresses (shrinks below the history latency), the 100%
    # row misses history on poll 1 and the loop sleeps a poll wait then re-fetches
    # history -> len(history_calls) == 2 and a poll wait appears.
    assert len(history_calls) == 1
    poll_waits = [c.args[0] for c in monitor.waitForAbort.call_args_list if c.args]
    assert not poll_waits, (
        "full-progress grace missed history on poll 1 and slept a poll wait "
        "before resolving; waitForAbort delays={}".format(poll_waits)
    )
    # Premise pin (load-independent): the structural proof above rides on the
    # ~0.02s simulated history latency landing inside the documented grace. An
    # intermediate grace reduction (e.g. 0.07) would still sit above 0.02 and
    # keep the len==1 / no-poll-wait snapshot green, yet shrink the real-world
    # safety margin. Pin the documented grace so such a drift goes red here.
    from resources.lib.resolver import (  # pylint: disable=import-outside-toplevel
        _POLL_FULL_PROGRESS_HISTORY_GRACE_SECONDS,
    )

    assert _POLL_FULL_PROGRESS_HISTORY_GRACE_SECONDS == 0.14


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value="nzo_abc")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver._wait_for_abort_or_timeout", return_value=False)
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_repolls_full_progress_history_miss_before_full_tick(
    mock_xbmc,
    mock_wait,
    mock_status,
    mock_history,
    mock_find,
    mock_stream_url,
    _mock_submit,
    _mock_find_completed,
):
    """A 100% queue row with a fast history miss should not wait a full tick."""
    from resources.lib.resolver import _POLL_NEAR_COMPLETE_FAST_REPOLL_SECONDS

    poll_interval = 1
    completed_history = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }
    history_calls = []

    mock_status.return_value = {"status": "Downloading", "percentage": "100"}

    def get_history(_nzo_id):
        history_calls.append(_nzo_id)
        if len(history_calls) == 1:
            return None
        return completed_history

    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    mock_xbmc.Monitor.return_value = monitor
    mock_history.side_effect = get_history
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = ("http://webdav/movie.mkv", {"Authorization": "x"})

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), poll_interval, 3600
    )

    assert url == "http://webdav/movie.mkv"
    assert headers == {"Authorization": "x"}
    # The full-progress miss forced exactly one repoll (2 history calls).
    assert len(history_calls) == 2
    # Structural, load-independent proof the repoll happened BEFORE the full
    # poll tick. The per-poll wait (_wait_for_abort_or_timeout) is mocked, so it
    # never really sleeps -- there is no wall-clock bound. The 100% (full
    # progress) row must pace the single inter-poll wait on the near-complete
    # fast-repoll interval, strictly shorter than the full poll_interval the
    # slow path would have waited. A regression that drops the near-complete
    # fast repoll would call this with poll_interval and fail the assertion.
    expected_wait = min(poll_interval, _POLL_NEAR_COMPLETE_FAST_REPOLL_SECONDS)
    mock_wait.assert_called_once_with(monitor, expected_wait)
    assert expected_wait < poll_interval


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.get_job_history", return_value=None)
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_user_cancel(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_find_completed,
    mock_cancel_job,
):
    """_poll_until_ready returns (None, None) when the user cancels."""
    mock_status.return_value = {"status": "Downloading", "percentage": "50"}
    mock_xbmc.Monitor.return_value = _make_monitor()

    dialog = _make_dialog(canceled=True)
    url, headers = _poll_until_ready("http://hydra/nzb", "movie", dialog, 2, 3600)

    assert url is None
    assert headers is None


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.get_job_history", return_value=None)
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.time")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_timeout(
    mock_xbmc,
    mock_time,
    mock_submit,
    mock_status,
    mock_history,
    mock_gui,
    mock_find_completed,
    mock_cancel_job,
):
    """_poll_until_ready returns (None, None) and shows dialog on timeout."""
    mock_xbmc.Monitor.return_value = _make_monitor()
    poll_started = [False]

    def status_downloading(_nzo_id):
        poll_started[0] = True
        return {"status": "Downloading", "percentage": "10"}

    mock_status.side_effect = status_downloading
    mock_time.time.side_effect = [0.0, 10.0]

    def _fake_monotonic():
        return 10.0 if poll_started[0] else 0.0

    mock_time.monotonic.side_effect = _fake_monotonic

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 0.01, 5
    )

    assert url is None
    assert headers is None
    mock_gui.Dialog.return_value.ok.assert_called()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.get_job_history", return_value=None)
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_job_failed(
    mock_xbmc, mock_submit, mock_status, mock_history, mock_gui, mock_find_completed
):
    """_poll_until_ready returns (None, None) when job reports Failed."""
    mock_status.return_value = {"status": "Failed", "percentage": "0"}
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url is None
    assert headers is None
    mock_gui.Dialog.return_value.ok.assert_called()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch(
    "resources.lib.resolver.get_job_history",
    return_value={"status": "Failed"},
)
@patch("resources.lib.resolver.get_job_status", return_value=None)
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_history_failed(
    mock_xbmc, mock_submit, mock_status, mock_history, mock_gui, mock_find_completed
):
    """_poll_until_ready returns (None, None) when history shows Failed."""
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url is None
    assert headers is None
    mock_gui.Dialog.return_value.ok.assert_called()


@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_already_downloaded(
    mock_xbmc, mock_find_completed, mock_find_video, mock_stream_url
):
    """_poll_until_ready returns stream URL immediately if already downloaded."""
    mock_find_completed.return_value = {
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie"
    }
    mock_find_video.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = ("http://webdav/movie.mkv", {})

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url == "http://webdav/movie.mkv"


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch(
    "resources.lib.resolver.get_job_history",
    return_value={
        "status": "Failed",
        "fail_message": "CRC error in article " + ("details " * 30),
    },
)
@patch("resources.lib.resolver.get_job_status", return_value=None)
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_history_failed_shows_fail_message(
    mock_xbmc, mock_submit, mock_status, mock_history, mock_gui, mock_find_completed
):
    """_poll_until_ready shows the server's fail_message to the user."""
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url is None
    assert headers is None
    # Should show modal dialog with the actual fail_message
    mock_gui.Dialog.return_value.ok.assert_called_once()
    assert mock_gui.Dialog.return_value.ok.call_args[0][
        1
    ] == "CRC error in article " + ("details " * 30)


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.find_video_file", return_value=None)
@patch(
    "resources.lib.resolver.get_job_history",
    return_value={
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    },
)
@patch("resources.lib.resolver.get_job_status", return_value=None)
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_no_video_after_retries(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_find_video,
    mock_gui,
    mock_find_completed,
):
    """_poll_until_ready shows dialog when completed but no video found."""
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 0, 3600
    )

    assert url is None
    assert headers is None
    mock_gui.Dialog.return_value.ok.assert_called_once()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.find_video_file")
@patch(
    "resources.lib.resolver.get_job_history",
    return_value={
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    },
)
@patch("resources.lib.resolver.get_job_status", return_value=None)
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_rechecks_completed_webdav_before_full_poll_interval(
    mock_xbmc,
    _mock_submit,
    _mock_status,
    _mock_history,
    mock_find_video,
    _mock_gui,
    mock_stream_url,
    _mock_find_completed,
):
    """A just-completed job should not wait a full poll tick for WebDAV visibility."""
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)
    mock_xbmc.Monitor.return_value = monitor
    mock_find_video.side_effect = [
        None,
        "/content/uncategorized/movie/movie.mkv",
    ]
    mock_stream_url.return_value = (
        "http://webdav/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic primary"},
    )

    from resources.lib import (
        resolver as _resolver,
    )  # pylint: disable=import-outside-toplevel

    helper_poll_waits = []
    _real_wait_for_abort = _resolver._wait_for_abort_or_timeout

    def _spy_wait_for_abort(mon, wait_seconds, *args, **kwargs):
        helper_poll_waits.append(wait_seconds)
        return _real_wait_for_abort(mon, wait_seconds, *args, **kwargs)

    with patch.object(_resolver, "_wait_for_abort_or_timeout", _spy_wait_for_abort):
        url, headers = _poll_until_ready(
            "http://hydra/nzb", "movie", _make_dialog(), 1, 3600
        )

    assert url == "http://webdav/content/uncategorized/movie/movie.mkv"
    assert headers == {"Authorization": "Basic primary"}
    # Completed-WebDAV recheck guard (replaces a flake-prone wall-clock bound).
    # The first find_video_file miss must recheck inline on the graduated 0.025s
    # fast recheck delay AND return the video on that recheck -- it must NOT fall
    # through to a full poll_interval (1s) tick before consuming the second lookup.
    wait_delays = [c.args[0] for c in monitor.waitForAbort.call_args_list if c.args]
    assert 0.025 in wait_delays, "inline 0.025s fast recheck was not requested"
    # The full poll tick is requested two ways and BOTH must be absent before the
    # recheck resolves (Codex P2): monitor.waitForAbort(poll_interval), and the
    # helper _wait_for_abort_or_timeout(monitor, poll_interval), which waits on a
    # threading.Event rather than waitForAbort. A 0.025s recheck followed by
    # either would delay playback ~1s while still returning the right URL.
    assert 1 not in wait_delays, (
        "completed-WebDAV path waited a full poll tick (waitForAbort) before "
        "consuming the recheck result; delays={}".format(wait_delays)
    )
    assert 1 not in helper_poll_waits, (
        "completed-WebDAV path waited a full poll tick via "
        "_wait_for_abort_or_timeout; helper waits={}".format(helper_poll_waits)
    )


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch(
    "resources.lib.resolver.get_job_history",
    return_value={
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    },
)
@patch("resources.lib.resolver.get_job_status", return_value=None)
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_rechecks_completed_webdav_quickly_after_first_miss(
    mock_xbmc,
    _mock_submit,
    _mock_status,
    _mock_history,
    mock_find_video,
    mock_stream_url,
    _mock_find_completed,
):
    """A first WebDAV miss should not add a fixed 100 ms to video start."""
    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)
    mock_xbmc.Monitor.return_value = monitor
    mock_find_video.side_effect = [
        None,
        "/content/uncategorized/movie/movie.mkv",
    ]
    mock_stream_url.return_value = (
        "http://webdav/content/uncategorized/movie/movie.mkv",
        {"Authorization": "Basic primary"},
    )

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 1, 3600
    )

    assert url == "http://webdav/content/uncategorized/movie/movie.mkv"
    assert headers == {"Authorization": "Basic primary"}
    # Fast-recheck-delay guard (replaces a flake-prone wall-clock bound: the
    # forbidden fixed 0.1s wait overlaps the ~0.06-0.09s jitter floor). The
    # first WebDAV miss must recheck on the graduated 0.025s fast delay; a
    # reintroduced fixed-100ms wait would call waitForAbort(0.1) instead.
    monitor.waitForAbort.assert_any_call(0.025)
    waited = [c.args[0] for c in monitor.waitForAbort.call_args_list if c.args]
    assert 0.1 not in waited, (
        "completed-WebDAV recheck used the forbidden fixed 100ms delay instead "
        "of the 0.025s fast delay; waitForAbort delays were {}".format(waited)
    )


# --- HTTP error classification tests for the submit retry loop ---


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_submit_http_500_no_retry(
    mock_xbmc, mock_submit, mock_gui, mock_find_completed
):
    """When submit_nzb returns an HTTP 500 tuple, the retry loop must
    NOT retry — it must show the dialog with the error body and abort
    after a single submit attempt."""
    mock_submit.return_value = (
        None,
        {"status": 500, "message": "Internal Server Error: duplicate nzo_id"},
    )
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url is None
    assert headers is None
    assert mock_submit.call_count == 1  # critically: NOT 3
    mock_gui.Dialog.return_value.ok.assert_called_once()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_submit_http_502_retries_then_surfaces(
    mock_xbmc, mock_submit, mock_gui, mock_find_completed
):
    """When submit_nzb returns HTTP 502 (transient gateway error), the
    retry loop SHOULD retry up to 3x. After all retries exhaust, the
    final dialog surfaces the actual error body, not the generic
    'check your settings' string."""
    mock_submit.return_value = (
        None,
        {"status": 502, "message": "Bad Gateway: upstream timeout"},
    )
    mock_xbmc.Monitor.return_value = _make_monitor()

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url is None
    assert headers is None
    assert mock_submit.call_count == 3  # all 3 retries attempted
    mock_gui.Dialog.return_value.ok.assert_called_once()
    # The dialog text should contain the 502 error body, not the
    # generic string. Inspect the call args:
    call_args_text = str(mock_gui.Dialog.return_value.ok.call_args)
    assert "502" in call_args_text or "Bad Gateway" in call_args_text


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_submit_http_400_no_retry(
    mock_xbmc, mock_submit, mock_gui, mock_find_completed
):
    """4xx errors are also non-transient and skip the retry loop."""
    mock_submit.return_value = (
        None,
        {"status": 400, "message": "Bad Request: malformed nzburl"},
    )
    mock_xbmc.Monitor.return_value = _make_monitor()

    _poll_until_ready("http://hydra/nzb", "movie", _make_dialog(), 2, 3600)

    assert mock_submit.call_count == 1
    mock_gui.Dialog.return_value.ok.assert_called_once()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.submit_nzb")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_submit_connection_error_still_retries(
    mock_xbmc, mock_submit, mock_gui, mock_find_completed
):
    """(None, None) — non-HTTP transient — still retries 3x as before
    and shows the generic dialog after exhausting."""
    mock_submit.return_value = (None, None)
    mock_xbmc.Monitor.return_value = _make_monitor()

    _poll_until_ready("http://hydra/nzb", "movie", _make_dialog(), 2, 3600)

    assert mock_submit.call_count == 3  # full retry loop
    mock_gui.Dialog.return_value.ok.assert_called_once()


# --- cleanup-on-abort tests (Group A) ---


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.time")
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value="nzo_xyz")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_cleanup_on_timeout(
    mock_xbmc,
    mock_submit,
    mock_time,
    mock_gui,
    mock_find_completed,
    mock_cancel_job,
):
    """When the download_timeout fires, _poll_until_ready must call
    cancel_job(nzo_id) before returning."""
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_time.monotonic.side_effect = [0.0, 700.0]

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 600
    )

    assert url is None
    assert headers is None
    mock_cancel_job.assert_called_once_with("nzo_xyz")


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.get_job_history", return_value=None)
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_cleanup_on_user_cancel(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_gui,
    mock_find_completed,
    mock_cancel_job,
):
    """When the user cancels the resolve dialog, cancel_job must fire."""
    mock_status.return_value = {"status": "Downloading", "percentage": "10"}
    mock_xbmc.Monitor.return_value = _make_monitor()
    dialog = _make_dialog(canceled=True)

    url, headers = _poll_until_ready("http://hydra/nzb", "movie", dialog, 2, 3600)

    assert url is None
    assert headers is None
    mock_cancel_job.assert_called_once_with("nzo_xyz")


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.get_job_history", return_value=None)
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_cleanup_on_kodi_shutdown(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_gui,
    mock_find_completed,
    mock_cancel_job,
):
    """When Kodi shutdown is signaled during the poll wait, cancel_job
    must fire."""
    mock_status.return_value = {"status": "Downloading", "percentage": "10"}
    monitor = MagicMock()
    # First abort flag check returns False (initial poll wait), second returns
    # True (Kodi shutdown signal). The resolver intentionally does not call
    # waitForAbort here because that can wedge Kodi's RunScript resolver path.
    monitor.abortRequested.side_effect = [False, True]
    mock_xbmc.Monitor.return_value = monitor

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url is None
    assert headers is None
    mock_cancel_job.assert_called_once_with("nzo_xyz")
    monitor.waitForAbort.assert_not_called()


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.MAX_POLL_ITERATIONS", 2)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.get_job_history", return_value=None)
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_cleanup_on_max_iterations(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_gui,
    mock_find_completed,
    mock_cancel_job,
):
    """When MAX_POLL_ITERATIONS is exceeded, cancel_job must fire.
    The test patches MAX_POLL_ITERATIONS to a small value to make the
    test fast."""
    mock_status.return_value = {"status": "Downloading", "percentage": "10"}
    mock_xbmc.Monitor.return_value = _make_monitor()

    # poll_interval=0: the per-poll wait here is the REAL _wait_for_abort_or_timeout
    # (not mocked in this test), which spins on a non-mockable real monotonic()
    # clock, so a non-zero interval costs that many real seconds PER iteration.
    # MAX_POLL_ITERATIONS is already patched small; zeroing the interval makes
    # each iteration instant without changing the cancel-on-exhaustion behavior.
    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 0, 3600
    )

    assert url is None
    assert headers is None
    mock_cancel_job.assert_called_once_with("nzo_xyz")


# --- negative cleanup tests (Group B — cleanup must NOT fire) ---


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.get_job_history", return_value=None)
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_no_cleanup_on_job_failed_status(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_gui,
    mock_find_completed,
    mock_cancel_job,
):
    """When job_status returns Failed, the resolver aborts but does NOT
    call cancel_job — Group B paths leave nzbdav's history alone."""
    mock_status.return_value = {"status": "Failed", "percentage": "0"}
    mock_xbmc.Monitor.return_value = _make_monitor()

    _poll_until_ready("http://hydra/nzb", "movie", _make_dialog(), 2, 3600)

    mock_cancel_job.assert_not_called()


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch(
    "resources.lib.resolver.get_job_history",
    return_value={"status": "Failed", "fail_message": "test failure"},
)
@patch("resources.lib.resolver.get_job_status", return_value=None)
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_no_cleanup_on_history_failed(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_gui,
    mock_find_completed,
    mock_cancel_job,
):
    """When history reports Failed, the resolver aborts but does NOT
    call cancel_job."""
    mock_xbmc.Monitor.return_value = _make_monitor()

    _poll_until_ready("http://hydra/nzb", "movie", _make_dialog(), 2, 3600)

    mock_cancel_job.assert_not_called()


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.find_video_file", return_value=None)
@patch(
    "resources.lib.resolver.get_job_history",
    return_value={
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    },
)
@patch("resources.lib.resolver.get_job_status", return_value=None)
@patch("resources.lib.resolver.submit_nzb", return_value=("nzo_xyz", None))
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_no_cleanup_on_completed_no_video(
    mock_xbmc,
    mock_submit,
    mock_status,
    mock_history,
    mock_find_video,
    mock_gui,
    mock_find_completed,
    mock_cancel_job,
):
    """When history reports Completed but find_video_file returns None
    after max retries, the resolver aborts but does NOT call cancel_job
    — the job actually completed, this is a WebDAV layer issue."""
    mock_xbmc.Monitor.return_value = _make_monitor()

    _poll_until_ready("http://hydra/nzb", "movie", _make_dialog(), 0, 3600)

    mock_cancel_job.assert_not_called()


# ---------------------------------------------------------------------------
# Post-#217 follow-up: body-probe the history-completion path (finding #6) and
# don't re-adopt a probe-rejected completed row during submit (finding #7).
# ---------------------------------------------------------------------------


def test_handle_history_result_rejects_context_with_legacy_options():
    from resources.lib.resolver_history import HistoryContext

    context = HistoryContext(max_no_video_retries=5)
    with pytest.raises(TypeError, match="context.*options"):
        _handle_history_result(
            {},
            "movie.mkv",
            0,
            5,
            context=context,
            download_size=1,
        )


@patch("resources.lib.resolver._completed_stream_body_available", return_value=False)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_rejects_completed_when_body_unavailable(
    mock_find_stream, mock_probe
):
    """A freshly-Completed history row whose mid-file body is unavailable must
    NOT be streamed to Kodi (the missing-articles empty-stream crash class).
    The pre-submit shortcut already probes; the normal submit/poll path through
    _handle_history_result must too. On a failed probe it falls through to the
    retry budget (keep polling) instead of returning the broken stream.
    """
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    should_stop, stream_url, stream_headers, retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "bad_completed",
        },
        "movie.mkv",
        no_video_retries=0,
        max_no_video_retries=5,
    )

    assert stream_url is None
    assert stream_headers is None
    assert should_stop is False  # keep polling, do not hand Kodi a broken stream
    assert retries == 1  # consumed one retry from the budget
    mock_probe.assert_called_once_with(
        "http://webdav/movie.mkv", {"Authorization": "Basic x"}
    )


@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_streams_completed_when_body_available(
    mock_find_stream, mock_probe
):
    """When the mid-file body probe passes, the Completed history row streams
    directly — the pre-existing happy-path behavior is preserved."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    should_stop, stream_url, stream_headers, retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "good_completed",
        },
        "movie.mkv",
        no_video_retries=0,
        max_no_video_retries=5,
    )

    assert should_stop is True
    assert stream_url == "http://webdav/movie.mkv"
    assert stream_headers == {"Authorization": "Basic x"}
    assert retries == 0
    mock_probe.assert_called_once()


@patch("resources.lib.resolver.find_completed_by_name")
@patch("resources.lib.resolver.find_queued_by_name", return_value=None)
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_skips_rejected_completed_row(
    mock_submit, mock_find_queued, mock_find_completed
):
    """When the pre-submit body probe has already rejected a Completed row,
    the concurrent history probe must NOT re-adopt that same row. Submit
    proceeds to a real re-download (returns the fresh nzo_id) instead of
    handing back the known-bad completed job."""
    probe_fired = threading.Event()

    def fake_find_completed(_title, **_kwargs):
        probe_fired.set()
        return {
            "nzo_id": "bad_completed",
            "status": "Completed",
            "name": "movie.mkv",
        }

    def fake_submit(_nzb_url, _title, **_kwargs):
        # Don't finish addurl until the history probe has had a chance to
        # (try to) adopt, so the test deterministically exercises the skip.
        probe_fired.wait(2.0)
        return "fresh_redownload", None

    mock_find_completed.side_effect = fake_find_completed
    mock_submit.side_effect = fake_submit

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
    monitor.abortRequested.return_value = False

    nzo_id, submit_error = _submit_nzb_with_ui_pump(
        "http://indexer/movie.nzb",
        "movie.mkv",
        dialog,
        monitor,
        rejected_completed_ids={"bad_completed"},
    )

    assert probe_fired.is_set()  # the history probe really ran
    assert submit_error is None
    assert nzo_id == "fresh_redownload"  # NOT the rejected "bad_completed" row


@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver._completed_stream_body_available", return_value=False)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_body_unavailable_exhaustion_message(
    mock_find_stream, mock_probe, mock_gui
):
    """On retry exhaustion for a body-unavailable Completed row, the user-facing
    dialog must explain incomplete articles, not misdirect to WebDAV settings
    (the file WAS found; its mid-file body is missing)."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    # no_video_retries=4 -> the increment hits max_no_video_retries=5 (exhaustion).
    should_stop, stream_url, _headers, retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "bad_completed",
        },
        "movie.mkv",
        no_video_retries=4,
        max_no_video_retries=5,
    )

    assert should_stop is True
    assert stream_url is None
    assert retries == 5
    dialog_msg = mock_gui.Dialog.return_value.ok.call_args.args[1].lower()
    assert "incomplete" in dialog_msg
    assert "articles" in dialog_msg
    assert "not found" not in dialog_msg


# ---------------------------------------------------------------------------
# #282: reject nzbdav's job-start stub .mp4. The completed WebDAV scan can
# return a tiny placeholder seconds after submit; serving it plays ~30s of a
# stub instead of the feature. A single-file release whose discovered video is
# far below the indexer-advertised size is rejected and the poll loop keeps
# waiting. Packs are exempt (one episode is legitimately a pack fraction).
# ---------------------------------------------------------------------------


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=362_076_665)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_rejects_stub_far_below_advertised(
    mock_find_stream, mock_probe, _mock_size
):
    """A single-file release whose discovered video (362 MB) is a tiny fraction
    of the advertised size (~81 GB) is nzbdav's job-start stub. It must NOT be
    streamed; keep polling for the real download. The body probe is skipped —
    the size mismatch alone rejects it — and the no-video retry budget is NOT
    consumed (the poll loop's download_timeout is the stop authority, #340).
    """
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/UNDERTAKERS.mp4",
        "http://webdav/UNDERTAKERS.mp4",
        {"Authorization": "Basic x"},
    )

    should_stop, stream_url, stream_headers, retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "stub_completed",
        },
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        no_video_retries=0,
        max_no_video_retries=5,
        download_size="81610612736",  # ~81 GB advertised
    )

    assert stream_url is None
    assert stream_headers is None
    assert should_stop is False  # keep polling, do not hand Kodi the stub
    assert retries == 0  # symlink-visibility budget untouched by the stub
    mock_probe.assert_not_called()  # rejected on size before any body probe


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=80_000_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_streams_single_file_matching_advertised(
    mock_find_stream, mock_probe, _mock_size
):
    """A single-file release whose discovered video (~80 GB) is close to the
    advertised size (~81 GB) is the real feature and streams normally."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    should_stop, stream_url, stream_headers, retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "good_completed",
        },
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        no_video_retries=0,
        max_no_video_retries=5,
        download_size="81610612736",
    )

    assert should_stop is True
    assert stream_url == "http://webdav/movie.mkv"
    assert stream_headers == {"Authorization": "Basic x"}
    assert retries == 0


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=30_000_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_streams_pack_episode_below_advertised(
    mock_find_stream, mock_probe, _mock_size
):
    """A season pack's advertised size covers every episode, so the single
    picked episode (3 GB) is legitimately far below the advertised pack size
    (30 GB). The stub guard must be SKIPPED for packs and the episode streams.
    """
    mock_find_stream.return_value = (
        "/content/uncategorized/show/show.s01e03.mkv",
        "http://webdav/show.s01e03.mkv",
        {"Authorization": "Basic x"},
    )

    should_stop, stream_url, stream_headers, retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/show",
            "nzo_id": "pack_completed",
        },
        "Some.Show.S01.1080p.WEB-DL.x264-GROUP",  # whole-season pack
        no_video_retries=0,
        max_no_video_retries=5,
        download_size="30000000000",  # 30 GB whole pack
    )

    assert should_stop is True
    assert stream_url == "http://webdav/show.s01e03.mkv"
    assert retries == 0


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=1_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_streams_when_advertised_size_unknown(
    mock_find_stream, mock_probe, _mock_size
):
    """Fail OPEN: with no advertised size the guard cannot judge plausibility,
    so a tiny discovered file streams exactly as before (no regression)."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    should_stop, stream_url, _headers, retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "no_advertised",
        },
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        no_video_retries=0,
        max_no_video_retries=5,
        download_size=None,
    )

    assert should_stop is True
    assert stream_url == "http://webdav/movie.mkv"
    assert retries == 0


@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.webdav.folder_video_total_bytes", return_value=362_076_665)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_stub_keeps_polling_and_defers_to_timeout(
    mock_find_stream, mock_probe, _mock_size, mock_gui
):
    """#340: nzbdav reports Completed the instant its job-start stub lands while
    the real file is still fetching. A stub rejection never self-fails and never
    consumes the short symlink-visibility budget — even far past it — so the
    poll loop's own download_timeout stays the stop authority and a configured
    long wait is honored. No user dialog is shown for a stub."""
    mock_find_stream.return_value = (
        "/content/uncategorized/movie/UNDERTAKERS.mp4",
        "http://webdav/UNDERTAKERS.mp4",
        {"Authorization": "Basic x"},
    )

    # Well past max_no_video_retries=5: the old shared budget would have failed.
    should_stop, stream_url, stream_headers, retries = _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "stub_completed",
        },
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        no_video_retries=999,
        max_no_video_retries=5,
        download_size="81610612736",
    )

    assert should_stop is False  # keep polling; defer to download_timeout
    assert stream_url is None
    assert stream_headers is None
    assert retries == 999  # budget untouched -- not incremented, not exhausted
    mock_gui.Dialog.return_value.ok.assert_not_called()  # no premature failure


@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.webdav.folder_video_total_bytes", return_value=362_076_665)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_stub_does_not_starve_symlink_budget(
    mock_find_stream, _mock_size, mock_gui
):
    """#340: because a stub rejection does not touch no_video_retries, a later
    genuine 'Completed but no video visible yet' gap still has its full
    symlink-visibility budget instead of failing immediately (the exact case the
    retries exist for)."""
    title = "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP"
    history = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "nzo_id": "stub_completed",
    }

    # A stub poll: keeps polling, leaves the budget at 0.
    with patch(
        "resources.lib.resolver._completed_stream_body_available", return_value=True
    ):
        mock_find_stream.return_value = (
            "/content/uncategorized/movie/UNDERTAKERS.mp4",
            "http://webdav/UNDERTAKERS.mp4",
            {"Authorization": "Basic x"},
        )
        should_stop, _u, _h, retries = _handle_history_result(
            history,
            title,
            no_video_retries=0,
            max_no_video_retries=5,
            download_size="81610612736",
        )
    assert should_stop is False
    assert retries == 0  # stub consumed none of the symlink-visibility budget

    # A genuine no-video gap now still has all of its retries (1/5, not failed).
    mock_find_stream.return_value = (None, None, None)
    should_stop, _u, _h, retries = _handle_history_result(
        history,
        title,
        no_video_retries=retries,
        max_no_video_retries=5,
        download_size="81610612736",
    )
    assert should_stop is False
    assert retries == 1  # budget intact, not pre-exhausted by the stub
    mock_gui.Dialog.return_value.ok.assert_not_called()


# ---------------------------------------------------------------------------
# #282 follow-up D: the advertised-size floor that powers _discovered_video_is_stub
# is also threaded into WebDAV discovery (_stub_min_size_floor -> min_video_size)
# so a root-level stub recurses into the subfolder holding the real file rather
# than every poll re-picking the same root stub until download_timeout. The
# floor is PACK-AGNOSTIC (advertised*0.5 for any known size, 0 for unknown),
# keeping the fail-open behavior unchanged.
# ---------------------------------------------------------------------------


def test_stub_min_size_floor_is_half_advertised_pack_agnostic():
    """The floor is half the advertised size for ANY release with a known size --
    PACK-AGNOSTIC, no title/release_is_pack consultation. Packs are handled at
    accept time by comparing the folder's TOTAL video bytes (episodes sum to
    ~advertised) against this floor, not a single picked episode, so the floor
    itself is the same for a movie and a season pack."""
    from resources.lib.resolver import (
        _STUB_VIDEO_MIN_ADVERTISED_FRACTION,
        _stub_min_size_floor,
    )

    assert _stub_min_size_floor("81610612736") == (
        81610612736 * _STUB_VIDEO_MIN_ADVERTISED_FRACTION
    )
    # Same floor regardless of whether the name looks like a pack.
    assert _stub_min_size_floor("30000000000") == (
        30000000000 * _STUB_VIDEO_MIN_ADVERTISED_FRACTION
    )


def test_stub_min_size_floor_unknown_advertised_is_zero():
    """Fail OPEN: with no parseable advertised size there is no floor (0), so a
    legitimately small file is never deferred or dropped."""
    from resources.lib.resolver import _stub_min_size_floor

    assert _stub_min_size_floor(None) == 0
    assert _stub_min_size_floor("0") == 0
    assert _stub_min_size_floor("not-a-number") == 0


def test_advertised_size_bytes_non_finite_is_unknown():
    """#340 Codex review: `inf` and overflowing exponents parse as float but
    raise OverflowError on int(); the helper must fail OPEN (0) per its docstring
    rather than let the exception escape the resolver. OverflowError is not in
    _RESOLVE_RUNTIME_ERRORS, so an uncaught one bypasses the
    setResolvedUrl-on-failure guarantee."""
    from resources.lib.resolver import _advertised_size_bytes

    assert _advertised_size_bytes("inf") == 0
    assert _advertised_size_bytes("-inf") == 0
    assert _advertised_size_bytes("1e10000") == 0
    assert _advertised_size_bytes("nan") == 0


def test_stub_min_size_floor_non_finite_advertised_is_zero():
    """A non-finite advertised size is unknown, so there is no floor (0) and the
    stub guard fails OPEN instead of raising out of discovery."""
    from resources.lib.resolver import _stub_min_size_floor

    assert _stub_min_size_floor("inf") == 0
    assert _stub_min_size_floor("1e10000") == 0


# ---------------------------------------------------------------------------
# _discovered_video_is_stub two-stage, PACK-AGNOSTIC logic (#282 redesign):
#   stage 1 (fast path) -- a picked file already >= advertised*0.5 is the real
#           single feature; accept WITHOUT a folder walk.
#   stage 2 -- a small/unknown picked file is a stub OR a pack episode; sum the
#           folder's TOTAL video bytes and reject only if the WHOLE folder is
#           below the floor. This gives packs real stub protection (the old
#           title-based release_is_pack exemption disabled the guard for them).
# ---------------------------------------------------------------------------


@patch("resources.lib.webdav.folder_video_total_bytes")
@patch("resources.lib.webdav.get_video_file_size_hint")
def test_discovered_video_is_stub_unknown_advertised_fails_open(
    mock_picked, mock_total
):
    """No advertised size -> floor 0 -> fail OPEN with no I/O at all (neither the
    picked-size hint nor the folder walk is consulted)."""
    from resources.lib.resolver import _discovered_video_is_stub

    assert _discovered_video_is_stub("/folder", "/folder/x.mkv", None) is False
    mock_picked.assert_not_called()
    mock_total.assert_not_called()


@patch("resources.lib.webdav.folder_video_total_bytes")
@patch("resources.lib.webdav.get_video_file_size_hint", return_value=80_000_000_000)
def test_discovered_video_is_stub_fast_path_skips_folder_walk(mock_picked, mock_total):
    """A real-sized single feature (picked file >= floor) is accepted WITHOUT the
    extra folder-total walk -- the latency-cheap common-movie path."""
    from resources.lib.resolver import _discovered_video_is_stub

    # advertised ~81 GB -> floor ~40.8 GB; picked 80 GB is above it.
    is_stub = _discovered_video_is_stub("/folder", "/folder/movie.mkv", "81610612736")

    assert is_stub is False
    mock_total.assert_not_called()  # no second walk for an obviously-real file


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=362_076_665)
@patch("resources.lib.webdav.get_video_file_size_hint", return_value=362_076_665)
def test_discovered_video_is_stub_rejects_stub_only_folder(mock_picked, mock_total):
    """Picked file below the floor AND the folder total is just the stub -> a
    job-start stub; reject. The walk runs to make the decision."""
    from resources.lib.resolver import _discovered_video_is_stub

    is_stub = _discovered_video_is_stub("/folder", "/folder/stub.mp4", "81610612736")

    assert is_stub is True
    mock_total.assert_called_once()


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=30_000_000_000)
@patch("resources.lib.webdav.get_video_file_size_hint", return_value=3_000_000_000)
def test_discovered_video_is_stub_accepts_real_pack_via_folder_total(
    mock_picked, mock_total
):
    """THE pack-agnostic win: a picked episode (3 GB) is far below the advertised
    pack size (30 GB, floor 15 GB), but the FOLDER total (30 GB of episodes) is
    at/above the floor -> a real pack, accept. The old title-based guard would
    have had to special-case this; the folder total handles it with no title."""
    from resources.lib.resolver import _discovered_video_is_stub

    is_stub = _discovered_video_is_stub("/pack", "/pack/Show.S01E03.mkv", "30000000000")

    assert is_stub is False
    mock_total.assert_called_once()


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=362_076_665)
@patch("resources.lib.webdav.get_video_file_size_hint", return_value=400_000_000)
def test_discovered_video_is_stub_rejects_pack_showing_only_stub(
    mock_picked, mock_total
):
    """THE hole this redesign closes: a PACK (advertised 30 GB) whose folder so
    far exposes only nzbdav's job-start stub (folder total 362 MB) is REJECTED,
    so the poll loop keeps waiting for the real episodes. The previous
    release_is_pack(title) exemption returned floor 0 for packs and would have
    streamed this stub."""
    from resources.lib.resolver import _discovered_video_is_stub

    is_stub = _discovered_video_is_stub(
        "/pack", "/pack/Some.Show.S01.Complete.stub.mp4", "30000000000"
    )

    assert is_stub is True  # pack stub no longer slips through
    mock_total.assert_called_once()


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=0)
@patch("resources.lib.webdav.get_video_file_size_hint", return_value=362_076_665)
def test_discovered_video_is_stub_fails_open_when_folder_scan_empty(
    mock_picked, mock_total
):
    """Picked file is small but the folder walk returns nothing (scan failed or
    raced) -> fail OPEN rather than reject a possibly-real stream."""
    from resources.lib.resolver import _discovered_video_is_stub

    is_stub = _discovered_video_is_stub("/folder", "/folder/x.mkv", "81610612736")

    assert is_stub is False


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=-1)
@patch("resources.lib.webdav.get_video_file_size_hint", return_value=362_076_665)
def test_discovered_video_is_stub_fails_open_on_incomplete_scan(
    mock_picked, mock_total
):
    """When folder_video_total_bytes signals INCOMPLETE (negative sentinel -- a
    PROPFIND error or an unsized video file), the total is untrustworthy, so the
    guard fails OPEN and never rejects real content on partial data."""
    from resources.lib.resolver import _discovered_video_is_stub

    is_stub = _discovered_video_is_stub("/folder", "/folder/x.mkv", "81610612736")

    assert is_stub is False


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=362_076_665)
@patch("resources.lib.webdav.get_video_file_size_hint", return_value=0)
def test_discovered_video_is_stub_walks_when_picked_size_unknown(
    mock_picked, mock_total
):
    """When the picked file's own size is unknown (no cached hint), the guard
    still walks the folder rather than fail-open blindly -- a stub-only folder is
    caught even without a per-file size."""
    from resources.lib.resolver import _discovered_video_is_stub

    is_stub = _discovered_video_is_stub("/folder", "/folder/x.mp4", "81610612736")

    assert is_stub is True
    mock_total.assert_called_once()


# Stage 2b (#355 review): the folder TOTAL clearing the floor does not prove the
# PICKED file is real -- a materialised sibling can lift the total while the
# requested file is still a stub. Reject a picked file dwarfed by the largest
# video; accept a real episode comparable to its siblings.


@patch("resources.lib.webdav.get_video_file_size_hint", return_value=10 * 1024**2)
@patch("resources.lib.webdav.folder_video_total_bytes")
def test_discovered_video_is_stub_rejects_below_floor_pick_dwarfed_by_sibling(
    mock_total, _mock_hint
):
    """The picked file (10 MB stub) is below the floor, the folder total clears
    the floor ONLY because an 8 GB sibling materialised. Pre-stage-2b this wrongly
    ACCEPTED (the total >= floor); now the picked file being ~0.1% of the largest
    video is rejected so the poll loop keeps waiting for the real requested file.
    """
    from resources.lib.resolver import _discovered_video_is_stub

    def total(_folder, settings_getter=None, _stats=None):
        if _stats is not None:
            _stats["max"] = 8 * 1024**3  # the large materialised sibling
        return 10 * 1024**2 + 8 * 1024**3  # >= floor, but driven by the sibling

    mock_total.side_effect = total

    is_stub = _discovered_video_is_stub(
        "/content/x/Show.S01.Pack/",
        "/content/x/Show.S01.Pack/Show.S01E05.mkv",
        str(4 * 1024**3),  # advertised 4 GB -> floor 2 GB
    )

    assert is_stub is True


@patch("resources.lib.webdav.get_video_file_size_hint", return_value=3 * 1024**3)
@patch("resources.lib.webdav.folder_video_total_bytes")
def test_discovered_video_is_stub_accepts_real_pack_episode_comparable_to_siblings(
    mock_total, _mock_hint
):
    """A real pack episode (3 GB) below the half-pack floor but comparable in size
    to its largest sibling (3 GB) must still stream -- stage 2b only rejects a file
    ANOMALOUSLY tiny versus the largest video, never a legitimate episode."""
    from resources.lib.resolver import _discovered_video_is_stub

    def total(_folder, settings_getter=None, _stats=None):
        if _stats is not None:
            _stats["max"] = 3 * 1024**3
        return 24 * 1024**3  # whole-pack total

    mock_total.side_effect = total

    is_stub = _discovered_video_is_stub(
        "/content/x/Show.S01.Complete/",
        "/content/x/Show.S01.Complete/Show.S01E05.mkv",
        str(24 * 1024**3),  # advertised 24 GB -> floor 12 GB
    )

    assert is_stub is False


@patch("resources.lib.webdav.get_video_file_size_hint", return_value=10 * 1024**2)
@patch("resources.lib.webdav.folder_video_total_bytes")
def test_discovered_video_is_stub_stage2b_fails_open_when_largest_unknown(
    mock_total, _mock_hint
):
    """If the walk reported a total >= floor but no per-file max (largest 0),
    stage 2b cannot judge and fails OPEN rather than reject real content."""
    from resources.lib.resolver import _discovered_video_is_stub

    def total(_folder, settings_getter=None, _stats=None):
        return 8 * 1024**3  # >= floor, but leaves _stats empty (no max recorded)

    mock_total.side_effect = total

    is_stub = _discovered_video_is_stub("/folder", "/folder/x.mkv", str(4 * 1024**3))

    assert is_stub is False


@patch("resources.lib.webdav.get_video_file_size_hint", return_value=300 * 1024**2)
@patch("resources.lib.webdav.folder_video_total_bytes")
def test_discovered_video_is_stub_accepts_short_pack_special(mock_total, _mock_hint):
    """#355 Codex review: a legitimately SHORT requested pack item (a 300 MB
    recap/special, ~7% of the 4 GB longest episode) must still stream -- it is real
    content, not a job-start stub. The folder total proves the pack is present; the
    picked file at ~7% of the largest is above the stub fraction (0.05), so it is
    accepted. (At the original 0.1 fraction this was wrongly rejected.)"""
    from resources.lib.resolver import _discovered_video_is_stub

    def total(_folder, settings_getter=None, _stats=None):
        if _stats is not None:
            _stats["max"] = 4 * 1024**3  # longest episode in the pack
        return 30 * 1024**3  # whole pack materialised

    mock_total.side_effect = total

    is_stub = _discovered_video_is_stub(
        "/content/x/Show.S01.Complete/",
        "/content/x/Show.S01.Complete/Show.S01E00.Recap.mkv",
        str(30 * 1024**3),  # pack advertised 30 GB -> floor 15 GB
    )

    assert is_stub is False


@patch("resources.lib.webdav.get_video_file_size_hint", return_value=10 * 1024**2)
@patch("resources.lib.webdav.folder_video_total_bytes")
def test_discovered_video_is_stub_rejects_dwarfed_stub_even_when_total_incomplete(
    mock_total, _mock_hint
):
    """#355 Codex review: when the folder-total scan is INCOMPLETE (negative
    sentinel) but already sized a real sibling that dwarfs the picked file, the
    picked file is a known job-start stub -- reject it. A transient second-PROPFIND
    glitch (or a sibling missing getcontentlength) must NOT fail-open a stub when a
    real sibling is visible. (Pre-fix, the incomplete total fail-opened first and
    streamed the stub.)"""
    from resources.lib.resolver import _discovered_video_is_stub

    def total(_folder, settings_getter=None, _stats=None):
        if _stats is not None:
            _stats["max"] = 8 * 1024**3  # a real sibling WAS sized
        return -1  # but the overall scan is INCOMPLETE (negative sentinel)

    mock_total.side_effect = total

    is_stub = _discovered_video_is_stub(
        "/folder", "/folder/stub.mp4", str(4 * 1024**3)  # floor 2 GB
    )

    assert is_stub is True


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=80_000_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_threads_stub_floor_into_discovery(
    mock_find_stream, _mock_probe, _mock_size
):
    """A single-file release threads its half-advertised floor into discovery so
    a root stub recurses to the real file in a subfolder (#282 follow-up D)."""
    from resources.lib.resolver import _STUB_VIDEO_MIN_ADVERTISED_FRACTION

    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )

    _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "x",
        },
        "The.Undertakers.2024.2160p.UHD.BluRay.x265-GROUP",
        no_video_retries=0,
        max_no_video_retries=5,
        download_size="81610612736",
    )

    assert mock_find_stream.call_args.kwargs["min_video_size"] == (
        81610612736 * _STUB_VIDEO_MIN_ADVERTISED_FRACTION
    )


@patch("resources.lib.webdav.folder_video_total_bytes", return_value=30_000_000_000)
@patch("resources.lib.resolver._completed_stream_body_available", return_value=True)
@patch("resources.lib.resolver._find_completed_video_stream_with_rechecks")
def test_handle_history_result_threads_advertised_floor_for_pack(
    mock_find_stream, _mock_probe, _mock_size
):
    """PACK-AGNOSTIC: a pack threads the SAME advertised*0.5 floor into discovery
    as a single file. The folder-total accept guard (mocked at the full 30 GB
    pack here) then accepts the real pack while a stub-only folder is rejected."""
    from resources.lib.resolver import _STUB_VIDEO_MIN_ADVERTISED_FRACTION

    mock_find_stream.return_value = (
        "/content/uncategorized/show/show.s01e03.mkv",
        "http://webdav/show.s01e03.mkv",
        {"Authorization": "Basic x"},
    )

    _handle_history_result(
        {
            "status": "Completed",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/show",
            "nzo_id": "pack",
        },
        "Some.Show.S01.1080p.WEB-DL.x264-GROUP",
        no_video_retries=0,
        max_no_video_retries=5,
        download_size="30000000000",
    )

    assert mock_find_stream.call_args.kwargs["min_video_size"] == (
        30000000000 * _STUB_VIDEO_MIN_ADVERTISED_FRACTION
    )


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_find_completed_video_stream_threads_min_video_size(mock_find_stream):
    """_find_completed_video_stream_with_rechecks forwards the floor down to
    _find_video_stream_for_folder so the recheck loop also recurses past stubs."""
    from resources.lib.resolver import _find_completed_video_stream_with_rechecks

    mock_find_stream.return_value = (
        "/content/x/v.mkv",
        "http://webdav/v.mkv",
        {},
    )

    _find_completed_video_stream_with_rechecks(
        "/content/x/", min_video_size=2_000_000_000
    )

    assert mock_find_stream.call_args.kwargs["min_video_size"] == 2_000_000_000


@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
def test_find_video_stream_for_folder_threads_min_video_size_to_find_video_file(
    mock_find_video_file, mock_stream_url
):
    """The shared _find_video_stream_for_folder forwards the floor into webdav's
    find_video_file (fallback path), where the stub-recursion logic lives."""
    from resources.lib.resolver import _find_video_stream_for_folder

    mock_find_video_file.return_value = "/content/x/v.mkv"
    mock_stream_url.return_value = ("http://webdav/v.mkv", {})

    _find_video_stream_for_folder("/content/x/", min_video_size=2_000_000_000)

    assert mock_find_video_file.call_args.kwargs["min_video_size"] == 2_000_000_000


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_picker_completed_stream_records_rejected_id(mock_find_stream):
    """A picker-supplied completed row rejected by the body probe records its
    nzo_id into the shared rejected set, so the submit history probe can skip
    it (PR #219 review: picker rejections must not be lost)."""
    from urllib.error import HTTPError

    from resources.lib.resolver import _picker_completed_stream

    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )
    head = _probe_response(content_length=85_000_000)
    midfile_500 = HTTPError("http://webdav/movie.mkv", 500, "err", {}, None)
    rejected = set()
    params = {
        "_completed_job": {
            "status": "Completed",
            "name": "movie.mkv",
            "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
            "nzo_id": "bad_picker",
        }
    }

    with patch("urllib.request.urlopen", side_effect=[head, midfile_500]):
        stream = _picker_completed_stream(
            "movie.mkv", params, rejected_completed_ids=rejected
        )

    assert stream is None
    assert "bad_picker" in rejected


@patch("resources.lib.resolver.probe_webdav_reachable", return_value=(False, None))
@patch("resources.lib.resolver.get_job_history", return_value=None)
@patch("resources.lib.resolver.get_job_status", return_value=None)
def test_poll_once_byname_fallback_skips_rejected_completed_row(
    mock_status, mock_history, mock_probe
):
    """The by-name terminal fallback must not surface a Completed row whose
    nzo_id was already rejected by the body probe (PR #219 review): otherwise
    the poll loop latches onto the stale bad row inside the 5s tolerance
    instead of waiting for the fresh re-download."""
    bad_row = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
        "name": "movie",
        "nzo_id": "bad_terminal",
        "fail_message": "",
        "completed": 10_000,
    }

    with patch("resources.lib.nzbdav_api.find_terminal_by_name", return_value=bad_row):
        # Without the rejected set, the fallback surfaces the terminal row.
        _js, history_default, _err = _poll_once(
            "fresh_nzo", "movie", _make_monitor(), submit_started_wall=10_000
        )
        # With the row's nzo_id rejected, the fallback suppresses it.
        _js2, history_rejected, _err2 = _poll_once(
            "fresh_nzo",
            "movie",
            _make_monitor(),
            submit_started_wall=10_000,
            rejected_completed_ids={"bad_terminal"},
        )

    assert history_default is not None
    assert history_default.get("status") == "Completed"
    assert history_rejected is None


# --- clear-queue-on-submit ---


def _clear_queue_setting(value):
    """settings_getter that returns ``value`` for clear_queue_on_submit."""

    def _sg(key, default=""):
        return value if key == "clear_queue_on_submit" else default

    return _sg


def test_clear_queue_on_submit_mode_maps_index_to_name():
    from resources.lib.resolver import _clear_queue_on_submit_mode

    assert (
        _clear_queue_on_submit_mode(settings_getter=_clear_queue_setting("0")) == "ask"
    )
    assert (
        _clear_queue_on_submit_mode(settings_getter=_clear_queue_setting("1"))
        == "always"
    )
    assert (
        _clear_queue_on_submit_mode(settings_getter=_clear_queue_setting("2"))
        == "never"
    )
    # Unset / unknown values fall back to the safe default (ask).
    assert (
        _clear_queue_on_submit_mode(settings_getter=_clear_queue_setting("")) == "ask"
    )
    assert (
        _clear_queue_on_submit_mode(settings_getter=_clear_queue_setting("9")) == "ask"
    )


@patch("resources.lib.resolver.clear_queue")
@patch("resources.lib.resolver.get_queue_slots")
def test_maybe_clear_queue_never_does_not_even_probe(mock_slots, mock_clear):
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    _maybe_clear_queue_before_submit("Title", settings_getter=_clear_queue_setting("2"))

    mock_slots.assert_not_called()
    mock_clear.assert_not_called()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.clear_queue")
@patch("resources.lib.resolver.get_queue_slots", return_value=[])
def test_maybe_clear_queue_empty_queue_no_clear(mock_slots, mock_clear, mock_find):
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    _maybe_clear_queue_before_submit("Title", settings_getter=_clear_queue_setting("0"))

    mock_slots.assert_called_once()
    mock_clear.assert_not_called()
    # The queue is probed FIRST: an empty queue must not pay for a (possibly
    # slow) history lookup before returning.
    mock_find.assert_not_called()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.clear_queue", return_value=2)
@patch(
    "resources.lib.resolver.get_queue_slots",
    return_value=[
        {"nzo_id": "a", "status": "Downloading"},
        {"nzo_id": "b", "status": "Queued"},
    ],
)
def test_maybe_clear_queue_always_clears_without_prompt(
    mock_slots, mock_clear, _mock_find
):
    from resources.lib import resolver
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    _maybe_clear_queue_before_submit("Title", settings_getter=_clear_queue_setting("1"))

    mock_clear.assert_called_once()
    # Probe uses the short, best-effort timeout (must not freeze the resolver
    # thread on a slow/unreachable nzbdav).
    assert (
        mock_slots.call_args.kwargs.get("timeout")
        == resolver._CLEAR_QUEUE_PROBE_TIMEOUT
    )
    # The clear reuses the exact slots that were probed — no second fetch — so
    # a job added between the probe and the clear is never cancelled unseen.
    assert mock_clear.call_args.kwargs.get("slots") == mock_slots.return_value
    # Each delete is bounded by the same short timeout so a stalled nzbdav
    # can't freeze the resolver across several deletes.
    assert (
        mock_clear.call_args.kwargs.get("timeout")
        == resolver._CLEAR_QUEUE_PROBE_TIMEOUT
    )


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.clear_queue", return_value=1)
@patch(
    "resources.lib.resolver.get_queue_slots",
    return_value=[{"nzo_id": "a", "status": "Downloading", "filename": "Foo"}],
)
def test_maybe_clear_queue_ask_yes_clears(
    mock_slots, mock_clear, mock_xbmcgui, _mock_find
):
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    mock_xbmcgui.Dialog.return_value.yesno.return_value = True

    _maybe_clear_queue_before_submit("Title", settings_getter=_clear_queue_setting("0"))

    assert mock_xbmcgui.Dialog.return_value.yesno.called
    mock_clear.assert_called_once()


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmcgui")
@patch("resources.lib.resolver.clear_queue")
@patch(
    "resources.lib.resolver.get_queue_slots",
    return_value=[{"nzo_id": "a", "status": "Downloading", "filename": "Foo"}],
)
def test_maybe_clear_queue_ask_no_keeps_queue(
    mock_slots, mock_clear, mock_xbmcgui, _mock_find
):
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    mock_xbmcgui.Dialog.return_value.yesno.return_value = False

    _maybe_clear_queue_before_submit("Title", settings_getter=_clear_queue_setting("0"))

    assert mock_xbmcgui.Dialog.return_value.yesno.called
    mock_clear.assert_not_called()


@patch("resources.lib.resolver.clear_queue")
@patch(
    "resources.lib.resolver.get_queue_slots",
    return_value=[{"nzo_id": "other", "status": "Queued", "filename": "Other"}],
)
@patch("resources.lib.resolver._existing_completed_stream")
def test_maybe_clear_queue_skips_when_title_already_completed(
    mock_find, mock_slots, mock_clear
):
    """If the title is already downloaded AND its body is streamable, playback
    adopts the completed copy (no new download), so other active jobs must NOT
    be cancelled for a replay — even when there are other jobs queued."""
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    # A truthy stream tuple means the completed copy is body-validated/adoptable.
    mock_find.return_value = ("http://webdav/Title.mkv", {})

    # 'always' would otherwise clear the other queued job.
    _maybe_clear_queue_before_submit("Title", settings_getter=_clear_queue_setting("1"))

    mock_slots.assert_called_once()  # probe runs first
    mock_find.assert_called_once()  # then the completed-stream guard (other jobs exist)
    mock_clear.assert_not_called()  # ... which skips the clear


@patch("resources.lib.resolver.clear_queue", return_value=1)
@patch(
    "resources.lib.resolver.get_queue_slots",
    return_value=[{"nzo_id": "other", "status": "Queued", "filename": "Other"}],
)
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
def test_maybe_clear_queue_clears_when_completed_row_body_unavailable(
    mock_find, mock_slots, mock_clear
):
    """A Completed history row whose mid-file body is missing is NOT adoptable —
    playback will resubmit a fresh download, so the queue guard must still clear
    the other jobs rather than skip on mere history existence."""
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    _maybe_clear_queue_before_submit("Title", settings_getter=_clear_queue_setting("1"))

    mock_find.assert_called_once()  # body-validated, not mere existence
    mock_clear.assert_called_once()  # not adoptable -> proceed to clear


@patch("resources.lib.resolver.clear_queue")
@patch(
    "resources.lib.resolver.get_queue_slots",
    return_value=[{"nzo_id": "other", "status": "Queued", "filename": "Other"}],
)
@patch("resources.lib.resolver._existing_completed_stream")
def test_maybe_clear_queue_completed_probe_is_time_bounded(
    mock_find, mock_slots, mock_clear
):
    """A slow/unreachable nzbdav must not freeze the pre-dialog clear-queue guard.

    The completed-adopt probe runs on a worker bounded to
    _CLEAR_QUEUE_PROBE_TIMEOUT; on timeout the guard returns promptly and leaves
    the queue intact (defensive) instead of blocking on the probe's own
    multi-second history/WebDAV socket timeouts before the progress dialog even
    appears.
    """
    from resources.lib import resolver
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    started = threading.Event()
    release = threading.Event()

    def _slow_probe(*_args, **_kwargs):
        started.set()
        release.wait(2)  # would block well past the probe budget
        return ("http://webdav/Title.mkv", {})

    mock_find.side_effect = _slow_probe

    try:
        with patch.object(resolver, "_CLEAR_QUEUE_PROBE_TIMEOUT", 0.05):
            start = _time.monotonic()
            _maybe_clear_queue_before_submit(
                "Title", settings_getter=_clear_queue_setting("1")
            )
            elapsed = _time.monotonic() - start
    finally:
        release.set()

    assert started.is_set()  # the probe really ran
    assert elapsed < 1.0  # bounded — did not block on the full probe
    mock_clear.assert_not_called()  # uncertain within budget -> leave queue intact


@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.clear_queue", return_value=1)
@patch("resources.lib.resolver.get_queue_slots")
def test_maybe_clear_queue_excludes_current_title_slot(
    mock_slots, mock_clear, _mock_find
):
    """The current title's own in-flight queue job must NOT be cleared (the
    submit path resumes it); only the OTHER queued jobs are cancelled."""
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    mine = {"nzo_id": "mine", "filename": "Title", "status": "Downloading"}
    other = {"nzo_id": "other", "filename": "Other", "status": "Queued"}
    mock_slots.return_value = [mine, other]

    _maybe_clear_queue_before_submit("Title", settings_getter=_clear_queue_setting("1"))

    assert mock_clear.call_args.kwargs.get("slots") == [other]


@patch("resources.lib.resolver._existing_completed_stream")
@patch("resources.lib.resolver.clear_queue")
@patch("resources.lib.resolver.get_queue_slots")
def test_maybe_clear_queue_only_current_title_skips_clear(
    mock_slots, mock_clear, mock_find
):
    """If the only queued job is the current title's own, there is nothing else
    to clear: don't cancel it (it will be resumed) and don't run the
    completed-stream guard."""
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    mock_slots.return_value = [
        {"nzo_id": "mine", "filename": "Title", "status": "Downloading"}
    ]

    _maybe_clear_queue_before_submit("Title", settings_getter=_clear_queue_setting("1"))

    mock_clear.assert_not_called()
    mock_find.assert_not_called()  # filtered-empty returns before the guard


@patch("resources.lib.resolver.clear_queue", return_value=1)
@patch("resources.lib.resolver.get_queue_slots", return_value=[{"nzo_id": "a"}])
@patch("resources.lib.resolver._existing_completed_stream")
def test_maybe_clear_queue_skips_completed_probe_when_picker_already_checked(
    mock_find, mock_slots, mock_clear
):
    """When the picker already ran a completed lookup and we still reached the
    submit path, a submit is certain — the guard must NOT do a redundant
    _existing_completed_stream probe (that dedup is a resolve-flow perf
    contract)."""
    from resources.lib.resolver import _maybe_clear_queue_before_submit

    _maybe_clear_queue_before_submit(
        "Title",
        settings_getter=_clear_queue_setting("1"),
        completed_lookup_done=True,
    )

    mock_find.assert_not_called()
    mock_clear.assert_called_once()


# --- (E) download-ledger pubdate recorded only after stream confirmed playable ---


def _poll_dialog():
    dlg = MagicMock()
    dlg.iscanceled.return_value = False
    return dlg


@patch("resources.lib.resolver.record_download")
@patch("resources.lib.resolver._handle_history_result")
@patch("resources.lib.resolver._handle_job_status", return_value=(False, None))
@patch("resources.lib.resolver._poll_once", return_value=({}, None, None))
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value="nzo-1")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_records_ledger_only_on_playable_success(
    mock_xbmc,
    _mock_existing,
    _mock_submit,
    _mock_poll_once,
    _mock_status,
    mock_history,
    mock_record,
):
    """A submit whose poll returns a stream URL DOES record the ledger pubdate
    (keyed to THIS confirmed-playable download)."""
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_history.return_value = (False, "http://webdav/movie.mkv", {"H": "1"}, 0)

    result = _poll_until_ready(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        _poll_dialog(),
        poll_interval=1,
        download_timeout=60,
        poll_ctx=PollContext(
            download_pubdate="2026-01-02",
            download_size=12345,
        ),
    )

    assert result == ("http://webdav/movie.mkv", {"H": "1"})
    mock_record.assert_called_once_with("movie.mkv", "2026-01-02", 12345)


@patch("resources.lib.resolver.record_download")
@patch("resources.lib.resolver._handle_history_result")
@patch("resources.lib.resolver._handle_job_status", return_value=(True, None))
@patch(
    "resources.lib.resolver._poll_once", return_value=({"status": "Failed"}, None, None)
)
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value="nzo-1")
@patch("resources.lib.resolver._existing_completed_stream", return_value=None)
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_does_not_record_ledger_when_poll_fails(
    mock_xbmc,
    _mock_existing,
    _mock_submit,
    _mock_poll_once,
    _mock_status,
    mock_history,
    mock_record,
):
    """A submit that succeeds but whose poll returns (None, None) (job failed /
    timed out / cancelled) must NOT record a ledger pubdate — otherwise a
    different repost's same-name completed row could later look adopted."""
    mock_xbmc.Monitor.return_value = _make_monitor()
    mock_history.return_value = (False, None, None, 0)

    result = _poll_until_ready(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        _poll_dialog(),
        poll_interval=1,
        download_timeout=60,
        poll_ctx=PollContext(
            download_pubdate="2026-01-02",
            download_size=12345,
        ),
    )

    assert result == (None, None)
    mock_record.assert_not_called()


# --- (J) delayed fallback submit aborts when playback goes inactive ---


def test_fallback_worker_stop_event_during_prewarm_aborts_submit():
    """Sanity check of the pre-existing in-process stop-event abort (held across
    the J fix). The nzbdav.playing regression itself is guarded by
    test_fallback_worker_submits_when_playback_stays_live and
    test_fallback_worker_inactive_property_during_prewarm_aborts_submit."""
    from resources.lib.resolver import (
        _signal_fallback_playback_started,
        _start_fallback_submit_worker,
    )

    submitted = []

    def fake_submit(*_args, **_kwargs):
        submitted.append(True)

    with patch(
        "resources.lib.resolver._submit_fallback_candidates", side_effect=fake_submit
    ), patch("resources.lib.resolver._FALLBACK_PREWARM_POLL_SECONDS", 0.01), patch(
        "resources.lib.resolver.xbmcgui"
    ) as mock_gui:
        mock_gui.Window.return_value.getProperty.return_value = "true"
        state = _start_fallback_submit_worker(
            candidates=[{"nzb_url": "x"}],
            prewarm_delay=5,
            wait_for_playback=True,
        )
        _signal_fallback_playback_started(state)
        _time.sleep(0.05)
        state["stop"].set()
        state["thread"].join(timeout=2)

    assert not submitted


def test_fallback_worker_inactive_property_during_prewarm_aborts_submit():
    """After playback is signaled and the cross-process ``nzbdav.playing``
    liveness flag has been observed live, it going non-"true" (service.py
    cleared it on stop/end) must abort the prewarm wait so no standby NZBs are
    submitted for a dead session."""
    from resources.lib.resolver import (
        _signal_fallback_playback_started,
        _start_fallback_submit_worker,
    )

    submitted = []

    def fake_submit(*_args, **_kwargs):
        submitted.append(True)

    playing_value = {"v": "true"}

    def get_property(name):
        return playing_value["v"] if name == "nzbdav.playing" else ""

    with patch(
        "resources.lib.resolver._submit_fallback_candidates", side_effect=fake_submit
    ), patch("resources.lib.resolver._FALLBACK_PREWARM_POLL_SECONDS", 0.01), patch(
        "resources.lib.resolver.xbmcgui"
    ) as mock_gui:
        mock_gui.Window.return_value.getProperty.side_effect = get_property
        state = _start_fallback_submit_worker(
            candidates=[{"nzb_url": "x"}],
            prewarm_delay=5,
            wait_for_playback=True,
        )
        _signal_fallback_playback_started(state)
        _time.sleep(0.05)
        # Simulate service.py clearing nzbdav.playing on stop/end (cross-process).
        playing_value["v"] = ""
        # prewarm_delay is 5s but the worker must abort well before that once the
        # liveness flag clears; a short join proves it returned early rather than
        # the join merely timing out before a still-pending submit.
        state["thread"].join(timeout=2)

    assert not state["thread"].is_alive(), "worker did not abort on inactive flag"
    assert not submitted
    assert not state["stop"].is_set()


def test_fallback_worker_submits_when_playback_stays_live():
    """REGRESSION (J): while ``nzbdav.playing`` stays "true" for the whole
    prewarm window, the worker must submit the standby backups. The prior fix
    polled ``nzbdav.active``, which ``service._check_active()`` consumes on the
    first tick after playback starts, so the worker wrongly read "inactive" and
    never submitted during normal service-monitored playback."""
    from resources.lib.resolver import (
        _signal_fallback_playback_started,
        _start_fallback_submit_worker,
    )

    submitted = []

    def fake_submit(*_args, **_kwargs):
        submitted.append(True)

    queried = []

    def get_property(name):
        # nzbdav.playing stays live; nzbdav.active is already consumed/empty.
        queried.append(name)
        return "true" if name == "nzbdav.playing" else ""

    with patch(
        "resources.lib.resolver._submit_fallback_candidates", side_effect=fake_submit
    ), patch("resources.lib.resolver._FALLBACK_PREWARM_POLL_SECONDS", 0.01), patch(
        "resources.lib.resolver.xbmcgui"
    ) as mock_gui:
        mock_gui.Window.return_value.getProperty.side_effect = get_property
        state = _start_fallback_submit_worker(
            candidates=[{"nzb_url": "x"}],
            prewarm_delay=0.05,
            wait_for_playback=True,
        )
        _signal_fallback_playback_started(state)
        state["thread"].join(timeout=2)

    assert not state["thread"].is_alive()
    assert submitted, "worker must submit while playback stays live"
    # Pin the actual fix: the worker must consult the DEDICATED liveness flag
    # ``nzbdav.playing``, never the consume-once ``nzbdav.active`` the buggy
    # version polled. Without this the test is a false-green (the old code also
    # reaches a submit, just via the degraded never-latched path).
    assert "nzbdav.playing" in queried
    assert "nzbdav.active" not in queried


def test_fallback_worker_submits_when_liveness_never_set():
    """When ``nzbdav.playing`` is never set (e.g. the await-playback cap path /
    an unusual handoff where service never marked liveness), the seen-live latch
    never engages, so the worker degrades to a late submit rather than being
    wrongly stranded."""
    from resources.lib.resolver import (
        _signal_fallback_playback_started,
        _start_fallback_submit_worker,
    )

    submitted = []

    def fake_submit(*_args, **_kwargs):
        submitted.append(True)

    with patch(
        "resources.lib.resolver._submit_fallback_candidates", side_effect=fake_submit
    ), patch("resources.lib.resolver._FALLBACK_PREWARM_POLL_SECONDS", 0.01), patch(
        "resources.lib.resolver.xbmcgui"
    ) as mock_gui:
        # Liveness flag never observed live (always empty).
        mock_gui.Window.return_value.getProperty.return_value = ""
        state = _start_fallback_submit_worker(
            candidates=[{"nzb_url": "x"}],
            prewarm_delay=0.05,
            wait_for_playback=True,
        )
        _signal_fallback_playback_started(state)
        state["thread"].join(timeout=2)

    assert not state["thread"].is_alive()
    assert submitted, "never-live liveness must not strand the late submit"


def test_resolve_delegates_to_nzbget_when_enabled():
    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: (
        "true" if key == "nzbget_enabled" else ""
    )
    with patch.object(sys.modules["xbmcaddon"], "Addon", return_value=addon), patch(
        "resources.lib.nzbget_resolver.resolve_and_play_nzbget"
    ) as nzbget_entry, patch(
        "resources.lib.resolver._clear_kodi_playback_state", return_value=137.0
    ) as scrub, patch(
        "resources.lib.resolver._resolve_resume_choice",
        side_effect=lambda params, scrubbed: ("rel-id", scrubbed),
    ) as resume_choice:
        resolve(7, {"nzburl": "http%3A%2F%2Fi%2Fx.nzb", "title": "X"})
    # Assert the exact handle + params payload is forwarded, plus the chosen
    # resume offset and the release identity the monitor keys on — so a
    # regression in handle/params routing or in carrying the resume choice is
    # caught.
    nzbget_entry.assert_called_once_with(
        7,
        {"nzburl": "http%3A%2F%2Fi%2Fx.nzb", "title": "X"},
        resume_seconds=137.0,
        resume_key="rel-id",
    )
    # Resume is resolved (release-identity lookup + native prompt) against the
    # scrubbed bookmark before the handoff.
    resume_choice.assert_called_once_with(
        {"nzburl": "http%3A%2F%2Fi%2Fx.nzb", "title": "X"}, 137.0
    )
    # The stale TMDBHelper bookmark must be scrubbed before the NZBGet handoff
    # (NZBGet bypasses the nzbdav playback-state cleanup).
    scrub.assert_called_once()


def test_resolve_nzbget_cancel_prompt_resolves_false_and_does_not_play():
    # When the user cancels the native resume prompt (chosen is None), the
    # handle-based NZBGet path must satisfy the setResolvedUrl(False) failure
    # contract, clear the playlist, and never hand off to NZBGet playback.
    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: (
        "true" if key == "nzbget_enabled" else ""
    )
    with patch.object(sys.modules["xbmcaddon"], "Addon", return_value=addon), patch(
        "resources.lib.nzbget_resolver.resolve_and_play_nzbget"
    ) as nzbget_entry, patch(
        "resources.lib.resolver._clear_kodi_playback_state", return_value=137.0
    ), patch(
        "resources.lib.resolver._resolve_resume_choice", return_value=("rel-id", None)
    ), patch(
        "resources.lib.resolver.xbmcplugin"
    ) as mock_plugin, patch(
        "resources.lib.resolver.xbmc"
    ) as mock_xbmc:
        resolve(7, {"nzburl": "http%3A%2F%2Fi%2Fx.nzb", "title": "X"})
    nzbget_entry.assert_not_called()
    assert mock_plugin.setResolvedUrl.call_args[0][:2] == (7, False)
    mock_xbmc.PlayList.return_value.clear.assert_called_once()


def test_resolve_skips_nzbget_when_disabled():
    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: ""  # nzbget_enabled falsy
    with patch.object(sys.modules["xbmcaddon"], "Addon", return_value=addon), patch(
        "resources.lib.nzbget_resolver.resolve_and_play_nzbget"
    ) as nzbget_entry, patch(
        "resources.lib.resolver._picker_completed_stream", return_value=None
    ), patch(
        "resources.lib.resolver._get_poll_settings", return_value=(1, 60)
    ), patch(
        "resources.lib.resolver._maybe_clear_queue_before_submit"
    ), patch(
        "resources.lib.resolver._poll_until_ready", return_value=(None, None)
    ):
        # Proceeds down the nzbdav path (poll returns no stream -> resolve False).
        # No try/except: an unexpected raise here must fail the test, not be
        # swallowed into a passing assert_not_called.
        resolve(7, {"nzburl": "http%3A%2F%2Fi%2Fx.nzb", "title": "X"})
    nzbget_entry.assert_not_called()


def test_resolve_and_play_delegates_to_nzbget_when_enabled():
    # resolve_and_play is the dominant playback entry point (TMDBHelper
    # /resolve, the in-addon search picker, script-play). With the toggle on
    # it must hand off to the handle-less play_nzbget, not the nzbdav path.
    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: (
        "true" if key == "nzbget_enabled" else ""
    )
    with patch.object(sys.modules["xbmcaddon"], "Addon", return_value=addon), patch(
        "resources.lib.nzbget_resolver.play_nzbget"
    ) as play_entry, patch(
        "resources.lib.resolver._clear_kodi_playback_state", return_value=90.0
    ) as scrub, patch(
        "resources.lib.resolver._resolve_resume_choice",
        side_effect=lambda params, scrubbed: ("rel-id", scrubbed),
    ) as resume_choice:
        resolve_and_play("http://i/x.nzb", "X", params={})
    # Assert the exact nzb_url/title/params payload is forwarded to the
    # handle-less entry plus the carried resume offset and release identity.
    play_entry.assert_called_once_with(
        "http://i/x.nzb", "X", {}, resume_seconds=90.0, resume_key="rel-id"
    )
    resume_choice.assert_called_once_with({"title": "X"}, 90.0)
    scrub.assert_called_once()  # bookmark scrubbed before the NZBGet handoff


def test_resolve_and_play_nzbget_cancel_prompt_does_not_play():
    # On the handle-less path there is no plugin handle, so a cancelled resume
    # prompt (chosen is None) must simply not start playback — no setResolvedUrl
    # in this path, matching its contract.
    addon = MagicMock()
    addon.getSetting.side_effect = lambda key: (
        "true" if key == "nzbget_enabled" else ""
    )
    with patch.object(sys.modules["xbmcaddon"], "Addon", return_value=addon), patch(
        "resources.lib.nzbget_resolver.play_nzbget"
    ) as play_entry, patch(
        "resources.lib.resolver._clear_kodi_playback_state", return_value=90.0
    ), patch(
        "resources.lib.resolver._resolve_resume_choice", return_value=("rel-id", None)
    ):
        resolve_and_play("http://i/x.nzb", "X", params={})
    play_entry.assert_not_called()


def test_resolve_and_play_reads_toggle_via_injected_getter():
    # The handle-less path passes _settings_getter to avoid Kodi settings-API
    # reads during RunScript/widget plays. The NZBGet toggle must be read
    # through that getter; if xbmcaddon.Addon is unavailable the injected
    # getter still enables NZBGet rather than silently falling back to nzbdav.
    def injected(key, default=""):
        return "true" if key == "nzbget_enabled" else default

    with patch.object(
        sys.modules["xbmcaddon"], "Addon", side_effect=RuntimeError("unavailable")
    ), patch("resources.lib.nzbget_resolver.play_nzbget") as play_entry, patch(
        "resources.lib.resolver._clear_kodi_playback_state", return_value=0.0
    ), patch(
        "resources.lib.resolver._resolve_resume_choice", return_value=("", 0.0)
    ):
        params = {"_settings_getter": injected}
        resolve_and_play("http://i/x.nzb", "X", params=params)
    play_entry.assert_called_once_with(
        "http://i/x.nzb", "X", params, resume_seconds=0.0, resume_key=""
    )


# --- CodeRabbit PR #358 quality-fix regression tests ---


def _joined_log(xbmc_mock):
    """Concatenate every xbmc.log() message for substring assertions."""
    return " ".join(str(c) for c in xbmc_mock.log.call_args_list)


def test_queue_probe_exception_redacted_before_logging():
    """Finding 1: queue-probe exception strings are redacted before logging."""
    secret_url = "http://nzbdav/api?apikey=SUPERSECRET123"
    with patch("resources.lib.resolver.xbmc") as xbmc_mock, patch(
        "resources.lib.resolver._clear_queue_on_submit_mode", return_value="ask"
    ), patch(
        "resources.lib.resolver.get_queue_slots",
        side_effect=Exception("probe failed " + secret_url),
    ):
        _maybe_clear_queue_before_submit(
            "Some.Title", settings_getter=lambda k, d=None: "0"
        )
    logged = _joined_log(xbmc_mock)
    assert "SUPERSECRET123" not in logged
    assert "apikey=REDACTED" in logged


def test_resume_stream_url_redacted_before_logging():
    """Finding 2: direct-play stream URLs are redacted in INFO logs."""
    secret_url = "http://host/stream.mp4?apikey=SUPERSECRET123"
    with patch("resources.lib.resolver.xbmc") as xbmc_mock, patch(
        "resources.lib.resolver.xbmcgui"
    ):
        _finish_player_playback({"stream_url": secret_url, "stream_headers": {}})
    logged = _joined_log(xbmc_mock)
    assert "SUPERSECRET123" not in logged
    assert "apikey=REDACTED" in logged


def test_submit_error_message_redacted_before_logging():
    """Finding 3: backend submit-error messages are redacted before logging."""
    submit_error = {
        "status": "rejected",
        "message": "rejected http://indexer/getnzb?apikey=SUPERSECRET123",
    }
    with patch("resources.lib.resolver.xbmc") as xbmc_mock, patch(
        "resources.lib.resolver.xbmcgui"
    ), patch(
        "resources.lib.resolver._submit_nzb_with_ui_pump",
        return_value=(None, submit_error),
    ), patch(
        "resources.lib.resolver._show_submit_error_dialog"
    ):
        result = _submit_nzb_with_retries(
            "nzburl", "Title", MagicMock(), MagicMock(), max_submit_retries=1
        )
    assert result is None
    logged = _joined_log(xbmc_mock)
    assert "SUPERSECRET123" not in logged
    assert "apikey=REDACTED" in logged


def test_fallback_worker_thread_start_failure_fails_soft():
    """Finding 4: a RuntimeError from thread.start() is caught, not propagated."""
    with patch("resources.lib.resolver.xbmc"), patch(
        "resources.lib.resolver.threading.Thread"
    ) as thread_cls:
        thread = MagicMock()
        thread.start.side_effect = RuntimeError("can't start new thread")
        thread_cls.return_value = thread
        state = _start_fallback_submit_worker(
            candidates=[{"nzburl": "http://i/x.nzb", "title": "X"}]
        )
    assert state["thread"] is None
    assert state["finished"].is_set()


def test_resolve_missing_url_resolves_false_even_if_dialog_raises():
    """Finding 5: setResolvedUrl(False) still runs if the notification raises."""
    with patch("resources.lib.resolver.xbmc"), patch(
        "resources.lib.resolver.xbmcgui"
    ) as gui, patch("resources.lib.resolver.xbmcplugin") as plugin:
        gui.Dialog.return_value.ok.side_effect = RuntimeError("no UI")
        resolve(7, {})
    plugin.setResolvedUrl.assert_called_once()
    args = plugin.setResolvedUrl.call_args[0]
    assert args[0] == 7 and args[1] is False


def test_handle_resolve_exception_resolves_false_even_if_dialog_raises():
    """Finding 6: an exception-path dialog must not block the False resolution."""
    with patch("resources.lib.resolver.xbmc"), patch(
        "resources.lib.resolver.xbmcgui"
    ) as gui, patch("resources.lib.resolver.xbmcplugin") as plugin:
        gui.Dialog.return_value.ok.side_effect = RuntimeError("no UI")
        _handle_resolve_exception("label", Exception("boom"), handle=9)
    plugin.setResolvedUrl.assert_called_once()
    args = plugin.setResolvedUrl.call_args[0]
    assert args[0] == 9 and args[1] is False


def test_poll_once_by_name_terminal_threads_completed_timestamp():
    """Finding 7: the synthesized by-name terminal row carries ``completed``."""
    completed_ts = 1_000_000
    slot = {
        "status": "Failed",
        "storage": "",
        "name": "My.Title",
        "nzo_id": "nzo-new",
        "fail_message": "boom",
        "completed": completed_ts,
    }
    with patch("resources.lib.resolver.xbmc"), patch(
        "resources.lib.resolver.get_job_status", return_value=None
    ), patch("resources.lib.resolver.get_job_history", return_value=None), patch(
        "resources.lib.nzbdav_api.find_terminal_by_name", return_value=slot
    ):
        _, history_status, _ = _poll_once(
            "nzo-old", "My.Title", MagicMock(), submit_started_wall=completed_ts
        )
    assert history_status is not None
    assert history_status.get("status") == "Failed"
    assert history_status.get("completed") == completed_ts
    assert history_status.get("nzo_id") == "nzo-new"


def test_late_accepted_submit_is_cancelled_after_user_abort():
    """Finding 8: an nzo_id accepted after user cancel is cancelled in nzbdav."""
    submit_release = threading.Event()
    cancelled = threading.Event()

    def fake_submit(nzb_url, title, **kwargs):
        submit_release.wait(2)
        return "nzo-late", None

    def fake_cancel(nzo_id, **kwargs):
        cancelled.set()
        return True

    dialog = MagicMock()
    dialog.iscanceled.return_value = True
    monitor = MagicMock()
    monitor.waitForAbort.return_value = False
    monitor.abortRequested.return_value = False

    with patch("resources.lib.resolver.xbmc"), patch(
        "resources.lib.resolver.xbmcgui"
    ), patch("resources.lib.resolver.submit_nzb", side_effect=fake_submit), patch(
        "resources.lib.resolver.cancel_job", side_effect=fake_cancel
    ), patch(
        "resources.lib.resolver._get_submit_timeout_seconds", return_value=60
    ):
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://i/x.nzb", "Title", dialog, monitor
        )
        assert nzo_id is None
        assert submit_error["status"] == "cancelled"
        # Release the still-blocked addurl worker so it returns its late nzo_id.
        submit_release.set()
        assert cancelled.wait(2), "late-accepted submit was not cancelled"


def test_kodi_video_db_version_parses_numeric_version():
    """Finding 9: version key parses the integer schema version."""
    assert _kodi_video_db_version("/db/MyVideos131.db") == 131
    assert _kodi_video_db_version("/db/MyVideos99.db") == 99
    assert _kodi_video_db_version("/db/Textures13.db") == -1


def test_locate_kodi_video_db_picks_highest_numeric_version():
    """Finding 9: newest DB chosen by numeric version, not lexicographic sort."""
    with patch("resources.lib.resolver.xbmc") as xbmc_mock, patch(
        "resources.lib.resolver.xbmcvfs"
    ) as vfs_mock, patch("glob.glob") as glob_mock:
        xbmc_mock.Player.return_value.isPlayingVideo.return_value = False
        vfs_mock.translatePath.return_value = "/db/"
        # Lexicographic sort would wrongly pick MyVideos99.db.
        glob_mock.return_value = [
            "/db/MyVideos99.db",
            "/db/MyVideos131.db",
            "/db/MyVideos100.db",
        ]
        result = _locate_kodi_video_db()
    assert result == "/db/MyVideos131.db"


def test_completed_copy_blocks_clear_fails_soft_on_probe_thread_start_failure():
    """A RuntimeError from the adopt-probe thread.start() (Kodi teardown) must
    not escape the clear-queue guard: fail soft like a probe timeout and leave
    the queue intact (return True = SKIP the clear) instead of aborting submit."""
    from resources.lib.resolver_queueclear import _completed_copy_blocks_clear

    with patch("resources.lib.resolver.xbmc"), patch(
        "resources.lib.resolver.threading.Thread"
    ) as thread_cls:
        thread = MagicMock()
        thread.start.side_effect = RuntimeError("can't start new thread")
        thread_cls.return_value = thread
        # Must not raise; must return True (queue left intact).
        blocked = _completed_copy_blocks_clear("Title", lambda _k, _d=None: "")
    assert blocked is True


def test_advertised_size_bytes_non_finite_numeric_fails_open():
    """#282 follow-up: a non-finite numeric size (inf from a JSON overflow
    literal, or nan) must fail OPEN (return 0) instead of raising OverflowError /
    ValueError out of the resolver -- neither is in _RESOLVE_RUNTIME_ERRORS, so
    an escape would skip the setResolvedUrl-on-failure path. The int/float branch
    now matches the string branch's fail-open contract."""
    from resources.lib.resolver import _advertised_size_bytes, _stub_min_size_floor

    assert _advertised_size_bytes(float("inf")) == 0
    assert _advertised_size_bytes(float("-inf")) == 0
    assert _advertised_size_bytes(float("nan")) == 0
    # finite values still parse normally.
    assert _advertised_size_bytes(5.0) == 5
    assert _advertised_size_bytes(81_610_612_736) == 81_610_612_736
    # the floor helper must not propagate the error either.
    assert _stub_min_size_floor(float("inf")) == 0
