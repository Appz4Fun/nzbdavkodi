# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""WebDAV availability checker for nzbdav streams.

The PROPFIND scan/recurse internals live in ``webdav_discovery`` and the
episode/title match scoring in ``webdav_match``; this module keeps the
test-patched surface (``find_video_file``, ``probe_webdav_reachable``,
``_get_settings``, ``_http_head``, ``urlopen``, the size-hint store) plus the
public stream-URL helpers, and re-exports the moved names so existing imports
(e.g. ``filter_pack``'s ``_episode_tags``) keep working.
"""

import base64
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import xbmc

from resources.lib.webdav_match import (
    _episode_tags,
    _hint_tokens,
    _title_hint_match_score,
)

# Re-exported for callers/tests that resolve these names on ``webdav``.
__all__ = [
    "_episode_tags",
    "_hint_tokens",
    "_title_hint_match_score",
    "_get_settings",
    "_http_head",
    "probe_webdav_reachable",
    "get_video_file_size_hint",
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
    min_video_size=0,
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
        min_video_size: Optional minimum plausible size (bytes) for the real
            single-file video, precomputed by the resolver from the advertised
            release size (#282). A current-level candidate whose size is a
            positive value BELOW this floor is treated as nzbdav's job-start
            stub: discovery recurses into subfolders for the real file first and
            falls back to the small candidate only if nothing better is found.
            ``0`` (the default) disables the floor, keeping the historical
            current-level short-circuit unchanged. The floor never DROPS a
            candidate -- it only defers it -- so a release that is legitimately
            small is still returned. Skipped for packs by passing ``0``.

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

    if _depth > 2:
        return None

    hint_tokens, hint_episode_tags = webdav_discovery._resolve_hint_sets(
        title_hint, title_hint_tokens, title_hint_episode_tags
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


def find_video_stream_for_folder(
    folder_path, settings_getter=None, title_hint=None, min_video_size=0
):
    """Find a folder's playable video path and stream URL with one settings read.

    ``title_hint`` is the optional requested release name; when supplied it
    steers multi-episode/multi-video folders toward the requested episode
    instead of the largest file (see ``find_video_file``).

    ``min_video_size`` is the optional advertised-size floor that lets discovery
    recurse past a root-level job-start stub into the subfolder holding the real
    file (#282); see ``find_video_file``. ``0`` (default) disables the floor.
    """
    settings = _read_settings(settings_getter)
    video_path = find_video_file(
        folder_path,
        _settings=settings,
        title_hint=title_hint,
        min_video_size=min_video_size,
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
