# BUG2.MD — 20-Agent Code Review of feat/dv-rpu-source-extraction (PR #84)

Orchestrator summary of findings from 20 parallel focused code-reviewer agents.
Branch: `feat/dv-rpu-source-extraction`. Commit: `bc42b24`.

**Scope:** DV RPU source-extraction work (dv_rpu.py + dv_source.py + stream_proxy
integration + tests + fixtures).

**Headline:** parser correctness is solid (bit-for-bit match vs. dovi_tool across
header, mapping, NLQ, MEL detection). The risks are on the I/O / container /
integration seams, plus one legal doc gap.

---

## P0 — Merge-blockers

### P0.1 — Attacker-controlled 4 GiB allocation via stsz first_sample_size
**File:** `plugin.video.nzbdav/resources/lib/dv_source.py:135-148`
**Reviewer:** security (#16), confirmed by HTTP review (#10)
**Severity:** Major / Security

`_extract_mp4_first_sample` reads `first_sample_size` from stsz as a 32-bit
big-endian uint. A malicious WebDAV response (compromised nzbdav or
attacker-authored NZB) can set this to `0xFFFFFFFF`, after which
`_http_range(url, chunk_offset, chunk_offset + first_sample_size - 1)` calls
`resp.read()` with **no size cap**. On 32-bit Kodi (CoreELEC target), a 4 GiB
allocation exhausts address space and crashes Kodi.

**Fix:** clamp `first_sample_size` to a sane ceiling (e.g. 16 MiB — larger
than any real HEVC access unit) and return `dv_unknown` if exceeded. Apply the
same ceiling inside `_http_range` via `resp.read(cap)`.

### P0.2 — Unbounded response read when server ignores Range
**File:** `plugin.video.nzbdav/resources/lib/dv_source.py:27-33`
**Reviewer:** HTTP/auth/timeouts (#10)
**Severity:** Major / Security

A WebDAV server that returns **200 OK instead of 206 Partial Content** (not
spec-compliant but observed in the wild) ships the whole body. For a 50 GB
MKV source, `_first_bytes(size=2*1024*1024)` calls `resp.read()` and receives
all 50 GB. Same OOM/crash vector as P0.1 through a different door.

**Fix:** call `resp.read(size)` with an explicit cap in both `_http_range`
and `_first_bytes`. Optionally assert `resp.status == 206` and bail otherwise.

### P0.3 — `_vint_width` IndexError on all-zero first byte
**File:** `plugin.video.nzbdav/resources/lib/dv_source.py:186-192` (and
callers at `:197`, `:206`)
**Reviewer:** MKV EBML (#8)
**Severity:** Critical (latent — reaches `_probe_mkv` except clause)

When `first_byte == 0x00`, the loop runs 8 iterations without finding a
length-descriptor bit, returns `width=9`. Callers then read `data[offset+i]`
for `i` up to 8, which is one byte past the VINT. For zero-padded or
malformed MKV headers this either raises IndexError (contained by
`_probe_mkv`'s except, which converts to `dv_unknown`) or silently returns
garbage IDs/sizes that then get yielded from `_iter_ebml` and misinterpreted.

**Fix:** cap width at 8 and raise `ValueError("invalid EBML VINT")` if no
bit found.

### P0.4 — `_iter_ebml` does not bound payload against parent end
**File:** `plugin.video.nzbdav/resources/lib/dv_source.py:213-223`
**Reviewer:** MKV EBML (#8)
**Severity:** Critical (latent)

`payload_end = payload_start + size` is yielded without clamping to the parent
`end`. A malformed child with declared size overrunning the parent:
- Yields offsets past `end` — callers slicing `segment[sub_start:sub_end]`
  silently under-read (Python slice truncation).
- An element with `size=0` at `offset == end-1` steps `_read_element_id` into
  `data[end]` → IndexError.

**Fix:** `payload_end = min(payload_end, end)`; break the loop when
`payload_start > end`.

---

## P1 — Major (should fix before merge)

### P1.1 — file_size not threaded through → moov-at-tail MP4 regression for SDR
**Files:** `plugin.video.nzbdav/resources/lib/stream_proxy.py:3974`,
`plugin.video.nzbdav/resources/lib/dv_source.py:168-169`
**Reviewers:** production file_size (#7), stream_proxy DV integration (#11),
regression risk (#21)
**Severity:** Major (production regression for existing SDR moov-at-tail MP4s)

`probe_dolby_vision_source` is called without `file_size`. `_probe_mp4`
defaults to `1 << 20` (1 MB). `fetch_remote_mp4_layout` uses this as the
file's EOF, so `_find_moov_by_tail_probe` only scans the first 1 MB — it
cannot find moov for any real MP4 with moov-at-tail (the common UHD-remuxer
layout from mkvtoolnix/MP4Box without `-faststart`).

**Regression on main:** SDR moov-at-tail MP4 files previously went through
fmp4 (ffmpeg-stderr probe returned "no DV") with full seek. Now they return
`dv_unknown` → matroska, losing fmp4 seek.

**Fix (one-liner):** at `stream_proxy.py:3974`, fetch content length and pass:
```python
dv_file_size = self._get_content_length(remote_url, auth_header)
dv_result = probe_dolby_vision_source(remote_url, auth_header,
                                       file_size=dv_file_size)
```
Guard with `if dv_file_size:` to preserve current behavior if length fetch fails.

### P1.2 — Missing co64 (64-bit chunk offset) support
**File:** `plugin.video.nzbdav/resources/lib/dv_source.py:127-148`
**Reviewers:** MP4 box walk (#6), test coverage gaps (#13)
**Severity:** Major

Real-world DV UHD files > 4 GiB use the `co64` box (64-bit chunk offsets),
not `stco`. `_extract_mp4_first_sample` only searches for `stco` and
returns None if absent → `dv_unknown` → matroska. For the exact files
this routing targets, the probe is inert.

**Fix:** try `stco` first, fall back to `co64` with `struct.unpack_from(">Q",
moov, co64_body_start + 8)`.

### P1.3 — No try/except around `probe_dolby_vision_source` call
**File:** `plugin.video.nzbdav/resources/lib/stream_proxy.py:3974`
**Reviewer:** stream_proxy DV integration (#11)
**Severity:** Major

The probe swallows most exceptions internally, but `http.client.InvalidURL`,
`ssl.SSLError`, `UnicodeEncodeError` (URL with `\x00`), and `RuntimeError`
can still escape. A probe crash kills `prepare_stream`, which kills
playback.

**Fix:** wrap the call in `try/except Exception` that logs the traceback and
returns a synthetic `DolbyVisionSourceResult("dv_unknown", "probe_crashed")`
to keep the control path alive.

### P1.4 — dv_source.py module docstring contradicts actual routing
**File:** `plugin.video.nzbdav/resources/lib/dv_source.py:1-9`
**Reviewer:** doc consistency (#20)
**Severity:** Major (misleading docs)

Docstring claims "profile 8 / profile 5 / non-DV / profile 7 MEL stay on
fmp4". Actual routing: P5 and P8 → matroska (per Option 3). Opposite of
reality.

**Fix:** rewrite docstring to match the matrix in `stream_proxy.py:3951-3973`.

### P1.5 — CHANGELOG.md and PROXY.md have stale references
**Files:** `CHANGELOG.md` (lines 57, 88), `PROXY.md` (lines 30, 264, 276)
**Reviewers:** dead code (#12), doc consistency (#20)
**Severity:** Major (misleading docs)

CHANGELOG still advertises `_probe_dv_profile` and `_parse_ffmpeg_dv_profile`
as features. PROXY.md documents the old "Profiles 5 and 8 allowed through
fmp4" gate and lists `_parse_ffmpeg_dv_profile` as a stream_proxy member.

**Fix:** update both docs to describe the new source-data probe and the
Option-3 routing matrix.

---

## P2 — Important (post-merge acceptable but should land soon)

### P2.1 — MIT attribution missing for vendored fixtures
**Path:** `tests/fixtures/dovi/`
**Reviewer:** fixtures (#15)
**Severity:** Important (license compliance)

The three binary fixtures (`fel_orig.bin`, `mel_orig.bin`, `profile8.bin`)
are vendored byte-for-byte from `quietvoid/dovi_tool@HEAD:assets/tests/`
(verified via `git hash-object` match). dovi_tool is MIT-licensed;
redistribution requires that the copyright and permission notice accompany
the files.

**Fix:** add `tests/fixtures/dovi/README.md` with:
- Upstream source URL
- Pinned upstream commit SHA
- Verbatim dovi_tool MIT LICENSE text or a pointer + attribution

### P2.2 — `_probe_mp4`/`_probe_mkv` swallow errors silently
**File:** `plugin.video.nzbdav/resources/lib/dv_source.py:172-173, 274-276`
**Reviewer:** code quality (#19)
**Severity:** Important (operability)

Exceptions inside `_extract_mp4_first_sample` / `_extract_mkv_first_sample`
return `dv_unknown` with no log. When a user reports "why did my DV P8 stream
go to matroska?", there's nothing in the log to diagnose.

**Fix:** add `xbmc.log("NZB-DAV: DV probe MP4 extraction failed: {!r}".format(e),
xbmc.LOGDEBUG)` before each fallback return.

### P2.3 — `_validate_url` + CRLF strip not applied to probe URL/auth
**Files:** `plugin.video.nzbdav/resources/lib/dv_source.py:27-33`
**Reviewer:** HTTP/auth/timeouts (#10)
**Severity:** Important (defense-in-depth)

`_http_range` passes the URL to `urlopen` and `auth_header` to
`req.add_header` with no sanitization. Current callers upstream validate,
but a future refactor could bypass.

**Fix:** call `stream_proxy._validate_url(url)` and
`auth_header.replace("\r", "").replace("\n", "")` at the top of `_http_range`.

### P2.4 — MEL field validation breadth is thin (only 1 MEL fixture)
**Files:** `tests/test_dv_rpu.py`, `tests/fixtures/dovi/mel_orig.bin`
**Reviewers:** MEL/FEL classifier (#4), regression risk (#21)
**Severity:** Important (soak risk)

`is_mel()` matches a strict zero/one NLQ sentinel. The rule is correct per
dovi_tool but validated against only ONE MEL fixture and one FEL. If a real
encoder produces MEL that differs slightly (or an unusual FEL happens to zero
one field and is misclassified as MEL), the result is wrong routing.

**Mitigations (pick one):**
- Gate MEL→fmp4 behind a user setting that defaults to matroska (belt-and-suspenders
  while field data accumulates).
- Route MEL→matroska until a confirmed MEL playback is logged on the real
  device; then flip.
- Add more fixtures (public Dolby sample clips) and re-validate.

### P2.5 — SimpleBlock lacing not handled
**File:** `plugin.video.nzbdav/resources/lib/dv_source.py:265-266`
**Reviewer:** MKV EBML (#8)
**Severity:** Important (correctness hole, latent for DV content)

`block[width + 3:]` assumes no lacing. If the SimpleBlock flags byte has
lacing bits set (Xiph/EBML/fixed), the return value begins with a lace-count
byte, lace sizes, then frames — NOT a NAL. The parser would read garbage.

HEVC muxes from mkvmerge/dovi_tool don't lace video, so this is latent for
current DV content, but should be defended.

**Fix (minimum):** `flags = block[width + 2]; lacing = (flags >> 1) & 0x03;
if lacing != 0: return None`.

### P2.6 — BlockGroup / Block (0xA0 / 0xA1) not handled
**File:** `plugin.video.nzbdav/resources/lib/dv_source.py:258-259`
**Reviewer:** MKV EBML (#8)
**Severity:** Important (correctness hole for non-mkvmerge mux)

Only SimpleBlock (0xA3) is checked. Files muxed with older tools using
BlockGroup wrappers would miss the first video frame → `no_rpu_nal_found` →
`non_dv` false negative → fmp4 path for DV content (could hang CAMLCodec).

**Fix:** descend into BlockGroup (0xA0) children and handle Block (0xA1).

### P2.7 — No log of the probe's structured result at the integration point
**File:** `plugin.video.nzbdav/resources/lib/stream_proxy.py:3974-4024`
**Reviewers:** code quality (#19), stream_proxy DV integration (#11)
**Severity:** Important (operability)

Existing log lines describe the routing decision but not the raw DV probe
result. For field debugging, log the full `dv_result` (classification, reason,
profile, el_type) at LOGDEBUG right after the probe call.

### P2.8 — `use_prev_vdr_rpu_flag` propagation
**File:** `plugin.video.nzbdav/resources/lib/dv_rpu.py:357-358`
**Reviewer:** MEL/FEL classifier (#4)
**Severity:** Important (edge case)

`parse_rpu_payload` returns `DolbyVisionRpuInfo(profile=7, el_type=None)` for
prev-RPU frames (they legitimately carry no NLQ). Callers who sample only
the first frame might land on one of these and misclassify (the probe picks
the FIRST video sample — not guaranteed to be a keyframe RPU).

**Mitigations:**
- Iterate to the next NAL if first UNSPEC62 returns `el_type=None` but
  `profile=7` (retry within the same sample or advance to next sample).
- Document the invariant loudly in the dv_rpu docstring so readers know.

### P2.9 — dv_rpu.py missing upstream version pin
**File:** `plugin.video.nzbdav/resources/lib/dv_rpu.py:1-9`
**Reviewer:** doc consistency (#20), fixtures (#15)
**Severity:** Important (maintenance)

The docstring references `quietvoid/dovi_tool` but no commit/tag. The RPU
parser has evolved; a drifting port with no anchor is a maintenance trap.

**Fix:** add "ported from quietvoid/dovi_tool@<SHA>, re-verify periodically"
to the module docstring.

---

## P3 — Minor / Nit

| # | Finding | File | Reviewer |
|---|---------|------|----------|
| P3.1 | Hardcoded `nal_length_size=4`; hvcC `lengthSizeMinusOne` not consulted (1 and 2 are legal but rare). | `dv_source.py:60-73` | NAL extraction (#9) |
| P3.2 | UNSPEC62 check `(nal[0] >> 1) == 62` — strictly should be `& 0x3f == 62`. Works in practice. | `dv_source.py:77-80` | NAL extraction (#9) |
| P3.3 | EBML magic numbers inline (0x18538067, 0x1654AE6B, 0xAE, 0xD7, 0x86, 0x1F43B675, 0xA3). Named constants would improve readability. | `dv_source.py:234-261` | code quality (#19) |
| P3.4 | `DolbyVisionRpuInfo.el_type: str = None` etc. should be `Optional[str]` for type-checker cleanliness. | `dv_rpu.py:21`, `dv_source.py:22-24` | code quality (#19) |
| P3.5 | Missing Args/Returns/Raises on public docstrings (`parse_rpu_payload`, `parse_unspec62_nalu`, `probe_dolby_vision_source`). | `dv_rpu.py:346-365`, `dv_source.py:289-302` | code quality (#19) |
| P3.6 | Field naming drift from upstream: Python `el_bit_depth_and_ext` vs Rust's split into `el_bit_depth_minus8 & 0xFF` + `ext_mapping_idc_*`. | `dv_rpu.py:192` | header parse (#2) |
| P3.7 | `_find_first_video_track` returns the stbl tuple, not a track. Name is misleading; rename to `_find_first_video_stbl`. | `dv_source.py:83-113` | code quality (#19) |
| P3.8 | `read_var` is a pure alias for `read_bits`. Add a one-line comment stating "alias for dovi_tool naming parity". | `dv_rpu.py:55-56` | bit reader (#1) |
| P3.9 | MEL LOGINFO message uses em-dash; some CoreELEC log viewers mangle non-ASCII. | `stream_proxy.py:3988-3995` | stream_proxy integration (#11) |
| P3.10 | `NotImplementedError` from polynomial linear-interp branch propagates instead of degrading to `profile=7, el_type=None`. Production DV content never hits this, but hardening is cheap. | `dv_rpu.py:238` | mapping+NLQ (#3), MEL/FEL (#4) |
| P3.11 | `len(data) < 7` gate in `_validated_rpu_payload` is arbitrary (dovi_tool defers size validation). Harmless. | `dv_rpu.py:125` | emulation prevention (#5) |
| P3.12 | `_mock_urlopen_from_file` doesn't handle open-ended `bytes=N-` ranges; future production refactor could break tests. | `tests/test_dv_source.py:59-71` | test mock quality (#14) |

---

## P4 — Test coverage gaps (add these tests)

Prioritized from test coverage review (#13) and regression risk (#21).

### Priority — should land with this PR or immediately after
1. **co64 MP4 fixture** — verify 64-bit chunk offset probe once P1.2 is fixed.
2. **moov-at-tail MP4 fixture** — verify probe works on the most common UHD layout, once P1.1 is fixed.
3. **Network error path in dv_source** — `urlopen` raising URLError → `dv_unknown`.
4. **Profile 5 fixture** — validates real `get_dovi_profile() == 5` code path (only mocked today).
5. **stream_proxy probe-crash test** — `probe_dolby_vision_source` raising an unexpected exception routes to matroska, doesn't kill playback (covers P1.3).

### Parser robustness
6. Malformed RPU — truncated payload, wrong `rpu_type`, bad prefix.
7. `coefficient_data_type == 1` path (no fixture exercises this today).
8. Emulation-prevention bytes mid-payload (not just at header boundary).
9. `use_prev_vdr_rpu_flag=True` → `profile=7, el_type=None`.
10. `disable_residual_flag=True` for profile 7 → `has_nlq=False`.

### Container robustness
11. Multi-track MP4 (audio first, video second).
12. MKV Cluster-before-Tracks ordering.
13. MKV SimpleBlock lacing.
14. MKV BlockGroup + Block.
15. Multiple Clusters; iterate until a video SimpleBlock is found.
16. Unsupported extensions (.ts, .webm, .avi) → `unsupported_container`.

### Integration
17. Real-DV-clip integration test — Dolby's public P5/P7/P8 short clips under `@pytest.mark.integration`.
18. Cross-check against live `dovi_tool info --frame 0` — compare parsed fields to the JSON, guards against upstream drift.

---

## Findings that are NOT bugs

Explicitly cleared by the review — no action needed.

| Area | Reviewer | Result |
|------|----------|--------|
| Bit reader MSB-first + exp-Golomb | #1 | Correct, matches HEVC/dovi_tool |
| RPU header field-by-field parse | #2 | Bit-for-bit match to dovi_tool |
| Mapping + NLQ parse | #3 | Bit-for-bit match to dovi_tool |
| MEL is_mel signature | #4 | Matches dovi_tool's convert_to_mel |
| Emulation prevention stripping | #5 | Correct; termination bounded |
| RPU wrapper stripping termination | #5 | Iterative, bounded by input length |
| Routing matrix correctness | #18 | All 8 cases trace correctly |
| elif ordering (MEL before generic) | #11, #18 | Correct |
| Log level choices | #11, #18 | Appropriate SNR |
| Threading / concurrency model | #17 | Worker thread, thread-safe, **latency improvement** vs ffmpeg probe |
| auth_header threading through ctx | #11 | Consistent |
| No dead code in stream_proxy.py | #12 | Clean; only stale PROXY.md refs remain |
| Python 3.8 compatibility | #19 | No incompatible syntax |
| Test mock isolation | #14 | Per-test closures, no state leak |
| Synthetic MP4/MKV fixtures genuinely exercise the parser | #14 | Not vacuously passing |
| Fixture provenance | #15 | Byte-for-byte match to quietvoid/dovi_tool@HEAD |
| SSRF via URL scheme/host | #16 | URL validated upstream in stream_proxy |

---

## Recommended disposition

**Block merge on:** P0.1, P0.2, P0.3, P0.4, P1.4, P1.5 (the two docstring
regressions are trivial fixes and shouldn't ship).

**Strongly recommend before merge:** P1.1, P1.2, P1.3.

**Can ship and follow up:** all P2, P3, P4.

The parser port itself is correct. The risk surface is entirely in the I/O
seams (HTTP safety, MP4 co64, MKV malformation tolerance) and documentation.
The biggest user-visible regression risk (P1.1) is a one-line fix.
