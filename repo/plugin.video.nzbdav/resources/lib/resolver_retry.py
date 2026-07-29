# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Sequential distinct-release retry orchestration."""

import resources.lib.resolver as _resolver


def _retry_attempts(nzb_url, title, candidates):
    attempts = [{"link": nzb_url, "title": title, "primary": True}]
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        link = candidate.get("link")
        candidate_title = candidate.get("title")
        if link and candidate_title:
            attempt = dict(candidate)
            attempt["link"] = link
            attempt["title"] = candidate_title
            attempt["primary"] = False
            attempts.append(attempt)
    return attempts


def _poll_with_release_retries(
    nzb_url,
    title,
    retry_candidates,
    dialog,
    poll_interval,
    download_timeout,
    poll_ctx,
):
    """Try a new release only after the preceding release is provably dead."""
    attempts = _retry_attempts(nzb_url, title, retry_candidates)
    for index, attempt in enumerate(attempts):
        if index:
            _resolver.xbmc.log(
                "NZB-DAV: Trying distinct release fallback {}/{}: '{}'".format(
                    index, len(attempts) - 1, attempt["title"]
                ),
                _resolver.xbmc.LOGINFO,
            )
            _resolver._safe_dialog_update(
                dialog,
                0,
                "Previous release was unavailable. Trying another posting...",
            )
        attempt_ctx = poll_ctx
        if index:
            attempt_ctx = poll_ctx._replace(
                completed_job_hint=None,
                completed_job_lookup_done=False,
                selected_indexer=attempt.get("indexer", ""),
                download_pubdate=attempt.get("pubdate"),
                download_size=attempt.get("size"),
            )
        stream = _resolver._poll_until_ready(
            attempt["link"],
            attempt["title"],
            dialog,
            poll_interval,
            download_timeout,
            poll_ctx=attempt_ctx,
        )
        if stream[0]:
            return stream
        dead = attempt_ctx.dead
        if dead is None or not dead.has_url(attempt["link"]):
            break
    return None, None
