# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Duration / content-length / content-type probing helpers.

Stage-3 mixin split of ``stream_proxy.StreamProxy``. These methods were moved
verbatim; every reference to a ``stream_proxy`` module-level name is reached at
call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``StreamProxy``; they keep using ``self`` for instance state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402
from resources.lib.http_util import prefer_ipv4_connections  # noqa: E402


class _MgrProbeMixin:  # pylint: disable=too-few-public-methods
    """Duration / content-length / content-type probing helpers."""

    @staticmethod
    def _probe_duration(ffmpeg_path, url, auth_header):
        """Probe file duration. Returns seconds or None.

        Two strategies, tried in order:

        1. ``ffprobe -show_entries format=duration`` — the clean path. One
           number on stdout, no stream-probe warnings. This is the only
           reliable approach for files with many subtitle streams: a 30-
           subtitle Blu-ray remux produces a wall of ``Could not find
           codec parameters for stream N (Subtitle: hdmv_pgs_subtitle)``
           warnings from ffmpeg that can trivially push ``Duration:`` past
           any stderr buffer budget before it gets a chance to print.
        2. ``ffmpeg -i <url> -f null -`` parsed out of stderr — the
           fallback path when ffprobe isn't installed. Budget is 64 KB
           (up from the original 8 KB) so the subtitle-warning wall
           doesn't evict the Duration line on pathological inputs.

        Args:
            ffmpeg_path: Path to the ffmpeg binary. Used by the fallback
                path and as the starting point for ffprobe discovery.
            url: Remote HTTP URL to probe.
            auth_header: Optional Basic auth header; passed to the child
                process via ``-headers`` so the input URL stays clean.
        """
        _sp._validate_url(url)
        auth_args = _sp._ffmpeg_auth_args(auth_header)

        ffprobe_path = _sp._find_ffprobe()
        if ffprobe_path:
            result = _sp.StreamProxy._probe_duration_ffprobe(
                ffprobe_path, url, auth_args=auth_args
            )
            if result is not None:
                return result

        return _sp.StreamProxy._probe_duration_ffmpeg(
            ffmpeg_path, url, auth_args=auth_args
        )

    @staticmethod
    def _probe_duration_ffprobe(ffprobe_path, input_url, auth_args=None):
        """Run ffprobe to get duration. Returns seconds or None."""
        try:
            cmd = [
                ffprobe_path,
                "-v",
                "error",
            ]
            if auth_args:
                cmd.extend(auth_args)
            cmd.extend(
                [
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=nokey=1:noprint_wrappers=1",
                    input_url,
                ]
            )
            proc = _sp.subprocess.Popen(  # nosec B603 — argv list, shell=False
                cmd,
                stdin=_sp.subprocess.DEVNULL,
                stdout=_sp.subprocess.PIPE,
                stderr=_sp.subprocess.PIPE,
                shell=False,
            )
            try:
                stdout_bytes, _ = proc.communicate(timeout=30)
            except _sp.subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.communicate(timeout=5)
                except (OSError, _sp.subprocess.SubprocessError):
                    pass
                _sp.xbmc.log("NZB-DAV: ffprobe duration timed out", _sp.xbmc.LOGWARNING)
                return None
            if proc.returncode != 0:
                return None
            text = stdout_bytes.decode("utf-8", "replace").strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return None
        except (OSError, _sp.subprocess.SubprocessError) as e:
            _sp.xbmc.log("NZB-DAV: ffprobe failed: {}".format(e), _sp.xbmc.LOGWARNING)
            return None

    @staticmethod
    def _probe_duration_ffmpeg(ffmpeg_path, input_url, auth_args=None):
        """Parse Duration out of ``ffmpeg -i`` stderr. Returns seconds or None.

        Uses the bounded-reader-thread pattern: a daemon thread reads
        stderr line-by-line into a shared buffer and signals an Event
        as soon as ``Duration:`` is matched or the 64 KB byte budget
        is exhausted. The main thread waits on the Event with a
        hard wall-clock deadline of ``_PROBE_DEADLINE_SECONDS`` so a
        stuck ffmpeg (slow upstream, stalled header parse, auth hang)
        can't wedge the probe forever. Either way — match, budget,
        deadline — the ffmpeg process is killed before returning.
        """
        return _sp.StreamProxy._probe_ffmpeg_stderr(
            ffmpeg_path,
            input_url,
            _sp._parse_ffmpeg_duration,
            "Duration",
            auth_args=auth_args,
        )

    @staticmethod
    def _probe_ffmpeg_stderr(ffmpeg_path, input_url, parser, label, auth_args=None):
        """Shared body of ``_probe_duration_ffmpeg`` (DV probing now uses
        the pure-Python ``dv_source.probe_dolby_vision_source``, which
        parses real RPU data instead of relying on ffmpeg's stderr).
        Spawns ``ffmpeg -v info -i <url> -f null -`` and runs the parser
        against collected stderr under a bounded reader thread +
        wall-clock deadline.

        ``auth_args`` is an optional list of ffmpeg argv pieces (from
        ``_ffmpeg_auth_args``) that gets inserted before ``-i`` so the
        Authorization header is passed via ``-headers`` instead of
        being spliced into the input URL. Callers that build the URL
        with ``_embed_auth_in_url`` should leave this None; new
        callers should prefer the ``-headers`` form.
        """
        cmd = [ffmpeg_path, "-v", "info"]
        if auth_args:
            cmd.extend(auth_args)
        cmd.extend(["-i", input_url, "-f", "null", "-"])

        proc = _sp.StreamProxy._spawn_probe_proc(cmd, label)
        if proc is None:
            return None

        collected = [""]
        done = _sp.threading.Event()
        # 64 KB budget: large enough that a 30-subtitle Blu-ray remux's
        # wall of per-stream probe warnings can't push the match line out.
        budget = 65536

        reader = _sp.threading.Thread(
            target=_sp.StreamProxy._probe_stderr_reader,
            args=(proc, parser, collected, budget, done, label),
            name="nzbdav-probe-reader",
            daemon=True,
        )
        reader.start()

        if not done.wait(timeout=_sp._PROBE_DEADLINE_SECONDS):
            _sp.xbmc.log(
                "NZB-DAV: {} probe wall-clock deadline ({}s) exceeded, "
                "killing ffmpeg".format(label, _sp._PROBE_DEADLINE_SECONDS),
                _sp.xbmc.LOGWARNING,
            )

        return _sp.StreamProxy._finish_probe(
            proc, reader, parser, collected, budget, label
        )

    @staticmethod
    def _spawn_probe_proc(cmd, label):
        """Spawn an ffmpeg probe process; log + return None on spawn failure."""
        try:
            return _sp.subprocess.Popen(  # nosec B603 — argv list, shell=False
                cmd,
                stdin=_sp.subprocess.DEVNULL,
                stdout=_sp.subprocess.DEVNULL,
                stderr=_sp.subprocess.PIPE,
                shell=False,
            )
        except (OSError, _sp.subprocess.SubprocessError, ValueError) as e:
            _sp.xbmc.log(
                "NZB-DAV: {} probe spawn failed: {}".format(label, e),
                _sp.xbmc.LOGWARNING,
            )
            return None

    @staticmethod
    def _probe_stderr_reader(proc, parser, collected, budget, done, label):
        """Read ffmpeg stderr into ``collected`` until match/budget/EOF."""
        try:
            for line in proc.stderr:
                collected[0] += line.decode(errors="replace")
                if parser(collected[0]) is not None:
                    return
                if len(collected[0]) > budget:
                    return
        except Exception as exc:  # pylint: disable=broad-except
            # Log the failure mode rather than swallowing silently —
            # a stderr decode error or pipe close that hides a real
            # probe failure used to surface as duration=None with no
            # diagnostic in kodi.log. Closes TODO.md §H.3 silent
            # stderr-reader failure.
            _sp.xbmc.log(
                "NZB-DAV: {} probe stderr-reader failed: {}".format(label, exc),
                _sp.xbmc.LOGWARNING,
            )
        finally:
            done.set()

    @staticmethod
    def _finish_probe(proc, reader, parser, collected, budget, label):
        """Kill the probe proc, join its reader, and return the parsed result."""
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (_sp.subprocess.TimeoutExpired, OSError):
            pass

        # Join the reader thread now that the proc is dead — its EOF on
        # stderr is the loop-exit condition, so once kill() takes effect
        # the thread should terminate within milliseconds. Joining here
        # (with a small timeout safety net) prevents probe threads from
        # accumulating in long-running services that probe a lot, e.g. a
        # search session that prepares many candidate streams.
        # daemon=True still covers the pathological case where stderr
        # never EOFs. Closes TODO.md §H.3 probe-reader thread leak.
        reader.join(timeout=2)

        result = parser(collected[0])
        if result is None and len(collected[0]) > budget:
            _sp.xbmc.log(
                "NZB-DAV: {} not found in first {}B of ffmpeg output".format(
                    label, budget
                ),
                _sp.xbmc.LOGWARNING,
            )
        return result

    @staticmethod
    def _get_content_length(url, auth_header, content_length_hint=None):
        """Get file size via HEAD or range probe."""
        content_length_hint = _sp._normalize_content_length_hint(content_length_hint)
        if content_length_hint > 0:
            confirmed = _sp._probe_content_length_hint(
                url, auth_header, content_length_hint
            )
            if confirmed > 0:
                return confirmed

        req = _sp.Request(url, method="HEAD")
        _sp._add_request_headers(req, auth_header)
        try:
            with prefer_ipv4_connections():
                # nosemgrep
                with _sp.urlopen(  # nosec B310 — URL from user-configured nzbdav/WebDAV setting
                    req, timeout=10
                ) as resp:
                    return int(resp.headers.get("Content-Length", 0))
        except (OSError, ValueError):
            pass
        return _sp._probe_content_length_tail(url, auth_header)

    @staticmethod
    def _detect_content_type(url):
        """Detect content type from URL extension."""
        lower = url.lower()
        if lower.endswith(".mkv"):
            return "video/x-matroska"
        if lower.endswith((".mp4", ".m4v")):
            return "video/mp4"
        if lower.endswith(".avi"):
            return "video/x-msvideo"
        if lower.endswith((".ts", ".m2ts")):
            return "video/mp2t"
        return "video/mp4"
