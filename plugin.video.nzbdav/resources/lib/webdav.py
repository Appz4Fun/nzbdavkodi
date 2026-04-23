# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""WebDAV availability checker for nzbdav streams."""

import base64
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import xbmc


def _get_settings():
    import xbmcaddon

    addon = xbmcaddon.Addon()
    return {
        "webdav_url": addon.getSetting("webdav_url").rstrip("/"),
        "nzbdav_url": addon.getSetting("nzbdav_url").rstrip("/"),
        "username": addon.getSetting("webdav_username"),
        "password": addon.getSetting("webdav_password"),
    }


def _http_head(
    url, username="", password=""
):  # nosec B107 — empty default = "no auth", not a real password
    req = Request(url, method="HEAD")
    if username:
        credentials = "{}:{}".format(username, password)
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
        req.add_header("Authorization", "Basic {}".format(encoded))
    try:
        with urlopen(
            req, timeout=10
        ) as resp:  # nosec B310 nosemgrep — URL from user's configured WebDAV setting
            return resp.getcode()
    except HTTPError as e:
        return e.code


def probe_webdav_reachable(monitor=None, max_retries=1, retry_delay=1):
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
    settings = _get_settings()
    base = settings["webdav_url"] or settings["nzbdav_url"]
    # Allow differently-routed nzbdav instances to override the content
    # root; default to "content" which matches the standard nzbdav layout.
    try:
        import xbmcaddon

        raw = xbmcaddon.Addon().getSetting("webdav_content_root")
        content_root = raw.strip("/") if isinstance(raw, str) and raw else "content"
    except Exception:  # pylint: disable=broad-except
        content_root = "content"
    url = "{}/{}/".format(base.rstrip("/"), content_root or "content")
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


def find_video_file(folder_path, _depth=0, _visited=None):
    """Browse a WebDAV folder and find the largest video file.

    Args:
        folder_path: WebDAV folder path to scan (may be absolute or relative).
        _depth: Internal recursion depth counter (used to cap traversal).
        _visited: Internal set of already-scanned paths; catches a hostile
            or misconfigured server that returns its parent (or itself) as
            a child and would otherwise recurse until the depth cap.

    Returns:
        The WebDAV href path of the largest video file found, typically an
        absolute server path beginning with "/", or None when no video is
        located or an error occurs.

    Side effects:
        Reads WebDAV settings from Kodi via xbmcaddon.Addon().
        Issues a PROPFIND request at the target path and, if no video is found
        at that level, recurses into subdirectories up to two levels deep
        (three total levels including the starting folder).
        Logs discovered files, recursion steps, and errors to the Kodi log.
    """
    import xml.etree.ElementTree as ET  # nosec B405 — parsing trusted WebDAV server response

    if _depth > 2:
        return None

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

    settings = _get_settings()
    base = settings["webdav_url"] or settings["nzbdav_url"]
    username = settings["username"]
    password = settings["password"]

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
        with urlopen(
            req, timeout=10
        ) as resp:  # nosec B310 nosemgrep — URL from user's configured WebDAV setting
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
        subdirs = []

        from urllib.parse import urlparse

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
                # Reject protocol-relative URLs ("//host/path") unless they
                # match the configured server; urlparse().scheme is empty
                # for these and we'd otherwise treat them as a path.
                if href_text.startswith("//"):
                    base_host = urlparse(url).netloc
                    if parsed_href_obj.netloc != base_host:
                        xbmc.log(
                            "NZB-DAV: Rejecting cross-host href '{}'".format(href_text),
                            xbmc.LOGWARNING,
                        )
                        continue
                    href_path = parsed_href_obj.path
                elif parsed_href_obj.scheme:
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
                request_path = urlparse(url).path.rstrip("/")
                if href_path.rstrip("/") != request_path:
                    subdirs.append(href_path.rstrip("/") + "/")
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

            if size > best_size:
                best_size = size
                best_file = href_path

        if best_file:
            file_path = best_file
            xbmc.log(
                "NZB-DAV: Found video file: {} ({} bytes)".format(file_path, best_size),
                xbmc.LOGINFO,
            )
            return file_path

        # No video found at this level, recurse into subdirectories
        for subdir in subdirs:
            xbmc.log(
                "NZB-DAV: No video at depth {}, checking subfolder: {}".format(
                    _depth, subdir
                ),
                xbmc.LOGDEBUG,
            )
            result = find_video_file(subdir, _depth + 1, _visited)
            if result:
                return result

        return None
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Error browsing WebDAV folder '{}': {} ({})".format(
                folder_path, e, type(e).__name__
            ),
            xbmc.LOGERROR,
        )
        return None


def get_webdav_stream_url_for_path(file_path):
    """Build a stream URL and auth headers for a full WebDAV path.

    Returns (url, headers_dict) where headers_dict may be empty if no auth.
    """
    settings = _get_settings()

    # file_path is already URL-encoded from PROPFIND
    base = settings["webdav_url"] or settings["nzbdav_url"]
    # Normalize base/file-path boundary so we never produce "host" + "path"
    # (missing slash) or "host//" + "/path" (double slash). The PROPFIND
    # response is *supposed* to hand us an absolute path with a leading
    # slash, but nothing enforces that on the server side.
    url = "{}/{}".format(base.rstrip("/"), file_path.lstrip("/"))
    headers = _build_auth_headers(settings["username"], settings["password"])
    return url, headers


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
