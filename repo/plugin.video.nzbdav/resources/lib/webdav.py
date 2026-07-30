# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""WebDAV availability checker for nzbdav streams.

The PROPFIND scan/recurse internals live in ``webdav_discovery`` and the
episode/title match scoring in ``webdav_match``; this module keeps the
test-patched surface (``find_video_file``, ``probe_webdav_reachable``,
``_get_settings``, ``_http_head``, ``urlopen``, the size-hint store) plus the
public stream-URL helpers, and re-exports the moved names so existing imports
(e.g. ``webdav_discovery``'s use of ``_episode_tags``) keep working.
"""

import base64
from typing import NamedTuple, Optional
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import xbmc

from resources.lib.http_util import prefer_ipv4_connections
from resources.lib.webdav_match import (
    _episode_tags,
    _hint_tokens,
    _title_hint_match_score,
)


class TitleHints(NamedTuple):
    """Requested-release name hint (plus its pre-parsed forms) for discovery.

    ``title_hint`` is the optional requested scene title; ``tokens`` and
    ``episode_tags`` are its pre-parsed forms threaded through recursion so the
    hint is parsed once per discovery rather than once per folder level.
    """

    title_hint: Optional[str] = None
    tokens: Optional[tuple] = None
    episode_tags: Optional[tuple] = None


# Re-exported for callers/tests that resolve these names on ``webdav``.
__all__ = [
    "_episode_tags",
    "_hint_tokens",
    "_title_hint_match_score",
    "TitleHints",
    "_get_settings",
    "_http_head",
    "probe_webdav_reachable",
    "get_video_file_size_hint",
    "folder_video_total_bytes",
    "folder_video_inventory",
    "find_video_file",
    "find_video_stream_for_folder",
    "get_webdav_stream_url_for_path",
    "check_file_in_folder",
    "_build_auth_headers",
    "_remember_video_file_size_hint",
]

_VIDEO_FILE_SIZE_HINTS_MAX = 64
_VIDEO_FILE_SIZE_HINTS = {}


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
        with prefer_ipv4_connections():
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
    content_root = _probe_content_root(settings_getter)
    url = "{}/{}/".format(base.rstrip("/"), content_root)
    mon = monitor or xbmc.Monitor()

    attempt = 0
    while attempt <= max_retries:
        try:
            status = _http_head(url, settings["username"], settings["password"])
            return _classify_probe_status(status)
        except Exception as e:  # pylint: disable=broad-except
            attempt += 1
            if attempt > max_retries:
                _log_probe_exhausted(e, max_retries)
                return False, "connection_error"
            _log_probe_retry(e, attempt, max_retries)
            if mon.waitForAbort(retry_delay):
                return False, "connection_error"
    # Unreachable in normal flow — defensive safety net for static analysis.
    return False, "connection_error"


def _log_probe_exhausted(error, max_retries):
    """Log a WebDAV probe failure after all retries were exhausted."""
    xbmc.log(
        "NZB-DAV: WebDAV probe connection error after {} "
        "attempts: {} ({})".format(max_retries + 1, error, type(error).__name__),
        xbmc.LOGERROR,
    )


def _log_probe_retry(error, attempt, max_retries):
    """Log a single WebDAV probe connection error before retrying."""
    xbmc.log(
        "NZB-DAV: WebDAV probe connection error "
        "(attempt {}/{}): {} ({})".format(
            attempt, max_retries, error, type(error).__name__
        ),
        xbmc.LOGDEBUG,
    )


def _probe_content_root(settings_getter):
    """Resolve the configured WebDAV content root, defaulting to "content".

    Allows differently-routed nzbdav instances to override the content root.
    `content_root` is guaranteed non-empty, so the historical trailing
    ``or "content"`` was dead code (closes §H.3 Low).
    """
    try:
        if settings_getter is not None:
            raw = settings_getter("webdav_content_root", "")
        else:
            import xbmcaddon

            raw = xbmcaddon.Addon("plugin.video.nzbdav").getSetting(
                "webdav_content_root"
            )
        return raw.strip("/") if isinstance(raw, str) and raw else "content"
    except Exception:  # pylint: disable=broad-except
        return "content"


def _classify_probe_status(status):
    """Classify an HTTP HEAD status into a (reachable, error_type) tuple."""
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


def _remember_video_file_size_hint(file_path, size):
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        return
    if not file_path:
        return
    # A non-positive size means this scan saw no getcontentlength for the path
    # (current size unknown). Drop any prior positive value so a later stub
    # check fails OPEN on the now-unknown size instead of re-rejecting the path
    # against a stale cached stub size (#282 / Codex).
    if size <= 0:
        _VIDEO_FILE_SIZE_HINTS.pop(file_path, None)
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


_FOLDER_TOTAL_INCOMPLETE = -1

# Video extensions summed by the folder-total walk. Kept module-level so the
# per-href video test below stays a one-liner (the genuine size accounting
# lives in ``_folder_total_video_size``).
_FOLDER_TOTAL_VIDEO_EXTENSIONS = (
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".ts",
    ".m2ts",
    ".wmv",
    ".mov",
)


def _folder_total_resource_key(resource_path):
    """Return a decoded, traversal-normalized path key for containment."""
    import posixpath
    from urllib.parse import unquote

    decoded = unquote(resource_path or "").replace("\\", "/")
    if not decoded:
        return "/"
    was_absolute = decoded.startswith("/")
    normalized = posixpath.normpath(decoded)
    if was_absolute:
        normalized = "/" + normalized.lstrip("/")
    normalized = normalized.rstrip("/")
    return normalized or "/"


def _folder_total_absolute_path(resource_path):
    """Return a slash-rooted href path without changing its URL encoding."""
    if not resource_path:
        return "/"
    return resource_path if resource_path.startswith("/") else "/" + resource_path


def _folder_total_resolve_child_path(href_path, request_path):
    """Resolve a relative PROPFIND href beneath the current request folder."""
    from urllib.parse import urljoin

    if href_path.startswith("/"):
        return href_path
    return urljoin(request_path.rstrip("/") + "/", href_path)


def _folder_total_path_is_contained(resource_path, request_path):
    """Return whether ``resource_path`` is self or beneath ``request_path``."""
    resource_key = _folder_total_resource_key(resource_path)
    request_key = _folder_total_resource_key(request_path)
    prefix = "/" if request_key == "/" else request_key + "/"
    return resource_key == request_key or resource_key.startswith(prefix)


def _webdav_url_for_path(base, resource_path, already_encoded=False):
    """Join a WebDAV path without duplicating a reverse-proxy base prefix."""
    from urllib.parse import urlsplit, urlunsplit

    encoded_path = resource_path if already_encoded else quote(resource_path, safe="/")
    base_parts = urlsplit(base)
    base_path = base_parts.path.rstrip("/") or "/"
    if encoded_path.startswith("/") and _folder_total_path_is_contained(
        encoded_path, base_path
    ):
        return urlunsplit((base_parts.scheme, base_parts.netloc, encoded_path, "", ""))
    return "{}/{}".format(base.rstrip("/"), encoded_path.lstrip("/"))


def _folder_total_enter(folder_path, depth, visited):
    """Apply the depth cap and cycle guard; return ``(visited, skip_reason)``.

    Depth truncation is incomplete because unseen resources may remain below
    the cap. An already-visited folder is a known cycle/duplicate and is safely
    ignored. The returned ``visited`` set is lazily created and has this folder
    marked.
    """
    if depth > 2:
        return visited, "depth"
    if visited is None:
        visited = set()
    normalized = _folder_total_resource_key(_folder_total_absolute_path(folder_path))
    if normalized in visited:
        return visited, "visited"
    visited.add(normalized)
    return visited, None


def _folder_total_resolve_url(settings_getter, settings, folder_path, already_encoded):
    """Resolve WebDAV settings and the PROPFIND URL for ``folder_path``.

    Returns ``(settings, url)``. Recursive calls pass hrefs the PROPFIND
    response already URL-encoded, so ``already_encoded`` skips a second
    ``quote()`` that would turn ``%`` into ``%25`` and 404 the probe.
    """
    settings = _read_settings(settings_getter) if settings is None else settings
    base = settings["webdav_url"] or settings["nzbdav_url"]
    url = _webdav_url_for_path(base, folder_path, already_encoded=already_encoded)
    if not url.endswith("/"):
        url += "/"
    return settings, url


def _folder_total_fetch_root(url, username, password):
    """PROPFIND ``url`` and return the parsed XML root with entities disabled.

    Parsing delegates to ``resources.lib.xml_safety.safe_fromstring``, which
    rejects entity declarations (XXE / billion-laughs) before the parser can
    act on them, so a hostile WebDAV server cannot coerce a local file read.
    Raises on any PROPFIND/parse failure; the caller turns that into the
    INCOMPLETE (fail-open) outcome.
    """
    from resources.lib.xml_safety import safe_fromstring

    req = Request(url, method="PROPFIND")
    req.add_header("Depth", "1")
    for header, value in _build_auth_headers(username, password).items():
        req.add_header(header, value)

    with prefer_ipv4_connections():
        # nosemgrep
        with urlopen(  # nosec B310 — URL from user's configured WebDAV setting
            req, timeout=10
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")

    return safe_fromstring(body)


def _folder_total_href_path(response, ns):
    """Extract a ``<D:response>``'s href as ``(href_text, href_path)``.

    Returns ``None`` to skip an entry with no/empty or malformed href. The
    cross-host/scheme normalization keeps only the path component when the
    server returns an absolute URL (or a scheme-relative ``//host/...``).
    """
    from urllib.parse import urlparse

    href = response.find("D:href", ns)
    if href is None:
        return None
    href_text = (href.text or "").strip()
    if not href_text:
        return None
    try:
        parsed_href = urlparse(href_text)
        # Always use the parsed path, including for relative hrefs, so query
        # strings and fragments cannot interfere with file classification.
        href_path = parsed_href.path
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: folder_video_total_bytes skipping malformed href "
            "'{}': {}".format(href_text, e),
            xbmc.LOGWARNING,
        )
        return None
    return href_text, href_path


def _folder_total_collect_subdir(response, href_path, request_path, subdirs, ns):
    """Append a real child subdir to ``subdirs`` if this entry is a collection.

    Returns ``True`` when the entry is a collection (so the caller stops
    classifying it as a video) -- the folder itself and dot-prefixed children
    are recognised as collections but not enqueued for recursion.
    """
    resource_type = response.find(".//D:resourcetype/D:collection", ns)
    if resource_type is None:
        return False
    child = href_path.rstrip("/")
    child_key = _folder_total_resource_key(child)
    request_key = _folder_total_resource_key(request_path)
    if child_key != request_key:
        segment = child_key.rsplit("/", 1)[-1]
        if not segment.startswith("."):
            subdirs.append(child + "/")
    return True


def _folder_total_video_size(response, ns):
    """Return ``(size, incomplete)`` for a video entry's getcontentlength.

    A matched video file whose ``getcontentlength`` is absent, non-numeric, or
    negative cannot be sized; flagging the scan incomplete (rather than counting
    0) makes the caller fail OPEN instead of under-counting into a false stub
    reject of real content (#282 / #355 review).
    """
    size_el = response.find(".//D:getcontentlength", ns)
    if size_el is None or not size_el.text:
        return 0, True
    try:
        size = int(size_el.text.strip())
    except ValueError:
        return 0, True
    if size < 0:
        return 0, True
    return size, False


def _folder_total_track_max(stats, size):
    """Record the largest single video file seen in ``stats["max"]``."""
    if stats is not None and size > stats.get("max", 0):
        stats["max"] = size


def _folder_total_track_video(stats, href_path, size):
    """Append one completely-sized video row to the optional walk stats."""
    if stats is not None:
        stats.setdefault("videos", []).append((href_path, size))


def _folder_total_entry_path(response, ns, request_path, seen_resources):
    """Return one contained, unseen entry path plus its incomplete flag."""
    pair = _folder_total_href_path(response, ns)
    if pair is None:
        return None, False
    _href_text, href_path = pair
    href_path = _folder_total_resolve_child_path(href_path, request_path)
    resource_key = _folder_total_resource_key(href_path)
    if resource_key in seen_resources:
        return None, False
    if not _folder_total_path_is_contained(href_path, request_path):
        xbmc.log(
            "NZB-DAV: folder inventory ignored out-of-tree href '{}' "
            "for '{}'".format(href_path, request_path),
            xbmc.LOGWARNING,
        )
        return None, True
    seen_resources.add(resource_key)
    return href_path, False


def _folder_total_entry_video_size(response, ns, href_path, request_path, subdirs):
    """Return video bytes plus incomplete flag, or ``(None, False)`` to skip."""
    from urllib.parse import unquote

    if _folder_total_collect_subdir(response, href_path, request_path, subdirs, ns):
        return None, False
    lowered = unquote(href_path).lower()
    if not any(lowered.endswith(ext) for ext in _FOLDER_TOTAL_VIDEO_EXTENSIONS):
        return None, False
    return _folder_total_video_size(response, ns)


def _folder_total_scan_entries(root, folder_path, stats, seen_resources):
    """Walk the PROPFIND responses once; return ``(total, incomplete, subdirs)``.

    Sums every video file's bytes at this level, collects child subdirs for the
    caller to recurse into, and flags ``incomplete`` when a video file cannot be
    sized. Tracks the largest single file via ``_folder_total_track_max``.
    """
    ns = {"D": "DAV:"}
    request_path = _folder_total_absolute_path(folder_path).rstrip("/") or "/"

    total = 0
    incomplete = False
    subdirs = []
    for response in root.findall(".//D:response", ns):
        href_path, entry_incomplete = _folder_total_entry_path(
            response, ns, request_path, seen_resources
        )
        if entry_incomplete:
            incomplete = True
        if href_path is None:
            continue
        size, entry_incomplete = _folder_total_entry_video_size(
            response, ns, href_path, request_path, subdirs
        )
        if size is None:
            continue
        if entry_incomplete:
            incomplete = True
            continue
        total += size
        _folder_total_track_max(stats, size)
        _folder_total_track_video(stats, href_path, size)
    return total, incomplete, subdirs


def _folder_total_sum_subdirs(subdirs, settings, depth, visited, stats, seen_resources):
    """Recurse into collected subdirs; return ``(added_total, incomplete)``.

    A subfolder that could not be fully sized (negative result) makes the whole
    folder incomplete; its bytes are otherwise accumulated into ``added_total``.
    """
    added = 0
    incomplete = False
    for subdir in subdirs:
        sub_total = folder_video_total_bytes(
            subdir,
            _settings=settings,
            _depth=depth + 1,
            _visited=visited,
            _already_encoded=True,
            _stats=stats,
            _seen_resources=seen_resources,
        )
        if sub_total < 0:
            incomplete = True
        else:
            added += sub_total
    return added, incomplete


def folder_video_total_bytes(
    folder_path,
    settings_getter=None,
    _settings=None,
    _depth=0,
    _visited=None,
    _already_encoded=False,
    _stats=None,
    _seen_resources=None,
):
    """Return the summed bytes of every video file in a completed WebDAV folder.

    Walks the release tree (same depth cap as ``find_video_file``) and sums each
    video file's PROPFIND ``getcontentlength``. Unlike ``find_video_file`` this
    does NOT short-circuit on the first usable file -- it visits the whole tree --
    so the resolver can compare the folder's REAL content against the advertised
    release size *pack-agnostically* (#282): a folder whose total video bytes are
    far below the advertised size is exposing only nzbdav's job-start stub --
    whether the release is a single movie or a multi-episode pack -- so the stub
    guard keeps polling; once the real feature/episodes materialise the total
    reaches the advertised size and playback proceeds. This replaces the old
    title-keyword ``release_is_pack`` gate, which guessed pack-ness from the name
    and disabled the stub guard entirely for anything it classified as a pack.

    Return contract -- three outcomes, two of which make the caller fail OPEN
    (the stub guard only ever BLOCKS playback on positive evidence of a stub, so
    an unknown total must never under-count into a false reject of real content):

    * ``> 0``  -- a complete, trustworthy total of the folder's video bytes.
    * ``0``    -- the folder is genuinely empty of video (no files seen).
    * ``< 0`` (``_FOLDER_TOTAL_INCOMPLETE``) -- the size picture is INCOMPLETE: a
      PROPFIND/parse error, or a matched video file whose ``getcontentlength`` was
      missing/non-numeric/negative, a discovered subtree extending beyond the
      traversal depth cap, or a suspicious href outside the requested folder
      subtree (so summing would silently under-count or mix jobs). The caller must
      fail OPEN here rather than compare a partial total against the floor --
      ``_discovered_video_is_stub``'s ``total <= 0 -> return False`` does exactly
      that. Incompleteness propagates up through recursion (a subfolder that could
      not be fully sized makes the whole folder incomplete).

    ``_stats`` (internal): when a dict is passed, ``_stats["max"]`` is populated
    with the size of the LARGEST single video file seen across the whole tree.
    The stub guard uses it to reject a picked file that is anomalously tiny
    versus a real sibling (the folder total can clear the floor on a sibling's
    bytes while ``video_path`` is still the job-start stub -- #355 review).
    """
    from urllib.parse import urlsplit

    settings, url = _folder_total_resolve_url(
        settings_getter, _settings, folder_path, _already_encoded
    )
    request_path = urlsplit(url).path
    _visited, skip_reason = _folder_total_enter(request_path, _depth, _visited)
    if skip_reason == "depth":
        return _FOLDER_TOTAL_INCOMPLETE
    if skip_reason:
        return 0
    if _seen_resources is None:
        _seen_resources = set()
    _seen_resources.add(
        _folder_total_resource_key(_folder_total_absolute_path(request_path))
    )

    try:
        root = _folder_total_fetch_root(url, settings["username"], settings["password"])
        total, incomplete, subdirs = _folder_total_scan_entries(
            root, request_path, _stats, _seen_resources
        )
    except Exception as error:  # pylint: disable=broad-except
        # A PROPFIND/parse failure means we cannot trust the total; signal
        # incomplete so the guard fails OPEN rather than rejecting on a partial
        # (the poll loop re-runs the guard, so this self-heals next iteration).
        xbmc.log(
            "NZB-DAV: folder_video_total_bytes scan failed for '{}': {}".format(
                folder_path, error
            ),
            xbmc.LOGDEBUG,
        )
        return _FOLDER_TOTAL_INCOMPLETE

    added, sub_incomplete = _folder_total_sum_subdirs(
        subdirs, settings, _depth, _visited, _stats, _seen_resources
    )
    total += added
    if sub_incomplete:
        incomplete = True
    return _FOLDER_TOTAL_INCOMPLETE if incomplete else total


def folder_video_inventory(
    folder_path, requested=None, settings_getter=None, _settings=None
):
    """Return a confirmed full-tree video inventory, or ``None`` if incomplete.

    The same bounded PROPFIND walk used by the completed-folder stub guard
    collects every supported video path and size. A transient traversal or
    sizing error therefore remains distinguishable from a reachable empty
    folder: errors return ``None`` while an empty folder returns an empty
    :class:`~resources.lib.episode_inventory.VideoInventory`.

    ``_settings`` is an internal one-settings-read fast path used by
    :func:`find_video_stream_for_folder`.
    """
    from resources.lib.episode_inventory import build_video_inventory

    stats = {"videos": []}
    total = folder_video_total_bytes(
        folder_path,
        settings_getter=settings_getter,
        _settings=_settings,
        _stats=stats,
    )
    if total < 0:
        return None
    return build_video_inventory(stats["videos"], requested=requested)


def find_video_file(
    folder_path,
    hints=None,
    min_video_size=0,
    settings_getter=None,
    _state=None,
):
    """Browse a WebDAV folder and find the requested (or largest) video file.

    Args:
        folder_path: WebDAV folder path to scan (may be absolute or relative).
        hints: Optional :class:`TitleHints` carrying the requested release name
            (``title_hint``) plus its pre-parsed ``tokens``/``episode_tags``.
            When a ``title_hint`` is supplied and a folder/pack holds several
            candidate videos, the one whose name matches the hint — especially
            the requested SxxExx episode — is preferred over the largest video.
            When omitted, the historical largest-video behavior is preserved.
        _state: Internal ``(depth, visited, already_encoded, settings)`` recursion
            tuple; external callers never pass it (default
            ``(0, None, False, None)``). ``depth`` caps traversal; ``visited`` is
            the set of already-scanned paths that catches a hostile or
            misconfigured server returning its parent (or itself) as a child;
            ``already_encoded`` is set by the recursive call when ``folder_path``
            came from a PROPFIND ``<D:href>`` (already URL-encoded) so recursive
            descents do not double-encode ``%20`` → ``%2520`` and 404; ``settings``
            reuses an already-read settings dict.
        min_video_size: Optional minimum plausible size (bytes) for the real
            single-file video, precomputed by the resolver from the advertised
            release size (#282). A current-level candidate whose size is a
            positive value BELOW this floor is treated as nzbdav's job-start
            stub: discovery recurses into subfolders for the real file first and
            falls back to the small candidate only if nothing better is found.
            ``0`` (the default) disables the floor, keeping the historical
            current-level short-circuit unchanged. The floor never DROPS a
            candidate -- it only defers it -- so a release that is legitimately
            small is still returned. The resolver now threads this floor for ALL
            releases (it is pack-AGNOSTIC: ``advertised * fraction`` regardless of
            title); a pack whose episodes all sit below the floor simply has no
            above-floor candidate, so ranking falls through to episode identity /
            size and the correct episode is still returned (#282 redesign).

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
    from resources.lib import webdav_discovery

    if hints is None:
        hints = TitleHints()
    _depth, _visited, _already_encoded, _settings = _state or (0, None, False, None)

    if _depth > 2:
        return None

    hint_tokens, hint_episode_tags = webdav_discovery._resolve_hint_sets(
        hints.title_hint, hints.tokens, hints.episode_tags
    )

    _visited = webdav_discovery._mark_visited(folder_path, _visited)
    if _visited is None:
        return None

    settings = _read_settings(settings_getter) if _settings is None else _settings
    req, url = webdav_discovery._build_propfind_request(
        folder_path, _already_encoded, settings
    )
    have_hint = bool(hint_tokens or hint_episode_tags)
    hint_ctx = (have_hint, hint_tokens, hint_episode_tags, min_video_size)

    try:
        return _browse_and_resolve(req, url, _depth, _visited, settings, hint_ctx)
    except Exception as e:
        error_detail = webdav_discovery._describe_webdav_error(e)
        xbmc.log(
            "NZB-DAV: Error browsing WebDAV folder '{}': {} ({})".format(
                folder_path, error_detail, type(e).__name__
            ),
            xbmc.LOGERROR,
        )
        return None


def _browse_and_resolve(req, url, _depth, _visited, settings, hint_ctx):
    """Issue the PROPFIND, scan responses, and resolve the best video path.

    ``hint_ctx`` is the ``(have_hint, hint_tokens, hint_episode_tags,
    min_video_size)`` tuple.
    """
    from resources.lib import webdav_discovery

    have_hint, hint_tokens, hint_episode_tags, min_video_size = hint_ctx
    with prefer_ipv4_connections():
        # nosemgrep
        with urlopen(  # nosec B310 — URL from user's configured WebDAV setting
            req, timeout=10
        ) as resp:
            body = resp.read().decode("utf-8", errors="replace")

    root = webdav_discovery._parse_propfind_xml(body)
    scan = webdav_discovery._scan_propfind_responses(
        root, url, have_hint, hint_tokens, hint_episode_tags, min_video_size
    )
    return webdav_discovery._resolve_best_or_recurse(
        scan,
        _depth,
        _visited,
        settings,
        have_hint,
        hint_tokens,
        hint_episode_tags,
        min_video_size,
    )


def _get_webdav_stream_url_for_path_with_settings(file_path, settings):
    """Build a stream URL and auth headers from an already-read settings dict."""
    base = settings["webdav_url"] or settings["nzbdav_url"]
    # Normalize base/file-path boundary so we never produce "host" + "path"
    # (missing slash) or "host//" + "/path" (double slash). The PROPFIND
    # response is *supposed* to hand us an absolute path with a leading
    # slash, but nothing enforces that on the server side.
    encoded_path = quote(file_path, safe="/%")
    url = _webdav_url_for_path(base, encoded_path, already_encoded=True)
    headers = _build_auth_headers(settings["username"], settings["password"])
    return url, headers


def get_webdav_stream_url_for_path(file_path, settings_getter=None):
    """Build a stream URL and auth headers for a full WebDAV path.

    Returns (url, headers_dict) where headers_dict may be empty if no auth.
    """
    return _get_webdav_stream_url_for_path_with_settings(
        file_path, _read_settings(settings_getter)
    )


def find_video_stream_for_folder(
    folder_path,
    settings_getter=None,
    title_hint=None,
    min_video_size=0,
    requested_episode=None,
    on_inventory=None,
):
    """Find a folder's playable video path and stream URL with one settings read.

    ``title_hint`` is the optional requested release name; when supplied it
    steers multi-episode/multi-video folders toward the requested episode
    instead of the largest file (see ``find_video_file``).

    ``min_video_size`` is the optional advertised-size floor that lets discovery
    recurse past a root-level job-start stub into the subfolder holding the real
    file (#282); see ``find_video_file``. ``0`` (default) disables the floor.

    ``requested_episode`` is an explicit ``(season, episode)`` identity. Unlike
    the advisory title hint, it fails closed when the completed folder contains
    named episodes but not the requested one, or multiple untagged videos that
    cannot be mapped to an episode. A single untagged video remains a valid
    ordinary-release fallback. ``on_inventory`` receives the confirmed
    full-tree inventory; callback failures never block playback.
    """
    settings = _read_settings(settings_getter)
    inventory = None
    if requested_episode is not None:
        inventory = folder_video_inventory(
            folder_path,
            requested=requested_episode,
            settings_getter=settings_getter,
            _settings=settings,
        )
        # Explicit route identity is authoritative and the complete inventory
        # is the sole traversal. A named different episode or incomplete scan
        # must never degrade to title-derived/legacy discovery.
        video_path = inventory.selected_path if inventory is not None else None
        if inventory is not None and video_path:
            _remember_video_file_size_hint(video_path, inventory.selected_size)
    else:
        video_path = find_video_file(
            folder_path,
            hints=TitleHints(title_hint=title_hint),
            min_video_size=min_video_size,
            _state=(0, None, False, settings),
        )
        if on_inventory is not None:
            inventory = folder_video_inventory(
                folder_path,
                settings_getter=settings_getter,
                _settings=settings,
            )
    if on_inventory is not None and inventory is not None:
        try:
            on_inventory(inventory)
        except Exception as error:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: WebDAV inventory callback failed: {}".format(error),
                xbmc.LOGDEBUG,
            )
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
