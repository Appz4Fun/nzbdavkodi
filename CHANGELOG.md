# Changelog

All notable changes to the **NZB-DAV Kodi addon** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

[![Latest Release](https://img.shields.io/github/v/release/xbmc4lyfe/nzbdavkodi?label=latest&color=FF570A)](https://github.com/xbmc4lyfe/nzbdavkodi/releases/latest)

---

## Quick navigation

| Version | Released | What it's about |
|---|---|---|
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
