# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""WebDAV availability checker for nzbdav streams."""

import base64
import time
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


def _http_head(url, username="", password=""):
    req = Request(url, method="HEAD")
    if username:
        credentials = "{}:{}".format(username, password)
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
        req.add_header("Authorization", "Basic {}".format(encoded))
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.getcode()
    except HTTPError as e:
        return e.code


def build_webdav_url(filename):
    settings = _get_settings()
    base = settings["webdav_url"] or settings["nzbdav_url"]
    return "{}/{}".format(base, quote(filename, safe=""))


def get_webdav_stream_url(filename):
    settings = _get_settings()
    base = settings["webdav_url"] or settings["nzbdav_url"]
    url = "{}/{}".format(base, quote(filename, safe=""))
    headers = _build_auth_headers(settings["username"], settings["password"])
    return url, headers


def check_file_available(filename):
    settings = _get_settings()
    url = build_webdav_url(filename)
    xbmc.log("NZB-DAV: WebDAV check URL: {}".format(url), xbmc.LOGDEBUG)
    try:
        status = _http_head(url, settings["username"], settings["password"])
        available = status == 200
        xbmc.log(
            "NZB-DAV: WebDAV check '{}': status={} available={}".format(
                filename, status, available
            ),
            xbmc.LOGDEBUG,
        )
        return available
    except Exception as e:
        xbmc.log(
            "NZB-DAV: WebDAV check failed for '{}': {}".format(filename, e),
            xbmc.LOGERROR,
        )
        return False


def check_file_available_with_retry(filename, max_retries=3, retry_delay=2):
    """Check WebDAV file availability with retry logic.

    Args:
        filename: The filename to check on the WebDAV server.
        max_retries: Maximum number of retries after a connection error; a
            connection error can trigger up to max_retries + 1 HEAD requests.
        retry_delay: Seconds to sleep between connection error retries.

    Returns:
        Tuple of (available, error_type). available is True only when the HEAD
        request returns 200. error_type is:
        - None when the file is available.
        - "not_found" for 404 or any other non-2xx/non-auth/server status.
        - "auth_failed" for 401/403 responses.
        - "server_error" for 5xx responses.
        - "connection_error" when all retries fail due to network errors.

    Side effects:
        Reads WebDAV credentials from Kodi via xbmcaddon.Addon().
        Performs one or more HTTP HEAD requests to the WebDAV server.
        Sleeps for retry_delay seconds between connection error retries.
        Logs availability checks and failures to the Kodi log.
    """
    settings = _get_settings()
    url = build_webdav_url(filename)
    xbmc.log("NZB-DAV: WebDAV check with retry URL: {}".format(url), xbmc.LOGDEBUG)

    attempt = 0
    while attempt <= max_retries:
        try:
            status = _http_head(url, settings["username"], settings["password"])

            if status == 200:
                xbmc.log(
                    "NZB-DAV: WebDAV file '{}' is available".format(filename),
                    xbmc.LOGINFO,
                )
                return True, None

            if status == 404:
                xbmc.log(
                    "NZB-DAV: WebDAV file '{}' not found (404)".format(filename),
                    xbmc.LOGDEBUG,
                )
                return False, "not_found"

            if status in (401, 403):
                xbmc.log(
                    "NZB-DAV: WebDAV auth failed for '{}' (status={})".format(
                        filename, status
                    ),
                    xbmc.LOGERROR,
                )
                return False, "auth_failed"

            if status >= 500:
                xbmc.log(
                    "NZB-DAV: WebDAV server error for '{}' (status={})".format(
                        filename, status
                    ),
                    xbmc.LOGERROR,
                )
                return False, "server_error"

            # Other non-200 status: treat as not found
            xbmc.log(
                "NZB-DAV: WebDAV unexpected status {} for '{}'".format(
                    status, filename
                ),
                xbmc.LOGDEBUG,
            )
            return False, "not_found"

        except Exception as e:
            attempt += 1
            if attempt > max_retries:
                xbmc.log(
                    "NZB-DAV: WebDAV connection error for '{}' "
                    "after {} attempts: {} ({})".format(
                        filename, max_retries + 1, e, type(e).__name__
                    ),
                    xbmc.LOGERROR,
                )
                return False, "connection_error"

            xbmc.log(
                "NZB-DAV: WebDAV connection error for '{}'"
                " (attempt {}/{}): {} ({})".format(
                    filename, attempt, max_retries, e, type(e).__name__
                ),
                xbmc.LOGDEBUG,
            )
            time.sleep(retry_delay)

    return False, "connection_error"


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
    # _get_settings() already rstrips "/" on both URL settings
    # (webdav.py:20-21), so this rstrip is defense-in-depth against a
    # future refactor that forgets to.
    url = "{}/content/".format(base.rstrip("/"))
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


def find_video_file(folder_path, _depth=0):
    """Browse a WebDAV folder and find the largest video file.

    Args:
        folder_path: WebDAV folder path to scan (may be absolute or relative).
        _depth: Internal recursion depth counter (used to cap traversal).

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
    import xml.etree.ElementTree as ET

    if _depth > 2:
        return None

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
    if username:
        credentials = "{}:{}".format(username, password)
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
        req.add_header("Authorization", "Basic {}".format(encoded))

    VIDEO_EXTENSIONS = (".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov")

    try:
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")

        # Parse the PROPFIND XML response
        root = ET.fromstring(body)  # nosec B314 — trusted WebDAV server response
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
                # For relative paths (no scheme), use href_text as the path directly
                if parsed_href_obj.scheme:
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
                    pass

            if size >= best_size:
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
            result = find_video_file(subdir, _depth + 1)
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
    base = settings["webdav_url"] or settings["nzbdav_url"]

    # file_path is already URL-encoded from PROPFIND
    url = "{}{}".format(base, file_path)
    headers = _build_auth_headers(settings["username"], settings["password"])
    return url, headers


def _build_auth_headers(username, password):
    """Build HTTP Basic Auth headers dict. Returns empty dict if no auth."""
    if username:
        credentials = "{}:{}".format(username, password)
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
        return {"Authorization": "Basic {}".format(encoded)}
    return {}


def check_file_in_folder(folder_path):
    """Check if a video file exists in a WebDAV folder.

    Returns (file_path, None) if found, (None, error_type) if not.
    """
    video_path = find_video_file(folder_path)
    if video_path:
        return video_path, None
    return None, "not_found"


def validate_stream(filename):
    """Verify the WebDAV file supports range requests (seekable streaming).

    Returns True if the stream supports seeking, False otherwise.
    """
    settings = _get_settings()
    url = build_webdav_url(filename)
    username = settings["username"]
    password = settings["password"]

    req = Request(url, method="HEAD")
    if username:
        credentials = "{}:{}".format(username, password)
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
        req.add_header("Authorization", "Basic {}".format(encoded))
    req.add_header("Range", "bytes=0-0")

    try:
        with urlopen(req, timeout=10) as resp:
            # 206 Partial Content means range requests are supported
            # 200 OK means the server ignores range (still playable but no seeking)
            status = resp.getcode()
            accept_ranges = resp.headers.get("Accept-Ranges", "")
            xbmc.log(
                "NZB-DAV: Stream validation for '{}': "
                "status={} Accept-Ranges={}".format(filename, status, accept_ranges),
                xbmc.LOGDEBUG,
            )
            return status in (200, 206)
    except HTTPError as e:
        xbmc.log(
            "NZB-DAV: Stream validation failed for '{}': HTTP {}".format(
                filename, e.code
            ),
            xbmc.LOGERROR,
        )
        return e.code in (200, 206)
    except Exception as e:
        xbmc.log(
            "NZB-DAV: Stream validation error for '{}': {}".format(filename, e),
            xbmc.LOGERROR,
        )
        return False
