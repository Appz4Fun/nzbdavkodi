## Part C — Stream Proxy Architecture Reference (`PROXY.md`)

> **Status:** This reference reflects the proxy as of `v1.0.0-pre-alpha` (tagged on `spike/hls-fmp4`). The fmp4 HLS branch (Tier 3.4.2) is opt-in via `force_remux_mode=hls_fmp4` and the runtime self-healing fallback to matroska is the safety net. The other three tiers (direct redirect, virtual MP4 faststart, MKV pass-through) are unchanged from main.

This part describes the local HTTP proxy that sits between Kodi's player and nzbdav's WebDAV server. Start here if you are touching any code in `plugin.video.nzbdav/resources/lib/stream_proxy.py` — or if you are trying to work out why a particular file plays the way it does.

Companion document to `README.md`. The README covers user-facing behavior; this section covers the internals.

---

### C.1 Why a proxy at all?

The proxy exists to solve four distinct problems that Kodi's native input streams can't handle on the target deployment (Kodi 21 Omega on 32-bit CoreELEC ARM):

1. **`PROPFIND` on the parent directory.** When Kodi 21 opens an HTTP URL whose path looks like a file, `CCurlFile` issues a `PROPFIND` against the parent directory first. On nzbdav's WebDAV endpoint, this triggers a recursive directory scan that either times out or throws `Open - Unhandled exception`. Routing the URL through a localhost HTTP server that only answers `GET`/`HEAD` cuts the `PROPFIND` path entirely. This is the original reason the proxy was introduced (see CHANGELOG v0.6.14).
2. **MP4 `moov` atom at the tail.** Fresh REMUX rips often have the `moov` box after `mdat`. Kodi's MP4 demuxer cannot play these without downloading the whole file first. The proxy solves this in pure Python by parsing the atom tree over ranged HTTP fetches and serving a "virtual faststart" MP4 where the `moov` has been moved to the front and the chunk offsets rewritten.
3. **32-bit Kodi and large files.** On 32-bit CoreELEC builds (the deployment this addon is tuned for), Kodi's `CFileCache` layer has a signed 32-bit offset somewhere in its cache bookkeeping. Pass-through of any file whose advertised `Content-Length` exceeds ~4 GB crashes playback open with `Open - Unhandled exception`. The proxy hides the true size behind a force-remux path that streams an unsized ffmpeg pipe, so Kodi never sees the overflowing number. See `memory/project_32bit_kodi_largefile_limit.md` and §D.2 for the detailed diagnosis — §D.2 also covers the `advancedsettings.xml` bypass that obviates force-remux.
4. **Missing Usenet articles.** nzbdav returns HTTP errors when a requested byte range hits unrecoverable articles. Kodi's native input stream treats that as fatal. The proxy catches upstream errors mid-response, probes forward to find the next readable offset, writes zero bytes across the gap, and keeps streaming. See §C.5.3.

Everything else the proxy does is in service of one of those four.

---

### C.2 Components and files

The proxy is not a single file. It is a subsystem that threads through most of the `resources/lib/` tree. This table shows who owns what and how the pieces connect.

| File | Role in the proxy subsystem |
|---|---|
| `stream_proxy.py` | Everything that runs inside the proxy process: `StreamProxy` (threaded `HTTPServer`), `_StreamHandler` (`BaseHTTPRequestHandler` that dispatches per-session), `HlsProducer` (ffmpeg lifecycle for the HLS fmp4 branch), `_parse_range`, `_parse_hls_resource`, `_validate_url`, `_embed_auth_in_url`, `_parse_ffmpeg_duration`, all four serving paths. |
| `mp4_parser.py` | Pure-Python MP4 box parser used by the virtual-faststart tier. Reads the box tree via `http_util.http_get_range`, locates `moov`/`mdat`, rewrites `stco`/`co64` chunk offsets. No ffmpeg dependency. Called from `stream_proxy._try_faststart_layout`. |
| `dv_rpu.py` | Pure-Python Dolby Vision RPU parser. Detects profile 5/7/8 and classifies P7 MEL vs FEL from the NLQ fields. Ports the minimum subset of `quietvoid/dovi_tool` needed for classification. No ffmpeg dependency. |
| `dv_source.py` | Remote container probe. Fetches only the bytes needed to locate the first HEVC access unit in an MP4 or MKV, extracts the first UNSPEC62 RPU NAL, and hands it to `dv_rpu.parse_rpu_payload` for classification. Returns a structured `DolbyVisionSourceResult` to `stream_proxy.prepare_stream`. |
| `http_util.py` | Shared HTTP primitives: `http_get`, `http_get_range`, `redact_url`, `notify`, scheme validation. The proxy and the parser both use `http_get_range` to avoid a hand-rolled ranged fetcher in two places. |
| `service.py` | Owns the long-lived `StreamProxy` instance. `service.py` starts the proxy on Kodi startup and stores the listening port in a window property (`nzbdav.proxy.port`) so `resolver.py` can find it. Also runs the playback monitor (`NzbdavPlayer`) that retries on failure and tears down sessions on stop. |
| `resolver.py` | Calls `prepare_stream_via_service(port, remote_url, auth_header)` in the service's process over a local control socket. Receives back the local proxy URL and stream info dict, wires them into a Kodi `ListItem`, and hands off to `xbmcplugin.setResolvedUrl`. This is the only place user code crosses the process boundary from the plugin process into the service process. |
| `router.py` | Entry point from Kodi. Dispatches `/play`, `/resolve`, and everything else. Not part of the proxy per se, but every play-path request flows through here before reaching the resolver. The C1 fix in the current branch ensures the handle is always resolved even if an action route throws. |
| `webdav.py` | Produces the remote URL that the proxy actually fetches from. Embeds basic auth into the URL or into a `Authorization` header depending on nzbdav's server config. |
| `playback_monitor.py` | Observes Kodi's player events and restarts a session if the decoder stalls on the proxy side. Does not itself speak HTTP — it signals through service state. |
| `resources/settings.xml` | Settings that the proxy reads at session construction: `force_remux_threshold_mb`, `force_remux_mode`, `proxy_convert_subs`. |

Inside `stream_proxy.py` the main classes are:

- **`StreamProxy`** — owns the `ThreadingHTTPServer`, the `stream_sessions` dict keyed by session UUID, the per-session context lock, `prepare_stream`, `_register_session`, `clear_sessions`, and the tier-selection logic that decides which of the four serving paths a given `remote_url` will use. One instance per Kodi service.
- **`_StreamHandler`** — per-request handler. Parses the URL to pull out a session ID, looks up the context, and dispatches to `_serve_direct` / `_serve_mp4_faststart` / `_serve_proxy` / `_serve_remux` / `_handle_hls`. Responsible for `Range` header parsing, `206 Partial Content`, `416 Range Not Satisfiable`, and `Connection: close` discipline.
- **`HlsProducer`** — spawns and supervises `ffmpeg` for the experimental fmp4 HLS force-remux branch. Manages a per-session working directory, handles seek-driven respawns, tracks segment generation boundaries with `_spawn_time`, and gates segment completeness checks so Kodi can't read a seg that is still being written.

---

### C.3 Session lifecycle

A single play is one session. The lifecycle below is the same for every tier — only the handler dispatch at step 5 differs.

```text
┌────────────────┐                                          ┌──────────────────┐
│ router.py      │                                          │ service.py       │
│ /resolve route │                                          │ StreamProxy      │
└───────┬────────┘                                          └─────────┬────────┘
        │  resolve_and_play(nzburl, title)                            │
        ▼                                                             │
┌───────────────────┐                                                 │
│ resolver.py       │                                                 │
│ _poll_until_ready │                                                 │
└────────┬──────────┘                                                 │
         │  remote_url from webdav.py                                 │
         ▼                                                            │
┌────────────────────┐    prepare_stream_via_service(port, url)       │
│ prepare_stream_via │────────────────────────────────────────────────▶
│ _service           │◀───────────────────────────────────────────────│
└─────────┬──────────┘   (local_url, stream_info)                     │
          │                                                           │
          │                                    (1) clear_sessions()   │
          │                                    (2) probe content type │
          │                                    (3) tier selection     │
          │                                    (4) _register_session  │
          │                                        → session UUID     │
          ▼                                                            │
┌─────────────────────┐                                                │
│ xbmcplugin          │    local_url = http://127.0.0.1:PORT/...       │
│ .setResolvedUrl     │                                                │
└─────────┬───────────┘                                                │
          │                                                            │
          ▼                                                            │
┌─────────────────────┐   GET /stream/<UUID>?...                       │
│ Kodi CVideoPlayer   │───────────────────────────────────────────────▶│
│ (CCurlFile)         │◀───────────────────────────────────────────────│
└─────────────────────┘   206 chunks stream out                        │
                                                                       │
                                       (5) handler looks up ctx        │
                                           dispatches to tier path     │
                                                                       │
         onPlayBackStopped / onPlayBackEnded                           │
                  │                                                    │
                  ▼                                                    │
         NzbdavPlayer clears the session ─────────────────────────────▶│
                                           clear_sessions()             │
                                           HlsProducer.close() if any  │
                                           rm -rf session workdir       │
```

Key invariants:

- **Only one session is live at a time.** Kodi plays one stream; `prepare_stream` calls `clear_sessions()` before registering a new session, which kills any lingering `HlsProducer` and deletes its workdir. This is the guard against zombie ffmpeg surviving an unclean stop.
- **Session IDs are server-generated UUIDs.** The session ID in the URL Kodi receives is never user-controllable. `_register_session` stamps `ctx["session_id"] = uuid.uuid4().hex` and every filesystem path inside the HLS workdir is built from that, not from whatever arrives on the wire. This is why path traversal via the HLS URL is not a concern.
- **The context dict is the source of truth.** Each tier's serving path reads everything it needs from `ctx` — `remote_url`, `auth_header`, `content_type`, `remux`, `faststart`, `mode`, `duration_seconds`, `total_bytes`, per-tier extras like `header_data` / `virtual_size` for faststart or `hls_segment_format` / `hls_segment_duration` for HLS. The handler code is stateless between requests aside from that dict.
- **`prepare_stream` runs in the service process.** The plugin process (`resolver.py`) uses `prepare_stream_via_service` to cross the process boundary via a local control socket. This keeps the `stream_sessions` table on the same side of the wall as the HTTP server.

---

### C.4 Tier selection

`StreamProxy.prepare_stream(remote_url, auth_header)` picks exactly one of four serving paths based on the file's container and size. The decision tree:

```text
prepare_stream(remote_url, auth_header)
│
├── Is the URL's path suffix .mp4 / .m4v?
│   │
│   ├── YES ── _get_content_length + _try_faststart_layout
│   │         │
│   │         ├── already_faststart (moov in front) ─▶ TIER 0: direct redirect
│   │         │
│   │         ├── moov_at_tail parsed OK            ─▶ TIER 1: virtual faststart
│   │         │                                        (mp4_parser rewrites stco/co64)
│   │         │
│   │         └── parse failed or file > 4 GB       ─▶ ffmpeg tempfile remux
│   │                                                 (fallback to matroska pipe
│   │                                                  if ffmpeg unavailable)
│   │
│   └── NO (MKV, MKA, WebM, TS, other)
│       │
│       ├── content_length < force_remux_threshold  ─▶ TIER 2: pass-through
│       │                                              (ranged upstream fetch
│       │                                               with zero-fill recovery)
│       │
│       └── content_length ≥ force_remux_threshold  ─▶ TIER 3: force remux
│                                                       │
│                                                       ├── force_remux_mode = matroska
│                                                       │   (default) ─▶ piped MKV
│                                                       │                via ffmpeg
│                                                       │
│                                                       └── force_remux_mode = hls_fmp4
│                                                           (experimental) ─▶ HLS fmp4
│                                                                             playlist
│                                                                             (gated on
│                                                                              DV profile
│                                                                              probe — P7
│                                                                              falls back
│                                                                              to matroska)
```

#### C.4.1 Settings that influence the decision

| Setting | Default | Where read | Effect |
|---|---|---|---|
| `force_remux_threshold_mb` | `20000` (20 GB), bounds `[0..1048576]` MB (1 TB cap) | `_get_force_remux_threshold_bytes` | Non-MP4 files at or above this size take the force-remux branch. `0` disables force remux entirely (files stream pass-through regardless of size). Values above `1048576` clamp silently (see §D.8.2). The 20 GB default is the breakpoint below which 32-bit Kodi has been observed to handle pass-through cleanly (12 GB tested) but above which it crashes (58 GB reproduces). |
| `force_remux_mode` | `matroska` | `_get_force_remux_mode` | Picks the shape of the force-remux output. `matroska` (empty / `0` / default) pipes an unsized MKV with `-c copy`. `hls_fmp4` (setting value `1`) switches to fragmented-MP4 HLS via `HlsProducer`. `passthrough` (value `2`) skips force remux entirely and requires `advancedsettings.xml` cache=0. The fmp4 branch is opt-in and experimental. |
| `proxy_convert_subs` | `true` | `_build_ffmpeg_cmd` | For MP4 → MKV remux, converts `mov_text`/`TX3G` to `srt`. MKV sources always use `-c:s copy` to avoid aborting on PGS/HDMV/DVD bitmap subs. |

Reliability/contract flags (`strict_contract_mode`, `density_breaker_enabled`, `retry_ladder_enabled`, `zero_fill_budget_enabled`, `send_200_no_range`) influence pass-through behavior rather than tier selection; their PR-1 defaults are listed in §A.4.1, and `strict_contract_mode`'s ENFORCE edge case is documented in §D.8.1.

---

### C.5 The four serving paths

#### C.5.1 Tier 0: direct MP4 redirect (`_serve_direct` or return upstream URL)

If the file is already faststart (moov before mdat), the proxy gets out of the way. `prepare_stream` returns the remote WebDAV URL itself instead of a local proxy URL, and Kodi seeks/plays it natively. This is the fastest tier — no process mediation, no extra sockets. The tradeoff is that Kodi's `PROPFIND` risk comes back, so this tier is only used for MP4 inputs where the parent-directory scan has been verified safe (nzbdav's WebDAV server handles `PROPFIND` on file paths cleanly enough that direct playback works for files that are already faststart-shaped).

#### C.5.2 Tier 1: virtual MP4 faststart (`_serve_mp4_faststart`)

When the moov is at the tail, `mp4_parser.py` reads the atom tree over ranged HTTP fetches:

1. Walk the top-level box list looking for `ftyp`, `mdat`, `moov`.
2. If `moov` is before `mdat`, short-circuit (already faststart).
3. Otherwise fetch the moov, decode its tree, and rewrite all `stco` (32-bit) and `co64` (64-bit) chunk-offset tables to account for the moov moving from tail to front.
4. Compose a virtual "header" blob: `ftyp` + rewritten `moov` + a 16-byte `free` pad.
5. Compute a virtual file size: `len(header) + mdat_size + trailing_bytes_after_mdat`.

`_serve_mp4_faststart` then serves ranged responses against this virtual layout:

- Ranges within the header range return bytes directly from the in-memory `header_data`.
- Ranges within the payload range translate to an upstream ranged fetch against the real file (payload offset + payload_remote_start).
- Ranges that straddle the boundary serve the header portion from memory and the payload portion from upstream in a single response.

This tier produces a valid MP4 byte stream that Kodi can seek natively — no ffmpeg, no remux, no extra CPU. The parser is pure Python (no dependencies).

If parsing fails — unusual box structure, unsupported `co64` edge case, or the file is > 4 GB (which would blow the tempfile tier's time budget) — `prepare_stream` falls through to the ffmpeg tempfile remux path (writes an faststart-shaped MP4 to `/tmp`, serves it as a static file) or, if ffmpeg is missing, to the matroska pipe path.

#### C.5.3 Tier 2: pass-through with zero-fill recovery (`_serve_proxy`)

For MKV, WebM, TS, and other non-MP4 containers at or below the force-remux threshold, the proxy does the simplest possible thing: it forwards Kodi's `Range` request to the upstream WebDAV URL and streams the response back byte-for-byte.

The subtlety is error recovery. On real Usenet sources, a small fraction of requested byte ranges hit articles that nzbdav cannot reconstruct. Without intervention, `urlopen` raises an `HTTPError` mid-response and Kodi's decoder dies.

`_serve_proxy` handles this by:

1. Catching upstream errors on each chunk read.
2. Issuing a HEAD-like probe forward from the current offset in increasing steps to find the next readable byte.
3. Writing `_ZERO_FILL_BUFFER` bytes across the gap (capped at `_MAX_TOTAL_ZERO_FILL = 64 MB` per response — enough for several seconds of 4K REMUX video; more than that probably means the file is mostly unrecoverable and the stream should just end).
4. Resuming the upstream read at the recovered offset.

Kodi sees a continuous byte stream with a few silent frames instead of a fatal decoder error.

Pass-through also handles:

- **`Range` parsing.** Standard `bytes=A-B` / `bytes=A-` / `bytes=-N` suffix ranges, `416` on out-of-bounds.
- **64 KB upstream read chunks** (`_UPSTREAM_READ_CHUNK`). Small because on 32-bit Kodi the address space is ~3 GB and a second concurrent handler thread opened during Kodi's `CCurlFile` reconnect-on-error recovery has been observed to hit `MemoryError` with 1 MB buffers.
- **`Connection: close` on every response.** Forces stale handler threads to unwind when Kodi reconnects instead of piling up on a keep-alive socket.
- **`Content-Type` from the file suffix.** `video/x-matroska` for `.mkv`, `video/webm` for `.webm`, `video/mp2t` for `.ts`, etc.

#### C.5.4 Tier 3: force remux

Used when the file is larger than `force_remux_threshold_mb` MB and non-MP4. Two shapes, driven by `force_remux_mode`:

##### C.5.4.1 Matroska pipe (default)

`_serve_remux` spawns `ffmpeg -i <upstream> -c copy -f matroska pipe:1` and streams stdout straight to Kodi with no `Content-Length`. Because the response size is unknown, Kodi treats the stream as "live" — which sidesteps the 32-bit `CFileCache` offset overflow — but also means seek is approximate. On a seek, `_serve_remux` kills the running ffmpeg and respawns with `-ss TARGET`; Kodi re-requests from byte 0 of the new process and gets a stream starting at the seeked-to keyframe.

Key properties:

- **`-c copy` for video and audio.** No re-encode. DV HEVC metadata, TrueHD / Atmos, DTS-HD MA all pass through untouched.
- **`-metadata DURATION=`** emitted into the MKV Segment Info so Kodi knows the total length — without this, piped MKV would look like a live stream and Kodi would hide the progress bar and disable seeking entirely.
- **`proxy_convert_subs`** controls whether MP4 `mov_text` subtitles are converted to `srt`. For MKV sources, `-c:s copy` is forced regardless — PGS/HDMV/DVD bitmap subtitles cannot be re-encoded to `srt` and would abort the remux.
- **60 s socket write timeout** (`_REMUX_WRITE_TIMEOUT`). If Kodi's decoder stalls without closing the socket, `wfile.write()` would block forever and ffmpeg would produce into the void. The timeout bounds zombie lifetime.

This is the known-good path for every huge file scenario that has been tested on the target device. Dolby Vision HEVC + TrueHD/Atmos 100 GB REMUXes play through this branch.

##### C.5.4.2 Fragmented MP4 HLS (experimental, opt-in)

`force_remux_mode=hls_fmp4` switches to an HLS VOD playlist backed by fragmented MP4 segments. The motivation is full random seek (the matroska pipe path is seek-approximate because each seek costs an ffmpeg respawn); the tradeoff is that HLS fmp4 on the Amlogic hardware decoder has not been proven stable for all content types.

Pipeline:

```text
prepare_stream
  └── ctx with content_type = application/vnd.apple.mpegurl,
             mode = "hls",
             hls_segment_format = "fmp4",
             hls_segment_duration = 30.0
  └── _register_session
        └── HlsProducer(ctx, base_workdir)
              ├── makedirs(session_dir)
              └── prepare()          ← eager spawn-time validation
                    └── Popen(ffmpeg ...) + poll 500 ms for early exit
                          └── on failure: _register_session catches the
                              exception and rewrites ctx in-place to the
                              matroska shape BEFORE returning the URL
                              (late-binding fallback)
```

###### C.5.4.2.a HTTP routes

HTTP routes exposed for an HLS session:

| Route | Handler | Purpose |
|---|---|---|
| `GET /hls/<session>/playlist.m3u8` | `_serve_hls_playlist` | VOD playlist with `#EXT-X-VERSION:7`, `#EXT-X-MAP:URI="init.mp4"` (fmp4 only), one `#EXTINF`/segment URI per `seg_%06d.m4s`, and `#EXT-X-ENDLIST`. |
| `GET /hls/<session>/init.mp4` | `_serve_hls_init` | Reads `HlsProducer.wait_for_init()` (which gates on the init file being fully written by the current ffmpeg generation) and serves the bytes with `Content-Type: video/mp4`. |
| `GET /hls/<session>/seg_NNNNNN.m4s` | `_serve_hls_segment` | Reads `HlsProducer.wait_for_segment(seg_n)` which blocks until the segment is complete, then serves the bytes with `Content-Type: video/mp4`. |

###### C.5.4.2.b ffmpeg lifecycle (`HlsProducer`)

`HlsProducer` manages the ffmpeg lifecycle:

- **One ffmpeg process, respawned on seeks.** `_ensure_ffmpeg_headed_for(seg_n)` checks whether the live process will eventually produce `seg_n` as it streams forward. If not (process dead, seeked backward, or seeked >60 segments forward), it kills and respawns with `-ss (seg_n * segment_seconds)`.
- **`-copyts` + `-ss T` before `-i`.** Together these make the new ffmpeg's first-frame PTS equal to `T`, which matches Kodi's EXTINF-based global time at `seg_T/segment_seconds`. Critical for seek-respawn continuity — the alternative (`-reset_timestamps 1`) was tried and caused Amlogic decoder stalls with "messy timestamps" errors. The current code does NOT add `-output_ts_offset`; with `-copyts + -ss T` the offset is already correct and adding another would double it.
- **Generation boundaries.** Every respawn unlinks `init.mp4` and the new target `seg_<N>.m4s` before `Popen`, and stamps `self._spawn_time = time.time()`. `_segment_complete(n)` for fmp4 sessions verifies that a seen `seg_<n+1>.m4s` was created after `_spawn_time` — otherwise a stale segment from a prior generation could make the "next file exists" signal falsely report that a half-written `seg_<n>` is complete. Prior-generation segments at other indices are deliberately left on disk to serve backward-seek from cache.
- **fmp4 init gate.** `wait_for_segment` in fmp4 mode checks `_init_file_complete` before returning any segment — `init.mp4` must be on disk with a valid size before Kodi sees its first segment.
- **Session stderr log.** Each session opens `ffmpeg.log` in its workdir at construction time and reuses it across every respawn. Fixes a latent `stderr=PIPE` deadlock from the earlier persistent-producer era.
- **`-tag:v hvc1`.** Forces the HLS-spec sample entry tag on HEVC video. HLS fmp4 mandates `hvc1` (parameter sets in the sample description box, not inband) and Amlogic's HLS demuxer uses this tag to locate the `dvcC`/`dvvC` DV configuration record in the init segment. Without the tag, `hev1`-sourced HEVC copied into fmp4 can hide the DV config from the hardware decoder.
- **DV source-RPU classifier.** Before committing to the fmp4 branch, `prepare_stream` calls `probe_dolby_vision_source` (in `dv_source.py`), which fetches only the bytes needed to locate the first HEVC access unit in the source (moov walk for MP4, EBML Segment → Tracks + Cluster → SimpleBlock for MKV), extracts the first UNSPEC62 RPU NAL, and hands it to `dv_rpu.parse_rpu_payload` for classification. Returns a structured `DolbyVisionSourceResult` with fields `classification` ∈ {`dv_profile_7_fel`, `dv_allowed_for_fmp4`, `non_dv`, `dv_unknown`}, `reason`, `profile`, `el_type`. The routing matrix is:
  - **P7 FEL** → matroska. fmp4 cannot carry the dual-layer BL+EL structure; dropping the EL silently would stall the Amlogic decoder.
  - **P7 MEL** → fmp4. MEL is ~2 Mbps of NLQ metadata (mapping coefficients), not a second HEVC layer — so it doesn't exercise the CAMLCodec dual-layer init path that tripped P8 on 2026-04-15. Experimental; tighten to P7-unconditional-matroska if field testing shows MEL also hangs.
  - **P8 / P5 / any other confirmed DV** → matroska. The 2026-04-15 Evangelion P8 test proved the Amlogic fmp4 DV path hangs at `onAVStarted` regardless of single-layer vs dual-layer.
  - **non-DV** → fmp4 (the happy path — the probe confirmed no UNSPEC62 NAL in the first sample).
  - **dv_unknown** (probe crash, unsupported container, truncated RPU) → matroska. Fail safe.

  The classifier runs on the service worker thread, so a 2–3-range HTTP probe typically adds <1 s to `prepare_stream` (a net **improvement** over the retired ffmpeg-stderr probe, which needed 5–10 s to spawn + analyse). A probe crash is caught at the call site and degrades to `dv_unknown` → matroska.
- **Late-binding matroska fallback.** `HlsProducer.prepare()` spawns ffmpeg immediately and polls 500 ms for early exit. If the deployed ffmpeg build rejects `-hls_segment_type fmp4`, `_register_session` catches the exception, calls `producer.close()` (best effort), and rewrites `ctx` in place to the matroska shape — all before returning the URL to Kodi. This guarantees that a bad ffmpeg build never hands Kodi a dead HLS URL.

###### C.5.4.2.c Working directory selection

`_choose_hls_workdir` walks a candidate list (`/var/media/CACHE_DRIVE/nzbdav-hls`, `/var/media/STORAGE/nzbdav-hls`, `/storage/nzbdav-hls`, `/tmp/nzbdav-hls`) and picks the first writable entry with enough free space. Each session gets its own subdirectory, which is `rm -rf`'d on session cleanup.

---

### C.6 Known constraints and gotchas

- **Kodi is single-stream.** The proxy does not need to multiplex sessions. `clear_sessions()` on every new `prepare_stream` is correct, not a limitation.
- **32-bit CoreELEC is the design target.** Several defaults (64 KB read chunks, 20 GB force-remux threshold, `Connection: close`) exist specifically because of the 32-bit address-space and `CFileCache` overflow behavior. 64-bit Kodi users could relax these, but the code is tuned for the worst case.
- **`session_id` is not user-controllable for file paths.** Every filesystem path uses `ctx["session_id"]` (a server-generated `uuid4().hex`), not the ID fragment from the URL. URL-supplied IDs are only used to look up sessions in the in-memory dict. Don't undo this when refactoring.
- **HLS fmp4 is still an opt-in spike.** `force_remux_mode` defaults to `matroska` for a reason — commit `50a6eb3` documents the Amlogic DV HEVC stall on the fmp4 path. The DV profile gate and the `hvc1` tag added in the current branch are the first two mitigations; deeper diagnosis is still open.
- **No Content-Length on remux branches.** Both force-remux shapes deliberately omit `Content-Length` (matroska pipe) or advertise it per-segment (HLS fmp4). This is how they sidestep the 32-bit offset overflow. Do not add a file-level `Content-Length` to either.
- **`-c copy` means format flaws survive.** The proxy preserves DV RPU SEIs, TrueHD/Atmos substreams, and subtitle codecs because it never re-encodes on the playback path. If the source has a broken container, the proxy cannot fix it.
- **ffmpeg auth in argv.** `_embed_auth_in_url` splices basic-auth credentials into the URL passed to ffmpeg as argv. Credentials are visible via `/proc/<pid>/cmdline` to local processes for the lifetime of the ffmpeg. A future cleanup should move auth to a short-lived `AUTHORIZATION` header via ffmpeg's `-headers` flag.

---

### C.7 Adding a new tier

If you want to add a fifth serving path:

1. Pick a tier selection branch in `StreamProxy.prepare_stream`. The decision tree is sequential — put your case above anything it would override.
2. Decide what goes in `ctx`. Define the minimum set of keys your handler will read. Treat `ctx` as immutable after `_register_session` returns.
3. Add a `_serve_<name>` method on `_StreamHandler`. It must:
   - Handle `Range` via `_parse_range` (if applicable).
   - Set `Connection: close` and `self.close_connection = True` on every response path.
   - Return `416` on out-of-bounds ranges, `404` on missing context, `500` on upstream exceptions.
   - Log at `xbmc.LOGINFO` or `xbmc.LOGDEBUG`, never `print`.
4. Wire dispatch in `do_GET` / `do_HEAD` / the URL parser. If your URL shape doesn't fit `/stream/<uuid>`, add a new prefix in `_parse_hls_resource` style.
5. Add tests in `tests/test_stream_proxy.py` — there's a pattern for `StreamProxy.__new__(StreamProxy) + mock _server + patch _get_content_length + patch Popen` that covers the tier-selection branch without needing a real Kodi process.
6. If your tier spawns subprocesses, use the `HlsProducer` pattern: eager `prepare()` for fail-fast validation, session-wide stderr log to avoid PIPE deadlock, `close()` in the session teardown path.

---

### C.8 Where to look when debugging

| Symptom | First place to look |
|---|---|
| Playback never starts | `kodi.log` around the `prepare_stream` call — which tier was chosen? Is the URL returned correct? Was `setResolvedUrl` called? |
| Playback starts then stalls | The session's `ffmpeg.log` (HLS) or the proxy's `_serve_remux` / `_serve_proxy` log lines. Look for upstream `HTTPError`, `MemoryError`, or socket timeout. |
| `Open - Unhandled exception` on a huge file | 32-bit `CFileCache` overflow — is `force_remux_threshold_mb` set low enough to catch this file? Is `_get_content_length` returning the real size? See `memory/project_32bit_kodi_largefile_limit.md`. |
| Black screen with silent audio mid-stream | Zero-fill recovery fired — grep `_serve_proxy` for `zero-fill` log lines. If the fill exceeds `_MAX_TOTAL_ZERO_FILL`, the stream is too corrupt to save and the response ends. |
| HLS session hangs at "buffering" | `wait_for_segment` or `wait_for_init` timing out. Check `ffmpeg.log` for the session — is ffmpeg alive? Is it producing segments? Is `_init_ready` ever getting set? |
| Seek freezes on matroska pipe | Expected — the pipe path kills and respawns ffmpeg on seek, and the first few seconds after a respawn are unavoidable cold-start latency. If it never recovers, check for a zombie ffmpeg from a previous session (should have been killed by `clear_sessions`; if not, that's a lifecycle bug). |
| DV HEVC stalls on hls_fmp4 | Expected — this is why `force_remux_mode` defaults to `matroska`. Profile 7 is explicitly routed around fmp4; P5/P8 may still stall. Switch `force_remux_mode` back to matroska (the default). |
| Every scrub past 4 GB returns `streamed=0` | 32-bit CFileCache seek-delta truncation (`FileCache.cpp:375`). Fix: `<cache><memorysize>0</memorysize></cache>` in `advancedsettings.xml`. See Part D §D.2. |

---

### C.9 Related memory and documentation

- `memory/project_32bit_kodi_largefile_limit.md` — full diagnosis of the 32-bit `CFileCache` overflow behavior.
- `memory/reference_test_device.md` — deployment target specifics (UGOOS AM6B CoreELEC, 32-bit Kodi binary on 64-bit kernel, WebDAV backend, `advancedsettings.xml`).
- `CHANGELOG.md` and `plugin.video.nzbdav/changelog.txt` — user-facing release notes including every proxy behavior change.
- `CLAUDE.md` — project layout, test commands, release workflow, Python 3.8 compatibility constraint.

---
