# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Settings/path/loading-dialog helpers split out of ``router``.

None of these are test-patched or imported by name, but several reach
router-resident or router-patched names (``_addon_name``, ``_fmt``,
``_script_play_stage``, the script-path constants) which are resolved at call
time through ``import resources.lib.router as _router`` so any ``@patch`` and
the central stage logger keep working. ``router`` re-exports them for its own
callers.
"""

import os

import xbmc
import xbmcaddon
import xbmcgui


def _addon_instance():
    """Return the addon object, accepting older tests' no-arg Addon mocks."""
    import xbmcaddon as addon_module

    try:
        return addon_module.Addon("plugin.video.nzbdav")
    except TypeError:
        return addon_module.Addon()


def _open_loading_dialog(title):
    """Open a NON-modal background progress dialog for the search->picker wait.

    The picker takes several seconds to appear (indexer search + filtering),
    which otherwise looks like a frozen/crashed screen. A background
    ``DialogProgressBG`` (top-right) gives a visible "working" indicator.

    We deliberately do NOT use the modal ``xbmcgui.DialogProgress`` here: on
    CoreELEC/Arctic Fuse it can native-crash Kodi mid-search (the same reason
    ``_handle_play`` avoids it — see
    ``test_handle_play_does_not_open_modal_progress_before_picker``). Any
    failure creating the dialog is swallowed so a missing indicator can never
    break playback. Returns the dialog handle, or ``None``.
    """
    import resources.lib.router as _router

    try:
        dialog = xbmcgui.DialogProgressBG()
        # Provider-agnostic: this fires for every search regardless of which
        # provider(s) are actually configured (NZBHydra2, Prowlarr, direct
        # indexers, any combination). #30083 hardcoded "NZBHydra" here even
        # when NZBHydra2 was disabled and only direct indexers ran.
        dialog.create(_router._addon_name(), _router._fmt(30367, title or ""))
        return dialog
    except Exception:  # pylint: disable=broad-except
        return None


def _update_loading_dialog(dialog, percent, message):
    """Update the background loading dialog; no-op when it failed to open."""
    import resources.lib.router as _router

    if dialog is None:
        return
    try:
        dialog.update(percent, _router._addon_name(), message)
    except Exception:  # pylint: disable=broad-except
        pass


def _close_loading_dialog(dialog):
    """Close the background loading dialog; safe to call more than once."""
    if dialog is None:
        return
    try:
        dialog.close()
    except Exception:  # pylint: disable=broad-except
        pass


def _translate_path(path):
    """Translate Kodi special:// paths, returning empty string on failure."""
    try:
        import xbmcvfs

        translated = xbmcvfs.translatePath(path)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""
    return translated if isinstance(translated, str) else ""


def _script_stage_paths():
    import resources.lib.router as _router

    paths = []
    translated_temp = _translate_path("special://temp/")
    if translated_temp:
        paths.append(os.path.join(translated_temp, "nzbdav-script-play-stage.log"))
    paths.append(_router._SCRIPT_PLAY_STAGE_PATH)
    return paths


def _script_settings_paths():
    import resources.lib.router as _router

    paths = []
    translated = _translate_path(
        "special://profile/addon_data/plugin.video.nzbdav/settings.xml"
    )
    if translated:
        paths.append(translated)
    paths.append(_router._SCRIPT_SETTINGS_PATH)
    return paths


def _show_error_dialog(message):
    """
    Display a modal error dialog in Kodi with the add-on name as the dialog title.

    Parameters:
        message (str): The error message to display.
    """
    import resources.lib.router as _router

    xbmcgui.Dialog().ok(_router._addon_name(), message)


def _get_addon_setting(addon, key, default="", runtime_default=None):
    """Read a Kodi setting, returning a default if Kodi's settings layer fails."""
    try:
        value = addon.getSetting(key)
    except RuntimeError as exc:
        xbmc.log(
            "NZB-DAV: setting '{}' unavailable; using default: {}".format(key, exc),
            xbmc.LOGWARNING,
        )
        return default if runtime_default is None else runtime_default
    return value if isinstance(value, str) else default


def _snapshot_settings_getter(settings_getter, defaults):
    snapshot = {}
    for key, default in defaults.items():
        try:
            snapshot[key] = settings_getter(key, default)
        except Exception as error:  # pylint: disable=broad-exception-caught
            xbmc.log(
                "NZB-DAV: setting '{}' unavailable during provider snapshot; "
                "using default: {}".format(key, error),
                xbmc.LOGWARNING,
            )
            snapshot[key] = default

    def get_snapshot_setting(key, default=""):
        return snapshot.get(key, default)

    return get_snapshot_setting


def _settings_getter_or_addon_default(settings_getter):
    """Return ``settings_getter`` or an addon-backed default that forces hydra on."""
    import resources.lib.router as _router

    if settings_getter is not None:
        _router._script_play_stage("providers using script settings")
        return settings_getter

    addon = xbmcaddon.Addon("plugin.video.nzbdav")
    _router._script_play_stage("providers addon created")

    def _addon_settings_getter(key, default=""):
        runtime_default = "true" if key == "nzbhydra_enabled" else default
        return _get_addon_setting(addon, key, default, runtime_default=runtime_default)

    return _addon_settings_getter


def _resolve_episode_tvdb_id(search_type, tvdb, tmdb_id, imdb, settings_getter):
    """Resolve a shared TheTVDB id for episode searches (issue #318).

    Many indexers key TV on tvdbid, so imdbid-based tvsearch misses. The
    TMDBHelper player token usually supplies tvdb directly; when it doesn't,
    resolve it once here (cached, fail-soft) from the tmdb/imdb id so every
    provider shares the same id rather than each repeating the lookup. Returns
    the (possibly newly resolved) tvdb id.
    """
    import resources.lib.router as _router

    if not (search_type == "episode" and not tvdb and (tmdb_id or imdb)):
        return tvdb
    from resources.lib.tvdb_resolver import resolve_tvdb_id

    resolved_tvdb = resolve_tvdb_id(
        tmdb_id=tmdb_id, imdb=imdb, settings_getter=settings_getter
    )
    if resolved_tvdb:
        tvdb = resolved_tvdb
        _router._script_play_stage("resolved tvdbid={}".format(tvdb))
    return tvdb
