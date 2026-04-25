# TODO — NZB-DAV Kodi Addon

Single source of truth for outstanding integration, rollout, gated epic work, stream-proxy architecture reference, DV plan, and fix-verification history. Completed work lives in `git log`.

## Status at a glance

| | |
|---|---|
| **Next action** | Install the staged zip on the CoreELEC box and run the §F.1 smoke. |
| **Blocked on** | CoreELEC smoke pass — gates §F.2, §F.3, and the §A.4/§A.5 epics. |
| **Active priority** | P0: §F.1 smoke. P1: §F.2 `send_200_no_range` validation. P2: §F.3 soak. |
| **Last reviewed** | 2026-04-24 |

Jump to: **[§A.1 Active Worklist](#a1-active-worklist)** · [Part F Playbooks](#part-f--rollout-playbooks) · [Glossary](#glossary)

## Table of Contents

| Part | Contents | When to read |
|---|---|---|
| [A](#part-a--outstanding-proxy-rollout-work-todomd) | Outstanding proxy rollout work | Always — this is the worklist |
| [B](#part-b--panicoreelec-dolby-vision-source-fix-todo_panimd) | PANI/CoreELEC Dolby Vision source fix | When touching `../piXBMC` or `../piCoreElec` C++ |
| [C](#part-c--stream-proxy-architecture-reference) | Stream proxy architecture pointer | When debugging proxy behaviour |
| [D](#part-d--dolby-vision-seek--scrub-plan-dvmd) | Dolby Vision seek/scrub plan | When implementing seek or DV routing |
| [E](#part-e--fix-verification-record-bug2md) | Fix verification record (BUG2.MD) | When reviewing 20-agent remediation |
| [F](#part-f--rollout-playbooks) | Rollout playbooks (smoke, send_200, soak) | When executing a §A.1 P0/P1/P2 task |
| [H](#part-h--static-analysis-audit-trail) | Static-analysis audit trail (was `ISSUE_REPORT.md`, `QA_SCAN_20260424.md`, `BUGS3.md`) | When triaging audit findings or planning a fix |

## Glossary

| Term | Meaning |
|---|---|
| **DV** | Dolby Vision |
| **RPU** | Dolby Vision Reference Processing Unit (NAL UNSPEC62 payload) |
| **P5 / P7 / P8** | DV profiles — 5 = HEVC+RPU single-layer; 7 = dual-layer BL+EL+RPU; 8.1 = cross-compatible single-layer |
| **FEL / MEL** | P7 Full / Minimal Enhancement Layer variants |
| **HEVC** | H.265 video codec |
| **CMAF** | Common Media Application Format (fragmented MP4 delivery) |
| **ISA** | `inputstream.adaptive` Kodi addon |
| **CAMLCodec** | CoreELEC Amlogic hardware codec interface |
| **nzbdav** / **nzbdav-rs** | upstream WebDAV + SABnzbd-compat backend |
| **PTT** | parse-torrent-title (vendored in `resources/lib/ptt/`) |
| **R9** | `avdvplus` CoreELEC build revision 9 (2026-03-23) |

<details>
<summary>Consolidation history — click to expand</summary>

Originally merged on 2026-04-23 from five separate docs; last updated 2026-04-24 after a 100-principal-engineer review pass. The five source files were collapsed into this single file and removed from the tree:

- `docs/TODO.md` — proxy rollout worklist (now Part A)
- `docs/TODO_PANI.md` — Dolby Vision source-fix plan for `../piXBMC` and `../piCoreElec` (now Part B)
- `PROXY.md` — stream proxy architecture reference (now Part C)
- `DV.md` — Dolby Vision seek/scrub synthesis from 10 parallel research agents (now Part D)
- `docs/BUG2.MD` — P0/P1/P2/P3 fix verification record (now Part E)

A second consolidation pass on 2026-04-24 absorbed three audit files:

- `ISSUE_REPORT.md` — three-audit consolidation, ~117 findings (now §H.2)
- `QA_SCAN_20260424.md` — 100-agent breadth scan, ~80 findings (now §H.3)
- `BUGS3.md` — 50-agent depth follow-up, 36 findings (now §H.4)

Edit this file directly. Do not re-fork sections into separate files; add new work under the appropriate Part (or create a Part I if nothing fits).

</details>

---

## Part A — Outstanding Proxy Rollout Work (`TODO.md`)

**Remaining proxy work only.** Completed work is in `git log` — PR-1 merge `16e7122` (2026-04-22), 20-agent review remediation `4103f5d`. Dolby Vision / PANI source work lives in Part B of this file (originally a standalone `TODO_PANI.md`).

---

### A.1 Active Worklist

Only the remaining work. Completed implementation belongs in `git log`.

| Pri | Item | Est | Depends | Plan |
|:---:|---|:---:|:---:|:---:|
| **P0** | Install `plugin.video.nzbdav-1.0.0-pre-alpha.zip` on the CoreELEC box via Kodi → Add-ons → Install from zip (zip already at `/storage/` on the box) | ~2 min | — | §F.1 step 3 |
| **P0** | Run CoreELEC smoke validation on a clean-article release (2 h, four seeks, audio sync check) | ~2 h | zip installed | §F.1 |
| **P1** | Validate `send_200_no_range=ON` on CoreELEC before ever enabling that flag | ~1 h | smoke passed | §F.2 |
| **P2** | Start and track the ≥1 week observability soak post-merge | 7+ days | smoke passed | §F.3 |
| **P2** | Decide whether to flip `strict_contract_mode` from `warn` to `enforce` | ~10 min | soak complete | §F.3.5 |
| **P2** | Decide whether to enable `density_breaker_enabled` | ~10 min | soak complete | §F.3.5 |

#### Gated

- Article-health filter epic (§A.4) stays blocked until the post-merge observability soak completes.
- nzbdav-rs NNTP retry / timeout tuning epic (§A.5) uses the same gate as §A.4.

#### Housekeeping

- [ ] Delete or archive `scripts/review-prompts/proxy-review.md` if it is no longer needed after integration. (Updated to reusable template on 2026-04-22; keep if any `REMAINING_*.md` or `PROXY_EPIC_*.md` will be reviewed.)
- [x] Obsolete proxy planning mirrors removed (`plans/PROXY_REMEDIATION.md`, `plans/PROXY_EXECUTION.md`, `plans/PROXY_ADJUDICATION.md`).

---

### A.2 Out-of-Scope: DV Work

Dolby Vision source-level fixes in `../piXBMC` and `../piCoreElec` are tracked in Part B of this file. They are intentionally separate from the proxy rollout and validation work here.

---

### A.3 Remaining Integration & Rollout Gates

#### A.3.1 Post-merge soak gates

- Collect ≥1 week of observability data before considering:
  - `strict_contract_mode = enforce`
  - `density_breaker_enabled = true`
  - starting §A.4 or §A.5

#### A.3.2 Still-manual acceptance items

- The PR-2 acceptance gate comparing zero-fill behavior on a synthetic short-read fixture remains a manual regression run, not an automated unit test.
- CoreELEC hardware validation is still the deciding signal for `send_200_no_range`.

---

### A.4 Epic — Article-Health Pre-Submit Filter

**Status:** gated epic. Do not start until entry criteria are satisfied.

**Goal:** reduce mid-stream recoveries and zero-fill by filtering unhealthy releases before submit.

#### Entry criteria

- ≥1 week of post-merge observability data showing zero-fill incidents keyed to specific NZBs.
- nzbdav SABnzbd-compat endpoint (or NZBHydra detail endpoint) that exposes per-NZB article-completeness metrics, verified in a local nzbdav-rs build.
- Owner assigned.
- Scope doc drafted and reviewed.

#### High-level design

Query the health endpoint during `_handle_play` / `_handle_search`, then down-rank or hard-filter results below a threshold in `resources/lib/filter.py`.

#### Risks

- False negatives on actually-playable releases.
- Cross-subsystem dependency on nzbdav API surface.
- Threshold calibration remains empirical.

#### Out of scope

- Automatic re-search on filter rejection.
- Health-aware caching.

---

### A.5 Epic — nzbdav-rs NNTP Retry / Timeout Tuning

**Status:** gated epic. Do not start until entry criteria are satisfied.

**Goal:** tune per-provider retry budget, provider priority, and NNTP read timeout using post-merge telemetry.

#### Entry criteria

- ≥1 week of post-merge observability data.
- Coordination with `nzbdav-rs` release cadence.
- Before/after recovery-rate measurement methodology agreed.
- Owner assigned.

#### Risks

- Cross-subsystem scope.
- Over-tuning can either drop healthy requests or prolong dead ones.

#### Out of scope

- Per-user provider selection.
- Dynamic provider ranking based on historical success rate.

---

### A.6 Artifact Inventory

#### Planning

- `TODO.md` — single source of truth for outstanding work + architecture + DV plan + fix-verification history + rollout playbooks (Parts A–F). Completed work lives in `git log`.
- §F.1 — CoreELEC smoke playbook (formerly `junk/plans/REMAINING_COREELEC_SMOKE.md`)
- §F.2 — `send_200_no_range=ON` validation playbook (formerly `junk/plans/REMAINING_SEND_200_VALIDATION.md`)
- §F.3 — observability soak playbook + decision gates (formerly `junk/plans/REMAINING_OBSERVABILITY_SOAK.md`)
- §A.4 — gated Article-Health epic (formerly `junk/plans/PROXY_EPIC_ARTICLE_HEALTH.md`)
- §A.5 — gated NNTP-tuning epic (formerly `junk/plans/PROXY_EPIC_NNTP_TUNING.md`)

#### Proxy source

- `plugin.video.nzbdav/resources/lib/stream_proxy.py`
- `plugin.video.nzbdav/resources/lib/mp4_parser.py`
- `plugin.video.nzbdav/resources/lib/resolver.py`
- `plugin.video.nzbdav/resources/lib/webdav.py`
- `plugin.video.nzbdav/service.py`
- `tests/test_stream_proxy.py`
- `tests/test_mp4_parser.py`
- `tests/test_resolver.py`
- `tests/test_webdav.py`

---

### A.7 Maintenance Conventions

- Keep `TODO.md` as the single source of truth for outstanding integration, rollout, gated epic work, stream-proxy architecture reference, DV plan, and fix-verification history. Completed work goes to `git log`, not a secondary archive file.
- Do not re-fork content into standalone files (the five source docs `docs/TODO.md`, `docs/TODO_PANI.md`, `PROXY.md`, `DV.md`, `docs/BUG2.MD` were consolidated here on 2026-04-24 and removed from the tree; `DONE.md` was merged back into this file on the same round).
- Do not recreate `DV_FIX.md`, `PLAN_FIX_PROXY.md`, `TODO_ALLEN_ASAP.md`, `plans/PROXY_REMEDIATION.md`, `plans/PROXY_EXECUTION.md`, or `plans/PROXY_ADJUDICATION.md`. Their content is preserved in `git log`.
- No artifact paths in `/tmp`.

---

## Part B — PANI/CoreELEC Dolby Vision Source Fix (`TODO_PANI.md`)

> **Relevance:** skip this Part unless you are touching `../piXBMC` or `../piCoreElec` C++ source. This is the upstream HEVC `hvcC → AnnexB` conversion fix track; it is intentionally separate from the addon-side proxy work in Part A and the addon-side DV routing in Part D.

<details>
<summary><b>Expand Part B — source-level DV remediation plan for piXBMC / piCoreElec</b></summary>

TODO for fixing Dolby Vision playback issues in the PANI/CoreELEC codebases:

- `../piXBMC`
- `../piCoreElec`

This section is for source-level DV remediation in those repos. It supersedes the old DV planning references that used to live in `TODO.md` (now Part A).

---

### B.1 Goal

Make Dolby Vision over HLS/fmp4 work on the PANI Amlogic/CoreELEC stack by fixing the HEVC `hvcC -> AnnexB` conversion path in `../piXBMC` and carrying that fix through the `../piCoreElec` build pipeline.

The working theory, based on the current code, is:

- `CBitstreamConverter::Open()` enables HEVC bitstream conversion for `hvcC` extradata in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):544.
- `CBitstreamConverter::BitstreamConvert()` then rewrites the access unit NAL-by-NAL, including the Dolby Vision RPU / EL handling in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1413.
- That rewrite path is the likely source of the DV failure for HLS/fmp4 on Amlogic.

---

### B.2 Technical Context

This is the condensed fact record that step 1 depends on. It replaces the deleted long-form DV background doc.

#### B.2.1 Two Distinct Layers

- Layer 1 is addon/ffmpeg metadata. The archived investigation notes show ffmpeg HLS drops the `dvvC` box from `init.mp4`; without reinjection, `hints.hdrType` stays `HDR_TYPE_NONE`, so the DV-specific Kodi path is never exercised. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md) lines 24-28 and [docs/pannal-xbmc-dv-hls-issue.md](docs/pannal-xbmc-dv-hls-issue.md):35.
- Layer 2 is Kodi bitstream mutation. In the current `../piXBMC` tree, HEVC `hvcC` detection in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):643 still sets `m_convert_bitstream` via `BitstreamConvertInitHEVC()` at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):647. Packets then flow through `BitstreamConvert()` at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1413, which prepends SPS/PPS on IDR at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1477, routes NAL 62 through `ProcessDoViRpu()` at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1498, and forces a 4-byte start code for UNSPEC62 at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1550. The working matroska-style branch is the `m_convert_bitstream == false` passthrough path in `Convert()` at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):758.

#### B.2.2 Why Step 0 Exists

- The archived successful investigation was against pannal forks, not the exact `CoreELEC/xbmc` source line that `../piCoreElec` builds today. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md) lines 10-15.
- Those same notes recorded two patch shapes: a pannal/xbmc variant that used `m_hints.hdrType` directly, and a CoreELEC/xbmc variant that needed a dedicated setter. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md) lines 49-56.
- In the current `../piXBMC` tree, `CBitstreamConverter` still stores `CDVDStreamInfo& m_hints` at [../piXBMC/xbmc/utils/BitstreamConverter.h](../piXBMC/xbmc/utils/BitstreamConverter.h):198, and `DVDVideoCodecAmlogic::Open()` still constructs the converter directly from `m_hints` at [../piXBMC/xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp](../piXBMC/xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp):307. At the same time, `../piCoreElec` still downloads `CoreELEC/xbmc` at [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk):11. That source-fork mismatch is why step 0 is blocking.

#### B.2.3 Dead Ends Already Explored

- `inputstream.ffmpegdirect` was already ruled out: the archived notes say the addon links against ffmpeg 6 while the test box ships ffmpeg 7, so the binary does not load. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md):43.
- MPEG-TS HLS was already ruled out: ffmpeg's muxer cannot write the Dolby Vision descriptor into the PMT. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md):44.
- Reverting to old `CoreELEC/xbmc@ff8ba16` as the Kodi source was already ruled out in the archived investigation because it was tied to ffmpeg 6 APIs while the pannal/CoreELEC toolchain was on ffmpeg 7. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md):45.

---

### B.3 Repos And Hotspots

#### B.3.1 `../piXBMC`

- [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):544
  `Open(bool to_annexb)` — HEVC `hvcC` detection and conversion setup.
- [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):716
  `Convert(uint8_t *pData, int iSize, double pts)` — hot path that dispatches to `BitstreamConvert()`.
- [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1413
  `BitstreamConvert(...)` — current NAL rewrite path.
- [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1375
  `ProcessDoViRpu(...)` — current DV RPU mutation path.
- [../piXBMC/xbmc/utils/BitstreamConverter.h](../piXBMC/xbmc/utils/BitstreamConverter.h):110
  class surface for adding a dedicated DV `hvcC` passthrough mode / helper methods / state.

#### B.3.2 `../piCoreElec`

- [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk):6
  Kodi source and version selection for the Amlogic-ne device build. Current fetch target is `CoreELEC/xbmc`, not `../piXBMC`.
- [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi)
  device-level patch carry path; currently already used for at least one Kodi patch.

---

### B.4 Active TODO

#### B.4.0 Resolve The Source-Fork Strategy First

This is a go/no-go decision. Do not start the actual `piXBMC` code change until it is settled.

- [ ] Choose one development strategy and record it here:
  - **A. Retarget `../piCoreElec` to the `piXBMC` source line** so the implementation and build source match.
  - **B. Develop two variants** if `../piXBMC` and the `CoreELEC/xbmc` tree fetched by `package.mk` are not patch-compatible enough.
  - **C. Implement and test in `../piXBMC`, then port the final delta into the `../piCoreElec` patch carry path as a separate step.**
- [ ] Record why the chosen strategy is safe given that [package.mk](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk):11 currently points at `https://github.com/CoreELEC/xbmc/...`.
- [ ] Treat step 2 below as blocked until this is decided. A patch written blindly against `../piXBMC` may not be portable to the tree `../piCoreElec` actually builds today.

#### B.4.1 Confirm The Exact Broken Path

- [ ] Re-read `Open()` in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):544 and verify the HEVC `hvcC` case still sets `m_convert_bitstream` when `m_to_annexb` is true.
- [ ] Re-read `BitstreamConvert()` in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1413 and confirm the current path:
  - prepends SPS/PPS on first IDR,
  - routes NAL type 62 through `ProcessDoViRpu()`,
  - routes NAL type 63 through the current EL handling,
  - rewrites the whole AU through `BitstreamAllocAndCopy(...)`.
- [ ] Record the exact lines to patch before touching code.

#### B.4.2 Add A Dedicated DV `hvcC` Passthrough Mode In `../piXBMC`

- [ ] Add a dedicated mode / flag / helper set in [../piXBMC/xbmc/utils/BitstreamConverter.h](../piXBMC/xbmc/utils/BitstreamConverter.h):110 for Dolby Vision `hvcC` passthrough.
- [ ] In `Open()`, gate that mode on:
  - codec = HEVC,
  - `to_annexb = true`,
  - extradata is `hvcC`,
  - stream is Dolby Vision.
- [ ] Convert extradata to AnnexB once during `Open()`:
  - emit VPS/SPS/PPS in canonical AnnexB form,
  - store it in converter state for reuse,
  - avoid the IDR SPS/PPS carousel in the hot path.
- [ ] Add a new packet conversion helper that:
  - reads HEVC NAL length fields from the `hvcC` packet,
  - rewrites only the length fields to `00 00 00 01`,
  - copies each NAL body byte-for-byte unchanged,
  - does not mutate DV RPU payloads,
  - does not synthesize EL payload changes.
- [ ] Ensure the new DV passthrough path logs clearly when enabled.

#### B.4.3 Keep Existing Non-DV Behavior Stable

- [ ] Keep the existing path unchanged for:
  - non-DV HEVC,
  - AVC,
  - AnnexB inputs,
  - dual-layer / HDR10+ conversion cases unless intentionally routed through the new mode.
- [ ] Make the new DV `hvcC` passthrough an explicit narrow branch, not a broad rewrite of the whole converter.

#### B.4.4 Carry The Fix Through `../piCoreElec`

- [ ] Decide the dev workflow:
  - patch `../piXBMC` directly for iteration,
  - then export a device patch into [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi),
  - or explicitly document why a source override is the better route.
- [ ] Keep [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk):6 aligned with the source tree the patch is meant for.
- [ ] If local development temporarily needs a source override, document it here and do not leave it as an unexplained permanent package change.

#### B.4.5 Build And Extract

- [ ] Build Kodi from `../piCoreElec` for the Amlogic-ne target.
- [ ] Extract the resulting `kodi.bin`.
- [ ] Record the exact build command and extraction path used for the successful build.

#### B.4.6 Hardware Validation

- [ ] Sideload the built `kodi.bin` onto the CoreELEC box.
- [ ] Validate:
  - DV P8 MKV direct play,
  - DV P7 FEL MKV,
  - DV via fmp4 HLS,
  - non-DV HEVC regression sample.
- [ ] For the HLS/fmp4 case, verify:
  - DV engages,
  - first frame appears,
  - no `stream stalled`,
  - seek still works,
  - no regression on non-DV HEVC.

#### B.4.7 Upstream / Carry Decision

- [ ] Decide where the fix should live long-term:
  - only as a `piCoreElec` device patch,
  - as a `piXBMC` commit carried by `piCoreElec`,
  - or both.
- [ ] If the patch is acceptable upstream, prepare a clean patch / issue note for the relevant PANI repo.

---

### B.5 Constraints

- The change should be narrowly targeted at the broken DV `hvcC` path.
- Do not regress non-DV HEVC playback.
- Do not rely on `/tmp` for long-term documentation; use repo-tracked paths or explicit extraction instructions.
- Prefer carrying the final fix as a normal CoreELEC patch in `../piCoreElec` unless there is a strong reason to change the source-fetch flow.

### B.6 Repo-Tracked Supporting Artifacts

Keep these. They are still useful for the post-build validation path in step 6.

- `kodi-4.9-patched/kodi.bin`
- `kodi-4.9-patched/README.md`
- `coreelec-g12b/CoreELEC-G12B-AM6B.img.gz`
- `coreelec-g12b/dovi.ko`
- `coreelec-g12b/README.md`
- `coreelec-g12b/DOVI_KO_5_4_README.md`
- `coreelec-g12b/dovi_wrapper.c`
- `coreelec-g12b/Makefile.dovi`
- `coreelec-g12b/g12b_s922x_ugoos_am6b-full.dts`
- `coreelec-g12b/meson-g12b-full.dtsi`

---

### B.7 Notes

- `../piCoreElec` already has a Kodi device patch directory:
  [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi)
- `../piXBMC` already contains the Dolby Vision conversion machinery and metadata plumbing, so this work is patching an existing DV-aware converter, not adding DV support from scratch.

</details>

---

## Part C — Stream Proxy Architecture Reference

> **Moved to `docs/proxy-architecture.md`**
>
> See [docs/proxy-architecture.md](docs/proxy-architecture.md) for the stream proxy architecture reference.

---

## Part D — Dolby Vision Seek & Scrub Plan (`DV.md`)

> **Relevance:** read this Part if you are implementing or debugging seek/scrub behavior on 32-bit Kodi, or routing Dolby Vision profiles through the addon. §D.1 is the exec summary; §D.2 is the root-cause diagnosis of the `CFileCache` truncation bug; §D.5 is the phased implementation plan; §D.8 lists addon bugs surfaced by live testing.

<details>
<summary><b>Expand Part D — full DV seek/scrub synthesis, matrix, agent findings, implementation plan, and citations</b></summary>

**Synthesized from 10 parallel research agents — 2026-04-23**

Target hardware: UGOOS AM6B (Amlogic S922X), CoreELEC `avdvplus R9`
(2026-03-23 build), Kodi 21 Omega 32-bit userspace. Upstream nzbdav
serves full-size MKV/MP4 WebDAV content; the addon currently force-remuxes
anything over 20 GB to protect 32-bit Kodi's `CFileCache`.

Goal of this document: capture every approach that could give **real
timestamp-accurate scrub + chapter-skip** for each DV profile, rank them
by feasibility on this hardware, and lay out the implementation plan.

---

### D.1 Executive summary

#### D.1.1 Top finding: the 32-bit ceiling is a CFileCache seek-delta bug, not a filesystem limit

From source review of `xbmc/filesystem/FileCache.cpp:375`:

```cpp
m_pCache->WaitForData(static_cast<uint32_t>(iTarget - m_seekPos), 10s)
```

The `uint32_t` cast truncates the **delta** between current cache position
and seek target. Files > 4 GB play fine as long as scrub deltas stay under
4 GB — but a single seek that jumps by more than 4 GB (≈ 11 min of UHD
video) triggers the wrap and Kodi surfaces `Open - Unhandled exception`.
This explains the observed non-determinism (small scrubs fine, big jumps
crash) and the memory note that a 78 GB MKV *has* played cleanly
mid-playback.

**Consequence:** there is a single `advancedsettings.xml` knob that
completely bypasses this:

```xml
<cache>
  <memorysize>0</memorysize>
  <readfactor>1</readfactor>
</cache>
```

With `CFileCache` out of the read path, `CCurlFile` talks to libcurl
directly, all offsets are `int64_t` clean, and Range/Content-Length up to
8 EB work. **This alone unblocks pass-through of any DV MKV at any size
for any profile — no ffmpeg, no disk, no fmp4, full scrub and chapters
via the source MKV's own Cues.**

#### D.1.2 Recommendation ranking

| # | Path | Scope | Complexity | Risk |
|---|---|---|---|---|
| 1 | **Pass-through with CFileCache bypass** | All DV profiles, any size | 1-liner in stream_proxy + user edits `advancedsettings.xml` | Low — user has to add 3 lines to their Kodi config |
| 2 | **fmp4 HLS with full flag cleanup** | P5, P7 MEL, P8 (NOT P7 FEL) | Tune ffmpeg flags; already ~80% working via `+delay_moov` | Medium — DV HDR renegotiation on seek causes visible flicker; session-2 resume fragile |
| 3 | **On-device dovi_tool P7→P8.1 conversion** | Unlocks P7 FEL sources | Moderate pipeline (demux → editor → inject → mkvmerge) | Low — well-documented dovi_tool paths; 3-5 min on device |
| 4 | **Live-growing fmp4 ring buffer** | All profiles, any size | Rewrite force-remux tier to fmp4 writer + 2 GB on-disk ring | High — significant new code; AC3 timestamp alignment known fragile |
| 5 | **DASH + ffmpeg muxer** | All profiles | Similar to fmp4 HLS; alternative manifest | Unknown — architecturally cleaner but DV+CAMLCodec unproven |
| X | **InputStream Adaptive** | — | — | **Rejected** — doesn't pipe DV metadata to CAMLCodec; DV falls back to HDR10 |

**Do #1 now**, then #2 behind a setting for users who want fmp4 seek on
smaller non-P7-FEL content, then #3 for P7 FEL unlock.

---

### D.1.A Live confirmation — 2026-04-23 evening

The synthesis above was theoretical when written. This section captures
what we actually saw on the box.

**Setup tested**:

- Hardware: UGOOS AM6B / CoreELEC avdvplus R9 (Amlogic S922X, 32-bit Kodi
  binary), Kodi 21 Omega
- Source: Uncut Gems 2019 UHD BluRay REMUX, **90,787,578,374 bytes** (84.5
  GB), DV.HDR.HEVC, TrueHD 7.1 Atmos — single largest DV REMUX tested
- Addon: v1.0.4-dev with the new `force_remux_mode=2` ("Direct
  pass-through") routing and `strict_contract_mode=1` ("Warn only") to
  tolerate nzbdav's RFC-edge response shape

**Result: pass-through alone, with `CFileCache` left enabled, does NOT
fix scrub. The §D.2 analysis is empirically validated.**

What Kodi does on every multi-GB scrub:

1. Sends `GET /stream/<id>` with `Range: bytes=N-` where N > 4 GB
2. Proxy responds 206 with the requested bytes
3. **Kodi closes the connection without reading a single byte** —
   `streamed=0` in every pass-through summary
4. Kodi retries with a different N (binary-search style: 7→8→11→13→14→17→
   18→21→24→28→30 GB up, then back down to 5 GB), always > 4 GB,
   always `streamed=0`
5. Eventually gives up, surfaces "Playback failed" in the UI

This is **exactly** the behavior §D.2 predicted: `FileCache.cpp:375` truncates
`(iTarget - m_seekPos)` to `uint32_t`. For deltas > 4 GB the wrapped value
points to garbage; `WaitForData` returns immediately with insufficient data;
`CCurlFile` aborts the request. The addon never gets the chance to deliver
useful bytes because Kodi never reads any.

**Below the 4 GB boundary everything works.** Initial play (byte 0) +
the MKV-cues tail probes (bytes 90,787,054,086 → end) both succeeded with
hundreds of KB to MB streamed per request — Kodi can absolutely do
range reads on a 90 GB file as long as no individual seek delta exceeds
the uint32 boundary.

**Conclusion**: shipping `force_remux_mode=passthrough` without ALSO
applying the cache=0 advancedsettings.xml change is a footgun. The
addon-side code is correct; it cannot work around the truncation bug
inside Kodi itself.

#### Other live-testing surprises

- **ENFORCE escalates soft mismatches.** nzbdav returns `status=200`
  (not `206`) for full-object Range requests (e.g. `Range: bytes=0-`).
  The strict-contract classifier correctly marks this as a *soft*
  mismatch (`is_full_object` and `Content-Range` matches expected), but
  the ENFORCE branch (`stream_proxy.py:2357`) returns `PROTOCOL_MISMATCH`
  on any mismatch — soft or hard — which trips the density breaker at
  byte 0 and kills playback before a single byte streams. Drop ENFORCE
  to WARN before testing pass-through, or fix ENFORCE to respect the
  soft/hard distinction the classifier already returns.

- **P8 fmp4 CAMLCodec hang reproduces on R9.** §D.4.8's earlier "may
  have been unnecessary" guess was wrong — Equalizer session 2 resume
  hit "Playback never started after 31s" mid-session. The matroska
  guardrail for P8/P5/unknown DV is load-bearing; do not remove it
  without re-verifying on this exact build with multiple sources.

---

### D.2 The 32-bit Kodi bug — precise mechanics

#### D.2.1 Where the truncation happens

`xbmc/filesystem/FileCache.cpp:375`:

```cpp
// iTarget is int64_t, m_seekPos is int64_t
// (iTarget - m_seekPos) is int64_t, but the cast truncates
m_pCache->WaitForData(static_cast<uint32_t>(iTarget - m_seekPos), 10s)
```

And inside `CSimpleFileCache::WaitForData(uint32_t iMinAvail, ...)`
(`CacheStrategy.cpp:163`, signature at line 159) — the argument stays
`uint32_t` all the way down.

#### D.2.2 What triggers the wrap

Kodi caches sequential bytes ahead of the current playhead. When a seek
target `iTarget` is further ahead than currently cached data:

- **Delta < 4 GB:** truncation is a no-op (`(uint32_t)X == X`), wait
  succeeds, cache advances to target, playback resumes.
- **Delta ≥ 4 GB:** truncation wraps to a tiny number (`0xFFFFFFFE + 1 = 0`
  or similar), `WaitForData` returns immediately with insufficient data,
  downstream read returns unexpected EOF, player throws "Unhandled
  exception" back to the UI.

This is **seek-delta-bounded**, not file-size-bounded. A 78 GB MKV plays
fine if you only ever scrub a few minutes at a time. A 12 GB file crashes
if you try to jump from minute 5 to minute 60 in one seek.

#### D.2.3 The fix

`advancedsettings.xml` at `/storage/.kodi/userdata/advancedsettings.xml`:

```xml
<advancedsettings>
  <cache>
    <memorysize>0</memorysize>
    <readfactor>1</readfactor>
  </cache>
</advancedsettings>
```

- `memorysize=0` disables `CFileCache` globally. Every `CFile::Open` skips
  the cache wrapper and routes reads through `CCurlFile` directly.
- `readfactor=1` prevents libcurl-level read-ahead from piling up.

**Trade-off:** tiny network hiccups that used to be hidden by the 150 MB
in-memory buffer will now propagate to the player sooner. For localhost
WebDAV traffic, this doesn't matter. For remote WebDAV over WAN, a 50–100
MB libcurl buffer may not be enough — but the `readfactor` setting
handles that on a per-stream basis.

#### D.2.4 Consequences of the fix

| Before | After |
|---|---|
| Force-remux required for everything > 20 GB | Pass-through viable for all sizes |
| DV source Cues unusable (remux strips them) | DV source Cues drive full scrubbing natively |
| Chapter marks lost in remux | Original MKV chapter marks intact |
| 10-30 s of ffmpeg init per play | Instant play |
| No real FF on P8 | Real FF everywhere |

---

### D.3 Per-profile container compatibility matrix

(Profile-centric counterpart to §C.4's flow-centric tier selection: §C.4 shows how the proxy routes a source; §D.3 shows which routed outcomes actually decode.)

Based on container carry capability + Kodi FFmpeg demuxer behavior +
CAMLCodec decode on CoreELEC `avdvplus R9` (2026-03-23):

| Container | P5 | P7 FEL | P7 MEL | P8.1 |
|---|---|---|---|---|
| **MKV + Cues** (pass-through, `CFileCache` off) | ✅ works end-to-end | ⚠️ BL+RPU on licensed SoCs; FF has 3-4 s black (Kodi forum tid=381195) | ✅ EL drop is free (metadata only) | ✅ works (licensed SoCs) |
| **MP4 faststart** | ⚠️ 32-bit moov risk even with cache off (large moov atoms themselves hit Kodi bugs) | ❌ dual-track MP4 — EL dropped by Kodi's demuxer | ✅ single BL track, EL drop harmless | ⚠️ same 32-bit moov risk as P5 |
| **fmp4 HLS** (our current experimental path) | ✅ works with ffmpeg flag cleanup | ❌ EL lost (Dolby ISOBMFF dual-track spec not implemented in Kodi fmp4 demuxer) | ✅ works — no EL to lose | ✅ **works** on your R9 build per live testing; older builds had onAVStarted hang |
| **DASH + CMAF** | ✅ via ffmpeg DASH muxer | ❌ same dual-track gap | ✅ | ✅ (unproven on this build) |
| **InputStream Adaptive** | ❌ DV metadata stripped before CAMLCodec | ❌ | ❌ | ❌ |

**Best paths for each profile** (assuming `<memorysize>0</memorysize>` is set):

- **P5**: MKV pass-through → full scrub, no special handling needed
- **P7 FEL**: MKV pass-through with dovi_tool `-m 2` pre-conversion to P8.1
  (losing the EL intentionally). Alternative: accept matroska-pipe
  small-skip-only for FEL until licensed-SoC FF bug is fixed upstream.
- **P7 MEL**: MKV pass-through → full scrub (EL drop is free)
- **P8.1**: MKV pass-through → full scrub

---

### D.4 Detailed agent findings

#### D.4.1 dovi_tool preprocessing (Agent: a826)

On-device pre-processing is **feasible** on this AM6B / S922X hardware.
Benchmarks: `dovi_tool` is ~200-400 MB/s on aarch64 for RPU-only ops. A
60 GB remux runs in ~3-5 min.

**P7 → P8.1 conversion pipeline** (strips EL, retains all DV dynamic
metadata, produces a single-layer HEVC + RPU that decodes on every
CAMLCodec build):

```bash
# 1. Demux the MKV into separate BL/EL/RPU streams
dovi_tool demux input.mkv    # produces BL.hevc, EL.hevc, RPU.bin

# 2. Edit the RPU to profile 8.1 semantics
dovi_tool editor -i RPU.bin -m 2 -o RPU_p81.bin

# 3. Inject back into the base layer
dovi_tool inject-rpu -i BL.hevc --rpu-in RPU_p81.bin -o BL_p81.hevc

# 4. Remux to MKV with Cues (mkvmerge auto-writes them)
mkvmerge -o output.mkv BL_p81.hevc \
  --no-video --audio-tracks 1,2 input.mkv
```

For our addon: this would run as a background job after download
completes (or on first play), with progress feedback. Output replaces the
upstream file on the nzbdav side, or is cached locally.

#### D.4.2 ffmpeg fmp4+DV flag cleanup (Agent: a1cd)

Our current HLS producer uses minimal flags + the newly added
`+delay_moov`. The research points to a more robust flag set that would
make init.mp4 **byte-identical across respawns** (fixing the "canonical
init cache" fragility) and handle AC3/HEVC timestamp alignment on seek:

```bash
ffmpeg -copyts -start_at_zero -ss T -i IN \
  -map 0:v:0 -map 0:a:0 -c copy \
  -avoid_negative_ts make_zero \
  -fflags +bitexact+flush_packets -flags +bitexact \
  -movflags +frag_custom+dash+delay_moov+separate_moof+default_base_moof+omit_tfhd_offset \
  -hls_segment_type fmp4 \
  -hls_flags +independent_segments+omit_endlist+delete_segments \
  -hls_fmp4_init_filename init.mp4 out.m3u8
```

Key flag roles:

- `+frag_custom+dash` — internal flags ffmpeg's hlsenc already forces for
  fmp4 HLS. Being explicit is harmless; documents intent.
- `+separate_moof+default_base_moof` — CMAF-style fragments.
- `+omit_tfhd_offset` — removes absolute byte offsets from `tfhd`, making
  segments self-relative and byte-stable across respawns.
- `+bitexact` (both `-fflags` and `-flags`) — zeros creation/modification
  timestamps in `mvhd`/`tkhd`, strips `©too` encoder tag. Critical for
  byte-identical init.mp4.
- `-copyts -start_at_zero -avoid_negative_ts make_zero` — per Jellyfin's
  exact incantation (`DynamicHlsController.cs:1850,1873`): "`-start_at_zero`
  is necessary to use with `-ss` when seeking, otherwise the target
  position cannot be determined."
- `-flush_packets 1 +flush_packets` — ensures moov flushes before
  fragment data. Prevents truncated init on killed respawn.
- `-hls_flags +delete_segments+omit_endlist` (instead of our current
  `+independent_segments` alone) — prevents stale playlist state
  carrying over from a previous session (a likely cause of the
  intermittent "session 2 demuxer error" observed).

#### D.4.3 Live-growing MKV rejected; fmp4 is the right live-writable format (Agent: a436)

FFmpeg's matroska muxer (`libavformat/matroskaenc.c`) emits the Cues
block **only at trailer time**, not incrementally. Even with
`-reserve_index_space 2M`, the reservation is filled at
`av_write_trailer`. A half-written MKV has no seek index. A two-pass
remux takes 5–30 min for 60 GB on ARM — too slow to be live.

**Conclusion:** forget live MKV with real seek. The industry-standard
answer for "streamable seekable large-file output" is **fragmented MP4**
— each fragment is self-describing, client can Range-seek into any
already-written fragment without a global cue table. That's what our
existing fmp4 HLS path already does, and what DASH does.

The only way to serve MKV with real seek is the pass-through path (§D.2),
where the original source MKV already has its Cues.

#### D.4.4 Virtual MKV proxy design (Agent: a102)

Tested alternatives to the pass-through-with-cache-off approach:

- **Capped Content-Length + real bytes** (e.g. advertise 20 GB, serve
  bytes 0-60 GB): **does not work**. Kodi's MKV demuxer reads Cues from
  the source MKV; Cues reference absolute offsets > 20 GB; `CCurlFile`
  clamps every seek to the advertised length. Demuxer sees wrong bytes →
  silent corruption.

- **Cues rewrite on the fly**: requires physical cluster relocation
  (i.e. full remux). Non-starter without disk.

- **Accept full Content-Length**: hits the `CFileCache` wrap. Only viable
  with cache disabled.

- **Disable CFileCache per-stream**: no protocol mechanism exists. Must
  be global via `advancedsettings.xml`.

So the final design is the simplest possible:

```text
Kodi CurlFile ──GET /stream/<id>──► StreamProxy
                Range: bytes=X-   │
                                  ├─► WebDAV upstream: Range: bytes=X-
                                  │   (pass bytes 1:1)
Response headers:
  Content-Length: <source-size>   (honest)
  Accept-Ranges: bytes             (honest)
  Content-Type: video/x-matroska
```

Pair with the user-side `<memorysize>0</memorysize>` setting. No
remuxing, no disk, no special protocol. Works for any DV profile, any
size, with full Cues-driven scrub + chapter support from the original
MKV.

#### D.4.5 DASH/CMAF alternative (Agent: ac4b)

FFmpeg's DASH muxer with live-growing support:

```bash
ffmpeg -i input.mkv \
  -map 0 -c copy \
  -f dash \
  -use_template 1 -use_timeline 1 \
  -seg_duration 4 -streaming 1 \
  -window_size 0 -extra_window_size 0 \
  -adaptation_sets "id=0,streams=v id=1,streams=a" \
  -dash_segment_type mp4 \
  -movflags +dash+cmaf \
  manifest.mpd
```

Advantages over fmp4 HLS:

- SegmentTimeline gives exact per-segment durations — no playlist
  arithmetic guessing past the write frontier.
- CMAF fragments carry `dvcC`/`dvvC` reliably.
- Skips the specific code path in Kodi's internal HLS demuxer that's
  been sensitive to session state.

Would need: a new `mode="dash"` in `stream_proxy.py` parallel to `mode="hls"`,
DASH manifest generation/serve, segment serve. Kodi opens the
`.mpd` URL directly (ffmpeg handles DASH natively; no
inputstream.adaptive).

Not recommended as the primary path because (per §D.4.6) Kodi's DASH
demuxer is the same ffmpeg code that HLS uses — just a different
manifest format. The session-2 issue (if it recurs) would likely recur on
DASH too.

#### D.4.6 Kodi demuxer internals (Agent: abbf) — the critical find

The `FileCache.cpp:375` truncation (§D.2.1) was found here. Additional
findings:

- **Seekability decision** is purely from HTTP headers: `Content-Length >
  0` + `Accept-Ranges: bytes` (or absent). Kodi does not inspect MKV
  Cues or MP4 `stss` to decide — that's ffmpeg's job.
- **Kodi resume from position** calls `SeekTime(ms)` not byte offset.
  HLS path maps time→segment inside ffmpeg's `hls.c`.
- **CCurlFile re-issues Range per scrub** via `CURLOPT_RESUME_FROM_LARGE`
  — no byte-counter truncation at the curl layer.
- **HLS byte-range seek** only works if the playlist has
  `EXT-X-BYTERANGE` tags; otherwise, seek is segment-granular only.

#### D.4.7 InputStream Adaptive dead-end (Agent: a89f)

**Rejected.** ISA parses manifests but **does not preserve DV metadata to
CAMLCodec** — the `dvcC`/`dvvC` signaling goes through Kodi's FFmpeg
demuxer, which ISA replaces. DV falls back to HDR10 or SDR. Also:

- `manifest_type` property deprecated in Kodi 21, **removed** in Kodi 22.
- Stream-headers auth is unreliable on redirects
  ([issue #1371](https://github.com/xbmc/inputstream.adaptive/issues/1371)).
- CoreELEC avdvplus builds have ISA present but disabled in recent
  nightlies.

#### D.4.8 CAMLCodec DV decode — the 2026-04-15 hang IS still real on R9 (Agent: aa52, corrected 2026-04-23 evening)

No public GitHub issue or forum thread documents the specific "P8 fmp4
onAVStarted hang" on 2026-04-15. The closest artifacts are:

- [CoreELEC forum 53566](https://discourse.coreelec.org/t/unable-to-play-1080p-profile-8-dv-files/53566)
  — 1080p P8 MKV black-screen on Ugoos AM8 (unresolved auto-close).
- [avdvplus Issue #128](https://github.com/avdvplus/Builds/issues/128)
  (2026-04-15) — HDR10→DV black crush, unrelated.

**Initial conclusion (wrong)**: "On R9 (2026-03-23 build), there's no
known unfixed P8 fmp4 hang. Our live testing today confirmed this: fmp4
playback + mid-session seek + session-2 resume all work with
`+delay_moov`."

**Corrected, 2026-04-23 evening**: The hang DID reproduce on this exact
build during Equalizer fmp4 session 2 resume — "Playback never started
after 31s" at seg 158 (t≈948s). It is intermittent (passes some times,
hangs others), which is what made the original test misread it as
fixed. **The matroska guardrail for P8/P5/unknown DV must stay** until
we have a hard reproducer + a real fix.

Also confirmed:

- MKV with inline RPU NALs is empirically more stable than fmp4 because
  RPU travels with every access unit — seek doesn't depend on
  re-reading a `moof`-level record.
- fmp4 `dvcC` is re-parsed per-segment; a seek-respawned init with a
  different edit list resets codec hints but doesn't by itself hang
  CAMLCodec.
- Commit trail (for future drift): `b7133c7331` (DV dual-layer NAL skip
  policy, 2025-02-02), `8ce1dc0728` (VVC extradata feed on first chunk,
  2025-09-22, already in R9).

#### D.4.9 Prior art — Jellyfin is the gold standard (Agent: a934)

**Key techniques to steal:**

1. **Always pair `-ss` with `-copyts -start_at_zero -avoid_negative_ts
   disabled`** per `Jellyfin.Api/Controllers/DynamicHlsController.cs:1850,1873`:
   > "-start_at_zero is necessary to use with -ss when seeking,
   > otherwise the target position cannot be determined."
   Our current HLS producer uses `-copyts + -ss T` but does NOT set
   `-start_at_zero`. This is a likely latent bug in timestamp
   calculations for seek-respawned segments.

2. **Strip DV via bitstream filter, not re-encode**: Jellyfin's fallback
   for DV-incompatible clients is `-bsf:v 'hevc_metadata=remove_dovi=1'`
   or `-bsf:v 'dovi_rpu=strip=1'`. Zero CPU cost. Could be a
   per-session toggle in our routing matrix for DV-broken Amlogic
   builds: "if we can't decode DV natively, strip it and serve HDR10."

3. **mpv's per-packet DOVI side-data model** (`demux_mkv.c:1794`): attach
   `AV_PKT_DATA_DOVI_CONF` to every packet, not once at stream open.
   Kodi/ffmpeg already does this for MKV input; not relevant to our mux
   side unless we implement our own demuxer.

#### D.4.10 DV container seek options matrix (Agent: a671)

See §D.3 for the consolidated matrix. Notable additional citations:

- [Kodi PR #18965](https://github.com/xbmc/xbmc/pull/18965) — MKV
  `dvvC`/`dvcC` `BlockAddIDType` support (merged; is the reason MKV+DV
  works on Kodi 21 now).
- [Kodi PR #22410](https://github.com/xbmc/xbmc/pull/22410) — notes
  "BL+EL+RPU freezes after 2 seconds" on non-MKV paths.
- [Kodi issue #24764](https://github.com/xbmc/xbmc/issues/24764) —
  DV MKV cropping on Omega beta 3.
- [Kodi forum tid=381195](https://forum.kodi.tv/showthread.php?tid=381195)
  — P7 FEL FF black screen on CoreELEC (still open).

---

### D.5 Implementation plan

> Version labels in this plan (`v1.0.4-dev`, `v1.0.5`, `v1.1.0`, `v1.1.x`) are **aspirational**. `plugin.video.nzbdav/addon.xml` currently tracks `v1.0.3`; bump per the plan as each phase ships.

#### D.5.1 Phase 1 — Quick win: CFileCache-off pass-through

Steps 1–5 shipped (passthrough mode, settings dropdown, advancedsettings probe, runtime gate, first-play dialog, `AGENTS.md` cache=0 section). Only step 6 remains:

6. ⏸ Integration test: verify pass-through + Kodi seek works on the Uncut Gems 90 GB file after `<memorysize>0</memorysize>` is set. (Pre-cache test on 2026-04-23 confirmed the failure mode: every scrub > 4 GB returns `streamed=0`.)

#### D.5.2 Phase 2 — fmp4 flag cleanup

**Effort: ~2 hours. Impact: fmp4 path becomes robust for users who prefer
it.**

1. Update `HlsProducer._build_cmd()` fmp4 branch with the full flag set
   from §D.4.2.
2. **Do NOT remove the 2026-04-15 P8→matroska guardrail from
   `stream_proxy.py`** — per §D.4.8 (corrected), it DOES reproduce on R9.
   Keep it until a hard reproducer + a real fix lands.
3. Add regression tests for the flag set in `tests/test_stream_proxy.py`.
4. Document in Part C which profiles fmp4 is known-good for
   (P5 / P7 MEL / P8.1).

#### D.5.3 Phase 3 — P7 FEL unlock via dovi_tool

**Effort: ~1 day. Impact: P7 FEL content becomes playable with real seek
after a 3-5 min on-device convert.**

1. Add dovi_tool dependency (bundled binary for aarch64, opt-in install).
2. New module `plugin.video.nzbdav/resources/lib/dv_convert.py`:
   - Detect P7 FEL from our existing `dv_source.probe_dolby_vision_source`
   - Offer "Convert to P8.1 for compatibility? (5 min, frees seek)"
     dialog
   - Run the demux → editor → inject → mkvmerge pipeline
   - Cache the output; on subsequent plays, use the converted file
3. Storage: output replaces upstream file on nzbdav side via a WebDAV
   PUT, OR is cached locally on /storage (if file fits in remaining
   budget), OR is re-generated per play if space is tight.

#### D.5.4 Phase 4 — fallbacks

- DV strip via `hevc_metadata=remove_dovi=1` bitstream filter for
  devices that can't decode DV at all. Outputs HDR10.
- DASH mode as an alternate serving path if fmp4 HLS proves brittle on
  some build. Implemented parallel to HLS in `stream_proxy.py`.

---

### D.6 Reject list (paths considered and eliminated)

| Path | Why not |
|---|---|
| InputStream Adaptive | DV metadata stripped before CAMLCodec — DV doesn't fire |
| Live-growing MKV with Cues | matroskaenc emits Cues only at trailer; no way to stream seekable MKV without 2-pass disk work |
| Virtual MKV with capped Content-Length | Kodi demuxer reads Cues at absolute offsets, clamped reads return wrong bytes |
| Rewrite Cues on the fly to remap offsets | Requires cluster relocation = full remux |
| Two-ffmpeg race (writer + reader) | Reader ffmpeg sees SeekHead pointing at unwritten Void region — falls back to linear scan or refuses seek |
| `Transfer-Encoding: chunked` + no Content-Length | Kodi falls back to non-seekable |
| DASH via inputstream.adaptive | Same DV-metadata-lost issue as HLS+ISA |

---

### D.7 Open questions / Phase 3 blockers

(Q1 and Q3 gate the §D.5.3 P7 FEL Phase 3 effort; Q2 and Q4 are orthogonal validations best folded into Phase 1.)

1. **Is P7 FEL really broken on CoreELEC avdvplus R9's CAMLCodec**, or
   does it work via the licensed dual-layer path on our S922X? Need a
   P7 FEL test sample to confirm before committing Phase 3 effort.

2. **Does `+start_at_zero` alone fix the session-2 resume issue** (not
   reproduced today on R9, but we haven't tested without
   `+delay_moov`)? A/B test in Phase 2.

3. **Is there a P7 FEL + MKV + Cues path** that works end-to-end on R9
   WITHOUT dovi_tool preprocessing? The Kodi P7→P8.1 on-the-fly
   conversion (merged 2024-01-28) may mean FEL "just works" as pass-
   through. Worth a live test.

4. **CFileCache-off side effects on small-file streaming** — does
   disabling the cache cause buffering stutters on non-force-remux
   pass-through of small MP4 files? Measure during Phase 1.

---

### D.8 Addon bugs surfaced by live testing (2026-04-23)

These are addon-side issues found while validating the §D.2 hypothesis.
None block scrub; all are paper cuts that make pass-through harder to
adopt.

#### D.8.1 ENFORCE strict-contract-mode escalates soft mismatches ✅ fixed

**Where**: `stream_proxy.py:2358`

The classifier (`_classify_contract_mismatch`) returns `(detail, hard)`
distinguishing hard mismatches (e.g. 206 with wrong Content-Range —
silent corruption if streamed) from soft ones (e.g. status 200 + valid
Content-Range covering the full object, which nzbdav legitimately does
for `Range: bytes=0-`). The condition was `ENFORCE or hard_mismatch`,
which made ENFORCE reject every mismatch — soft included — killing
pass-through playback at byte 0 with `Recovery density breaker tripped`.

Fix: tightened the rejection branch to `if hard_mismatch:` only. Hard
mismatches now reject regardless of mode (since serving wrong bytes is
silent corruption), and soft mismatches stream while still being logged
under WARN/ENFORCE. The setting now means "Off / log soft mismatches /
log all mismatches" rather than "Off / log soft / reject everything".
Test coverage flipped accordingly — `test_stream_upstream_range_enforce_streams_soft_contract_mismatch`
(streams soft) and `test_stream_upstream_range_enforce_mode_rejects_hard_contract_mismatch`
(rejects hard) replace the old `enforce_mode_rejects_soft` test.

#### D.8.2 Threshold clamping is silently wrong ✅ fixed

**Where**: `stream_proxy.py:294` (`_FORCE_REMUX_THRESHOLD_MB_MAX`)

A user-set `force_remux_threshold_mb=20000000` (intent: 20 TB =
effectively-unlimited) used to clamp to 1,048,576 MB (1 TB). At 1 TB, no
realistic file ever trips force-remux, which is fine — but the warning
log "Setting force_remux_threshold_mb=20000000 out of range [0..1048576];
clamping to 1048576" fired on *every play*.

Fix: raised the cap to `(1 << 53) - 1` (the JSON-safe int ceiling, ~9 PB
in MB units). Realistic "effectively-unlimited" inputs survive without
triggering the clamp warning every play. Inputs above that ceiling are
genuine user error and still log loudly on first hit. The matching
`test_get_force_remux_threshold_clamps_typo_high_and_logs` test was
updated to use a value above the new ceiling.

---

### D.9 Citations

#### Kodi source

- [FileCache.cpp (32-bit delta truncation at line 375)](https://github.com/xbmc/xbmc/blob/master/xbmc/filesystem/FileCache.cpp)
- [CacheStrategy.cpp (WaitForData signature at line 159-163)](https://github.com/xbmc/xbmc/blob/master/xbmc/filesystem/CacheStrategy.cpp)
- [CurlFile.cpp (Range protocol, seekability decision)](https://github.com/xbmc/xbmc/blob/master/xbmc/filesystem/CurlFile.cpp)
- [DVDDemuxFFmpeg.cpp (seekability delegation)](https://github.com/xbmc/xbmc/blob/master/xbmc/cores/VideoPlayer/DVDDemuxers/DVDDemuxFFmpeg.cpp)
- [PR #18965 — MKV DolbyVision support](https://github.com/xbmc/xbmc/pull/18965)
- [PR #22410 — DV support main PR](https://github.com/xbmc/xbmc/pull/22410)
- [Issue #24764 — DV MKV cropping on Omega beta 3](https://github.com/xbmc/xbmc/issues/24764)

#### CoreELEC / AMLCodec

- [CoreELEC/xbmc AMLCodec.cpp](https://github.com/CoreELEC/xbmc/blob/aml-5.15.196-22.0/xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp)
- [CoreELEC/xbmc DVDVideoCodecAmlogic.cpp](https://github.com/CoreELEC/xbmc/blob/aml-5.15.196-22.0/xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp)
- [`b7133c7331` — DV dual-layer NAL skip policy (CoreELEC/xbmc)](https://github.com/CoreELEC/xbmc/commit/b7133c7331)
- [avdvplus/Builds R9 release](https://github.com/avdvplus/Builds/releases/tag/avdvplus_R9)
- [CoreELEC forum 50998 — DV + CoreELEC dev thread](https://discourse.coreelec.org/t/learning-about-dolby-vision-and-coreelec-development/50998)
- [CoreELEC forum 53566 — P8 1080p MKV black-screen](https://discourse.coreelec.org/t/unable-to-play-1080p-profile-8-dv-files/53566)
- [Kodi forum tid=381195 — P7 FEL FF black screen](https://forum.kodi.tv/showthread.php?tid=381195)

#### FFmpeg / dovi_tool

- [FFmpeg matroskaenc.c (Cues at trailer)](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/matroskaenc.c)
- [FFmpeg movenc.c (dvcC/dvvC tag writing, 4.4+)](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/movenc.c)
- [FFmpeg hlsenc.c (fmp4 HLS internal flag forcing)](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/hlsenc.c)
- [FFmpeg dovi_isom.c — dvcC/dvvC parser](https://ffmpeg.org/doxygen/trunk/dovi__isom_8c_source.html)
- [quietvoid/dovi_tool](https://github.com/quietvoid/dovi_tool)
- [dovi_tool discussion #195 — P7→P8 in MKV](https://github.com/quietvoid/dovi_tool/discussions/195)

#### Prior art — Jellyfin, mpv, VLC

- [jellyfin EncodingHelper.cs (DV strip via bsf)](https://github.com/jellyfin/jellyfin/blob/master/MediaBrowser.Controller/MediaEncoding/EncodingHelper.cs)
- [jellyfin DynamicHlsController.cs (`-copyts -start_at_zero` with `-ss`)](https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Api/Controllers/DynamicHlsController.cs)
- [mpv demux_mkv.c (per-packet DOVI side-data)](https://github.com/mpv-player/mpv/blob/master/demux/demux_mkv.c)
- [videolan/vlc libmp4.c (dvcC atom)](https://code.videolan.org/videolan/vlc/-/blob/master/modules/demux/mp4/libmp4.c)
- [Plex forum — P7 dual-layer MP4](https://forums.plex.tv/t/feature-request-support-profile-7-dolby-vision-dual-layer-dv-in-mp4s/532651)

#### inputstream.adaptive

- [xbmc/inputstream.adaptive wiki — Integration](https://github.com/xbmc/inputstream.adaptive/wiki/Integration)
- [Issue #968 — DV mode regression 20.2.0](https://github.com/xbmc/inputstream.adaptive/issues/968)
- [Issue #1007 — CMAF support tracking](https://github.com/xbmc/inputstream.adaptive/issues/1007)
- [Issue #1371 — stream_headers auth on redirects](https://github.com/xbmc/inputstream.adaptive/issues/1371)
- [Issue #1933 — Kodi 22 ABI churn](https://github.com/xbmc/inputstream.adaptive/issues/1933)

#### DASH / DV spec

- [FFmpeg dashenc.c](https://github.com/FFmpeg/FFmpeg/blob/master/libavformat/dashenc.c)
- [DASH-IF Live Media Ingest](https://dashif-documents.azurewebsites.net/Ingest/master/DASH-IF-Ingest.html)
- [Dolby ISOBMFF DV spec (Dec 2017)](https://professional.dolby.com/siteassets/content-creation/dolby-vision-for-content-creators/dolby_vision_bitstreams_within_the_iso_base_media_file_format_dec2017.pdf)
- [Dolby KB — How to signal DV in ISOBMFF](https://professionalsupport.dolby.com/s/article/How-to-signal-Dolby-Vision-in-ISOBMFF-format-AKA-mp4-container)

</details>

---

## Part E — Fix Verification Record (`BUG2.MD`)

> **Relevance:** skim only when merging or reviewing the 20-agent-review remediation. This Part is a historical fix-verification record, not an action list.

All P0, P1, P2, and P3 findings from the 20-agent review are fixed and verified in commit `4103f5d`. 643 → 657 tests pass; lint clean on Python 3.10/3.12 CI matrix. The deferred-items list that follows is still active and worth skimming.

### E.2 Deferred (require upstream material we don't have or large synthesis work)

These are **nice-to-have** additional coverage; none block merge.

1. **Profile 5 RPU fixture** — upstream `quietvoid/dovi_tool` doesn't ship one. We have mocked routing coverage for P5 but not a real P5 RPU parser test. Would require either a public DV P5 sample clip or a hand-crafted synthesis.
2. **`coefficient_data_type == 1` test** — the alternative fixed-point encoding branch. No real-world fixture exercises it; all three vendored dovi_tool fixtures use `coefficient_data_type == 0`. See §D.4.1 for the on-device dovi_tool preprocessing approach that would generate a test corpus covering both encoding types.
3. **`use_prev_vdr_rpu_flag=True` test** — rare mid-stream frame type; requires hand-synthesizing an RPU with the flag set. The edge-case behavior is now documented in the `dv_rpu.py` module docstring (P2.8).
4. **Real open-source DV clip + dovi_tool cross-check CI harness** — a `@pytest.mark.integration` test that compares `dv_rpu.parse_rpu_payload` output against live `dovi_tool info --frame 0` output. Would guard against upstream drift, but is out of scope for this PR.

---

## Part F — Rollout Playbooks

> **Relevance:** read this Part when executing one of the §0 P0/P1/P2 validation or soak steps. Each section below is a self-contained one-pager originally filed under `junk/plans/`. The five source files have been consolidated here and removed from the tree.

The five source playbooks merged into Part F:

- `junk/plans/REMAINING_COREELEC_SMOKE.md` — now §F.1
- `junk/plans/REMAINING_SEND_200_VALIDATION.md` — now §F.2
- `junk/plans/REMAINING_OBSERVABILITY_SOAK.md` — now §F.3
- `junk/plans/PROXY_EPIC_ARTICLE_HEALTH.md` — already captured verbatim in §A.4 (pointer here only)
- `junk/plans/PROXY_EPIC_NNTP_TUNING.md` — already captured verbatim in §A.5 (pointer here only)

---

### F.1 CoreELEC Smoke Validation (clean-article release)

**Status:** gated on PR-1 merge.

**Goal:** confirm the merged PR-1 code plays a clean-article release identically on the real CoreELEC box. Catches regressions the unit-test suite cannot (actual HW decoder, Kodi UI path, WebDAV roundtrip).

#### F.1.1 Entry criteria

- PR-1 merged into the branch that produces the addon zip.
- CoreELEC box available and idle (no family member watching).
- `kodi.log` is clean / rotated before starting so failures are easy to find.

#### F.1.2 Setup

- Box: `root@coreelec.local`, 32-bit Kodi on Amlogic AM6B.
- Build: `just release` → `plugin.video.nzbdav.zip`.
- Pick a release with **low article-dead rate** — reference a recent Trakt top-10 popular movie submitted to nzbdav within the last 48 h. Avoid anything older than 30 days (higher article decay).

#### F.1.3 Steps

1. `just release` on the merged branch.
2. `scp dist/plugin.video.nzbdav.zip root@coreelec.local:/storage/`.
3. Install via Kodi Add-ons → Install from zip file.
4. Restart the addon via the main menu if needed. **Ask permission before `systemctl restart kodi`** — per `memory/feedback_no_kodi_restart_without_permission.md`.
5. Trigger playback via TMDBHelper → "Play with NZB-DAV" on the chosen title.
6. Watch continuously for at least 2 hours. Seek to `00:10`, `00:30`, `01:00`, `01:30`. Each seek should resume within 5 s.
7. On the box, tail `kodi.log` for the final terminal summary line (grep for `NZB-DAV: Stream summary`).

#### F.1.4 What to verify in `kodi.log`

- One terminal summary line at playback stop containing bytes-served, bytes zero-filled, recovery count, reason.
- Zero occurrences of `strict_contract_mode` rejection messages (default is `warn`, should be silent on conformant nzbdav).
- Zero `PROTOCOL_MISMATCH` / `UPSTREAM_ERROR` reason codes.
- No new error-level lines vs. a pre-merge baseline of the same release.

#### F.1.5 Acceptance

- Full 2-hour playback end-to-end with no manual intervention.
- All four seek resumes successful within 5 s.
- Audio sync within ±50 ms at every seek.
- Terminal summary shows 0 bytes zero-filled on the clean-article release.

#### F.1.6 If it fails

Attach the full `kodi.log` to a new issue. Do NOT flip any flags (`strict_contract_mode`, `density_breaker_enabled`) to try to mask the failure — PR-1 is supposed to be a no-op at default flags. A regression at defaults means PR-1 actually regressed something.

---

### F.2 Validate `send_200_no_range=ON` on CoreELEC

**Status:** gated on PR-1 merge + clean smoke pass (§F.1).

**Goal:** verify Kodi tolerates HTTP 200 (vs. always-206) on no-Range pass-through GETs before shipping the `send_200_no_range` flag default-ON. This is an HTTP-correctness cleanup that the addon has deliberately not shipped default-ON because Kodi's `CCurlFile` behavior at `Content-Length` + `200 OK` without `Accept-Ranges` was untested on the target build.

#### F.2.1 Entry criteria

- Clean smoke test passed per §F.1.
- Merged branch installed on the box.

#### F.2.2 Setup

- Two back-to-back playback runs on the same clean-article release from the smoke test.
- Tail `kodi.log` for the full session in both runs.

#### F.2.3 Steps

##### Run A — baseline (default OFF)

1. Confirm in addon settings: `send_200_no_range = OFF` (default).
2. Play the release from start to `00:30`, seek to `01:00`, play 30 s, stop.
3. Save `kodi.log` → `soak-data/send200-off-<date>.log`.

##### Run B — flag flipped ON

1. Enable `send_200_no_range = ON` via Kodi addon settings UI.
2. Repeat the same playback sequence: start → `00:30` → seek to `01:00` → play 30 s → stop.
3. Save `kodi.log` → `soak-data/send200-on-<date>.log`.

#### F.2.4 What to compare

- Initial buffer time to first frame (should be within ±1 s between A and B).
- Seek-to-resume time at `01:00` (should be within ±2 s).
- Presence / absence of `Pass-through write aborted` warnings — should match A.
- Any new error-level lines in B that are not in A.

#### F.2.5 Acceptance

- B behaves identically to A on all three comparisons.
- No new error lines in B.
- Optional byte-accuracy spot-check: `curl --range 0-1048575 http://<proxy>/<stream>` with the flag OFF vs ON, compare md5 — should match byte-for-byte for the first 1 MB.

#### F.2.6 Decision

- **If green across both runs:** the flag is safe to ship default-ON in a follow-up PR. Update the default in `resources/settings.xml` and record the decision in the commit message.
- **If any regression in Run B:** the flag stays default-OFF. Open an issue tagging the specific regression observed and defer.

---

### F.3 Observability Soak (≥1 week)

**Status:** gated on PR-1 merge.

**Goal:** collect at least 7 days of real-world playback telemetry from the merged code to inform:

- whether to flip `density_breaker_enabled` default-ON
- whether to flip `strict_contract_mode` from `warn` to `enforce`
- threshold calibration for the Article-Health epic (§A.4)

Without this data, every remaining flag-flip and epic-kickoff decision is a guess.

#### F.3.1 Entry criteria

- PR-1 merged and installed on the primary CoreELEC box.
- Normal daily viewing resumes (no synthetic load required).
- `kodi.log` rotation at default size (usually 1 MB) — do NOT set to unlimited; rotation means old logs roll to `kodi.old.log` and you have a window to grab them before they're lost.

#### F.3.2 Setup

- Daily cron / manual step: pull `kodi.log` and `kodi.old.log` off the box into `docs/soak-data/<date>.log`. Script stub:

```bash
DATE=$(date +%Y-%m-%d)
scp root@coreelec.local:/storage/.kodi/temp/kodi.log docs/soak-data/${DATE}.log
scp root@coreelec.local:/storage/.kodi/temp/kodi.old.log docs/soak-data/${DATE}.old.log 2>/dev/null || true
```

#### F.3.3 What to extract each day

Grep targets. Counts matter more than details.

| Signal | Grep | What it tells you |
|---|---|---|
| session count | `Stream summary` | how many streams happened |
| bytes zero-filled | `zero_fill_bytes=` | per-session recovery volume |
| reason code mix | `reason=` | `upstream_open_failed` vs `short_read_recoverable` vs `protocol_mismatch` vs `session_zero_fill_budget_exceeded` vs `recovery_exhausted` vs `density_breaker_tripped` vs `client_disconnected` (see `stream_proxy.py` `reason=` log lines for the canonical taxonomy) |
| strict-contract warn lines | `strict_contract_mismatch` | logged-but-tolerated mismatch count |
| density breaker trips | `density_breaker_tripped` | terminal-line count when the breaker fires |

Tokens above are the actual lowercase strings emitted by the proxy.

#### F.3.4 Analysis after 7 days

Record results inline in this section before making decisions.

1. **Zero-fill distribution.** 90th percentile bytes-per-session? If > 10 MB, a significant fraction of streams would hit the per-session cap.
2. **Reason-code mix.** Dominant cause of recoveries? Informs whether the retry ladder (P1.5) or upstream-side work (§A.5) is the better next lever.
3. **strict_contract warn counts.** If 0 across 7 days → flip to `enforce` is safe. If > 0 → investigate each case before flipping.
4. **density_would_trip counts.** If 0 across 7 days → flip default-ON is safe. If > 0 → inspect the cases: are they bad releases the breaker SHOULD catch, or false positives?

#### F.3.5 Exit criteria

- At least 7 distinct calendar days of logs captured in `docs/soak-data/`.
- Decisions recorded on:
  - [ ] `strict_contract_mode` → keep `warn` | flip to `enforce`
  - [ ] `density_breaker_enabled` → keep OFF | flip default ON
  - [ ] start Article-Health epic (§A.4) — go / no-go
  - [ ] start NNTP-tuning epic (§A.5) — go / no-go
- Record completed decisions in the commit message that flips them.

---

### F.4 Gated epics — see §A.4 and §A.5

The two `PROXY_EPIC_*.md` one-pagers (`ARTICLE_HEALTH`, `NNTP_TUNING`) are already reproduced verbatim in §A.4 and §A.5 respectively. No duplicate copy maintained here — edit §A.4 / §A.5 directly when those epics move forward.

---

## Part H — Static Analysis Audit Trail

> **Relevance:** read this Part when triaging static-analysis findings or deciding what to fix next. Three independent audit passes have run on this codebase; their outputs were originally separate files (`ISSUE_REPORT.md`, `QA_SCAN_20260424.md`, `BUGS3.md`) and were folded in here on 2026-04-24 to keep all audit history in one place. Each subsection is wrapped in `<details>` so it stays out of the way until you click; expand only the audit you need.

### H.1 Audit catalog

| ID | Subsection | Date | Scope | Tree | Raw findings | Status today |
|---|---|---|---|---|---|---|
| H.2 | Three-audit consolidation (was `ISSUE_REPORT.md`) | 2026-04-22 | three independent audits → deduplicated | pre-PR-1 | 117 (0 C / 19 H / 50 M / 38 L + 6 UX themes) | Many fixed by PR-1 (`16e7122`) and Part E remediation (`4103f5d`); see §E.1 for the verification tally. |
| H.3 | 100-agent breadth scan (was `QA_SCAN_20260424.md`) | 2026-04-24 | 100 Explore agents — files / bug-classes / interactions / scenarios | `3ee019b` | ~80 (1 C / 37 H / 32 M / 10 L) | Live triage list. The `cache=0` D.5.1 line and the §D.8 bugs were closed from this pass. Remainder is open. |
| H.4 | 50-agent depth follow-up (was `BUGS3.md`) | 2026-04-24 | 50 Explore agents — newer code, deeper bug classes, build/CI/test scaffolding | `3ee019b` | 36 (1 C / 6 H / 18 M / 11 L) | 24 cleared in commits `06a047a` + `0ae903f`; remaining 12 listed in §H.4 (3 architectural High items, 1 deferred Medium, 8 false-positive/cosmetic). |

**How to cite a finding.** Use `§H.<sub>-<id>` — e.g. `§H.2-H1c` is the use-after-free on prune from the three-audit consolidation; `§H.3-Critical-1` is the raw-exception-string Kodi dialog leak; `§H.4-High-2` is the shared-stream-context concurrency bug. Subsection-internal IDs match the ones used in the original files so existing test docstrings (`# Regression test for ISSUE_REPORT.md C5`) still resolve.

**Severity legend.** **C** = Critical, **H** = High, **M** = Medium, **L** = Low. All severities are agent-assigned on static-analysis confidence and have not been re-triaged against live workloads. Treat them as triage hints, not bug priorities.

---

### H.2 Three-audit consolidation (was `ISSUE_REPORT.md`)

> Consolidated from three independent audits run before the PR-1 reliability/security baseline merge. Deduplicated and organized by severity, then by theme within each severity. Many of these were closed by the PR-1 work (commit `16e7122`) and the 20-agent review remediation (commit `4103f5d`, see §E.1 for the verification tally). The remainder is the canonical list of architectural / cross-cutting concerns to design against.

<details>
<summary><b>Expand §H.2 — full three-audit list (~117 findings)</b></summary>

#### H.2.HIGH

##### H1. Stream proxy session/lifetime races (cluster of 7+ findings)

- **H1a. Use-after-free on prune:** handler reads `ctx`, dispatches; concurrent prune can `pop` + `_cleanup_session(ctx)` in between, handler serves from a deleted temp file and killed ffmpeg. (`stream_proxy.py:204 vs 981-983`)
- **H1b. LRU eviction kills active playback:** the 9th `prepare_stream` kills the oldest active playback via `_cleanup_session`. (`stream_proxy.py:985-993`)
- **H1c. TTL prune trusts stale `last_access`:** playbacks >6h get evicted mid-stream. (`stream_proxy.py:973-983`)
- **H1d. Concurrent seek spawns duplicate ffmpeg:** two concurrent GETs both spawn ffmpeg and store as `active_ffmpeg`; first finisher's `is` proc check only clears its own slot, leaving two ffmpegs fighting over stdout. (`stream_proxy.py:514-580`)
- **H1e. `_register_session` holds `_context_lock` while calling `_cleanup_session`,** blocking `proc.kill()/wait()`. (`stream_proxy.py:959-964`)
- **H1f. `stop()` clears sessions while in-flight handlers still hold `ctx` refs.** (`stream_proxy.py:879-892`)
- **H1g. `_context_lock` lives on StreamProxy, not the handler/server** — `_get_stream_context()` reads `stream_sessions` and mutates `ctx["last_access"]` with no lock. (`stream_proxy.py:203-207`)

##### H2. Credential leaks via ffmpeg argv and URL redaction gaps

- **H2a. `_embed_auth_in_url` splices `user:password@host` into ffmpeg argv;** credentials visible in `/proc/<pid>/cmdline` and `ps -ef`. (`stream_proxy.py:1307-1322`, `1240`, `115-141`, `309-321`, `1215-1239`)
- **H2b. `_prepare_tempfile_faststart` logs `stderr.decode()[:300]` on non-zero exit;** ffmpeg messages echo full URL with basic-auth. (`stream_proxy.py:1240-1247`)
- **H2c. `redact_url` only redacts `apikey`;** `password/auth/token/api_key/key/secret/access_token` all leak. (`http_util.py:14`)
- **H2d. `redact_url` does not redact `user:password@host` userinfo in netloc.** (`http_util.py:10-19`)
- **H2e. HTTPError/URLError messages containing full URL with `apikey=`** surfaced to user via `_hydra_unavailable_error` and notifications. (`hydra.py:96-130`)
- **H2f. `nzbdav_api.py` exception messages can contain unredacted apikey URLs.** (`nzbdav_api.py:74`)

##### H3. mp4_parser unvalidated box sizes (cluster of 6 findings)

- **H3a. DoS via unbounded count:** `_rewrite_stco`/`_rewrite_co64` read attacker-controlled count as unbounded uint32 and iterate `range(count)`; `count=0xFFFFFFFF` → 4-billion-iteration loop. (`mp4_parser.py:98-117`)
- **H3b. O(N²) memory in moov rewrite:** `_rewrite_offsets_recursive` slices `data[body_start:body_end]` and re-parses per box; a 50 MB moov hangs the proxy. (`mp4_parser.py:152-155`)
- **H3c. No bounds validation:** no check that `offset + total_size` stays within `len(data)`; child boxes claiming size > parent corrupt the buffer. (`mp4_parser.py:135`)
- **H3d. `scan_top_level_boxes` advances by `total_size` even when `read_box_header` returns `size == 0`,** silently discarding everything after. (`mp4_parser.py:81`)
- **H3e. No moov/mdat overlap check;** malformed file passes as faststart with overlapping ranges. (`mp4_parser.py:83-85`)
- **H3f. `_find_moov_after_mdat` can produce `file_size - 1 = -1` on empty files → malformed Range.** (`mp4_parser.py:225-239`)

##### H4. Stream proxy upstream response validation gaps

- **H4a. `_stream_upstream_range` never validates `resp.status`;** if upstream returns 200 instead of 206, content streams from offset 0 while client expects start — silent corruption. (`stream_proxy.py:736-758`)
- **H4b. `_serve_mp4_faststart` writes whole 1 MB chunks without clamping to `length - bytes_sent`;** emits up to ~1 MB past advertised Content-Length on partial-range requests. (`stream_proxy.py:437-444`)
- **H4c. Nothing bounds written by `end - start + 1`;** misbehaving upstream exceeds advertised Content-Length. (`stream_proxy.py:744-758`)
- **H4d. Empty chunk from upstream treated as EOF;** on a slow connection this could be a temporary condition, prematurely ending the stream. (`stream_proxy.py:440`)

##### H5. `_poll_once` non-daemon threads leak across iterations
**File:** `resolver.py:420-425`. Non-daemon threads with `join(timeout=10)` leak across iterations.

##### H6. `nzbdav_api.py` HTTP calls with no timeout — indefinite blocking
**Files:** `nzbdav_api.py:111`, `154`, `178`, `226`. `get_job_history`, `find_completed_by_name`, `get_completed_names` call `_http_get(url)` with no timeout, blocking the resolver indefinitely.

##### H7. Broad exception catches mask specific errors and swallow everything

- **H7a. `nzbdav_api.py:74`:** `except (URLError, json.JSONDecodeError, Exception)` — `Exception` swallows everything; bare except collapses 401/403/404/timeout to `None`.
- **H7b. `hydra.py:96`, `120`:** `except (URLError, Exception)` — catching `Exception` after `URLError` (which is a subclass of `OSError`) means all exceptions are caught, including `KeyboardInterrupt` and `SystemExit` in Python 2, and `MemoryError` in any version. The `URLError` in the tuple is redundant.

##### H8. Service monitor: `xbmcgui.Dialog().ok()` from service loop thread
**File:** `service.py:233-238`. Calling `xbmcgui.Dialog().ok()` from the service loop (every 1 s) blocks the loop until the user dismisses the dialog.

##### H9. `resolve_and_play` error path escapes without `setResolvedUrl(handle, False)`
**File:** `resolver.py:666-669`. `_get_poll_settings()` and `xbmcgui.DialogProgress().create()` run outside `resolve()`'s `try/except`.

##### H10. `_play_via_proxy` never sets `nzbdav.active`/`stream_url`/`stream_title`
**File:** `resolver.py:319-359`. Service-side retry and error dialogs never fire on the RunPlugin code path.

##### H11. Language filter compares PTT lowercase codes against UI labels
**File:** `filter.py:424`. Compares `"en"`, `"es"` against `"English"` — any enabled language filter rejects every result.

##### H12. `prepare_stream_via_service` 60s HTTP timeout vs 600s ffmpeg allowance
**Files:** `stream_proxy.py:1307-1322` vs `1240`. Plugin times out while service still completes, orphaning session + ffmpeg + temp file.

##### H13. `_probe_duration` reads ffmpeg stderr line-by-line with no timeout
**File:** `stream_proxy.py:1172-1193`. Silent upstream blocks indefinitely. Broad-except for `TimeoutExpired` never `proc.kill()`s, orphaning ffmpeg.

##### H14. `http_get` performs no scheme validation — SSRF / local file read
**File:** `http_util.py:22-26`. `urlopen` opens `file:///`, `ftp://`.

##### H15. ✅ Fixed
`notify()` builtin injection via unescaped interpolation (was `http_util.py:41-45`). See §H.3 fix-comment — `_escape_builtin_arg` now maps `,` `)` and CR/LF to inert lookalikes before format-interpolating. Regression test: `tests/test_http_util.py::test_notify_escapes_builtin_metacharacters`.

##### H16. `service.py` state mutated by Kodi callbacks on internal threads without locks
**File:** `service.py:117-160`. `NzbdavPlayer._state`/`_retry_count`/`_av_started`/`_last_position` mutated by Kodi player callbacks while `tick()` reads-then-writes on the service main thread.

##### H17. `webdav.py` best-file selection picks 0-byte placeholder over real video
**File:** `webdav.py:281`. `best_file = href_path` with `size >= best_size` and `best_size = 0` initial picks a 0-byte placeholder `.mkv` over real video.

##### H18. `cache.py` non-atomic write — concurrent writers interleave and corrupt JSON
**Files:** `cache.py:71-72`. No `.tmp` + `os.replace`.

##### H19. `_retry_playback` never distinguishes user-stopped (IDLE) intent
**File:** `service.py:269`. User-stopped streams get resurrected.

#### H.2.MEDIUM

##### M1. Poll/timeout configuration bugs

- **M1a.** `MAX_POLL_ITERATIONS = 720` hard-codes 5s cadence; `poll_interval=1` caps total wait at ~720s instead of 3600s. (`resolver.py:30`, `505`)
- **M1b.** `poll_interval=0` produces `waitForAbort(0)` tight loop. (`resolver.py:366-368`)
- **M1c.** `int(percentage or 0)` raises on `"45.5"`. (`resolver.py:566`, `nzbdav_api.py:275`, `284`)

##### M2. `_storage_to_webdav_path` doesn't URL-encode
**File:** `resolver.py:383` — Space / `#` / `?` / `&` break the WebDAV URL.

##### M3. `nzbdav.stream_url` stores pipe-header form; service retry turns `|Header=Value` into filesystem path
**File:** `resolver.py:285-286`

##### M4. `_play_via_proxy` direct branch passes `li.getPath()` AND the ListItem, duplicating headers
**File:** `resolver.py:347`

##### M5. LIKE pattern with `%`/`_` in user-controlled `tmdb_id` matches unintended rows
**File:** `resolver.py:141-149`

##### M6. `sys.argv[0]` used as plugin URL for DB cleanup — stale in `resolve_and_play()` service context
**File:** `resolver.py:130-133`

##### M7. ffmpeg process leak on `TimeoutExpired` in `_prepare_tempfile_faststart`
**File:** `stream_proxy.py:1240-1252`

##### M8. `urlopen(timeout=120)` has no per-read timeout
**File:** `stream_proxy.py:437`

##### M9. Request abandonment: small range → fetch whole tail → break mid-stream
**File:** `stream_proxy.py:430-437` — Small repeated ranges thrash upstream.

##### M10. `_probe_duration` stdout=PIPE never read; muxer banner can deadlock
**File:** `stream_proxy.py:1163-1168`

##### M11. `_register_session` unconditionally overwrites `self._server.stream_context`; legacy `/stream` clients rebind
**File:** `stream_proxy.py:962`

##### M12. Faststart temp files leak forever on crash; no `atexit` or startup orphan scan
**File:** `stream_proxy.py:1199-1258`

##### M13. `_MAX_RECOVERY_SECONDS` budget only checked at loop top; handler can block ~60s vs advertised 30s
**File:** `stream_proxy.py:785-788`

##### M14. `time.sleep(delay)` in handler can't observe shutdown; should use `Monitor.waitForAbort()`
**File:** `stream_proxy.py:788`

##### M15. Concurrent `_serve_remux` on different sessions clobber server-wide `active_ffmpeg`/`current_byte_pos`
**Files:** `stream_proxy.py:579-580`, `644-645`

##### M16. `_resolve_seek` holds per-ctx `ffmpeg_lock` across `kill()+wait()`; chunk loop stalls
**File:** `stream_proxy.py:514-541`

##### M17. `_get_settings` `.rstrip("/")` on `None`; no non-empty validation
**File:** `nzbdav_api.py:20-22`

##### M18. `search_term = name.split(".")[0]` keeps only the first dot-segment
**File:** `nzbdav_api.py:142`

##### M19. No deduplication of aggregated multi-indexer results
**File:** `hydra.py:100-130`

##### M20. ✅ Fixed
`int(max_results or 25)` raises on non-numeric setting (was `hydra.py:69`, now `hydra.py:97` after refactor). See §H.3 fix-comment for current `hydra.py:97` patch — try/except + clamp to [1..100].

##### M21. `root.iter("item")` traverses entire tree including nested items; should scope to `channel`
**File:** `hydra.py:156`

##### M22. `parsedate_to_datetime` naive datetime subtraction with tz-aware `now` raises `TypeError`, silently swallowed
**File:** `hydra.py:230`

##### M23. Filter bugs

- **M23a.** "Filtered N→M" count computed after `max_results` truncation; user-visible count wrong. (`filter.py:474`)
- **M23b.** min/max size unit mismatch (MB vs GB). (`filter.py:443`)
- **M23c.** Size filter skipped when size is falsy; 0-byte results bypass constraints. (`filter.py:441`)
- **M23d.** All metadata filters short-circuit on empty parsed metadata; unparseable releases bypass quality filters. (`filter.py:406`)
- **M23e.** Preferred/include `release_group` setting is never enforced as a hard filter; only exclude list is checked. (`filter.py:436`)
- **M23f.** Redundant `.lower()` in list comprehension — `settings["exclude_release_group"]` is already lowercased at line 327. (`filter.py:436-438`)

##### M24. WebDAV silent fallback to `nzbdav_url` hits wrong server with WebDAV creds
**File:** `webdav.py:316-327`

##### M25. `find_video_file` catches all; 401/403/5xx all collapse to "not found"
**File:** `webdav.py:174`

##### M26. `request_path` encoded vs decoded hrefs cause recursive PROPFIND until `_depth > 2`
**File:** `webdav.py:262`

##### M27. No scheme enforcement on URL settings; `http://` sends basic-auth/apikey cleartext
**Files:** `webdav.py:20-32`, `211-213`

##### M28. `/search` success path calls `endOfDirectory(handle, succeeded=False)` even on successful playback
**File:** `router.py:504-510`

##### M29. `parse_params` without `keep_blank_values=True` drops blank params; duplicates silently discarded
**File:** `router.py:25-34`

##### M30. Season-0 specials silently dropped by `if il_s and il_s not in ("","-1","0")`
**File:** `router.py:229-232`

##### M31. `_test_*_connection` builds `?apikey={}` via raw format; urllib error containing apikey surfaces via notify
**Files:** `router.py:606`, `635`

##### M32. Cache bugs

- **M32a.** Size accounting uses logical file size not block size; 2-10x over the 50 MB limit possible. (`cache.py:91`)
- **M32b.** LRU by mtime but `get_cached` never utimes; it's oldest-written, not LRU. (`cache.py:95`)
- **M32c.** TOCTOU on `exists → open → load`; concurrent writers race. (`cache.py:45-50`)
- **M32d.** Corrupt cache missing timestamp treated expired but never deleted. (`cache.py:51`)
- **M32e.** `_cache_key` truncates to 200 chars after sanitizing; distinct queries with same prefix collide. (`cache.py:32`)
- **M32f.** Sanitization lossy: `"Foo Bar"`, `"Foo-Bar"`, `"Foo.Bar"`, `"Foo:Bar"`, `"Foo/Bar"` all map to the same key. (`cache.py:31`)

##### M33. `proxy.start()` → immediate `setProperty(port)` with no bind-complete guarantee
**File:** `service.py:282-284`

##### M34. Service-scoped window properties not cleared on startup; stale state from prior session survives
**File:** `service.py:273-297`

##### M35. 5s "playback never started" detector uses `time.time()` not monotonic
**File:** `service.py:223`

##### M36. `proxy.stop()` has no exception guard; failure leaves stale `_PROP_PROXY_PORT`
**File:** `service.py:295`

##### M37. `_retry_playback` called from `tick` while Player callback may still be in flight; no sync
**File:** `service.py:190-208`

##### M38. Cross-process IPC writes URL/TITLE/ACTIVE as three independent `setProperty` calls; no atomicity
**Files:** `resolver.py:286-300` vs `service.py:101-111`

##### M39. `install_player()` unconditionally overwrites `nzbdav.json`; hand-edits lost
**File:** `player_installer.py:51-54`

##### M40. `_FALLBACK_STRINGS` missing ids 30121/30115/30116 (service.py) and 30054/30055 (router.py); blank dialogs when `strings.po` unavailable
**File:** `i18n.py:9`

##### M41. `history["status"]` access without null check
**File:** `resolver.py:570` (same pattern at line 583) — `history` checked for truthiness, but empty dict `{}` passes truthiness then raises `KeyError`.

##### M42. `clear_cache()` calls `os.listdir()` on non-existent directory
**File:** `cache.py:116` — Raises `FileNotFoundError` if cache directory was never created.

##### M43. `clear_cache()` / `_evict_oldest()` race condition
**File:** `cache.py:91-108` — Total size computed, then files deleted; another thread/process could modify files in between.

##### M44. Year validation upper bound is stale — time bomb
**File:** `filter.py:674` — `if 1920 <= yr <= 2030` rejects valid 2031+ releases.

##### M45. Suffix range with value > content_length produces negative start offset
**File:** `stream_proxy.py:831-832`

##### M46. `validate_stream` dead code: `e.code in (200, 206)` in `HTTPError` branch
**File:** `webdav.py:386` — `HTTPError` is only raised for 4xx/5xx; 200/206 never raise it.

##### M47. `setResolvedUrl` called then window properties set after — race condition
**File:** `resolver.py:282-288` — If service's `tick()` fires between these, it reads stale window properties.

##### M48. `_play_via_proxy` plays with `li.getPath()` which may differ from intended URL
**File:** `resolver.py:347` (same pattern at line 359)

##### M49. File open without encoding specification
**File:** `cache.py:49`, `71` — On non-UTF-8 systems, could fail to read JSON cache files.

##### M50. `_lookup_episode_info` uses undocumented IMDB API endpoint
**File:** `router.py:154` (same URL at line 569) — `https://v2.sg.media-imdb.com/suggestion/t/{}.json` — if IMDB removes this, episode lookups silently fail.

#### H.2.LOW

##### L1. User-cancel never DELETEs `nzo_id`; cancelled jobs accumulate in queue
**File:** `resolver.py:529-534`

##### L2. `_validate_stream_url` doesn't catch `http.client.HTTPException` subclasses
**File:** `resolver.py:51`

##### L3. `history["storage"]` bare key access raises `KeyError` on partial responses
**File:** `resolver.py:584`

##### L4. `_cache_bust_url` appends query after fragment, invisible to servers on `#`-bearing URLs
**File:** `resolver.py:91`

##### L5. `_apply_proxy_mime()` only sets duration on remux; pass-through/faststart branches skip it
**File:** `resolver.py:218-227`

##### L6. `probe_end = min(target+1023, range_end)` produces 1-byte probe at exact boundary
**File:** `stream_proxy.py:781`

##### L7. Skip tier never retried smaller after advancing
**File:** `stream_proxy.py:777-815`

##### L8. Legacy `stream_context` field never cleared on eviction
**Files:** `stream_proxy.py:849`, `962`

##### L9. Concurrent legacy-fallback clients collide on shared `ctx`
**File:** `stream_proxy.py:194-195`

##### L10. `_parse_ffmpeg_duration` requires fractional seconds; localized builds without them disable seek
**File:** `stream_proxy.py:144-158`

##### L11. `RangeCache` instantiated per session but never read; dead code
**File:** `stream_proxy.py:1031`

##### L12. Faststart temp file leak if `_embed_auth_in_url` raises before `try`
**File:** `stream_proxy.py:1209-1213`

##### L13. `os.path.getsize()` race between `_prepare_tempfile_faststart` return and `_register_session`
**File:** `stream_proxy.py:1082`

##### L14. Bare `except Exception` around subtitle-setting silently drops all subs
**File:** `stream_proxy.py:333-340`

##### L15. `_resolve_seek` uses linear byte-to-seconds map; visible seek drift
**File:** `stream_proxy.py:502-512`

##### L16. `{:.1f}.format(None)` in `_resolve_seek` fragile if duration unset
**File:** `stream_proxy.py:525-530`

##### L17. `.get("history", {}).get("slots", [])` breaks on `"history": null`
**File:** `nzbdav_api.py:116+`

##### L18. Fallback HDR regex maps bare "HDR" to HDR10 but could be HLG
**File:** `filter.py:631`

##### L19. Fallback group regex truncates groups with hyphens or underscores
**File:** `filter.py:682`

##### L20. Size kept as raw string; `int(size)` crashes on malformed attr
**File:** `hydra.py:174-186`

##### L21. `int(argv[1])` raises if Kodi calls script entry without numeric handle
**File:** `router.py:39-41`

##### L22. Hardcoded English in connection-test `notify()` calls
**Files:** `router.py:603`, `610`, `612`, `614`, `632`, `639`, `641`, `643`

##### L23. `f.write()` return value not checked; partial writes reported as success
**File:** `player_installer.py:52`

##### L24. `os.makedirs` without `exist_ok=True` races with concurrent first-call
**File:** `cache.py:22`

##### L25. `fmt()` calls `str.format` with no `try/except`; crashes on missing string
**File:** `i18n.py:67`

##### L26. Hardcoded English in retry `notify()` calls
**File:** `playback_monitor.py:88-92`, `155-157`

##### L27. `http_get` no status check; non-UTF-8 raises `UnicodeDecodeError`
**File:** `http_util.py:24-26`

##### L28. No User-Agent; some Cloudflare-protected upstreams 403 `Python-urllib/...`
**File:** `http_util.py:24`

##### L29. `_check_active()` clears only `_PROP_ACTIVE`; stale URL/title persist
**File:** `service.py:111`

##### L30. Second `xbmc.Monitor()` instance unnecessary
**File:** `service.py:78`

##### L31. `sys.path.insert` without dedup check
**File:** `addon.py:11`

##### L32. Unicode lightning bolt without fallback — may not render on all Kodi skins
**File:** `results_dialog.py:141`

##### L33. `bytes(data)` creates copy on every iteration in `_rewrite_offsets_recursive`
**File:** `mp4_parser.py:136`

##### L34. Docstring after early return — no-op string literal
**File:** `ptt/adult.py:27-29`

##### L35. Mutable default argument `extend_options(options: Dict[str, Any] = {})`
**File:** `ptt/parse.py:92`, `ptt/handlers.py`

##### L36. Playlist cleared after `setResolvedUrl` — could interfere with queued items
**File:** `resolver.py:662-663`, `679-680`, `684-685`

##### L37. Redundant empty string check `if not path or path == ""`
**File:** `router.py:20-21`

##### L38. WebDAV error type only set when both queue and history are `None`
**File:** `resolver.py:429`

#### H.2.UX-IMPROVEMENTS

1. **Empty/None results:** Present a non-modal toast/notification with reason and "Try again / Change filters" CTA instead of opening a blank modal dialog.
2. **Sorting controls:** Expose sort order (relevance/date/size/seeders) and reflect the active choice in the header instead of a fixed "Sorted" label.
3. **Counts and pagination:** Show "Showing X of Y (page P/N)" rather than conflicting count vs. filtered/total labels; add a pager or "Load more."
4. **Information density:** Align columns, ensure consistent color coding for resolution/quality, add language/subtitle badges, truncate long filenames with expand affordance.
5. **Selection confidence:** Add preview/metadata pane on focus (size, age, indexer trust score, history status) and a clear "Already downloaded" state.
6. **Failure transparency:** Show inline validation errors before calling Hydra (e.g., missing title/season/episode); avoid silent empty dialogs.

#### H.2.TOP-PRIORITY-THEMES

| # | Theme | Findings | Key files |
|---|---|:---:|---|
| 1 | Router handle-hang bugs | 6 | `router.py` |
| 2 | Stream proxy session/lifetime races | 7+ | `stream_proxy.py` |
| 3 | Credential leaks (ffmpeg argv + redact_url gaps) | 6 | `stream_proxy.py`, `http_util.py`, `hydra.py`, `nzbdav_api.py` |
| 4 | mp4_parser unvalidated box sizes | 6 | `mp4_parser.py` |
| 5 | Broad exception catches masking errors | 4 | `nzbdav_api.py`, `hydra.py`, `stream_proxy.py` |
| 6 | Cache race conditions and correctness | 6 | `cache.py` |
| 7 | Service/player thread-safety | 4 | `service.py` |

**Totals:** 0 Critical · 19 High · 50 Medium · 38 Low · 6 UX improvements = **117 findings**.

</details>

---

### H.3 100-agent breadth scan (was `QA_SCAN_20260424.md`)

> Scope: 100 Explore agents run in parallel across file deep-dives, bug-class sweeps, cross-module interactions, and end-to-end scenario replays on the addon tree at commit `3ee019b`. Items below still need triage — none have been verified against a running CoreELEC playback, and a spot-check of 8 findings against the source confirmed the bulk but flagged some overstated impact claims. Findings already captured in §H.2 (was `ISSUE_REPORT.md`) were dropped during this scan's dedup, so cross-reference §H.2 when planning fixes.
>
> **Caveat:** severities below are agent-assigned on static-analysis confidence only. Re-check each item's actual user impact against the live code before scheduling fix work — several "High" items are defensive hardening that may never fire on today's inputs.

<details>
<summary><b>Expand §H.3 — full 100-agent scan list (~80 findings)</b></summary>

#### H.3.Critical

- **Raw exception strings surfaced in Kodi dialog** | `resolver.py:1117` | `_handle_resolve_exception` feeds `str(error)` into `xbmcgui.Dialog().ok`; URL/apikey text inside upstream error messages reaches the TV screen.

#### H.3.High

- **Density breaker trips on empty recovery window** | `stream_proxy.py:2129` | First zero-fill produces 100% ratio and aborts the stream before any real recovery attempt.
- **Unlocked `_get_stream_context` mutation when lock is None** | `stream_proxy.py:938-949` | `last_access` update and session dict read race with prune/register paths; use-after-free window larger than it looks.
- **Double-write to shared ffmpeg ref** | `stream_proxy.py:1032-1035` | ctx + server both hold the proc handle; concurrent handler can observe stale pointer after respawn.
- **Probe reader daemon thread never joined** | `stream_proxy.py:4349-4378` | Thread orphaned on ffprobe deadline expiry; accumulates over long-running service.
- **Socket leak on protocol-mismatch early return** | `stream_proxy.py:2359 + 2393-2414` | Multiple early returns from upstream-range loop bypass the `finally` that closes the upstream socket.
- **Silent stderr-reader failure hides ffmpeg probe errors** | `stream_proxy.py:4344` | Bare `except` around stderr parsing turns real probe failures into duration=None silently.
- **CL promised before body finishes** | `stream_proxy.py:2034-2056 + 2036/2370` | Client receives full Content-Length header, then the read loop has no socket timeout and can short-read; CL mismatch stalls Kodi.
- **Prune evicts ctx that active handlers still reference** | `stream_proxy.py:3789, 3805, 941` | LRU eviction pops a session while another handler is mid-serve; temp file / ffmpeg disappears under it. (Partial overlap with §H.2-H1c, but these lines are post-refactor.)
- **LOGERROR in chunked-write hot loop** | `stream_proxy.py:1514` | On a flaky connection this spams `kodi.log` and will evict rotation window on the 1 MB default.
- **`wait_for_init` uses `time.sleep(0.25)`** | `stream_proxy.py:2800-2851` | Blocks Kodi shutdown — should be `Monitor.waitForAbort(0.25)`.
- **`wait_for_segment` uses `time.sleep(0.25)`** | `stream_proxy.py:2866-2888` | Same shutdown-block issue as above.
- **`HlsProducer.prepare()` production loop uses `time.sleep(0.25)`** | `stream_proxy.py:3297-3332` | Same shutdown-block issue; service can't exit cleanly during HLS warmup.
- **`stream_info` never sets `"direct"` key** | `stream_proxy.py:4176-4188` | Both `resolver.py:411` and `:479` branch on `stream_info.get("direct")` which is always falsy, so the direct-play fast path is dead code.
- **ffmpeg remux path has no respawn/retry on early crash** | `stream_proxy.py:1063-1082` | MP4 remux crashes during startup return a dead stream; only the HLS path auto-respawns.
<!-- Fixed in commit a78510b: _stream_remux_output now also catches (OSError, ValueError) and classifies them as "ffmpeg_pipe_closed" so a mid-stream ffmpeg crash doesn't leave the request handler hung on an open response. -->

- **HLS playlist served before `init.mp4` exists** | `stream_proxy.py:1335` | fMP4 manifest handed to Kodi before init segment lands on disk; first segment fetch 404s on cold start.
- **`_play_direct` missing `setResolvedUrl` on exception path** | `resolver.py:407-410` | Exception inside the direct-play branch leaves the handle unresolved, Kodi spins.
<!-- Fixed in commit a78510b: resolver._RESOLVE_RUNTIME_ERRORS now includes URLError + socket.timeout. -->

- **`xbmcaddon.Addon()` per-tick cost in hot loops** | `resolver.py:804`, `service.py:354,433`, `stream_proxy.py:308` | Constructing the Addon object every 250 ms (resolver) or every service tick (service) is wasteful; cache once per call site.
- **/resolve route falls through to `setResolvedUrl(False)` after `resolve_and_play` starts playback** | `router.py:110-119, 174` | Missing `return` after `resolve_and_play`; the trailing `_safe_resolve_handle` kills the handle that resolver already claimed.
<!-- Fixed in commit a78510b: router.py route() now validates argv length + tries int(argv[1]) defensively. -->
<!-- False positive (verified against current code): _handle_search IS wrapped — it sits inside the `try:` at route() line 128 and the matching `except Exception as e:` at the end of route(). -->

<!-- Fixed in commit a78510b: settings.xml now declares webdav_content_root as a hidden power-user setting (default ""), with a comment explaining the rationale. -->

<!-- Fixed in this commit: hydra.py:97 now wraps int(max_results) in try/except + clamps to [1..100]. -->

<!-- Fixed in this commit: hydra.py:_fetch_hydra_xml + nzbdav_api.py submit error path + prowlarr.py search error paths now route exception str() through redact_text() before logging. The broader §H.2-H2c gap (redact_url only covers `apikey=`, not `password/auth/token/api_key/key/secret/access_token`) is still open. -->

<!-- Fixed in commit a78510b: filter.parse_title_metadata now wraps hdr/audio/language list construction in `list(dict.fromkeys(...))` to dedup while preserving order. -->

<!-- Fixed in commit a78510b: _rewrite_co64 now returns bool matching _rewrite_stco's contract; the recursive walker propagates failure on truncated co64 boxes. -->

<!-- False positive (verified): mp4_parser docstring at line 383 explicitly says "payload_remote_end: last byte + 1 in original file" and all downstream consumers use it as exclusive (payload_size = end - start, virtual_size = header + payload_size). No off-by-one in the call chain. -->

<!-- Fixed in commit a78510b: player_installer now uses os.path.commonpath instead of startswith, so sibling directories (`addon_data_evil`) can't pass the prefix check. -->

<!-- Fixed in this commit: http_util.notify() now routes heading/message through `_escape_builtin_arg`, which maps `,` and `)` to visually-similar Unicode lookalikes that the Kodi builtin parser treats as inert. Regression test in tests/test_http_util.py::test_notify_escapes_builtin_metacharacters. -->

- **`_playback_error` reset ordering incomplete** | `playback_monitor.py:70-72` | Rapid back-to-back failures can trigger a false retry because the error flag clears before the state machine re-reads it.
<!-- Fixed in this commit: kodi_advancedsettings.py wraps the translatePath call in a broad try/except so any partly-initialized-Kodi exception path returns False, matching the docstring's "any failure → False" contract. -->

- **`service.py` monitor state resets ERROR→MONITORING mid-retry** | `service.py:110-128, 178-192, 271-311` | `_check_active` unconditionally transitions when resolver flips `nzbdav.active=true`, clobbering an in-flight retry.
- **onPlayBackSeek races onPlayBackError on `_last_position`** | `service.py:202` | Concurrent Kodi callbacks mutate the shared dict without a lock.
- **Port-bind race on proxy restart** | `service.py:393, 413, 3469-3477` | `proxy_port` window property is set before `HTTPServer.serve_forever` is actually listening; client races lose.
<!-- Fixed in this commit: strings.po #30124 msgstr filled in to match the msgid. -->

<!-- Fixed in this commit: tests/conftest.py _FakePlayer now has isPlayingVideo() mirroring isPlaying(). -->


#### H.3.Medium

- **`HlsProducer.prepare()` early-exit vs file-existence check ordering** | `stream_proxy.py:3305-3331` | Race with ffmpeg flush window — the file-exists check can pass while the final moof is still in stderr buffer.
- **`_register_session_locked` TOCTOU vs `_get_stream_context` read** | `stream_proxy.py:3763-3766` | Server-state init is not atomic with the session-dict read in the other path.
- **120 s streaming timeout too long** | `stream_proxy.py:1499` | Slow/broken connection blocks a handler for 2 minutes before recovery.
- **`_retry_original_range` retries same boundaries** | `stream_proxy.py:2236-2273` | Retry after partial write doesn't advance `start`; repeats bytes the client already has.
- **`_prepare_tempfile_faststart` leaks tempfile on TimeoutExpired/SubprocessError** | `stream_proxy.py:4388` | `mkstemp` path never unlinked when ffmpeg times out.
- **Hot tempfile left behind on client disconnect** | `stream_proxy.py:1559` | `BrokenPipeError` swallowed without removing the still-open temp file — grows /tmp over time.
- **Session counter keys rely on `.get` fallback** | `stream_proxy.py` ctx-init paths | `session_streamed_bytes` / `session_zero_fill_bytes` / `session_recovery_count` not explicitly zeroed; any logger that hits `ctx[key]` direct raises KeyError.
- **`_HLS_PRIVATE_TEMP_ROOT` persists across service respawns** | `stream_proxy.py:58, 212-226` | Stale path reused, with prior session's init files still in place.
- **Module-level `_proxy` singleton never reset** | `stream_proxy.py:4589-4594` | Service reload can keep the old object alive alongside a new one.
<!-- False positive (verified): _ThreadedHTTPServer at stream_proxy.py:2598 sets `allow_reuse_address = True` on the class, which is the stdlib's documented way to set SO_REUSEADDR on an HTTPServer subclass. -->

- **`clear_sessions` doesn't join ffmpeg threads** | `stream_proxy.py:3535-3541` | New StreamProxy spawns while child procs from the old one still draining stderr.
- **`stderr_thread.join(timeout=5)` too short** | `stream_proxy.py:1097` | Slow stderr drain leaves the thread leaking past the join.
- **Missing CL treated as hard mismatch** | `stream_proxy.py:474` | Comparing `None` against `str(expected)` classifies every missing CL as a hard strict-contract violation.
- **206 without Content-Range not marked hard=True** | `stream_proxy.py:454-457` | Current code logs a warning only; ENFORCE mode doesn't reject.
- **ENOSPC not distinguished from generic OSError** | `stream_proxy.py:4388, 1561` | User gets a generic failure notification; no actionable "disk full" message.
- **`_ensure_ffmpeg_headed_for` holds lock across `proc.wait(timeout=2)`** | `stream_proxy.py:2903-3023` | Concurrent segment requests serialize behind the 2 s wait.
- **`resolve_and_play` calls `_clear_kodi_playback_state()` without params** | `resolver.py:1248` | TMDBHelper bookmark entry keyed by (tmdb_id, title) isn't cleared; bookmarks go stale.
- **Dialog.update exception can skip thread joins** | `resolver.py:730` | If `dialog.update` raises, `submit_t`/`probe_t` never join; threads leak.
- **`while-True` poll loop lacks hard iteration cap (mitigated)** | `resolver.py:1152` | `MAX_POLL_ITERATIONS=720` caps it today; still worth asserting.
- **Queue-adoption nzo_id returned before submit worker completes** | `resolver.py:710, 739` | If backend assigns a different id for the same NZB, downstream poll targets the wrong job.
- **`_poll_once` join-timeout paths don't cancel upstream job** | `resolver.py:595-602` | Abandoned poll leaves nzbdav still downloading.
- **Force-quit during submit orphans nzbdav job** | `resolver.py:1136` | No cross-session nzo_id persistence; the queue entry stays.
- **Silent `proxy.stop()` exception during restart** | `service.py:454` | Restart path swallows the exception and leaves the new proxy unstarted.
- **Stale `nzbdav.proxy_port` property after failed restart** | `service.py:405, 465` | On the exception path, the old port stays published; clients hit a dead listener.
<!-- Fixed in this commit: router.py:39 parse_qs now uses keep_blank_values=True so empty params survive (Kodi plugin URLs don't repeat keys, so duplicate-drop is benign and now visible to anyone iterating parsed.items()). -->

- **Cancel/submit status check asymmetric** | `nzbdav_api.py:204 vs 285` | Submit uses truthy `if response.get("status")`, cancel uses `is True` identity — one nzbdav build that returns `"ok"` instead of `True` would make cancel silently fail.
- **Silent body-read exception during error reporting** | `nzbdav_api.py:174` | Exception inside the error-reporting branch masks the real malformed response.
- **Size filter skipped when size falsy** | `filter.py:455` | 0-byte placeholder results bypass min/max size constraints.
- **Metadata filters short-circuit on empty parse** | `filter.py:418-440` | Releases that PTT can't parse bypass resolution/audio/HDR filters instead of being rejected.
- **Initial moov probe limited to 16 bytes** | `mp4_parser.py:250` | Extended-size box headers (size=1 + uint64 largesize) exceed 16 bytes; probe fails on uncommon containers.
- **`schema_version` missing from generated player JSON** | `player_installer.py:30 + generated nzbdav.json` | Constant has it, write path skips it — future TMDBHelper versions that gate on schema_version refuse to load.
- **`tmdb_id` missing from `play_movie` URL template** | `player_installer.py:32-33 + generated nzbdav.json` | `_clear_kodi_playback_state` needs the tmdb_id to match the TMDBHelper bookmark row.
<!-- False positive (verified): _save_position lives in service.py now (playback_monitor.py was deleted in an earlier refactor). Current code at service.py:218-224 catches a typed _PLAYER_RUNTIME_ERRORS tuple, not bare Exception. -->

- **Session dedup window-property read vs write race** | `cache_prompt.py:68` | Two concurrent plays could show the cache-prompt dialog twice.
<!-- Fixed in commit a78510b: i18n.fmt now wraps str.format in try/except (IndexError, KeyError, ValueError); falls back to the raw template plus stringified args and logs the mismatch. -->

- **Stale cache entries outlive a lowered TTL** | `cache.py:51-79` | User shortens cache_ttl but old entries honour their original expiry.
- **`submit_timeout` default mismatch** | `settings.xml:91 = 30 vs nzbdav_api.py:24 = 120` | User-facing default disagrees with the fallback the code uses when the setting is unset.

#### H.3.Low

- **`prepare()` argv loop uses `time.sleep(0.05)`** | `stream_proxy.py:3272-3286` | 50 ms sleep is short enough to not block shutdown noticeably, but it's inconsistent with the `Monitor.waitForAbort` convention.
<!-- Fixed in commit a78510b: HlsProducer ffmpeg Popen now passes stdin=subprocess.DEVNULL. -->

- **Bare except around `proxy_convert_subs` setting read** | `stream_proxy.py:1154` | Subtitle setting silently ignored on any unexpected error.
- **`-map 0:s?` discards subtitle language metadata** | `stream_proxy.py:1152-1158` | Output SRT has no track language; works but a papercut.
- **`size` kept as raw string in hydra XML** | `hydra.py:224 + filter.py:457` | `int(result["size"])` can raise on malformed `<size>` element; wrap or pre-validate.
- **`content_root or "content"` dead code** | `webdav.py:82` | `content_root` is already defaulted on line 79, so the trailing `or "content"` is unreachable.
- **Reused string IDs for different UI contexts** | `settings.xml:7,12,17 (#30003), 70-73 (#30054/30055)` | Translators see one string, addon shows it in two unrelated spots; cosmetic today, painful when one caller wants a context-specific phrasing.
- **No max-entry count in cache** | `cache.py:14` | Only a size cap — small-entry workloads can blow up entry count without hitting the size limit.
- **TOCTOU on cache eviction size sum** | `cache.py:129` | Concurrent writes between `getsize` calls make the eviction decision off; low impact on single-service usage.
- **Several timing-based tests use `time.sleep`** | `tests/test_cache.py:65`, `tests/test_integration_hls_ffmpeg.py`, `tests/test_stream_proxy.py:3362`, others | CI flakes under load; prefer monotonic fakes.

</details>

---

### H.4 50-agent depth follow-up (was `BUGS3.md`)

> Scope: 50 Explore agents run in parallel against the same `3ee019b` tree as §H.3, biased toward newer code paths (Dolby Vision parser, cache prompt, contract-mismatch hardening, Prowlarr search), deeper bug classes (typed-contract drift, TOCTOU, NTP wall-clock arithmetic), more end-to-end scenarios, and the build / CI / test scaffolding (`scripts/`, `.github/workflows/`, `tests/conftest.py`). Items already present in §H.2 or §H.3 were dropped during dedup; spot-checks against the source confirmed the bulk but rejected a handful (build_zip `os.walk` symlinks, generate_repo zip-version, stream_max_retries `int()` wrapping, generate_repo repo-fanart asset).
>
> **Status (2026-04-24):** ~24 of the original 36 findings have been fixed in commits `06a047a` and `0ae903f`. The entries below are what remains: three architectural items that need design before code, plus a handful of false-positive / cosmetic items that were rejected on closer inspection of the live code. Severities are still agent-assigned and may not match real-world impact.

#### H.4.High — still open (architectural)

- **Cached `ctx["auth_header"]` survives nzbdav apikey rotation** | `stream_proxy.py` (ctx auth header) | 401/403 from a rotated key is caught generically and feeds the zero-fill recovery loop, masking auth failure as data corruption. Fix needs a 401-aware exception path in `_stream_upstream_range` plus a refresh hook to re-read the WebDAV auth headers and tear down the active session, which is invasive enough to merit a design note before code.
- **Shared `self._server.stream_context` torn down by second client** | `stream_proxy.py:_get_stream_context` | Concurrent `prepare_stream` from a second player calls `clear_sessions()` mid-handler on the first. Fix needs the stream-context registry to be keyed by `session_id` rather than a singleton on the server, which touches every call site of `_get_stream_context` and the prune/eviction path.
- **`tests/conftest.py` module-level mock install with no teardown** | `tests/conftest.py:14-15,51,92` | `sys.modules["xbmc"]` patches and `xbmc.Player = _FakePlayer` persist for the entire test session. Refactoring this to a session-scoped autouse fixture with explicit teardown is a large test-scaffolding change and not tackled in this sweep.

#### H.4.Medium — still open / deferred

- **`mp4_parser._CONTAINERS` excludes `mvex`** | `mp4_parser.py:148` | Fragmented MP4 (`moof`/`trex`) is silently unsupported. Out of scope for the proxy today — fragmented MP4 is what `HlsProducer` produces, not what `mp4_parser` rewrites — but worth a docstring note. Deferred until the fragmented-MP4 input case actually shows up in field traffic.

#### H.4.Low — false positives / not-actionable

- **`vdr_rpu_profile > 1` silently maps to profile 0** | `dv_rpu.py:94-105` | `return 0` here is an intentional "no clear DV signal" sentinel matching the `bl_video_full_range_flag` path on line 96. Callers (`dv_source._classify_parsed_rpu`) handle profile=0 as `non_dv`. Not a bug.
- **Emulation-prevention byte-removal edge case** | `dv_rpu.py:174` | The HEVC spec defines exactly one `0x03` after `00 00` as the emulation byte; consecutive `0x03 0x03` is data + emulation, not double-emulation. Current behavior is per-spec correct.
- **`DolbyVisionSourceResult.profile` Optional vs `DolbyVisionRpuInfo.profile` non-Optional** | `dv_source.py:50` vs `dv_rpu.py` | The two layers have different invariants by design — RpuInfo is post-parse (always set), SourceResult is post-classification (None for non-DV). Annotation is correct.
- **`tests/test_cache_prompt.py` mutates `Addon.return_value` without try/finally** | `tests/test_cache_prompt.py:104+` | Each test uses `@patch("resources.lib.cache_prompt.xbmcaddon")` which creates a fresh per-test mock; the patch context restores at test exit. Mutations on the per-test mock don't leak. False positive.
- **`build_zip.py` `os.walk` follows symlinks** | `scripts/build_zip.py` | `os.walk` defaults to `followlinks=False`. False positive.
- **`build_zip.py` flattens file modes to 0o644** | `scripts/build_zip.py:26` | Cosmetic; the addon ships no executable Python files. Flagged but not fixed.
- **`tests/test_stream_proxy.py` repeats the same mock-Addon save/restore 34×** | `tests/test_stream_proxy.py` | Refactor opportunity, not a bug. Deferred — touching 34 tests is high-blast-radius for low-value cleanup.
- **`_canonical_init_bytes` read/write ABA** | `stream_proxy.py:1813,2828` | Mitigated by GIL today; the agent flagged this as "not future-proof under sub-interpreters / no-GIL" rather than a present-day bug. Not fixed.
- **PTT vendored, returned-dict shape never asserted** | `plugin.video.nzbdav/resources/lib/ptt/` vs `filter.py` | `filter.py` `parse_title_metadata` now wraps the normalisation block in `try/except (TypeError, AttributeError, KeyError)`, which gives runtime tolerance even when PTT drifts — but no formal contract assertion was added. Defensive-only, deferred.

---

> **End of TODO.md.** Source files removed from the tree after consolidation (full history in `git log`):
>
> - First pass (2026-04-24, Part F merge): `docs/TODO.md`, `docs/TODO_PANI.md`, `PROXY.md`, `DV.md`, `docs/BUG2.MD`, plus the five `junk/plans/*.md` playbooks.
> - Second pass (2026-04-24, Part H merge): `ISSUE_REPORT.md` (now §H.2), `QA_SCAN_20260424.md` (now §H.3), `BUGS3.md` (now §H.4).
>
> **Last reviewed:** 2026-04-24 (100-principal-engineer parallel review pass + Part F rollout-playbook consolidation + Part H audit-file consolidation).
