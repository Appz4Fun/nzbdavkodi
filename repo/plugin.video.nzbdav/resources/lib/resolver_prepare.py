# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Direct-playback prepare + service-config helpers.

Cohesive helper group split out of ``resolver`` to keep every module under
Codacy's 500-NLOC file gate. References to names that live in (or are patched
via) ``resolver`` are resolved at call time through
``import resources.lib.resolver as _resolver`` so the suite's
``@patch("resources.lib.resolver.<name>")`` decorators keep working with no
top-level import cycle; same-module sibling helpers are called directly. Every
moved name is re-exported from ``resolver``.
"""

import resources.lib.resolver as _resolver  # noqa: F401  pylint: disable=unused-import


def _prepare_direct_playback(
    stream_url,
    stream_headers,
    fallback_sources=None,
    service_port=None,
    prepare_token=None,
    settings_getter=None,
    settings_snapshot=None,
):
    """Prepare resolver playback without touching Kodi UI state."""
    from resources.lib.stream_proxy import (
        build_settings_snapshot,
        get_service_proxy_port,
        prepare_stream_via_service,
    )

    if service_port is None:
        service_port = get_service_proxy_port()

    prepared = {
        "service_port": service_port,
        "stream_url": stream_url,
        "stream_headers": stream_headers,
        "proxy_url": "",
        "stream_info": {},
    }
    if not service_port:
        return prepared

    auth_header = _resolver._stream_auth_header(stream_headers)
    prepare_kwargs = {"fallback_sources": fallback_sources}
    content_length_hint = _resolver._get_stream_content_length_hint(
        stream_url, auth_header
    )
    if content_length_hint > 0:
        prepare_kwargs["content_length_hint"] = content_length_hint
    if settings_snapshot is None:
        settings_snapshot = build_settings_snapshot(settings_getter=settings_getter)
    if any(settings_snapshot.values()):
        prepare_kwargs["settings_snapshot"] = settings_snapshot
    if prepare_token is not None:
        prepare_kwargs["prepare_token"] = prepare_token
    proxy_url, stream_info = prepare_stream_via_service(
        service_port, stream_url, auth_header, **prepare_kwargs
    )
    prepared["proxy_url"] = proxy_url
    prepared["stream_info"] = stream_info
    return prepared


def _direct_playback_service_config():
    """Read proxy connection details on the resolver thread."""
    from resources.lib import stream_proxy

    if getattr(stream_proxy, "get_service_proxy_port", None) is getattr(
        stream_proxy, "_ORIGINAL_GET_SERVICE_PROXY_PORT", None
    ) and getattr(stream_proxy, "get_service_proxy_token", None) is getattr(
        stream_proxy, "_ORIGINAL_GET_SERVICE_PROXY_TOKEN", None
    ):
        return stream_proxy.get_service_proxy_config()

    from resources.lib.stream_proxy import (
        get_service_proxy_port,
        get_service_proxy_token,
    )

    service_port = get_service_proxy_port()
    prepare_token = get_service_proxy_token() if service_port else ""
    return service_port, prepare_token


def _ready_direct_playback_service_config_state(service_port, prepare_token):
    done = _resolver.threading.Event()
    done.set()
    return {
        "done": done,
        "error": None,
        "service_port": service_port,
        "prepare_token": prepare_token,
        "thread": None,
    }


def _start_direct_playback_service_config_lookup():
    """Start proxy service config lookup before stream readiness."""
    done = _resolver.threading.Event()
    state = {
        "done": done,
        "error": None,
        "service_port": None,
        "prepare_token": "",  # nosec B105 — empty init value, not a secret
        "thread": None,
    }

    def _worker():
        try:
            state["service_port"], state["prepare_token"] = (
                _resolver._direct_playback_service_config()
            )
        except Exception as error:  # pylint: disable=broad-except
            state["error"] = error
        finally:
            done.set()

    thread = _resolver.threading.Thread(
        target=_worker, name="nzbdav-direct-playback-service-config", daemon=True
    )
    state["thread"] = thread
    try:
        thread.start()
    except RuntimeError:
        try:
            service_port, prepare_token = _resolver._direct_playback_service_config()
            return _ready_direct_playback_service_config_state(
                service_port, prepare_token
            )
        except Exception as error:  # pylint: disable=broad-except
            state["error"] = error
            done.set()
    return state


def _wait_direct_playback_service_config(state):
    if not state:
        return _resolver._direct_playback_service_config()
    done = state.get("done")
    if done:
        done.wait()
    error = state.get("error")
    if error is not None:
        raise error
    return state.get("service_port") or 0, state.get("prepare_token") or ""


def _prepare_direct_playback_with_service_config(
    stream_url,
    stream_headers,
    fallback_sources,
    service_config_state,
    settings_getter=None,
    settings_snapshot=None,
):
    from resources.lib.stream_proxy import (
        ServiceProxyUnavailableError,
        build_settings_snapshot,
    )

    if settings_snapshot is None:
        settings_snapshot = build_settings_snapshot(settings_getter=settings_getter)
    service_port, prepare_token = _wait_direct_playback_service_config(
        service_config_state
    )
    try:
        return _resolver._prepare_direct_playback(
            stream_url,
            stream_headers,
            fallback_sources=fallback_sources,
            service_port=service_port,
            prepare_token=prepare_token,
            settings_getter=settings_getter,
            settings_snapshot=settings_snapshot,
        )
    except ServiceProxyUnavailableError:
        fresh_service_port, fresh_prepare_token = (
            _resolver._direct_playback_service_config()
        )
        if (fresh_service_port, fresh_prepare_token) == (service_port, prepare_token):
            raise
        return _resolver._prepare_direct_playback(
            stream_url,
            stream_headers,
            fallback_sources=fallback_sources,
            service_port=fresh_service_port,
            prepare_token=fresh_prepare_token,
            settings_getter=settings_getter,
            settings_snapshot=settings_snapshot,
        )


def _ready_direct_playback_prepare_state(prepared):
    done = _resolver.threading.Event()
    done.set()
    return {"done": done, "error": None, "prepared": prepared, "thread": None}


def _monitor_abort_requested(monitor):
    """Return Kodi's abort flag without entering a wait call."""
    try:
        return monitor.abortRequested() is True
    except (AttributeError, RuntimeError, TypeError):
        return False


def _wait_for_abort_or_timeout(monitor, wait_seconds, tick_seconds=0.05):
    import time as real_time

    deadline = real_time.monotonic() + max(0, wait_seconds)
    while True:
        if _monitor_abort_requested(monitor):
            return True
        remaining = deadline - real_time.monotonic()
        if remaining <= 0:
            return False
        _resolver.threading.Event().wait(min(tick_seconds, remaining))


def _settings_getter_kwargs(settings_getter):
    return {"settings_getter": settings_getter} if settings_getter is not None else {}


def _claim_dialog_update_slot(key):
    """Reserve the single in-flight update slot for ``key`` or return None."""
    with _resolver._DIALOG_UPDATE_LOCK:
        inflight = _resolver._DIALOG_UPDATE_INFLIGHT.get(key)
        if inflight is not None and not inflight.is_set():
            return None
        done = _resolver.threading.Event()
        _resolver._DIALOG_UPDATE_INFLIGHT[key] = done
    return done


def _release_dialog_update_slot(key, done):
    """Drop the in-flight update slot for ``key`` when ``done`` still owns it."""
    done.set()
    with _resolver._DIALOG_UPDATE_LOCK:
        if _resolver._DIALOG_UPDATE_INFLIGHT.get(key) is done:
            _resolver._DIALOG_UPDATE_INFLIGHT.pop(key, None)


def _safe_dialog_update(dialog, progress, message):
    """Best-effort progress update that cannot block the resolver loop."""
    key = id(dialog)
    done = _claim_dialog_update_slot(key)
    if done is None:
        return False

    def _worker():
        try:
            dialog.update(progress, message)
        except Exception as error:  # pylint: disable=broad-except
            _resolver.xbmc.log(
                "NZB-DAV: progress dialog update failed: {}".format(error),
                _resolver.xbmc.LOGDEBUG,
            )
        finally:
            _release_dialog_update_slot(key, done)

    try:
        _resolver.threading.Thread(
            target=_worker, name="nzbdav-dialog-progress-update", daemon=True
        ).start()
        return True
    except RuntimeError as error:
        _release_dialog_update_slot(key, done)
        _resolver.xbmc.log(
            "NZB-DAV: progress dialog update thread failed: {}".format(error),
            _resolver.xbmc.LOGDEBUG,
        )
        return False


def _start_direct_playback_prepare(
    stream_url,
    stream_headers,
    fallback_sources=None,
    service_config_state=None,
    settings_getter=None,
):
    """Start proxy prepare in the background and return its state."""
    from resources.lib.stream_proxy import build_settings_snapshot

    if service_config_state is None:
        service_port, prepare_token = _resolver._direct_playback_service_config()
    else:
        service_port, prepare_token = None, None
    if service_config_state is None and not service_port:
        prepared = _resolver._prepare_direct_playback(
            stream_url,
            stream_headers,
            fallback_sources=fallback_sources,
            service_port=service_port,
            prepare_token=prepare_token,
            settings_getter=settings_getter,
            settings_snapshot={},
        )
        return _ready_direct_playback_prepare_state(prepared)

    done = _resolver.threading.Event()
    state = {
        "done": done,
        "error": None,
        "prepared": None,
        "thread": None,
    }

    def _worker():
        try:
            settings_snapshot = build_settings_snapshot(settings_getter=settings_getter)
            if service_config_state is None:
                state["prepared"] = _resolver._prepare_direct_playback(
                    stream_url,
                    stream_headers,
                    fallback_sources=fallback_sources,
                    service_port=service_port,
                    prepare_token=prepare_token,
                    settings_getter=settings_getter,
                    settings_snapshot=settings_snapshot,
                )
            else:
                state["prepared"] = _prepare_direct_playback_with_service_config(
                    stream_url,
                    stream_headers,
                    fallback_sources,
                    service_config_state,
                    settings_getter=settings_getter,
                    settings_snapshot=settings_snapshot,
                )
        except Exception as error:  # pylint: disable=broad-except
            state["error"] = error
        finally:
            done.set()

    thread = _resolver.threading.Thread(
        target=_worker, name="nzbdav-direct-playback-prepare", daemon=True
    )
    state["thread"] = thread
    try:
        thread.start()
    except RuntimeError:
        state["thread"] = None
        _worker()
    return state


def _wait_direct_playback_prepare(
    state, wait_seconds=_resolver._PLAYBACK_PREPARE_HANDOFF_GRACE_SECONDS
):
    done = state.get("done")
    if done:
        if not done.wait(max(0, wait_seconds)):
            _resolver.xbmc.log(
                "NZB-DAV: Proxy prepare still running; "
                "waiting for local proxy handoff",
                _resolver.xbmc.LOGWARNING,
            )
            done.wait()
    error = state.get("error")
    if error is not None:
        raise error
    return state.get("prepared")
