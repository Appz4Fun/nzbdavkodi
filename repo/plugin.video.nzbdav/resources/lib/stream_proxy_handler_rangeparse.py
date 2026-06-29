# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Skip-probe offset search, zero-fill, and HTTP Range header parsing.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


class _RangeParseMixin:  # pylint: disable=too-few-public-methods
    """Skip-probe offset search, zero-fill, and HTTP Range header parsing."""

    @staticmethod
    def _retry_skip_probe(ctx, skip, target, probe_end, start_time):
        """Retry one skip size with backoff. Returns (succeeded, aborted).

        ``aborted`` is True when the recovery budget is exhausted or Kodi is
        shutting down, signalling the caller to stop probing entirely.
        """
        probe_monitor = _sp.xbmc.Monitor()
        for delay in (0,) + _sp._PROBE_RETRY_DELAYS:
            if _sp.time.monotonic() - start_time >= _sp._MAX_RECOVERY_SECONDS:
                return False, True
            # waitForAbort yields the same backoff as time.sleep but returns
            # True (and aborts the loop) when Kodi is shutting down.
            # TODO.md §H.2-M14.
            if delay and probe_monitor.waitForAbort(delay):
                return False, True
            if _sp._StreamHandler._skip_probe_succeeds(
                ctx, skip, target, probe_end, start_time
            ):
                return True, False
        return False, False

    @staticmethod
    def _skip_probe_succeeds(ctx, skip, target, probe_end, start_time):
        """Probe one range; True only when upstream serves non-empty bytes."""
        req = _sp.Request(ctx["remote_url"])
        _sp._add_request_headers(req, ctx.get("auth_header"))
        req.add_header("Range", "bytes={}-{}".format(target, probe_end))
        try:
            # nosemgrep
            with _sp.urlopen(  # nosec B310 — URL from user-configured stream
                req, timeout=_sp._SKIP_PROBE_TIMEOUT
            ) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                if status not in (200, 206):
                    return False
                # Validate the probe actually returned bytes — an upstream
                # that 206s with an empty body would otherwise be accepted
                # as recovered, sending the main loop straight back into the
                # same bad region on the next range read.
                body = resp.read(64)
                if not body:
                    _sp.xbmc.log(
                        "NZB-DAV: Probe at +{} bytes returned status={} but "
                        "empty body; treating as probe failure".format(skip, status),
                        _sp.xbmc.LOGWARNING,
                    )
                    return False
                elapsed = _sp.time.monotonic() - start_time
                _sp.xbmc.log(
                    "NZB-DAV: Probe succeeded at +{} bytes after "
                    "{:.1f}s".format(skip, elapsed),
                    _sp.xbmc.LOGINFO,
                )
                return True
        except (OSError, ValueError) as e:
            _sp.xbmc.log(
                "NZB-DAV: Probe at +{} bytes failed ({}): {}".format(
                    skip, type(e).__name__, e
                ),
                _sp.xbmc.LOGDEBUG,
            )
            return False

    def _write_zeros(self, count):
        """Write 'count' zero bytes to the client in fixed-size chunks."""
        remaining = count
        while remaining > 0:
            chunk_size = min(remaining, len(_sp._ZERO_FILL_BUFFER))
            self.wfile.write(_sp._ZERO_FILL_BUFFER[:chunk_size])
            remaining -= chunk_size

    @staticmethod
    def _parse_range_spec(range_header):
        """Return the validated single-range spec text, or None."""
        if not isinstance(range_header, str) or not range_header.startswith("bytes="):
            return None
        range_spec = range_header[len("bytes=") :].strip()
        if "," in range_spec or "-" not in range_spec:
            return None
        return range_spec

    @staticmethod
    def _parse_suffix_range(suffix_text, content_length):
        """Parse a ``-N`` suffix range into (start, end) or (None, None)."""
        suffix = int(suffix_text)
        if suffix <= 0 or suffix > content_length:
            return None, None
        return content_length - suffix, content_length - 1

    @staticmethod
    def _parse_bounded_range(range_spec, content_length):
        """Parse a ``start-`` / ``start-end`` range into (start, end)."""
        start_text, end_text = range_spec.split("-", 1)
        if not start_text:
            return None, None
        start = int(start_text)
        if start < 0 or start >= content_length:
            return None, None
        end = int(end_text) if end_text else content_length - 1
        if end < start:
            return None, None
        return start, min(end, content_length - 1)

    @staticmethod
    def _parse_range(range_header, content_length):
        """Parse Range header, return (start, end) or (None, None)."""
        try:
            if content_length <= 0:
                return None, None
            range_spec = _sp._StreamHandler._parse_range_spec(range_header)
            if range_spec is None:
                return None, None
            if range_spec.startswith("-"):
                return _sp._StreamHandler._parse_suffix_range(
                    range_spec[1:], content_length
                )
            return _sp._StreamHandler._parse_bounded_range(range_spec, content_length)
        except (ValueError, IndexError):
            return None, None
