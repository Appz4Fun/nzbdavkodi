# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Per-session tracking of provably-dead fallback candidates.

A "dead" candidate is a Usenet release that has failed in a way re-trying
cannot fix: its first article is missing, the NNTP server rejected it, or its
nzbdav job reached a terminal Failed/Deleted state. Such a release must never
be (re-)admitted to the fallback pool for the rest of the playback session.

A *timeout* is deliberately NOT dead: on a slow nzbdav backend a timeout often
means load, not a missing post, so a timed-out candidate stays eligible.

Keyed primarily by ``nzb_url`` (the indexer ``link``) because that is the only
identifier stable across both the picker's primary attempt and the fallback
worker -- ``job_name`` embeds a per-position index and an nzb_url digest, and
``nzo_id`` changes on every resubmit. ``nzo_id`` is tracked additionally so the
live push path (which knows only ``nzo_id``) can drop an adopted standby.
"""


class DeadCandidates:
    """A small per-session set of dead candidate identities."""

    def __init__(self):
        self._urls = set()
        self._nzo_ids = set()

    def add(self, nzb_url=None, nzo_id=None):
        """Record a dead candidate by nzb_url and/or nzo_id (empties ignored)."""
        if nzb_url:
            self._urls.add(nzb_url)
        if nzo_id:
            self._nzo_ids.add(nzo_id)

    def has_url(self, nzb_url):
        return bool(nzb_url) and nzb_url in self._urls

    def has_nzo(self, nzo_id):
        return bool(nzo_id) and nzo_id in self._nzo_ids


# Statuses the primary submit path (resolver.py) retries, so a single such
# failure is a transient backend hiccup, not proof the post is dead. Kept as a
# local constant to avoid a circular import (resolver imports this module).
_TRANSIENT_HTTP_STATUSES = (408, 502, 503, 504)


def is_provably_dead_submit_error(submit_error):
    """Return True when a ``submit_nzb`` error means the post is dead.

    ``submit_nzb`` returns ``(None, {"status": <int|str>, "message": str})`` on
    failure. A ``"timeout"`` status and the transient HTTP statuses the primary
    submit path retries (``408/502/503/504``) are NOT provably dead -- they
    signal a slow or hiccuping backend, so the candidate stays eligible. Any
    other status (HTTP 5xx like ``500``, ``"rejected"``, or an unknown status)
    is treated as provably dead -- conservatively dead so we do not loop on a
    doomed candidate. A bare ``None`` (or any non-dict) is not a classified
    submit error, so it is not provably dead.
    """
    if not isinstance(submit_error, dict):
        return False
    status = submit_error.get("status")
    if status == "timeout" or status in _TRANSIENT_HTTP_STATUSES:
        return False
    return True
