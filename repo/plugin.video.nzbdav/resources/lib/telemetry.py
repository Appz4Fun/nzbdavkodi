"""Lightweight timing telemetry helpers."""

import time
from contextlib import contextmanager

import xbmc


def log_timing(label, elapsed_ms, **fields):
    """Log one timing sample without letting Kodi logging failures escape."""
    parts = [
        "NZB-DAV: timing {}".format(label),
        "elapsed_ms={:.1f}".format(elapsed_ms),
    ]
    for key in sorted(fields):
        parts.append("{}={}".format(key, fields[key]))
    try:
        xbmc.log(" ".join(parts), xbmc.LOGDEBUG)
    except Exception:  # pylint: disable=broad-except
        pass


@contextmanager
def timed_block(label, **fields):
    """Measure a block and emit one debug timing line."""
    started = time.monotonic()
    try:
        yield
    finally:
        log_timing(label, (time.monotonic() - started) * 1000.0, **fields)
