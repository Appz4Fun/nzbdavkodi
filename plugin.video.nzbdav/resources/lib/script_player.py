# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Script-mode TMDBHelper player entrypoints."""

from urllib.parse import unquote_plus

NZBDAV_ADDON_ID = "plugin.video.nzbdav"


def _clean_script_value(value):
    value = unquote_plus(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def parse_script_args(args):
    """Parse RunScript key=value arguments into router-style params.

    Kodi splits RunScript arguments on commas. TMDBHelper can pass titles with
    commas, so fragments without "=" are joined back onto the previous key.
    """
    params = {}
    current_key = None
    for arg in args:
        if "=" in arg:
            key, value = arg.split("=", 1)
            current_key = key
            params[key] = _clean_script_value(value)
        elif current_key:
            if params[current_key]:
                value = unquote_plus(arg)
                params[current_key] = params[current_key] + "," + value
            else:
                value = _clean_script_value(arg)
                params[current_key] = value
    return params


def run_tmdb_play(args):
    """Run the TMDBHelper player flow without handing Kodi a plugin:// URL."""
    import xbmcaddon

    from resources.lib.router import _handle_script_play

    original_addon = xbmcaddon.Addon

    def addon_with_default_id(*addon_args, **addon_kwargs):
        if not addon_args and not addon_kwargs:
            return original_addon(NZBDAV_ADDON_ID)
        return original_addon(*addon_args, **addon_kwargs)

    xbmcaddon.Addon = addon_with_default_id
    try:
        _handle_script_play(parse_script_args(args))
    finally:
        xbmcaddon.Addon = original_addon
