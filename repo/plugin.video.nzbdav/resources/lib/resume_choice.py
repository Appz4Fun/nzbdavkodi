# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Resume-or-restart choice for replaying a downloaded NZB.

Pure helpers that key resume on a stable release identity (title + size +
pubdate), render Kodi's native resume labels, honor the user's
``myvideos.selectaction`` preference, and prompt only when Kodi would.
No persistence happens here; callers own ``resume_store``.
"""

import json
from urllib.parse import unquote

import xbmc
import xbmcgui

# Kodi SelectAction enum (CSettings myvideos.selectaction). Only RESUME and
# PLAY map to a deterministic action; everything else (CHOOSE, PLAY_OR_RESUME,
# INFO, MORE, PLAYLIST, QUEUE, unknown, parse/RPC failure) prompts the user.
_SELECT_ACTION_RESUME = 2
_SELECT_ACTION_PLAY = 5

# Kodi built-in localized strings used for the prompt labels.
_STRING_RESUME_FROM = 12022  # "Resume from %s"
_STRING_START_FROM_BEGINNING = 12021  # "Start from beginning"

# Kodi 21 core string #12022 is "Resume from {0:s}" (str.format style); the
# fallback mirrors it. ``_fill_template`` tolerates printf "%s" too.
_FALLBACK_RESUME_FROM = "Resume from {0:s}"
_FALLBACK_START_FROM_BEGINNING = "Start from beginning"


def release_identity(params):
    """Return a deterministic resume key derived from release metadata.

    Anchored on the (unquoted) title, refined by ``_download_size`` and
    ``_download_pubdate`` when present. Returns ``""`` when there is no
    usable title so the caller can skip resume entirely.
    """
    title = unquote(params.get("title", "") or "")
    title = title.strip()
    if not title:
        return ""
    size = str(params.get("_download_size", "") or "")
    pubdate = str(params.get("_download_pubdate", "") or "")
    return "{}|{}|{}".format(title, size, pubdate)


def format_resume_label(seconds):
    """Return ``H:MM:SS`` (or ``MM:SS`` when under an hour) for ``seconds``."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        total = 0
    total = max(total, 0)
    hours = total // 3600
    minutes = (total % 3600) // 60
    secs = total % 60
    if hours:
        return "{}:{:02d}:{:02d}".format(hours, minutes, secs)
    return "{:02d}:{:02d}".format(minutes, secs)


def native_resume_action():
    """Return Kodi's resume preference as ``ask``/``resume``/``beginning``.

    Reads ``myvideos.selectaction`` over JSON-RPC. Maps RESUME to
    ``"resume"`` and PLAY to ``"beginning"``; anything else (including any
    RPC or parse failure) falls back to ``"ask"``. Never raises.
    """
    try:
        raw = xbmc.executeJSONRPC(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "Settings.GetSettingValue",
                    "params": {"setting": "myvideos.selectaction"},
                }
            )
        )
        response = json.loads(raw)
        value = response.get("result", {}).get("value")
        value = int(value)
    except (TypeError, ValueError, KeyError, AttributeError):
        return "ask"
    except Exception:  # noqa: BLE001 - never let a Kodi RPC failure escape
        return "ask"
    if value == _SELECT_ACTION_RESUME:
        return "resume"
    if value == _SELECT_ACTION_PLAY:
        return "beginning"
    return "ask"


def _localized_or(string_id, fallback):
    """Return Kodi's localized string for ``string_id`` or ``fallback``."""
    try:
        text = xbmc.getLocalizedString(string_id)
    except Exception:  # noqa: BLE001 - degrade to the bundled English label
        text = ""
    if isinstance(text, str) and text:
        return text
    return fallback


def _fill_template(template, value):
    """Insert ``value`` into a Kodi label template across placeholder styles.

    Kodi 21 core strings use ``str.format`` placeholders ("Resume from
    {0:s}"); older Kodi builds and the bundled fallbacks use printf
    ("Resume from %s"). Try ``str.format`` first, then printf, then append —
    so a placeholder-style mismatch can never raise. The printf path was the
    bug that crashed ``resolve_and_play`` with "not all arguments converted
    during string formatting" on Kodi 21's ``{0:s}`` string.
    """
    if "{" in template and "}" in template:
        try:
            return template.format(value)
        except (IndexError, KeyError, ValueError):
            pass
    if "%" in template:
        try:
            return template % (value,)
        except (TypeError, ValueError):
            pass
    base = template.strip()
    if base:
        return "{} {}".format(base, value)
    return value


def choose_resume_seconds(release_id, seconds, dialog=None):
    """Resolve the resume offset for a replay, prompting only when needed.

    Returns the chosen offset in seconds (``seconds`` to resume, ``0.0`` to
    start over) or ``None`` when the user cancels the prompt. ``seconds <= 0``
    short-circuits to ``0.0`` with no prompt. Choosing "beginning" returns
    ``0.0`` but never clears the stored point (the service overwrites/clears
    it on the next stop/end, matching Kodi's bookmark behavior).
    """
    if seconds <= 0:
        return 0.0
    action = native_resume_action()
    if action == "resume":
        return seconds
    if action == "beginning":
        return 0.0
    # action == "ask"
    if dialog is None:
        dialog = xbmcgui.Dialog()
    resume_label = _fill_template(
        _localized_or(_STRING_RESUME_FROM, _FALLBACK_RESUME_FROM),
        format_resume_label(seconds),
    )
    beginning_label = _localized_or(
        _STRING_START_FROM_BEGINNING, _FALLBACK_START_FROM_BEGINNING
    )
    index = dialog.contextmenu([resume_label, beginning_label])
    if index == 0:
        return seconds
    if index == 1:
        return 0.0
    return None
