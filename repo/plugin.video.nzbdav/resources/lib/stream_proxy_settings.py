# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Addon-settings access + runtime-settings snapshots for the stream proxy.

Extracted from ``stream_proxy.py`` (Stage 1 decomposition). These helpers read
Kodi addon settings, build/normalize the immutable settings snapshot consumed
on the proxy thread, and derive the per-stream runtime knobs (force-remux
threshold/mode, contract mode, density/zero-fill/retry toggles, stall wait,
read-ahead buffer size). All names are re-exported by ``stream_proxy`` so
existing references and test patches (e.g. ``stream_proxy._get_addon_setting``)
keep resolving.

Plain constants are imported from ``stream_proxy``; every parent-namespace
function and any monkeypatch target (``xbmc``, ``_get_addon_setting``) is
reached at call time via ``_sp.<name>`` so patching keeps working without
threading new parameters.
"""

import resources.lib.stream_proxy as _sp  # noqa: E402
from resources.lib.stream_proxy import (  # noqa: E402
    _DEFAULT_FORCE_REMUX_THRESHOLD_MB,
    _DEFAULT_PASSTHROUGH_STALL_WAIT_SECONDS,
    _DEFAULT_READAHEAD_BUFFER_MB,
    _FORCE_REMUX_THRESHOLD_MB_MAX,
    _KODI_SETTING_ERRORS,
    _PASSTHROUGH_RUNTIME_SETTINGS_DONE_KEY,
    _PASSTHROUGH_RUNTIME_SETTINGS_ERROR_KEY,
    _PASSTHROUGH_RUNTIME_SETTINGS_KEY,
    _PASSTHROUGH_STALL_WAIT_MAX_SECONDS,
    _READAHEAD_BUFFER_MB_MAX,
    _SETTINGS_SNAPSHOT_KEYS,
    _STRICT_CONTRACT_MODE_ENFORCE,
    _STRICT_CONTRACT_MODE_OFF,
    _STRICT_CONTRACT_MODE_WARN,
)


def _get_addon_setting(setting_id, default=None):
    """Best-effort Kodi addon setting lookup safe for tests and CLI.

    Prefer the Kodi API when it is available, so tests and live settings
    overrides win. Fall back to reading the addon's ``settings.xml``
    directly only when the Kodi binding is unavailable or raises during
    script-mode startup.
    """
    if _sp.xbmcaddon is not None:
        try:
            value = _sp.xbmcaddon.Addon("plugin.video.nzbdav").getSetting(setting_id)
            return default if value is None else value
        except _KODI_SETTING_ERRORS:
            pass
    try:
        from resources.lib.router import _get_script_setting

        value = _get_script_setting(setting_id, default if default is not None else "")
        if value or default is None:
            return value
    except Exception:  # pylint: disable=broad-except
        pass
    return default


def normalize_settings_snapshot(settings_snapshot):
    """Return a sanitized prepare-time setting snapshot."""
    if not isinstance(settings_snapshot, dict):
        return {}
    snapshot = {}
    for key in _SETTINGS_SNAPSHOT_KEYS:
        value = settings_snapshot.get(key)
        if isinstance(value, str):
            snapshot[key] = value
    return snapshot


def build_settings_snapshot(settings_getter=None):
    """Read proxy settings on the caller side before the service /prepare hop."""
    snapshot = {}
    for key in _SETTINGS_SNAPSHOT_KEYS:
        if settings_getter is None:
            value = _sp._get_addon_setting(key, "")
        else:
            try:
                value = settings_getter(key, "")
            except _KODI_SETTING_ERRORS:
                value = ""
        snapshot[key] = value if isinstance(value, str) else ""
    return snapshot


def _set_addon_setting(setting_id, value):
    """Best-effort Kodi addon setting write safe for tests and CLI."""
    if _sp.xbmcaddon is None:
        return False
    try:
        _sp.xbmcaddon.Addon("plugin.video.nzbdav").setSetting(setting_id, value)
    except _KODI_SETTING_ERRORS:
        return False
    return True


def _clamp_int_setting(setting_id, value, lo, hi):
    """Clamp an integer setting and log when user input was out of range."""
    clamped = value
    if value < lo:
        clamped = lo
    elif value > hi:
        clamped = hi
    if clamped != value:
        _sp.xbmc.log(
            "NZB-DAV: Setting {}={} out of range [{}..{}]; clamping to {}".format(
                setting_id, value, lo, hi, clamped
            ),
            _sp.xbmc.LOGWARNING,
        )
    return clamped


def _bool_from_snapshot(snapshot, setting_id, default=False):
    raw = snapshot.get(setting_id) if isinstance(snapshot, dict) else None
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _int_from_snapshot(snapshot, setting_id, default, lo, hi):
    """Return a clamped int setting from a snapshot. Does not read Kodi
    settings: any parse failure falls back to ``default`` (an out-of-range value
    is still clamped via ``_clamp_int_setting``, which logs a warning)."""
    raw = snapshot.get(setting_id) if isinstance(snapshot, dict) else None
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        value = default
    return _sp._clamp_int_setting(setting_id, value, lo, hi)


def _strict_contract_mode_from_snapshot(snapshot):
    raw = snapshot.get("strict_contract_mode") if isinstance(snapshot, dict) else None
    key = str(raw).strip().lower() if raw is not None else ""
    mapping = {
        "0": _STRICT_CONTRACT_MODE_OFF,
        _STRICT_CONTRACT_MODE_OFF: _STRICT_CONTRACT_MODE_OFF,
        "1": _STRICT_CONTRACT_MODE_WARN,
        _STRICT_CONTRACT_MODE_WARN: _STRICT_CONTRACT_MODE_WARN,
        "2": _STRICT_CONTRACT_MODE_ENFORCE,
        _STRICT_CONTRACT_MODE_ENFORCE: _STRICT_CONTRACT_MODE_ENFORCE,
    }
    return mapping.get(key, _STRICT_CONTRACT_MODE_WARN)


def _force_remux_threshold_bytes_from_snapshot(snapshot):
    raw = (
        snapshot.get("force_remux_threshold_mb") if isinstance(snapshot, dict) else None
    )
    try:
        mb = int(raw) if raw not in (None, "") else _DEFAULT_FORCE_REMUX_THRESHOLD_MB
    except (TypeError, ValueError):
        mb = _DEFAULT_FORCE_REMUX_THRESHOLD_MB
    mb = _sp._clamp_int_setting(
        "force_remux_threshold_mb", mb, 0, _FORCE_REMUX_THRESHOLD_MB_MAX
    )
    if mb == 0:
        return 0
    return mb * 1024 * 1024


def _force_remux_mode_from_snapshot(snapshot):
    raw = snapshot.get("force_remux_mode") if isinstance(snapshot, dict) else None
    migrated = (
        snapshot.get("force_remux_mode_v2_migrated", "false")
        if isinstance(snapshot, dict)
        else "false"
    )
    if str(migrated).lower() != "true" and raw == "2":
        raw = "0"
    if raw == "1":
        return "hls_fmp4"
    if raw == "2":
        return "matroska"
    return "passthrough"


def _passthrough_runtime_settings_from_snapshot(snapshot):
    contract_mode = _sp._strict_contract_mode_from_snapshot(snapshot)
    density_breaker_enabled = False
    if contract_mode != _STRICT_CONTRACT_MODE_OFF:
        density_breaker_enabled = _sp._bool_from_snapshot(
            snapshot, "density_breaker_enabled", default=False
        )
    return {
        "contract_mode": contract_mode,
        "density_breaker_enabled": density_breaker_enabled,
        "zero_fill_budget_enabled": _sp._bool_from_snapshot(
            snapshot, "zero_fill_budget_enabled", default=True
        ),
        "retry_ladder_enabled": _sp._bool_from_snapshot(
            snapshot, "retry_ladder_enabled", default=True
        ),
        "send_200_no_range_enabled": _sp._bool_from_snapshot(
            snapshot, "send_200_no_range", default=False
        ),
        "passthrough_stall_wait_seconds": _sp._int_from_snapshot(
            snapshot,
            "passthrough_stall_wait",
            _DEFAULT_PASSTHROUGH_STALL_WAIT_SECONDS,
            0,
            _PASSTHROUGH_STALL_WAIT_MAX_SECONDS,
        ),
        "readahead_buffer_mb": _sp._int_from_snapshot(
            snapshot,
            "readahead_buffer_mb",
            _DEFAULT_READAHEAD_BUFFER_MB,
            0,
            _READAHEAD_BUFFER_MB_MAX,
        ),
    }


def _get_server_context_lock(server):
    """Return the proxy's context lock when the handler is attached to one."""
    server_state = getattr(server, "__dict__", None)
    if not isinstance(server_state, dict):
        return None
    owner_proxy = server_state.get("owner_proxy")
    return getattr(owner_proxy, "_context_lock", None)


def _get_force_remux_threshold_bytes():
    """Return the remux-force threshold in bytes, or 0 to disable."""
    raw = _sp._get_addon_setting("force_remux_threshold_mb")
    try:
        mb = int(raw) if raw not in (None, "") else _DEFAULT_FORCE_REMUX_THRESHOLD_MB
    except (TypeError, ValueError):
        mb = _DEFAULT_FORCE_REMUX_THRESHOLD_MB
    mb = _sp._clamp_int_setting(
        "force_remux_threshold_mb", mb, 0, _FORCE_REMUX_THRESHOLD_MB_MAX
    )
    if mb == 0:
        return 0
    return mb * 1024 * 1024


def _get_force_remux_mode():
    """Return 'matroska', 'hls_fmp4', or 'passthrough' for the force-remux
    branch.

    Empty string, unset, or '0' -> 'passthrough' (default).
    '1' -> 'hls_fmp4' (experimental, DV-capable).
    '2' -> 'matroska' (compatibility remux).
    Any other value -> 'passthrough'.
    """
    raw = _sp._get_addon_setting("force_remux_mode")
    migrated = _sp._get_addon_setting("force_remux_mode_v2_migrated", "false")
    if str(migrated).lower() != "true":
        if raw == "2":
            # Before pass-through became the default, enum value 2 meant
            # explicit pass-through. Preserve that intent once, then let
            # future value 2 selections mean Matroska compatibility mode.
            _sp._set_addon_setting("force_remux_mode", "0")
            raw = "0"
        _sp._set_addon_setting("force_remux_mode_v2_migrated", "true")
    if raw == "1":
        return "hls_fmp4"
    if raw == "2":
        return "matroska"
    return "passthrough"


def _get_strict_contract_mode():
    """Return off/warn/enforce for upstream response validation."""
    raw = _sp._get_addon_setting("strict_contract_mode")
    key = str(raw).strip().lower() if raw is not None else ""
    mapping = {
        "0": _STRICT_CONTRACT_MODE_OFF,
        _STRICT_CONTRACT_MODE_OFF: _STRICT_CONTRACT_MODE_OFF,
        "1": _STRICT_CONTRACT_MODE_WARN,
        _STRICT_CONTRACT_MODE_WARN: _STRICT_CONTRACT_MODE_WARN,
        "2": _STRICT_CONTRACT_MODE_ENFORCE,
        _STRICT_CONTRACT_MODE_ENFORCE: _STRICT_CONTRACT_MODE_ENFORCE,
    }
    return mapping.get(key, _STRICT_CONTRACT_MODE_WARN)


def _get_bool_setting(setting_id, default=False):
    """Return a Kodi bool-like setting with a safe default."""
    raw = _sp._get_addon_setting(setting_id)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _density_breaker_enabled(contract_mode=None):
    """Return True when the recovery density breaker should run."""
    mode = contract_mode or _sp._get_strict_contract_mode()
    if mode == _STRICT_CONTRACT_MODE_OFF:
        return False
    return _sp._get_bool_setting("density_breaker_enabled", default=False)


def _zero_fill_budget_enabled():
    return _sp._get_bool_setting("zero_fill_budget_enabled", default=True)


def _retry_ladder_enabled():
    return _sp._get_bool_setting("retry_ladder_enabled", default=True)


def _send_200_no_range_enabled():
    return _sp._get_bool_setting("send_200_no_range", default=False)


def _get_passthrough_stall_wait_seconds():
    """Return the patient forward-stall budget in seconds, clamped [0, 600]."""
    raw = _sp._get_addon_setting("passthrough_stall_wait")
    try:
        secs = (
            int(raw)
            if raw not in (None, "")
            else _DEFAULT_PASSTHROUGH_STALL_WAIT_SECONDS
        )
    except (TypeError, ValueError):
        secs = _DEFAULT_PASSTHROUGH_STALL_WAIT_SECONDS
    return _sp._clamp_int_setting(
        "passthrough_stall_wait", secs, 0, _PASSTHROUGH_STALL_WAIT_MAX_SECONDS
    )


def _get_readahead_buffer_mb():
    """Return the read-ahead prefetch buffer size in MB, clamped [0, MAX].

    0 disables the read-ahead layer (no buffer, no thread) so behavior is
    byte-for-byte identical to today.
    """
    raw = _sp._get_addon_setting("readahead_buffer_mb")
    try:
        mb = int(raw) if raw not in (None, "") else _DEFAULT_READAHEAD_BUFFER_MB
    except (TypeError, ValueError):
        mb = _DEFAULT_READAHEAD_BUFFER_MB
    return _sp._clamp_int_setting(
        "readahead_buffer_mb", mb, 0, _READAHEAD_BUFFER_MB_MAX
    )


def _read_passthrough_runtime_settings():
    """Read per-session pass-through recovery settings once."""
    contract_mode = _sp._get_strict_contract_mode()
    return {
        "contract_mode": contract_mode,
        "density_breaker_enabled": _sp._density_breaker_enabled(contract_mode),
        "zero_fill_budget_enabled": _sp._zero_fill_budget_enabled(),
        "retry_ladder_enabled": _sp._retry_ladder_enabled(),
        "send_200_no_range_enabled": _sp._send_200_no_range_enabled(),
        "passthrough_stall_wait_seconds": _sp._get_passthrough_stall_wait_seconds(),
        "readahead_buffer_mb": _sp._get_readahead_buffer_mb(),
    }


def _passthrough_runtime_settings(ctx):
    """Return cached pass-through settings, waiting for a prefetch if present."""
    if isinstance(ctx, dict):
        settings = ctx.get(_PASSTHROUGH_RUNTIME_SETTINGS_KEY)
        if isinstance(settings, dict):
            return settings
        done = ctx.get(_PASSTHROUGH_RUNTIME_SETTINGS_DONE_KEY)
        if done is not None:
            try:
                done.wait()
            except (AttributeError, RuntimeError):
                pass
            settings = ctx.get(_PASSTHROUGH_RUNTIME_SETTINGS_KEY)
            if isinstance(settings, dict):
                return settings
            error = ctx.get(_PASSTHROUGH_RUNTIME_SETTINGS_ERROR_KEY)
            if error is not None:
                _sp.xbmc.log(
                    "NZB-DAV: Pass-through settings prefetch failed: {}".format(error),
                    _sp.xbmc.LOGDEBUG,
                )

    settings = _sp._read_passthrough_runtime_settings()
    if isinstance(ctx, dict):
        ctx[_PASSTHROUGH_RUNTIME_SETTINGS_KEY] = settings
    return settings
