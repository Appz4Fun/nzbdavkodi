# HLS fmp4 Dolby Vision HEVC stalls with zero frames while matroska with identical bitstream plays correctly

## Summary

On CoreELEC-Amlogic builds based on `aml-4.9-21.3`, a fragmented-MP4 HLS stream containing HEVC + Dolby Vision (verified with Profile 8, Level 6, BL=1 EL=0 RPU=1, cross-compatible ID=1) successfully opens the Amlogic HW decoder, engages the full DV pipeline (`aml_dv_open`, IPT Tunnel, PQ transfer, HDMI DV mode negotiation, TrueHD passthrough audio), downloads all segments, and then produces **zero frames**. The `CVideoPlayerAudio::Process` thread reports `stream stalled` after ~30s and the player crashes.

The **identical video bitstream** delivered via an `ffmpeg → named pipe → matroska` path on the same box plays flawlessly with full DV engagement.

This issue isolates the divergence to `BitstreamConverter::Open()` in `xbmc/utils/BitstreamConverter.cpp`, and proposes a minimal fix.

## Environment

- **Box**: UGOOS AM6B (Amlogic S922X, rev b)
- **CoreELEC**: `Amlogic-ng.arm-21.3-Omega_p3i_T3b_20260322020632` (pannal p3i build)
- **Kernel**: 4.9.269 aarch64 (32-bit Kodi userspace on 64-bit kernel)
- **Kodi**: built from `pannal/xbmc` branch `aml-4.9-21.3`
- **Display**: Dolby Vision-capable TV over HDMI, DV mode advertised and negotiated (verified in log)
- **Source content**: HEVC Main10 + DV Profile 8.1 (BL=1 EL=0 RPU=1, cross-compatible ID=1), TrueHD 5.1 audio

## Reproduction

1. Take any HEVC P8.1 + TrueHD Matroska source that plays correctly via normal Kodi playback.
2. Remux to fragmented MP4 HLS:
   ```
   ffmpeg -fflags +fastseek -i INPUT.mkv \
     -map 0:v:0 -map 0:a \
     -c:v copy -c:a copy -sn -copyts \
     -strict -2 -tag:v hvc1 \
     -f hls -hls_time 6 -hls_segment_type fmp4 \
     -hls_fmp4_init_filename init.mp4 \
     -hls_segment_filename seg_%06d.m4s \
     -hls_playlist_type vod -hls_flags independent_segments \
     playlist.m3u8
   ```
3. Note ffmpeg 6.x's HLS muxer **strips the `dvvC` box** from the init segment (this is a separate upstream ffmpeg bug — workaround: inject `dvvC` manually into `init.mp4` after ffmpeg writes it; code available, happy to share). Without the `dvvC` box, `hints.hdrType` stays `HDR_TYPE_NONE` and nothing below applies — so this step is required just to reach the bug.
4. Serve the segments over HTTP and play the playlist through Kodi.
5. Observe: `aml_dv_open`, IPT Tunnel, PQ transfer, HDMI DV all engage; all segments download; zero frames produced; stream stalls; player crashes.

Compare against: the same source fed to Kodi via an ffmpeg → named pipe → matroska path on the same box. Works perfectly.

## Analysis: where the paths diverge

Both routes enter `CDVDVideoCodecAmlogic::Open()`, which unconditionally constructs `CBitstreamConverter(m_hints)` and calls `Open(true)`:

- `xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp:307-308`

Divergence happens inside `BitstreamConverter::Open()` based on the shape of `hints.extradata`:

| Step | HLS fmp4 (broken) | Matroska pipe (works) |
|---|---|---|
| `hints.extradata` format | length-prefixed `hvcC` (mov demuxer) | AnnexB (ffmpeg matroska demuxer emits CodecPrivate as-is) |
| `BitstreamConverter.cpp:643` — `extradata[0] \|\| extradata[1] \|\| extradata[2] > 1` | **true** | false |
| `:645` log | `"bitstream to annexb init"` | — |
| `:647` → `BitstreamConvertInitHEVC` | runs, builds SPS/PPS prefix blob (strips non-parameter-set NALs at `:1130-1136`) | skipped |
| `:687` return | — | `return false` |
| `m_convert_bitstream` | `true` | stays `false` |
| Per-packet `Convert()` behavior | `BitstreamConvert()` per-NAL rewrite (`:1448-1516`), prepends SPS/PPS on every IDR (`:1477-1482`), re-serializes RPU via `ProcessDoViRpu` (`:1498-1501`), mixed 3/4-byte start codes (`:1545 vs :1550`) | passthrough (`:758-762`, `m_inputBuffer = pData`) |
| Bytes to `CAMLCodec::AddData` / `codec_write` | reassembled AnnexB | original packet bytes, untouched |

Both paths call `aml_dv_open(hints.hdrType, hints.bitdepth, hints.colorPrimaries)` (`AMLCodec.cpp:2092`) identically. `aml_dv_open` receives only scalars, not buffers — so the DV engagement is byte-independent. The difference is entirely in what ES bytes reach the kernel amstream fd.

## Hypothesized root causes

Two candidate mechanisms are consistent with "opens, 30s wait, zero frames, stall":

### H1: parameter-set carousel on every IDR breaks the active DV pipeline

`BitstreamConvert()` re-prepends VPS/SPS/PPS to every IDR access unit (`BitstreamConverter.cpp:1477-1482`). The matroska pipe path never does this — it passes the packet bytes through untouched, so parameter sets appear only where the remuxer originally placed them.

The Amlogic amvideo kernel driver with DV IPT-tunnel engaged (`aml_dv_send_el_type`, `aml_dv_send_profile`) may not tolerate parameter-set change events inside an active DV session, silently dropping all frames until the next decoder reset.

### H2: RPU NAL reconstruction is not byte-identical

`ProcessDoViRpu` rewrites NAL 62 with a forced 4-byte start code at `BitstreamConverter.cpp:1550-1551`, while adjacent NALs in the same AU use 3-byte separators (`:1545`). This is a valid AnnexB bytestream per spec — but the amvideo DV firmware gates frame emission on RPU byte-position relative to the slice header, so a bit-exact match between "what the encoder laid down" and "what the decoder sees" may be required.

The matroska passthrough path preserves the exact RPU position and start-code widths the encoder emitted; the per-NAL reassembly path cannot guarantee that.

## Proposed fix

**Short-circuit the per-NAL rewrite when the stream is Dolby Vision and the extradata is `hvcC`-format**: pre-convert the extradata to AnnexB once at `Open()` time (so `CAMLCodec::hevc_add_header` still gets a valid AnnexB SPS/VPS/PPS blob), then set `m_convert_bitstream = false` so packet bodies pass through unmodified.

Pseudocode sketch, at `BitstreamConverter::Open()` for HEVC in `xbmc/utils/BitstreamConverter.cpp`:

```cpp
if (m_to_annexb && codec == AV_CODEC_ID_HEVC &&
    (in_extradata[0] || in_extradata[1] || in_extradata[2] > 1))
{
  // Pannal DV passthrough: if this is a DV stream, the per-NAL rewrite path
  // breaks Amlogic DV pipeline (parameter-set carousel on every IDR + RPU
  // byte-position changes). Convert extradata once, then let packets pass
  // through untouched.
  if (m_hints.hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION)
  {
    if (!ConvertHvcCToAnnexB(in_extradata, in_extrasize,
                             &m_extraData, &m_extraSize))
      return false;
    m_convert_bitstream = false;        // force passthrough in Convert()
    CLog::Log(LOGINFO, "CBitstreamConverter::Open DV hvcC passthrough "
                       "(extradata converted once, packet bodies untouched)");
    return true;
  }
  // ... existing hvcC → per-NAL init path
}
```

`ConvertHvcCToAnnexB` would walk the hvcC structure, emit `00 00 00 01 <NAL>` for each VPS/SPS/PPS unit, and write the result into `m_extraData`. The existing `BitstreamConvertInitHEVC` already knows how to walk hvcC arrays; most of that logic can be factored out.

This fix assumes the demuxer is delivering length-prefixed NAL bodies in packets. For pure fmp4/mov HEVC, that is always the case — ffmpeg's mov demuxer emits the sample data as stored, which is length-prefixed per ISO/IEC 14496-15. The Amlogic kernel ES fd would then receive length-prefixed NAL bodies, which is **wrong** for `am-h265` (it expects AnnexB). So a simple "clear `m_convert_bitstream` and passthrough" is insufficient — packet bodies also need length-prefix → start-code conversion, **without** re-prepending parameter sets on every IDR and **without** touching RPU bytes beyond the 4-byte length field.

Refined proposal:

```cpp
// New DOVIMode (or flag) for pannal's DOVIMode enum:
DOVIMode::MODE_PASSTHROUGH_HVCC
```

When set, `Convert()` should:
1. Walk the length-prefixed NAL stream in `pData`.
2. Replace each 4-byte length field with `00 00 00 01` (or `00 00 01`).
3. **Not** re-prepend SPS/PPS on IDR.
4. **Not** route NAL 62 through `ProcessDoViRpu` — copy it verbatim.
5. **Not** touch SEI or HDR10+ at all.

This preserves the encoder's exact NAL ordering and start-code pattern inside each AU, and avoids the `BitstreamConvertInitHEVC` parameter-set stripping. `DVDVideoCodecAmlogic::Open` would select this mode when `hints.hdrType == HDR_TYPE_DOLBYVISION` and extradata is `hvcC`.

## Alternatives considered

- **Expose `m_convert_bitstream` passthrough via a Kodi setting** so users can toggle it from the GUI without a rebuild. Workable but a hidden knob; a correct default is better.
- **Detect the divergence at the demuxer level** and have `DVDDemuxFFmpeg` pre-convert HEVC extradata + packets to AnnexB for Amlogic DV streams. Higher impact, cross-cuts non-DV paths.
- **Fix it in ffmpeg's HLS muxer** so it emits AnnexB-style samples. Not a valid fmp4 — rejected by every other HLS consumer.

## What I can contribute

- Self-contained reproduction: HLS fmp4 test vectors (init + segments + playlist) with injected `dvvC`, plus a standalone Python script that performs the injection against any ffmpeg-produced HLS init segment.
- Kodi log excerpts from both broken (fmp4 HLS) and working (matroska pipe) paths on the same box with the same source, with `setextraloglevel=128` enabled.
- Happy to test a prototype patch — I have SSH access to the test box and can iterate quickly.

## Related

- ffmpeg 6.x HLS muxer drops `dvvC` from fmp4 init segments (separate upstream bug; worked around locally by post-processing). The same ffmpeg with `-f mp4` writes `dvvC` correctly — the bug is isolated to the HLS muxer's init-write path, not the mov muxer.
- This affects any project trying to deliver DV-over-HLS to CoreELEC-Amlogic boxes (e.g., Jellyfin transcoders, custom Kodi players).

---

*Filed against `pannal/xbmc` branch `aml-4.9-21.3` after tracing the code path with multiple source-diff passes against upstream xbmc/xbmc 21.2-Omega. Happy to answer follow-up questions and test patches.*
