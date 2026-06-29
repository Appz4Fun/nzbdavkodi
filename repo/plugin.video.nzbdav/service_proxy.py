# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Stream-proxy lifecycle helpers for the NZB-DAV background service.

These functions manage the long-lived stream proxy that the service hosts
(start, advertise port/token, restart a dead daemon thread, shut down). They
were split out of ``service.py`` to keep that entry module under the file-size
budget; ``service.py`` re-exports the lifecycle entry points so existing
imports keep resolving.

The ``home`` window and ``proxy_cls`` (the ``StreamProxy`` class) are passed in
by ``service.main()`` rather than imported here. ``main()`` resolves them from
the ``service`` module's own namespace at call time, so test patches such as
``@patch("service.StreamProxy")`` and ``@patch("service._HOME_WINDOW")`` reach
this code unchanged, and there is no import cycle back into ``service``. The
shared ``xbmc`` module is imported directly: ``@patch("service.xbmc.log")``
patches the attribute on that shared module, so logging stays observable.
"""

import xbmc

# IPC window-property keys the proxy advertises itself under. Kept in sync with
# the same names in ``service.py`` (which still owns the canonical definitions).
_PROP_PROXY_PORT = "nzbdav.proxy_port"
_PROP_PROXY_TOKEN = "nzbdav.proxy_token"  # nosec B105 — settings key, not a secret


def _publish_proxy_props(home, proxy):
    """Advertise the live proxy's port/token to plugin-side callers."""
    home.setProperty(_PROP_PROXY_PORT, str(proxy.port))
    home.setProperty(_PROP_PROXY_TOKEN, proxy.prepare_token)


def _clear_proxy_props(home):
    """Drop the proxy port/token so plugin callers fall back quickly."""
    home.clearProperty(_PROP_PROXY_PORT)
    home.clearProperty(_PROP_PROXY_TOKEN)


def _restart_dead_proxy(home, proxy_cls, proxy, player):
    """Rebuild the proxy when its daemon thread has died; return the proxy.

    The HTTP server runs in a daemon thread. If serve_forever ever exits
    (unhandled exception, socket error, rare memory-pressure path), every
    subsequent /prepare call hangs on "connection refused" with no recovery.
    Detect the dead thread and rebuild so streams keep working. Returns the
    same proxy when it is still alive, otherwise the freshly built one.
    """
    if proxy.is_alive():
        return proxy
    xbmc.log(
        "NZB-DAV: Stream proxy thread is dead; restarting "
        "(reason=proxy_thread_died)",
        xbmc.LOGERROR,
    )
    try:
        proxy.stop()
    except Exception as e:  # pylint: disable=broad-except
        # Logged at LOGWARNING (not LOGERROR) because we're about
        # to spawn a fresh proxy anyway — the stop failure is
        # diagnostic-only, not user-actionable. Closes §H.3.
        xbmc.log(
            "NZB-DAV: proxy.stop() raised during restart "
            "(continuing): {!r}".format(e),
            xbmc.LOGWARNING,
        )
    proxy = proxy_cls()
    try:
        proxy.start()
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Stream proxy restart failed: {} "
            "(reason=proxy_restart_failed)".format(e),
            xbmc.LOGERROR,
        )
        _clear_proxy_props(home)
    else:
        _publish_proxy_props(home, proxy)
        # The player holds a reference to the old proxy for cleanup calls
        # from onPlayBackStopped; point it at the new one so the next
        # stop() fires on the live proxy.
        player._proxy = proxy  # pylint: disable=protected-access
        xbmc.log(
            "NZB-DAV: Stream proxy restarted on port {}".format(proxy.port),
            xbmc.LOGINFO,
        )
    return proxy


def _start_proxy(home, proxy_cls, monitor):
    """Start the stream proxy; return it, or None if startup failed.

    The proxy lives in this long-lived service process because plugin scripts
    are short-lived — their daemon threads get killed when Kodi's
    CPythonInvoker destroys the interpreter after the script exits.

    On a start failure (socket bind / port in use, permission error, etc) the
    service must not die silently — every plugin-side /prepare would then hang
    on "connection refused" with no log hint. Surface it, clear the port
    property so callers fall back fast, then idle until Kodi shuts down (so we
    aren't restarted every few seconds spamming the same failure) and return
    None to signal the caller to exit.
    """
    proxy = proxy_cls()
    try:
        proxy.start()
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: Service failed to start stream proxy: {}".format(e),
            xbmc.LOGERROR,
        )
        _clear_proxy_props(home)
        while not monitor.abortRequested():
            if monitor.waitForAbort(5):
                break
        return None
    _publish_proxy_props(home, proxy)
    return proxy


def _shutdown_proxy(home, proxy):
    """Stop the proxy and clear its port/token, guarding the stop().

    The stop() is guarded the same way the restart path is: without it an
    exception (socket already closed, thread join timeout, etc.) would skip
    clearing ``_PROP_PROXY_PORT`` and leave a stale port visible to the next
    service launch, so clients would connect to a dead port. TODO.md §H.2-M36.
    """
    try:
        proxy.stop()
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: proxy.stop() raised during shutdown "
            "(continuing): {!r}".format(e),
            xbmc.LOGWARNING,
        )
    _clear_proxy_props(home)
