# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Shared tri-state result for exact completed-history job lookups."""

from typing import NamedTuple


class ExactJobLookup(NamedTuple):
    """An exact history lookup that preserves transient uncertainty.

    ``valid`` carries the exact completed job. ``stale`` means a successful,
    complete lookup proved that exact job is absent or no longer completed.
    ``transient`` means the backend could not answer conclusively.
    """

    job: object
    lookup_done: bool

    @property
    def state(self):
        if not self.lookup_done:
            return "transient"
        return "valid" if self.job is not None else "stale"

    @classmethod
    def valid(cls, job):
        return cls(job, True)

    @classmethod
    def stale(cls):
        return cls(None, True)

    @classmethod
    def transient(cls):
        return cls(None, False)
