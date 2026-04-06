# Remux Proxy: Subtitle Support + Seeking

**Date:** 2026-04-06
**Status:** Approved
**Scope:** `stream_proxy.py`, `resolver.py`, `resources/settings.xml`

## Background

The addon's stream proxy remuxes MP4 files to MKV on the fly using
`ffmpeg -c copy`. This bypasses a Kodi 32-bit CFileCache bug that
prevents parsing large MP4 moov atoms over HTTP. The current
implementation has two limitations:

1. **No subtitles.** The ffmpeg command maps only video and audio
   (`-map 0:v:0 -map 0:a`). MP4 subtitle tracks use the `mov_text`
   codec, which is incompatible with the MKV container, so they were
   dropped to avoid muxing errors.

2. **No seeking.** The remuxed stream is a linear pipe
   (`Accept-Ranges: none`). Kodi cannot fast-forward, rewind, or
   show a progress bar.

## Feature 1: Subtitle Conversion

### Change

Add `-map 0:s?` and `-c:s srt` to the ffmpeg command. This maps all
subtitle streams (if any exist — the `?` prevents errors when none are
present) and converts them from `mov_text` to SRT, which MKV supports.
Video and audio remain `-c copy` (no re-encoding). The CPU cost of
text-to-text subtitle conversion is negligible.

### Toggleable Setting

Not every installation has ffmpeg, and some users may not want subtitle
conversion overhead. Add a boolean setting:

- **ID:** `proxy_convert_subs`
- **Label:** "Convert MP4 subtitles"
- **Default:** `true`
- **Behavior when off:** Drop `-map 0:s?` and `-c:s srt` from the
  ffmpeg command. Only video and audio are remuxed.

### ffmpeg Command (with subs)

```
ffmpeg -v warning
  -reconnect 1 -reconnect_streamed 1
  -i {url}
  -map 0:v:0 -map 0:a -map 0:s?
  -c copy -c:s srt
  -f matroska
  -fflags +genpts+flush_packets
  pipe:1
```

### ffmpeg Command (without subs)

```
ffmpeg -v warning
  -reconnect 1 -reconnect_streamed 1
  -i {url}
  -map 0:v:0 -map 0:a
  -c copy
  -f matroska
  -fflags +genpts+flush_packets
  pipe:1
```

## Feature 2: Byte-Range Seeking

### Overview

Kodi seeks by sending HTTP Range requests (`Range: bytes=X-`). The
proxy translates byte offsets into timestamps, kills the current ffmpeg
process, and spawns a new one with `-ss {timestamp}`. Kodi gets a
progress bar and can fast-forward/rewind.

### Prepare Phase

During `prepare_stream()`, after finding ffmpeg:

1. **Get file size:** Already done via `_get_content_length()` (HEAD
   request). Stored as `total_bytes`.
2. **Get duration:** Run `ffmpeg -i {url} -f null -` (exits immediately
   after reading headers). Parse `Duration: HH:MM:SS.xx` from stderr.
   Stored as `duration_seconds`. No ffprobe binary needed — uses the
   same ffmpeg binary.
3. **If duration probe fails:** Fall back to non-seekable mode (linear
   pipe, `Accept-Ranges: none`). The remux still works; only seeking
   is disabled.

### HTTP Response Logic

| Request | Condition | Action |
|---|---|---|
| HEAD | Always | `Content-Length: {total_bytes}`, `Accept-Ranges: bytes` |
| GET `bytes=0-` | Initial play | Spawn ffmpeg from start. Respond 206 with `Content-Range: bytes 0-{total_bytes-1}/{total_bytes}` |
| GET `bytes=X-` | X is close to current position (within 10MB) | Continue streaming from existing ffmpeg. Do not respawn. |
| GET `bytes=X-` | X is far from current position (>10MB gap or backward) | Kill ffmpeg. Respawn with `-ss {seek_seconds}`. Respond 206 with `Content-Range: bytes X-{total_bytes-1}/{total_bytes}` |

### Byte-to-Time Mapping

```
seek_seconds = (X / total_bytes) * duration_seconds
```

This is approximate for VBR content — seeks may land a few seconds
before or after the requested position. Kodi does not validate that the
byte counts match exactly; it uses `Content-Range` for progress bar
positioning only.

### ffmpeg Seeking Flag

When seeking, add `-ss {seek_seconds}` **before** `-i` for input-level
seeking (fast, demuxer-level). This avoids decoding all frames up to
the seek point.

```
ffmpeg -ss {seek_seconds}
  -v warning
  -reconnect 1 -reconnect_streamed 1
  -i {url}
  -map 0:v:0 -map 0:a [-map 0:s? -c:s srt]
  -c copy
  -f matroska
  -fflags +genpts+flush_packets
  pipe:1
```

### Process Management

- Store the active `subprocess.Popen` reference on the server object
  (`self.server.active_ffmpeg`).
- On each new GET: acquire a lock, kill the previous ffmpeg process if
  still running, spawn the new one, release the lock.
- The 10MB tolerance window prevents unnecessary respawns from Kodi's
  normal buffering probes and range re-requests. Only genuine jumps
  (skip-forward, rewind, scrub) trigger a respawn.
- Track `current_byte_position` (total bytes sent) on the server object
  to determine whether a request is a continuation or a seek.

### Graceful Degradation

| Condition | Behavior |
|---|---|
| No ffmpeg binary found | MKV plays direct, MP4 plays direct (may fail on pathological files) |
| ffmpeg found, duration probe fails | Remux works, no seeking (linear pipe) |
| ffmpeg found, duration obtained | Full remux with seeking and progress bar |

## Files Changed

- **`stream_proxy.py`**: Add duration probe in `prepare_stream()`. Add
  seek detection and ffmpeg respawn logic in `_serve_remux()`. Add
  subtitle flags to ffmpeg command. Add process management with lock.
- **`resolver.py`**: No changes needed (already routes MP4 through proxy).
- **`resources/settings.xml`**: Add `proxy_convert_subs` boolean setting.
- **`resources/language/.../strings.po`**: Add label for new setting.
- **`tests/test_stream_proxy.py`**: Add tests for duration parsing,
  seek detection threshold, subtitle flag toggling, process management.

## Out of Scope

- HLS segmented output (requires `inputstream.adaptive` which is broken
  on the target device).
- Exact byte-accurate seeking (impractical for VBR remuxed streams).
- Multiple simultaneous streams (single-stream context is sufficient for
  the addon's use case).
