# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Temp-file faststart remux helpers.

Stage-3 mixin split of ``stream_proxy.StreamProxy``. These methods were moved
verbatim; every reference to a ``stream_proxy`` module-level name is reached at
call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``StreamProxy``; they keep using ``self`` for instance state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _MgrFaststartMixin:  # pylint: disable=too-few-public-methods
    """Temp-file faststart remux helpers."""

    @staticmethod
    def _prepare_tempfile_faststart(ffmpeg_path, url, auth_header):
        """Remux MP4 with faststart to a temp file. Returns path or None."""
        if not ffmpeg_path:
            return None

        _sp._validate_url(url)
        fd, temp_path = _sp.tempfile.mkstemp(
            prefix="nzbdav_faststart_",
            suffix=".mp4",
        )
        _sp.os.close(fd)

        cmd = _sp.StreamProxy._build_faststart_cmd(
            ffmpeg_path, url, auth_header, temp_path
        )
        return _sp.StreamProxy._run_faststart_remux(cmd, temp_path)

    @staticmethod
    def _build_faststart_cmd(ffmpeg_path, url, auth_header, temp_path):
        """Build the temp-file faststart remux ffmpeg argv."""
        auth_args = _sp._ffmpeg_auth_args(auth_header)
        cmd = [
            ffmpeg_path,
            "-v",
            "warning",
            "-y",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
        ]
        if auth_args:
            cmd.extend(auth_args)
        cmd.extend(
            [
                "-i",
                url,
                "-map",
                "0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                temp_path,
            ]
        )
        return cmd

    @staticmethod
    def _run_faststart_remux(cmd, temp_path):
        """Run the faststart remux, returning temp_path on success.

        On any failure (non-zero exit, timeout, subprocess error) the
        partial output is removed and None is returned.
        """
        proc = None
        try:
            _sp.xbmc.log(
                "NZB-DAV: Temp-file faststart remux starting", _sp.xbmc.LOGINFO
            )
            proc = _sp.subprocess.Popen(  # nosec B603 — argv list, shell=False
                cmd,
                stdin=_sp.subprocess.DEVNULL,
                stdout=_sp.subprocess.PIPE,
                stderr=_sp.subprocess.PIPE,
                shell=False,
            )
            _, stderr = proc.communicate(timeout=600)  # 10 min timeout
            result = _sp.StreamProxy._faststart_remux_result(proc, stderr, temp_path)
            if result is not None:
                return result
            # Fall through to the cleanup block below instead of
            # returning early — the partial output that ffmpeg
            # leaves on a failed remux otherwise leaks until the OS
            # next clears tempdir. Closes TODO.md §H.3 (mkstemp
            # leak on TimeoutExpired / SubprocessError) for the
            # ffmpeg-non-zero-exit case in particular.
        except _sp.subprocess.TimeoutExpired as e:
            # communicate() timing out does NOT kill the child. Without
            # an explicit kill + reap, the ffmpeg process orphans and
            # holds the output fd + the inbound HTTP socket, potentially
            # for hours. Kill + drain the pipe before the exception
            # propagates; .communicate() on the killed proc reaps it.
            _sp.xbmc.log(
                "NZB-DAV: Temp faststart timed out after 600s; killing ffmpeg "
                "(reason=temp_faststart_timeout)",
                _sp.xbmc.LOGWARNING,
            )
            _sp.StreamProxy._kill_and_reap_faststart(proc)
            _ = e  # keep linters quiet; exception detail already logged
        except (OSError, _sp.subprocess.SubprocessError) as e:
            _sp.xbmc.log(
                "NZB-DAV: Temp faststart error: {}".format(e), _sp.xbmc.LOGWARNING
            )
            # Non-timeout subprocess errors usually mean Popen itself
            # failed or communicate() hit a pipe error. Still try to
            # reap the child defensively — it's cheap when the proc
            # already exited and essential when it didn't.
            if proc is not None and proc.poll() is None:
                _sp.StreamProxy._kill_and_reap_faststart(proc)
        _sp.StreamProxy._remove_temp_file(temp_path)
        return None

    @staticmethod
    def _faststart_remux_result(proc, stderr, temp_path):
        """Evaluate a completed faststart proc.

        Returns temp_path on success (rc==0 and non-empty output), or
        None on failure (after logging the redacted ffmpeg stderr).
        """
        if proc.returncode != 0:
            # ffmpeg error messages routinely echo the full input URL,
            # including embedded basic-auth (legacy callers) and
            # apikey=... query strings. Strip those before they land in
            # kodi.log. Closes TODO.md §H.2-H2b.
            _sp.xbmc.log(
                "NZB-DAV: Temp faststart failed: {}".format(
                    _sp._redact_text(stderr.decode(errors="replace")[:300])
                ),
                _sp.xbmc.LOGWARNING,
            )
            return None
        if _sp.os.path.exists(temp_path) and _sp.os.path.getsize(temp_path) > 0:
            return temp_path
        return None

    @staticmethod
    def _kill_and_reap_faststart(proc):
        """Kill + drain a faststart ffmpeg child (documented reap idiom)."""
        try:
            proc.kill()
            proc.communicate(timeout=5)
        except (OSError, _sp.subprocess.SubprocessError):
            pass

    @staticmethod
    def _remove_temp_file(temp_path):
        """Best-effort removal of a temp file if it exists."""
        if _sp.os.path.exists(temp_path):
            try:
                _sp.os.remove(temp_path)
            except OSError:
                pass
