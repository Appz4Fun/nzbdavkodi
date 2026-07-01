# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Per-request pass-through state holder for ``stream_proxy``.

Stage-final decomposition of ``stream_proxy``: the ``_ProxyStreamState``
``__slots__`` holder used by ``_StreamHandler._serve_proxy`` was moved here
verbatim (its ``__init__`` and ``__slots__`` are unchanged). ``stream_proxy``
re-exports it so ``stream_proxy._ProxyStreamState`` keeps resolving for every
caller and test patch. It captures no module-level state, so the move needs no
``_sp`` indirection.
"""


class _ProxyStreamState:  # pylint: disable=too-few-public-methods
    """Mutable per-request state for ``_StreamHandler._serve_proxy``.

    A plain attribute holder that lets the pass-through loop body be split
    into cohesive per-phase helpers without changing behavior: every value
    that the original single-function loop kept as a local now lives here so
    the extracted step methods can read and mutate the SAME state, in the SAME
    order, exactly as the inline code did.
    """

    __slots__ = (
        "start",
        "end",
        "current",
        "total_streamed",
        "total_skipped",
        "recovery_count",
        "terminal_reason",
        "fallback_pending_fallthroughs",
        "last_fallthrough_streamed",
        "awaiting_download_no_progress",
        "density_window",
        "active_ctx",
        "fallback_pending_candidate",
        "candidate_delivered",
        "fallback_failed_to_notify",
        "streamed_real_upstream_bytes",
        "forward_stall_t0",
        "stall_wait_budget",
        "contract_mode",
        "density_breaker_enabled",
        "zero_fill_budget_enabled",
        "retry_ladder_enabled",
        "result",
        "progressed_this_iter",
    )

    def __init__(self):
        # Slot defaults; every field is overwritten by _serve_proxy_init_state /
        # _serve_proxy_unpack_runtime before it is read. Declared here so the
        # extracted step helpers don't trip attribute-defined-outside-init.
        self.start = 0
        self.end = 0
        self.current = 0
        self.total_streamed = 0
        self.total_skipped = 0
        self.recovery_count = 0
        self.terminal_reason = "unknown"
        self.fallback_pending_fallthroughs = 0
        self.last_fallthrough_streamed = -1
        self.awaiting_download_no_progress = 0
        self.density_window = None
        self.active_ctx = None
        self.fallback_pending_candidate = None
        self.candidate_delivered = False
        self.fallback_failed_to_notify = None
        self.streamed_real_upstream_bytes = 0
        self.forward_stall_t0 = None
        self.stall_wait_budget = 0
        self.contract_mode = None
        self.density_breaker_enabled = False
        self.zero_fill_budget_enabled = False
        self.retry_ladder_enabled = False
        self.result = None
        self.progressed_this_iter = False
