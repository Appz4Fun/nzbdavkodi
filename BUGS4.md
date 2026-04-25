# BUGS4 - Proxy Bug Status

Date: 2026-04-25

Status: all BUGS4 items are fixed and locally verified.

Fixed items:
- B4-001: recovery exhaustion now closes instead of zero-filling the remaining response.
- B4-002: losing duplicate remux handlers no longer stream or finish the winner's ffmpeg process.
- B4-003: virtual MP4 faststart clamps upstream payload reads to the advertised response range.
- B4-004: HLS playlist proxy URLs carry `application/vnd.apple.mpegurl` metadata.
- B4-005: the default force-remux threshold is below the documented 15.8 GiB crash case.
- B4-007: fMP4 live segments require a next-segment or terminal-process completion signal.
- B4-008: MP4 range fetches reject non-206 and mismatched range responses.
- B4-009: fetched `moov` boxes must match the declared length and parsed box size.
- B4-010: remux stdout reads have an idle watchdog so stalled ffmpeg output is cleaned up.
- B4-011: piped Matroska remux no longer maps response byte offsets to source timestamps.
- B4-012: HLS playlist serving prefers ffmpeg-generated durations when available.
- B4-013: HLS workdir selection checks required free space.
- B4-014: `/prepare` requires a service token and rejects control characters in auth headers.
- B4-015: pending HLS warmup contexts are tracked and cleaned during session clears.
- B4-016: playback-never-started cleanup now clears active proxy sessions.

Validation:
- `python3 -m pytest tests/test_stream_proxy.py tests/test_mp4_parser.py tests/test_resolver.py::test_play_direct_hls_proxy_sets_playlist_mime tests/test_resolver.py::test_apply_proxy_mime_matroska_remux_still_sets_matroska tests/test_service.py::test_tick_cleans_proxy_when_playback_never_started -q`
- `python3 -m pytest tests/test_dv_source.py::test_probe_mp4_profile8_from_first_sample tests/test_dv_source.py::test_probe_mp4_without_rpu_is_non_dv tests/test_dv_source.py::test_probe_mp4_with_co64_chunk_offsets tests/test_dv_source.py::test_probe_mp4_clamps_unreasonable_first_sample_size -q`
- `just lint`
- `just test`
