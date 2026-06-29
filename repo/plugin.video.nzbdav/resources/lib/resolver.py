# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import,unused-import

"""Resolve flow: submit NZB to nzbdav, poll until stream is ready, play.

This module is the resolver's public surface: every helper lives in a sibling
``resolver_*`` module and is re-imported below so the test suite's
``from resources.lib.resolver import <name>`` imports and
``@patch("resources.lib.resolver.<name>")`` decorators keep resolving, and the
sibling modules' own ``import resources.lib.resolver as _resolver`` back-edges
(call-time resolution) reach the dependency imports kept here. The re-exports
are therefore deliberately unused within this file.
"""

import http.client  # noqa: F401
import socket  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401
from urllib.error import URLError  # noqa: F401
from urllib.parse import unquote  # noqa: F401

import xbmc  # noqa: F401
import xbmcaddon  # noqa: F401
import xbmcgui  # noqa: F401
import xbmcplugin  # noqa: F401
import xbmcvfs  # noqa: F401

from resources.lib import resume_choice, resume_store  # noqa: F401
from resources.lib.dead_candidates import (  # noqa: F401
    DeadCandidates,
    is_provably_dead_submit_error,
)
from resources.lib.download_ledger import record_download  # noqa: F401
from resources.lib.fallback_streams import (  # noqa: F401
    FALLBACK_CANDIDATES_DISABLED,
    build_fallback_job_name,
    build_prepare_fallback_payload,
)
from resources.lib.http_util import notify as _notify  # noqa: F401
from resources.lib.i18n import addon_name as _addon_name  # noqa: F401
from resources.lib.i18n import fmt as _fmt  # noqa: F401
from resources.lib.i18n import string as _string  # noqa: F401
from resources.lib.nzbdav_api import (  # noqa: F401
    cancel_job,
    clear_queue,
    find_completed_by_name,
    find_completed_by_names,
    find_queued_by_name,
    find_queued_by_names,
    get_job_history,
    get_job_status,
    get_queue_slots,
    submit_nzb,
)
from resources.lib.webdav import (  # noqa: F401
    find_video_file,
    find_video_stream_for_folder,
    get_webdav_stream_url_for_path,
    probe_webdav_reachable,
)

# Sentinel returned by the per-iteration poll helper to mean "keep looping"
# (distinct from the ``(None, None)`` tuple, which is a terminal failure).
_POLL_CONTINUE = object()

_POLL_INTERVAL_MIN = 1

_POLL_INTERVAL_MAX = 60

_DOWNLOAD_TIMEOUT_MIN = 60

_DOWNLOAD_TIMEOUT_MAX = 86400

MAX_POLL_ITERATIONS = _DOWNLOAD_TIMEOUT_MAX // _POLL_INTERVAL_MIN

_FALLBACK_SHUTDOWN_JOIN_TIMEOUT = 10

_POLL_NEAR_COMPLETE_PERCENTAGE = 99.0

_POLL_LATE_ACTIVE_HISTORY_GRACE_PERCENTAGE = 95.0

_POLL_ACTIVE_HISTORY_GRACE_SECONDS = 0.025

_POLL_LATE_ACTIVE_HISTORY_GRACE_SECONDS = 0.025

_POLL_NEAR_COMPLETE_HISTORY_GRACE_SECONDS = 0.1

_POLL_FULL_PROGRESS_HISTORY_GRACE_SECONDS = 0.14

_POLL_NEAR_COMPLETE_FAST_REPOLL_SECONDS = 0.1

_POLL_NEAR_COMPLETE_FAST_REPOLL_COUNT = 5

_PLAYBACK_CLEANUP_HANDOFF_GRACE_SECONDS = 0.25

_PLAYBACK_PREPARE_HANDOFF_GRACE_SECONDS = 8.0

_STREAM_CONTENT_LENGTH_HINT_TTL_SECONDS = 30.0

_STREAM_CONTENT_LENGTH_HINTS_MAX = 128

_STREAM_CONTENT_LENGTH_HINTS = {}

_STREAM_CONTENT_LENGTH_HINTS_LOCK = threading.Lock()

# HTTP status codes the submit retry loop treats as transient and worth
# retrying. RFC 9110 explicitly calls 408 retry-friendly ("client may
# assume the server closed the connection due to inactivity and retry").
# 502/503/504 are classic gateway/service-layer transients. 429 is
# deliberately excluded because the current 2s retry spacing would just
# stack rate-limit violations — if 429 ever becomes a real failure mode
# we'll need backoff first.
_TRANSIENT_HTTP_STATUSES = (408, 502, 503, 504)

_DB_DISCOVERY_ERRORS = (
    AttributeError,
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)

_RESOLVE_RUNTIME_ERRORS = (
    # Network-layer exceptions that escaped earlier helpers — `socket.timeout`
    # is a `TimeoutError` subclass on 3.10+ but a separate type on 3.8/3.9,
    # `URLError` wraps DNS / connection-refused / unreachable, `HTTPException`
    # covers `BadStatusLine` and friends. All three could otherwise bypass
    # the resolver's setResolvedUrl-on-failure guarantee. TODO.md §H.3.
    URLError,
    socket.timeout,
    AttributeError,
    KeyError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)

# Per-setting warn suppression: we log the out-of-range clamp exactly once
# per (setting_id, value) so a user with a typo'd setting doesn't see the
# same warning spam on every play.
_CLAMP_LOGGED = set()

_DIALOG_UPDATE_LOCK = threading.Lock()

_DIALOG_UPDATE_INFLIGHT = {}

_SCRIPT_PLAY_STAGE_PATH = "/storage/.kodi/temp/nzbdav-script-play-stage.log"

_STATUS_MESSAGES = {
    "Queued": 30102,
    "Fetching": 30103,
    "Propagating": 30104,
    "Downloading": 30105,
    "Paused": 30106,
}

_ERROR_MESSAGES = {
    "auth_failed": 30107,
    "server_error": 30108,
    "connection_error": 30109,
}

_ACTIVE_QUEUE_STATUSES = frozenset(
    (
        "queued",
        "downloading",
        "paused",
        "quickcheck",
        "verifying",
        "repairing",
        "extracting",
        "moving",
    )
)

# UI update cadence while submit_nzb is running on a background thread.
# Kept slower than adoption checks so the progress dialog looks live without
# redrawing for every queue-probe poll.
_SUBMIT_UI_PUMP_INTERVAL_SECONDS = 0.25

_SUBMIT_ADOPTION_CHECK_INTERVAL_SECONDS = 0.05

_SUBMIT_QUEUE_PROBE_INITIAL_DELAY_SECONDS = 0.0

_SUBMIT_QUEUE_PROBE_FAST_INTERVAL_SECONDS = 0.05

_SUBMIT_QUEUE_PROBE_FAST_WINDOW_SECONDS = 2.0

_SUBMIT_QUEUE_PROBE_INTERVAL_SECONDS = 0.25

_SUBMIT_HISTORY_PROBE_PARALLEL_GRACE_SECONDS = 0.01

_COMPLETED_NO_VIDEO_RECHECK_DELAYS_SECONDS = (0.025, 0.075, 0.1)

# #282: nzbdav writes a small placeholder .mp4 at job start; the completed
# WebDAV scan can pick it up seconds after submit and stream it instead of the
# feature (playing ~30s of a stub). A single-file (non-pack) release whose
# discovered video is smaller than this fraction of the indexer-advertised size
# is treated as that stub and rejected so the poll loop keeps waiting for the
# real download. Conservatively low: a genuine single file is ~0.85-0.95 of its
# advertised NZB size (par2/rar/sample overhead), while the reported stub was
# ~0.004, so 0.5 sits far from both. The guard fails OPEN when either size is
# unknown and is skipped entirely for packs (release_is_pack), where one
# episode is legitimately a fraction of the whole-pack advertised size.
_STUB_VIDEO_MIN_ADVERTISED_FRACTION = 0.5

# After a submit timeout, how many times to poll nzbdav before giving up
# on adoption and retrying the submit. 6 polls * 2 s = 12 s of total wait
# — enough headroom for nzbdav to finish fetching/parsing a moderately
# large NZB, short enough not to double the user's wait on a genuine
# network failure.
_SUBMIT_ADOPT_POLL_COUNT = 6

_SUBMIT_ADOPT_POLL_INTERVAL_SECONDS = 2

_CLEAR_QUEUE_ON_SUBMIT_MODES = {"0": "ask", "1": "always", "2": "never"}

# Short, best-effort timeout for the pre-submit queue operations — both the
# probe (mode=queue) and EACH per-slot delete. They run on the resolver thread
# before the threaded submit/dialog pump, so a slow/unreachable nzbdav must
# fail fast rather than freezing playback for minutes across several deletes.
_CLEAR_QUEUE_PROBE_TIMEOUT = 5

# Seconds INTO playback to hold the fallback prewarm/submit burst. Anchored to
# actual playback start (not primary submission, which can precede playback by a
# whole download for a slow primary), so backups are only fetched well after the
# video is established: a working playback that never needs them submits none,
# and the burst never contends with the fragile startup cache-fill window. The
# wait is cancellable -- a session stop aborts it with no submission.
_FALLBACK_PREWARM_DELAY_SECONDS = 120

# Poll granularity while waiting for the playback-start signal, plus a defensive
# cap after which the worker proceeds even if the signal never arrived (so a
# missed signal degrades to a late submit, never a permanently stranded backup).
_FALLBACK_PLAYBACK_WAIT_POLL_SECONDS = 1.0

_FALLBACK_PLAYBACK_WAIT_CAP_SECONDS = 300.0

# Short interval the prewarm wait wakes on to re-check whether playback went
# inactive cross-process. Small enough to abort promptly, big enough to avoid
# busy-spinning. Module-level so tests can patch it to run fast.
_FALLBACK_PREWARM_POLL_SECONDS = 1.0

_FALLBACK_TERMINAL_STATUSES = frozenset(
    (
        "aborted",
        "cancelled",
        "canceled",
        "completed",
        "complete",
        "deleted",
        "failed",
        "failure",
        "finished",
        "history",
        "success",
    )
)

# Cohesive helper groups split into sibling ``resolver_*`` modules to keep
# this file under Codacy's 500-NLOC gate. Re-exported here so the suite's
# ``from resources.lib.resolver import <name>`` imports, the
# ``@patch("resources.lib.resolver.<name>")`` decorators, and the sibling
# modules' own ``_resolver.<name>`` back-references all keep resolving. The
# moved helpers reach names that live in (or are patched via) this module
# through a top-of-module ``import resources.lib.resolver as _resolver``
# (call-time resolution preserves the patches without a top-level cycle).
from resources.lib.resolver_completed import (  # noqa: E402,F401
    _close_dialog_before_submit_error,
    _completed_job_stream,
    _completed_job_video_rejected,
    _completed_job_webdav_folder,
    _delegated_find_video_stream_for_folder,
    _existing_completed_stream,
    _find_video_stream_for_folder,
    _picker_completed_lookup_done,
    _picker_completed_stream,
    _record_rejected_completed_id,
    _show_submit_error_dialog,
    _start_existing_completed_cleanup,
    _submit_error_is_too_many_requests,
    _submit_error_with_indexer,
)
from resources.lib.resolver_entry import (  # noqa: E402,F401
    resolve,
    resolve_and_play,
)
from resources.lib.resolver_fallback import (  # noqa: E402,F401
    _adopt_existing_fallback_job,
    _await_fallback_worker_finish,
    _await_playback_start,
    _cancel_fallback_job,
    _cancel_fallback_submitted_jobs,
    _collect_fallback_candidate_jobs,
    _fallback_candidate_row,
    _fallback_job_pending,
    _fallback_job_value,
    _fallback_streams_enabled,
    _fallback_submit_jobs_snapshot,
    _get_fallback_submit_delay_seconds,
    _invoke_fallback_job_cancel,
    _load_and_submit_fallback_candidates,
    _lookup_existing_fallback_jobs,
    _notify_no_fallback_candidates,
    _playback_active_flag,
    _prefetch_fallback_candidate_loader,
    _prewarm_playback_latch,
    _recover_fallback_submit_error,
    _resolve_active_fallback_candidates,
    _resolve_fallback_candidate_job,
    _run_fallback_on_append_hook,
    _signal_fallback_playback_started,
    _start_fallback_submit_worker,
    _stop_fallback_submit_worker,
    _submit_fallback_candidates,
    _submit_one_fallback_candidate,
    _wait_prewarm_or_inactive,
)
from resources.lib.resolver_flow import (  # noqa: E402,F401
    _invoke_poll_until_ready,
    _nzbget_enabled,
    _prepare_player_ready_stream_for_handoff,
    _prepare_ready_stream_for_handoff,
    _reject_resolve_handle,
    _resolve_and_play_finish_or_stop,
    _resolve_and_play_nzbget_delegate,
    _resolve_and_play_ready_stream,
    _resolve_and_play_submit_and_poll,
    _resolve_finish_or_reject,
    _resolve_nzbget_delegate,
    _resolve_play_ready_stream,
    _resolve_submit_and_poll,
    _scrub_bookmark_for_nzbget,
)
from resources.lib.resolver_history import (  # noqa: E402,F401
    _abort_poll_before_fetch,
    _advance_no_video_retry,
    _advertised_size_bytes,
    _classify_completed_video,
    _discover_completed_video,
    _discovered_video_is_stub,
    _find_completed_video_stream_with_rechecks,
    _handle_completed_history,
    _handle_history_result,
    _handle_job_status,
    _handle_resolve_exception,
    _handle_webdav_error,
    _report_history_failed,
    _report_no_video_exhaustion,
    _status_dialog_message,
    _stub_min_size_floor,
)
from resources.lib.resolver_playback import (  # noqa: E402,F401
    _add_own_plugin_target_ids,
    _add_request_headers,
    _add_tmdb_helper_target_ids,
    _apply_proxy_mime,
    _apply_remux_proxy_mime,
    _arm_live_fallback_push,
    _bookmark_columns,
    _bookmark_resume_query,
    _build_play_url,
    _cache_bust_url,
    _captured_bookmark_resume_seconds,
    _clamp_int_setting,
    _clear_kodi_playback_state,
    _coerce_resume_seconds,
    _collect_kodi_playback_target_ids,
    _completed_stream_body_available,
    _completed_stream_head_length,
    _completed_stream_midfile_present,
    _get_stream_content_length_hint,
    _like_escape,
    _locate_kodi_video_db,
    _make_playable_listitem,
    _numeric_query_param_matches,
    _playback_fallback_sources_for_stream,
    _remember_resolved_stream_content_length_hint,
    _remember_stream_content_length_hint,
    _resolve_stage,
    _start_playback_state_cleanup,
    _stream_auth_header,
    _stream_content_length_hint_key,
    _tmdb_helper_url_matches_params,
    _url_path,
    _validate_stream_url,
    _video_mime_for_path,
    _wait_playback_state_cleanup,
)
from resources.lib.resolver_poll import (  # noqa: E402,F401
    _by_name_completed_after_submit,
    _by_name_terminal_history,
    _get_poll_settings,
    _history_status_is_terminal,
    _poll_active_queue_grace_seconds,
    _poll_clearly_active_grace_seconds,
    _poll_once,
    _poll_once_await_apis,
    _poll_wait_after_status,
    _queue_status_has_active_status,
    _queue_status_history_grace_seconds,
    _queue_status_is_clearly_active,
    _queue_status_is_late_active,
    _queue_status_is_nearly_complete,
    _storage_to_webdav_path,
    _wait_for_nearly_complete_history,
)
from resources.lib.resolver_pollloop import (  # noqa: E402,F401
    _cancel_job_on_shutdown,
    _job_status_is_dead,
    _mark_dead_on_failed_history,
    _mark_dead_on_terminal_job_status,
    _notify_primary_submitted,
    _poll_until_ready,
    _record_download_soft,
    _wait_between_polls,
)
from resources.lib.resolver_prepare import (  # noqa: E402,F401
    _claim_dialog_update_slot,
    _direct_playback_service_config,
    _monitor_abort_requested,
    _prepare_direct_playback,
    _prepare_direct_playback_with_service_config,
    _ready_direct_playback_prepare_state,
    _ready_direct_playback_service_config_state,
    _release_dialog_update_slot,
    _safe_dialog_update,
    _settings_getter_kwargs,
    _start_direct_playback_prepare,
    _start_direct_playback_service_config_lookup,
    _wait_direct_playback_prepare,
    _wait_direct_playback_service_config,
    _wait_for_abort_or_timeout,
)
from resources.lib.resolver_queueclear import (  # noqa: E402,F401
    _adoptable_copy_suppresses_clear,
    _clear_queue_on_submit_mode,
    _clear_queue_slots,
    _completed_copy_blocks_clear,
    _completed_copy_blocks_clear_result,
    _confirm_queue_clear,
    _maybe_clear_queue_before_submit,
    _probe_clearable_queue_slots,
    _queue_clear_prompt_message,
    _queue_slot_is_title,
)
from resources.lib.resolver_resume import (  # noqa: E402,F401
    _apply_resume_start_offset,
    _finish_direct_playback,
    _finish_player_playback,
    _migrate_legacy_resume,
    _play_direct,
    _play_via_proxy,
    _preserve_resume_on_cancel,
    _read_stored_resume,
    _resolve_resume_choice,
    _resume_params_with_title,
    _set_playback_monitor_properties,
    _show_cache_prompt_after_playback,
)
from resources.lib.resolver_submit import (  # noqa: E402,F401
    _adopt_queued_or_completed_job,
    _await_adoptable_probe_result,
    _drop_rejected_completed_match,
    _find_adoptable_job_during_submit,
    _get_submit_timeout_seconds,
    _job_nzo_id,
    _log_submit_attempt_failed,
    _report_all_submit_attempts_failed,
    _safe_probe_by_name,
    _start_probe_thread_or_run,
    _submit_nzb_with_retries,
    _submit_nzb_with_ui_pump,
    _submit_probe_interval,
    _submit_retry_backoff_aborted,
)
from resources.lib.resolver_submiterror import (  # noqa: E402,F401
    _adopt_after_submit_failure,
    _build_submit_error_ctx,
    _handle_submit_4xx,
    _handle_submit_attempt_error,
    _handle_submit_nontransient,
    _handle_submit_rejected,
    _handle_submit_timeout,
    _surface_terminal_submit_error,
    _terminal_submit_error_result,
)
