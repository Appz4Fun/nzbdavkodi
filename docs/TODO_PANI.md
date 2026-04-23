# TODO_PANI.md

TODO for fixing Dolby Vision playback issues in the PANI/CoreELEC codebases:
- `../piXBMC`
- `../piCoreElec`

This file is for source-level DV remediation in those repos. It supersedes the old DV planning references that used to live in `TODO.md`.

---

## Goal

Make Dolby Vision over HLS/fMP4 work on the PANI Amlogic/CoreELEC stack by fixing the HEVC `hvcC -> AnnexB` conversion path in `../piXBMC` and carrying that fix through the `../piCoreElec` build pipeline.

The working theory, based on the current code, is:
- `CBitstreamConverter::Open()` enables HEVC bitstream conversion for `hvcC` extradata in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):544.
- `CBitstreamConverter::BitstreamConvert()` then rewrites the access unit NAL-by-NAL, including the Dolby Vision RPU / EL handling in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1413.
- That rewrite path is the likely source of the DV failure for HLS/fMP4 on Amlogic.

---

## Technical Context

This is the condensed fact record that step 1 depends on. It replaces the deleted long-form DV background doc.

### Two Distinct Layers

- Layer 1 is addon/ffmpeg metadata. The archived investigation notes show ffmpeg HLS drops the `dvvC` box from `init.mp4`; without reinjection, `hints.hdrType` stays `HDR_TYPE_NONE`, so the DV-specific Kodi path is never exercised. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md) lines 24-28 and [docs/pannal-xbmc-dv-hls-issue.md](docs/pannal-xbmc-dv-hls-issue.md):35.
- Layer 2 is Kodi bitstream mutation. In the current `../piXBMC` tree, HEVC `hvcC` detection in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):643 still sets `m_convert_bitstream` via `BitstreamConvertInitHEVC()` at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):647. Packets then flow through `BitstreamConvert()` at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1413, which prepends SPS/PPS on IDR at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1477, routes NAL 62 through `ProcessDoViRpu()` at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1498, and forces a 4-byte start code for UNSPEC62 at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1550. The working matroska-style branch is the `m_convert_bitstream == false` passthrough path in `Convert()` at [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):758.

### Why Step 0 Exists

- The archived successful investigation was against pannal forks, not the exact `CoreELEC/xbmc` source line that `../piCoreElec` builds today. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md) lines 10-15.
- Those same notes recorded two patch shapes: a pannal/xbmc variant that used `m_hints.hdrType` directly, and a CoreELEC/xbmc variant that needed a dedicated setter. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md) lines 49-56.
- In the current `../piXBMC` tree, `CBitstreamConverter` still stores `CDVDStreamInfo& m_hints` at [../piXBMC/xbmc/utils/BitstreamConverter.h](../piXBMC/xbmc/utils/BitstreamConverter.h):198, and `DVDVideoCodecAmlogic::Open()` still constructs the converter directly from `m_hints` at [../piXBMC/xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp](../piXBMC/xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp):307. At the same time, `../piCoreElec` still downloads `CoreELEC/xbmc` at [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk):11. That source-fork mismatch is why step 0 is blocking.

### Dead Ends Already Explored

- `inputstream.ffmpegdirect` was already ruled out: the archived notes say the addon links against ffmpeg 6 while the test box ships ffmpeg 7, so the binary does not load. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md):43.
- MPEG-TS HLS was already ruled out: ffmpeg's muxer cannot write the Dolby Vision descriptor into the PMT. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md):44.
- Reverting to old `CoreELEC/xbmc@ff8ba16` as the Kodi source was already ruled out in the archived investigation because it was tied to ffmpeg 6 APIs while the pannal/CoreELEC toolchain was on ffmpeg 7. See [docs/memory/DV_CONTEXT_SUMMARY.md](docs/memory/DV_CONTEXT_SUMMARY.md):45.

---

## Repos And Hotspots

### `../piXBMC`

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

### `../piCoreElec`

- [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk):6
  Kodi source and version selection for the Amlogic-ne device build. Current fetch target is `CoreELEC/xbmc`, not `../piXBMC`.
- [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi)
  device-level patch carry path; currently already used for at least one Kodi patch.

---

## Active TODO

### 0. Resolve The Source-Fork Strategy First

This is a go/no-go decision. Do not start the actual `piXBMC` code change until it is settled.

- [ ] Choose one development strategy and record it here:
  - **A. Retarget `../piCoreElec` to the `piXBMC` source line** so the implementation and build source match.
  - **B. Develop two variants** if `../piXBMC` and the `CoreELEC/xbmc` tree fetched by `package.mk` are not patch-compatible enough.
  - **C. Implement and test in `../piXBMC`, then port the final delta into the `../piCoreElec` patch carry path as a separate step.**
- [ ] Record why the chosen strategy is safe given that [package.mk](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk):11 currently points at `https://github.com/CoreELEC/xbmc/...`.
- [ ] Treat step 2 below as blocked until this is decided. A patch written blindly against `../piXBMC` may not be portable to the tree `../piCoreElec` actually builds today.

### 1. Confirm The Exact Broken Path

- [ ] Re-read `Open()` in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):544 and verify the HEVC `hvcC` case still sets `m_convert_bitstream` when `m_to_annexb` is true.
- [ ] Re-read `BitstreamConvert()` in [../piXBMC/xbmc/utils/BitstreamConverter.cpp](../piXBMC/xbmc/utils/BitstreamConverter.cpp):1413 and confirm the current path:
  - prepends SPS/PPS on first IDR,
  - routes NAL type 62 through `ProcessDoViRpu()`,
  - routes NAL type 63 through the current EL handling,
  - rewrites the whole AU through `BitstreamAllocAndCopy(...)`.
- [ ] Record the exact lines to patch before touching code.

### 2. Add A Dedicated DV `hvcC` Passthrough Mode In `../piXBMC`

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

### 3. Keep Existing Non-DV Behavior Stable

- [ ] Keep the existing path unchanged for:
  - non-DV HEVC,
  - AVC,
  - AnnexB inputs,
  - dual-layer / HDR10+ conversion cases unless intentionally routed through the new mode.
- [ ] Make the new DV `hvcC` passthrough an explicit narrow branch, not a broad rewrite of the whole converter.

### 4. Carry The Fix Through `../piCoreElec`

- [ ] Decide the dev workflow:
  - patch `../piXBMC` directly for iteration,
  - then export a device patch into [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi),
  - or explicitly document why a source override is the better route.
- [ ] Keep [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/packages/mediacenter/kodi/package.mk):6 aligned with the source tree the patch is meant for.
- [ ] If local development temporarily needs a source override, document it here and do not leave it as an unexplained permanent package change.

### 5. Build And Extract

- [ ] Build Kodi from `../piCoreElec` for the Amlogic-ne target.
- [ ] Extract the resulting `kodi.bin`.
- [ ] Record the exact build command and extraction path used for the successful build.

### 6. Hardware Validation

- [ ] Sideload the built `kodi.bin` onto the CoreELEC box.
- [ ] Validate:
  - DV P8 MKV direct play,
  - DV P7 FEL MKV,
  - DV via fMP4 HLS,
  - non-DV HEVC regression sample.
- [ ] For the HLS/fMP4 case, verify:
  - DV engages,
  - first frame appears,
  - no `stream stalled`,
  - seek still works,
  - no regression on non-DV HEVC.

### 7. Upstream / Carry Decision

- [ ] Decide where the fix should live long-term:
  - only as a `piCoreElec` device patch,
  - as a `piXBMC` commit carried by `piCoreElec`,
  - or both.
- [ ] If the patch is acceptable upstream, prepare a clean patch / issue note for the relevant PANI repo.

---

## Constraints

- The change should be narrowly targeted at the broken DV `hvcC` path.
- Do not regress non-DV HEVC playback.
- Do not rely on `/tmp` for long-term documentation; use repo-tracked paths or explicit extraction instructions.
- Prefer carrying the final fix as a normal CoreELEC patch in `../piCoreElec` unless there is a strong reason to change the source-fetch flow.

## Repo-Tracked Supporting Artifacts

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

## Notes

- `../piCoreElec` already has a Kodi device patch directory:
  [../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi](../piCoreElec/projects/Amlogic-ce/devices/Amlogic-ne/patches/kodi)
- `../piXBMC` already contains the Dolby Vision conversion machinery and metadata plumbing, so this work is patching an existing DV-aware converter, not adding DV support from scratch.
