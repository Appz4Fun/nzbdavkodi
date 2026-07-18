# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Submit-error classification and dialogs.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


def _surface_terminal_submit_error(dialog, submit_error, selected_indexer):
    """Close the progress dialog and show the terminal submit error dialog."""
    _resolver._close_dialog_before_submit_error(dialog)
    _resolver._show_submit_error_dialog(
        _resolver._submit_error_with_indexer(submit_error, selected_indexer)
    )


def _terminal_submit_error_result(submit_error, ctx):
    """Surface a terminal submit error and signal the loop to stop."""
    _surface_terminal_submit_error(ctx["dialog"], submit_error, ctx["selected_indexer"])
    return "return", None


def _adopt_after_submit_failure(submit_error, ctx, after_timeout):
    """Probe queue/history after a timeout or non-transient HTTP submit error.

    Returns ``("return", adopted_nzo_id)`` when an existing job was adopted,
    otherwise ``None`` to let the caller continue its classification.
    """
    title = ctx["title"]
    adopted_nzo_id = _resolver._adopt_queued_or_completed_job(
        title,
        ctx["monitor"],
        settings_getter=ctx["settings_getter"],
        rejected_completed_ids=ctx["rejected_completed_ids"],
    )
    if not adopted_nzo_id:
        return None
    if after_timeout:
        suffix = "after submit timeout"
    else:
        suffix = "after HTTP {} rejection".format(submit_error["status"])
    _resolver.xbmc.log(
        "NZB-DAV: Adopted existing nzbdav job nzo_id={} for '{}' {}".format(
            adopted_nzo_id, title, suffix
        ),
        _resolver.xbmc.LOGINFO,
    )
    return "return", adopted_nzo_id


def _handle_submit_attempt_error(submit_error, ctx):
    """Classify a failed submit attempt's error.

    ``ctx`` carries the per-call context (title, dialog, monitor,
    attempt_label, settings_getter, selected_indexer, rejected_completed_ids).
    Returns ``("return", value)`` when the retry loop must stop and return
    ``value``, or ``("retry", None)`` when the caller should keep retrying.
    """
    status = submit_error["status"]
    title = ctx["title"]
    if status in ("cancelled", "shutdown"):
        # User hit cancel on the progress dialog or Kodi is shutting down.
        # Stop immediately — no retry, no adoption, no error dialog.
        _resolver.xbmc.log(
            "NZB-DAV: Submit aborted ({}) for '{}'".format(status, title),
            _resolver.xbmc.LOGINFO,
        )
        return "return", None
    if status == "timeout":
        return _handle_submit_timeout(submit_error, ctx)
    if status in _resolver._TRANSIENT_HTTP_STATUSES:
        _resolver.xbmc.log(
            "NZB-DAV: Submit attempt {} hit transient HTTP {}: {}".format(
                ctx["attempt_label"],
                status,
                _resolver._redact_log(submit_error["message"]),
            ),
            _resolver.xbmc.LOGWARNING,
        )
        return "retry", None
    if status == "rejected":
        return _handle_submit_rejected(submit_error, ctx)
    if isinstance(status, int) and 400 <= status < 500:
        return _handle_submit_4xx(submit_error, ctx)
    return _handle_submit_nontransient(submit_error, ctx)


def _handle_submit_rejected(submit_error, ctx):
    """Handle an explicit nzbdav NZB rejection (not retryable).

    nzbdav explicitly rejected the NZB (empty / truncated / password-only /
    unparseable). Surface the specific message immediately instead of looping
    3× and showing a generic failure.
    """
    _resolver.xbmc.log(
        "NZB-DAV: nzbdav rejected the NZB for '{}': {}".format(
            ctx["title"], _resolver._redact_log(submit_error["message"])
        ),
        _resolver.xbmc.LOGERROR,
    )
    return _terminal_submit_error_result(submit_error, ctx)


def _handle_submit_4xx(submit_error, ctx):
    """Handle a terminal HTTP 4xx submit rejection (no job to adopt).

    HTTP 4xx means nzbdav reached upstream and got a terminal client/indexer-
    side rejection (for example Hydra 429 mapped to nzbdav's HTTP 400). There
    is no nzbdav job to adopt, and probing queue/history just leaves the
    progress dialog stuck.
    """
    _resolver.xbmc.log(
        "NZB-DAV: Submit failed with HTTP {}, not probing queue: {}".format(
            submit_error["status"], _resolver._redact_log(submit_error["message"])
        ),
        _resolver.xbmc.LOGERROR,
    )
    return _terminal_submit_error_result(submit_error, ctx)


def _handle_submit_timeout(submit_error, ctx):
    """Handle a client-side submit timeout: probe-then-adopt, else retry.

    nzbdav's submit handler can take > 30 s on big NZBs (parse +
    enumerate) — longer than the default HTTP timeout. A timeout does
    NOT mean the submit failed. Probe the queue before retrying so we adopt the
    job nzbdav is already processing instead of double-submitting.
    """
    title = ctx["title"]
    _resolver.xbmc.log(
        "NZB-DAV: Submit attempt {} timed out; probing nzbdav "
        "queue for '{}' before retrying".format(ctx["attempt_label"], title),
        _resolver.xbmc.LOGWARNING,
    )
    adopted = _adopt_after_submit_failure(submit_error, ctx, after_timeout=True)
    if adopted is not None:
        return adopted
    _resolver.xbmc.log(
        "NZB-DAV: '{}' not found in nzbdav queue or history "
        "after submit timeout; retrying".format(title),
        _resolver.xbmc.LOGWARNING,
    )
    return "retry", None


def _handle_submit_nontransient(submit_error, ctx):
    """Handle a non-transient HTTP error (often 500 "duplicate nzo_id").

    Before surfacing the error to the user, probe the queue: if the job is
    already running, attach to it. This covers the race where a concurrent
    submit (e.g. retried play of the same title) beat us to nzbdav.
    """
    adopted = _adopt_after_submit_failure(submit_error, ctx, after_timeout=False)
    if adopted is not None:
        return adopted
    _resolver.xbmc.log(
        "NZB-DAV: Submit failed with HTTP {}, not retrying: {}".format(
            submit_error["status"], _resolver._redact_log(submit_error["message"])
        ),
        _resolver.xbmc.LOGERROR,
    )
    return _terminal_submit_error_result(submit_error, ctx)


def _build_submit_error_ctx(
    title, dialog, monitor, settings_getter, selected_indexer, rejected_completed_ids
):
    """Build the shared per-call context for submit-error classification."""
    return {
        "title": title,
        "dialog": dialog,
        "monitor": monitor,
        "attempt_label": "",
        "settings_getter": settings_getter,
        "selected_indexer": selected_indexer,
        "rejected_completed_ids": rejected_completed_ids,
    }
