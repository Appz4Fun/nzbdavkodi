# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""HTTP client for the Rust orchestrator's /v1/search route.

Phase 1 of docs/rust-migration-plan.md. When the
``use_orchestrator`` setting is on, ``router.py``'s
``_search_all_providers`` short-circuits through
:func:`search_via_orchestrator`. The orchestrator already runs the
provider fan-out (Hydra/Prowlarr/direct Newznab) and the filter
rules; this client converts its JSON response back into the
list-of-dicts shape Python downstream code expects (``link``,
``title``, ``size``, ``pubDate``, ``indexer``, ``categories``,
``newznabAttrs``).

Failure modes return ``(None, reason)`` so the caller can fall
back to the legacy Python pipeline cleanly. We never raise.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

import xbmc
import xbmcaddon
import xbmcvfs

_ADDON_ID = "plugin.video.nzbdav"
_ORCH_TIMEOUT_S = 30.0


def _addon() -> xbmcaddon.Addon:
    return xbmcaddon.Addon(_ADDON_ID)


def _is_enabled(settings_getter) -> bool:
    if settings_getter is None:
        return _addon().getSetting("use_orchestrator").lower() == "true"
    return settings_getter("use_orchestrator", "false").lower() == "true"


def _orch_addr() -> Optional[str]:
    """Read the orchestrator's bound host:port from the addr file
    that service.py / orchestrator_bootstrap wrote on startup. None
    means the orchestrator never started (or is disabled).
    """
    try:
        addon = _addon()
        profile = xbmcvfs.translatePath(addon.getAddonInfo("profile"))
    except Exception:  # pylint: disable=broad-except
        return None
    candidate = os.path.join(profile, "orchestrator.addr")
    try:
        with open(candidate, "r", encoding="utf-8") as fh:
            content = fh.read().strip()
        return content or None
    except OSError:
        return None


def _provider_config(settings_getter) -> dict:
    """Build the JSON `providers` block from addon settings.

    Mirrors the per-provider enabled flags `_search_all_providers`
    reads in `router.py`. Returns an empty dict (no providers) when
    none are enabled — the orchestrator will then 400 and we fall
    back.
    """

    def _g(key, default=""):
        if settings_getter is not None:
            return settings_getter(key, default)
        return _addon().getSetting(key) or default

    out: dict[str, Any] = {}

    if _g("nzbhydra_enabled", "false").lower() == "true":
        try:
            max_results = int(_g("max_results", "25"))
        except (TypeError, ValueError):
            max_results = 25
        out["hydra"] = {
            "base_url": (_g("hydra_url") or "").rstrip("/"),
            "api_key": _g("hydra_api_key"),
            "max_results": max(1, min(10_000, max_results)),
            "search_timeout_secs": 15,
        }

    if _g("prowlarr_enabled", "false").lower() == "true":
        indexer_ids_raw = _g("prowlarr_indexer_ids", "")
        ids = [s.strip() for s in indexer_ids_raw.split(",") if s.strip()]
        out["prowlarr"] = {
            "host": (_g("prowlarr_host") or "").rstrip("/"),
            "api_key": _g("prowlarr_api_key"),
            "indexer_ids": ids,
            "search_timeout_secs": 15,
        }

    if _g("direct_indexers_enabled", "false").lower() == "true":
        # Reads the unified list `direct_indexers.get_configured_indexers`
        # returns — managed indexers from addon_data/indexers.json plus
        # any legacy slots still set in settings.xml. The Python helper
        # already handles the dedup + caps lookup, so all we do here is
        # reshape each entry into the DirectIndexer JSON the
        # orchestrator expects.
        try:
            from resources.lib.direct_indexers import get_configured_indexers
        except Exception:  # pylint: disable=broad-except
            get_configured_indexers = None  # type: ignore[assignment]

        direct_entries: list = []
        if get_configured_indexers is not None:
            try:
                for entry in get_configured_indexers() or ():
                    direct_entries.append(
                        {
                            "id": entry.get("id") or "",
                            "label": entry.get("label") or entry.get("id") or "",
                            "api_url": entry.get("api_url") or "",
                            "api_key": entry.get("api_key") or "",
                            "caps": entry.get("caps") or None,
                        }
                    )
            except Exception:  # pylint: disable=broad-except
                direct_entries = []

        if direct_entries:
            out["direct"] = direct_entries

    return out


def _result_dict_from_candidate(candidate: dict) -> dict:
    """Map an orchestrator Candidate JSON to the Python result dict
    shape that `_search_all_providers` historically produced.

    Python downstream code reads (at minimum): ``link``, ``title``,
    ``size``, ``pubDate``, ``indexer``, ``newznabAttrs``.
    """
    extra = candidate.get("extra") or {}
    # `extra` is a flat map of newznab:attr name → value. Python
    # callers expect a list of {name, value} dicts under
    # ``newznabAttrs``.
    newznab_attrs = [
        {"name": k, "value": str(v)} for k, v in extra.items() if v is not None
    ]
    return {
        "link": candidate.get("nzb_url") or "",
        "title": candidate.get("title") or "",
        "size": int(candidate.get("size") or 0),
        "pubDate": candidate.get("pubdate") or "",
        "indexer": candidate.get("indexer") or "",
        "guid": candidate.get("guid") or "",
        "categories": list(candidate.get("categories") or ()),
        "newznabAttrs": newznab_attrs,
    }


def search_via_orchestrator(
    search_type: str,
    title: str,
    year: str = "",
    imdb: str = "",
    season: str = "",
    episode: str = "",
    settings_getter=None,
):
    """Run the search through the Rust orchestrator.

    Returns ``(results, None)`` on success or ``(None, reason)`` on
    any failure — the caller is expected to fall back to the legacy
    Python pipeline whenever ``results`` is None.
    """
    if not _is_enabled(settings_getter):
        return None, "orchestrator_disabled"

    addr = _orch_addr()
    if addr is None:
        return None, "orchestrator_addr_unavailable"

    providers = _provider_config(settings_getter)
    if not providers:
        return None, "no_providers_enabled_for_orchestrator"

    # Map Python "movie"/"tv" search_type → orchestrator's lowercase
    # SearchKind. Anything unrecognised falls back to movie which
    # matches the addon's most-common path.
    kind = "tv" if search_type and "tv" in search_type.lower() else "movie"

    body = {
        "search": {
            "kind": kind,
            "title": title,
            "year": int(year) if year and str(year).isdigit() else None,
            "imdb_id": imdb or None,
            "season": int(season) if season and str(season).isdigit() else None,
            "episode": int(episode) if episode and str(episode).isdigit() else None,
        },
        # Empty settings → orchestrator filter passes everything
        # through. The downstream Python `filter_results` will run
        # over the raw candidates as it does today, preserving the
        # ranking/UX behaviour.
        "settings": {},
        "providers": providers,
    }

    request = urllib.request.Request(
        "http://{}/v1/search".format(addr),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback only
            request, timeout=_ORCH_TIMEOUT_S
        ) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.URLError as e:
        xbmc.log(
            "NZB-DAV: orchestrator.error endpoint='/v1/search' "
            "reason=transport message={}".format(e),
            xbmc.LOGWARNING,
        )
        return None, "orchestrator_transport_error: {}".format(e)
    except OSError as e:
        xbmc.log(
            "NZB-DAV: orchestrator.error endpoint='/v1/search' "
            "reason=io message={}".format(e),
            xbmc.LOGWARNING,
        )
        return None, "orchestrator_io_error: {}".format(e)

    if status != 200:
        xbmc.log(
            "NZB-DAV: orchestrator.error endpoint='/v1/search' "
            "reason=non_200 status={}".format(status),
            xbmc.LOGWARNING,
        )
        return None, "orchestrator_non_200: {}".format(status)

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return None, "orchestrator_bad_json: {}".format(e)

    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        return None, "orchestrator_bad_shape"

    xbmc.log(
        "NZB-DAV: orchestrator.call endpoint='/v1/search' outcome=ok "
        "search_id={} candidates={}".format(payload.get("search_id"), len(candidates)),
        xbmc.LOGINFO,
    )

    return [_result_dict_from_candidate(c) for c in candidates], None
