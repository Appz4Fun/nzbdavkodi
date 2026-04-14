# Bug Audit — nzbdav Kodi Addon

Consolidated from three independent audits. Deduplicated and organized by severity, then by theme within each severity.

---

## HIGH

### H1. Stream proxy session/lifetime races (cluster of 7+ findings)

- **H1a. Use-after-free on prune:** handler reads `ctx`, dispatches; concurrent prune can `pop` + `_cleanup_session(ctx)` in between, handler serves from a deleted temp file and killed ffmpeg. (`stream_proxy.py:204 vs 981-983`)
- **H1b. LRU eviction kills active playback:** the 9th `prepare_stream` kills the oldest active playback via `_cleanup_session`. (`stream_proxy.py:985-993`)
- **H1c. TTL prune trusts stale `last_access`:** playbacks >6h get evicted mid-stream. (`stream_proxy.py:973-983`)
- **H1d. Concurrent seek spawns duplicate ffmpeg:** two concurrent GETs both spawn ffmpeg and store as `active_ffmpeg`; first finisher's `is` proc check only clears its own slot, leaving two ffmpegs fighting over stdout. (`stream_proxy.py:514-580`)
- **H1e. `_register_session` holds `_context_lock` while calling `_cleanup_session`,** blocking `proc.kill()/wait()`. (`stream_proxy.py:959-964`)
- **H1f. `stop()` clears sessions while in-flight handlers still hold `ctx` refs.** (`stream_proxy.py:879-892`)
- **H1g. `_context_lock` lives on StreamProxy, not the handler/server** — `_get_stream_context()` reads `stream_sessions` and mutates `ctx["last_access"]` with no lock. (`stream_proxy.py:203-207`)

### H2. Credential leaks via ffmpeg argv and URL redaction gaps

- **H2a. `_embed_auth_in_url` splices `user:password@host` into ffmpeg argv;** credentials visible in `/proc/<pid>/cmdline` and `ps -ef`. (`stream_proxy.py:1307-1322`, `1240`, `115-141`, `309-321`, `1215-1239`)
- **H2b. `_prepare_tempfile_faststart` logs `stderr.decode()[:300]` on non-zero exit;** ffmpeg messages echo full URL with basic-auth. (`stream_proxy.py:1240-1247`)
- **H2c. `redact_url` only redacts `apikey`;** `password/auth/token/api_key/key/secret/access_token` all leak. (`http_util.py:14`)
- **H2d. `redact_url` does not redact `user:password@host` userinfo in netloc.** (`http_util.py:10-19`)
- **H2e. HTTPError/URLError messages containing full URL with `apikey=`** surfaced to user via `_hydra_unavailable_error` and notifications. (`hydra.py:96-130`)
- **H2f. `nzbdav_api.py` exception messages can contain unredacted apikey URLs.** (`nzbdav_api.py:74`)

### H3. mp4_parser unvalidated box sizes (cluster of 6 findings)

- **H3a. DoS via unbounded count:** `_rewrite_stco`/`_rewrite_co64` read attacker-controlled count as unbounded uint32 and iterate `range(count)`; `count=0xFFFFFFFF` → 4-billion-iteration loop. (`mp4_parser.py:98-117`)
- **H3b. O(N²) memory in moov rewrite:** `_rewrite_offsets_recursive` slices `data[body_start:body_end]` and re-parses per box; a 50 MB moov hangs the proxy. (`mp4_parser.py:152-155`)
- **H3c. No bounds validation:** no check that `offset + total_size` stays within `len(data)`; child boxes claiming size > parent corrupt the buffer. (`mp4_parser.py:135`)
- **H3d. `scan_top_level_boxes` advances by `total_size` even when `read_box_header` returns `size == 0`,** silently discarding everything after. (`mp4_parser.py:81`)
- **H3e. No moov/mdat overlap check;** malformed file passes as faststart with overlapping ranges. (`mp4_parser.py:83-85`)
- **H3f. `_find_moov_after_mdat` can produce `file_size - 1 = -1` on empty files → malformed Range.** (`mp4_parser.py:225-239`)

### H4. Stream proxy upstream response validation gaps

- **H4a. `_stream_upstream_range` never validates `resp.status`;** if upstream returns 200 instead of 206, content streams from offset 0 while client expects start — silent corruption. (`stream_proxy.py:736-758`)
- **H4b. `_serve_mp4_faststart` writes whole 1 MB chunks without clamping to `length - bytes_sent`;** emits up to ~1 MB past advertised Content-Length on partial-range requests. (`stream_proxy.py:437-444`)
- **H4c. Nothing bounds written by `end - start + 1`;** misbehaving upstream exceeds advertised Content-Length. (`stream_proxy.py:744-758`)
- **H4d. Empty chunk from upstream treated as EOF;** on a slow connection this could be a temporary condition, prematurely ending the stream. (`stream_proxy.py:440`)

### H5. `_poll_once` non-daemon threads leak across iterations
**File:** `resolver.py:420-425`

Non-daemon threads with `join(timeout=10)` leak across iterations.

### H6. `nzbdav_api.py` HTTP calls with no timeout — indefinite blocking
**Files:** `nzbdav_api.py:111`, `154`, `178`, `226`

`get_job_history`, `find_completed_by_name`, `get_completed_names` call `_http_get(url)` with no timeout, blocking the resolver indefinitely.

### H7. Broad exception catches mask specific errors and swallow everything

- **H7a. `nzbdav_api.py:74`:** `except (URLError, json.JSONDecodeError, Exception)` — `Exception` swallows everything; bare except collapses 401/403/404/timeout to `None`.
- **H7b. `hydra.py:96`, `120`:** `except (URLError, Exception)` — catching `Exception` after `URLError` (which is a subclass of `OSError`) means all exceptions are caught, including `KeyboardInterrupt` and `SystemExit` in Python 2, and `MemoryError` in any version. The `URLError` in the tuple is redundant.

### H8. Service monitor: `xbmcgui.Dialog().ok()` from service loop thread
**File:** `service.py:233-238`

While the comment in `onPlayBackError` correctly defers dialog showing to `tick()`, calling `xbmcgui.Dialog().ok()` from the service loop (which runs every 1 second) blocks the loop if the user doesn't dismiss the dialog promptly. No further state changes are processed until dismissal.

### H9. `resolve_and_play` error path escapes without `setResolvedUrl(handle, False)`
**File:** `resolver.py:666-669`

`_get_poll_settings()` and `xbmcgui.DialogProgress().create()` run outside `resolve()`'s `try/except`; a bad setting or dialog construction failure escapes without `setResolvedUrl(handle, False)`.

### H10. `_play_via_proxy` never sets `nzbdav.active`/`stream_url`/`stream_title`
**File:** `resolver.py:319-359`

Service-side retry and error dialogs never fire on the RunPlugin code path.

### H11. Language filter compares PTT lowercase codes against UI labels
**File:** `filter.py:424`

Compares `"en"`, `"es"` against `"English"` — any enabled language filter rejects every result.

### H12. `prepare_stream_via_service` 60s HTTP timeout vs 600s ffmpeg allowance
**Files:** `stream_proxy.py:1307-1322` vs `1240`

Plugin times out while service still completes, orphaning session + ffmpeg + temp file.

### H13. `_probe_duration` reads ffmpeg stderr line-by-line with no timeout
**File:** `stream_proxy.py:1172-1193`

Silent upstream blocks indefinitely. Broad-except for `TimeoutExpired` never `proc.kill()`s, orphaning ffmpeg.

### H14. `http_get` performs no scheme validation — SSRF / local file read
**File:** `http_util.py:22-26`

`urlopen` opens `file:///`, `ftp://`, enabling SSRF / local file read.

### H15. `notify()` builtin injection via unescaped interpolation
**File:** `http_util.py:41-45`

`executebuiltin` with no escaping; `,` or `)` in upstream messages allows builtin injection.

### H16. `service.py` state mutated by Kodi callbacks on internal threads without locks
**File:** `service.py:117-160`

`NzbdavPlayer._state`/`_retry_count`/`_av_started`/`_last_position` mutated by Kodi player callbacks on internal threads while `tick()` reads-then-writes on the service main thread with no lock.

### H17. `webdav.py` best-file selection picks 0-byte placeholder over real video
**File:** `webdav.py:281`

`best_file = href_path` with `size >= best_size` and `best_size = 0` initial picks a 0-byte placeholder `.mkv` over real video files.

### H18. `cache.py` non-atomic write — concurrent writers interleave and corrupt JSON
**Files:** `cache.py:71-72`

No `.tmp` + `os.replace`; concurrent writers interleave.

### H19. `_retry_playback` never distinguishes user-stopped (IDLE) intent
**File:** `service.py:269`

User-stopped streams get resurrected.

---

## MEDIUM

### M1. Poll/timeout configuration bugs

- **M1a.** `MAX_POLL_ITERATIONS = 720` hard-codes 5s cadence; `poll_interval=1` caps total wait at ~720s instead of 3600s. (`resolver.py:30`, `505`)
- **M1b.** `poll_interval=0` produces `waitForAbort(0)` tight loop. (`resolver.py:366-368`)
- **M1c.** `int(percentage or 0)` raises on `"45.5"`. (`resolver.py:566`, `nzbdav_api.py:275`, `284`)

### M2. `_storage_to_webdav_path` doesn't URL-encode
**File:** `resolver.py:383` — Space / `#` / `?` / `&` break the WebDAV URL.

### M3. `nzbdav.stream_url` stores pipe-header form; service retry turns `|Header=Value` into filesystem path
**File:** `resolver.py:285-286`

### M4. `_play_via_proxy` direct branch passes `li.getPath()` AND the ListItem, duplicating headers
**File:** `resolver.py:347`

### M5. LIKE pattern with `%`/`_` in user-controlled `tmdb_id` matches unintended rows
**File:** `resolver.py:141-149`

### M6. `sys.argv[0]` used as plugin URL for DB cleanup — stale in `resolve_and_play()` service context
**File:** `resolver.py:130-133`

### M7. ffmpeg process leak on `TimeoutExpired` in `_prepare_tempfile_faststart`
**File:** `stream_proxy.py:1240-1252`

### M8. `urlopen(timeout=120)` has no per-read timeout
**File:** `stream_proxy.py:437`

### M9. Request abandonment: small range → fetch whole tail → break mid-stream
**File:** `stream_proxy.py:430-437` — Small repeated ranges thrash upstream.

### M10. `_probe_duration` stdout=PIPE never read; muxer banner can deadlock
**File:** `stream_proxy.py:1163-1168`

### M11. `_register_session` unconditionally overwrites `self._server.stream_context`; legacy `/stream` clients rebind
**File:** `stream_proxy.py:962`

### M12. Faststart temp files leak forever on crash; no `atexit` or startup orphan scan
**File:** `stream_proxy.py:1199-1258`

### M13. `_MAX_RECOVERY_SECONDS` budget only checked at loop top; handler can block ~60s vs advertised 30s
**File:** `stream_proxy.py:785-788`

### M14. `time.sleep(delay)` in handler can't observe shutdown; should use `Monitor.waitForAbort()`
**File:** `stream_proxy.py:788`

### M15. Concurrent `_serve_remux` on different sessions clobber server-wide `active_ffmpeg`/`current_byte_pos`
**Files:** `stream_proxy.py:579-580`, `644-645`

### M16. `_resolve_seek` holds per-ctx `ffmpeg_lock` across `kill()+wait()`; chunk loop stalls
**File:** `stream_proxy.py:514-541`

### M17. `_get_settings` `.rstrip("/")` on `None`; no non-empty validation
**File:** `nzbdav_api.py:20-22`

### M18. `search_term = name.split(".")[0]` keeps only the first dot-segment
**File:** `nzbdav_api.py:142`

### M19. No deduplication of aggregated multi-indexer results
**File:** `hydra.py:100-130`

### M20. `int(max_results or 25)` raises on non-numeric setting
**File:** `hydra.py:69`

### M21. `root.iter("item")` traverses entire tree including nested items; should scope to `channel`
**File:** `hydra.py:156`

### M22. `parsedate_to_datetime` naive datetime subtraction with tz-aware `now` raises `TypeError`, silently swallowed
**File:** `hydra.py:230`

### M23. Filter bugs

- **M23a.** "Filtered N→M" count computed after `max_results` truncation; user-visible count wrong. (`filter.py:474`)
- **M23b.** min/max size unit mismatch (MB vs GB). (`filter.py:443`)
- **M23c.** Size filter skipped when size is falsy; 0-byte results bypass constraints. (`filter.py:441`)
- **M23d.** All metadata filters short-circuit on empty parsed metadata; unparseable releases bypass quality filters. (`filter.py:406`)
- **M23e.** Preferred/include `release_group` setting is never enforced as a hard filter; only exclude list is checked. (`filter.py:436`)
- **M23f.** Redundant `.lower()` in list comprehension — `settings["exclude_release_group"]` is already lowercased at line 327. (`filter.py:436-438`)

### M24. WebDAV silent fallback to `nzbdav_url` hits wrong server with WebDAV creds
**File:** `webdav.py:316-327`

### M25. `find_video_file` catches all; 401/403/5xx all collapse to "not found"
**File:** `webdav.py:174`

### M26. `request_path` encoded vs decoded hrefs cause recursive PROPFIND until `_depth > 2`
**File:** `webdav.py:262`

### M27. No scheme enforcement on URL settings; `http://` sends basic-auth/apikey cleartext
**Files:** `webdav.py:20-32`, `211-213`

### M28. `/search` success path calls `endOfDirectory(handle, succeeded=False)` even on successful playback
**File:** `router.py:504-510`

### M29. `parse_params` without `keep_blank_values=True` drops blank params; duplicates silently discarded
**File:** `router.py:25-34`

### M30. Season-0 specials silently dropped by `if il_s and il_s not in ("","-1","0")`
**File:** `router.py:229-232`

### M31. `_test_*_connection` builds `?apikey={}` via raw format; urllib error containing apikey surfaces via notify
**Files:** `router.py:606`, `635`

### M32. Cache bugs

- **M32a.** Size accounting uses logical file size not block size; 2-10x over the 50 MB limit possible. (`cache.py:91`)
- **M32b.** LRU by mtime but `get_cached` never utimes; it's oldest-written, not LRU. (`cache.py:95`)
- **M32c.** TOCTOU on `exists → open → load`; concurrent writers race. (`cache.py:45-50`)
- **M32d.** Corrupt cache missing timestamp treated expired but never deleted. (`cache.py:51`)
- **M32e.** `_cache_key` truncates to 200 chars after sanitizing; distinct queries with same prefix collide. (`cache.py:32`)
- **M32f.** Sanitization lossy: `"Foo Bar"`, `"Foo-Bar"`, `"Foo.Bar"`, `"Foo:Bar"`, `"Foo/Bar"` all map to the same key. (`cache.py:31`)

### M33. `proxy.start()` → immediate `setProperty(port)` with no bind-complete guarantee
**File:** `service.py:282-284`

### M34. Service-scoped window properties not cleared on startup; stale state from prior session survives
**File:** `service.py:273-297`

### M35. 5s "playback never started" detector uses `time.time()` not monotonic
**File:** `service.py:223`

### M36. `proxy.stop()` has no exception guard; failure leaves stale `_PROP_PROXY_PORT`
**File:** `service.py:295`

### M37. `_retry_playback` called from `tick` while Player callback may still be in flight; no sync
**File:** `service.py:190-208`

### M38. Cross-process IPC writes URL/TITLE/ACTIVE as three independent `setProperty` calls; no atomicity
**Files:** `resolver.py:286-300` vs `service.py:101-111`

### M39. `install_player()` unconditionally overwrites `nzbdav.json`; hand-edits lost
**File:** `player_installer.py:51-54`

### M40. `_FALLBACK_STRINGS` missing ids 30121/30115/30116 (service.py) and 30054/30055 (router.py); blank dialogs when `strings.po` unavailable
**File:** `i18n.py:9`

### M41. `history["status"]` access without null check
**File:** `resolver.py:570` (same pattern at line 583) — `history` checked for truthiness, but empty dict `{}` passes truthiness then raises `KeyError`.

### M42. `clear_cache()` calls `os.listdir()` on non-existent directory
**File:** `cache.py:116` — Raises `FileNotFoundError` if cache directory was never created.

### M43. `clear_cache()` / `_evict_oldest()` race condition
**File:** `cache.py:91-108` — Total size computed, then files deleted; another thread/process could modify files in between.

### M44. Year validation upper bound is stale — time bomb
**File:** `filter.py:674` — `if 1920 <= yr <= 2030` rejects valid 2031+ releases.

### M45. Suffix range with value > content_length produces negative start offset
**File:** `stream_proxy.py:831-832`

### M46. `validate_stream` dead code: `e.code in (200, 206)` in `HTTPError` branch
**File:** `webdav.py:386` — `HTTPError` is only raised for 4xx/5xx; 200/206 never raise it.

### M47. `setResolvedUrl` called then window properties set after — race condition
**File:** `resolver.py:282-288` — If service's `tick()` fires between these, it reads stale window properties.

### M48. `_play_via_proxy` plays with `li.getPath()` which may differ from intended URL
**File:** `resolver.py:347` (same pattern at line 359)

### M49. File open without encoding specification
**File:** `cache.py:49`, `71` — On non-UTF-8 systems, could fail to read JSON cache files.

### M50. `_lookup_episode_info` uses undocumented IMDB API endpoint
**File:** `router.py:154` (same URL at line 569) — `https://v2.sg.media-imdb.com/suggestion/t/{}.json` — if IMDB removes this, episode lookups silently fail.

---

## LOW

### L1. User-cancel never DELETEs `nzo_id`; cancelled jobs accumulate in queue
**File:** `resolver.py:529-534`

### L2. `_validate_stream_url` doesn't catch `http.client.HTTPException` subclasses
**File:** `resolver.py:51`

### L3. `history["storage"]` bare key access raises `KeyError` on partial responses
**File:** `resolver.py:584`

### L4. `_cache_bust_url` appends query after fragment, invisible to servers on `#`-bearing URLs
**File:** `resolver.py:91`

### L5. `_apply_proxy_mime()` only sets duration on remux; pass-through/faststart branches skip it
**File:** `resolver.py:218-227`

### L6. `probe_end = min(target+1023, range_end)` produces 1-byte probe at exact boundary
**File:** `stream_proxy.py:781`

### L7. Skip tier never retried smaller after advancing
**File:** `stream_proxy.py:777-815`

### L8. Legacy `stream_context` field never cleared on eviction
**Files:** `stream_proxy.py:849`, `962`

### L9. Concurrent legacy-fallback clients collide on shared `ctx`
**File:** `stream_proxy.py:194-195`

### L10. `_parse_ffmpeg_duration` requires fractional seconds; localized builds without them disable seek
**File:** `stream_proxy.py:144-158`

### L11. `RangeCache` instantiated per session but never read; dead code
**File:** `stream_proxy.py:1031`

### L12. Faststart temp file leak if `_embed_auth_in_url` raises before `try`
**File:** `stream_proxy.py:1209-1213`

### L13. `os.path.getsize()` race between `_prepare_tempfile_faststart` return and `_register_session`
**File:** `stream_proxy.py:1082`

### L14. Bare `except Exception` around subtitle-setting silently drops all subs
**File:** `stream_proxy.py:333-340`

### L15. `_resolve_seek` uses linear byte-to-seconds map; visible seek drift
**File:** `stream_proxy.py:502-512`

### L16. `{:.1f}.format(None)` in `_resolve_seek` fragile if duration unset
**File:** `stream_proxy.py:525-530`

### L17. `.get("history", {}).get("slots", [])` breaks on `"history": null`
**File:** `nzbdav_api.py:116+`

### L18. Fallback HDR regex maps bare "HDR" to HDR10 but could be HLG
**File:** `filter.py:631`

### L19. Fallback group regex truncates groups with hyphens or underscores
**File:** `filter.py:682`

### L20. Size kept as raw string; `int(size)` crashes on malformed attr
**File:** `hydra.py:174-186`

### L21. `int(argv[1])` raises if Kodi calls script entry without numeric handle
**File:** `router.py:39-41`

### L22. Hardcoded English in connection-test `notify()` calls
**Files:** `router.py:603`, `610`, `612`, `614`, `632`, `639`, `641`, `643`

### L23. `f.write()` return value not checked; partial writes reported as success
**File:** `player_installer.py:52`

### L24. `os.makedirs` without `exist_ok=True` races with concurrent first-call
**File:** `cache.py:22`

### L25. `fmt()` calls `str.format` with no `try/except`; crashes on missing string
**File:** `i18n.py:67`

### L26. Hardcoded English in retry `notify()` calls
**File:** `playback_monitor.py:88-92`, `155-157`

### L27. `http_get` no status check; non-UTF-8 raises `UnicodeDecodeError`
**File:** `http_util.py:24-26`

### L28. No User-Agent; some Cloudflare-protected upstreams 403 `Python-urllib/...`
**File:** `http_util.py:24`

### L29. `_check_active()` clears only `_PROP_ACTIVE`; stale URL/title persist
**File:** `service.py:111`

### L30. Second `xbmc.Monitor()` instance unnecessary
**File:** `service.py:78`

### L31. `sys.path.insert` without dedup check
**File:** `addon.py:11`

### L32. Unicode lightning bolt without fallback — may not render on all Kodi skins
**File:** `results_dialog.py:141`

### L33. `bytes(data)` creates copy on every iteration in `_rewrite_offsets_recursive`
**File:** `mp4_parser.py:136`

### L34. Docstring after early return — no-op string literal
**File:** `ptt/adult.py:27-29`

### L35. Mutable default argument `extend_options(options: Dict[str, Any] = {})`
**File:** `ptt/parse.py:92`, `ptt/handlers.py`

### L36. Playlist cleared after `setResolvedUrl` — could interfere with queued items
**File:** `resolver.py:662-663`, `679-680`, `684-685`

### L37. Redundant empty string check `if not path or path == ""`
**File:** `router.py:20-21`

### L38. WebDAV error type only set when both queue and history are `None`
**File:** `resolver.py:429`

---

## UX Improvement Opportunities

1. **Empty/None results:** Present a non-modal toast/notification with reason and "Try again / Change filters" CTA instead of opening a blank modal dialog.
2. **Sorting controls:** Expose sort order (relevance/date/size/seeders) and reflect the active choice in the header instead of a fixed "Sorted" label.
3. **Counts and pagination:** Show "Showing X of Y (page P/N)" rather than conflicting count vs. filtered/total labels; add a pager or "Load more."
4. **Information density:** Align columns, ensure consistent color coding for resolution/quality, add language/subtitle badges, truncate long filenames with expand affordance.
5. **Selection confidence:** Add preview/metadata pane on focus (size, age, indexer trust score, history status) and a clear "Already downloaded" state.
6. **Failure transparency:** Show inline validation errors before calling Hydra (e.g., missing title/season/episode); avoid silent empty dialogs.

---

## Top Priority Themes

| # | Theme | Findings | Key Files |
|---|-------|:---:|-----------|
| 1 | Router handle-hang bugs | 6 | `router.py` |
| 2 | Stream proxy session/lifetime races | 7+ | `stream_proxy.py` |
| 3 | Credential leaks (ffmpeg argv + redact_url gaps) | 6 | `stream_proxy.py`, `http_util.py`, `hydra.py`, `nzbdav_api.py` |
| 4 | mp4_parser unvalidated box sizes | 6 | `mp4_parser.py` |
| 5 | Broad exception catches masking errors | 4 | `nzbdav_api.py`, `hydra.py`, `stream_proxy.py` |
| 6 | Cache race conditions and correctness | 6 | `cache.py` |
| 7 | Service/player thread-safety | 4 | `service.py` |

**Totals:** 0 Critical · 19 High · 50 Medium · 38 Low · 6 UX improvements = **117 findings**
