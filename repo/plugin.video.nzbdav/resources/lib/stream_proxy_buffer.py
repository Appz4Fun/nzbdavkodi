# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Per-session bounded forward read-ahead buffer for the stream proxy.

Extracted verbatim from ``stream_proxy.py`` (Stage 1 decomposition). The
``ReadAheadBuffer`` class is fully self-contained (only ``threading``) and is
re-exported by ``stream_proxy`` so existing ``stream_proxy.ReadAheadBuffer``
references and test patches keep resolving.
"""

import threading


class ReadAheadBuffer:
    """A per-session bounded contiguous forward read-ahead window.

    Holds a single contiguous run of bytes ``data`` starting at
    ``base_offset`` (the first buffered byte). A daemon prefetch thread
    appends bytes strictly contiguous-forward; the serve path reads the
    contiguous prefix starting exactly at the requested offset and frees
    bytes behind the play head. The window never grows past ``cap_bytes``
    nor past ``content_length``. A SEEK outside the window discards the
    stale data and repoints ``base_offset`` so the prefetch thread refills
    from the seek target. All mutation is guarded by a short lock so the
    serve path never blocks on the prefetch thread for long.
    """

    def __init__(self, cap_bytes, content_length):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        try:
            self.cap_bytes = max(0, int(cap_bytes or 0))
        except (TypeError, ValueError):
            self.cap_bytes = 0
        try:
            self.content_length = max(0, int(content_length or 0))
        except (TypeError, ValueError):
            self.content_length = 0
        self.base_offset = 0
        self.data = bytearray()
        self.served_high_water = 0

    def read_prefix(self, start, end):
        """Return the contiguous bytes the window holds starting exactly at
        ``start`` (b'' on miss/gap/seek-mismatch), capped to ``end`` and to
        what is present. The strict ``start == base_offset`` invariant means a
        stale window can never feed wrong-offset bytes to Kodi after a seek."""
        if not isinstance(start, int) or not isinstance(end, int):
            return b""
        if end < start:
            return b""
        with self._lock:
            if not self.data or start != self.base_offset:
                return b""
            want = end - start + 1
            return bytes(self.data[:want])

    def append(self, offset, chunk):
        """Add ``chunk`` only if it is strictly contiguous-forward (``offset``
        == base_offset+len(data)). Truncates at ``cap_bytes`` and at
        ``content_length``. Returns True when any bytes were stored."""
        if not chunk:
            return False
        with self._lock:
            expected = self.base_offset + len(self.data)
            if offset != expected:
                return False
            room = self.cap_bytes - len(self.data)
            if room <= 0:
                return False
            eof_room = self.content_length - expected
            if eof_room <= 0:
                return False
            limit = min(room, eof_room, len(chunk))
            if limit <= 0:
                return False
            self.data.extend(chunk[:limit])
            return True

    def free_behind(self, served_offset):
        """Drop bytes before ``served_offset`` and advance ``base_offset``.
        A no-op when ``served_offset`` is at or behind the current base."""
        try:
            served_offset = int(served_offset)
        except (TypeError, ValueError):
            return
        with self._lock:
            drop = served_offset - self.base_offset
            if drop <= 0:
                return
            drop = min(drop, len(self.data))
            del self.data[:drop]
            self.base_offset += drop

    def update_served_high_water(self, offset):
        """Record the highest offset delivered to the client.

        When the play head has been served PAST the end of the buffered window
        (the common startup case: an initial read MISSES the read-ahead window
        and is served directly from upstream, so ``free_behind`` has no buffered
        bytes to consume), repoint ``base_offset`` to the served offset and drop
        any now-behind stale data. This advances ``next_fetch_offset`` so the
        prefetch daemon builds a FORWARD lead from the play head instead of
        re-fetching from offset 0 behind it.

        For the IN-WINDOW case (``base_offset < offset < window_end``: a
        read-ahead miss served directly upstream while the prefetch had already
        filled bytes from the old ``base_offset``), trim the now-consumed prefix
        ``[base_offset, offset)`` and advance ``base_offset`` to the served
        offset. This keeps the forward lead instead of letting the consumed
        prefix pin the window behind the play head and throttle the prefetch.
        The trim is INLINED (not ``free_behind``) because ``self._lock`` is a
        non-reentrant ``threading.Lock`` already held here."""
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            return
        with self._lock:
            self.served_high_water = max(self.served_high_water, offset)
            window_end = self.base_offset + len(self.data)
            if offset >= window_end:
                self.data = bytearray()
                self.base_offset = offset
            elif offset > self.base_offset:
                del self.data[: offset - self.base_offset]
                self.base_offset = offset

    def note_seek(self, new_start):
        """Repoint the window for a seek.

        ``read_prefix`` only serves when ``start == base_offset``, so an
        in-window FORWARD seek must advance ``base_offset`` to the seek target
        or the buffered lead is missed and refetched. We therefore TRIM the
        now-behind prefix ``[base_offset, new_start)`` and keep the still-ahead
        bytes ``[new_start, window_end)``, so a skip forward into the buffered
        lead is served from memory. An out-of-window seek (forward past the
        lead or any backward seek before ``base_offset``) discards the data and
        sets ``base_offset`` to the seek target so the prefetch thread refills
        forward. Never blocks the seek — a cheap lock-protected pointer reset."""
        if not isinstance(new_start, int) or new_start < 0:
            return
        with self._lock:
            window_end = self.base_offset + len(self.data)
            if new_start == self.base_offset:
                return
            if self.base_offset < new_start <= window_end:
                # In-window forward seek: drop the consumed prefix, keep the lead.
                del self.data[: new_start - self.base_offset]
                self.base_offset = new_start
                self.served_high_water = max(self.served_high_water, new_start)
                return
            self.data = bytearray()
            self.base_offset = new_start
            self.served_high_water = max(self.served_high_water, new_start)

    def space_remaining(self):
        with self._lock:
            remaining = self.cap_bytes - len(self.data)
        return remaining if remaining > 0 else 0

    def next_fetch_offset(self):
        with self._lock:
            return self.base_offset + len(self.data)

    def is_full(self):
        with self._lock:
            return len(self.data) >= self.cap_bytes

    def stop(self):
        self._stop.set()

    def should_stop(self):
        return self._stop.is_set()
