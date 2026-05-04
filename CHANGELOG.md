# Changelog

All notable changes to the **NZB-DAV Kodi addon** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

[![Latest Release](https://img.shields.io/github/v/release/xbmc4lyfe/nzbdavkodi?label=latest&color=FF570A)](https://github.com/xbmc4lyfe/nzbdavkodi/releases/latest)

---

## Quick navigation

| Version | Released | What it's about |
|---|---|---|
| **[1.0.5](#105--2026-05-04)** | 2026-05-04 | Direct Newznab indexers, manual Indexers settings, concurrent provider fan-out, Kodi repo publishing fixes, WebDAV range compatibility, cache eviction race fix |
| **[1.0.4](#104--2026-04-25)** | 2026-04-25 | Pass-through stall watchdog (closes the slow-trickle wedge where seek doesn't unstick), proxy seek perf, sha256 cache keys, credential redaction sweep, Prowlarr UI label fix, §H.2 audit closure batch |
| **[1.0.3](#103--2026-04-23)** | 2026-04-23 | Hotfix: ffmpeg safety check no longer rejects -headers values with legitimate CR/LF — unblocks every auth'd force-remux stream that regressed in v1.0.0-pre-alpha/v1.0.1/v1.0.2 |
| **[1.0.2](#102--2026-04-23)** | 2026-04-23 | Hotfix: find_video_file no longer rejects cross-origin PROPFIND hrefs — unblocks reverse-proxied nzbdav setups that regressed in v1.0.0-pre-alpha / v1.0.1 |
| **[1.0.1](#101--2026-04-23)** | 2026-04-23 | Source-data Dolby Vision probe: pure-Python RPU parser replaces the ffmpeg-stderr probe, adds P7 MEL/FEL discrimination with a hybrid routing matrix that keeps the 2026-04-15 P8 matroska fix in place |
| **[1.0.0-pre-alpha](#100-pre-alpha--2026-04-15)** | 2026-04-15 | Force-remux for 20 GB+ files (matroska default), self-healing fmp4 HLS opt-in (full random seek, DV-aware), threaded submit + queue adoption, real-ffmpeg integration tests, PROXY.md |
| **[0.6.21](#0621--2026-04-13)** | 2026-04-13 | Stale-job cleanup + real nzbdav error messages on submit failure |
| **[0.6.20](#0620--2026-04-13)** | 2026-04-13 | Resolve-loop: no UI freeze, no silent retry on bad WebDAV creds |
| **[0.6.19](#0619--2026-04-12)** | 2026-04-12 | README refresh + pylint CI fix |
| **[0.6.18](#0618--2026-04-12)** | 2026-04-12 | Big MKVs play, seek, and self-heal. **Recommended upgrade.** |
| [0.6.17](#0617--2026-04-12) | 2026-04-12 | Kill zombie ffmpeg after a stall |
| [0.6.16](#0616--2026-04-12) | 2026-04-12 | First attempt at fixing huge MKVs (superseded by 0.6.18) |
| **[0.6.15](#0615--2026-04-12)** | 2026-04-12 | Stream survives missing Usenet articles |
| **[0.6.14](#0614--2026-04-12)** | 2026-04-12 | Route every file through the local proxy |
| [0.6.13](#0613--2026-04-12) | 2026-04-12 | Configurable NZB submit timeout |
| [0.6.12](#0612--2026-04-12) | 2026-04-12 | Fix replaying the same NZB |
| [0.6.11](#0611--2026-04-12) | 2026-04-12 | Full modal error dialogs |
| [0.6.10](#0610--2026-04-12) | 2026-04-12 | Modal dialogs on every playback failure |
| [0.6.9](#069--2026-04-12) | 2026-04-12 | Deduplicate NZBs across indexers |
| [0.6.8](#068--2026-04-12) | 2026-04-12 | Auth header fix, modal download errors |
| [0.6.7](#067--2026-04-10) | 2026-04-10 | Pylint hygiene |
| [0.6.6](#066--2026-04-10) | 2026-04-10 | Redact secrets from logs |
| [0.6.5](#065--2026-04-10) | 2026-04-10 | Surface failures to the user |
| [0.6.2](#062--2026-04-10) | 2026-04-10 | Flaky test fix |
| [0.6.1](#061--2026-04-10) | 2026-04-10 | Narrow exception handlers |
| **[0.6.0](#060--2026-04-07)** | 2026-04-07 | Native MP4 seeking via pure-Python moov proxy |
| [0.5.0](#050--2026-04-06) | 2026-04-06 | Release-group dialogs, settings simplification |
| **[0.4.3](#043--2026-04-06)** | 2026-04-06 | Stream proxy + MP4→MKV remux (first cut) |
| [0.4.1](#041--2026-04-06) | 2026-04-06 | Security review fixes |
| [0.4.0](#040--2026-04-06) | 2026-04-06 | Lightning-bolt "already downloaded" indicator |
| [0.3.0](#030--2026-04-05) | 2026-04-05 | CoreELEC streaming, Test Connection buttons |
| [0.2.0](#020--2026-04-05) | 2026-04-05 | Kodi localization |
| **[0.1.0](#010--2026-04-05)** | 2026-04-05 | Initial release |

> **Bolded** versions are either major features or recommended upgrades.

---

## [1.0.5] — 2026-05-04

> **Direct Newznab indexers and repository update hardening.** Users who do
> not run NZBHydra2 or Prowlarr can now configure Newznab-compatible
> indexers directly in Kodi. This release also fixes the GitHub Pages
> repository publishing path so Kodi clients see the latest release zip in
> `addons.xml`, and rolls in several proxy/cache correctness fixes from the
> post-1.0.4 hardening pass.

**Added**
- **Direct Newznab indexer support** in
  `plugin.video.nzbdav/resources/lib/direct_indexers.py`. The addon can now
  search Newznab-compatible indexers directly and hand the selected NZB URL
  to the existing nzbdav submit/poll/playback flow.
- **New Settings -> Indexers section** for manual setup when NZBHydra2 or
  Prowlarr is not available. Includes curated presets for NZBGeek,
  NZBFinder, DrunkenSlug, NZBPlanet, DOGnzb, and NZB.life / NZB.su, plus
  custom Newznab-compatible slots with name, URL, and API key fields.
- **Test Direct Indexers action** in settings. It probes each configured
  provider's Newznab caps endpoint and reports partial failures while still
  counting working indexers.

**Changed**
- **Search fan-out now includes direct indexers** alongside NZBHydra2 and
  Prowlarr. Results are merged into the existing picker and deduped by NZB
  link before playback.
- **Direct indexer search and caps checks run concurrently** with a shared
  wall-clock deadline and capped worker count, so one slow indexer no longer
  blocks Kodi while every provider is checked serially.
- **Kodi repository publishing path hardened.** Pages metadata is generated
  from the latest release zip, keeps the release zip filename intact, writes
  Kodi-compatible CRLF `addons.xml.md5`, and keeps the generated repository
  index slim enough for Kodi clients to refresh reliably.
- **CI parity tightened** by adding pylint to `just lint` and keeping the
  automatic Claude review workflow advisory when external Claude auth is not
  available.

**Fixed**
- **WebDAV range compatibility:** accept standards-compliant HTTP 200
  full-object responses for full-object range probes per RFC 9110 instead of
  treating them as hard protocol mismatches.
- **Cache eviction race:** prune cache entries using one stat snapshot per
  file, closing the TOCTOU window where a file could disappear between size
  accounting and deletion.
- **Direct indexer XML parsing:** remove fragile `parser.parser` internal
  handler access and rely on Python 3.8+'s default external-entity behavior.

---

## [1.0.4] — 2026-04-25

> **Pass-through stall watchdog closes the playback wedge.** Live triage on
> the CoreELEC test box on 2026-04-25 produced a complete repro: a 4K HDR
> HEVC MKV (6.88 GB) played fine for 14 minutes via the pass-through proxy,
> then the upstream WebDAV started delivering tiny chunks every ~25 s. Each
> chunk arrived under the 30 s per-read socket timeout, so neither
> `_UPSTREAM_OPEN_TIMEOUT` nor Kodi's own watchdog ever fired — bytes
> dripped in below playable rate, `CVideoPlayerAudio::Process - stream
> stalled` logged 33 s later, and a user-issued seek-back-1min was logged
> in `service.py` but **no follow-up Pass-through entry** ever appeared in
> kodi.log: Kodi's CFileCache had marked the source as still "open" and
> never issued a fresh range request. The fix samples bytes-per-second
> over a 20 s rolling window and closes the response when rate drops below
> 100 KB/s — Kodi's CCurlFile sees EOF and reconnects. Gated to `video/*`
> content types so a 64 kbps audio stream (~8 KB/s) isn't false-killed
> every 20 s.
>
> Also rolls up a batch of §H.2 audit closures (high-severity credential
> leakage in error log paths, hardened SSRF guards, TMDBHelper installer
> write-result verification), the Prowlarr Test Connection label fix, the
> sha256 cache-key migration (transparent regen on next access), and an
> internal CI hardening pass (Pylint W0108/W0621, all workflow actions
> SHA-pinned to immutable digests, deprecated returntocorp/semgrep-action
> retired in favour of the digest-pinned semgrep/semgrep Docker image).

**Added**
- **`_passthrough_watchdog_applies(ctx)` helper** in `stream_proxy.py` —
  encodes the policy "throughput watchdog runs only when the response is
  serving a `video/*` content type" as a tiny, separately-testable
  function. Avoids the fragile invariant where the watchdog placement
  inside `_stream_upstream_range` happens to be passthrough-only by
  call-graph today; future refactors that add a remux-mode caller would
  silently start killing slow remux startup unless the gate is explicit.
- **`_PASSTHROUGH_MIN_THROUGHPUT_BPS = 102400` and
  `_PASSTHROUGH_THROUGHPUT_WINDOW_SECONDS = 20.0`** constants in
  `stream_proxy.py`. 100 KB/s is well below any video bit rate that
  needs streaming (slowest video is ~1 Mbps = 125 KB/s) and well above
  realistic audio rates (a 64 kbps MP3 is 8 KB/s).
- **Per-session ctx keys** `passthrough_window_t0`,
  `passthrough_window_bytes`, `passthrough_stall_detected`,
  `passthrough_stall_bps`, `passthrough_stall_window_seconds` —
  bookkeeping for the rolling-window throughput sampler. The flag
  `passthrough_stall_detected` is set immediately before the
  `_socket.timeout` raise so the existing `_serve_proxy` exception
  handler can distinguish stall-induced unwind from a genuine Kodi
  disconnect and emit `terminal_reason=passthrough_stall` in the
  summary log instead of the generic `client_disconnected`.
- **Four watchdog tests** at `tests/test_stream_proxy.py`:
  `_aborts_on_low_throughput` (1 KB/s trickle → stall fires, log line
  names passthrough_stall not client_disconnected),
  `_resets_after_a_fast_burst` (3 MB at the 22 s mark clears threshold
  → window resets, no stall), `_skips_audio_streams` (same trickle
  pattern under audio/mpeg → watchdog never engages), and
  `_returns_true_only_for_video` (case-insensitive video/*; excludes
  audio/, application/octet-stream, empty, None, missing key).

**Fixed**
- **Pass-through stall wedge** at
  `plugin.video.nzbdav/resources/lib/stream_proxy.py:2604-2640` (the
  read loop in `_stream_upstream_range`) and `:2337-2374` (the
  `(BrokenPipeError, ConnectionResetError, _socket.timeout)` handler in
  `_serve_proxy` that now branches on `ctx.get("passthrough_stall_detected")`).
  Repro evidence at TODO.md §D.1 ("Other live-testing surprises") plus
  the kodi.log timeline pasted into the v1.0.4 commit message.
- **Improve proxy seek performance**. The stream proxy's seek-path code
  was reworked for lower latency on backseek and chapter jumps; effect
  is most visible on multi-hundred-gigabyte files where the previous
  implementation stalled briefly while restarting the upstream
  connection.
- **Prowlarr "Test Connection" button** in the addon settings now shows
  the right label in the success/failure dialog when both NZBHydra2 and
  Prowlarr indexers are configured. Previously the dialog identified
  the wrong indexer when only Prowlarr was being tested.
- **TMDBHelper player JSON installer** now writes via `xbmcvfs.File`
  and verifies the return value of `.write()`; a disk-full or
  permission failure on `special://profile/addon_data/.../players/`
  used to leave a half-written `nzbdav.json` and silently log
  "successfully installed". A failed write now surfaces as an install
  notification with the failure reason. Closes §H.2-L23.
- **Service watchdog window-property cleanup** at `service.py:_check_active`
  now clears all three `nzbdav.*` window properties (`active`,
  `stream_url`, `stream_title`) on tick, not just `nzbdav.active`. A
  pre-empted playback can no longer leave a stale stream URL or title
  visible to the next play. Closes §H.2-L29.
- **`Playback never started` notification** is now a non-modal toast
  (5 s, via `_notify`) instead of a blocking `Dialog().ok()` modal.
  The previous modal blocked the service tick thread while the user
  dismissed it, and a stuck modal could prevent shutdown cleanup.
  Closes §H.2-H8.

**Changed**
- **Search and proxy cache keys** are now sha256 of their canonical
  content instead of the previous lossy fingerprint. Existing cache
  entries are silently regenerated on next access — transparent to
  the user, no settings changed, no manual cache-clear needed. Removes
  a class of cache-collision false-hits that were shipping the wrong
  results when two distinct queries happened to fingerprint the same
  way.
- **Credential redaction in error logs**. API keys, bearer tokens,
  OAuth tokens, basic-auth passwords, session cookies, and any
  `*token*`/`*secret*`/`*password*`/`*key*` query parameter are now
  redacted by a shared helper before any exception message,
  format-string interpolation, or `xbmc.log` call. Closes the
  H2-tier of the §H.2 audit pass — previously a wrapped exception's
  `repr()` was leaking the apikey through error-formatting helpers
  that bypassed the existing `redact_url` path.
- **Path-traversal and SSRF guards** tightened across the addon's
  HTTP surface. Closes remaining §H.2-high audit findings and the
  trailing scanner alerts that landed under the §H.4 sweep.
- **Internal: pylint hygiene + CI hardening.** Pylint W0108
  (unnecessary-lambda) and W0621 (redefined-outer-name) cleanups in
  tests so the Python 3.8 pylint CI matrix returns 10/10 instead of
  9.99/10 with exit code 4. Every workflow action under
  `.github/workflows/` is now SHA-pinned to an immutable commit
  digest; the deprecated `returntocorp/semgrep-action` was retired
  in favour of running `semgrep/semgrep` CLI from a digest-pinned
  Docker image. CI security posture is now consistent across every
  workflow file.

**Security**
- §H.2-H credential-leakage findings closed (see "Credential redaction
  in error logs" above and the §H.2 cluster in TODO.md Part H).
- §H.4 scanner-alert sweep closed (path-traversal / SSRF guards above).
- §H.2-L23 TMDBHelper installer disk-full path closed.
- All `.github/workflows/` actions SHA-pinned (no remaining floating
  `@v1` / `@v6` references on third-party actions).

---

## [1.0.3] — 2026-04-23

> **Hotfix for "Refusing to start unsafe ffmpeg command" HTTP 500 on every
> auth'd force-remux stream.** `_is_safe_ffmpeg_cmd` (PR #83 security
> hardening) banned CR/LF in every argv element, but the `-headers` value
> that carries the `Authorization` header LEGITIMATELY contains `\r\n` as
> the HTTP header separator required by ffmpeg's HTTP demuxer. v1.0.0-pre-
> alpha through v1.0.2 therefore rejected every auth'd ffmpeg command with
> "Refusing to start unsafe ffmpeg command", Kodi got an immediate HTTP 500
> from the proxy, and the "Playback never started" watchdog tripped 30 s
> later. Real users hitting force-remux (20 GB+ MKVs, every DV file) on
> nzbdav with WebDAV auth hit this on every playback.
>
> The fix narrows the CR/LF ban to argv elements OTHER than the value
> immediately following `-headers`. NUL is still rejected everywhere
> (execve-level hazard), and CR/LF in URLs, input paths, or any other
> argument is still rejected (those are real injection vectors). Only the
> one argv position where ffmpeg legitimately expects CR/LF is exempted.

**Fixed**
- **`Refusing to start unsafe ffmpeg command` on every Authorization-
  carrying force-remux stream** at `stream_proxy.py:1097-1125`. Added
  regression tests at `tests/test_stream_proxy.py`
  (`test_is_safe_ffmpeg_cmd_accepts_crlf_in_headers_value` and four
  adjacent negative tests).

---

## [1.0.2] — 2026-04-23

> **Hotfix for `Completed but no video found` on reverse-proxied nzbdav
> setups.** `v1.0.0-pre-alpha` added a cross-origin host check on every
> PROPFIND href inside `find_video_file`. nzbdav legitimately returns its
> INTERNAL hostname (e.g. `http://localhost:8080/…`) in href values even
> when the client addresses it at a different public endpoint (e.g.
> `http://192.168.1.93:3000`), so the check rejected every href on host
> mismatch and the video-find loop exhausted its 5 retries with no
> candidate file. Real users with `nzbdav_url=http://192.168.1.93:3000`
> and nzbdav's internal `localhost:8080` hit this on every playback.
>
> The fix: when the href host doesn't match the client-configured host,
> trust the PATH portion but ignore the host. All follow-up requests
> still go to the configured WebDAV host, so the original security goal
> — preventing an attacker-controlled server from redirecting us to a
> different host — is preserved without breaking real-world setups.

**Fixed**
- **Cross-origin PROPFIND href regression** at `webdav.py:239-267` from
  PR #83's security hardening. The fully-qualified-href host check now
  logs a `LOGDEBUG` note and uses the href's path portion instead of
  rejecting the entire response. Regression test added at
  `tests/test_webdav.py` (`test_find_video_file_accepts_cross_origin_href_path`).

---

## [1.0.1] — 2026-04-23

> **Source-data Dolby Vision classifier.** The ffmpeg-stderr DV probe shipped
> with `1.0.0-pre-alpha` is retired in favour of a pure-Python RPU parser
> (`dv_rpu.py`) and remote container probe (`dv_source.py`) that reads real
> RPU data out of MP4 / MKV files via HTTP range requests. For the first
> time the addon can tell P7 MEL from P7 FEL, profile 8 from profile 5, and
> no-DV-at-all from probe-couldn't-read — information the old stderr probe
> could never produce. Routing uses a hybrid matrix that keeps the
> 2026-04-15 P8 matroska fix in place while enabling the new P7 MEL → fmp4
> capability (MEL is metadata-only EL and should not trip the CAMLCodec
> dual-layer init path that hung on P8). Falls back to matroska on any
> probe failure or unrecognised profile — no regression surface for content
> that previously played.

**Added**
- **`plugin.video.nzbdav/resources/lib/dv_rpu.py`** — pure-Python Dolby
  Vision RPU parser. Ports the minimum subset of `quietvoid/dovi_tool`
  needed to detect profile (5/7/8) and classify profile 7 MEL vs FEL from
  the NLQ fields. Includes a bit-stream reader, exp-Golomb unsigned/signed
  decoders, full RPU header + mapping + NLQ parse, HEVC start-code
  emulation-prevention byte stripping, and wrapper-format stripping
  (Annex-B, UNSPEC62 NAL header, single-byte prefix). Cross-validated
  bit-for-bit against dovi_tool run on three vendored fixtures (FEL orig,
  MEL orig, profile 8). Pinned upstream commit in the module docstring so
  drift is detectable.
- **`plugin.video.nzbdav/resources/lib/dv_source.py`** — remote container
  probe. Fetches only the bytes needed to locate the first HEVC access
  unit in an MP4 (moov walk → stbl → stsz + stco/co64 → first chunk
  offset → `_http_range`) or MKV (EBML walk → Segment → Tracks + Cluster
  → SimpleBlock / BlockGroup → first video frame). Hands the extracted
  sample to the RPU parser and returns a structured
  `DolbyVisionSourceResult(classification, reason, profile, el_type)`.
  Supports both `.mp4`/`.m4v` and `.mkv`; unsupported containers return
  `dv_unknown` so the caller can fail safe. Handles 64-bit chunk offsets
  (`co64`) for DV UHD files > 4 GiB, and refuses SimpleBlock lacing /
  BlockGroup wrappers rather than producing garbage frame data.
- **Size caps at every attacker-controlled I/O seam.** `_http_range` has a
  16 MiB default read cap; `first_sample_size` is clamped to 16 MiB before
  being used in a Range request. A malicious moov or an RFC-violating
  server that returns 200 OK for a ranged request can no longer OOM
  32-bit Kodi.
- **DV routing matrix in `stream_proxy.prepare_stream`**. P7 FEL →
  matroska, P7 MEL → fmp4, P8/P5/unknown → matroska, non-DV → fmp4.
  Probe crashes are caught and degrade to the matroska path.
- **Vendored DV RPU fixtures** at `tests/fixtures/dovi/` with a README
  documenting the upstream commit SHA and the MIT license notice.
- **Comprehensive tests** at `tests/test_dv_rpu.py` and
  `tests/test_dv_source.py` — parser tests backed by real RPU fixtures,
  container tests driving synthetic MP4/MKV through mocked urlopen.
  Routing tests at `tests/test_stream_proxy.py` cover each cell of the
  matrix.

**Changed**
- **DV routing in `stream_proxy.py`** now consumes a structured
  `DolbyVisionSourceResult` instead of an integer profile. Latency drops
  from ~5–10 s (ffmpeg subprocess spawn + analysis) to typically <1 s
  (two to three HTTP Range requests totalling a few MiB). Content-Length
  is now threaded through to the probe so moov-at-tail MP4 files are
  probed correctly — previously the probe defaulted to a 1 MiB ceiling
  that silently regressed SDR moov-at-tail MP4s off the fmp4 path.

**Removed**
- **`_parse_ffmpeg_dv_profile`** and **`_probe_dv_profile`** from
  `stream_proxy.py`. Their role is fully subsumed by the pure-Python
  source-RPU probe above.

---

## [1.0.0-pre-alpha] — 2026-04-15

> **First major rewrite milestone — tagged on the spike/hls-fmp4 branch as a pre-release before merging to main.** Big-file force-remux on by default, the fmp4 HLS spike landed and self-heals on failure, the submit pipeline stops freezing on slow nzbdav, the resolver hardens against typo'd settings, and `PROXY.md` documents the proxy subsystem end-to-end. 100 GB DV REMUXes now force-remux through ffmpeg instead of crashing 32-bit Kodi on pass-through, and the experimental fragmented-MP4 HLS branch (opt-in via Advanced settings) automatically falls back to the known-good piped-Matroska path before Kodi ever sees a broken URL when ffmpeg can't produce output. Verified on a CoreELEC ARM64 test box against multiple UHD remuxes — fmp4 HLS gives full random seek for non-DV-Profile-7 sources, matroska fallback covers the DV P7 case.

**Added**
- **`Force remux output format` setting** (`force_remux_mode`) in Advanced. Default `matroska` (piped MKV, DV-safe, seek-limited and known-good on Amlogic). Experimental `hls_fmp4` produces an HLS VOD playlist with fragmented-MP4 segments for full random seek across multi-hundred-gigabyte sources. Gated by an automatic Dolby Vision profile 7 fallback (P7 dual-layer FEL has no fmp4 representation), an early-spawn `ffmpeg` validation, and a 30 s production-output watchdog — see "Runtime fmp4→matroska fallback" under **Changed**.
- **`HlsProducer` class** in `stream_proxy.py`. Owns the long-lived ffmpeg subprocess for fmp4 HLS sessions: per-session working directory, seek-driven respawns with generation-bound segment completeness tracking, an init-file readiness gate, a session-wide stderr log reused across respawns (fixes a latent `stderr=PIPE` deadlock from the per-segment-spawn era), and a canonical-init-bytes cache that lets Kodi keep its `EXT-X-MAP` cache valid across seek respawns even though ffmpeg writes a slightly different `init.mp4` each time.
- **HLS HTTP routes** in the stream handler: `/hls/<session>/playlist.m3u8`, `/hls/<session>/init.mp4`, `/hls/<session>/seg_NNNNNN.m4s`. Playlist emits `#EXT-X-VERSION:7` and `#EXT-X-MAP:URI="init.mp4"` when `hls_segment_format=fmp4`. Segment and init URLs enforce that the extension in the request path matches the session's configured segment format. The init handler serves the canonical-bytes cache, never the on-disk file, so a respawn-time overwrite race can't poison Kodi.
- **Dolby Vision profile probe** (historical). The initial `v1.0.0-pre-alpha` tag shipped with `_probe_dv_profile` + `_parse_ffmpeg_dv_profile`, a pair of helpers that ran `ffmpeg -i ... -f null -` against the source and scanned stderr for `DOVI configuration record: ... profile: N`. This pair has since been **retired** in favour of the pure-Python source-RPU probe (`dv_rpu.py` + `dv_source.probe_dolby_vision_source`) that parses the first HEVC access unit's RPU NAL directly — see the entry below for details. Later in the same pre-release cycle the routing gate was broadened so ANY confirmed DV profile routed to matroska (commit 3dce841), and that broadening was preserved when the source-RPU probe landed.
- **`-tag:v hvc1`** on the fmp4 branch. HLS fmp4 spec mandates `hvc1` sample entry for HEVC (parameter sets in the sample description box, not inband), and Amlogic's HLS demuxer needs the right tag to locate `dvcC`/`dvvC` DV configuration records in the init segment. Metadata swap, not a re-encode.
- **`-strict -2`** on the fmp4 branch. Required to enable TrueHD and DTS-HD MA in the MP4/fMP4 muxer on ffmpeg 6.x; without it ffmpeg refuses to write the init header at all on virtually every UHD REMUX.
- **Runtime fmp4 → matroska self-healing fallback.** `HlsProducer.prepare()` now has TWO failure-detection windows in series: (1) a 500 ms argv-rejection poll catching "ffmpeg refuses my flags" failures, and (2) a 30 s production-output wait that polls the filesystem for `init.mp4` + `seg_000000.m4s` while watching ffmpeg liveness. If either window trips, prepare raises and the existing `_register_session` catch rewrites `ctx` to the matroska shape *before* the proxy URL goes back to Kodi. Every fmp4 bug we found on this branch — absolute-path init bug, `-strict -2` missing, analysis hang, runaway respawn loop — now recovers automatically to the known-good matroska path with no user-visible failure. Window 1 also handles the rc=0 early-completion case so synthetic short-source integration tests don't false-trip.
- **`_submit_nzb_with_ui_pump`** in `resolver.py`. `submit_nzb` now runs in a daemon thread with the resolve-side dialog pumping at 250 ms cadence (advancing the progress bar, redrawing the message, watching for cancel). A SECOND daemon thread concurrently polls nzbdav's queue via the new `find_queued_by_name` — as soon as nzbdav has enqueued the job (typically a few seconds after the submit arrives, well before its `addurl` response is generated), the resolver adopts that `nzo_id` and returns immediately without waiting for the rest of the addurl reply. Common case: the "submitting" phase feels instant on any NZB nzbdav accepts, regardless of how slow its addurl processing is. Cancel and Kodi-shutdown are routed through structured sentinels so the resolver bails out cleanly without orphaning state.
- **`find_queued_by_name`** in `nzbdav_api.py`. Structural sibling of the existing `find_completed_by_name` — reads `/api?mode=queue` and returns `{nzo_id, status, name}` if the title is currently in nzbdav's active queue. Used by both the concurrent submit probe and a post-timeout adoption path that runs whenever `submit_nzb` returns the new `{"status": "timeout"}` sentinel.
- **Settings clamping** for `submit_timeout`. Now bound to `[5, 600]` seconds via a new `_clamp_int_setting` helper. After observing a real-world `submit_timeout=300000` (83 hours, accidentally typed extra zeros) bricking the resolver, the clamp guarantees a typo'd setting can't cascade into a multi-hour blocking call.
- **Persistent `ffmpeg.log` archive.** Every `HlsProducer.close()` now copies the session's `ffmpeg.log` to `special://temp/nzbdav-hls-logs/ffmpeg-<session_id>.log` (rolling 10 most-recent) BEFORE the session directory is wiped. Every fmp4 bug we hit during this spike was harder to debug because the smoking-gun log was already gone; now post-mortem inspection survives the cleanup.
- **`just test-integration` target** + `tests/test_integration_hls_ffmpeg.py`. Runs the actual `ffmpeg` binary against a synthetic test MKV (generated on the fly via `lavfi`, served from a localhost HTTP server), exercises `HlsProducer` end-to-end with the production command, and asserts that `init.mp4` is a valid ISO BMFF file (`ftyp`/`moov`/`stsd` present) and that segments are non-empty. Skips automatically if no ffmpeg is on PATH. Catches every class of ffmpeg-related bug we hit on this spike at PR time — every single one was invisible to the existing unit tests because they mock `subprocess.Popen`.
- **`PROXY.md`** — detailed architecture document covering rationale, component interactions, session lifecycle, the four serving tiers, force-remux modes, `HlsProducer` internals (generation boundaries, canonical init cache, late-binding fallback), and a symptom→file debugging playbook.

**Changed**
- **`force_remux_threshold_mb` default** bumped from `0` (off) to `20000` (20 GB). 12 GB MKVs pass through cleanly on 32-bit Kodi, 58 GB REMUXes reliably crash `CFileCache::OpenInputStream`; 20 GB is the empirical breakpoint. Huge files now force-remux out of the box. Set back to `0` to restore the previous pass-through-only behavior.
- **`HLS segment duration`** shortened from 30 s to 6 s. Fixed-EXTINF playlists drift relative to ffmpeg's actual keyframe-aligned cuts; with 30 s nominal segments and 3–5 s source GOPs, the drift accumulated into visible seek miss + A/V desync over a 2-hour movie. 6 s matches typical UHD REMUX GOP lengths and the CMAF / Apple HLS author guide default.
- **ffmpeg probe limits** for the HLS producer: `-probesize` raised from 1 MB → 50 MB, `-analyzeduration` raised from 0 → 15 s. Fixes "track 1: codec frame size is not set" on E-AC-3 / DTS-HD MA / TrueHD sources where a sparsely-interleaved MKV doesn't give ffmpeg enough audio packets to lock down the codec frame size at default analyze settings — that warning previously caused outright "no audio" or a constant ~0.7 s desync depending on the codec. Costs ~3–5 s extra startup latency per spawn (covered by the 30 s playback-never-started watchdog below).
- **`_DEFAULT_SUBMIT_TIMEOUT`** raised from 30 s → 120 s. nzbdav's `/api?mode=addurl` handler routinely takes 30+ s on a big NZB (fetch the .nzb from the indexer, parse XML, enumerate segments). The previous 30 s default was tripping client-side on every large submission, and the resolver was incorrectly retrying — sometimes hitting nzbdav's duplicate-rejection path and producing misleading errors while the original submit was still completing in the background. The 120 s ceiling pairs with the new timeout sentinel + queue adoption to make the worst case "concurrent probe wins, submit silently completes in the background" instead of "user sees a failure".
- **`Playback never started` watchdog** raised from 5 s to 30 s in `service.py:tick`. The previous threshold was tripping during the legitimate fmp4 startup sequence (HlsProducer spawn + ffmpeg analyzeduration + first segment write + Kodi HLS demuxer + Amlogic decoder init can land at 4–8 s on a healthy stream), killing playback before Kodi could even fire `onAVStarted`. 30 s gives every legitimate path comfortable headroom while still catching genuinely dead streams within a reasonable window.
- **fmp4 ffmpeg arguments switched to relative filenames + cwd.** `-hls_fmp4_init_filename` on ffmpeg 6.0.1 (CoreELEC build) rejects absolute paths with "Failed to open segment" / "No such file or directory" even when the parent directory exists and is writable. Fixed by passing bare relative names (`init.mp4`, `seg_%06d.m4s`, `ffmpeg_playlist.m3u8`) to ffmpeg and spawning the subprocess with `cwd=session_dir`. Reader-side code (segment_path, _init_file_complete, wait_for_init, the HTTP handlers) still uses absolute paths, so nothing on the disk-reading path changes.
- **`_segment_complete`** in `HlsProducer` now records a `_spawn_time` at every ffmpeg `Popen` and verifies that any `seg_<n+1>.m4s` used as a "seg_n is done" signal was written by the current generation (mtime ≥ spawn_time). Without this, a stale `seg_<n+1>` left on disk from the backward-seek cache could make a half-written new `seg_<n>` look complete and produce a truncated response.
- **`init.mp4` is no longer unlinked on respawn.** The canonical init bytes cache committed to serving the first generation's bytes on every Kodi request; whatever ffmpeg writes to the disk file on subsequent generations is irrelevant. Unlinking would just race the on-disk overwrite and momentarily fail `_init_file_complete` for no gain.
- **HLS producer ffmpeg duration probe** (`_probe_duration_ffmpeg`) refactored to use a bounded reader thread + wall-clock deadline (`_PROBE_DEADLINE_SECONDS = 20`). The old probe did a synchronous `for line in proc.stderr` with no time guard; a stuck ffmpeg (slow upstream, analysis hang) would block the probe forever, eventually wedging the `prepare_stream_via_service` 60 s timeout and accumulating zombie ffmpeg processes. The new helper guarantees the probe terminates within its budget and kills the ffmpeg before returning. The same pattern originally covered a DV-profile probe too; the DV probe has since been replaced with a pure-Python source-RPU parser (see below) that doesn't spawn ffmpeg at all.
- **`prepare_stream`'s HLS dispatch** now requires `duration > 0`, not just `duration is not None`. A zero-duration probe previously built an HLS ctx that would 500 on the first playlist request.
- **CI test matrix** moved from Python 3.8 to 3.9 + 3.12 after bumping `pytest` to `>=9.0.3` (CVE-2025-71176). The pylint job stays on Python 3.8 so addon source compatibility for the Kodi runtime target is still validated.
- **README stream-proxy section** rewritten to describe all four serving tiers (direct redirect / virtual faststart / pass-through / force-remux) and both force-remux modes; links to the new `PROXY.md`.

**Fixed**
- **C1: router handle-hang.** `router.py` dispatch now guarantees the Kodi plugin handle is resolved on every action route. Previously `/resolve`, `/install_player`, `/clear_cache`, `/settings`, `/configure_*`, `/test_hydra`, and `/test_nzbdav` were reached from menu items with `isFolder=False` but never called `setResolvedUrl` or `endOfDirectory`, leaving Kodi waiting indefinitely on a thrown exception. A new `_safe_resolve_handle` helper is wired into a try/except wrapping every dispatch path.
- **C5: SQLite resolver cleanup.** `resolver._clear_kodi_playback_state` narrowed from a multi-table DELETE to the `bookmark` table only, with a 2 s SQLite busy timeout, proper `LIKE` wildcard escaping on `tmdb_id` (`%`, `_`, `\` all escaped with `ESCAPE '\\'`), `sqlite3.OperationalError` caught separately from the generic exception path, and a skip path when `xbmc.Player().isPlayingVideo()` is true so the cleanup doesn't race Kodi's own internal library vacuum (which was occasionally freezing the decoder during database-heavy sessions).
- **Submit timeout misclassification.** `submit_nzb` now distinguishes `socket.timeout` (and `URLError(reason=socket.timeout(...))`) from other network errors and returns a structured `(None, {"status": "timeout"})` sentinel instead of the old `(None, None)` "retry freely" shape. The resolver runs a 12 s queue + history adoption probe on the sentinel and adopts the existing `nzo_id` if found, instead of retrying the submit and either bouncing as a duplicate or orphaning the in-progress job. Works on Python 3.8 (bare `socket.timeout`) and 3.10+ (where `socket.timeout` is an alias for `TimeoutError`).
- **Submit running on the plugin thread.** `submit_nzb` no longer blocks the Kodi plugin thread for the full 120 s timeout window. The new `_submit_nzb_with_ui_pump` runs it in a daemon worker thread and pumps the dialog at 250 ms cadence so the cancel button is live, the message updates, and the user sees progress instead of a frozen "Submitting NZB..." dialog.
- **Nondeterministic init.mp4 across seek respawns.** When ffmpeg respawns at a different `-ss` for a seek, the new `init.mp4` has a different `edts`/`elst` (edit list) box than the original — the codec config (`hvcC`/`mp4a`) is byte-identical, but Kodi caches `EXT-X-MAP` once per playlist, so segments produced after the respawn referenced an edit list the cached init doesn't know about and playback stalled or desynced. Fixed by caching the first generation's init bytes in `HlsProducer._canonical_init_bytes` and serving those bytes on every Kodi fetch regardless of what's on disk.
- **fmp4 ffmpeg probe pair could block indefinitely** on a stuck ffmpeg (slow upstream, analysis hang). Initially fixed by the shared `_probe_ffmpeg_stderr` helper (daemon reader thread + wall-clock deadline, killing the ffmpeg on match, byte budget, OR deadline expiry). Later in the same pre-release cycle the DV-profile probe was retired entirely in favour of a pure-Python source-RPU parser that does HTTP range requests instead of spawning ffmpeg, removing this class of stuck-subprocess failure from the DV path altogether.
- **`MagicMock/` directory leak** in unit test runs: `_archive_ffmpeg_log` was passing a mocked `xbmcvfs.translatePath()` return value straight to `os.makedirs`, creating a literal `"MagicMock"` directory in cwd. Now isinstance-checks the candidate before accepting it.
- **fmp4 segments could be served before fully written** — the old `_segment_complete` could mistake a stale prior-generation `seg_n+1` for a current-generation completeness signal and return True while the new `seg_n` was still being written. Fixed by the per-generation mtime check (see Changed).
- **`_clear_kodi_playback_state` no longer runs during active playback** — avoids contention with Kodi's own `MyVideos131.db` / `Textures13.db` vacuum, which was occasionally freezing the decoder during database-heavy sessions.

**Security**
- Bumped `pytest` dev dependency to `>=9.0.3,<10` for [CVE-2025-71176](https://github.com/advisories/GHSA-6w46-j5rx-g56g) (insecure permissions on `/tmp/pytest-of-<user>` allowing local DoS or privilege escalation on shared UNIX hosts). Dev-only dependency; no runtime impact on Kodi installations.

---

## [0.6.21] — 2026-04-13

> **Two `submit_nzb` lifecycle fixes.** The addon now tells nzbdav to cancel an in-flight job when it gives up on a download, so re-submitting the same NZB doesn't get blocked by stale duplicates. And when nzbdav rejects a submit, the dialog now shows the actual server message instead of a generic "check your settings" string.

**Fixed**
- Stale jobs in nzbdav's queue after the addon aborts. When `_poll_until_ready` gave up — download timeout, user-cancelled the resolve dialog, max poll iterations, or Kodi shutdown — it left the job sitting in nzbdav's queue indefinitely. The next attempt to play the same NZB would then hit nzbdav's duplicate-rejection path with HTTP 500. The addon now calls a new `cancel_job(nzo_id)` helper on every Group A abort path that issues a SABnzbd-compatible `mode=queue&name=delete` with a 3-second timeout, so the next submit lands on a clean queue. Group B paths (nzbdav itself reporting Failed/Completed) and any race where the job moves to history between polls deliberately leave history entries alone so users can still inspect what nzbdav reported in the web UI. (Audit finding L1.)
- Generic dialog on submit failures hiding nzbdav's real error message. `submit_nzb` used to catch every error class (HTTPError, URLError, JSONDecodeError, anything else) the same way and return a bare `None`; the resolver's retry loop then attempted 3 submits before showing a generic "check nzbdav URL and API key" dialog. The function now catches `HTTPError` specifically, captures the response body via `e.read()` (HTML-stripped and whitespace-collapsed), and returns a `(None, {"status", "message"})` tuple. The retry loop classifies the error: HTTP 408/502/503/504 still retry as before (they're classically transient gateway issues — 408 is RFC 9110's "request timeout, please retry"), but HTTP 4xx and HTTP 500/501 short-circuit immediately and surface nzbdav's actual response message — retrying nzbdav's "duplicate" rejection won't make it not a duplicate. Connection errors and JSON decode failures continue to retry exactly as before.

---

## [0.6.20] — 2026-04-13

> **Two resolve-loop fixes.** Kodi's UI no longer briefly freezes during resolve polling when nzbdav is momentarily unreachable, and an invalid WebDAV password now surfaces as the "Authentication failed" dialog within a poll iteration instead of being silently masked until the download timeout fires.

**Fixed**
- Brief UI freeze during resolve polling. The WebDAV retry delay used `time.sleep()` on Kodi's main thread, locking the UI and delaying shutdown. It now uses `xbmc.Monitor().waitForAbort()`, which yields to the Kodi event loop and unwinds immediately on shutdown. (Audit finding C4.)
- Silent retry loop when WebDAV credentials are wrong. When nzbdav's queue and history APIs both returned no data, the addon probed the WebDAV server using the movie's human-readable title as a filename — which always returned 404, so an invalid-credentials failure never surfaced as the "Authentication failed" dialog and the resolve spinner would just run until the download timeout. The probe now HEADs the WebDAV content root, so a bad password produces the auth dialog within one poll iteration. Server 5xx and network-outage probes also now return accurate error codes in the logs, though those paths remain log-only in the resolver — adding dialogs for them is a separate follow-up. (Audit finding C3.)

---

## [0.6.19] — 2026-04-12

> **Documentation refresh.** Nothing user-visible. The README finally catches up with how the stream proxy has actually worked since v0.6.14.

**Changed**
- Rewrote the README's stream-proxy section to describe the three real code paths (direct MP4, virtual faststart, MKV pass-through with zero-fill recovery). The old copy still talked about MP4-only remuxing, which stopped being the full story four releases ago.

**Fixed**
- Silenced pylint `W0201` on `self.close_connection = True` assignments in `stream_proxy.py`. `close_connection` is a real `BaseHTTPRequestHandler` attribute initialized by the parent class, but pylint's static analysis couldn't see it, so CI was failing on two false positives.

---

## [0.6.18] — 2026-04-12

> **Big MKVs now play, seek, and recover from bad Usenet articles — without ffmpeg in the loop.** Pass-through is finally the default on 32-bit Kodi, which gives you native scrubbing through the source file's real Cues plus v0.6.15's zero-fill recovery on missing articles. If huge remuxes ever felt fragile, this is the release you want.

**Changed**
- **`force_remux_threshold_mb` default flipped from `4096` to `0`.** Live testing on 32-bit CoreELEC confirmed the pass-through path handles 12+ GB MKVs fine and gives strictly more features than force-remux: native user seeking via real MKV Cues, zero-fill recovery on missing Usenet articles, and zero ffmpeg CPU overhead. Set the threshold to a non-zero MB value to restore v0.6.16 behaviour if you need it.
- Pass-through responses now send `Connection: close` and set `close_connection = True` on the handler, so stale handler threads unwind immediately when Kodi reconnects instead of lingering with multi-megabyte TCP buffers and a live upstream HTTP response.

**Fixed**
- `MemoryError` in the pass-through proxy's upstream read loop on 32-bit Python. Kodi's `CFileCache` alone can reserve around 1.5 GB (`cachemembuffersize × readbufferfactor`), leaving very little headroom in Python's 32-bit heap; a 1 MB read chunk was tipping a fragmented heap over. Read chunk is now 64 KB and `MemoryError` is explicitly caught.
- `_clear_kodi_playback_state()` DB cleanup is skipped when a video is already playing, avoiding contention with Kodi's internal `MyVideos131.db` / `Textures13.db` vacuum which could otherwise freeze the decoder mid-playback.

---

## [0.6.17] — 2026-04-12

> **Kill the zombie.** Fixes a nasty bug where a stalled playback would leave an ffmpeg remux process behind, so the next time you hit Play the addon hung with "Playback never started after 70 s". Restarts just work again.

**Fixed**
- Zombie ffmpeg remux lingering after a Kodi playback stall (e.g. when Kodi's automatic DB vacuum froze the decoder for 10+ seconds). Kodi would stop consuming bytes without firing `onPlayBackStopped`, so ffmpeg kept writing into a dead TCP socket forever.
- Prior stream sessions are now torn down on every new `prepare_stream` call, and the remux socket has a 60 s write timeout so stuck writes always unwind and kill ffmpeg.
- `NzbdavPlayer.onPlayBackStopped` / `onPlayBackEnded` hooks clear active proxy sessions immediately on clean stops.

---

## [0.6.16] — 2026-04-12

> **First attempt at fixing huge MKVs on 32-bit Kodi.** Force-remuxes anything over 4 GB through ffmpeg so Kodi never sees an overflowing Content-Length. **Superseded by v0.6.18** — the real root cause turned out to be elsewhere (already fixed in v0.6.14). The setting is still available if your platform needs it.

**Added**
- New setting **"Force ffmpeg remux above (MB)"** (default `4096`, `0` disables) to force-remux large non-MP4 files through ffmpeg.
- Subtitles are now copied verbatim when remuxing MKV sources, so PGS/DVD/HDMV bitmap subs (which can't be re-encoded to SRT) no longer abort the remux.

**Fixed**
- "Open - Unhandled exception" on >4 GB MKV files on 32-bit Kodi builds (Amlogic CoreELEC and similar).

---

## [0.6.15] — 2026-04-12

> **Your stream no longer dies on a single bad Usenet article.** The proxy probes forward to find a readable offset, fills the gap with zeros, and keeps playing. Retries for up to 30 seconds in case the backend is just briefly unhappy.

**Added**
- **Stream proxy zero-fill recovery.** The pass-through proxy now survives mid-playback upstream failures (missing Usenet articles, brief nzbdav restarts) by probing forward to find a readable offset, zero-filling the gap, and resuming the stream. No more black screen or "Playback failed" dialog on a single bad article.
- Recovery uses retry-with-backoff up to 30 s so transient upstream unavailability gets a chance to come back before the region is declared unrecoverable.

---

## [0.6.14] — 2026-04-12

> **Fixes "Playback failed to start" on MKV files** by routing every format through the local stream proxy. Kodi no longer pokes the WebDAV server directly, which was triggering a PROPFIND cascade that broke playback on some setups.

**Fixed**
- MKV playback failing with "Playback failed to start" / "Unhandled exception". All file types now route through the local stream proxy so Kodi never talks to the WebDAV server directly, avoiding a PROPFIND parent-directory scan that cascaded into an Open failure.
- `tmdb_id` is now forwarded in the TMDBHelper `play_movie` template so the replay-bookmark cleanup actually matches movie entries.

---

## [0.6.13] — 2026-04-12

> **NZB submit timeout is now configurable.** Large NZBs can take longer than 15 seconds for nzbdav to parse; the default is now 30 seconds and you can tune it in settings.

**Added**
- Configurable NZB submit timeout in **Advanced → Polling** (default `30 s`, was a hardcoded `15 s`).

---

## [0.6.12] — 2026-04-12

> **Fixes "Playback failed to start" when you replay the same NZB.** Kodi was auto-resuming from stale bookmarks on plugin URLs; those are now wiped before each play.

**Fixed**
- Replay of the same NZB failing with "Playback failed to start". Kodi's stale bookmarks, settings, and streamdetails are now cleared before each play so auto-resume doesn't misfire on plugin URLs.

---

## [0.6.11] — 2026-04-12

> **Error messages stop getting truncated.** NZBHydra failures and invalid responses pop up in proper modal dialogs with the full text, and duplicate results across indexers stay visible instead of being merged.

**Changed**
- Preserve full modal error dialog text where Kodi's `Dialog` API allows it (no silent truncation).
- Duplicate NZB results from multiple indexers are kept visible instead of being merged.

**Fixed**
- NZBHydra outages and invalid responses are now surfaced in modal dialogs instead of being silently swallowed.

---

## [0.6.10] — 2026-04-12

> **Every playback failure gets a real dialog now.** Toast notifications were too easy to miss, especially for late failures like missing articles or auth rejection mid-stream.

**Added**
- Modal error dialog on every playback failure (replaces toast notifications).
- Detection for post-resolve stream crashes (missing articles, auth rejection) with matching error dialog.

**Fixed**
- Video playlist is now cleared on stream failure to prevent a TMDBHelper retry loop.

---

## [0.6.9] — 2026-04-12

> **Duplicate results from multiple indexers collapse into one row.** Also, the **Max results** setting actually limits NZBHydra2 queries now — it was previously hardcoded to 100.

**Added**
- Identical NZBs from multiple indexers (same title + size) are deduplicated and shown with **"Multiple"** as the indexer label.

**Fixed**
- The configured **Max results** limit is now sent to the NZBHydra2 API (was previously hardcoded to `100`).

---

## [0.6.8] — 2026-04-12

> **Fixes playback failing because Kodi was mangling the auth header.** URL-encoded `=` characters in pipe-syntax auth were being eaten. Download failures also get a modal dialog instead of a toast now.

**Added**
- Download failures are shown in a modal dialog instead of a toast notification.

**Fixed**
- Playback failing due to URL-encoded `=` in the auth header (Kodi pipe syntax).
- Video playlist is cleared on resolve failure to prevent a Kodi retry loop.

---

## [0.6.7] — 2026-04-10

> **Internal cleanup.** No user-visible changes.

**Fixed**
- Pylint `W0621`: removed a redundant `import os` in `_serve_temp_faststart`.

---

## [0.6.6] — 2026-04-10

> **Security hardening.** API keys and auth credentials are now redacted from debug logs, and the stream proxy rejects non-HTTP URLs. Includes several internal cleanups to clear code-scanning alerts.

**Added**
- Validate URL scheme in the stream proxy to reject non-HTTP URLs.
- Localized "no video file found" notification string.

**Changed**
- Redact API keys and auth credentials from debug logs.
- Reuse the shared notification helper (removed duplicated logic).
- Reduced `_serve_remux` complexity by extracting seek and faststart helpers.
- Truncated server failure messages to 80 chars for the TV UI.

**Fixed**
- All GitHub code-scanning alerts (complexity, indentation, broad-except).

---

## [0.6.5] — 2026-04-10

> **The addon actually tells you when something goes wrong.** Download failures, missing video files, stream errors — all surfaced to the user instead of silently eaten. Also fixes a playback freeze when ffmpeg's stderr buffer filled up.

**Added**
- Show nzbdav failure reason to the user when a download fails.
- Notify the user when the download completes but no video file is found on WebDAV.
- Show stream errors to the user instead of silently swallowing them.

**Fixed**
- Playback freeze caused by ffmpeg's stderr pipe buffer filling up.

---

## [0.6.2] — 2026-04-10

> **Flaky test fix.** No user-visible changes.

**Fixed**
- Flaky cache eviction test due to non-deterministic file sizes.

---

## [0.6.1] — 2026-04-10

> **Internal cleanup.** No user-visible changes.

**Changed**
- Narrowed broad `except` handlers to specific exception types in the stream proxy.
- Removed an unnecessary pylint disable comment in the MP4 parser.

---

## [0.6.0] — 2026-04-07

> **A huge one.** MP4 files now get native Kodi seeking, pause, and Dolby Vision support via a **pure-Python proxy that rewrites the MP4's internal chunk offsets on the fly** — no ffmpeg re-encode. Files over 4 GB work too. There's a three-tier fallback if the fast path fails, and a new "container" column in the results list makes MP4 vs MKV obvious at a glance.

**Added**
- **Pure-Python MP4 moov-relocation proxy** for native seeking, pause, and Dolby Vision support.
  - Parses the `moov` atom from the remote MP4 via HTTP range requests and rewrites `stco` / `co64` chunk offsets in place.
  - Serves a virtual faststart MP4 (moov-before-mdat) with `Accept-Ranges: bytes`.
  - **Three-tier fallback:** virtual faststart → temp-file faststart → MKV remux.
  - Direct redirect for MP4s that are already faststart (skips the proxy entirely).
  - LRU byte cache for WebDAV range-request coalescing.
  - `co64` support (64-bit chunk offsets) for files >4 GB.
  - `moov` location computed from `mdat` size instead of blind tail probing.
  - Single streaming connection per seek instead of per-chunk requests.
- Container column in the NZB results list (MKV green, MP4 red).

**Changed**
- Removed unused HLS segment endpoints.

**Fixed**
- Zombie ffmpeg from the duration probe eating 100% CPU.
- MKV duration via `-metadata DURATION=` for a correct progress bar.

---

## [0.5.0] — 2026-04-06

> **Settings are simpler.** The WebDAV URL field is gone (it defaults to the nzbdav URL automatically), and release-group fields became proper multi-select dialogs with 93 known groups pre-loaded. The TMDBHelper installer no longer prompts — it just installs.

**Added**
- 93 known release groups with curated preferred / excluded defaults.
- Handler-level tests for `HEAD` and `GET` responses.

**Changed**
- Simplified the player installer to TMDBHelper-only (no multi-select dialog).
- Removed the WebDAV URL setting (now defaults to the nzbdav URL automatically).
- Replaced release-group text fields with multiselect dialogs.
- Extracted `_embed_auth_in_url` helper to DRY auth credential embedding.
- Removed duplicate server attribute initialization.
- Narrowed exception handling in the seek-kill block to `OSError`.

**Fixed**
- Continuation requests were restarting ffmpeg from byte 0 instead of resuming.
- `_validate_url` crash when the URL is `None`.

---

## [0.4.3] — 2026-04-06

> **First cut of the stream proxy.** MP4 files get remuxed to MKV on the fly via ffmpeg to work around a 32-bit Kodi `CFileCache` bug, subtitles are converted from `mov_text` to SRT, and seeking works with a real progress bar. The proxy now lives in the background service, so it survives script exit and can auto-recover on playback failure.

**Added**
- **On-the-fly MP4 → MKV remux** via ffmpeg (works around a 32-bit Kodi `CFileCache` bug with large MP4 moov atoms).
- Subtitle conversion (`mov_text` → SRT) with a toggleable setting.
- Byte-range seeking with a duration probe and progress-bar support.
- Background service for playback auto-recovery with retry.
- Helpful error messages on playback failure.

**Changed**
- Moved the stream proxy into the background service so it survives script exit.

**Fixed**
- "Playback failed" dialog caused by `setResolvedUrl(False)`.
- All GitHub code-scanning alerts (pylint, bandit, CodeQL).

---

## [0.4.1] — 2026-04-06

> **Security review fixes.** Addressed HIGH/MEDIUM findings from code review. No user-visible behaviour changes.

**Fixed**
- Addressed HIGH/MEDIUM findings from code review.

---

## [0.4.0] — 2026-04-06

> **Spot instant plays at a glance.** NZBs that are already downloaded in nzbdav now get a lightning-bolt indicator in the results list. Also silences some noisy log spam when you stop playback.

**Added**
- Lightning-bolt indicator for NZBs that are already downloaded in nzbdav history.

**Fixed**
- Suppressed `BrokenPipeError` log spam when stopping playback.

---

## [0.3.0] — 2026-04-05

> **WebDAV streaming works reliably on CoreELEC** via an MP4 faststart proxy. Adds Test Connection buttons for NZBHydra2 and nzbdav in settings, skips re-downloading NZBs that are already complete, and TV show search finally uses IMDb ID so it finds the right show every time.

**Added**
- WebDAV streaming on CoreELEC via an MP4 faststart proxy.
- **Test Connection** buttons for NZBHydra2 and nzbdav in settings.
- LRU cache eviction when the search cache exceeds the 50 MB limit.
- Detailed progress logging during search stages.
- Thread-safety lock on stream proxy context assignment.
- Max-iterations safeguard on the resolve poll loop.

**Changed**
- Route all formats through the local proxy to fix stale-handle playback.
- Skip re-downloading if the NZB is already complete in nzbdav history.
- Retry NZB submission up to 3 times on transient failure.
- TV show searches now use IMDb ID instead of title-only text search.
- URL-encode WebDAV folder paths and auth header values.
- Surface NZBHydra search errors to the user with clear messages.
- Harden PROPFIND XML parsing against malformed responses.

**Fixed**
- Filter count / "show all" prompt when everything was filtered out.

---

## [0.2.0] — 2026-04-05

> **Kodi localization support.** The addon UI and settings labels are now driven through Kodi's standard string system, so translations are possible.

**Added**
- Kodi localization support for the addon UI and settings labels.
- GitHub community health files and repository best-practice coverage tests.

**Fixed**
- Enum settings labels so the sort-order dropdown renders localized text in Kodi.
- Hardened numeric setting parsing to avoid filter/search regressions.

---

## [0.1.0] — 2026-04-05

> **Initial release.** Search NZBHydra2, submit to nzbdav, stream via WebDAV — all wrapped in a TMDBHelper player.

**Added**
- NZBHydra2 search integration (movie + TV).
- nzbdav submission and WebDAV streaming.
- TMDBHelper player integration.
- PTT-based quality filtering (resolution, HDR, audio, codec, language).
- Custom full-screen results dialog with color-coded labels.
- Keyword and release-group filters.
- Relevance-based sorting.
- Search result caching.
- Auto-select best match mode.
- Playback monitoring with retry.

---

[1.0.5]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v1.0.5
[1.0.4]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v1.0.4
[1.0.3]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v1.0.3
[1.0.2]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v1.0.2
[1.0.1]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v1.0.1
[1.0.0-pre-alpha]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v1.0.0-pre-alpha
[0.6.21]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.21
[0.6.20]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.20
[0.6.19]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.19
[0.6.18]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.18
[0.6.17]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.17
[0.6.16]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.16
[0.6.15]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.15
[0.6.14]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.14
[0.6.13]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.13
[0.6.12]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.12
[0.6.11]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.11
[0.6.10]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.10
[0.6.9]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.9
[0.6.8]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.8
[0.6.7]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.7
[0.6.6]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.6
[0.6.5]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.5
[0.6.2]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.2
[0.6.1]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.1
[0.6.0]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.6.0
[0.5.0]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.5.0
[0.4.3]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.4.3
[0.4.1]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.4.1
[0.4.0]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.4.0
[0.3.0]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.3.0
[0.2.0]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.2.0
[0.1.0]: https://github.com/xbmc4lyfe/nzbdavkodi/releases/tag/v0.1.0
