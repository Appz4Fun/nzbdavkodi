# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Background-service proxy property readers.

Stage-final decomposition of ``stream_proxy``: the ``get_service_proxy_*``
helpers that read the proxy port / prepare token off the Kodi Home window were
moved here verbatim. ``stream_proxy`` re-exports them (and rebinds its
``_ORIGINAL_GET_SERVICE_PROXY_*`` aliases to the same objects) so every caller
and test patch target — ``resources.lib.stream_proxy.get_service_proxy_port``
and the ``_ORIGINAL_*`` identity check in ``resolver_prepare`` — keeps working.
Module-level names (``_KODI_SETTING_ERRORS``, ``_PROP_PROXY_TOKEN``) are reached
via ``_sp.<name>`` at call time so they resolve against the shared namespace.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402


def get_service_proxy_port():
    """Get the proxy port from the background service, or 0 if not running."""
    try:
        import xbmcgui

        home = xbmcgui.Window(10000)
        port_str = home.getProperty("nzbdav.proxy_port")
        return int(port_str) if port_str else 0
    except _sp._KODI_SETTING_ERRORS:
        return 0


def get_service_proxy_token():
    """Get the loopback /prepare token from the background service."""
    try:
        import xbmcgui

        home = xbmcgui.Window(10000)
        return home.getProperty(_sp._PROP_PROXY_TOKEN) or ""
    except _sp._KODI_SETTING_ERRORS:
        return ""


def get_service_proxy_config():
    """Get proxy port and token with a single Kodi Home window read."""
    try:
        import xbmcgui

        home = xbmcgui.Window(10000)
        port_str = home.getProperty("nzbdav.proxy_port")
        service_port = int(port_str) if port_str else 0
        prepare_token = home.getProperty(_sp._PROP_PROXY_TOKEN) if service_port else ""
        return service_port, prepare_token or ""
    except _sp._KODI_SETTING_ERRORS:
        return 0, ""
