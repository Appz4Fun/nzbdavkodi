# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Shared HTTP and Kodi utility functions."""

from urllib.request import Request, urlopen


def http_get(url, timeout=15):
    """Perform an HTTP GET and return the response body as text."""
    req = Request(url)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def notify(heading, message, duration=5000):
    """Show a Kodi notification."""
    import xbmc

    xbmc.executebuiltin("Notification({}, {}, {})".format(heading, message, duration))
