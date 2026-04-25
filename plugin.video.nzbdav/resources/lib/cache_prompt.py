# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""First-play ``advancedsettings.xml`` setup dialog.

When force-remux triggers on a large file, surface a one-off dialog
offering the user the pass-through upgrade path: add
``<cache><memorysize>0</memorysize></cache>`` to their
``advancedsettings.xml``. The addon never writes to that file —
merging arbitrary XML would risk clobbering existing ``<video>``,
``<network>``, or ``<videodatabase>`` entries, so the dialog shows
the snippet and lets the user paste it themselves.

Shown at most once per Kodi session (window property) and once
globally if the user picks "Never ask" (persistent setting). See
TODO.md §D.5.1 Phase 1 step 4.
"""

import xbmcaddon
import xbmcgui

from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _f
from resources.lib.i18n import string as _s
from resources.lib.kodi_advancedsettings import has_cache_memorysize_zero

_HOME_WINDOW_ID = 10000
_PROP_SHOWN_THIS_SESSION = "nzbdav.cache_dialog.shown_this_session"

_DLG_NOT_NOW = 0
_DLG_SHOW_INSTRUCTIONS = 1
_DLG_NEVER_ASK = 2

_SUPPRESSED_EXCEPTIONS = (OSError, RuntimeError)


def should_show_cache_prompt(
    stream_remux, cache_is_set, session_shown, persistent_dismissed
):
    """Pure decision: return True iff the first-play dialog should fire.

    ``stream_remux`` is the ``stream_info['remux']`` flag from
    ``prepare_stream_via_service`` — True means the force-remux tier
    was selected, which is the "file is large enough for passthrough
    to matter" signal.
    """
    if not stream_remux:
        return False
    if cache_is_set:
        return False
    if session_shown:
        return False
    if persistent_dismissed:
        return False
    return True


def maybe_show_cache_prompt(stream_info):
    """Evaluate show/suppress conditions and surface the dialog if
    appropriate. Handles the button result (Show instructions / Not
    now / Never ask) and records the session / persistent dismissal.
    """
    window = xbmcgui.Window(_HOME_WINDOW_ID)
    addon = xbmcaddon.Addon()

    stream_remux = bool(stream_info.get("remux"))
    cache_is_set = has_cache_memorysize_zero()
    session_shown = window.getProperty(_PROP_SHOWN_THIS_SESSION).lower() == "true"
    persistent_dismissed = addon.getSetting("cache_dialog_dismissed").lower() == "true"

    if not should_show_cache_prompt(
        stream_remux, cache_is_set, session_shown, persistent_dismissed
    ):
        return

    # Mark shown for this session BEFORE surfacing the dialog so a
    # cancelled / dismissed dialog still counts as "shown once" — we
    # don't re-prompt on every subsequent large-file play.
    try:
        window.setProperty(_PROP_SHOWN_THIS_SESSION, "true")
    except _SUPPRESSED_EXCEPTIONS:
        pass

    total_bytes = int(stream_info.get("total_bytes") or 0)
    size_gb = total_bytes / (1024.0**3) if total_bytes else 0.0
    message = _f(30153, size_gb) if size_gb else _s(30154)

    # Dialog().yesnocustom can raise RuntimeError on Kodi lifecycle issues
    # (e.g. shutdown, no display). The session-shown flag was already set
    # above, so a raised exception would silence all future prompts in
    # this session — which is acceptable, but we still want a log line so
    # the failure isn't completely invisible.
    try:
        result = xbmcgui.Dialog().yesnocustom(
            _addon_name(),
            message,
            _s(30155),  # custom label: Never ask
            _s(30156),  # no label: Not now
            _s(30157),  # yes label: Show instructions
        )
    except RuntimeError as exc:
        try:
            import xbmc

            xbmc.log(
                "NZB-DAV: cache_prompt dialog suppressed: {!r}".format(exc),
                xbmc.LOGWARNING,
            )
        except Exception:  # pylint: disable=broad-except
            pass
        return

    if result == _DLG_SHOW_INSTRUCTIONS:
        _show_instructions_dialog()
    elif result == _DLG_NEVER_ASK:
        try:
            addon.setSetting("cache_dialog_dismissed", "true")
        except _SUPPRESSED_EXCEPTIONS as exc:
            # Failed to persist "Never ask" — the dialog will return next
            # session. Surface that to the log so the user has a clue why
            # they're seeing it again, without crashing the resolve flow.
            try:
                import xbmc

                xbmc.log(
                    "NZB-DAV: cache_prompt failed to persist 'Never ask' "
                    "(setting=cache_dialog_dismissed): {!r}".format(exc),
                    xbmc.LOGWARNING,
                )
            except Exception:  # pylint: disable=broad-except
                pass
    # _DLG_NOT_NOW (0) or cancelled (-1): session flag already set


def _show_instructions_dialog():
    """Display the XML snippet the user needs to paste."""
    xbmcgui.Dialog().textviewer(_s(30158), _s(30159))
