# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Unit tests for the per-session read-ahead prefetch cache.

The read-ahead layer is ADDITIVE in front of the hardened miss/recovery
path. These tests pin: the ReadAheadBuffer contiguous-prefix invariant,
free-behind / seek-repoint, the setting reader + snapshot wiring, the
serve-from-window helper, the additive _stream_upstream_range consult,
the bounded daemon prefetch run-loop, and lifecycle/teardown.
"""

import threading
from unittest.mock import MagicMock, patch

from resources.lib import stream_proxy
from resources.lib.stream_proxy import (
    _DEFAULT_READAHEAD_BUFFER_MB,
    _READAHEAD_BUFFER_KEY,
    _READAHEAD_BUFFER_MB_MAX,
    _READAHEAD_THREAD_KEY,
    _UPSTREAM_RANGE_OK,
    ReadAheadBuffer,
    _StreamHandler,
)


def _make_handler():
    handler = _StreamHandler.__new__(_StreamHandler)
    handler.wfile = MagicMock()
    return handler


def _make_proxy():
    """A bare StreamProxy for the spawn / run-loop methods (no real Kodi)."""
    return stream_proxy.StreamProxy.__new__(stream_proxy.StreamProxy)


# ---------------------------------------------------------------------------
# ReadAheadBuffer
# ---------------------------------------------------------------------------


def test_read_prefix_empty_returns_blank():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    assert buf.read_prefix(0, 99) == b""


def test_append_and_read_prefix_contiguous():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    assert buf.append(0, b"ABCDEFGHIJ") is True
    # Exact contiguous prefix from base.
    assert buf.read_prefix(0, 4) == b"ABCDE"
    # Capped to what is present.
    assert buf.read_prefix(0, 100) == b"ABCDEFGHIJ"


def test_read_prefix_requires_exact_base_match():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDEFGHIJ")
    # A request starting inside-but-not-at base is a gap/non-prefix -> miss.
    assert buf.read_prefix(3, 9) == b""


def test_read_prefix_never_exceeds_requested_or_content_length():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=5)
    # Only 5 bytes valid even if appended more is rejected past content_length.
    assert buf.append(0, b"ABCDEFGHIJ") is True
    assert len(buf.read_prefix(0, 100)) <= 5
    assert buf.read_prefix(0, 2) == b"ABC"


def test_append_rejects_out_of_order():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDE")
    # Non-contiguous (gap) append is ignored.
    assert buf.append(10, b"ZZZ") is False
    assert buf.read_prefix(0, 100) == b"ABCDE"


def test_append_truncates_at_content_length():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=8)
    buf.append(0, b"ABCDE")
    # Would exceed content_length=8; only 3 more bytes accepted.
    buf.append(5, b"FGHIJ")
    body = buf.read_prefix(0, 100)
    assert body == b"ABCDEFGH"
    assert buf.next_fetch_offset() == 8


def test_append_respects_cap():
    buf = ReadAheadBuffer(cap_bytes=8, content_length=10_000)
    buf.append(0, b"ABCDE")
    buf.append(5, b"FGHIJ")  # would overflow cap of 8
    assert len(buf.read_prefix(0, 100)) == 8
    assert buf.is_full() is True
    assert buf.space_remaining() == 0


def test_free_behind_advances_base_and_shrinks():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDEFGHIJ")
    buf.free_behind(4)
    assert buf.read_prefix(0, 9) == b""  # old base no longer valid
    assert buf.read_prefix(4, 9) == b"EFGHIJ"
    assert buf.next_fetch_offset() == 10


def test_free_behind_idempotent_for_old_offset():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDEFGHIJ")
    buf.free_behind(4)
    buf.free_behind(2)  # behind current base -> no-op
    assert buf.read_prefix(4, 9) == b"EFGHIJ"


def test_note_seek_in_window_trims_prefix_and_serves_lead():
    """Regression (id 3365909885): an in-window FORWARD seek must rebase the
    window to the seek target so read_prefix (which only serves at base_offset)
    serves the still-buffered lead from memory instead of missing and refetching
    bytes already held. The consumed prefix [old base, new_start) is dropped and
    the forward lead [new_start, window_end) is kept (next_fetch_offset stays)."""
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDEFGHIJ")
    buf.note_seek(3)  # inside [0, 10): forward seek into the buffered lead
    # The lead from the seek target is served straight from memory...
    assert buf.read_prefix(3, 9) == b"DEFGHIJ"
    # ...the consumed prefix is gone (read_prefix only serves at base_offset)...
    assert buf.read_prefix(0, 9) == b""
    # ...and the buffered lead was preserved, not discarded for a refetch.
    assert buf.next_fetch_offset() == 10


def test_note_seek_to_window_end_trims_all_and_resumes_forward():
    """Boundary: a seek to exactly window_end consumes the whole lead -- data
    empties, base rebases to the target, and the prefetch resumes forward from
    there (no negative slice / off-by-one)."""
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDEFGHIJ")  # window [0, 10)
    buf.note_seek(10)  # exactly window_end
    assert buf.read_prefix(10, 12) == b""
    assert buf.next_fetch_offset() == 10


def test_note_seek_outside_window_discards_and_repoints():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDEFGHIJ")
    buf.note_seek(5_000)  # forward past the lead
    assert buf.read_prefix(0, 9) == b""
    assert buf.read_prefix(5_000, 5_010) == b""  # nothing yet at new base
    assert buf.next_fetch_offset() == 5_000


def test_note_seek_backward_discards():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(100, b"ABCDE")  # base offset moved up first
    # backward seek before base
    buf.note_seek(0)
    assert buf.next_fetch_offset() == 0


def test_is_full_and_space_remaining():
    buf = ReadAheadBuffer(cap_bytes=10, content_length=10_000)
    assert buf.is_full() is False
    assert buf.space_remaining() == 10
    buf.append(0, b"ABCDEFGHIJ")
    assert buf.is_full() is True
    assert buf.space_remaining() == 0


def test_served_high_water_past_window_advances_next_fetch_offset():
    """Regression (id 3352749425): a direct upstream serve on a read-ahead
    MISS must advance next_fetch_offset past the served bytes so the prefetch
    daemon builds a forward lead instead of re-fetching from offset 0."""
    buf = ReadAheadBuffer(cap_bytes=256 * 1024 * 1024, content_length=10_000_000)
    # Startup miss: nothing buffered, base still at 0.
    assert buf.next_fetch_offset() == 0
    served_end = 4_000_000
    buf.update_served_high_water(served_end)
    # Buffer now knows the play head; prefetch resumes AHEAD of it.
    assert buf.next_fetch_offset() >= served_end
    assert buf.next_fetch_offset() == served_end


def test_served_high_water_within_window_keeps_forward_lead():
    """A high-water at/behind the buffered window (hit path, post free_behind)
    must NOT discard the forward lead the prefetch daemon already built."""
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDEFGHIJ")
    buf.free_behind(5)  # hit path consumes ABCDE; base=5, data=FGHIJ
    buf.update_served_high_water(5)
    # Forward lead preserved.
    assert buf.read_prefix(5, 9) == b"FGHIJ"
    assert buf.next_fetch_offset() == 10


def test_served_high_water_discards_stale_behind_data():
    """A miss served past stale buffered bytes drops the now-behind data and
    repoints the base to the served offset."""
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDE")  # stale prefetch at offset 0
    buf.update_served_high_water(500)  # played far past the stale bytes
    assert buf.read_prefix(0, 4) == b""
    assert buf.next_fetch_offset() == 500


def test_never_buffers_past_content_length_via_next_fetch_offset():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=3)
    buf.append(0, b"ABC")
    assert buf.next_fetch_offset() == 3
    # Further append at content_length is rejected (nothing past EOF).
    assert buf.append(3, b"D") is False


def test_stop_and_should_stop():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10)
    assert buf.should_stop() is False
    buf.stop()
    assert buf.should_stop() is True


# ---------------------------------------------------------------------------
# _get_readahead_buffer_mb + snapshot/live wiring
# ---------------------------------------------------------------------------


def _patch_setting(value):
    return patch.object(stream_proxy, "_get_addon_setting", return_value=value)


def test_readahead_setting_default_when_blank():
    with _patch_setting(""):
        assert stream_proxy._get_readahead_buffer_mb() == _DEFAULT_READAHEAD_BUFFER_MB
    with _patch_setting(None):
        assert stream_proxy._get_readahead_buffer_mb() == _DEFAULT_READAHEAD_BUFFER_MB


def test_readahead_setting_parses_int():
    with _patch_setting("128"):
        assert stream_proxy._get_readahead_buffer_mb() == 128


def test_readahead_setting_zero_disables():
    with _patch_setting("0"):
        assert stream_proxy._get_readahead_buffer_mb() == 0


def test_readahead_setting_clamps():
    with _patch_setting("-5"):
        assert stream_proxy._get_readahead_buffer_mb() == 0
    with _patch_setting(str(_READAHEAD_BUFFER_MB_MAX + 1000)):
        assert stream_proxy._get_readahead_buffer_mb() == _READAHEAD_BUFFER_MB_MAX


def test_readahead_setting_non_numeric_falls_back():
    with _patch_setting("garbage"):
        assert stream_proxy._get_readahead_buffer_mb() == _DEFAULT_READAHEAD_BUFFER_MB


def test_snapshot_wiring_includes_readahead():
    snap = {"readahead_buffer_mb": "64"}
    out = stream_proxy._passthrough_runtime_settings_from_snapshot(snap)
    assert out["readahead_buffer_mb"] == 64


def test_snapshot_wiring_default_when_absent():
    out = stream_proxy._passthrough_runtime_settings_from_snapshot({})
    assert out["readahead_buffer_mb"] == _DEFAULT_READAHEAD_BUFFER_MB


def test_live_runtime_settings_includes_readahead():
    with _patch_setting("32"):
        out = stream_proxy._read_passthrough_runtime_settings()
    assert out["readahead_buffer_mb"] == 32


# ---------------------------------------------------------------------------
# _serve_from_readahead
# ---------------------------------------------------------------------------


def test_serve_from_readahead_no_buffer_returns_zero():
    handler = _make_handler()
    assert handler._serve_from_readahead({}, 0, 99) == 0
    handler.wfile.write.assert_not_called()


def test_serve_from_readahead_hit_writes_and_frees():
    handler = _make_handler()
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDEFGHIJ")
    ctx = {_READAHEAD_BUFFER_KEY: buf}
    n = handler._serve_from_readahead(ctx, 0, 4)
    assert n == 5
    handler.wfile.write.assert_called_once_with(b"ABCDE")
    # free-behind advanced the base so the served bytes are dropped.
    assert buf.read_prefix(0, 4) == b""
    assert buf.read_prefix(5, 9) == b"FGHIJ"


def test_serve_from_readahead_miss_returns_zero():
    handler = _make_handler()
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDE")
    ctx = {_READAHEAD_BUFFER_KEY: buf}
    # request at non-base offset -> miss
    assert handler._serve_from_readahead(ctx, 100, 200) == 0
    handler.wfile.write.assert_not_called()


# ---------------------------------------------------------------------------
# _stream_upstream_range additive consult
# ---------------------------------------------------------------------------


def test_stream_upstream_range_window_hit_skips_urlopen():
    handler = _make_handler()
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"X" * 100)
    ctx = {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": 10_000,
        _READAHEAD_BUFFER_KEY: buf,
    }
    with patch.object(stream_proxy, "urlopen") as mock_urlopen:
        result, written = handler._stream_upstream_range(ctx, 0, 99)
    assert result == _UPSTREAM_RANGE_OK
    assert written == 100
    mock_urlopen.assert_not_called()
    handler.wfile.write.assert_called_once_with(b"X" * 100)


def test_stream_upstream_range_no_buffer_unchanged():
    """With no read-ahead buffer the path must reach the upstream urlopen."""
    handler = _make_handler()
    ctx = {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": 10_000,
    }
    # urlopen raising a benign OSError keeps us out of the heavy branches but
    # proves the buffer consult did NOT short-circuit the upstream attempt.
    with patch.object(
        stream_proxy, "urlopen", side_effect=OSError("boom")
    ) as mock_urlopen, patch.object(
        handler, "_pop_cached_fallback_range", return_value=b""
    ):
        handler._stream_upstream_range(ctx, 0, 99)
    mock_urlopen.assert_called_once()


def _mock_range_response(chunks, status=206, headers=None):
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(side_effect=list(chunks) + [b""])
    resp.status = status
    resp.getcode = MagicMock(return_value=status)
    header_map = {str(k).lower(): v for k, v in (headers or {}).items()}
    resp.headers.get = MagicMock(
        side_effect=lambda key, default=None: header_map.get(str(key).lower(), default)
    )
    resp.close = MagicMock()
    return resp


def test_stream_upstream_range_miss_updates_served_high_water():
    """Regression (id 3352749425): on a read-ahead MISS the bytes are served
    directly from upstream; after the write the buffer must be told the served
    position so next_fetch_offset advances past it (forward lead, not re-fetch
    from 0)."""
    handler = _make_handler()
    content_length = 10_000_000
    buf = ReadAheadBuffer(cap_bytes=256 * 1024 * 1024, content_length=content_length)
    # Startup: nothing buffered -> read_prefix MISS, served straight upstream.
    assert buf.next_fetch_offset() == 0
    ctx = {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": content_length,
        _READAHEAD_BUFFER_KEY: buf,
    }
    payload = b"Z" * 100
    resp = _mock_range_response(
        [payload],
        headers={"Content-Range": "bytes 0-99/{}".format(content_length)},
    )
    with patch.object(stream_proxy, "urlopen", return_value=resp), patch.object(
        handler, "_pop_cached_fallback_range", return_value=b""
    ), patch.object(handler, "_wait_for_initial_range_prefetch"):
        result, written = handler._stream_upstream_range(ctx, 0, 99)
    assert result == _UPSTREAM_RANGE_OK
    assert written == 100
    served_end = 0 + written
    assert buf.next_fetch_offset() >= served_end


# ---------------------------------------------------------------------------
# Prefetch run-loop
# ---------------------------------------------------------------------------


class _FakeMonitor:
    def __init__(self, abort_after=None):
        self._calls = 0
        self._abort_after = abort_after

    def waitForAbort(self, _timeout=0.0):
        self._calls += 1
        if self._abort_after is not None and self._calls >= self._abort_after:
            return True
        return False

    def abortRequested(self):
        return self._abort_after is not None and self._calls >= self._abort_after


def test_run_readahead_prefetch_fills_then_stops_at_eof():
    proxy = _make_proxy()
    content_length = 200_000
    buf = ReadAheadBuffer(cap_bytes=10 * 1024 * 1024, content_length=content_length)
    ctx = {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": content_length,
        _READAHEAD_BUFFER_KEY: buf,
    }

    def fake_fetch(_url, _auth, start, end, _clen):
        return b"Y" * (end - start + 1)

    monitor = _FakeMonitor()
    with patch("xbmc.Monitor", return_value=monitor), patch.object(
        _StreamHandler, "_fetch_primary_range_bytes", staticmethod(fake_fetch)
    ):
        proxy._run_readahead_prefetch(ctx)

    # Filled exactly up to content_length, never past.
    assert buf.next_fetch_offset() == content_length
    assert buf.read_prefix(0, 0) == b"Y"


def test_run_readahead_prefetch_repoints_to_url_after_fallback_cutover():
    """A live fallback cutover mutates ctx['remote_url']/['auth_header'];
    the daemon must pick up the NEW source on the next iteration instead of
    hammering the dead primary. (QA pass #2 finding: the URL was hoisted into
    a thread-local once at start, so post-cutover prefetch stalled.)"""
    proxy = _make_proxy()
    content_length = 200_000
    buf = ReadAheadBuffer(cap_bytes=10 * 1024 * 1024, content_length=content_length)
    ctx = {
        "remote_url": "http://webdav/primary.mkv",
        "auth_header": "Basic PRIMARY",
        "content_length": content_length,
        _READAHEAD_BUFFER_KEY: buf,
    }

    seen = []

    def fake_fetch(url, auth, start, end, _clen):
        seen.append((url, auth))
        # Simulate the live cutover after the first chunk: the serve path has
        # repointed the session to the fallback source.
        if len(seen) == 1:
            ctx["remote_url"] = "http://webdav/fallback.mkv"
            ctx["auth_header"] = "Basic FALLBACK"
        return b"Y" * (end - start + 1)

    monitor = _FakeMonitor()
    with patch("xbmc.Monitor", return_value=monitor), patch.object(
        _StreamHandler, "_fetch_primary_range_bytes", staticmethod(fake_fetch)
    ):
        proxy._run_readahead_prefetch(ctx)

    # First fetch hit the primary; every fetch after the cutover used the
    # fallback URL + its auth header (not the stale primary).
    assert seen[0] == ("http://webdav/primary.mkv", "Basic PRIMARY")
    assert all(s == ("http://webdav/fallback.mkv", "Basic FALLBACK") for s in seen[1:])
    assert len(seen) > 1  # it kept prefetching from the new source


def test_run_readahead_prefetch_bounded_by_cap():
    proxy = _make_proxy()
    content_length = 50 * 1024 * 1024
    cap = 256 * 1024  # small cap
    buf = ReadAheadBuffer(cap_bytes=cap, content_length=content_length)
    ctx = {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": content_length,
        _READAHEAD_BUFFER_KEY: buf,
    }

    def fake_fetch(_url, _auth, start, end, _clen):
        return b"Y" * (end - start + 1)

    # Abort quickly so the throttle loop does not spin forever once full.
    monitor = _FakeMonitor(abort_after=200)
    with patch("xbmc.Monitor", return_value=monitor), patch.object(
        _StreamHandler, "_fetch_primary_range_bytes", staticmethod(fake_fetch)
    ):
        proxy._run_readahead_prefetch(ctx)

    assert buf.is_full() is True
    assert len(buf.read_prefix(0, cap * 2)) == cap


def test_run_readahead_prefetch_error_does_not_raise_or_touch_recovery():
    proxy = _make_proxy()
    content_length = 200_000
    buf = ReadAheadBuffer(cap_bytes=10 * 1024 * 1024, content_length=content_length)
    ctx = {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": content_length,
        _READAHEAD_BUFFER_KEY: buf,
    }

    calls = {"n": 0}

    def fake_fetch(_url, _auth, start, end, _clen):
        calls["n"] += 1
        if calls["n"] <= 3:
            return None  # best-effort upstream error / awaiting download
        return b"Y" * (end - start + 1)

    monitor = _FakeMonitor()
    with patch("xbmc.Monitor", return_value=monitor), patch.object(
        _StreamHandler, "_fetch_primary_range_bytes", staticmethod(fake_fetch)
    ):
        # Must not raise.
        proxy._run_readahead_prefetch(ctx)

    # It backed off and ultimately filled to EOF.
    assert buf.next_fetch_offset() == content_length


def test_run_readahead_prefetch_swallows_exceptions():
    proxy = _make_proxy()
    content_length = 200_000
    buf = ReadAheadBuffer(cap_bytes=10 * 1024 * 1024, content_length=content_length)
    ctx = {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": content_length,
        _READAHEAD_BUFFER_KEY: buf,
    }

    def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    # Abort after a couple loops so the swallowed exception path returns.
    monitor = _FakeMonitor(abort_after=3)
    with patch("xbmc.Monitor", return_value=monitor), patch.object(
        _StreamHandler, "_fetch_primary_range_bytes", staticmethod(boom)
    ):
        proxy._run_readahead_prefetch(ctx)  # must not raise


def test_run_readahead_prefetch_stops_on_stop_event():
    proxy = _make_proxy()
    content_length = 200_000
    buf = ReadAheadBuffer(cap_bytes=10 * 1024 * 1024, content_length=content_length)
    buf.stop()  # pre-signalled
    ctx = {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": content_length,
        _READAHEAD_BUFFER_KEY: buf,
    }

    fetch = MagicMock(return_value=b"Y")
    monitor = _FakeMonitor()
    with patch("xbmc.Monitor", return_value=monitor), patch.object(
        _StreamHandler, "_fetch_primary_range_bytes", staticmethod(fetch)
    ):
        proxy._run_readahead_prefetch(ctx)
    fetch.assert_not_called()


def test_run_readahead_prefetch_aborts_on_waitforabort():
    proxy = _make_proxy()
    content_length = 50 * 1024 * 1024
    cap = 256 * 1024
    buf = ReadAheadBuffer(cap_bytes=cap, content_length=content_length)
    ctx = {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": content_length,
        _READAHEAD_BUFFER_KEY: buf,
    }

    def fake_fetch(_url, _auth, start, end, _clen):
        return b"Y" * (end - start + 1)

    monitor = _FakeMonitor(abort_after=1)  # abort immediately
    with patch("xbmc.Monitor", return_value=monitor), patch.object(
        _StreamHandler, "_fetch_primary_range_bytes", staticmethod(fake_fetch)
    ):
        proxy._run_readahead_prefetch(ctx)
    # Aborted on the first loop guard -> nothing buffered.
    assert buf.next_fetch_offset() == 0


# ---------------------------------------------------------------------------
# Lifecycle: _start_readahead_prefetch + _cleanup_session teardown
# ---------------------------------------------------------------------------


def _prefetchable_ctx(content_length=1_000_000, mb=256):
    return {
        "remote_url": "http://webdav/x.mkv",
        "auth_header": None,
        "content_length": content_length,
        stream_proxy._PASSTHROUGH_RUNTIME_SETTINGS_KEY: {
            "readahead_buffer_mb": mb,
        },
    }


def test_start_readahead_prefetch_disabled_when_zero():
    proxy = _make_proxy()
    ctx = _prefetchable_ctx(mb=0)
    proxy._start_readahead_prefetch(ctx)
    assert _READAHEAD_BUFFER_KEY not in ctx
    assert _READAHEAD_THREAD_KEY not in ctx


def test_start_readahead_prefetch_gated_off_for_remux():
    proxy = _make_proxy()
    ctx = _prefetchable_ctx(mb=256)
    ctx["remux"] = True
    proxy._start_readahead_prefetch(ctx)
    assert _READAHEAD_BUFFER_KEY not in ctx


def test_start_readahead_prefetch_spawns_daemon():
    proxy = _make_proxy()
    ctx = _prefetchable_ctx(mb=256)
    # Prevent the real loop from running upstream fetches.
    with patch.object(
        stream_proxy.StreamProxy, "_run_readahead_prefetch", lambda self, c: None
    ):
        proxy._start_readahead_prefetch(ctx)
        thread = ctx.get(_READAHEAD_THREAD_KEY)
        assert isinstance(ctx.get(_READAHEAD_BUFFER_KEY), ReadAheadBuffer)
        assert isinstance(thread, threading.Thread)
        assert thread.daemon is True
        thread.join(2.0)


def test_start_readahead_prefetch_runtimeerror_pops_keys():
    proxy = _make_proxy()
    ctx = _prefetchable_ctx(mb=256)
    with patch.object(
        threading.Thread, "start", side_effect=RuntimeError("no threads")
    ):
        proxy._start_readahead_prefetch(ctx)
    assert _READAHEAD_BUFFER_KEY not in ctx
    assert _READAHEAD_THREAD_KEY not in ctx


def test_cleanup_session_stops_readahead_buffer():
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10)
    ctx = {_READAHEAD_BUFFER_KEY: buf}
    stream_proxy.StreamProxy._cleanup_session(ctx, wait_for_process=False)
    assert buf.should_stop() is True


def test_cleanup_session_no_buffer_is_noop():
    # Should not raise when there is no read-ahead buffer attached.
    stream_proxy.StreamProxy._cleanup_session({}, wait_for_process=False)


# ---------------------------------------------------------------------------
# Seek correctness end-to-end (handler level)
# ---------------------------------------------------------------------------


def test_post_seek_window_returns_blank_until_repoint():
    handler = _make_handler()
    buf = ReadAheadBuffer(cap_bytes=1024, content_length=10_000)
    buf.append(0, b"ABCDEFGHIJ")
    ctx = {_READAHEAD_BUFFER_KEY: buf}
    # A real seek outside the window discards the stale window.
    buf.note_seek(5_000)
    # The first post-seek read must MISS (so it falls through to upstream),
    # never feeding stale wrong-offset bytes.
    assert handler._serve_from_readahead(ctx, 5_000, 5_099) == 0
    handler.wfile.write.assert_not_called()
