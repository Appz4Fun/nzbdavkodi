# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""PROPFIND scan/recurse internals for WebDAV video discovery.

Split out of ``webdav.py``: parses a folder's PROPFIND listing, ranks the
candidate videos at the current level, and decides between the current-level
best, a sibling subfolder, or recursing deeper. Match scoring lives in
``webdav_match``; the size-hint store, settings, auth headers, ``urlopen`` and
the ``find_video_file`` recursion entry stay in ``webdav`` (all test-patched).

To avoid an import cycle this module imports ``webdav`` LAZILY inside the few
functions that call those test-patched names, so ``@patch("resources.lib.webdav
.find_video_file")`` (etc.) still resolves at call time. The lazy ``webdav``
import is function-local (never executed at module load), so there is no real
runtime cycle; pylint's static graph still pairs it with ``webdav``'s top-level
re-export edge, so cyclic-import is suppressed module-wide here.
"""

# pylint: disable=cyclic-import

import threading
from queue import Queue
from urllib.parse import quote
from urllib.request import Request

import xbmc

from resources.lib import webdav_match

_WEBDAV_SUBDIR_SCAN_WORKERS = 4
_VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")
_DAV_NS = {"D": "DAV:"}


def _find_video_file_in_subdirs(
    subdirs,
    depth,
    visited,
    settings,
    hint_tokens=None,
    hint_episode_tags=None,
    min_video_size=0,
):
    """Probe sibling WebDAV subfolders and return the best video found.

    When a requested title/episode hint is available, a sibling whose video
    name matches the hint (especially the requested SxxExx episode) is preferred
    over the largest video — so a multi-episode pack returns the requested
    episode instead of whichever sibling happens to be biggest. With no hint
    (or no hint match) the historical "largest video wins" behavior is kept.

    Every sibling is scanned (no early-exit on the first match) so a polluted
    release folder can't win by ordering. Among equally-scored hint matches, or
    when no hint matches, ties break toward the larger then earlier-listed
    sibling so behavior stays stable.
    """
    if not subdirs:
        return None
    hint_tokens = hint_tokens or frozenset()
    hint_episode_tags = hint_episode_tags or frozenset()

    pending = list(subdirs)
    scan_hints = (hint_tokens, hint_episode_tags, min_video_size)
    result_queue = _launch_subdir_scan(pending, depth, visited, settings, scan_hints)

    have_hint = bool(hint_tokens or hint_episode_tags)
    return _best_sibling_from_queue(
        result_queue,
        len(pending),
        have_hint,
        hint_tokens,
        hint_episode_tags,
        min_video_size,
    )


def _launch_subdir_scan(pending, depth, visited, settings, scan_hints):
    """Start the sibling-scan worker pool and return its result queue."""
    workers = max(1, min(_WEBDAV_SUBDIR_SCAN_WORKERS, len(pending)))
    result_queue = Queue()
    worker_args = (
        pending,
        [0],
        threading.Lock(),
        result_queue,
        depth,
        visited,
        settings,
        scan_hints,
    )
    for _index in range(workers):
        thread = threading.Thread(
            target=_scan_subdir_worker, args=worker_args, daemon=True
        )
        thread.start()
    return result_queue


def _best_sibling_from_queue(
    result_queue, expected, have_hint, hint_tokens, hint_episode_tags, min_video_size
):
    """Drain ``expected`` worker results and return the highest-ranked video path.

    Ties break toward the larger then earlier-listed sibling via
    :func:`_sibling_rank_key` (negative index), so collection order is stable.
    """
    best_path = None
    best_key = None
    for _ in range(expected):
        index, result = result_queue.get()
        if not result:
            continue
        key = _sibling_rank_key(
            result,
            index,
            have_hint,
            hint_tokens,
            hint_episode_tags,
            min_video_size,
        )
        if best_key is None or key > best_key:
            best_path = result
            best_key = key
    return best_path


def _scan_subdir_worker(
    pending, next_index, index_lock, result_queue, depth, visited, settings, scan_hints
):
    """Worker loop: pull the next pending subdir, scan it, queue ``(index, result)``.

    ``scan_hints`` is the ``(hint_tokens, hint_episode_tags, min_video_size)``
    tuple shared by every worker. The index counter is guarded by ``index_lock``
    so each subdir is scanned exactly once across the worker pool.
    """
    import resources.lib.webdav as _webdav

    hint_tokens, hint_episode_tags, min_video_size = scan_hints
    while True:
        with index_lock:
            if next_index[0] >= len(pending):
                return
            index = next_index[0]
            subdir = pending[index]
            next_index[0] += 1
        xbmc.log(
            "NZB-DAV: No video at depth {}, checking subfolder: {}".format(
                depth, subdir
            ),
            xbmc.LOGDEBUG,
        )
        try:
            result = _webdav.find_video_file(
                subdir,
                depth + 1,
                visited,
                True,
                settings,
                title_hint_tokens=hint_tokens,
                title_hint_episode_tags=hint_episode_tags,
                min_video_size=min_video_size,
            )
        except Exception as e:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: Error scanning WebDAV subfolder in parallel: "
                "{} ({})".format(e, type(e).__name__),
                xbmc.LOGWARNING,
            )
            result = None
        result_queue.put((index, result))


def _sibling_rank_key(
    result, index, have_hint, hint_tokens, hint_episode_tags, min_video_size
):
    """Build the ranking key for a sibling-subfolder scan result.

    Rank by (not-wrong-episode, above-floor, episode identity, size, token
    overlap) and break ties toward the earlier sibling (negative index sorts a
    smaller index higher). The above-floor dimension mirrors the current-level
    key so a child returning a below-floor requested-episode stub never beats a
    sibling's above-floor real file on episode identity alone -- otherwise the
    stub wins and the resolver re-rejects it every poll (Codex #340). "not a
    wrong episode" sits ABOVE the floor flag so a wrong-episode above-floor
    sibling can't be promoted over the requested stub either. Episode match
    stays primary among same-class candidates; size outranks loose token
    overlap. With no hint and no floor every flag is constant, so this reduces
    to the historical largest-wins rule.
    """
    import resources.lib.webdav as _webdav

    size = _webdav.get_video_file_size_hint(result)
    if have_hint:
        ep_score, tok_score = webdav_match._title_hint_match_score(
            result, hint_tokens, hint_episode_tags
        )
    else:
        ep_score, tok_score = 0, 0
    above_floor = size <= 0 or size >= min_video_size
    return (ep_score >= 0, above_floor, ep_score, size, tok_score, -index)


def _resolve_hint_sets(title_hint, title_hint_tokens, title_hint_episode_tags):
    """Resolve the (tokens, episode-tags) hint sets for this discovery.

    Parse the title hint once at the top of a discovery; recursive calls
    receive the already-parsed token/episode-tag sets so the cost is paid once,
    not per folder level.
    """
    if title_hint_tokens is None and title_hint_episode_tags is None:
        return (
            webdav_match._hint_tokens(title_hint),
            webdav_match._episode_tags(title_hint),
        )
    return (
        title_hint_tokens or frozenset(),
        title_hint_episode_tags or frozenset(),
    )


def _build_propfind_request(folder_path, _already_encoded, settings):
    """Build the PROPFIND Request and return (request, url)."""
    import resources.lib.webdav as _webdav

    base = settings["webdav_url"] or settings["nzbdav_url"]
    # Recursive calls pass hrefs that the PROPFIND response already
    # URL-encoded for us (e.g. "My%20Show"). Re-running quote() on that
    # would turn ``%`` into ``%25``, 404'ing every subdirectory probe.
    # Top-level callers pass a raw path which needs encoding.
    if _already_encoded:
        encoded_path = folder_path
    else:
        encoded_path = quote(folder_path, safe="/")
    url = "{}/{}".format(base.rstrip("/"), encoded_path.lstrip("/"))
    if not url.endswith("/"):
        url += "/"

    req = Request(url, method="PROPFIND")
    req.add_header("Depth", "1")
    for header, value in _webdav._build_auth_headers(
        settings["username"], settings["password"]
    ).items():
        req.add_header(header, value)
    return req, url


def _parse_propfind_xml(body):
    """Parse a PROPFIND XML body with external entities disabled (XXE-safe)."""
    # nosemgrep
    import xml.etree.ElementTree as ET  # nosec B405 — parsing trusted WebDAV server response

    # Python's stdlib XMLParser doesn't accept resolve_entities as a kwarg, but
    # calling expat to disable external DTD loading has the same effect for XXE
    # prevention. Use the expat target builder so a hostile WebDAV server can't
    # coerce us into reading local files via an external entity reference.
    # nosemgrep
    _xml_parser = ET.XMLParser()  # nosec B314 — entities disabled below
    try:
        _xml_parser.parser.DefaultHandler = lambda _d: None
        _xml_parser.parser.ExternalEntityRefHandler = lambda *_: False
    except AttributeError:  # pragma: no cover — non-expat parser backend
        pass
    # nosemgrep
    return ET.fromstring(
        body, parser=_xml_parser
    )  # nosec B314 — trusted WebDAV server response; entities disabled above


def _extract_href_path(href_text, base_host):
    """Return the path portion of a PROPFIND href, or None if unusable.

    Handles cross-host hrefs. nzbdav legitimately returns its INTERNAL hostname
    in PROPFIND hrefs (e.g. localhost:8080) while we address it at the
    configured public endpoint (e.g. 192.168.1.93:3000). Trust only the PATH
    portion of the href -- all follow-up requests hit the configured WebDAV
    host anyway, so an attacker-controlled href host cannot redirect us
    off-server. Previously we rejected the entire href on host mismatch, which
    broke real users with reverse-proxied nzbdav setups ("Completed but no
    video found").
    """
    from urllib.parse import urlparse

    try:
        parsed_href_obj = urlparse(href_text)
        if href_text.startswith("//"):
            if parsed_href_obj.netloc != base_host:
                xbmc.log(
                    "NZB-DAV: cross-host href '{}' — using path "
                    "portion only".format(href_text),
                    xbmc.LOGDEBUG,
                )
            return parsed_href_obj.path
        if parsed_href_obj.scheme:
            if parsed_href_obj.netloc != base_host:
                xbmc.log(
                    "NZB-DAV: cross-origin href '{}' — using path "
                    "portion only".format(href_text),
                    xbmc.LOGDEBUG,
                )
            return parsed_href_obj.path
        return href_text
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Skipping malformed href '{}': {}".format(href_text, e),
            xbmc.LOGWARNING,
        )
        return None


def _collect_subdir(href_path, request_path, subdirs):
    """Append a non-hidden child collection to subdirs (skip self / dot dirs)."""
    # Skip the folder itself (href matches our request URL)
    child = href_path.rstrip("/")
    if child == request_path:
        return
    # Skip hidden (dot-prefixed) subfolders. nzbdav release folders sometimes
    # get polluted with a leading-dot child holding a different (often wrong,
    # smaller) movie — e.g. a '.and_justice_for_all...1080p...' folder
    # hijacking a 2160p release. Leading dots are not URL-encoded, so the
    # encoded path segment still starts with ".".
    segment = child.rsplit("/", 1)[-1]
    if segment.startswith("."):
        xbmc.log(
            "NZB-DAV: Skipping hidden WebDAV subfolder '{}'".format(child),
            xbmc.LOGDEBUG,
        )
    else:
        subdirs.append(child + "/")


def _parse_content_length(response, href_path):
    """Return the integer getcontentlength for a response, 0 if missing/bad."""
    size_el = response.find(".//D:getcontentlength", _DAV_NS)
    if size_el is None or not size_el.text:
        return 0
    try:
        return int(size_el.text.strip())
    except ValueError:
        # Malformed getcontentlength body — log so a server bug doesn't
        # silently cause every file to be reported as size 0 (and thus never
        # selected as "largest").
        xbmc.log(
            "NZB-DAV: Non-numeric getcontentlength '{}' for "
            "href '{}'; treating as 0".format(size_el.text[:40], href_path),
            xbmc.LOGWARNING,
        )
        return 0


def _current_level_file_key(
    href_path, size, have_hint, hint_tokens, hint_episode_tags, min_video_size
):
    """Return (file_key, ep_score) ranking a current-level candidate video.

    Rank candidate videos by (episode identity, size, token overlap). Without a
    hint both scores are 0, so this stays the historical largest-wins rule.
    With a hint, the requested SxxExx episode outranks a larger non-matching
    sibling, while size outranks loose token overlap so a small token-rich
    extra can't beat the feature.

    "Above the advertised-size floor" is the TOP ranking dimension so a
    below-floor job-start stub never outranks a real file at the SAME level: an
    episode-tagged stub (ep=1000) would otherwise beat a generically-named
    above-floor real file (ep=0), win selection, and -- with no subdir to defer
    into -- be returned and re-rejected every poll (#282 follow-up D / Codex).
    With no floor (min_video_size <= 0) this flag is constantly True, so the key
    reduces to the historical (ep, size, tok) and ranking is byte-identical. A
    size-0 file is NOT below-floor (unknown size, not a known stub), so it is
    unaffected.

    A WRONG-episode file (ep=-1000) must never be promoted above a
    requested-episode stub by the above-floor boost: an above-floor S01E04
    would otherwise outrank a below-floor requested-S01E05 stub, become
    best_file, pass the resolver's stub guard (its size is real) and STREAM THE
    WRONG EPISODE instead of waiting (Codex #340). Rank "not a wrong episode"
    ABOVE the floor flag so wrong episodes sink below everything; the
    requested-ep stub then stays best_file and the poll loop keeps waiting.
    ``ep_score >= 0`` is monotonic in ep_score at the wrong-ep boundary, so with
    no floor this term never reverses the historical (ep, size, tok) ordering.
    """
    if have_hint:
        ep_score, tok_score = webdav_match._title_hint_match_score(
            href_path, hint_tokens, hint_episode_tags
        )
    else:
        ep_score, tok_score = 0, 0
    file_above_floor = size <= 0 or size >= min_video_size
    file_key = (ep_score >= 0, file_above_floor, ep_score, size, tok_score)
    return file_key, ep_score


def _classify_propfind_response(response, base_host, request_path, subdirs):
    """Classify one PROPFIND response.

    Returns ``(href_path, size)`` for a video file. For a collection it appends
    to ``subdirs`` and returns None; returns None for any non-video or
    skipped/malformed response.
    """
    href = response.find("D:href", _DAV_NS)
    if href is None:
        return None
    href_text = (href.text or "").strip()
    if not href_text:
        xbmc.log(
            "NZB-DAV: Skipping response with empty href in PROPFIND",
            xbmc.LOGWARNING,
        )
        return None

    href_path = _extract_href_path(href_text, base_host)
    if href_path is None:
        return None

    # Check if it's a collection (subdirectory)
    if response.find(".//D:resourcetype/D:collection", _DAV_NS) is not None:
        _collect_subdir(href_path, request_path, subdirs)
        return None

    # Check if it's a video file
    lower_href = href_text.lower()
    if not any(lower_href.endswith(ext) for ext in _VIDEO_EXTENSIONS):
        return None

    return href_path, _parse_content_length(response, href_path)


def _scan_propfind_responses(
    root, url, have_hint, hint_tokens, hint_episode_tags, min_video_size
):
    """Scan all PROPFIND responses, returning the best video and the subdirs.

    Returns a tuple ``(best_file, best_size, best_file_key, best_match_score,
    subdirs)``.
    """
    from urllib.parse import urlparse

    parsed_request_url = urlparse(url)
    base_host = parsed_request_url.netloc
    request_path = parsed_request_url.path.rstrip("/")

    best_file = None
    best_size = 0
    best_file_key = None
    best_match_score = 0
    subdirs = []

    for response in root.findall(".//D:response", _DAV_NS):
        candidate = _classify_propfind_response(
            response, base_host, request_path, subdirs
        )
        if candidate is None:
            continue
        href_path, size = candidate
        file_key, ep_score = _current_level_file_key(
            href_path, size, have_hint, hint_tokens, hint_episode_tags, min_video_size
        )
        if _propfind_candidate_wins(size, ep_score, file_key, best_file_key):
            best_file_key = file_key
            best_size = size
            best_file = href_path
            # Keep the episode score as the recurse/adoption signal so the
            # wrong-episode gate compares episode identity, not token noise.
            best_match_score = ep_score

    return best_file, best_size, best_file_key, best_match_score, subdirs


def _propfind_candidate_wins(size, ep_score, file_key, best_file_key):
    """Return True when a current-level video should displace the best so far.

    A current-level video only displaces "nothing yet" when it carries a
    positive selection signal: a real (non-zero) size -- the historical
    largest-wins rule, under which main never adopted a size-0 file but
    recursed past it -- or, with a hint, a positive episode/token score.
    This keeps the no-hint movie path byte-identical to main (deliberate
    decision f12b3c3): a top-level video missing getcontentlength is skipped
    so recursion can find the real feature in a subfolder rather than
    returning a placeholder/teaser.

    Token overlap alone must NOT establish a signal: per the docstring, raw
    token overlap ranks BELOW size, so a zero-size placeholder/teaser that
    merely shares title tokens must not be adopted and pre-empt recursion into
    the subfolder holding the real feature. Only a real size or a positive
    episode match establishes a selectable signal.
    """
    has_signal = size > 0 or ep_score > 0
    return has_signal and (best_file_key is None or file_key > best_file_key)


def _sibling_beats_deferred(
    result, best_file_key, have_hint, hint_tokens, hint_episode_tags, min_video_size
):
    """Return True if a sibling result should be adopted over the deferred file.

    If we deferred a wrong-episode current-level file, only adopt the sibling
    when it is at least as good a hint match; otherwise the mismatched
    current-level file is no worse and stays the fallback. The key carries the
    same above-floor flag as the current-level ranking, so a below-floor stub
    never wins on episode identity: an above-floor (or unknown-size) child
    always outranks a deferred stub -- including an exact-episode child whose
    PROPFIND has no getcontentlength (size hint 0 is NOT below-floor)
    (#282 / Codex).
    """
    import resources.lib.webdav as _webdav

    if not have_hint:
        return True
    result_ep_score, result_tok_score = webdav_match._title_hint_match_score(
        result, hint_tokens, hint_episode_tags
    )
    result_size = _webdav.get_video_file_size_hint(result)
    result_above_floor = result_size <= 0 or result_size >= min_video_size
    result_key = (
        result_ep_score >= 0,
        result_above_floor,
        result_ep_score,
        result_size,
        result_tok_score,
    )
    return result_key >= best_file_key


def _defer_decision(
    best_file, best_size, best_match_score, subdirs, hint_episode_tags, min_video_size
):
    """Return ``(best_is_stub, defer_to_subdirs)`` for the current-level best.

    A current-level best that is grossly undersized versus the advertised
    release size (#282) is nzbdav's job-start stub. Treat it like the
    wrong-episode case: defer it so discovery recurses into the subfolder
    holding the real file first, falling back to the stub only if the descent
    finds nothing better. The floor never DROPS the candidate (a legitimately
    small release is still returned as the fallback). A 0 floor -- movie/no-floor
    or pack -- disables this so the historical current-level short-circuit is
    byte-identical to before.

    When an episode was requested but the current-level best is NOT a confirmed
    episode match (score below the confirmed-match threshold of 1000), the
    requested episode may still live in a sibling subdir. This covers both an
    explicit wrong-episode file (score -1000) AND a generic current-level video
    that merely shares show tokens but carries no SxxExx tag (score 0) -- either
    would otherwise be returned before we ever scan the subdir holding the exact
    requested episode. A movie/token-only hint has empty hint_episode_tags so it
    keeps the historical short-circuit, as does the no-hint path. The stub case
    defers on the same terms. Both only defer when there is actually a subdir to
    descend into.
    """
    best_is_stub = (
        best_file is not None and min_video_size > 0 and 0 < best_size < min_video_size
    )
    defer_to_subdirs = bool(subdirs) and (
        (bool(hint_episode_tags) and best_match_score < 1000) or best_is_stub
    )
    return best_is_stub, defer_to_subdirs


def _resolve_best_or_recurse(
    scan,
    _depth,
    _visited,
    settings,
    have_hint,
    hint_tokens,
    hint_episode_tags,
    min_video_size,
):
    """Decide between the current-level best, a sibling, or recursion.

    ``scan`` is the tuple returned by :func:`_scan_propfind_responses`. Returns
    the chosen video href path, or None.
    """
    best_file, best_size, best_file_key, best_match_score, subdirs = scan

    best_is_stub, defer_to_subdirs = _defer_decision(
        best_file,
        best_size,
        best_match_score,
        subdirs,
        hint_episode_tags,
        min_video_size,
    )
    if best_file and not defer_to_subdirs:
        return _accept_current_level(
            best_file,
            best_size,
            "NZB-DAV: Found video file: {} ({} bytes)".format(best_file, best_size),
        )

    if best_file:
        _log_defer_reason(best_file, best_is_stub)

    result = _recurse_and_adopt_sibling(
        subdirs,
        _depth,
        _visited,
        settings,
        best_file,
        best_file_key,
        (have_hint, hint_tokens, hint_episode_tags, min_video_size),
    )
    if result:
        return result

    return _fallback_to_current_level(best_file, best_size)


def _fallback_to_current_level(best_file, best_size):
    """Return the deferred current-level video when recursion found nothing."""
    if not best_file:
        return None
    # Recursion found nothing better; fall back to the wrong-episode
    # current-level file rather than returning nothing at all.
    return _accept_current_level(
        best_file,
        best_size,
        "NZB-DAV: No matching episode in sibling subfolders; falling "
        "back to current-level video: {} ({} bytes)".format(best_file, best_size),
    )


def _log_defer_reason(best_file, best_is_stub):
    """Log why the current-level best is being deferred for a sibling scan."""
    reason = "an undersized stub" if best_is_stub else "a wrong-episode match"
    xbmc.log(
        "NZB-DAV: Current-level video '{}' is {} for the requested "
        "title; checking sibling subfolders first".format(best_file, reason),
        xbmc.LOGDEBUG,
    )


def _accept_current_level(best_file, best_size, message):
    """Remember the size hint, log ``message`` at INFO, and return ``best_file``."""
    import resources.lib.webdav as _webdav

    _webdav._remember_video_file_size_hint(best_file, best_size)
    xbmc.log(message, xbmc.LOGINFO)
    return best_file


def _recurse_and_adopt_sibling(
    subdirs, _depth, _visited, settings, best_file, best_file_key, hint_ctx
):
    """Recurse into subdirs and return a sibling only if it beats the deferred.

    ``hint_ctx`` is the ``(have_hint, hint_tokens, hint_episode_tags,
    min_video_size)`` tuple. Subdirs came from PROPFIND hrefs and are already
    URL-encoded, so recursive calls skip top-level quote() to avoid
    `%20` -> `%2520`.
    """
    have_hint, hint_tokens, hint_episode_tags, min_video_size = hint_ctx
    result = _find_video_file_in_subdirs(
        subdirs,
        _depth,
        _visited,
        settings,
        hint_tokens=hint_tokens,
        hint_episode_tags=hint_episode_tags,
        min_video_size=min_video_size,
    )
    if not result:
        return None
    if best_file and not _sibling_beats_deferred(
        result,
        best_file_key,
        have_hint,
        hint_tokens,
        hint_episode_tags,
        min_video_size,
    ):
        return None
    return result


def _describe_webdav_error(e):
    """Append a human-readable hint to a WebDAV browse exception detail."""
    error_detail = "{}".format(e)
    if "401" in error_detail or "Unauthorized" in error_detail:
        error_detail += " — Check WebDAV username/password in addon settings"
    elif "404" in error_detail or "Not Found" in error_detail:
        error_detail += (
            " — WebDAV folder not found, check nzbdav is creating " "/content/ symlinks"
        )
    elif "Connection" in error_detail or "urlopen" in str(type(e).__name__):
        error_detail += " — Check WebDAV server is reachable at configured URL"
    return error_detail


def _mark_visited(folder_path, visited):
    """Record ``folder_path`` in the visited set, or return None if already seen.

    Catches a hostile/misconfigured server that returns its parent (or itself)
    as a child, which would otherwise recurse until the depth cap.
    """
    if visited is None:
        visited = set()
    normalized = (folder_path or "").rstrip("/")
    if normalized in visited:
        xbmc.log(
            "NZB-DAV: Skipping already-visited WebDAV folder '{}'".format(folder_path),
            xbmc.LOGDEBUG,
        )
        return None
    visited.add(normalized)
    return visited
