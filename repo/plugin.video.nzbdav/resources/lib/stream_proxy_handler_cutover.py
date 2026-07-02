# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Live fallback cutover: source activation, demotion, and resolved-source selection.

Stage-2 mixin split of ``stream_proxy._StreamHandler``. These methods were
moved verbatim; every reference to a ``stream_proxy`` module-level name is
reached at call time via ``_sp.<name>`` so test monkeypatches on
``resources.lib.stream_proxy`` keep resolving. MRO composes them back onto
``_StreamHandler``; they keep using ``self`` for handler state and methods.
"""

from typing import NamedTuple

import resources.lib.stream_proxy as _sp  # noqa: E402


class FirstCandidateProbe(NamedTuple):
    """Pre-probed first-candidate results forwarded into fallback selection."""

    stream_url: str = None
    failed: bool = None
    nzo_id: str = None
    standby: dict = None


class _FallbackCutoverMixin:  # pylint: disable=too-few-public-methods
    """Live fallback cutover: activation, demotion, and resolved-source select."""

    @staticmethod
    def _bump_awaiting_no_progress(ctx, result, current_count, current_byte=None):
        """Advance the session-persistent no-progress AWAITING_DOWNLOAD count.

        Only a clean download-high-water short read (AWAITING_DOWNLOAD) that
        delivered nothing advances the streak; any other result resets it to 0
        (keeping the streak strictly consecutive AWAITING reads). The count
        lives in ctx so it survives Kodi's Connection: close reconnects (a dead
        region reads as a clean short read on every fresh GET). See F-route.

        The streak is also scoped to the failing byte offset: a single session
        issues many ranges (startup tail probe, reconnects, seeks), and a
        no-progress AWAITING at one offset must not advance a streak begun at an
        unrelated offset. When current_byte differs from the offset that last
        advanced the streak, the count resets first so only CONSECUTIVE
        no-progress reads of the SAME stuck region escalate to failover.
        """
        if current_byte is not None:
            last_byte = ctx.get("_awaiting_download_no_progress_byte")
            if last_byte is not None and last_byte != current_byte:
                current_count = 0
        if result == _sp._UPSTREAM_RANGE_SHORT_READ_AWAITING_DOWNLOAD:
            current_count += 1
            ctx["_awaiting_download_no_progress"] = current_count
            if current_byte is not None:
                ctx["_awaiting_download_no_progress_byte"] = current_byte
            return current_count
        ctx["_awaiting_download_no_progress"] = 0
        ctx.pop("_awaiting_download_no_progress_byte", None)
        return 0

    @staticmethod
    def _demote_active_source(ctx, fallback):
        """Re-add the currently-active (pre-cutover) source as a trusted backup.

        The original was just serving these exact bytes, so it is marked
        validated and demoted; skipped when it is already in the pool or is
        the source we are cutting over to.
        """
        demoted_url = ctx.get("remote_url")
        if not demoted_url or demoted_url == fallback.get("stream_url"):
            return
        sources = ctx.setdefault("fallback_sources", [])
        if any(src.get("stream_url") == demoted_url for src in sources):
            return
        demoted_auth = ctx.get("auth_header")
        sources.append(
            {
                "stream_url": demoted_url,
                "stream_headers": (
                    {"Authorization": demoted_auth} if demoted_auth else {}
                ),
                "content_length": ctx.get("content_length", 0) or 0,
                "demoted": True,
                "validated": True,
            }
        )

    def _activate_fallback_source(self, ctx, fallback, current, stuck_awaiting=False):
        """Point the session at a selected fallback source for live cutover.

        Shared by the recoverable cutover path and the F-route stuck-
        AWAITING_DOWNLOAD failover so both perform identical bookkeeping
        (URL/auth swap, watchdog window reset, switch counters, index).
        """
        # Demote the currently-active source to a last-resort fallback BEFORE we
        # repoint at the new one. The original primary is never its own backup at
        # session start; it only re-enters the pool here, after a real cutover to
        # a different source. It carries a resolved stream_url, so no nzo
        # re-resolution is needed; the byte-identity gate still guards serving.
        #
        # Mark it validated: this source was just actively serving these exact
        # bytes, so it is content-identical by construction. Without the flag the
        # prevalidation warmer would treat it as a fresh source and re-fingerprint
        # it (an extra upstream open of a peer we already trust); validated=True
        # skips that while leaving it selectable as a last resort.
        self._demote_active_source(ctx, fallback)
        ctx["remote_url"] = fallback["stream_url"]
        ctx["auth_header"] = (fallback.get("stream_headers") or {}).get("Authorization")
        ctx.pop("upstream_down_notified", None)
        ctx.pop("upstream_unreachable_error", None)
        # Reset throughput watchdog window so the new upstream gets a fresh
        # stall window — otherwise the prior peer's wedge-induced low B/s would
        # carry over and trip the watchdog mid-handshake against the healthy
        # peer.
        ctx["passthrough_window_t0"] = _sp.time.monotonic()
        ctx["passthrough_window_bytes"] = 0
        # The new source gets a fresh AWAITING_DOWNLOAD streak; the prior
        # (dead) primary's stuck count must not carry over and prematurely
        # escalate the healthy peer.
        ctx["_awaiting_download_no_progress"] = 0
        ctx.pop("_awaiting_download_no_progress_byte", None)
        ctx["fallback_switch_count"] = int(ctx.get("fallback_switch_count", 0) or 0) + 1
        try:
            ctx["fallback_active_index"] = ctx.get("fallback_sources", []).index(
                fallback
            )
        except ValueError:
            ctx["fallback_active_index"] = -1
        _sp.xbmc.log(
            self._cutover_log_message(stuck_awaiting).format(
                current,
                fallback.get("nzo_id", ""),
                ctx["fallback_switch_count"],
            ),
            _sp.xbmc.LOGWARNING,
        )

    @staticmethod
    def _cutover_log_message(stuck_awaiting):
        """Log-message template for a live fallback cutover."""
        if stuck_awaiting:
            return (
                "NZB-DAV: Primary stuck on no-progress AWAITING_DOWNLOAD at "
                "byte {}; failing over to fallback nzo_id={} (switch_count={})"
            )
        return (
            "NZB-DAV: Switched pass-through source at byte {} to fallback "
            "nzo_id={} (switch_count={})"
        )

    @staticmethod
    def _apply_fallback_match_result(source, match_result):
        """Apply a tri-state match result to a candidate source (F8-dropout).

        Returns True when the source is usable now (MATCH). On a definitive
        MISMATCH the source is failed permanently. On a transient
        INCONCLUSIVE the source is left eligible — its
        ``transient_miss_count`` is bumped and only after exceeding
        ``_FALLBACK_SOURCE_TRANSIENT_MISS_MAX`` is it abandoned, so a peer
        that is briefly a few bytes short (or hiccups one probe) is
        reconsidered on the next cutover instead of being killed for the
        whole session. Any definitive answer resets the transient counter.
        """
        if match_result is _sp._FALLBACK_INCONCLUSIVE:
            misses = int(source.get("transient_miss_count", 0) or 0) + 1
            source["transient_miss_count"] = misses
            if misses > _sp._FALLBACK_SOURCE_TRANSIENT_MISS_MAX:
                # Stuck INCONCLUSIVE forever — abandon so the queue can't
                # reconsider it on every cutover indefinitely.
                source["failed"] = True
            return False
        # A definitive answer arrived; clear any prior transient streak.
        if source.get("transient_miss_count"):
            source["transient_miss_count"] = 0
        if match_result:
            return True
        # Definitive MISMATCH (provably a different file).
        source["failed"] = True
        return False

    def _select_live_fallback_source(self, ctx, failed_byte, range_end):
        """Return a validated fallback source for the failed byte range."""
        fallback_sources = ctx.get("fallback_sources") or []
        if not fallback_sources:
            return None
        expected_length = self._fallback_expected_content_length(ctx)
        if expected_length <= 0:
            return None
        ctx["_fallback_expected_content_length"] = expected_length
        (
            first_selectable_index,
            first_selectable_stream_url,
            first_selectable_nzo_id,
        ) = self._find_first_selectable_fallback(fallback_sources)
        if first_selectable_index < 0:
            return None
        first_standby = {}
        source = self._select_resolved_fallback_source(
            ctx,
            failed_byte,
            range_end,
            start_index=first_selectable_index,
            first=FirstCandidateProbe(
                stream_url=first_selectable_stream_url,
                failed=False,
                nzo_id=first_selectable_nzo_id,
                standby=first_standby,
            ),
            expected_length=expected_length,
        )
        if source:
            return source
        self._seed_first_standby(
            first_standby,
            first_selectable_index,
            first_selectable_stream_url,
            first_selectable_nzo_id,
        )
        if not first_standby:
            return None
        return self._retry_standby_fallback_sources(
            ctx, failed_byte, range_end, fallback_sources, first_standby
        )

    @staticmethod
    def _seed_first_standby(first_standby, index, stream_url, nzo_id):
        """Seed first_standby from the first selectable source when still empty."""
        if not first_standby and nzo_id:
            first_standby.update(
                {"index": index, "stream_url": stream_url, "nzo_id": nzo_id}
            )

    def _retry_standby_fallback_sources(
        self, ctx, failed_byte, range_end, fallback_sources, first_standby
    ):
        """Refresh + match standby sources starting at the first standby index."""
        first_standby_index = first_standby["index"]
        for index in range(first_standby_index, len(fallback_sources)):
            source = fallback_sources[index]
            resolved = self._resolve_standby_source_fields(
                source, first_standby, index == first_standby_index
            )
            if resolved is None:
                continue
            stream_url, nzo_id, source_failed = resolved
            matched = self._try_standby_fallback_source(
                ctx,
                source,
                failed_byte,
                range_end,
                nzo_id=nzo_id,
                known_stream_url=stream_url,
                known_failed=source_failed,
            )
            if matched is not None:
                return matched
        return None

    @staticmethod
    def _resolve_standby_source_fields(source, first_standby, is_first):
        """Return (stream_url, nzo_id, source_failed) or None when skippable.

        A source is skipped when it has already failed, when it already
        carries a resolved stream URL, or when it lacks an nzo_id to refresh.
        """
        source_failed = False if is_first else source.get("failed")
        if source_failed:
            return None
        stream_url = (
            first_standby["stream_url"] if is_first else source.get("stream_url")
        )
        if stream_url:
            return None
        nzo_id = first_standby["nzo_id"] if is_first else source.get("nzo_id")
        if not nzo_id:
            return None
        return stream_url, nzo_id, source_failed

    def _try_standby_fallback_source(
        self,
        ctx,
        source,
        failed_byte,
        range_end,
        nzo_id,
        known_stream_url,
        known_failed,
    ):
        """Refresh + fingerprint-match one standby source under selector hints."""
        hint_snapshot = self._snapshot_fallback_hint_keys(
            ctx,
            (
                "_fallback_source_content_length_hint",
                "_fallback_source_auth_hint",
                _sp._FALLBACK_SOURCE_STREAM_URL_HINT_KEY,
            ),
        )
        try:
            if not self._refresh_standby_fallback_source(
                ctx,
                source,
                nzo_id=nzo_id,
                known_stream_url=known_stream_url,
                known_failed=known_failed,
            ):
                return None
            match_result = self._fallback_source_matches(
                ctx, source, failed_byte, range_end
            )
            if not self._apply_fallback_match_result(source, match_result):
                return None
            return source
        finally:
            self._restore_fallback_hint_keys(ctx, hint_snapshot)

    @staticmethod
    def _find_first_selectable_fallback(fallback_sources):
        """Return (index, stream_url, nzo_id) of the first usable fallback."""
        for index, source in enumerate(fallback_sources):
            if source.get("failed"):
                continue
            stream_url = source.get("stream_url")
            nzo_id = "" if stream_url else source.get("nzo_id")
            if stream_url or nzo_id:
                return index, stream_url or "", nzo_id or ""
        return -1, "", ""

    def _select_resolved_fallback_source(
        self,
        ctx,
        failed_byte,
        range_end,
        start_index=0,
        first=None,
        expected_length=None,
    ):
        """Return a validated already-resolved fallback source, if any.

        ``first`` is a :class:`FirstCandidateProbe` carrying the pre-probed
        stream URL / failed flag / nzo_id / standby dict for the first
        selectable candidate (all ``None`` when not supplied).
        """
        if first is None:
            first = FirstCandidateProbe()
        sources = ctx.get("fallback_sources", [])
        start_index = max(0, start_index)
        if expected_length is None:
            expected_length = self._coerce_ctx_content_length(ctx)
        selection = {
            "start_index": start_index,
            "failed_byte": failed_byte,
            "range_end": range_end,
            "expected_length": expected_length,
            "first_stream_url": first.stream_url,
            "first_failed": first.failed,
            "first_nzo_id": first.nzo_id,
            "first_standby": first.standby,
        }
        state = {
            "primary_url": _sp._FALLBACK_SOURCE_STATE_NOT_PROVIDED,
            "primary_auth_for_url": _sp._AUTH_HEADER_NOT_PROVIDED,
        }
        for index in range(start_index, len(sources)):
            matched = self._evaluate_resolved_source(
                ctx, sources[index], index, selection, state
            )
            if matched is not None:
                return matched
        return None

    def _evaluate_resolved_source(self, ctx, source, index, selection, state):
        """Evaluate one resolved candidate; return the source when it matches.

        ``state`` carries the lazily-resolved primary URL/auth across the
        selector loop; ``selection`` holds the per-call gate inputs.
        """
        is_first = index == selection["start_index"]
        stream_url = self._resolved_candidate_stream_url(
            source,
            index,
            is_first,
            selection["first_failed"],
            selection["first_stream_url"],
            selection["first_nzo_id"],
            selection["first_standby"],
        )
        if not stream_url:
            return None
        if state["primary_url"] is _sp._FALLBACK_SOURCE_STATE_NOT_PROVIDED:
            state["primary_url"] = ctx.get("remote_url")
        (
            is_primary_self,
            source_auth,
            primary_auth,
            state["primary_auth_for_url"],
        ) = self._resolved_primary_self_match(
            ctx,
            source,
            stream_url,
            state["primary_url"],
            state["primary_auth_for_url"],
        )
        if is_primary_self:
            source["failed"] = True
            return None
        identity = self._resolved_identity(
            stream_url, source_auth, state["primary_url"], primary_auth
        )
        if self._resolved_source_matches(
            ctx,
            source,
            selection["failed_byte"],
            selection["range_end"],
            selection["expected_length"],
            identity,
        ):
            return source
        return None

    @staticmethod
    def _resolved_identity(stream_url, source_auth, primary_url, primary_auth):
        """Bundle selector-hint identity values for a resolved candidate."""
        return {
            "stream_url": stream_url,
            "source_auth": source_auth,
            "primary_url": primary_url,
            "primary_auth": primary_auth,
        }

    def _resolved_candidate_stream_url(
        self,
        source,
        index,
        is_first,
        first_failed,
        first_stream_url,
        first_nzo_id,
        first_standby,
    ):
        """Return a usable resolved stream URL, or "" to skip this source.

        Captures the first unresolved standby for later refresh, matching the
        original inline skip semantics (failed sources and empty URLs).
        """
        source_failed = self._resolved_override(
            is_first, first_failed, source, "failed"
        )
        if source_failed:
            return ""
        stream_url = self._resolved_override(
            is_first, first_stream_url, source, "stream_url"
        )
        if not stream_url:
            first_nzo_id_for_index = self._resolved_override(
                is_first, first_nzo_id, None, None
            )
            self._capture_first_standby(
                source, index, first_standby, first_nzo_id_for_index
            )
            return ""
        return stream_url

    def _resolved_source_matches(
        self, ctx, source, failed_byte, range_end, expected_length, identity
    ):
        """Apply the length gate + fingerprint match for one resolved source.

        ``identity`` carries ``stream_url``, ``source_auth``, ``primary_url``
        and ``primary_auth`` for the selector hint snapshot.
        """
        if expected_length > 0:
            source_length = self._coerce_source_length(source)
            if source_length != expected_length:
                source["failed"] = True
                return False
        hint_values = {
            "stream_url": identity["stream_url"],
            "source_length": source_length,
            "source_auth": identity["source_auth"],
            "primary_url": identity["primary_url"],
            "primary_auth": identity["primary_auth"],
        }
        matches = self._match_resolved_source_with_hints(
            ctx, source, failed_byte, range_end, hint_values
        )
        return self._apply_fallback_match_result(source, matches)

    @staticmethod
    def _resolved_override(is_first, override_value, source, key):
        """First-index override: use override_value only when first and set.

        Falls back to ``source.get(key)`` lazily (only when the override is
        not used) so the source dict read happens exactly when the original
        inline expression performed it. ``key`` of ``None`` yields ``None``.
        """
        if is_first and override_value is not None:
            return override_value
        if key is None:
            return None
        return source.get(key)

    @staticmethod
    def _resolved_primary_self_match(
        ctx, source, stream_url, primary_url, primary_auth_for_url
    ):
        """Detect a source that IS the primary; return (self, auths, cache)."""
        source_auth = _sp._AUTH_HEADER_NOT_PROVIDED
        primary_auth = _sp._AUTH_HEADER_NOT_PROVIDED
        is_primary_self = False
        if stream_url == primary_url:
            source_auth = (source.get("stream_headers") or {}).get("Authorization")
            if primary_auth_for_url is _sp._AUTH_HEADER_NOT_PROVIDED:
                primary_auth_for_url = ctx.get("auth_header")
            primary_auth = primary_auth_for_url
            is_primary_self = source_auth == primary_auth
        return is_primary_self, source_auth, primary_auth, primary_auth_for_url
