# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""WebDAV availability checker for nzbdav streams."""

import base64
import re
import threading
from queue import Queue
from urllib.error import HTTPError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen

import xbmc

_WEBDAV_SUBDIR_SCAN_WORKERS = 4
_VIDEO_FILE_SIZE_HINTS_MAX = 64
_VIDEO_FILE_SIZE_HINTS = {}
# Capture a season followed by one or more episode numbers so a multi-episode
# file like "S01E01E02E03" yields every episode it contains, not just the
# first. The episode run allows light separators (e.g. "E01-E02", "E01.E02")
# between consecutive episode numbers within the same Sxx tag.
_EPISODE_TAG_RE = re.compile(r"s(\d{1,3})[. _-]*((?:e\d{1,4}[. _-]*)+)", re.IGNORECASE)
_EPISODE_NUM_RE = re.compile(r"e(\d{1,4})", re.IGNORECASE)
# Episode RANGE notation "SxxEaa-Ebb" / "SxxEaa-bb" (e.g. S01E01-E03,
# S01E01-03). The base _EPISODE_TAG_RE only records the literal endpoints
# (E01 and E03) and drops a bare "-03" half entirely, so a request for a
# covered middle episode (E02) would be scored as a different episode. We
# expand the inclusive span below. _EPISODE_RANGE_MAX_SPAN caps expansion so
# a malformed/absurd range (e.g. S01E01-E999) can't balloon the tag set --
# beyond the cap we leave the literal endpoints from _EPISODE_TAG_RE intact.
_EPISODE_RANGE_RE = re.compile(
    r"s(\d{1,3})[. _-]*e(\d{1,4})[. _-]*-[. _-]*e?(\d{1,4})", re.IGNORECASE
)
_EPISODE_RANGE_MAX_SPAN = 64
# Older/scene-alternate "NxNN" / "NNxNN" episode notation (e.g. 2x05,
# 02x05) that PTT's parse_title recognizes (ptt/handlers.py:1444 season
# handler) but the SxxExx regex above does not. Zero-width `(?<!\d)` /
# `(?!\d)` digit-lookarounds anchor the tag without CONSUMING the
# surrounding separator, so adjacent tags ("2x05.2x06") both register
# instead of the boundary eating the gap and dropping the second (and the
# middle of a 3-pack). The `\d{1,2}` season cap (unchanged) is what keeps
# resolutions like 1920x1080 / 1280x720 / 3840x2160 and codec tokens like
# x264/x265 from registering as episodes. Accepts the Cyrillic 'х' the PTT
# handler also allows.
_EPISODE_NXN_RE = re.compile(r"(?<!\d)(\d{1,2})[xх](\d{1,3})(?!\d)", re.IGNORECASE)


def _hint_tokens(value):
    """Return lowercased alphanumeric tokens for loose name matching."""
    if not isinstance(value, str) or not value:
        return frozenset()
    cleaned = re.sub(r"[\W_]+", " ", value.lower())
    return frozenset(token for token in cleaned.split() if token)


def _episode_tags(value):
    """Return the set of (season, episode) tags found in a release name.

    A multi-episode tag such as "S01E01E02E03" expands to every episode it
    spans -- {(1, 1), (1, 2), (1, 3)} -- so a request for a middle episode
    (E02) still matches the combined file rather than only its first episode.
    """
    if not isinstance(value, str) or not value:
        return frozenset()
    tags = set()
    for match in _EPISODE_TAG_RE.finditer(value):
        season = int(match.group(1))
        for episode in _EPISODE_NUM_RE.findall(match.group(2)):
            tags.add((season, int(episode)))
    for match in _EPISODE_RANGE_RE.finditer(value):
        season = int(match.group(1))
        start = int(match.group(2))
        end = int(match.group(3))
        if start <= end <= start + _EPISODE_RANGE_MAX_SPAN:
            for episode in range(start, end + 1):
                tags.add((season, episode))
    for match in _EPISODE_NXN_RE.finditer(value):
        tags.add((int(match.group(1)), int(match.group(2))))
    return frozenset(tags)


def _title_hint_match_score(file_path, hint_tokens, hint_episode_tags):
    """Return how strongly a video file name matches the requested title hint.

    Returns a 2-tuple ``(episode_score, token_score)`` so callers can rank
    episode identity ABOVE size but raw token overlap BELOW it:

    * ``episode_score`` is the strongest signal -- ``1000`` when the requested
      SxxExx episode is present, ``-1000`` when the file names a different
      episode, ``0`` when no episode comparison applies. An episode pack must
      pick the requested episode, not the largest file.
    * ``token_score`` is the raw token-overlap count (``0`` when there is no
      token hint). For a movie hint (no episode tag) this must NOT outrank
      size, or a small token-rich extra/trailer would hijack the feature; the
      folder/sibling sort keys therefore place size between the two scores.

    Returns ``(0, 0)`` when there is no usable hint or the name is empty.
    """
    if not hint_tokens and not hint_episode_tags:
        return (0, 0)
    name = unquote(file_path.rsplit("/", 1)[-1]) if file_path else ""
    if not name:
        return (0, 0)
    parent = (
        unquote(file_path.rsplit("/", 1)[0]) if file_path and "/" in file_path else ""
    )
    episode_score = 0
    if hint_episode_tags:
        # Basename FIRST: the file's own episode tag is authoritative. Only
        # when the basename carries no episode tag (a generically-named file
        # like "video.mkv") do we fall back to the parent directory's tag --
        # this is a LAYERED fallback, NOT a union: a matching dir tag must
        # never mask a wrong-episode FILENAME, or the wrong-episode gate
        # would regress. A season-complete parent ("Show.S01.Complete")
        # yields no tag, so largest-wins is preserved there.
        file_tags = _episode_tags(name)
        if not file_tags and parent:
            file_tags = _episode_tags(parent)
        if file_tags:
            if hint_episode_tags & file_tags:
                # Strong match: same episode requested and present.
                episode_score = 1000
            else:
                # The file names a different episode than requested; never
                # prefer it over a true match (but token overlap may still
                # rank it among non-episode candidates).
                episode_score = -1000
    token_score = len(hint_tokens & _hint_tokens(name)) if hint_tokens else 0
    return (episode_score, token_score)


def _get_settings(settings_getter=None):
    if settings_getter is None:
        import xbmcaddon

        # When the plugin is invoked via `RunScript(...)` (TMDBHelper's
        # tmdb_play hook), repeatedly calling `addon.getSetting()` from the
        # long-running poll loop SIGSEGVs in the Kodi C++ binding even with
        # an explicit addon ID. Callers in the script-mode play path now
        # pass settings_getter explicitly (via
        # router._get_script_setting which reads settings.xml from disk),
        # so this fallback only fires for the GUI plugin path where the
        # script-mode crash doesn't apply.
        addon = xbmcaddon.Addon("plugin.video.nzbdav")

        def settings_getter(key, default=""):
            value = addon.getSetting(key)
            return value if isinstance(value, str) else default

    return {
        # .strip() before .rstrip("/"): a stray trailing space in the
        # configured URL otherwise survives into built stream URLs, where the
        # strict netloc-whitespace guard in _split_http_url rejects them on the
        # fallback content-length probe path (urllib tolerates the space, so the
        # primary plays — only fallback validation breaks).
        "webdav_url": settings_getter("webdav_url", "").strip().rstrip("/"),
        "nzbdav_url": settings_getter("nzbdav_url", "").strip().rstrip("/"),
        "username": settings_getter("webdav_username", ""),
        "password": settings_getter("webdav_password", ""),
    }


def _read_settings(settings_getter=None):
    if settings_getter is None:
        return _get_settings()
    return _get_settings(settings_getter=settings_getter)


def _http_head(
    url, username="", password=""
):  # nosec B107 — empty default = "no auth", not a real password
    req = Request(url, method="HEAD")
    if username:
        credentials = "{}:{}".format(username, password)
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
        req.add_header("Authorization", "Basic {}".format(encoded))
    try:
        # nosemgrep
        with urlopen(  # nosec B310 — URL from user's configured WebDAV setting
            req, timeout=30
        ) as resp:
            return resp.getcode()
    except HTTPError as e:
        return e.code


def probe_webdav_reachable(
    monitor=None, max_retries=1, retry_delay=1, settings_getter=None
):
    """Probe WebDAV reachability and classify any error.

    HEADs the WebDAV content root to determine whether nzbdav/WebDAV is
    reachable and whether credentials are valid. This is a reachability
    probe, not a filename existence check: 404/405 on the root is treated
    as "reachable" because some WebDAV servers do not allow HEAD on
    collections but the server is clearly up.

    Args:
        monitor: Optional xbmc.Monitor instance. If None, a new one is
            created. Passing one in avoids creating a fresh Monitor on
            every poll iteration in the resolve loop.
        max_retries: Number of retries after a connection error
            (max_retries + 1 total HEAD attempts).
        retry_delay: Seconds between connection-error retries, using
            Monitor.waitForAbort so Kodi can shut down cleanly.

    Returns:
        Tuple of (reachable, error_type):
        - (True, None)                - server is up, auth OK
        - (False, "auth_failed")      - 401 or 403
        - (False, "server_error")     - 5xx
        - (False, "connection_error") - network error after retries, or
                                        abort signal received during
                                        retry wait
    """
    settings = _get_settings(settings_getter=settings_getter)
    base = settings["webdav_url"] or settings["nzbdav_url"]
    # Allow differently-routed nzbdav instances to override the content
    # root; default to "content" which matches the standard nzbdav layout.
    if settings_getter is not None:
        try:
            raw = settings_getter("webdav_content_root", "")
            content_root = raw.strip("/") if isinstance(raw, str) and raw else "content"
        except Exception:  # pylint: disable=broad-except
            content_root = "content"
    else:
        try:
            import xbmcaddon

            raw = xbmcaddon.Addon("plugin.video.nzbdav").getSetting(
                "webdav_content_root"
            )
            content_root = raw.strip("/") if isinstance(raw, str) and raw else "content"
        except Exception:  # pylint: disable=broad-except
            content_root = "content"
    # `content_root` is guaranteed non-empty by the branches above, so the
    # earlier trailing `or "content"` was dead code (closes §H.3 Low).
    url = "{}/{}/".format(base.rstrip("/"), content_root)
    mon = monitor or xbmc.Monitor()

    attempt = 0
    while attempt <= max_retries:
        try:
            status = _http_head(url, settings["username"], settings["password"])
            if status in (401, 403):
                xbmc.log(
                    "NZB-DAV: WebDAV probe auth failed (status={})".format(status),
                    xbmc.LOGERROR,
                )
                return False, "auth_failed"
            if status >= 500:
                xbmc.log(
                    "NZB-DAV: WebDAV probe server error (status={})".format(status),
                    xbmc.LOGWARNING,
                )
                return False, "server_error"
            # Any other status - server responded, classify as reachable.
            xbmc.log(
                "NZB-DAV: WebDAV probe reachable (status={})".format(status),
                xbmc.LOGDEBUG,
            )
            return True, None
        except Exception as e:  # pylint: disable=broad-except
            attempt += 1
            if attempt > max_retries:
                xbmc.log(
                    "NZB-DAV: WebDAV probe connection error after {} "
                    "attempts: {} ({})".format(max_retries + 1, e, type(e).__name__),
                    xbmc.LOGERROR,
                )
                return False, "connection_error"
            xbmc.log(
                "NZB-DAV: WebDAV probe connection error "
                "(attempt {}/{}): {} ({})".format(
                    attempt, max_retries, e, type(e).__name__
                ),
                xbmc.LOGDEBUG,
            )
            if mon.waitForAbort(retry_delay):
                return False, "connection_error"
    # Unreachable in normal flow — defensive safety net for static analysis.
    return False, "connection_error"


def _find_video_file_in_subdirs(
    subdirs, depth, visited, settings, hint_tokens=None, hint_episode_tags=None
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
    workers = max(1, min(_WEBDAV_SUBDIR_SCAN_WORKERS, len(pending)))
    result_queue = Queue()
    next_index = [0]
    index_lock = threading.Lock()

    def _scan_worker():
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
                result = find_video_file(
                    subdir,
                    depth + 1,
                    visited,
                    True,
                    settings,
                    title_hint_tokens=hint_tokens,
                    title_hint_episode_tags=hint_episode_tags,
                )
            except Exception as e:  # pylint: disable=broad-except
                xbmc.log(
                    "NZB-DAV: Error scanning WebDAV subfolder in parallel: "
                    "{} ({})".format(e, type(e).__name__),
                    xbmc.LOGWARNING,
                )
                result = None
            result_queue.put((index, result))

    for _index in range(workers):
        thread = threading.Thread(target=_scan_worker, daemon=True)
        thread.start()

    best_path = None
    best_key = None
    have_hint = bool(hint_tokens or hint_episode_tags)
    for _ in range(len(pending)):
        index, result = result_queue.get()
        if not result:
            continue
        size = get_video_file_size_hint(result)
        if have_hint:
            ep_score, tok_score = _title_hint_match_score(
                result, hint_tokens, hint_episode_tags
            )
        else:
            ep_score, tok_score = 0, 0
        # Rank by (episode identity, size, token overlap) and break ties toward
        # the earlier sibling (negative index sorts a smaller index higher).
        # Episode match is primary so the requested SxxExx still wins; size
        # outranks loose token overlap so a small token-rich extra can't beat
        # the feature. Without a hint both scores stay 0, so this reduces to the
        # historical largest-wins rule.
        key = (ep_score, size, tok_score, -index)
        if best_key is None or key > best_key:
            best_path = result
            best_key = key
    return best_path


def _remember_video_file_size_hint(file_path, size):
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        return
    if not file_path or size <= 0:
        return
    _VIDEO_FILE_SIZE_HINTS[file_path] = size
    while len(_VIDEO_FILE_SIZE_HINTS) > _VIDEO_FILE_SIZE_HINTS_MAX:
        _VIDEO_FILE_SIZE_HINTS.pop(next(iter(_VIDEO_FILE_SIZE_HINTS)), None)


def get_video_file_size_hint(file_path):
    """Return the PROPFIND getcontentlength captured for a discovered file."""
    try:
        return int(_VIDEO_FILE_SIZE_HINTS.get(file_path, 0) or 0)
    except (TypeError, ValueError):
        return 0


def find_video_file(
    folder_path,
    _depth=0,
    _visited=None,
    _already_encoded=False,
    _settings=None,
    settings_getter=None,
    title_hint=None,
    title_hint_tokens=None,
    title_hint_episode_tags=None,
):
    """Browse a WebDAV folder and find the requested (or largest) video file.

    Args:
        folder_path: WebDAV folder path to scan (may be absolute or relative).
        _depth: Internal recursion depth counter (used to cap traversal).
        _visited: Internal set of already-scanned paths; catches a hostile
            or misconfigured server that returns its parent (or itself) as
            a child and would otherwise recurse until the depth cap.
        _already_encoded: Internal flag set by the recursive call when the
            supplied ``folder_path`` came from a PROPFIND ``<D:href>`` (which
            the server already URL-encoded for us). Without this, recursive
            descents double-encode ``%20`` → ``%2520`` and every subdirectory
            lookup 404s.
        title_hint: Optional requested release name (e.g. the selected scene
            title). When supplied and a folder/pack holds several candidate
            videos, the one whose name matches the hint — especially the
            requested SxxExx episode — is preferred over the largest video.
            When omitted, the historical largest-video behavior is preserved.
        title_hint_tokens / title_hint_episode_tags: Internal pre-parsed forms
            of ``title_hint`` threaded through recursion so the hint is parsed
            once per discovery rather than once per folder level.

    Returns:
        The WebDAV href path of the largest video file found, typically an
        absolute server path beginning with "/", or None when no video is
        located or an error occurs.

    Side effects:
        Reads WebDAV settings from Kodi via xbmcaddon.Addon("plugin.video.nzbdav").
        Issues a PROPFIND request at the target path and, if no video is found
        at that level, recurses into subdirectories up to two levels deep
        (three total levels including the starting folder).
        Logs discovered files, recursion steps, and errors to the Kodi log.
    """
    import xml.etree.ElementTree as ET  # nosec B405 — parsing trusted WebDAV server response

    if _depth > 2:
        return None

    # Parse the title hint once at the top of a discovery; recursive calls
    # receive the already-parsed token/episode-tag sets so the cost is paid
    # once, not per folder level.
    if title_hint_tokens is None and title_hint_episode_tags is None:
        hint_tokens = _hint_tokens(title_hint)
        hint_episode_tags = _episode_tags(title_hint)
    else:
        hint_tokens = title_hint_tokens or frozenset()
        hint_episode_tags = title_hint_episode_tags or frozenset()

    if _visited is None:
        _visited = set()
    normalized = (folder_path or "").rstrip("/")
    if normalized in _visited:
        xbmc.log(
            "NZB-DAV: Skipping already-visited WebDAV folder '{}'".format(folder_path),
            xbmc.LOGDEBUG,
        )
        return None
    _visited.add(normalized)

    settings = _read_settings(settings_getter) if _settings is None else _settings
    base = settings["webdav_url"] or settings["nzbdav_url"]
    username = settings["username"]
    password = settings["password"]

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
    for header, value in _build_auth_headers(username, password).items():
        req.add_header(header, value)

    VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")

    try:
        # nosemgrep
        with urlopen(  # nosec B310 — URL from user's configured WebDAV setting
            req, timeout=10
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")

        # Parse the PROPFIND XML response with external entities disabled.
        # Python's stdlib XMLParser doesn't accept resolve_entities as a
        # kwarg, but calling expat to disable external DTD loading has
        # the same effect for XXE prevention. Use the expat target builder
        # so a hostile WebDAV server can't coerce us into reading local
        # files via an external entity reference.
        _xml_parser = ET.XMLParser()  # nosec B314 — entities disabled below
        try:
            _xml_parser.parser.DefaultHandler = lambda _d: None
            _xml_parser.parser.ExternalEntityRefHandler = lambda *_: False
        except AttributeError:  # pragma: no cover — non-expat parser backend
            pass
        root = ET.fromstring(
            body, parser=_xml_parser
        )  # nosec B314 — trusted WebDAV server response; entities disabled above
        ns = {"D": "DAV:"}

        best_file = None
        best_size = 0
        best_file_key = None
        best_match_score = 0
        have_hint = bool(hint_tokens or hint_episode_tags)
        subdirs = []

        from urllib.parse import urlparse

        parsed_request_url = urlparse(url)
        base_host = parsed_request_url.netloc
        request_path = parsed_request_url.path.rstrip("/")

        for response in root.findall(".//D:response", ns):
            href = response.find("D:href", ns)
            if href is None:
                continue
            href_text = (href.text or "").strip()

            if not href_text:
                xbmc.log(
                    "NZB-DAV: Skipping response with empty href in PROPFIND",
                    xbmc.LOGWARNING,
                )
                continue

            try:
                parsed_href_obj = urlparse(href_text)
                # Handle cross-host hrefs. nzbdav legitimately returns its
                # INTERNAL hostname in PROPFIND hrefs (e.g. localhost:8080)
                # while we address it at the configured public endpoint
                # (e.g. 192.168.1.93:3000). Trust only the PATH portion of
                # the href — all follow-up requests hit the configured
                # WebDAV host anyway, so an attacker-controlled href host
                # cannot redirect us off-server. Previously we rejected the
                # entire href on host mismatch, which broke real users with
                # reverse-proxied nzbdav setups ("Completed but no video
                # found").
                if href_text.startswith("//"):
                    if parsed_href_obj.netloc != base_host:
                        xbmc.log(
                            "NZB-DAV: cross-host href '{}' — using path "
                            "portion only".format(href_text),
                            xbmc.LOGDEBUG,
                        )
                    href_path = parsed_href_obj.path
                elif parsed_href_obj.scheme:
                    if parsed_href_obj.netloc != base_host:
                        xbmc.log(
                            "NZB-DAV: cross-origin href '{}' — using path "
                            "portion only".format(href_text),
                            xbmc.LOGDEBUG,
                        )
                    href_path = parsed_href_obj.path
                else:
                    href_path = href_text
            except Exception as e:
                xbmc.log(
                    "NZB-DAV: Skipping malformed href '{}': {}".format(href_text, e),
                    xbmc.LOGWARNING,
                )
                continue

            # Check if it's a collection (subdirectory)
            resource_type = response.find(".//D:resourcetype/D:collection", ns)
            if resource_type is not None:
                # Skip the folder itself (href matches our request URL)
                child = href_path.rstrip("/")
                if child != request_path:
                    # Skip hidden (dot-prefixed) subfolders. nzbdav release
                    # folders sometimes get polluted with a leading-dot child
                    # holding a different (often wrong, smaller) movie — e.g.
                    # a '.and_justice_for_all...1080p...' folder hijacking a
                    # 2160p release. Leading dots are not URL-encoded, so the
                    # encoded path segment still starts with ".".
                    segment = child.rsplit("/", 1)[-1]
                    if segment.startswith("."):
                        xbmc.log(
                            "NZB-DAV: Skipping hidden WebDAV subfolder '{}'".format(
                                child
                            ),
                            xbmc.LOGDEBUG,
                        )
                    else:
                        subdirs.append(child + "/")
                continue

            # Check if it's a video file
            lower_href = href_text.lower()
            if not any(lower_href.endswith(ext) for ext in VIDEO_EXTENSIONS):
                continue

            # Get file size
            size_el = response.find(".//D:getcontentlength", ns)
            size = 0
            if size_el is not None and size_el.text:
                try:
                    size = int(size_el.text.strip())
                except ValueError:
                    # Malformed getcontentlength body — log so a server
                    # bug doesn't silently cause every file to be
                    # reported as size 0 (and thus never selected as
                    # "largest").
                    xbmc.log(
                        "NZB-DAV: Non-numeric getcontentlength '{}' for "
                        "href '{}'; treating as 0".format(size_el.text[:40], href_path),
                        xbmc.LOGWARNING,
                    )

            # Rank candidate videos by (episode identity, size, token overlap).
            # Without a hint both scores are 0, so this stays the historical
            # largest-wins rule. With a hint, the requested SxxExx episode
            # outranks a larger non-matching sibling, while size outranks loose
            # token overlap so a small token-rich extra can't beat the feature.
            if have_hint:
                ep_score, tok_score = _title_hint_match_score(
                    href_path, hint_tokens, hint_episode_tags
                )
            else:
                ep_score, tok_score = 0, 0
            file_key = (ep_score, size, tok_score)
            if best_file_key is None or file_key > best_file_key:
                best_file_key = file_key
                best_size = size
                best_file = href_path
                # Keep the episode score as the recurse/adoption signal so the
                # wrong-episode gate compares episode identity, not token noise.
                best_match_score = ep_score

        # When an episode was requested but the current-level best is NOT a
        # confirmed episode match (score below the confirmed-match threshold of
        # 1000), the requested episode may still live in a sibling subdir. This
        # covers both an explicit wrong-episode file (score -1000) AND a generic
        # current-level video that merely shares show tokens but carries no
        # SxxExx tag (score 0) -- either would otherwise be returned before we
        # ever scan the subdir holding the exact requested episode. Recurse
        # first; fall back to the current-level file only if the descent finds
        # nothing better. A movie/token-only hint has empty hint_episode_tags so
        # it keeps the historical short-circuit, as does the no-hint path.
        if best_file and not (
            hint_episode_tags and best_match_score < 1000 and subdirs
        ):
            file_path = best_file
            _remember_video_file_size_hint(file_path, best_size)
            xbmc.log(
                "NZB-DAV: Found video file: {} ({} bytes)".format(file_path, best_size),
                xbmc.LOGINFO,
            )
            return file_path

        if best_file:
            xbmc.log(
                "NZB-DAV: Current-level video '{}' is a wrong-episode match for "
                "the requested title; checking sibling subfolders first".format(
                    best_file
                ),
                xbmc.LOGDEBUG,
            )

        # No (usable) video found at this level, recurse into subdirectories.
        # Subdirs came from PROPFIND hrefs and are already URL-encoded, so
        # recursive calls skip top-level quote() to avoid `%20` -> `%2520`.
        result = _find_video_file_in_subdirs(
            subdirs,
            _depth,
            _visited,
            settings,
            hint_tokens=hint_tokens,
            hint_episode_tags=hint_episode_tags,
        )
        if result:
            # If we deferred a wrong-episode current-level file, only adopt the
            # sibling when it is at least as good a hint match; otherwise the
            # mismatched current-level file is no worse and stays the fallback.
            if best_file and have_hint:
                result_ep_score, result_tok_score = _title_hint_match_score(
                    result, hint_tokens, hint_episode_tags
                )
                result_key = (
                    result_ep_score,
                    get_video_file_size_hint(result),
                    result_tok_score,
                )
                if result_key < best_file_key:
                    result = None
            if result:
                return result

        # Recursion found nothing better; fall back to the wrong-episode
        # current-level file rather than returning nothing at all.
        if best_file:
            _remember_video_file_size_hint(best_file, best_size)
            xbmc.log(
                "NZB-DAV: No matching episode in sibling subfolders; falling "
                "back to current-level video: {} ({} bytes)".format(
                    best_file, best_size
                ),
                xbmc.LOGINFO,
            )
            return best_file

        return None
    except Exception as e:
        error_detail = "{}".format(e)
        if "401" in error_detail or "Unauthorized" in error_detail:
            error_detail += " — Check WebDAV username/password in addon settings"
        elif "404" in error_detail or "Not Found" in error_detail:
            error_detail += (
                " — WebDAV folder not found, check nzbdav is creating "
                "/content/ symlinks"
            )
        elif "Connection" in error_detail or "urlopen" in str(type(e).__name__):
            error_detail += " — Check WebDAV server is reachable at configured URL"
        xbmc.log(
            "NZB-DAV: Error browsing WebDAV folder '{}': {} ({})".format(
                folder_path, error_detail, type(e).__name__
            ),
            xbmc.LOGERROR,
        )
        return None


def _get_webdav_stream_url_for_path_with_settings(file_path, settings):
    """Build a stream URL and auth headers from an already-read settings dict."""
    base = settings["webdav_url"] or settings["nzbdav_url"]
    # Normalize base/file-path boundary so we never produce "host" + "path"
    # (missing slash) or "host//" + "/path" (double slash). The PROPFIND
    # response is *supposed* to hand us an absolute path with a leading
    # slash, but nothing enforces that on the server side.
    encoded_path = quote(file_path, safe="/%")
    url = "{}/{}".format(base.rstrip("/"), encoded_path.lstrip("/"))
    headers = _build_auth_headers(settings["username"], settings["password"])
    return url, headers


def get_webdav_stream_url_for_path(file_path, settings_getter=None):
    """Build a stream URL and auth headers for a full WebDAV path.

    Returns (url, headers_dict) where headers_dict may be empty if no auth.
    """
    return _get_webdav_stream_url_for_path_with_settings(
        file_path, _read_settings(settings_getter)
    )


def find_video_stream_for_folder(folder_path, settings_getter=None, title_hint=None):
    """Find a folder's playable video path and stream URL with one settings read.

    ``title_hint`` is the optional requested release name; when supplied it
    steers multi-episode/multi-video folders toward the requested episode
    instead of the largest file (see ``find_video_file``).
    """
    settings = _read_settings(settings_getter)
    video_path = find_video_file(folder_path, _settings=settings, title_hint=title_hint)
    if not video_path:
        return None, None, None
    stream_url, stream_headers = _get_webdav_stream_url_for_path_with_settings(
        video_path, settings
    )
    return video_path, stream_url, stream_headers


def _build_auth_headers(username, password):
    """Build HTTP Basic Auth headers dict. Returns empty dict if no auth."""
    if not username:
        return {}
    # RFC 7617 forbids CR/LF in Basic-auth credentials; some servers silently
    # split on them (header injection). Drop them defensively so a setting
    # with a stray newline can't corrupt the Authorization header.
    safe_user = username.replace("\r", "").replace("\n", "")
    safe_pass = (password or "").replace("\r", "").replace("\n", "")
    credentials = "{}:{}".format(safe_user, safe_pass)
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return {"Authorization": "Basic {}".format(encoded)}


def check_file_in_folder(folder_path):
    """Check if a video file exists in a WebDAV folder.

    Returns (file_path, None) if found, (None, error_type) if not.
    """
    video_path = find_video_file(folder_path)
    if video_path:
        return video_path, None
    return None, "not_found"
