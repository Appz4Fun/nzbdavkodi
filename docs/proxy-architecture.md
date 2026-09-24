## Part C — Stream Proxy Architecture Reference

> **Status:** This reference describes the current proxy code, where `stream_proxy.py` is a facade over `stream_proxy_*.py` sibling modules (split out in the 2.0.0-beta.1 refactor). **Pass-through is the default for every non-MP4 container.** Force-remux (piped Matroska, or the experimental fragmented-MP4 HLS branch with its self-healing fallback to Matroska) is opt-in through the `force_remux_mode` setting. MP4 sources still get moov-relocation rescue tiers. There is no direct-redirect tier: every play is served through the local proxy.

This part describes the local HTTP proxy that sits between Kodi's player and nzbdav's WebDAV server. Start here if you are touching any `resources/lib/stream_proxy*.py` module in `repo/plugin.video.nzbdav/` — or if you are trying to work out why a particular file plays the way it does.

Companion document to the user docs in `docs-site/` (notably [`how-it-works/stream-proxy.md`](../docs-site/how-it-works/stream-proxy.md) and [`how-it-works/fallback-cutover.md`](../docs-site/how-it-works/fallback-cutover.md)). Those cover user-facing behavior; this section covers the internals.

---

### C.1 Why a proxy at all?

The proxy exists to solve four distinct problems that Kodi's native input streams can't handle on the target deployment (Kodi 21 Omega on 32-bit CoreELEC ARM):

1. **`PROPFIND` on the parent directory.** When Kodi 21 opens an HTTP URL whose path looks like a file, `CCurlFile` issues a `PROPFIND` against the parent directory first. On nzbdav's WebDAV endpoint, this triggers a recursive directory scan that either times out or throws `Open - Unhandled exception`. Routing the URL through a localhost HTTP server that only answers `GET`/`HEAD` cuts the `PROPFIND` path entirely. This is the original reason the proxy was introduced (see CHANGELOG v0.6.14).
2. **MP4 `moov` atom at the tail.** Fresh REMUX rips often have the `moov` box after `mdat`. Kodi's MP4 demuxer cannot play these without downloading the whole file first. The proxy solves this in pure Python by parsing the atom tree over ranged HTTP fetches and serving a "virtual faststart" MP4 where the `moov` has been moved to the front and the chunk offsets rewritten.
3. **32-bit Kodi and large files.** On 32-bit CoreELEC builds (the deployment this addon is tuned for), Kodi's `CFileCache` layer has a signed 32-bit offset somewhere in its cache bookkeeping. Pass-through of a file whose advertised `Content-Length` is large enough (12 GB tested clean; 15.8 GB and 58 GB REMUXes reproduced the crash) fails at open with `Open - Unhandled exception`, and scrubs past 4 GB can fail. There are two escapes: set `<cache><memorysize>0</memorysize></cache>` in `advancedsettings.xml` (the recommended fix, which keeps pass-through and full seeking; see [`reference/advancedsettings.md`](../docs-site/reference/advancedsettings.md)), or opt into a force-remux mode, which hides the true size behind an unsized ffmpeg stream. NZB-DAV never writes `advancedsettings.xml`. It only reads it (`kodi_advancedsettings.py`): when a remux tier fires on the handle-less `resolve_and_play` path (TMDBHelper's RunScript player) and the cache setting is missing, `cache_prompt.py` shows the snippet in an advisory dialog, at most once per Kodi session.
4. **Missing Usenet articles.** nzbdav returns HTTP errors when a requested byte range hits unrecoverable articles. Kodi's native input stream treats that as fatal. The proxy catches upstream errors mid-response, retries, switches to a validated fallback release when one is attached, and only then probes forward to find the next readable offset, writes zero bytes across the gap, and keeps streaming. See §C.5.3.

Everything else the proxy does is in service of one of those four.

---

### C.2 Components and files

The proxy is not a single file. It is a subsystem that threads through most of the `resources/lib/` tree. `stream_proxy.py` is now a thin facade: `StreamProxy`, `_StreamHandler`, and `HlsProducer` are composed from mixins in sibling modules, and `stream_proxy.py` re-exports the helpers so `resources.lib.stream_proxy.<name>` stays the single patch surface for tests. Mixins reach module globals at call time via `import resources.lib.stream_proxy as _sp` — keep that pattern when adding code.

| File | Role in the proxy subsystem |
|---|---|
| `stream_proxy.py` | Facade. Defines `_StreamHandler` (a `BaseHTTPRequestHandler` composed from the `stream_proxy_handler_*` mixins), `HlsProducer` (composed from the `stream_proxy_hls_*` mixins), `StreamProxy` (composed from the `stream_proxy_mgr_*` mixins), and the `get_proxy()` singleton. Re-exports constants and helpers from the siblings below. |
| `stream_proxy_mgr_handoff.py` | `prepare_stream`, `_validate_and_classify`, `_build_stream_context`, `_finalize_and_register`. |
| `stream_proxy_mgr_context.py` | Tier selection: `_build_ctx_fallback`, `_build_ctx_mp4`, `_build_ctx_default`, `_decide_force_remux`, `_try_faststart_layout`, and the DV routing gate (`_probe_dv_source`, `_dv_route_allows_fmp4`, `_dv_route_tail`). |
| `stream_proxy_mgr_faststart.py` | ffmpeg temp-file faststart (`_prepare_tempfile_faststart`, `-movflags +faststart` into a `mkstemp` file). |
| `stream_proxy_mgr_probe.py` | `_get_content_length` (hint, HEAD, then tail probe), `_detect_content_type`, `_probe_duration` (ffprobe first, ffmpeg stderr fallback). |
| `stream_proxy_mgr_sessions.py` | `_register_session`, `_register_hls_session`, `_rewrite_ctx_to_matroska`, `_prune_sessions_locked`, `merge_session_fallbacks`. |
| `stream_proxy_mgr_prefetch.py` | Per-session background helpers started at prepare time: byte-0 prefetch, read-ahead daemon, tail prewarm, pass-through settings prefetch, fallback prevalidation. |
| `stream_proxy_mgr_lifecycle.py` | `start` / `stop` / `is_alive`, `clear_sessions`, `cleanup_session_by_id`, ffmpeg capability probe (`_probe_hls_fmp4_capability`). |
| `stream_proxy_handler_dispatch.py` | `do_GET`, `do_HEAD`, `do_POST` (`/prepare` and `/stream/<id>/fallbacks`, both gated on the `X-NZBDAV-Token` header), `_handle_hls`. |
| `stream_proxy_handler_serve.py` | `_serve_mp4_faststart`, `_serve_temp_faststart`, `_serve_remux`, `_build_ffmpeg_cmd`. |
| `stream_proxy_handler_remux.py` | Remux process helpers (`_start_remux_process`, stdout streaming, subtitle/duration args, argv safety checks) and `_parse_hls_resource`. |
| `stream_proxy_handler_proxyserve.py`, `stream_proxy_handler_proxyserve2.py` | `_serve_proxy` and its per-iteration steps (read, cutover, retry ladder, stall wait, zero-fill, finalize), `_retry_original_range`, `_serve_from_readahead`. |
| `stream_proxy_handler_upstream.py` | `_stream_upstream_range` (ranged upstream read + contract checks), the throughput watchdog, `_find_skip_offset`. |
| `stream_proxy_handler_rangeparse.py` | `_parse_range`, skip-probe retries, `_write_zeros`. |
| `stream_proxy_handler_hlsserve.py` | `_serve_hls_playlist`, `_serve_hls_init`, `_serve_hls_segment`. |
| `stream_proxy_handler_cutover.py`, `_standby.py`, `_probe.py`, `_fingerprint.py`, `_rangecache.py` | Live fallback cutover: candidate selection (`_select_live_fallback_source`), activation (`_activate_fallback_source`), standby URL resolution, byte-identity fingerprinting, and the range digest cache. |
| `stream_proxy_hls_ffmpeg.py`, `stream_proxy_hls_segment.py` | `HlsProducer` internals: spawn/respawn, `prepare()`, `close()`, ffmpeg command build; segment completeness, `wait_for_init`, `wait_for_segment`. |
| `stream_proxy_settings.py` | Setting readers (`_get_force_remux_mode`, `_get_force_remux_threshold_bytes`, …) and the settings snapshot (`build_settings_snapshot`, `*_from_snapshot`). |
| `stream_proxy_ffmpeg.py` | ffmpeg/ffprobe discovery, `_ffmpeg_auth_args`, `_choose_hls_workdir`, `_parse_ffmpeg_duration`, deprecated `_embed_auth_in_url`. |
| `stream_proxy_const.py`, `stream_proxy_contract.py`, `stream_proxy_recovery.py`, `stream_proxy_fallback.py`, `stream_proxy_buffer.py`, `stream_proxy_state.py`, `stream_proxy_httpserver.py` | Constants; upstream contract classification and density breaker; recovery/starvation notifications; URL/auth validation and fallback-source normalisation; `ReadAheadBuffer`; per-request pass-through state; `_ThreadedHTTPServer` (handler threads bounded by `_MAX_PROXY_WORKERS`). |
| `stream_proxy_service.py`, `stream_proxy_serviceprops.py` | Plugin-side client: `prepare_stream_via_service`, `update_stream_fallbacks_via_service`, and the `get_service_proxy_port` / `_token` / `_config` window-property readers. |
| `mp4_parser.py` | Pure-Python MP4 box parser used by the virtual-faststart tier. Fetches the box tree with its own ranged `urlopen` helper, locates `ftyp`/`mdat`/`moov` (`fetch_remote_mp4_layout`), rewrites `stco`/`co64` chunk offsets (`rewrite_moov_offsets`, `build_faststart_layout`), and provides `RangeCache`. No ffmpeg dependency. Called from `_MgrContextBuildMixin._try_faststart_layout`. |
| `dv_rpu.py` | Pure-Python Dolby Vision RPU parser (`parse_rpu_payload`). Detects profile 5/7/8 and classifies P7 MEL vs FEL from the NLQ fields. Ports the minimum subset of `quietvoid/dovi_tool` needed for classification. No ffmpeg dependency. |
| `dv_source.py` | Remote container probe (`probe_dolby_vision_source`). Fetches only the bytes needed to locate the first HEVC access unit in an MP4 or MKV, extracts the first UNSPEC62 RPU NAL, and hands it to `dv_rpu` for classification. Returns a structured `DolbyVisionSourceResult` to `_probe_dv_source`. Only consulted on the fMP4 HLS branch. |
| `http_util.py` | Shared HTTP primitives used across the addon: `http_get`, `redact_url` / `redact_text`, `notify`, `HTTP_USER_AGENT`. There is no shared ranged fetcher; `mp4_parser.py` and `dv_source.py` each carry a private `_http_range`. |
| `service.py`, `service_proxy.py` | The background service owns the long-lived `StreamProxy`. It starts the proxy on Kodi startup, publishes `nzbdav.proxy_port` and `nzbdav.proxy_token` on the Home window (`service_proxy._publish_proxy_props`), and rebuilds the proxy if its server thread dies. `NzbdavPlayer` (in `service.py`) is the playback monitor: it retries on `onPlayBackError` and calls `proxy.clear_sessions()` on stop/end. |
| `resolver_prepare.py`, `resolver_resume.py`, `resolver_playback.py` | `_prepare_direct_playback` builds a settings snapshot and content-length hint and calls `prepare_stream_via_service`. `_play_direct` (plugin-handle path, ends in `xbmcplugin.setResolvedUrl`) and `_play_via_proxy` (`resolve_and_play` path, ends in `xbmc.Player().play`) hand the proxy URL to Kodi. Late-adopted fallback releases are pushed into the live session with `update_stream_fallbacks_via_service`. If the service port is missing, the resolver plays the WebDAV URL directly as a last resort. |
| `router.py`, `router_dispatch.py`, `router_directplay.py` | Entry point from Kodi. `/play`, `/search`, and `/direct_play` resolve their own handle; `/resolve` is an action route that calls `resolve_and_play`. Not part of the proxy per se, but every play-path request flows through here before reaching the resolver. |
| `webdav.py` | Produces the remote URL the proxy fetches from, plus a Basic `Authorization` header (`_build_auth_headers`). The proxy forwards that header upstream and passes it to ffmpeg via `-headers`. |
| `resources/settings.xml` | Settings that shape a session (§C.4.1). They are read in the plugin process into a settings snapshot and sent with `/prepare`, so the service thread doesn't call Kodi's `getSetting`. |

Inside `stream_proxy.py` the main classes are:

- **`StreamProxy`** — owns the `_ThreadedHTTPServer` (whose `stream_sessions` dict is keyed by session ID), the `_context_lock`, the per-process `prepare_token`, `prepare_stream`, `_register_session`, `clear_sessions`, and the tier-selection logic that decides which serving path a given `remote_url` will use. One instance per Kodi service.
- **`_StreamHandler`** — per-request handler. Parses the URL to pull out a session ID, looks up the context, and dispatches to `_serve_mp4_faststart` / `_serve_temp_faststart` / `_serve_remux` / `_serve_proxy`, or to `_handle_hls` for `/hls/` paths. Responsible for `Range` header parsing, `206 Partial Content`, `416 Range Not Satisfiable`, and `Connection: close` discipline.
- **`HlsProducer`** — spawns and supervises `ffmpeg` for the experimental fMP4 HLS force-remux branch. Manages a per-session working directory, handles seek-driven respawns, tracks segment generation boundaries with `_spawn_time`, and gates segment completeness checks so Kodi can't read a segment that is still being written.

---

### C.3 Session lifecycle

A single play is one session. The lifecycle below is the same for every tier — only the handler dispatch at step 5 differs.

```text
┌────────────────┐                                          ┌──────────────────┐
│ router.py      │                                          │ service.py       │
│ /play, /resolve│                                          │ StreamProxy      │
└───────┬────────┘                                          └─────────┬────────┘
        │  resolve(handle, …) / resolve_and_play(nzburl, title)       │
        ▼                                                             │
┌───────────────────┐                                                 │
│ resolver_pollloop │                                                 │
│ _poll_until_ready │                                                 │
└────────┬──────────┘                                                 │
         │  remote_url + auth header from webdav.py                   │
         ▼                                                            │
┌────────────────────┐    POST /prepare (X-NZBDAV-Token)              │
│ prepare_stream_via │────────────────────────────────────────────────▶
│ _service           │◀───────────────────────────────────────────────│
└─────────┬──────────┘   (local_url, stream_info)                     │
          │                                                           │
          │                                    (1) clear_sessions()   │
          │                                    (2) content type/size  │
          │                                    (3) tier selection     │
          │                                    (4) start prefetchers, │
          │                                        _register_session  │
          │                                        → session ID       │
          ▼                                                            │
┌─────────────────────┐                                                │
│ setResolvedUrl or   │    local_url = http://127.0.0.1:PORT/...       │
│ Player().play       │                                                │
└─────────┬───────────┘                                                │
          │                                                            │
          ▼                                                            │
┌─────────────────────┐   GET /stream/<id>  or  /hls/<id>/...          │
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

- **Only one session is live at a time.** Kodi plays one stream; `prepare_stream` calls `clear_sessions(wait_for_process=False)` before registering a new session, which kills any lingering ffmpeg or `HlsProducer` and deletes its workdir. This is the guard against zombie ffmpeg surviving an unclean stop. As a backstop, `_prune_sessions_locked` also evicts sessions idle for more than `_SESSION_TTL_SECONDS` (6 h) and caps the table at `_MAX_STREAM_SESSIONS` (8).
- **Session IDs are server-generated.** The session ID in the URL Kodi receives is never user-controllable. `_register_session` stamps `ctx["session_id"] = uuid.uuid4().hex` and every filesystem path inside the HLS workdir is built from that, not from whatever arrives on the wire. This is why path traversal via the HLS URL is not a concern.
- **The context dict is the source of truth.** Each tier's serving path reads everything it needs from `ctx` — `remote_url`, `auth_header`, `content_type`, `content_length` (pass-through) or `total_bytes` (remux), `remux`, `faststart`, `temp_faststart`, `mode`, `duration_seconds`, per-tier extras like `header_data` / `virtual_size` for faststart or `hls_segment_format` / `hls_segment_duration` for HLS, and `fallback_sources` when fallbacks are attached. The handler code is stateless between requests aside from that dict. It is *not* immutable: a live fallback cutover rewrites `remote_url` / `auth_header`, and `merge_session_fallbacks` swaps in a new `fallback_sources` list.
- **`prepare_stream` runs in the service process.** The plugin process uses `prepare_stream_via_service` to cross the process boundary with a loopback HTTP `POST /prepare` to the proxy itself, authenticated by the per-process `prepare_token` (read from the `nzbdav.proxy_token` window property). Fast connection resets are retried up to `_PREPARE_MAX_ATTEMPTS` times with a `waitForAbort` backoff. This keeps the `stream_sessions` table on the same side of the wall as the HTTP server.

---

### C.4 Tier selection

`StreamProxy.prepare_stream(remote_url, auth_header, fallback_sources, content_length_hint, settings_snapshot)` picks exactly one serving path. `_validate_and_classify` treats URLs ending in `.mp4` / `.m4v` as MP4; `_build_stream_context` then walks this tree:

```text
prepare_stream(...)
│
├── fallback_sources attached? ── YES ─▶ pass-through (any container, MP4 included;
│                                        MP4 repair and remux tiers are skipped
│                                        so live cutover stays possible)
│
├── .mp4 / .m4v  (_build_ctx_mp4)
│   │
│   ├── _get_content_length + _try_faststart_layout
│   │     ├── moov at tail, rewrite OK   ─▶ virtual faststart (_serve_mp4_faststart)
│   │     ├── already faststart          ─▶ pass-through (_serve_proxy)
│   │     └── parse failed / stco overflow
│   │           ├── ffmpeg, size known, ≤ 4 GB ─▶ temp-file faststart
│   │           │                                 (_serve_temp_faststart)
│   │           ├── ffmpeg available          ─▶ piped Matroska remux (_serve_remux)
│   │           └── no ffmpeg                 ─▶ pass-through
│   │                                             (error if size unknown)
│
└── everything else: MKV, AVI, TS/M2TS, … (_build_ctx_default)
    │
    ├── _decide_force_remux: mode = passthrough (default), OR threshold = 0,
    │   OR size < threshold                  ─▶ pass-through (_serve_proxy)
    │
    └── mode ∈ {matroska, hls_fmp4} AND (size ≥ threshold OR size unknown)
          ├── no ffmpeg                        ─▶ pass-through + warning
          │                                       (error if size unknown)
          ├── mode = hls_fmp4 AND ffmpeg has the fMP4 HLS muxer flags
          │   AND duration probed AND DV gate allows (§C.5.4.2)
          │                                    ─▶ fMP4 HLS playlist
          └── otherwise                        ─▶ piped Matroska remux
```

#### C.4.1 Settings that influence the decision

All of these live in the **Advanced** settings category (`Proxy` and `Pass-through validation` groups).

| Setting (label) | Default | Where read | Effect |
|---|---|---|---|
| `force_remux_mode` ("Large non-MP4 stream mode") | `0` — Direct pass-through | `_get_force_remux_mode` / `_force_remux_mode_from_snapshot` | `0` → `passthrough` (no force remux), `1` → `hls_fmp4` (experimental fMP4 HLS via `HlsProducer`), `2` → `matroska` (piped MKV with `-c copy`). A one-shot migration (hidden `force_remux_mode_v2_migrated`) rewrites a legacy stored `2` — which used to mean explicit pass-through — to `0`. |
| `force_remux_threshold_mb` ("Force ffmpeg remux above (MB, 0=off)") | `15000` (≈15 GB), clamped to `[0, 2^53−1]` | `_get_force_remux_threshold_bytes` / `_force_remux_threshold_bytes_from_snapshot` | Only matters when `force_remux_mode` selects a remux tier. Non-MP4 files at or above this size (or of unknown size) take the force-remux branch. `0` disables force remux entirely. The default sits below the lowest known-bad size on 32-bit Kodi (15.8 GB crashed; 12 GB passed). |
| `proxy_convert_subs` ("Convert MP4 subtitles to SRT") | `true` | `_append_subtitle_args` | Matroska remux only. When on, MKV sources map subtitles with `-c:s copy` (PGS/HDMV/DVD bitmap subs can't be converted) and other sources convert text subs to `srt`. When off, subtitle streams are left out of the remux. The fMP4 HLS branch always drops subtitles (`-sn`). |
| `readahead_buffer_mb` | `256`, clamped to `[0, 4096]` | `_get_readahead_buffer_mb` | Size of the pass-through read-ahead window (§C.5.3.1). `0` disables it. |
| `passthrough_stall_wait` | `120` s, clamped to `[0, 600]` | `_get_passthrough_stall_wait_seconds` | Patient forward-stall budget for an established pass-through stream (§C.5.3). `0` closes immediately. |

The reliability/contract flags — `strict_contract_mode` (default `1` = warn), `density_breaker_enabled` (`false`), `retry_ladder_enabled` (`true`), `zero_fill_budget_enabled` (`true`), `send_200_no_range` (`false`) — influence pass-through behavior rather than tier selection. They are resolved once per session into the pass-through runtime settings (`_passthrough_runtime_settings`). The density breaker is forced off when `strict_contract_mode` is off.

---

### C.5 The serving paths

#### C.5.1 Already-faststart MP4: pass-through (no direct-redirect tier)

Earlier releases returned the remote WebDAV URL for MP4s whose `moov` was already in front. That tier is gone: since v1.2.3, `prepare_stream` always returns a local proxy URL (`stream_info["direct"]` is always `False`). An already-faststart MP4 gets a plain pass-through context (§C.5.3), so it keeps `PROPFIND` protection, zero-fill recovery, and live fallback cutover. The only non-proxy play left is the resolver's last resort when the service port isn't published.

#### C.5.2 Virtual MP4 faststart (`_serve_mp4_faststart`)

When the moov is at the tail, `mp4_parser.py` reads the atom tree over ranged HTTP fetches:

1. Walk the top-level box list looking for `ftyp`, `mdat`, `moov` (with a tail probe if `moov` isn't found after `mdat`).
2. If `moov` is before `mdat`, short-circuit (already faststart → pass-through, §C.5.1).
3. Otherwise fetch the moov and rewrite all `stco` (32-bit) and `co64` (64-bit) chunk-offset tables by the moov's size, since everything after `ftyp` shifts right by that much.
4. Compose a virtual "header" blob: `ftyp` + rewritten `moov`.
5. Compute a virtual file size: `len(header) + payload_size`, where the payload is the original bytes from the end of `ftyp` to the start of the original `moov`.

`_serve_mp4_faststart` then serves ranged responses against this virtual layout:

- Ranges within the header range return bytes directly from the in-memory `header_data`.
- Ranges within the payload range translate to an upstream ranged fetch against the real file (`payload_remote_start + (virtual_offset - header_len)`), with a per-session `RangeCache`.
- Ranges that straddle the boundary serve the header portion from memory and the payload portion from upstream in a single response.

This tier produces a valid MP4 byte stream that Kodi can seek natively — no ffmpeg, no remux, no extra CPU. The parser is pure Python (no dependencies).

If layout parsing fails — unusual box structure, or a 32-bit `stco` table that would overflow after the shift — `_build_ctx_mp4_tempfile_or_remux` takes over. With ffmpeg available and a known size of at most 4 GB, it runs `ffmpeg -c copy -movflags +faststart` into a `mkstemp` temp file and serves that file with range support (`_serve_temp_faststart`). Larger files skip the temp-file step because it would outlast the 60 s `/prepare` budget. Those, and any failed temp remux, fall to the piped Matroska remux (§C.5.4.1). Without ffmpeg the MP4 is served as plain pass-through.

#### C.5.3 Pass-through with recovery (`_serve_proxy`)

For MKV, AVI, TS/M2TS, and other non-MP4 containers (unless force remux applies), for already-faststart MP4s, and for every fallback-enabled session, the proxy forwards Kodi's `Range` request to the upstream WebDAV URL and streams the response back byte-for-byte.

The subtlety is error recovery. On real Usenet sources, a small fraction of requested byte ranges hit articles that nzbdav cannot reconstruct, or regions nzbdav hasn't downloaded yet. `_stream_upstream_range` classifies each read (`OK`, `SHORT_READ_RECOVERABLE`, `SHORT_READ_AWAITING_DOWNLOAD`, `UPSTREAM_ERROR`, `CLIENT_ERROR`, `PROTOCOL_MISMATCH`), and `_serve_proxy_loop_body` runs ordered steps on each iteration:

1. **Read** the next chunk of the range from the active source.
2. **Cutover** — on a recoverable short read or upstream error, switch to a validated fallback source when one is attached (§C.5.3.2).
3. **Retry ladder** (`retry_ladder_enabled`) — `_retry_original_range` re-requests the unread range with backoff (a short schedule for a first read, a longer one mid-stream).
4. **Awaiting-download failover** — after `_AWAITING_DOWNLOAD_NO_PROGRESS_MAX` no-progress passes on a still-downloading region, fail over to a fallback.
5. **Patient stall wait** — an established stream that stalls on a recoverable backend condition holds the client connection open and re-reads with abortable backoff, up to `passthrough_stall_wait`.
6. **Exhaustion / abort** — close cleanly with `fallback_exhausted` after `_FALLBACK_PENDING_FALLTHROUGH_MAX` fruitless fall-throughs, or abort on a terminal client/protocol error.
7. **Zero-fill** — `_find_skip_offset` probes forward in 1 / 4 / 16 MB steps (`_SKIP_PROBE_SIZES`), retrying each probe with backoff within a 30 s budget (`_MAX_RECOVERY_SECONDS`), then writes zeros across the gap from `_ZERO_FILL_BUFFER` and resumes. The per-response cap is `_MAX_TOTAL_ZERO_FILL = 64 MB`, and a per-session ratio cap is `_SESSION_ZERO_FILL_RATIO_MAX` (5%), both gated on `zero_fill_budget_enabled`. The opt-in density breaker aborts when recent output is mostly synthetic. The skip probe short-circuits when the session already knows upstream is down.

Kodi sees a continuous byte stream with a few silent frames instead of a fatal decoder error. The final `terminal_reason` (`complete`, `client_disconnected`, `recovery_exhausted`, `session_zero_fill_budget_exceeded`, `fallback_exhausted`, …) is logged by `_serve_proxy_finalize`.

Pass-through also handles:

- **`Range` parsing.** Standard `bytes=A-B` / `bytes=A-` / `bytes=-N` suffix ranges, `416` on out-of-bounds. A request without `Range` gets a full-length `206` unless `send_200_no_range` is on.
- **64 KB upstream read chunks** (`_UPSTREAM_READ_CHUNK`). Small because on 32-bit Kodi the address space is ~3 GB and a second concurrent handler thread opened during Kodi's `CCurlFile` reconnect-on-error recovery has been observed to hit `MemoryError` with 1 MB buffers.
- **`Connection: close` on every response.** Forces stale handler threads to unwind when Kodi reconnects instead of piling up on a keep-alive socket. A 60 s socket write timeout (`_REMUX_WRITE_TIMEOUT`) bounds a handler stuck writing to a stalled Kodi.
- **Throughput watchdog.** For video content, a response that trickles below 100 KB/s over a 20 s window (`_PASSTHROUGH_MIN_THROUGHPUT_BPS`, `_PASSTHROUGH_THROUGHPUT_WINDOW_SECONDS`) is closed so Kodi reconnects with a fresh upstream fetch.
- **`Content-Type` from the file suffix** (`_detect_content_type`). `video/x-matroska` for `.mkv`, `video/x-msvideo` for `.avi`, `video/mp2t` for `.ts` / `.m2ts`, and `video/mp4` for everything else.

##### C.5.3.1 Prefetch helpers

`_finalize_and_register` starts these daemon threads for pass-through contexts only (`_initial_range_prefetchable` excludes faststart, temp-faststart, remux, and HLS sessions):

- **Byte-0 prefetch** — fetches the first 64 KB during the prepare/handoff gap. `_serve_proxy` serves it as a cached prefix on Kodi's first range request.
- **Tail prewarm** — after a 1.5 s defer, reads the last 1 MB (`_TAIL_PREWARM_BYTES`) and discards it, so nzbdav already has the MKV cues articles when Kodi asks.
- **Read-ahead** — a `ReadAheadBuffer` of `readahead_buffer_mb` filled by `_run_readahead_prefetch` ahead of the highest served offset (after a 1.5 s start defer). It keeps filling while Kodi is paused, throttles when full, frees bytes behind the play head, discards on a seek outside the window, and follows a live cutover to the new source. `_serve_from_readahead` serves hits before the upstream read.
- **Settings prefetch** — resolves the pass-through runtime settings once, off the request path.

##### C.5.3.2 Live fallback cutover

When the resolver submits duplicate releases (`fallback_streams_enabled`), their metadata rides into the session as `ctx["fallback_sources"]`, either at `/prepare` time or later through `POST /stream/<id>/fallbacks` (`merge_session_fallbacks`, deduped by `(nzo_id, stream_url)`). The hooks are:

- **Prevalidation** — `_start_fallback_prevalidation` resolves standby URLs and fingerprints them in the background (at prepare and after every merge), so a later cutover doesn't cold-resolve under Kodi's timeout.
- **Selection** — `_select_live_fallback_source` returns only a source whose content length and sampled byte ranges match the primary (`_classify_fallback_fingerprint`). An inconclusive probe is a non-match.
- **Activation** — `_activate_fallback_source` demotes the current source to a last-resort fallback, swaps `ctx["remote_url"]` / `["auth_header"]`, resets the watchdog and awaiting-download counters, and bumps `fallback_switch_count`. The same Kodi response keeps streaming from the new source at the same byte offset.

User-facing behavior is in [`how-it-works/fallback-cutover.md`](../docs-site/how-it-works/fallback-cutover.md).

#### C.5.4 Force remux

Used for non-MP4 files when `force_remux_mode` selects a remux tier and the file is at or above `force_remux_threshold_mb` (or its size is unknown). The piped Matroska shape is also the MP4 rescue when faststart fails (§C.5.2). Two shapes:

##### C.5.4.1 Matroska pipe

`_serve_remux` spawns `ffmpeg -i <upstream> -map 0:v:0 -map 0:a -c:v copy -c:a copy [subtitle args] -metadata DURATION=… -f matroska pipe:1` and streams stdout to Kodi as `200 OK` with `Accept-Ranges: none` and no `Content-Length`. Because the response size is unknown, Kodi treats the stream as sequential — which sidesteps the 32-bit `CFileCache` offset overflow — but seeking is limited to what Kodi has cached. Piped MKV has no Cues, and the proxy deliberately does **not** translate a byte `Range` into an ffmpeg `-ss` time (`_resolve_seek` never derives one), so a later range request just restarts the pipe. A second GET that arrives while a live ffmpeg is still streaming loses the compare-and-swap in `_start_remux_process` and gets `409`.

Key properties:

- **`-c copy` for video and audio.** No re-encode. DV HEVC metadata, TrueHD / Atmos, DTS-HD MA all pass through untouched. Only the first video stream is mapped.
- **`-metadata DURATION=`** emitted into the MKV Segment Info so Kodi knows the total length — without this, piped MKV would look like a live stream and Kodi would hide the progress bar and disable seeking entirely.
- **`proxy_convert_subs`** controls subtitle mapping (§C.4.1).
- **Timeouts.** A 60 s socket write timeout (`_REMUX_WRITE_TIMEOUT`) bounds zombie lifetime if Kodi's decoder stalls without closing the socket, and a 30 s ffmpeg stdout idle guard (`_REMUX_STDOUT_IDLE_TIMEOUT`) catches a wedged ffmpeg.
- **Auth via `-headers`.** Every ffmpeg spawn passes the `Authorization` header through `_ffmpeg_auth_args`, so the input URL stays credential-free.

This is the known-good force-remux path on the target device. Dolby Vision HEVC + TrueHD/Atmos 100 GB REMUXes play through this branch.

##### C.5.4.2 Fragmented MP4 HLS (experimental, opt-in)

`force_remux_mode=1` (`hls_fmp4`) switches to an HLS VOD playlist backed by fragmented MP4 segments. The motivation is full random seek (the Matroska pipe can only seek within Kodi's cache). The tradeoff is that fMP4 HLS on the Amlogic hardware decoder has not been proven stable for all content types. The branch is taken only when ffmpeg advertises `-hls_segment_type` and `-hls_fmp4_init_filename` (`_probe_hls_fmp4_capability`), the duration probe succeeds, and the DV gate below allows it. Otherwise the session falls back to the Matroska pipe.

Pipeline:

```text
prepare_stream
  └── ctx with content_type = application/vnd.apple.mpegurl,
             mode = "hls",
             hls_segment_format = "fmp4",
             hls_segment_duration = 6.0   (_HLS_SEGMENT_SECONDS)
  └── _register_session → _register_hls_session
        └── HlsProducer(ctx, _choose_hls_workdir(total_bytes))
              ├── makedirs(session_dir), open ffmpeg.log
              └── prepare()          ← eager spawn-time validation
                    ├── 500 ms window: ffmpeg exits early with rc != 0?
                    └── up to 30 s: init.mp4 + seg_000000.m4s on disk?
                          └── on failure: _register_hls_session closes the
                              producer and _rewrite_ctx_to_matroska rewrites
                              ctx in place BEFORE returning the URL
                              (late-binding fallback)
```

###### C.5.4.2.a HTTP routes

HTTP routes exposed for an HLS session:

| Route | Handler | Purpose |
|---|---|---|
| `GET /hls/<session>/playlist.m3u8` | `_serve_hls_playlist` | Serves ffmpeg's own `ffmpeg_playlist.m3u8` with segment names normalised to `seg_N.m4s` (`generated_playlist_body`). Before ffmpeg has written one, it synthesises a VOD playlist (`_build_hls_playlist_body`) with `#EXT-X-VERSION:7`, `#EXT-X-MAP:URI="init.mp4"`, one `#EXTINF` / `seg_N.m4s` pair per segment, and `#EXT-X-ENDLIST`. |
| `GET /hls/<session>/init.mp4` | `_serve_hls_init` | Waits on `HlsProducer.wait_for_init()`, then serves the **canonical** init bytes cached from the first generation (`_canonical_init_bytes`) with `Content-Type: video/mp4`. Returns `504` on timeout. |
| `GET /hls/<session>/seg_N.m4s` | `_serve_hls_segment` | Waits on `HlsProducer.wait_for_segment(N)`, which blocks until the segment is complete (respawning ffmpeg if needed), then serves the bytes. Zero-padded names are accepted too. A segment whose extension doesn't match the session format gets `404`. |

###### C.5.4.2.b ffmpeg lifecycle (`HlsProducer`)

`HlsProducer` manages the ffmpeg lifecycle:

- **One ffmpeg process, respawned on seeks.** `_ensure_ffmpeg_headed_for(seg_n)` asks `_needs_ffmpeg_restart(seg_n)` whether the live process will eventually produce `seg_n`. It restarts when the process is dead, on a backward seek (`seg_n < start_segment`), or when `seg_n` is more than `_HLS_FORWARD_WAIT_SEGMENTS` (2) segments ahead of the start segment. The new process starts with `-ss (seg_n * segment_seconds)` and `-start_number seg_n`.
- **`-copyts` + `-ss T` before `-i`.** Together these make the new ffmpeg's first-frame PTS equal to `T`, which matches Kodi's EXTINF-based global time at `seg_T/segment_seconds`. Critical for seek-respawn continuity — the alternative (`-reset_timestamps 1`) was tried and caused Amlogic decoder stalls with "messy timestamps" errors. The current code does NOT add `-output_ts_offset`; with `-copyts + -ss T` the offset is already correct and adding another would double it. `-start_at_zero` and `-avoid_negative_ts make_zero` keep respawned output on a deterministic timeline.
- **Generation boundaries.** Every respawn unlinks only the new target `seg_<N>.m4s` before `Popen`, resets `_init_ready`, and stamps `self._spawn_time = time.time()`. `init.mp4` is not unlinked: Kodi loads `EXT-X-MAP` once, so the proxy keeps serving the first generation's canonical init bytes even though each respawn rewrites the on-disk `init.mp4` with a different edit list. `_segment_complete(n)` for fMP4 sessions only trusts a `seg_<n+1>.m4s` created after `_spawn_time`, and requires `seg_<n>` itself to be from the current generation — otherwise a stale segment from a prior generation could be served or falsely mark a half-written one complete. Prior-generation segments at other indices stay on disk.
- **fMP4 init gate.** `wait_for_segment` checks `_init_file_complete` (via `_wait_for_segment_init_gate`) before returning any segment — `init.mp4` must be on disk and ffmpeg must have moved on to segment output before Kodi sees its first segment.
- **Session stderr log.** Each session opens `ffmpeg.log` in its workdir at construction time and reuses it across every respawn. This fixes a latent `stderr=PIPE` deadlock from the earlier persistent-producer era. `close()` archives the log to a rolling location (`_archive_ffmpeg_log`) before the workdir is removed.
- **`-tag:v hvc1`.** Forces the HLS-spec sample entry tag on HEVC video. HLS fMP4 mandates `hvc1` (parameter sets in the sample description box, not inband) and Amlogic's HLS demuxer uses this tag to locate the `dvcC`/`dvvC` DV configuration record in the init segment. Without the tag, `hev1`-sourced HEVC copied into fMP4 can hide the DV config from the hardware decoder.
- **`-strict -2` and `-sn`.** `-strict -2` lets the fMP4 muxer accept TrueHD and DTS-HD MA; `-sn` drops subtitle streams.
- **DV source-RPU classifier.** Before committing to the fMP4 branch, `_dv_route_allows_fmp4` calls `_probe_dv_source` → `probe_dolby_vision_source` (in `dv_source.py`), which fetches only the bytes needed to locate the first HEVC access unit in the source (moov walk for MP4, EBML Segment → Tracks + Cluster → SimpleBlock for MKV), extracts the first UNSPEC62 RPU NAL, and hands it to `dv_rpu` for classification. It returns a `DolbyVisionSourceResult` with fields `classification` ∈ {`dv_profile_7_fel`, `dv_allowed_for_fmp4`, `non_dv`, `dv_unknown`}, `reason`, `profile`, `el_type`. The routing matrix is:
  - **P7 FEL** → Matroska. fMP4 cannot carry the dual-layer BL+EL structure; dropping the EL silently would stall the Amlogic decoder.
  - **P7 MEL** → fMP4. MEL is ~2 Mbps of NLQ metadata (mapping coefficients), not a second HEVC layer — so it doesn't exercise the CAMLCodec dual-layer init path that tripped P8 on 2026-04-15. Experimental; tighten to P7-unconditional-Matroska if field testing shows MEL also hangs.
  - **P8 / P5 / any other confirmed DV** → Matroska. The 2026-04-15 Evangelion P8 test proved the Amlogic fMP4 DV path hangs at `onAVStarted` regardless of single-layer vs dual-layer.
  - **non-DV** → fMP4 (the happy path — the probe confirmed no UNSPEC62 NAL in the first sample).
  - **dv_unknown** (probe crash, unsupported container, truncated RPU) → Matroska. Fail safe.

  The classifier runs on the service worker thread, so a 2–3-range HTTP probe typically adds <1 s to `prepare_stream` (a net **improvement** over the retired ffmpeg-stderr probe, which needed 5–10 s to spawn + analyse). A probe crash is caught in `_probe_dv_source` and degrades to `dv_unknown` → Matroska.
- **Late-binding Matroska fallback.** `HlsProducer.prepare()` spawns ffmpeg immediately, then runs two windows: a 500 ms argument-rejection window (early exit with a non-zero rc) and a production window of up to `_PREPARE_PRODUCTION_TIMEOUT_SECONDS` (30 s) that waits for `init.mp4` and `seg_000000.m4s`. If either fails, `_register_hls_session` catches the exception, calls `producer.close()` (best effort), and `_rewrite_ctx_to_matroska` rewrites `ctx` in place — all before returning the URL to Kodi. This guarantees that a bad ffmpeg build or a source that never produces output never hands Kodi a dead HLS URL.

###### C.5.4.2.c Working directory selection

`_choose_hls_workdir(total_bytes)` walks `_HLS_WORKDIR_CANDIDATES` (`/var/media/CACHE_DRIVE/nzbdav-hls`, `/var/media/STORAGE/nzbdav-hls`, `/storage/nzbdav-hls`) and picks the first entry whose parent exists and is writable and that has at least `total_bytes` free. If none qualifies it falls back to a private `mkdtemp()` root (`_get_private_hls_temp_root`), and raises if that lacks the space too. Each session gets its own subdirectory, which is `rm -rf`'d on session cleanup.

---

### C.6 Known constraints and gotchas

- **Kodi is single-stream.** The proxy does not need to multiplex sessions. `clear_sessions()` on every new `prepare_stream` is correct, not a limitation.
- **32-bit CoreELEC is the design target.** Several defaults (64 KB read chunks, `Connection: close`, the 15 GB force-remux threshold when a remux mode is chosen) exist specifically because of the 32-bit address-space and `CFileCache` overflow behavior. 64-bit Kodi users could relax these, but the code is tuned for the worst case.
- **`session_id` is not user-controllable for file paths.** Every filesystem path uses `ctx["session_id"]` (a server-generated `uuid4().hex`), not the ID fragment from the URL. URL-supplied IDs are only used to look up sessions in the in-memory dict. Don't undo this when refactoring.
- **fMP4 HLS is still an opt-in experiment.** `force_remux_mode` defaults to pass-through, and when users opt into remux the known-good choice is Matroska. Commit `50a6eb3` documents the Amlogic DV HEVC stall on the fMP4 path. The DV routing gate and the `hvc1` tag are mitigations; deeper diagnosis is still open.
- **No file-level Content-Length on remux branches.** The Matroska pipe omits `Content-Length`, and HLS fMP4 advertises it only per segment. This is how they sidestep the 32-bit offset overflow. Do not add a file-level `Content-Length` to either.
- **`-c copy` means format flaws survive.** The proxy preserves DV RPU SEIs, TrueHD/Atmos substreams, and subtitle codecs because it never re-encodes on the playback path. If the source has a broken container, the proxy cannot fix it.
- **ffmpeg auth in argv.** Every ffmpeg/ffprobe spawn passes credentials with `-headers "Authorization: Basic …"` (`_ffmpeg_auth_args`), so URLs and `ffmpeg.log` stay clean. The base64 header line is still in argv and readable via `/proc/<pid>/cmdline` for the lifetime of the process. `_embed_auth_in_url` (user:password in the URL) is deprecated and no spawn uses it.

---

### C.7 Adding a new tier

If you want to add another serving path:

1. Pick a branch in `_build_stream_context` / `_build_ctx_mp4` / `_build_ctx_default` (`stream_proxy_mgr_context.py`, `stream_proxy_mgr_handoff.py`). The decision tree is sequential — put your case above anything it would override, and remember that fallback-enabled sessions short-circuit to pass-through first.
2. Decide what goes in `ctx`. Define the minimum set of keys your handler will read. If your tier is not byte pass-through, make sure `_initial_range_prefetchable` excludes it so the prefetch daemons don't start.
3. Add a `_serve_<name>` method on a `stream_proxy_handler_*` mixin (reaching module globals through `_sp.<name>`) and compose it into `_StreamHandler`. It must:
   - Handle `Range` via `_parse_range` (if applicable).
   - Set `Connection: close` and `self.close_connection = True` on every response path.
   - Return `416` on out-of-bounds ranges, `404` on missing context, `500` on upstream exceptions.
   - Log at `xbmc.LOGINFO` or `xbmc.LOGDEBUG`, never `print`.
4. Wire dispatch in `do_GET` / `do_HEAD` (`stream_proxy_handler_dispatch.py`). If your URL shape doesn't fit `/stream/<id>`, add a new prefix in `_parse_hls_resource` style and return it from `_register_session`.
5. Add tests in `tests/test_stream_proxy.py` — there's a pattern for `StreamProxy.__new__(StreamProxy)` + mock `_server` + patch `_get_content_length` + patch `Popen` that covers the tier-selection branch without needing a real Kodi process. Read-ahead has its own file, `tests/test_stream_proxy_readahead.py`, and cutover edge cases live in `tests/test_cutover_edge_cases.py` and `tests/test_fallback_streams.py`.
6. If your tier spawns subprocesses, use the `HlsProducer` pattern: eager `prepare()` for fail-fast validation, session-wide stderr log to avoid PIPE deadlock, `close()` in the session teardown path, and `_ffmpeg_auth_args` for credentials.

---

### C.8 Where to look when debugging

| Symptom | First place to look |
|---|---|
| Playback never starts | `kodi.log` around the `prepare_stream` call — look for `Proxy ready (remux=…, faststart=…)`. Which tier was chosen? Is the URL returned correct? Was `setResolvedUrl` (plugin path) or `Player().play` (RunScript path) called? A `background service unreachable` error means `/prepare` failed. |
| Playback starts then stalls | The session's `ffmpeg.log` (HLS) or the proxy's `_serve_remux` / `_serve_proxy` log lines. Look for upstream `HTTPError`, `MemoryError`, socket timeouts, `reason=patient_forward_stall`, or `passthrough_stall`. |
| `Open - Unhandled exception` on a huge file | 32-bit `CFileCache` overflow on pass-through. Set `<cache><memorysize>0</memorysize></cache>` (see [`reference/advancedsettings.md`](../docs-site/reference/advancedsettings.md)), or pick a remux mode in `force_remux_mode` with a `force_remux_threshold_mb` low enough to catch the file. Check that `_get_content_length` returns the real size. |
| Black screen with silent audio mid-stream | Zero-fill recovery fired — grep for `reason=zero_fill_resume`. If the stream then ended, the `terminal_reason` (`recovery_exhausted`, `session_zero_fill_budget_exceeded`, `density_breaker_tripped`) says which budget ran out. |
| Fallback-enabled stream closes instead of switching | Grep for `reason=fallback_pending_retry_primary` and `fallback_exhausted`: no fallback source had been validated yet. Check the `prevalidation pending sources=` and `Prevalidated N fallback stream(s)` lines. |
| HLS session hangs at "buffering" | `wait_for_segment` or `wait_for_init` timing out (`HLS init wait timed out`). Check `ffmpeg.log` for the session — is ffmpeg alive? Is it producing segments? Is `_init_ready` ever getting set? |
| Seeking on the Matroska pipe only works near the play head | Expected — the pipe has no Cues and no `-ss` respawn, so seeks are bounded by Kodi's cache. Use pass-through with the `advancedsettings.xml` cache fix, or fMP4 HLS, for full seeking. A lingering ffmpeg from a previous session should have been killed by `clear_sessions`; if not, that's a lifecycle bug. |
| DV HEVC stalls on `hls_fmp4` | P7 FEL, P5, P8, and unknown DV are already routed to Matroska (`dv_route=` log lines), so a stall on fMP4 means non-DV or P7 MEL content. Switch `force_remux_mode` to Matroska or back to pass-through. |
| Every scrub past 4 GB returns `streamed=0` | 32-bit `CFileCache` seek-delta truncation (`FileCache.cpp:375`). Fix: `<cache><memorysize>0</memorysize></cache>` in `advancedsettings.xml`. See [`reference/advancedsettings.md`](../docs-site/reference/advancedsettings.md). |

---

### C.9 Related documentation

- [`docs-site/reference/advancedsettings.md`](../docs-site/reference/advancedsettings.md) — why the 32-bit `CFileCache` overflow happens and the `memorysize` fix.
- [`docs-site/operations/coreelec-tuning.md`](../docs-site/operations/coreelec-tuning.md) — optional CoreELEC/Linux tuning for 4K DV playback on low-memory ARM boxes.
- [`docs-site/how-it-works/stream-proxy.md`](../docs-site/how-it-works/stream-proxy.md) and [`docs-site/how-it-works/fallback-cutover.md`](../docs-site/how-it-works/fallback-cutover.md) — user-level descriptions of the proxy and live fallback switching.
- `docs/pannal-xbmc-dv-hls-issue.md` — upstream issue write-up for the Amlogic fMP4 HLS Dolby Vision zero-frame stall.
- `CHANGELOG.md` and `repo/plugin.video.nzbdav/changelog.txt` — release notes including every proxy behavior change.
- `AGENTS.md` — project layout, test commands, release workflow, Python 3.8 compatibility constraint.

---
