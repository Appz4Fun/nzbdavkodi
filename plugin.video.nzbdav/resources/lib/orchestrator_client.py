# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""HTTP client for the Rust orchestrator's phase-gated routes.

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
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

import xbmc
import xbmcaddon
import xbmcvfs

_ADDON_ID = "plugin.video.nzbdav"
_ORCH_TIMEOUT_S = 30.0
_ORCH_RESOLVE_GRACE_S = 30.0
_ORCH_EVENT_TIMEOUT_S = 2.0
_ORCH_EVENT_RECONNECT_DELAY_S = 0.1


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


def _setting(settings_getter, key, default=""):
    if settings_getter is not None:
        return settings_getter(key, default)
    return _addon().getSetting(key) or default


def _nzbdav_config(settings_getter) -> dict:
    return {
        "base_url": (_setting(settings_getter, "nzbdav_url") or "").rstrip("/"),
        "api_key": _setting(settings_getter, "nzbdav_api_key"),
        "webdav_url": (_setting(settings_getter, "webdav_url") or "").rstrip("/"),
        "webdav_username": _setting(settings_getter, "webdav_username"),
        "webdav_password": _setting(settings_getter, "webdav_password"),
        "webdav_content_root": (
            _setting(settings_getter, "webdav_content_root", "content") or "content"
        ),
    }


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


def _candidate_peers_from_results(fallback_candidates):
    peers = []
    for candidate in fallback_candidates or []:
        if not isinstance(candidate, dict):
            continue
        nzb_url = candidate.get("link") or candidate.get("nzb_url") or ""
        title = candidate.get("title") or ""
        if not nzb_url or not title:
            continue
        try:
            size = int(candidate.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        extra = {}
        attrs = candidate.get("newznabAttrs") or []
        if isinstance(attrs, list):
            for attr in attrs:
                if not isinstance(attr, dict):
                    continue
                name = str(attr.get("name") or "").strip()
                if not name:
                    continue
                value = attr.get("value")
                if value is None:
                    continue
                extra[name] = str(value)
        peer = {
            "nzb_url": nzb_url,
            "title": title,
            "size": size,
            "indexer": candidate.get("indexer") or "",
            "extra": extra,
        }
        peers.append(peer)
    return peers


def _resolve_return(
    return_fallback_sources, stream_url, stream_headers, reason, sources=None
):
    if return_fallback_sources:
        return stream_url, stream_headers, list(sources or []), reason
    return stream_url, stream_headers, reason


def _fallback_sources_from_resolve_payload(payload):
    """Extract Rust-validated ready peers for Python proxy cutover."""
    primary_peer_id = payload.get("primary_peer_id")
    primary_stream_url = payload.get("stream_url")
    peers = payload.get("peers") or []
    if not isinstance(peers, list):
        return []

    sources = []
    for peer in peers:
        if not isinstance(peer, dict):
            continue
        if peer.get("peer_id") == primary_peer_id:
            continue
        if peer.get("state") != "ready":
            continue
        if peer.get("validation_state") != "byte_sample_validated_phase_3":
            continue
        stream_url = peer.get("stream_url") or ""
        if not stream_url or stream_url == primary_stream_url:
            continue
        stream_headers = peer.get("stream_headers") or {}
        if not isinstance(stream_headers, dict):
            stream_headers = {}
        try:
            content_length = int(peer.get("content_length") or 0)
        except (TypeError, ValueError):
            content_length = 0
        sources.append(
            {
                "title": peer.get("title") or "",
                "nzb_url": peer.get("nzb_url") or "",
                "job_name": peer.get("job_name") or "",
                "nzo_id": peer.get("nzo_id") or "",
                "stream_url": stream_url,
                "stream_headers": dict(stream_headers),
                "content_length": max(0, content_length),
                "validated": True,
            }
        )
    return sources


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


def resolve_via_orchestrator(
    nzb_url: str,
    title: str,
    poll_interval=None,
    download_timeout=None,
    fallback_candidates=None,
    peer_pool_cache_key=None,
    settings_getter=None,
    return_fallback_sources=False,
    resolve_id=None,
):
    """Resolve a single NZB through the Rust orchestrator.

    Returns ``(stream_url, stream_headers, None)`` on success, or
    ``(None, None, reason)`` on failure. This mirrors the search bridge:
    callers can fall back to the legacy Python resolver without catching
    transport or JSON exceptions.
    """
    if not _is_enabled(settings_getter):
        return _resolve_return(
            return_fallback_sources, None, None, "orchestrator_disabled"
        )

    addr = _orch_addr()
    if addr is None:
        return _resolve_return(
            return_fallback_sources, None, None, "orchestrator_addr_unavailable"
        )

    candidate_peers = _candidate_peers_from_results(fallback_candidates)
    body = {
        "nzb_url": nzb_url,
        "title": title,
        "fallback_count": len(candidate_peers),
        "candidate_peers": candidate_peers,
        "poll_interval_secs": int(poll_interval) if poll_interval else 1,
        "download_timeout_secs": int(download_timeout) if download_timeout else 3600,
        "nzbdav": _nzbdav_config(settings_getter),
    }
    if peer_pool_cache_key:
        body["peer_pool_cache_key"] = str(peer_pool_cache_key)
    if resolve_id:
        body["resolve_id"] = str(resolve_id)

    request = urllib.request.Request(
        "http://{}/v1/resolve".format(addr),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout = max(
        _ORCH_TIMEOUT_S,
        float(body["download_timeout_secs"]) + _ORCH_RESOLVE_GRACE_S,
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - loopback only
            request, timeout=timeout
        ) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        xbmc.log(
            "NZB-DAV: orchestrator.error endpoint='/v1/resolve' "
            "reason=non_200 status={}".format(e.code),
            xbmc.LOGWARNING,
        )
        return _resolve_return(
            return_fallback_sources,
            None,
            None,
            "orchestrator_non_200: {}".format(e.code),
        )
    except urllib.error.URLError as e:
        xbmc.log(
            "NZB-DAV: orchestrator.error endpoint='/v1/resolve' "
            "reason=transport message={}".format(e),
            xbmc.LOGWARNING,
        )
        return _resolve_return(
            return_fallback_sources,
            None,
            None,
            "orchestrator_transport_error: {}".format(e),
        )
    except OSError as e:
        xbmc.log(
            "NZB-DAV: orchestrator.error endpoint='/v1/resolve' "
            "reason=io message={}".format(e),
            xbmc.LOGWARNING,
        )
        return _resolve_return(
            return_fallback_sources,
            None,
            None,
            "orchestrator_io_error: {}".format(e),
        )

    if status != 200:
        xbmc.log(
            "NZB-DAV: orchestrator.error endpoint='/v1/resolve' "
            "reason=non_200 status={}".format(status),
            xbmc.LOGWARNING,
        )
        return _resolve_return(
            return_fallback_sources,
            None,
            None,
            "orchestrator_non_200: {}".format(status),
        )

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return _resolve_return(
            return_fallback_sources,
            None,
            None,
            "orchestrator_bad_json: {}".format(e),
        )

    stream_url = payload.get("stream_url")
    stream_headers = payload.get("stream_headers") or {}
    if not stream_url or not isinstance(stream_headers, dict):
        return _resolve_return(
            return_fallback_sources, None, None, "orchestrator_bad_shape"
        )
    fallback_sources = _fallback_sources_from_resolve_payload(payload)

    xbmc.log(
        "NZB-DAV: orchestrator.call endpoint='/v1/resolve' outcome=ok "
        "resolve_id={} peers={} fallback_sources={}".format(
            payload.get("resolve_id"),
            len(payload.get("peers") or []),
            len(fallback_sources),
        ),
        xbmc.LOGINFO,
    )
    return _resolve_return(
        return_fallback_sources, stream_url, stream_headers, None, fallback_sources
    )


def _stop_requested(stop_event):
    return stop_event is not None and stop_event.is_set()


def _decode_sse_line(line):
    if isinstance(line, bytes):
        return line.decode("utf-8", errors="replace")
    return str(line)


def _iter_sse_events(resp, stop_event=None):
    event_type = None
    data_lines = []

    while not _stop_requested(stop_event):
        line = resp.readline()
        if not line:
            break
        line = _decode_sse_line(line).rstrip("\r\n")

        if not line:
            event = _sse_event_from_parts(event_type, data_lines)
            if event is not None:
                yield event
            event_type = None
            data_lines = []
            continue

        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if not sep:
            continue
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)

    event = _sse_event_from_parts(event_type, data_lines)
    if event is not None:
        yield event


def _sse_event_from_parts(event_type, data_lines):
    if not data_lines:
        return None
    try:
        event = json.loads("\n".join(data_lines))
    except ValueError as error:
        xbmc.log(
            "NZB-DAV: orchestrator.error endpoint='/v1/resolve/:id/events' "
            "reason=bad_sse_json message={}".format(error),
            xbmc.LOGDEBUG,
        )
        return None
    if not isinstance(event, dict):
        return None
    if event_type and not event.get("event"):
        event["event"] = event_type
    return event


def _event_sequence(event):
    try:
        return int(event.get("sequence") or 0)
    except (TypeError, ValueError):
        return 0


def _event_tail_should_retry(error):
    reason = getattr(error, "reason", None)
    return isinstance(error, (socket.timeout, TimeoutError)) or isinstance(
        reason, (socket.timeout, TimeoutError)
    )


def tail_resolve_events(
    resolve_id,
    on_event,
    settings_getter=None,
    stop_event=None,
):
    """Tail Rust resolve progress events over SSE.

    Calls ``on_event(event_dict)`` for each parsed event and returns
    ``None`` when the stream reaches ``resolve.completed`` or the caller
    asks us to stop. Transport failures are reported as a reason string
    so resolver callers can keep falling back without raising.
    """
    if not _is_enabled(settings_getter):
        return "orchestrator_disabled"

    addr = _orch_addr()
    if addr is None:
        return "orchestrator_addr_unavailable"

    resolve_id = str(resolve_id or "").strip()
    if not resolve_id:
        return "orchestrator_resolve_id_missing"

    quoted_resolve_id = urllib.parse.quote(resolve_id, safe="")
    url = "http://{}/v1/resolve/{}/events?tail=true".format(addr, quoted_resolve_id)
    seen_sequences = set()

    while not _stop_requested(stop_event):
        request = urllib.request.Request(
            url,
            headers={"Accept": "text/event-stream"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - loopback only
                request, timeout=_ORCH_EVENT_TIMEOUT_S
            ) as resp:
                status = getattr(resp, "status", None)
                if status is not None and status != 200:
                    return "orchestrator_events_non_200: {}".format(status)
                for event in _iter_sse_events(resp, stop_event=stop_event):
                    sequence = _event_sequence(event)
                    if sequence and sequence in seen_sequences:
                        continue
                    if sequence:
                        seen_sequences.add(sequence)
                    try:
                        on_event(event)
                    except Exception as error:  # pylint: disable=broad-except
                        xbmc.log(
                            "NZB-DAV: orchestrator progress callback failed: {}".format(
                                error
                            ),
                            xbmc.LOGDEBUG,
                        )
                    if event.get("event") == "resolve.completed":
                        return None
                return None
        except urllib.error.HTTPError as error:
            xbmc.log(
                "NZB-DAV: orchestrator.error endpoint='/v1/resolve/:id/events' "
                "reason=non_200 status={}".format(error.code),
                xbmc.LOGWARNING,
            )
            return "orchestrator_events_non_200: {}".format(error.code)
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
            if _event_tail_should_retry(error) and not _stop_requested(stop_event):
                if stop_event is not None:
                    stop_event.wait(_ORCH_EVENT_RECONNECT_DELAY_S)
                else:
                    time.sleep(_ORCH_EVENT_RECONNECT_DELAY_S)
                continue
            if _stop_requested(stop_event):
                return None
            xbmc.log(
                "NZB-DAV: orchestrator.error endpoint='/v1/resolve/:id/events' "
                "reason=transport message={}".format(error),
                xbmc.LOGWARNING,
            )
            return "orchestrator_events_transport_error: {}".format(error)

    return None
