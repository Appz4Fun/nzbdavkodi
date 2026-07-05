# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Provider-search execution + result identity/tagging helpers from ``router``.

These are the non-test-patched internals behind ``_search_all_providers`` and
``_tag_available`` (both of which stay in ``router`` because the suite imports /
patches them). Names that the suite patches via ``resources.lib.router`` —
``telemetry``, ``downloaded_pubdate_epochs`` — are
reached at call time through ``import resources.lib.router as _router`` so those
``@patch`` decorators keep resolving; everything else is imported normally.
"""

import time
from concurrent.futures import ThreadPoolExecutor

from resources.lib.http_util import pubdate_to_epoch
from resources.lib.hydra import _DEFAULT_HYDRA_URL
from resources.lib.nzbdav_api import completed_jobs_lookup_done

# Pre-read defaults for the provider-search settings snapshot
# (router._search_all_providers wraps every getter in _snapshot_settings_getter
# seeded from this map, so worker threads never touch Kodi settings).
# ``hydra_url`` seeds the schema default: the snapshot pre-reads every key, so
# a URL left at its displayed default (absent from the profile XML) would
# otherwise snapshot to "" and bypass the ``hydra._DEFAULT_HYDRA_URL`` mirror
# for the whole provider-search path.
_PROVIDER_SEARCH_SETTING_DEFAULTS = {
    "hydra_url": _DEFAULT_HYDRA_URL,
    "hydra_api_key": "",
    "prowlarr_host": "",
    "prowlarr_api_key": "",
    "prowlarr_indexer_ids": "",
    "max_results": "25",
}


def _build_provider_jobs(
    nzbhydra_enabled,
    prowlarr_enabled,
    direct_indexers_enabled,
    search_args,
    common_kwargs,
    provider_settings_getter,
):
    """Assemble the (key, label, func, args, kwargs) tuples for enabled providers."""
    provider_jobs = []

    if nzbhydra_enabled:
        from resources.lib.hydra import search_hydra

        kwargs = dict(common_kwargs, settings_getter=provider_settings_getter)
        provider_jobs.append(("hydra", "NZBHydra2", search_hydra, search_args, kwargs))

    if prowlarr_enabled:
        from resources.lib.prowlarr import search_prowlarr

        kwargs = dict(common_kwargs, settings_getter=provider_settings_getter)
        provider_jobs.append(
            ("prowlarr", "Prowlarr", search_prowlarr, search_args, kwargs)
        )

    if direct_indexers_enabled:
        from resources.lib.direct_indexers import (
            _read_max_results,
            get_configured_indexers,
            search_direct_indexers,
        )
        from resources.lib.search_planner import SearchQuery

        search_type, title = search_args
        query = SearchQuery(
            search_type=search_type,
            title=title,
            year=common_kwargs.get("year", ""),
            imdb=common_kwargs.get("imdb", ""),
            season=common_kwargs.get("season", ""),
            episode=common_kwargs.get("episode", ""),
            tvdb=common_kwargs.get("tvdb", ""),
        )
        kwargs = dict(
            indexers=get_configured_indexers(),
            max_results=_read_max_results(provider_settings_getter),
        )
        provider_jobs.append(
            (
                "direct indexers",
                "Direct indexer",
                search_direct_indexers,
                (query,),
                kwargs,
            )
        )

    return provider_jobs


def _run_one_provider(provider_key, _provider_label, search_func, args, kwargs):
    """Run a single provider search, emitting stage logs + timing telemetry."""
    import resources.lib.router as _router

    provider_started = time.monotonic()
    results = []
    error = None
    provider_failed = False
    _router._script_play_stage("{} search start".format(provider_key))
    try:
        results, error = search_func(*args, **kwargs)
        _router._script_play_stage(
            "{} search done count={} error={}".format(
                provider_key, len(results or []), bool(error)
            )
        )
        return results, error
    except Exception:
        provider_failed = True
        raise
    finally:
        _router.telemetry.log_timing(
            "provider_search",
            (time.monotonic() - provider_started) * 1000.0,
            provider=provider_key.replace(" ", "_"),
            count=len(results or []),
            error=provider_failed or bool(error),
        )


def _provider_error_message(provider_label, error):
    """Format a provider failure, redacting any secrets the exception leaked.

    urllib/provider exceptions routinely embed the full request URL (apikey
    and all) in ``str(error)``. router.py later logs this and surfaces it to
    the UI, so it must pass through the same ``redact_text`` scrub the
    connection tests use before any provider error escapes.
    """
    from resources.lib.http_util import redact_text

    return "{} search failed: {}".format(provider_label, redact_text(str(error)))


def _run_provider_jobs(provider_jobs):
    """Run provider jobs (serially for one, threaded for many).

    Returns a list of ``(provider_label, (results, error))`` outcomes; a job
    that raises is surfaced as an empty-results error outcome.
    """
    if len(provider_jobs) == 1:
        provider_key, provider_label, search_func, args, kwargs = provider_jobs[0]
        try:
            outcome = _run_one_provider(
                provider_key, provider_label, search_func, args, kwargs
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            outcome = ([], _provider_error_message(provider_label, error))
        return [(provider_label, outcome)]

    with ThreadPoolExecutor(max_workers=len(provider_jobs)) as executor:
        futures = [
            (
                provider_label,
                executor.submit(
                    _run_one_provider,
                    provider_key,
                    provider_label,
                    search_func,
                    args,
                    kwargs,
                ),
            )
            for (
                provider_key,
                provider_label,
                search_func,
                args,
                kwargs,
            ) in provider_jobs
        ]
        provider_outcomes = []
        for provider_label, future in futures:
            try:
                provider_outcomes.append((provider_label, future.result()))
            except Exception as error:  # pylint: disable=broad-exception-caught
                provider_outcomes.append(
                    (
                        provider_label,
                        ([], _provider_error_message(provider_label, error)),
                    )
                )
    return provider_outcomes


def _dedupe_results_by_link(all_results):
    """Drop linkless results and collapse duplicates that share a ``link``."""
    seen_links = set()
    deduped = []
    for result in all_results:
        key = result.get("link", "")
        if not key:
            # No link → no way to play this result. Dropping is better
            # than presenting a dead entry in the selection dialog.
            continue
        if key in seen_links:
            continue
        seen_links.add(key)
        deduped.append(result)
    return deduped


# How close an indexer result's advertised size must be to a completed nzbdav
# download's ``bytes`` for them to be treated as the SAME upload. nzbdav history
# is keyed by NAME only, so a name match alone collapses distinct uploads that
# merely share a filename (a different release/resolution, or a repost at a
# different retention). The tolerance is generous enough to absorb the gap
# between an indexer's advertised NZB size and the actually-downloaded bytes
# (yEnc/par2/rar overhead) so a genuine cache hit is never hidden, while still
# separating clearly-different files (e.g. a 1080p vs a 2160p sharing a generic
# filename). True per-upload identity is the article list, but that is not
# available at picker time without fetching every NZB.
_COMPLETED_SIZE_MATCH_TOLERANCE = 0.15


def _result_size_bytes(result):
    """Best-effort parse of an indexer result's advertised size in bytes."""
    value = result.get("size") if isinstance(result, dict) else None
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not value:
            return 0
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            return 0
    return 0


def _completed_job_matches_result(result, completed_job):
    """Return whether a name-matched completed job plausibly IS this result's
    upload, disambiguating same-filename collisions by size.

    Fails OPEN when either size is unknown (keep the prior name-only behavior
    rather than hide a real cache hit). A clearly-different size means a
    different file — do not mark it ``DL`` or reuse its cached stream.
    """
    result_size = _result_size_bytes(result)
    try:
        job_bytes = int(completed_job.get("bytes") or 0)
    except (TypeError, ValueError):
        job_bytes = 0
    if result_size <= 0 or job_bytes <= 0:
        return True
    return (
        abs(result_size - job_bytes)
        <= max(result_size, job_bytes) * _COMPLETED_SIZE_MATCH_TOLERANCE
    )


# A same-name release posted on a *different day* is a different upload, even
# when the size matches (a repost / re-rip). nzbdav history records only the
# download time, never the Usenet post date, so we compare the result's
# pubdate against the post-dates we captured at submit time (download_ledger).
# The tolerance absorbs sub-hour indexer/TZ formatting jitter for the SAME
# post while still separating day-apart reposts cleanly.
_PUBDATE_MATCH_TOLERANCE_SECONDS = 3600


def _result_pubdate_consistent_with_downloads(result):
    """Return whether a name+size-matched result's pubdate is consistent with
    what we actually downloaded under that name.

    Fails OPEN (returns True) when we have no recorded pubdate for the name
    (e.g. downloaded before this feature, or via an external invocation) or
    the result advertises no parseable pubdate -- we'd rather keep the prior
    name+size behavior than hide a real cache hit. Returns False only when we
    DO have recorded pubdates and the result's pubdate matches none of them,
    i.e. it is a same-name repost posted at a different time.
    """
    import resources.lib.router as _router

    if not isinstance(result, dict):
        return True
    recorded = _router.downloaded_pubdate_epochs(result.get("title"))
    if not recorded:
        return True
    result_epoch = pubdate_to_epoch(result.get("pubdate"))
    if result_epoch is None:
        return True
    return any(
        abs(result_epoch - epoch) <= _PUBDATE_MATCH_TOLERANCE_SECONDS
        for epoch in recorded
    )


class _LookupDoneJobs(dict):
    """Empty mapping that still reports a finished completed-history lookup.

    ``_completed_lookup_was_done`` keys on the ``_lookup_done`` attribute, so
    returning this from a tagging path tells selection not to re-query history.
    """

    _lookup_done = True


def _nzbget_mode_enabled(settings_getter=None):
    """Return whether the NZBGet backend toggle is on (resolver's reader)."""
    from resources.lib.resolver import _nzbget_enabled

    return _nzbget_enabled(settings_getter)


def _tag_available_nzbget(results, settings_getter=None):
    """Mark results already completed in NZBGet history (NZBGet-mode "DL").

    A release still in NZBGet's history as SUCCESS already has its finished
    files on the SMB share, so the picker shows the same "DL" chip the nzbdav
    cached-stream tag uses, gated by the same name+size(+recorded pubdate)
    identity checks. The matched row is attached as ``_nzbget_completed_job``
    so the NZBGet resolver plays the row's completed files directly instead
    of re-submitting — NZBGet's duplicate check (DupeCheck=yes by default)
    would dupe-delete a re-submission of a SUCCESS item and fail the resolve.
    Deliberately does NOT attach ``_completed_job``: that hint is the nzbdav
    cached-stream reuse contract. Always returns a lookup-done mapping — even
    when the history RPC fails — because the per-selection nzbdav history
    fallback it would otherwise trigger is meaningless on the NZBGet path.
    """
    from resources.lib import nzbget_api

    completed = nzbget_api.completed_history(settings_getter=settings_getter)
    for result in results:
        completed_job = completed.get(result.get("title"))
        if (
            completed_job
            and _completed_job_matches_result(result, completed_job)
            and _result_pubdate_consistent_with_downloads(result)
        ):
            result["_available"] = True
            result["_nzbget_completed_job"] = completed_job
    if completed_jobs_lookup_done(completed):
        return completed
    return _LookupDoneJobs()


def _completed_lookup_was_done(completed_jobs):
    """Return whether picker-time completed-history lookup can be reused."""
    return (isinstance(completed_jobs, dict) and bool(completed_jobs)) or (
        completed_jobs_lookup_done(completed_jobs)
    )


def _hydra_duplicate_lookup_enabled(selected, settings_getter=None):
    """Return whether the selected row should use Hydra's duplicate API."""
    if not isinstance(selected, dict):
        return False
    if settings_getter is not None:
        return _hydra_lookup_enabled_by_settings(settings_getter)
    return _hydra_lookup_enabled_by_selection(selected)


def _hydra_lookup_enabled_by_settings(settings_getter):
    """Hydra-duplicate gate when an explicit settings getter is available.

    ``hydra_url`` falls back to its settings.xml schema default: raw-XML
    getters (``_get_script_setting``, the dupe-loader getters) return the
    passed fallback for a setting left at its displayed default, while the
    live Kodi layer returns the schema default -- without the mirror a
    default-URL Hydra setup silently fails this gate off the live path.
    """
    from resources.lib.hydra import _DEFAULT_HYDRA_URL

    enabled = settings_getter("nzbhydra_enabled", "false")
    if str(enabled).lower() != "true":
        return False
    hydra_url = settings_getter("hydra_url", _DEFAULT_HYDRA_URL)
    return bool(str(hydra_url or "").strip())


def _hydra_lookup_enabled_by_selection(selected):
    """Hydra-duplicate gate inferred from the selected row's own fields."""
    if "indexer" not in selected and "link" not in selected:
        return False
    indexer = str(selected.get("indexer", "") or "").lower()
    if "hydra" in indexer:
        return True
    link = str(selected.get("link", "") or "").lower()
    return "hydra" in link and isinstance(selected.get("_meta"), dict)
