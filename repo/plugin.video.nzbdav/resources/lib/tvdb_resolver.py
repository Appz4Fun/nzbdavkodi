# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Resolve a show's TheTVDB id from a TMDB id / IMDb id via the TMDB API.

TMDBHelper normally hands us the series TVDB id for free through the
``{tvdb}`` player token, but the in-addon search path (and the rare case
where TMDBHelper omits it) has only a TMDB/IMDb id. This module is the
fallback: it asks TMDB for the show's external ids so episode searches can
key on ``tvdbid`` (issue #318).

Everything here is *fail-soft*: any missing key, network hiccup, or odd
payload returns ``""`` so the caller simply falls back to imdbid/title.
Successful lookups are cached on disk because TVDB ids never change.
"""

import json

import xbmc
import xbmcaddon
import xbmcvfs

_TMDB_BASE = "https://api.themoviedb.org/3"


def _default_settings_getter():
    addon = xbmcaddon.Addon("plugin.video.nzbdav")

    def getter(key, default=""):
        return addon.getSetting(key) or default

    return getter


def _get_tmdb_api_key(settings_getter):
    """Return nzbdav's configured TMDB API key, or ``""`` when unset.

    Reads ONLY through the supplied ``settings_getter`` (which, on the
    RunScript/script-play path, is ``router._get_script_setting`` reading
    settings.xml off disk). It must never touch ``xbmcaddon.Addon`` — that
    binding can SIGSEGV CoreELEC in the script context (see
    ``webdav._get_settings``), and ``resolve_tvdb_id`` runs on that path.

    An earlier best-effort borrow of TMDBHelper's key was dropped: it
    reintroduced exactly that ``Addon`` risk, and current TMDBHelper bundles
    its own key and exposes no ``tmdb_apikey`` setting to borrow anyway. When
    no key is set the caller skips the network and relies on the imdbid/title
    fallback (the ``{tvdb}`` player token remains the primary path).
    """
    try:
        return (settings_getter("tmdb_api_key", "") or "").strip()
    except Exception:  # pylint: disable=broad-except
        return ""


def _cache_path():
    import os

    # Resolve the profile dir via special:// rather than
    # xbmcaddon.Addon(...).getAddonInfo("profile"): resolve_tvdb_id can run in
    # the RunScript/script-play path (script_player -> _search_all_providers),
    # where the codebase deliberately avoids xbmcaddon.Addon — repeated/odd
    # binding use there can SIGSEGV CoreELEC (see webdav._get_settings and
    # router._get_script_setting). xbmcvfs.translatePath needs no Addon handle.
    profile = xbmcvfs.translatePath("special://profile/addon_data/plugin.video.nzbdav")
    cache_dir = os.path.join(profile, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, "tvdb_ids.json")


def _load_file_cache():
    # A disk cache must never break a search — swallow any path/IO/parse error.
    try:
        with open(_cache_path(), "r") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:  # pylint: disable=broad-except
        return {}


def _save_file_cache(store):
    try:
        with open(_cache_path(), "w") as handle:
            json.dump(store, handle)
    except Exception:  # pylint: disable=broad-except
        # A cache write failure must never break a search.
        pass


def _tvdb_from_external_ids(payload):
    """Extract a non-empty numeric tvdb id (as str) from a TMDB payload."""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("tvdb_id")
    if not value:
        return ""
    return "".join(ch for ch in str(value) if ch.isdigit())


def _query(http_get, path, key, extra=None):
    from urllib.parse import urlencode

    params = {"api_key": key}
    if extra:
        params.update(extra)
    url = "{}{}?{}".format(_TMDB_BASE, path, urlencode(params))
    return json.loads(http_get(url, timeout=15))


def _series_tmdb_id_from_imdb(http_get, imdb, key):
    data = _query(
        http_get, "/find/{}".format(imdb), key, {"external_source": "imdb_id"}
    )
    results = data.get("tv_results") if isinstance(data, dict) else None
    if not results:
        return ""
    first = results[0]
    series_id = first.get("id") if isinstance(first, dict) else None
    return str(series_id) if series_id else ""


def resolve_tvdb_id(
    tmdb_id="",
    imdb="",
    settings_getter=None,
    http_get=None,
    cache=None,
):
    """Resolve a show's TheTVDB id from ``tmdb_id`` (preferred) or ``imdb``.

    Returns the numeric TVDB id as a string, or ``""`` if it can't be
    resolved (no id, no API key, network error, not found). Never raises.

    ``settings_getter`` / ``http_get`` / ``cache`` are injectable for tests;
    in production they default to the addon settings, ``http_util.http_get``,
    and an on-disk JSON cache respectively.
    """
    tmdb_id = (str(tmdb_id) if tmdb_id else "").strip()
    imdb = (str(imdb) if imdb else "").strip()
    cache_key = (
        "tmdb:{}".format(tmdb_id)
        if tmdb_id
        else ("imdb:{}".format(imdb) if imdb else "")
    )
    if not cache_key:
        return ""

    use_file = cache is None
    store = _load_file_cache() if use_file else cache
    cached = store.get(cache_key)
    if cached:
        return cached

    if settings_getter is None:
        try:
            settings_getter = _default_settings_getter()
        except Exception:  # pylint: disable=broad-except
            return ""
    key = _get_tmdb_api_key(settings_getter)
    if not key:
        return ""

    if http_get is None:
        from resources.lib.http_util import http_get as _http_get

        http_get = _http_get

    try:
        if tmdb_id:
            series_id = tmdb_id
        else:
            series_id = _series_tmdb_id_from_imdb(http_get, imdb, key)
            if not series_id:
                return ""
        payload = _query(http_get, "/tv/{}/external_ids".format(series_id), key)
        tvdb = _tvdb_from_external_ids(payload)
    except Exception as error:  # pylint: disable=broad-except
        # HTTPError/URLError str() can echo the failing URL, which embeds the
        # TMDB api_key — redact before logging (same defense as hydra/prowlarr).
        from resources.lib.http_util import redact_text

        xbmc.log(
            "NZB-DAV: TVDB resolve failed for tmdb={} imdb={}: {}".format(
                tmdb_id or "-", imdb or "-", redact_text(str(error))
            ),
            xbmc.LOGDEBUG,
        )
        return ""

    if tvdb:
        store[cache_key] = tvdb
        if use_file:
            _save_file_cache(store)
    return tvdb
