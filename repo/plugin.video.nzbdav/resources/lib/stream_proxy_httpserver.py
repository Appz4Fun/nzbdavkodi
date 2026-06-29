# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Threaded HTTP server for the nzbdav stream proxy.

Stage-final decomposition of ``stream_proxy``: the ``_ThreadedHTTPServer``
class (a worker-bounded, RuntimeError-tolerant ``ThreadingMixIn`` server) was
moved here verbatim. ``stream_proxy`` re-exports it so
``stream_proxy._ThreadedHTTPServer`` keeps resolving for callers and test
patches. Module-level names this class reads at runtime (``xbmc``,
``_MAX_PROXY_WORKERS``) are reached via ``_sp.<name>`` so a test patch on
``resources.lib.stream_proxy`` keeps resolving; the stdlib bases and
``threading`` are imported directly because they are needed at class-definition
time and are never patched.
"""

import threading
from http.server import HTTPServer
from socketserver import ThreadingMixIn as _ThreadingMixIn

import resources.lib.stream_proxy as _sp  # noqa: E402


class _ThreadedHTTPServer(_ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request in a new thread.

    Hardened against thread/stack exhaustion: handler threads are bounded by a
    semaphore (``_MAX_PROXY_WORKERS``) and the per-connection spawn is wrapped
    so a transient ``RuntimeError: can't start new thread`` drops the accepted
    connection cleanly (Kodi reconnects) instead of escaping as an unhandled
    traceback while the listener stays up but unanswering. Stdlib
    ``ThreadingMixIn`` does neither, which is what let a playback burst wedge
    the proxy into "background service unreachable on 127.0.0.1:<port>".
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.stream_context = None
        self.stream_sessions = {}
        self.pending_stream_contexts = {}
        self.active_ffmpeg = None
        self.current_byte_pos = 0
        self.ffmpeg_lock = threading.Lock()
        self.owner_proxy = None
        self.prepare_token = ""  # nosec B105 — empty init value, not a secret
        self._worker_slots = threading.BoundedSemaphore(_sp._MAX_PROXY_WORKERS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        """Spawn a bounded, RuntimeError-tolerant handler thread.

        ``__new__``-built test doubles (and any subclass that skips __init__)
        have no ``_worker_slots`` — fall back to an unbounded-but-guarded spawn
        in that case rather than erroring.
        """
        slots = getattr(self, "_worker_slots", None)
        if slots is not None and not slots.acquire(blocking=False):
            _sp.xbmc.log(
                "NZB-DAV: Proxy at worker cap ({}); dropping connection so the "
                "client reconnects (reason=worker_cap)".format(_sp._MAX_PROXY_WORKERS),
                _sp.xbmc.LOGWARNING,
            )
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except RuntimeError:
            # Out of OS thread/stack budget. Release the slot we reserved and
            # close the accepted socket so the client sees a reset and retries,
            # instead of leaking the connection behind an unhandled traceback.
            if slots is not None:
                try:
                    slots.release()
                except ValueError:
                    pass
            _sp.xbmc.log(
                "NZB-DAV: Could not start proxy handler thread; dropping "
                "connection so the client reconnects (reason=thread_exhausted)",
                _sp.xbmc.LOGWARNING,
            )
            self.shutdown_request(request)

    def process_request_thread(self, request, client_address):
        """Release the worker slot once the handler thread finishes."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            slots = getattr(self, "_worker_slots", None)
            if slots is not None:
                try:
                    slots.release()
                except ValueError:
                    pass
