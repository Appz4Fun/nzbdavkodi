# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import sys
import threading
import time as _time
from unittest.mock import MagicMock, patch

from resources.lib.resolver import (
    _DOWNLOAD_TIMEOUT_MAX,
    _DOWNLOAD_TIMEOUT_MIN,
    _POLL_INTERVAL_MAX,
    _POLL_INTERVAL_MIN,
    MAX_POLL_ITERATIONS,
    _cache_bust_url,
    _clear_kodi_playback_state,
    _completed_job_stream,
    _direct_playback_service_config,
    _existing_completed_stream,
    _fallback_submit_jobs_snapshot,
    _get_poll_settings,
    _get_submit_timeout_seconds,
    _handle_history_result,
    _handle_job_status,
    _handle_resolve_exception,
    _make_playable_listitem,
    _play_direct,
    _play_via_proxy,
    _poll_once,
    _poll_until_ready,
    _prefetch_fallback_candidate_loader,
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
    assert elapsed < 0.09, "proxy config lookup took {:.3f}s".format(elapsed)


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
    def stuck_update(*_args):
        _time.sleep(0.25)

    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    dialog.update.side_effect = stuck_update

    started = _time.perf_counter()
    should_stop, last_status = _handle_job_status(
        {"status": "Downloading", "percentage": "99"},
        "nzo_stuck_dialog",
        dialog,
        None,
    )
    elapsed = _time.perf_counter() - started

    assert should_stop is False
    assert last_status == "Downloading"
    assert elapsed < 0.05


@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.find_completed_by_name")
def test_existing_completed_stream_ignores_partial_history_row(
    mock_find_completed, mock_find_video
):
    mock_find_completed.return_value = {"status": "Completed"}

    assert _existing_completed_stream("movie.mkv") is None
    mock_find_video.assert_not_called()


@patch("resources.lib.resolver._find_video_stream_for_folder")
def test_completed_job_stream_passes_settings_getter_to_webdav_lookup(mock_find_stream):
    def settings_getter(_key, default=""):
        return default

    mock_find_stream.return_value = (
        "/content/uncategorized/movie/movie.mkv",
        "http://webdav/movie.mkv",
        {"Authorization": "Basic x"},
    )
    completed_job = {
        "status": "Completed",
        "name": "movie.mkv",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }

    stream = _completed_job_stream(
        "movie.mkv",
        completed_job,
        settings_getter=settings_getter,
    )

    assert stream == ("http://webdav/movie.mkv", {"Authorization": "Basic x"})
    mock_find_stream.assert_called_once_with(
        "/content/uncategorized/movie/",
        settings_getter=settings_getter,
    )


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
    timeInSeconds REAL
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
            _clear_kodi_playback_state({"tmdb_id": "389", "type": "movie"})

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
            _clear_kodi_playback_state()

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
    call_order = []

    def poll_ready(*args, **kwargs):
        call_order.append("poll")
        assert mock_start_fallback.call_count == 0
        kwargs["on_primary_submitted"]("SABnzbd_nzo_primary")
        mock_start_fallback.assert_called_once_with(
            fallback_candidates, candidate_loader=None
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
    mock_snapshot.assert_called_once_with(fallback_state)
    mock_start_fallback.assert_called_once_with(
        fallback_candidates, candidate_loader=None
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
    mock_finish_playback.assert_called_once_with(1, prepared_playback)
    mock_stop_fallback.assert_not_called()


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

    mock_snapshot.assert_called_once_with(fallback_state)
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
    mock_finish_playback.assert_called_once_with(1, {"state": "prepared"})


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
    mock_finish_playback.assert_called_once_with(1, {"state": "prepared"})


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
        kwargs["on_primary_submitted"]("SABnzbd_nzo_primary")
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
        kwargs["on_primary_submitted"]("SABnzbd_nzo_primary")
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
    assert elapsed < 0.35, "selected-to-play path took {:.3f}s".format(elapsed)


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
        kwargs["on_primary_submitted"]("SABnzbd_nzo_primary")
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
    assert ready_to_play < 0.05, (
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
        elapsed_to_submit < 0.05
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
        kwargs["on_primary_submitted"]("SABnzbd_nzo_primary")
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
    elapsed = timing["resolved"] - timing["ready"]
    assert elapsed < 0.32, "ready-to-resolved stayed serial at {:.3f}s".format(elapsed)


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
        elapsed_to_resolved < 0.08
    ), "remux cache prompt delayed setResolvedUrl by {:.3f}s".format(
        elapsed_to_resolved
    )
    mock_cache_prompt.assert_not_called()


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
    mock_finish_playback.assert_called_once_with(1, {"state": "prepared"})
    mock_clear_state.assert_called_once()


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
        kwargs["on_primary_submitted"]("SABnzbd_nzo_primary")
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
    mock_finish_playback.assert_called_once_with({"state": "prepared"})
    cleanup_can_finish.set()


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
    _mock_clear_state,
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
    mock_finish_playback.assert_called_once_with({"state": "prepared"})


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

    mock_snapshot.assert_called_once_with(fallback_state)
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
    mock_finish_playback.assert_called_once_with({"state": "prepared"})


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
        elapsed_to_play < 0.08
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

    def find_video(_path):
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
    assert elapsed < 0.35, "existing-completed selected-to-play took {:.3f}s".format(
        elapsed
    )


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
        elapsed < 0.1
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
    assert elapsed < 0.08, "completed hint paid dialog startup {:.3f}s".format(elapsed)
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

    def slow_find_video(folder):
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
        elapsed_to_submit < 0.18
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
        kwargs["on_primary_submitted"]("SABnzbd_nzo_primary")
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
        kwargs["on_primary_submitted"]("SABnzbd_nzo_primary")
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
    }

    def settings_getter(key, default=""):
        return values.get(key, default)

    resolve_and_play(
        "http://hydra/getnzb/primary",
        "movie.mkv",
        params={"_settings_getter": settings_getter},
    )

    assert mock_prepare.call_args.kwargs["settings_snapshot"] == values


@patch("resources.lib.webdav.urlopen")
@patch("resources.lib.webdav._get_settings")
def test_completed_history_reuses_webdav_settings_for_stream_url(
    mock_settings, mock_urlopen
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
    assert elapsed < 0.07, "completed-history stream URL took {:.3f}s".format(elapsed)


@patch("resources.lib.stream_proxy.prepare_stream_via_service")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
def test_completed_history_propfind_length_hint_does_not_override_stream_head(
    mock_find_video,
    mock_stream_url,
    mock_prepare_via_service,
):
    """PROPFIND sizes can be stale; proxy prepare must use the HTTP stream HEAD."""
    from resources.lib.resolver import _handle_history_result, _prepare_direct_playback

    mock_find_video.return_value = "/content/uncategorized/movie/movie.mkv"
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
        prepare_token="token",
    )


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
        kwargs["on_primary_submitted"]("SABnzbd_nzo_primary")
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
    assert elapsed < 0.15
    assert 0 not in [call.args[0] for call in monitor.waitForAbort.call_args_list]


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
    def terminal_submit(_nzb_url, _title, **_kwargs):
        _time.sleep(0.03)
        return None, {"status": 400, "message": "TooManyRequests"}

    def slow_probe(*_args, **_kwargs):
        _time.sleep(0.25)

    mock_submit.side_effect = terminal_submit
    mock_find_queued.side_effect = slow_probe
    mock_find_completed.side_effect = slow_probe
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = MagicMock()
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
    assert elapsed < 0.15


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
    started = _time.perf_counter()
    prepared = _wait_direct_playback_prepare(state, wait_seconds=0.01)
    elapsed = _time.perf_counter() - started

    assert elapsed >= 0.03
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

    def delayed_submit(_nzb_url, _title):
        assert queue_seen.wait(timeout=1)
        submit_can_finish.wait(timeout=0.75)
        return "SABnzbd_nzo_submitted_late", None

    def queued_job(_title):
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

    started = _time.perf_counter()
    try:
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
    finally:
        submit_can_finish.set()
    elapsed = _time.perf_counter() - started

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_existing_queue", None)
    assert elapsed < 0.035, "existing queue adoption took {:.3f}s".format(elapsed)


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
@patch("resources.lib.resolver.find_queued_by_name")
@patch("resources.lib.resolver.submit_nzb")
def test_submit_ui_pump_rechecks_queue_quickly_after_initial_fast_miss(
    mock_submit, mock_find_queued, _mock_find_completed
):
    """A queue hit just after the first probe should not wait 200 ms."""
    submit_can_finish = threading.Event()
    queue_probe_times = []

    def delayed_submit(_nzb_url, _title):
        submit_can_finish.wait(timeout=0.75)
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

    started = _time.perf_counter()
    try:
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
    finally:
        submit_can_finish.set()
    elapsed = _time.perf_counter() - started

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_second_fast_probe", None)
    assert len(queue_probe_times) == 2
    assert elapsed < 0.18, "second fast queue probe took {:.3f}s".format(elapsed)


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

    started = _time.perf_counter()
    try:
        with patch(
            "resources.lib.resolver._SUBMIT_HISTORY_PROBE_PARALLEL_GRACE_SECONDS",
            0.2,
        ):
            nzo_id, submit_error = _submit_nzb_with_ui_pump(
                "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
            )
    finally:
        submit_can_finish.set()
    elapsed = _time.perf_counter() - started
    history_delay = history_probe_times[0] - queue_probe_times[0]

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_completed_fast_history", None)
    assert history_delay < 0.05, (
        "completed-history probe waited for grace after queue miss; "
        "history_delay={:.3f}s elapsed={:.3f}s".format(history_delay, elapsed)
    )
    assert elapsed < 0.08, "completed-history adoption took {:.3f}s".format(elapsed)


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
        elapsed < 0.18
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

    def slow_queue_miss(_title):
        _time.sleep(0.12)

    mock_find_queued.side_effect = slow_queue_miss
    mock_find_completed.return_value = {
        "nzo_id": "SABnzbd_nzo_completed_timeout",
        "name": "movie.mkv",
        "status": "Completed",
    }
    monitor = _make_monitor()

    started = _time.perf_counter()
    nzo_id = _adopt_queued_or_completed_job("movie.mkv", monitor)
    elapsed = _time.perf_counter() - started

    assert nzo_id == "SABnzbd_nzo_completed_timeout"
    assert (
        elapsed < 0.07
    ), "timeout adoption serialized history behind queue miss: {:.3f}s".format(elapsed)


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
    assert elapsed < 0.08, "late-history catch took {:.3f}s".format(elapsed)
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

    def delayed_submit(_nzb_url, _title):
        submit_can_finish.wait(timeout=0.75)
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

    def slow_history_miss(_title):
        _time.sleep(0.18)

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = queued_job
    mock_find_completed.side_effect = slow_history_miss
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
    probe_gap = queue_probe_times[1] - queue_probe_times[0]

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_second_fast_probe", None)
    assert len(queue_probe_times) == 2
    assert elapsed < 0.14, (
        "slow history miss blocked second queue probe; "
        "elapsed={:.3f}s probe_gap={:.3f}s".format(elapsed, probe_gap)
    )


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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

    started = _time.perf_counter()
    nzo_id, submit_error = _submit_nzb_with_ui_pump(
        "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
    )
    elapsed = _time.perf_counter() - started

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_fast_submit", None)
    assert (
        elapsed < 0.04
    ), "fast submit result waited for poll tick; elapsed={:.3f}s".format(elapsed)


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
        elapsed < 0.08
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

    def delayed_submit(_nzb_url, _title):
        submit_can_finish.wait(timeout=0.75)
        return "SABnzbd_nzo_submitted", None

    def slow_queue_miss(_title):
        _time.sleep(0.14)

    mock_submit.side_effect = delayed_submit
    mock_find_queued.side_effect = slow_queue_miss
    mock_find_completed.return_value = {
        "nzo_id": "SABnzbd_nzo_completed_probe",
        "name": "movie.mkv",
        "status": "Completed",
    }
    dialog = MagicMock()
    dialog.iscanceled.return_value = False
    monitor = _make_monitor()

    started = _time.perf_counter()
    try:
        nzo_id, submit_error = _submit_nzb_with_ui_pump(
            "http://hydra/getnzb/abc", "movie.mkv", dialog, monitor
        )
    finally:
        submit_can_finish.set()
    elapsed = _time.perf_counter() - started

    assert (nzo_id, submit_error) == ("SABnzbd_nzo_completed_probe", None)
    assert (
        elapsed < 0.08
    ), "completed history adoption waited behind queue miss for {:.3f}s".format(elapsed)


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
    mock_poll.return_value = (poll_interval, 3600)
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

    assert elapsed < 0.15
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

    def slow_history(_nzo_id):
        _time.sleep(0.25)

    mock_status.return_value = {"status": "Downloading", "percentage": "50"}
    mock_history.side_effect = slow_history

    started = _time.monotonic()
    job_status, history, webdav_error = _poll_once(
        "nzo_active", "movie", _make_monitor()
    )
    elapsed = _time.monotonic() - started

    assert elapsed < 0.15
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

    def slow_history(_nzo_id):
        _time.sleep(0.25)

    mock_status.return_value = {"status": "Downloading", "percentage": "100"}
    mock_history.side_effect = slow_history

    started = _time.monotonic()
    job_status, history, webdav_error = _poll_once("nzo_full", "movie", _make_monitor())
    elapsed = _time.monotonic() - started

    assert elapsed < 0.15, "100% queue row waited on history for {:.3f}s".format(
        elapsed
    )
    assert job_status == mock_status.return_value
    assert history is None
    assert webdav_error is None
    mock_probe.assert_not_called()


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
    assert elapsed < 0.15
    monitor.waitForAbort.assert_not_called()


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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

    started = _time.perf_counter()
    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 0.2, 3600
    )
    elapsed = _time.perf_counter() - started

    assert url == "http://webdav/movie.mkv"
    assert headers == {"Authorization": "x"}
    assert elapsed < 0.14, "nearly-complete poll delayed WebDAV by {:.3f}s".format(
        elapsed
    )


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
        if len(history_calls) == 1:
            _time.sleep(0.12)
        return completed_history

    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)
    mock_xbmc.Monitor.return_value = monitor
    mock_history.side_effect = get_history
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = ("http://webdav/movie.mkv", {"Authorization": "x"})

    started = _time.perf_counter()
    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 0.25, 3600
    )
    elapsed = _time.perf_counter() - started

    assert url == "http://webdav/movie.mkv"
    assert headers == {"Authorization": "x"}
    assert len(history_calls) == 1
    assert elapsed < 0.25, "full-progress history missed grace by {:.3f}s".format(
        elapsed
    )


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
@patch("resources.lib.resolver._submit_nzb_with_retries", return_value="nzo_abc")
@patch("resources.lib.resolver.get_webdav_stream_url_for_path")
@patch("resources.lib.resolver.find_video_file")
@patch("resources.lib.resolver.get_job_history")
@patch("resources.lib.resolver.get_job_status")
@patch("resources.lib.resolver.xbmc")
def test_poll_until_ready_repolls_full_progress_history_miss_before_full_tick(
    mock_xbmc,
    mock_status,
    mock_history,
    mock_find,
    mock_stream_url,
    _mock_submit,
    _mock_find_completed,
):
    """A 100% queue row with a fast history miss should not wait a full tick."""
    completed_history = {
        "status": "Completed",
        "storage": "/mnt/nzbdav/completed-symlinks/uncategorized/movie",
    }
    history_calls = []

    mock_status.return_value = {"status": "Downloading", "percentage": "100"}

    def get_history(_nzo_id):
        history_calls.append(_time.perf_counter())
        if len(history_calls) == 1:
            return None
        return completed_history

    monitor = MagicMock()
    monitor.waitForAbort.side_effect = lambda seconds: (_time.sleep(seconds) or False)
    mock_xbmc.Monitor.return_value = monitor
    mock_history.side_effect = get_history
    mock_find.return_value = "/content/uncategorized/movie/movie.mkv"
    mock_stream_url.return_value = ("http://webdav/movie.mkv", {"Authorization": "x"})

    started = _time.perf_counter()
    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 1, 3600
    )
    elapsed = _time.perf_counter() - started

    assert url == "http://webdav/movie.mkv"
    assert headers == {"Authorization": "x"}
    assert len(history_calls) == 2
    history_gap = history_calls[1] - history_calls[0]
    assert history_gap < 0.15, "full-progress history retry gap was {:.3f}s".format(
        history_gap
    )
    assert elapsed < 0.25, "full-progress repoll waited {:.3f}s".format(elapsed)


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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

    started = _time.perf_counter()
    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 1, 3600
    )
    elapsed = _time.perf_counter() - started

    assert url == "http://webdav/content/uncategorized/movie/movie.mkv"
    assert headers == {"Authorization": "Basic primary"}
    assert elapsed < 0.3, "completed WebDAV recheck waited {:.3f}s".format(elapsed)


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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

    started = _time.perf_counter()
    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 1, 3600
    )
    elapsed = _time.perf_counter() - started

    assert url == "http://webdav/content/uncategorized/movie/movie.mkv"
    assert headers == {"Authorization": "Basic primary"}
    assert elapsed < 0.08, "first completed WebDAV recheck waited {:.3f}s".format(
        elapsed
    )


# --- HTTP error classification tests for the submit retry loop ---


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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


@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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

    url, headers = _poll_until_ready(
        "http://hydra/nzb", "movie", _make_dialog(), 2, 3600
    )

    assert url is None
    assert headers is None
    mock_cancel_job.assert_called_once_with("nzo_xyz")


# --- negative cleanup tests (Group B — cleanup must NOT fire) ---


@patch("resources.lib.resolver.cancel_job")
@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
@patch("resources.lib.resolver.find_completed_by_name", return_value=None)
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
