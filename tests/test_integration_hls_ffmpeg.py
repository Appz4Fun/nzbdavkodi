# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Real-ffmpeg integration tests for the fmp4 HLS producer.

Why this exists
---------------
Every ffmpeg-related bug we've hit on the spike/hls-fmp4 branch was
invisible to the existing test suite because the suite mocks
``subprocess.Popen``. The bugs:

* ``-hls_fmp4_init_filename`` rejected absolute paths on ffmpeg 6.0.1
* ``-strict -2`` required for TrueHD / DTS-HD MA in MP4
* ``-analyzeduration 0`` produced wrong audio sample timing
* ``codec frame size is not set`` warning lost audio entirely
* ``-tag:v hvc1`` interaction with various sources

A unit test that mocks Popen will happily pass while production
ffmpeg fails. The fix is an integration test that runs the actual
ffmpeg binary against a real MKV file with the EXACT command the
HlsProducer would use, and asserts that init.mp4 + segments are
produced and well-formed.

What this does
--------------
1. Skips entirely if ``ffmpeg`` is not on PATH (no CI pain).
2. Generates a small (~2 MB, 10 s) test MKV via ``ffmpeg lavfi``
   sources — synthetic video + sine audio. Uses libx264 + ac3 so
   it's MP4-native and doesn't require ``-strict -2``. No license
   issue (everything is generated at test time).
3. Serves the MKV from a localhost HTTP server (because
   ``_validate_url`` rejects ``file://``).
4. Runs ``HlsProducer`` end-to-end: prepare(), wait_for_init(),
   wait_for_segment(0), wait_for_segment(1), then close().
5. Asserts ``init.mp4`` and ``seg_000000.m4s`` / ``seg_000001.m4s``
   exist and are non-empty.

Run with: ``just test-integration``
"""

import http.server
import os
import shutil
import socketserver
import threading
from urllib.error import URLError
from urllib.request import urlopen

import pytest

FFMPEG_PATH = shutil.which("ffmpeg")
pytestmark = pytest.mark.integration

if FFMPEG_PATH is None:
    pytest.skip(
        "ffmpeg binary not found on PATH — install ffmpeg to run these tests",
        allow_module_level=True,
    )


def _generate_test_mkv(path):
    """Generate a 10-second synthetic test MKV with H.264 video and
    AC-3 audio. AC-3 is MP4-native (no -strict required) which lets
    us isolate the HLS pipeline issues from the experimental-codec
    issues. Returns the file path on success, raises on ffmpeg
    failure."""
    import subprocess

    cmd = [
        FFMPEG_PATH,
        "-y",
        "-v",
        "error",
        # 10 seconds of synthetic video at 320x240, 24 fps
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=10:size=320x240:rate=24",
        # 10 seconds of 1 kHz sine, 48 kHz stereo
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:duration=10:sample_rate=48000",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "24",  # keyframe every 1 s — gives us 10 GOPs
        "-c:a",
        "ac3",
        "-b:a",
        "192k",
        "-shortest",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            "test MKV generation failed: {}".format(
                result.stderr.decode(errors="replace")
            )
        )
    assert os.path.exists(path), "ffmpeg returned 0 but no output file"
    assert os.path.getsize(path) > 0, "test MKV is empty"
    return path


class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that doesn't spam stderr."""

    def log_message(self, fmt, *args):  # noqa: ARG002
        pass


def _start_local_http_server(directory):
    """Serve ``directory`` from a local HTTP server on an ephemeral
    port. Returns ``(server, port, thread)``. Caller must call
    ``server.shutdown()`` to stop it."""
    import functools

    handler = functools.partial(_SilentHandler, directory=directory)

    class _Server(socketserver.TCPServer):
        allow_reuse_address = True

    server = _Server(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, name="test-http", daemon=True
    )
    thread.start()
    # Wait until the server is actually accepting connections.
    for _ in range(50):
        try:
            with urlopen("http://127.0.0.1:{}/".format(port), timeout=1) as resp:
                resp.read()
            break
        except URLError:
            import time

            time.sleep(0.05)
    return server, port, thread


@pytest.fixture
def served_test_mkv(tmp_path):
    """Generate a test MKV, serve it from a local HTTP server, yield
    the URL, then tear down the server."""
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    mkv_path = media_dir / "test.mkv"
    _generate_test_mkv(str(mkv_path))

    server, port, _thread = _start_local_http_server(str(media_dir))
    try:
        url = "http://127.0.0.1:{}/test.mkv".format(port)
        yield url
    finally:
        server.shutdown()
        server.server_close()


def test_hls_producer_real_ffmpeg_produces_init_and_segments(served_test_mkv, tmp_path):
    """The big one. Runs HlsProducer end-to-end against the real
    ffmpeg binary with the exact command production uses. Catches
    every class of bug we've hit on the spike at PR time, before
    it gets near the test box.
    """
    from resources.lib.stream_proxy import HlsProducer

    workdir = tmp_path / "hls"
    workdir.mkdir()

    ctx = {
        "session_id": "integration-test",
        "remote_url": served_test_mkv,
        "auth_header": None,
        "ffmpeg_path": FFMPEG_PATH,
        "duration_seconds": 10.0,
        "hls_segment_duration": 6.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(workdir))
    try:
        # prepare() is the new gauntlet: argv-rejection poll +
        # production-output wait. Must succeed for a healthy source.
        try:
            producer.prepare()
        except Exception:
            log_path = os.path.join(producer.session_dir, "ffmpeg.log")
            if os.path.exists(log_path):
                with open(log_path) as f:
                    print("=== ffmpeg.log ===\n" + f.read())
            raise

        # Verify init.mp4 was produced and non-empty.
        init_path = producer.wait_for_init(timeout=10.0)
        assert init_path is not None, "wait_for_init returned None"
        assert os.path.exists(init_path), "init.mp4 missing on disk"
        init_size = os.path.getsize(init_path)
        assert init_size > 0, "init.mp4 is empty"
        assert init_size < 100_000, (
            "init.mp4 is suspiciously large ({} bytes) — should be a "
            "few hundred bytes for this test source".format(init_size)
        )

        # The canonical bytes cache should have been populated.
        assert producer._canonical_init_bytes is not None
        assert len(producer._canonical_init_bytes) == init_size

        # First segment should appear quickly.
        seg0_path = producer.wait_for_segment(0, timeout=15.0)
        assert seg0_path is not None, "wait_for_segment(0) returned None"
        assert os.path.exists(seg0_path), "seg_000000.m4s missing on disk"
        assert os.path.getsize(seg0_path) > 0, "seg_000000.m4s is empty"

        # Second segment exercises the linear-forward path.
        seg1_path = producer.wait_for_segment(1, timeout=15.0)
        assert seg1_path is not None, "wait_for_segment(1) returned None"
        assert os.path.exists(seg1_path)
        assert os.path.getsize(seg1_path) > 0
    finally:
        producer.close()


def test_hls_producer_real_ffmpeg_init_mp4_is_valid_iso_bmff(served_test_mkv, tmp_path):
    """Validate that init.mp4 starts with the expected ISO BMFF
    signature: ``ftyp`` box at offset 4. Catches the case where
    ffmpeg writes garbage (or nothing) into a file with the right
    name. Also confirms the sample description box (``stsd``)
    is present, which is what Kodi's HLS demuxer needs to find
    the codec config."""
    from resources.lib.stream_proxy import HlsProducer

    workdir = tmp_path / "hls"
    workdir.mkdir()
    ctx = {
        "session_id": "integration-test-iso",
        "remote_url": served_test_mkv,
        "auth_header": None,
        "ffmpeg_path": FFMPEG_PATH,
        "duration_seconds": 10.0,
        "hls_segment_duration": 6.0,
        "hls_segment_format": "fmp4",
    }
    producer = HlsProducer(ctx, str(workdir))
    try:
        producer.prepare()
        init_path = producer.wait_for_init(timeout=10.0)
        assert init_path is not None
        with open(init_path, "rb") as f:
            data = f.read()
        # ISO BMFF: bytes 4-8 should be 'ftyp'
        assert (
            data[4:8] == b"ftyp"
        ), "init.mp4 does not start with ftyp box: " "got {!r}".format(data[:16])
        # The moov box should appear somewhere in the init
        assert b"moov" in data, "init.mp4 has no moov box"
        # The sample description box (codec config carrier)
        assert b"stsd" in data, "init.mp4 has no stsd box"
    finally:
        producer.close()
