# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Localization helpers for Kodi-visible strings."""

import xbmcaddon

_FALLBACK_NAME = "NZB-DAV"
_FALLBACK_STRINGS = {
    30011: "Install Player File",
    30082: "Search cache cleared",
    30083: "Searching NZBHydra for {}...",
    30084: "Querying NZBHydra2...",
    30085: "Caching {} results...",
    30086: "Loaded {} results from cache",
    30087: "No results found for {}",
    30088: "Filtering results...",
    30089: "No results after filtering for {}",
    30091: "Clear Cache",
    30092: "Settings",
    30093: "Install NZB-DAV Player To",
    30094: "Player installed to: {}",
    30095: "Failed to install to: {}",
    30096: "No NZB URL provided",
    30097: "Submitting NZB to nzbdav...",
    30098: "Failed to submit NZB to nzbdav",
    30099: "Download timed out after {} seconds",
    30100: "Download failed",
    30101: "Download timed out",
    30102: "Queued...",
    30103: "Fetching NZB...",
    30104: "Waiting for propagation...",
    30105: "Downloading... {}%",
    30106: "Paused",
    30107: "WebDAV authentication failed. Check credentials.",
    30108: "WebDAV server error. Retrying...",
    30109: "WebDAV connection error. Check server.",
    30110: "{} sources found",
    30111: "Sorted by relevance",
    30112: "Showing {} of {} sources after filters",
    # 30115/30116/30121 are surfaced from the service-side retry/error
    # handler when strings.po hasn't been loaded yet (early in service
    # startup). Without these, the user saw a blank notification body.
    # 30054/30055 are settings-context-menu labels used by router.py.
    # All five are duplicated here from
    # `resources/language/resource.language.en_gb/strings.po` so any
    # future translator change there should be mirrored here too. TODO.md §H.2-M40.
    30054: "Configure Preferred Groups...",
    30055: "Configure Excluded Groups...",
    30115: "Stream failed. Try an MKV version or check nzbdav server.",
    30116: "Stream failed after {} retries. Try a different source.",
    30120: "Completed but no video file found on WebDAV",
    30121: ("Playback failed to start. The stream may be unavailable or corrupted."),
    30122: "NZB submit timeout (seconds)",
    30124: (
        "nzbdav rejected the submission (HTTP {0}). "
        "Server message: {1}. Check nzbdav's logs for details."
    ),
}


def addon():
    """Return the active addon instance, or None if Kodi isn't fully up yet.

    Early in service startup, `xbmcaddon.Addon()` can raise RuntimeError
    ("unknown addon id") because the plugin subsystem hasn't finished
    registering us. Return None so callers fall through to their fallback
    instead of crashing the service entry point.
    """
    try:
        return xbmcaddon.Addon()
    except RuntimeError:
        return None


def addon_name():
    """Return the localized addon name from addon metadata."""
    a = addon()
    if a is None:
        return _FALLBACK_NAME
    name = a.getAddonInfo("name")
    return name if isinstance(name, str) and name else _FALLBACK_NAME


def string(msg_id):
    """Return a localized string by numeric id."""
    a = addon()
    if a is not None:
        value = a.getLocalizedString(msg_id)
        if isinstance(value, str) and value:
            return value
    return _FALLBACK_STRINGS.get(msg_id, "")


def fmt(msg_id, *args, **kwargs):
    """Format a localized string with arguments.

    Wrapped in try/except (TODO.md §H.3): if the localized template's
    placeholder count is wrong (e.g. translator dropped a `{1}`) or the
    caller supplies the wrong number of args, we'd otherwise raise
    IndexError / KeyError out of every dialog and notification site.
    Fall back to the raw template plus a stringified arg list so the
    user still gets something useful, and log the underlying mismatch
    so the bad string can be fixed.
    """
    template = string(msg_id)
    try:
        return template.format(*args, **kwargs)
    except (IndexError, KeyError, ValueError) as exc:
        try:
            import xbmc

            xbmc.log(
                "NZB-DAV: i18n.fmt({}) format failure ({}); "
                "args={!r} kwargs={!r}".format(msg_id, exc, args, kwargs),
                xbmc.LOGWARNING,
            )
        except Exception:  # pylint: disable=broad-except
            pass
        suffix = (
            " ({})".format(", ".join(repr(a) for a in args)) if args or kwargs else ""
        )
        return template + suffix
