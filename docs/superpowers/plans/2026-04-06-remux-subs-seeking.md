# Remux Proxy Subtitles + Seeking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add subtitle conversion and byte-range seeking to the ffmpeg remux proxy so MP4 playback includes all audio/subtitle tracks and supports fast-forward/rewind with a progress bar.

**Architecture:** Two independent features layered onto the existing `_serve_remux()` path in `stream_proxy.py`. Subtitles add ffmpeg flags controlled by a setting. Seeking adds a duration probe at prepare time, byte-to-time mapping in the GET handler, and process management to kill/respawn ffmpeg on seek. Both features degrade gracefully — subs are togglable, seeking falls back to linear pipe when duration is unavailable.

**Tech Stack:** Python 3.8+, ffmpeg CLI, Kodi addon API (`xbmcaddon`), `subprocess.Popen`, `http.server`

---

### Task 1: Duration Parsing Helper

**Files:**
- Modify: `plugin.video.nzbdav/resources/lib/stream_proxy.py`
- Test: `tests/test_stream_proxy.py`

- [ ] **Step 1: Write failing tests for `_probe_duration()`**

Add to `tests/test_stream_proxy.py`:

```python
# ---------------------------------------------------------------------------
# _probe_duration — parse duration from ffmpeg stderr
# ---------------------------------------------------------------------------


def test_probe_duration_parses_hms():
    from resources.lib.stream_proxy import _parse_ffmpeg_duration

    stderr = "  Duration: 01:30:45.67, start: 0.000000, bitrate: 30000 kb/s\n"
    assert _parse_ffmpeg_duration(stderr) == 5445.67


def test_probe_duration_parses_minutes_only():
    from resources.lib.stream_proxy import _parse_ffmpeg_duration

    stderr = "  Duration: 00:02:30.00, start: 0.000000\n"
    assert _parse_ffmpeg_duration(stderr) == 150.0


def test_probe_duration_returns_none_on_missing():
    from resources.lib.stream_proxy import _parse_ffmpeg_duration

    assert _parse_ffmpeg_duration("no duration here") is None


def test_probe_duration_returns_none_on_n_a():
    from resources.lib.stream_proxy import _parse_ffmpeg_duration

    stderr = "  Duration: N/A, start: 0.000000\n"
    assert _parse_ffmpeg_duration(stderr) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test`
Expected: 4 failures — `_parse_ffmpeg_duration` not found

- [ ] **Step 3: Implement `_parse_ffmpeg_duration()`**

Add to `stream_proxy.py` after the `_find_ffmpeg()` function (around line 43):

```python
import re

def _parse_ffmpeg_duration(stderr_text):
    """Parse 'Duration: HH:MM:SS.xx' from ffmpeg stderr output.

    Returns duration in seconds as a float, or None if not found.
    """
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", stderr_text)
    if not match:
        return None
    hours, minutes, seconds, frac = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(frac) / (10 ** len(frac))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add plugin.video.nzbdav/resources/lib/stream_proxy.py tests/test_stream_proxy.py
git commit -m "feat: add _parse_ffmpeg_duration helper for seeking"
```

---

### Task 2: Duration Probe in prepare_stream()

**Files:**
- Modify: `plugin.video.nzbdav/resources/lib/stream_proxy.py`
- Test: `tests/test_stream_proxy.py`

- [ ] **Step 1: Write failing test for duration probe**

Add to `tests/test_stream_proxy.py`:

```python
def test_prepare_stream_probes_duration_for_mp4():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (
        b"",
        b"  Duration: 02:00:00.00, start: 0.000000, bitrate: 30000 kb/s\n",
    )
    mock_proc.returncode = 1  # ffmpeg -f null returns non-zero

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
    ), patch.object(
        sp, "_get_content_length", return_value=5000000000
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ):
        sp.prepare_stream("http://host/film.mp4", auth_header="Basic abc")

    ctx = sp._server.stream_context
    assert ctx["remux"] is True
    assert ctx["duration_seconds"] == 7200.0
    assert ctx["total_bytes"] == 5000000000
    assert ctx["seekable"] is True


def test_prepare_stream_falls_back_to_non_seekable_on_probe_failure():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (b"", b"some error\n")
    mock_proc.returncode = 1

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
    ), patch.object(
        sp, "_get_content_length", return_value=5000000000
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ):
        sp.prepare_stream("http://host/film.mp4")

    ctx = sp._server.stream_context
    assert ctx["remux"] is True
    assert ctx["seekable"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test`
Expected: Failures — ctx missing `duration_seconds`, `total_bytes`, `seekable` keys

- [ ] **Step 3: Update `prepare_stream()` to probe duration**

In `stream_proxy.py`, replace the `if use_remux:` block in `prepare_stream()` (lines 302-313) with:

```python
        if use_remux:
            content_length = self._get_content_length(remote_url, auth_header)
            duration = self._probe_duration(ffmpeg_path, remote_url, auth_header)
            seekable = duration is not None and content_length > 0
            ctx = {
                "remote_url": remote_url,
                "auth_header": auth_header,
                "content_type": "video/x-matroska",
                "remux": True,
                "ffmpeg_path": ffmpeg_path,
                "total_bytes": content_length,
                "duration_seconds": duration,
                "seekable": seekable,
            }
            xbmc.log(
                "NZB-DAV: Will remux MP4->MKV via {} (seekable={}, duration={})".format(
                    ffmpeg_path, seekable, duration
                ),
                xbmc.LOGINFO,
            )
```

Add the `_probe_duration()` method to `StreamProxy`:

```python
    def _probe_duration(self, ffmpeg_path, url, auth_header):
        """Probe file duration using ffmpeg. Returns seconds or None."""
        input_url = url
        if auth_header and auth_header.startswith("Basic "):
            import base64
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            input_url = url.replace("://", "://{}@".format(decoded), 1)

        try:
            proc = subprocess.Popen(
                [ffmpeg_path, "-v", "warning", "-i", input_url, "-f", "null", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, stderr = proc.communicate(timeout=120)
            return _parse_ffmpeg_duration(stderr.decode(errors="replace"))
        except Exception as e:
            xbmc.log(
                "NZB-DAV: Duration probe failed: {}".format(e), xbmc.LOGWARNING
            )
            return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add plugin.video.nzbdav/resources/lib/stream_proxy.py tests/test_stream_proxy.py
git commit -m "feat: probe MP4 duration at prepare time for seeking"
```

---

### Task 3: Seekable HEAD/GET Response Headers

**Files:**
- Modify: `plugin.video.nzbdav/resources/lib/stream_proxy.py`
- Test: `tests/test_stream_proxy.py`

- [ ] **Step 1: Write failing test for seek detection**

Add to `tests/test_stream_proxy.py`:

```python
# ---------------------------------------------------------------------------
# Seek detection — is_seek_request
# ---------------------------------------------------------------------------

_SEEK_THRESHOLD = 10 * 1024 * 1024  # 10MB


def test_seek_detection_continuation():
    """Request near current position is NOT a seek."""
    from resources.lib.stream_proxy import _is_seek_request

    assert _is_seek_request(5000000, 5500000) is False  # 500KB ahead


def test_seek_detection_forward_jump():
    """Request far ahead IS a seek."""
    from resources.lib.stream_proxy import _is_seek_request

    assert _is_seek_request(5000000, 50000000) is True  # 45MB ahead


def test_seek_detection_backward():
    """Any backward request IS a seek."""
    from resources.lib.stream_proxy import _is_seek_request

    assert _is_seek_request(50000000, 10000000) is True


def test_seek_detection_from_zero():
    """Request at 0 when current is 0 is NOT a seek."""
    from resources.lib.stream_proxy import _is_seek_request

    assert _is_seek_request(0, 0) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `just test`
Expected: Failures — `_is_seek_request` not found

- [ ] **Step 3: Implement `_is_seek_request()` and update HEAD handler**

Add to `stream_proxy.py` after `_parse_ffmpeg_duration()`:

```python
_SEEK_THRESHOLD = 10 * 1024 * 1024  # 10MB


def _is_seek_request(current_byte_pos, requested_byte_pos):
    """Determine if a range request is a genuine seek or a continuation.

    Returns True if the request is far from the current position (>10MB
    gap or backward), meaning ffmpeg should be restarted with -ss.
    """
    delta = requested_byte_pos - current_byte_pos
    if delta < 0:
        return True  # backward seek
    return delta > _SEEK_THRESHOLD
```

Update the `do_HEAD()` remux branch (lines 90-95) to advertise seekability:

```python
        if ctx.get("remux"):
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            if ctx.get("seekable"):
                self.send_header("Content-Length", str(ctx["total_bytes"]))
                self.send_header("Accept-Ranges", "bytes")
            else:
                self.send_header("Accept-Ranges", "none")
            self.send_header("Connection", "close")
            self.end_headers()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `just test`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add plugin.video.nzbdav/resources/lib/stream_proxy.py tests/test_stream_proxy.py
git commit -m "feat: add seek detection and seekable HEAD response"
```

---

### Task 4: Process Management + Seek-Aware GET Handler

**Files:**
- Modify: `plugin.video.nzbdav/resources/lib/stream_proxy.py`

- [ ] **Step 1: Add process tracking to the server**

In `StreamProxy.start()`, after setting `self._server.stream_context = None`, add:

```python
        self._server.active_ffmpeg = None
        self._server.current_byte_pos = 0
        self._server.ffmpeg_lock = threading.Lock()
```

- [ ] **Step 2: Add `_build_ffmpeg_cmd()` helper to `_StreamHandler`**

Add to `_StreamHandler` class, extracting the command-building from `_serve_remux()`:

```python
    def _build_ffmpeg_cmd(self, ctx, seek_seconds=None):
        """Build the ffmpeg remux command list."""
        ffmpeg = ctx["ffmpeg_path"]
        input_url = ctx["remote_url"]
        if ctx.get("auth_header"):
            auth = ctx["auth_header"]
            if auth.startswith("Basic "):
                import base64
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                input_url = ctx["remote_url"].replace(
                    "://", "://{}@".format(decoded), 1
                )

        cmd = [ffmpeg]
        if seek_seconds is not None and seek_seconds > 0:
            cmd.extend(["-ss", "{:.3f}".format(seek_seconds)])
        cmd.extend([
            "-v", "warning",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-i", input_url,
            "-map", "0:v:0",
            "-map", "0:a",
        ])

        # Subtitle conversion (toggleable via setting)
        try:
            import xbmcaddon
            convert_subs = xbmcaddon.Addon().getSetting("proxy_convert_subs")
            if convert_subs != "false":
                cmd.extend(["-map", "0:s?", "-c:s", "srt"])
        except Exception:
            pass  # outside Kodi context (tests), skip subtitle setting

        cmd.extend([
            "-c", "copy",
            "-f", "matroska",
            "-fflags", "+genpts+flush_packets",
            "pipe:1",
        ])
        return cmd
```

- [ ] **Step 3: Rewrite `_serve_remux()` with seeking and process management**

Replace the entire `_serve_remux()` method:

```python
    def _serve_remux(self, ctx):
        """Remux MP4 to MKV on the fly, with optional seeking."""
        total_bytes = ctx.get("total_bytes", 0)
        duration = ctx.get("duration_seconds")
        seekable = ctx.get("seekable", False)

        # Parse range request
        range_header = self.headers.get("Range")
        requested_start = 0
        if range_header:
            parsed = self._parse_range(range_header, total_bytes or 1)
            if parsed[0] is not None:
                requested_start = parsed[0]

        # Determine if this is a seek
        seek_seconds = None
        with self.server.ffmpeg_lock:
            current_pos = self.server.current_byte_pos
            if seekable and requested_start > 0 and _is_seek_request(
                current_pos, requested_start
            ):
                seek_seconds = (requested_start / total_bytes) * duration
                xbmc.log(
                    "NZB-DAV: Seek to byte {} -> {:.1f}s".format(
                        requested_start, seek_seconds
                    ),
                    xbmc.LOGINFO,
                )
                # Kill existing ffmpeg
                if self.server.active_ffmpeg:
                    try:
                        self.server.active_ffmpeg.kill()
                    except Exception:
                        pass
                    self.server.active_ffmpeg = None
            elif (
                seekable
                and requested_start > 0
                and not _is_seek_request(current_pos, requested_start)
            ):
                # Continuation — but we can't "resume" a pipe, so just
                # keep streaming from the existing process. Kodi will
                # get data from where we left off.
                pass

        cmd = self._build_ffmpeg_cmd(ctx, seek_seconds=seek_seconds)
        xbmc.log(
            "NZB-DAV: Remuxing MP4->MKV (seek={})".format(seek_seconds),
            xbmc.LOGINFO,
        )

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except Exception as e:
            xbmc.log(
                "NZB-DAV: Failed to start ffmpeg: {}".format(e), xbmc.LOGERROR
            )
            self.send_error(500)
            return

        with self.server.ffmpeg_lock:
            self.server.active_ffmpeg = proc
            self.server.current_byte_pos = requested_start

        # Send response headers
        if seekable and total_bytes > 0:
            self.send_response(206)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(total_bytes - requested_start))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header(
                "Content-Range",
                "bytes {}-{}/{}".format(
                    requested_start, total_bytes - 1, total_bytes
                ),
            )
        else:
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Accept-Ranges", "none")
        self.send_header("Connection", "close")
        self.end_headers()

        # Stream ffmpeg output
        total = 0
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                total += len(chunk)
                with self.server.ffmpeg_lock:
                    self.server.current_byte_pos = requested_start + total
        except (BrokenPipeError, ConnectionResetError):
            xbmc.log(
                "NZB-DAV: Remux client disconnected after {} MB".format(
                    total // 1048576
                ),
                xbmc.LOGDEBUG,
            )
        finally:
            proc.kill()
            stderr = proc.stderr.read().decode(errors="replace")
            if stderr.strip():
                xbmc.log(
                    "NZB-DAV: ffmpeg: {}".format(stderr[:300]), xbmc.LOGDEBUG
                )
            xbmc.log(
                "NZB-DAV: Remux done: {} MB sent".format(total // 1048576),
                xbmc.LOGINFO,
            )
```

- [ ] **Step 4: Run tests to verify nothing broke**

Run: `just test`
Expected: All existing tests pass

- [ ] **Step 5: Commit**

```bash
git add plugin.video.nzbdav/resources/lib/stream_proxy.py
git commit -m "feat: add seeking and process management to remux proxy"
```

---

### Task 5: Subtitle Setting in GUI

**Files:**
- Modify: `plugin.video.nzbdav/resources/settings.xml`
- Modify: `plugin.video.nzbdav/resources/language/resource.language.en_gb/strings.po`

- [ ] **Step 1: Add the setting to settings.xml**

In `plugin.video.nzbdav/resources/settings.xml`, inside the last `<category>` (the "Advanced" category starting at line 79), add before the closing `</category>` tag (before line 89):

```xml
		<setting label="30118" type="lsep" />
		<setting id="proxy_convert_subs" label="30119" type="bool" default="true" />
```

- [ ] **Step 2: Add label strings**

In `plugin.video.nzbdav/resources/language/resource.language.en_gb/strings.po`, add after the last entry:

```
msgctxt "#30118"
msgid "Proxy"
msgstr "Proxy"

msgctxt "#30119"
msgid "Convert MP4 subtitles to SRT"
msgstr "Convert MP4 subtitles to SRT"
```

- [ ] **Step 3: Run tests and lint**

Run: `just test && just lint`
Expected: All pass, no lint errors

- [ ] **Step 4: Commit**

```bash
git add plugin.video.nzbdav/resources/settings.xml plugin.video.nzbdav/resources/language/resource.language.en_gb/strings.po
git commit -m "feat: add proxy_convert_subs setting for subtitle conversion"
```

---

### Task 6: Integration Test + Cleanup

**Files:**
- Modify: `tests/test_stream_proxy.py`

- [ ] **Step 1: Add integration-style tests for the new features**

Add to `tests/test_stream_proxy.py`:

```python
# ---------------------------------------------------------------------------
# _build_ffmpeg_cmd — subtitle flag toggling
# ---------------------------------------------------------------------------


def test_build_ffmpeg_cmd_includes_subs_by_default():
    """Default: subtitle mapping flags are present."""
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
    }
    cmd = handler._build_ffmpeg_cmd(ctx)
    assert "-map" in cmd
    # Check subs mapping is present (0:s? appears after 0:a)
    assert "0:s?" in cmd
    assert "srt" in cmd


def test_build_ffmpeg_cmd_excludes_subs_when_setting_off():
    """When proxy_convert_subs is false, no subtitle flags."""
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
    }
    import sys

    mock_addon = MagicMock()
    mock_addon.getSetting.return_value = "false"
    sys.modules["xbmcaddon"].Addon.return_value = mock_addon

    cmd = handler._build_ffmpeg_cmd(ctx)
    assert "0:s?" not in cmd
    assert "srt" not in cmd


def test_build_ffmpeg_cmd_includes_seek():
    """When seek_seconds is set, -ss appears before -i."""
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
    }
    cmd = handler._build_ffmpeg_cmd(ctx, seek_seconds=3600.5)
    ss_idx = cmd.index("-ss")
    i_idx = cmd.index("-i")
    assert ss_idx < i_idx
    assert cmd[ss_idx + 1] == "3600.500"


def test_build_ffmpeg_cmd_no_seek_when_none():
    """When seek_seconds is None, -ss is not in the command."""
    handler = _make_handler()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": None,
    }
    cmd = handler._build_ffmpeg_cmd(ctx, seek_seconds=None)
    assert "-ss" not in cmd


def test_build_ffmpeg_cmd_embeds_basic_auth():
    """Basic auth header is embedded in the URL for ffmpeg."""
    import base64

    handler = _make_handler()
    auth = "Basic " + base64.b64encode(b"user:pass").decode()
    ctx = {
        "ffmpeg_path": "/usr/bin/ffmpeg",
        "remote_url": "http://host/film.mp4",
        "auth_header": auth,
    }
    cmd = handler._build_ffmpeg_cmd(ctx)
    i_idx = cmd.index("-i")
    assert "user:pass@host" in cmd[i_idx + 1]
```

- [ ] **Step 2: Update existing `prepare_stream` test for new context keys**

Replace `test_prepare_stream_remuxes_mp4_when_ffmpeg_available` with:

```python
def test_prepare_stream_remuxes_mp4_when_ffmpeg_available():
    from resources.lib.stream_proxy import StreamProxy

    sp = StreamProxy.__new__(StreamProxy)
    sp._server = MagicMock()
    sp._context_lock = __import__("threading").Lock()
    sp.port = 9999

    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (
        b"",
        b"  Duration: 01:00:00.00, start: 0.000000\n",
    )

    with patch(
        "resources.lib.stream_proxy._find_ffmpeg", return_value="/usr/bin/ffmpeg"
    ), patch.object(
        sp, "_get_content_length", return_value=5000000000
    ), patch(
        "resources.lib.stream_proxy.subprocess.Popen", return_value=mock_proc
    ):
        url = sp.prepare_stream("http://host/film.mp4", auth_header="Basic abc")

    assert url == "http://127.0.0.1:9999/stream"
    ctx = sp._server.stream_context
    assert ctx["remux"] is True
    assert ctx["seekable"] is True
    assert ctx["duration_seconds"] == 3600.0
```

- [ ] **Step 3: Run full test suite and lint**

Run: `just test && just lint`
Expected: All pass, clean lint

- [ ] **Step 4: Commit**

```bash
git add tests/test_stream_proxy.py
git commit -m "test: add tests for subtitle toggling, seeking, and auth embedding"
```

---

### Task 7: Deploy + Manual Test

**Files:** None (deployment only)

- [ ] **Step 1: Deploy to CoreELEC**

```bash
scp plugin.video.nzbdav/resources/lib/stream_proxy.py root@coreelec.local:/storage/.kodi/addons/plugin.video.nzbdav/resources/lib/stream_proxy.py
scp plugin.video.nzbdav/resources/settings.xml root@coreelec.local:/storage/.kodi/addons/plugin.video.nzbdav/resources/settings.xml
scp plugin.video.nzbdav/resources/language/resource.language.en_gb/strings.po root@coreelec.local:/storage/.kodi/addons/plugin.video.nzbdav/resources/language/resource.language.en_gb/strings.po
ssh root@coreelec.local 'systemctl restart kodi'
```

- [ ] **Step 2: Test MP4 playback with seeking**

Play the Avatar MP4 via TMDBHelper. Verify:
- Playback starts (remux working)
- Subtitles appear in Kodi's subtitle selector
- Fast-forward works (skip ahead 10 minutes)
- Rewind works (skip back)
- Progress bar shows correct position

- [ ] **Step 3: Test MKV playback still works**

Play an MKV file. Verify it plays directly without the proxy remuxing.

- [ ] **Step 4: Check logs for seek events**

```bash
ssh root@coreelec.local 'grep "NZB-DAV.*Seek\|NZB-DAV.*Remux\|NZB-DAV.*duration" /storage/.kodi/temp/kodi.log | tail -20'
```

- [ ] **Step 5: Final commit with any fixes**

```bash
git add -A
git commit -m "fix: adjustments from manual testing"
```
