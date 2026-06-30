# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""HTTP probe, manifest cache, origin allow-list, and fingerprint helpers."""

import copy
import hashlib
import os
import posixpath
import re
import threading
import time
from collections import namedtuple
from functools import lru_cache
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request

try:
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover - Kodi installs may not bundle defusedxml
    from xml.etree import ElementTree as ET

import resources.lib.fallback_streams as _fs

_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")


_FINGERPRINT_SAMPLE_COUNT = 100


_FINGERPRINT_SMALL_SAMPLE_COUNT = 20


_FINGERPRINT_DENSE_SAMPLE_MIN_BYTES = 1024 * 1024 * 1024


_FINGERPRINT_BYTES = 4096


_FALLBACK_MANIFEST_CACHE_TTL_SECONDS = 10.0


_FALLBACK_MANIFEST_CACHE_MAX_ENTRIES = 128


_ALLOWED_STREAM_SCHEMES = frozenset(("http", "https"))


_ADDON_SETTINGS_SCHEMA = (
    "special://home/addons/plugin.video.nzbdav/resources/settings.xml"
)


_PrecomputedProbeBase = namedtuple("_PrecomputedProbeBase", "parts origin path")


_FALLBACK_MANIFEST_CACHE = {}


_FALLBACK_MANIFEST_CACHE_LOCK = threading.Lock()


def _fallback_manifest_cache_now():
    return time.monotonic()


def clear_fallback_manifest_cache():
    """Clear the short-lived fallback manifest cache."""
    with _fs._FALLBACK_MANIFEST_CACHE_LOCK:
        _fs._FALLBACK_MANIFEST_CACHE.clear()


def _fallback_manifest_cache_key(link):
    return _fs.fetch_nzb_video_manifest, link


def _copy_manifest(manifest):
    return copy.deepcopy(manifest) if isinstance(manifest, dict) else manifest


def _cached_fallback_manifest(link, now):
    key = _fs._fallback_manifest_cache_key(link)
    with _fs._FALLBACK_MANIFEST_CACHE_LOCK:
        cached = _fs._FALLBACK_MANIFEST_CACHE.get(key)
        if cached is None:
            return None
        expires_at, manifest = cached
        if expires_at <= now:
            _fs._FALLBACK_MANIFEST_CACHE.pop(key, None)
            return None
        return _fs._copy_manifest(manifest)


def _store_fallback_manifest(link, manifest, now):
    if not isinstance(manifest, dict):
        return
    key = _fs._fallback_manifest_cache_key(link)
    expires_at = now + _fs._FALLBACK_MANIFEST_CACHE_TTL_SECONDS
    with _fs._FALLBACK_MANIFEST_CACHE_LOCK:
        if (
            len(_fs._FALLBACK_MANIFEST_CACHE)
            >= _fs._FALLBACK_MANIFEST_CACHE_MAX_ENTRIES
        ):
            expired = [
                cache_key
                for cache_key, (cached_expires_at, _manifest) in (
                    _fs._FALLBACK_MANIFEST_CACHE.items()
                )
                if cached_expires_at <= now
            ]
            for cache_key in expired:
                _fs._FALLBACK_MANIFEST_CACHE.pop(cache_key, None)
            if (
                len(_fs._FALLBACK_MANIFEST_CACHE)
                >= _fs._FALLBACK_MANIFEST_CACHE_MAX_ENTRIES
            ):
                oldest_key = min(
                    _fs._FALLBACK_MANIFEST_CACHE,
                    key=lambda cache_key: _fs._FALLBACK_MANIFEST_CACHE[cache_key][0],
                )
                _fs._FALLBACK_MANIFEST_CACHE.pop(oldest_key, None)
        _fs._FALLBACK_MANIFEST_CACHE[key] = (expires_at, _fs._copy_manifest(manifest))


def _fetch_fallback_manifest(link):
    now = _fs._fallback_manifest_cache_now()
    manifest = _fs._cached_fallback_manifest(link, now)
    if isinstance(manifest, dict):
        return manifest
    try:
        manifest = _fs.fetch_nzb_video_manifest(
            link, health_check=_fs._manifest_candidate_message_ids_are_healthy
        )
    except Exception:  # pylint: disable=broad-except
        manifest = _fs._manifest_error("fetch_error")
    if not isinstance(manifest, dict):
        manifest = _fs._manifest_error("fetch_error")
    _fs._store_fallback_manifest(link, manifest, _fs._fallback_manifest_cache_now())
    return _fs._copy_manifest(manifest)


def _setting_bool(addon, key, default=False):
    """Read a Kodi boolean setting with a safe fallback."""
    raw = addon.getSetting(key)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized == "":
            return bool(default)
        if normalized in ("false", "0", "no", "off"):
            return False
    return bool(default)


def _setting_int(addon, key, default=0):
    """Read a Kodi integer setting with a safe fallback."""
    raw = addon.getSetting(key)
    try:
        return int(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        return int(default)


def _split_http_url(url):
    """Parse a URL and return parts only for simple HTTP(S) URLs.

    Returns the parsed ``SplitResult`` on accept, ``None`` on reject —
    NOT ``False``. Returning a bool here was a contract drift; callers
    use both truthiness (``if parts:``) and identity checks
    (``parts is None``) and the latter silently miss-classified rejected
    URLs as valid.
    """
    if not isinstance(url, str) or any(ord(char) < 0x20 for char in url):
        return None
    # IDN homograph / RTL-override / full-width digit attacks: any byte
    # > 0x7F could let an eyeball-identical hostname slip past the
    # configured-origin allow-list. Reject high-bit input outright;
    # operators who need IDN should pre-IDNA-encode the stream base.
    if any(ord(char) > 0x7F for char in url):
        return None
    try:
        parts = urlsplit(url)
        if not _fs._http_url_parts_are_valid(parts):
            return None
    except ValueError:
        return None
    return parts


def _http_url_parts_are_valid(parts):
    """Return whether parsed URL parts are a simple, safe HTTP(S) location.

    Accessing ``parts.port`` may raise ``ValueError`` for a malformed port;
    that propagates to the caller's ``except ValueError`` exactly as before.
    """
    if parts.scheme.lower() not in _fs._ALLOWED_STREAM_SCHEMES:
        return False
    if not parts.netloc or not parts.hostname:
        return False
    # urlsplit accepts whitespace inside netloc (e.g. "http:// host /")
    # — `parts.hostname` silently strips it, masking a malformed URL
    # that would never resolve. Reject any whitespace in the raw
    # netloc explicitly.
    if any(ch.isspace() for ch in parts.netloc):
        return False
    if parts.username or parts.password:
        return False
    # Accessing .port validates that any explicit port is numeric/ranged.
    _port = parts.port
    return True


def _origin_key(parts):
    """Return the normalized origin tuple for parsed URL parts."""
    scheme = parts.scheme.lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parts.hostname.lower(), port


def _canonical_probe_path(path):
    """Return a decoded, normalized absolute path for allow-list checks.

    Percent-encoded traversal (``/dav/%2e%2e/admin``) survives a raw-prefix
    match but many WebDAV servers decode/normalize it back outside the base
    path, which would forward the Authorization header to an escaped path. Decode
    the path and reject anything containing a ``..`` segment or a backslash so the
    containment check sees what the server will actually resolve.
    """
    try:
        decoded = unquote(path or "/", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if "\\" in decoded or any(part == ".." for part in decoded.split("/")):
        return None
    normalized = posixpath.normpath(decoded)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def _path_is_under_base(path, base_path):
    """Return whether a URL path is within the configured base path."""
    path = _canonical_probe_path(path)
    base_path = _canonical_probe_path(base_path or "/")
    if path is None or base_path is None:
        return False
    prefix = base_path.rstrip("/")
    if not prefix:
        return True
    return path == prefix or path.startswith(prefix + "/")


def _schema_setting_default(setting_id):
    """Read a default from resources/settings.xml without Kodi settings APIs."""
    try:
        import xbmcvfs

        schema_path = xbmcvfs.translatePath(_fs._ADDON_SETTINGS_SCHEMA)
    except Exception:  # pylint: disable=broad-except
        schema_path = ""
    if not isinstance(schema_path, str):
        schema_path = ""
    candidate_paths = []
    if schema_path:
        candidate_paths.append(schema_path)
    candidate_paths.append(
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "settings.xml"))
    )
    for path in candidate_paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        default = _fs._setting_default_from_root(root, setting_id)
        if default is not None:
            return default
    return ""


def _setting_default_from_root(root, setting_id):
    """Return a setting's default value from a parsed settings.xml root, or None."""
    for setting in root.findall(".//setting"):
        if setting.get("id") == setting_id:
            return setting.get("default", "") or ""
    return None


def _configured_stream_bases():
    """Return configured WebDAV/nzbdav bases that fallback probes may hit.

    Reads from settings.xml on disk first — calling addon.getSetting()
    from a background prevalidation thread (script-mode invocation)
    SIGSEGVs in the Kodi C++ binding the same way webdav.py /
    stream_proxy.py did before we switched their reads to disk."""
    raw_bases = ()
    try:
        from resources.lib.router import _get_script_setting

        raw_bases = (
            _get_script_setting("webdav_url", ""),
            _get_script_setting("nzbdav_url", ""),
        )
    except Exception:  # pylint: disable=broad-except
        raw_bases = ()
    # If the script-mode settings.xml read returned nothing (no profile
    # yet, or addon hasn't saved settings), fall back to the addon API.
    # In script-mode this is the very SIGSEGV path we're trying to avoid,
    # but with raw_bases populated from disk above we never get here in
    # the script-mode case — only on a fresh GUI invocation where the
    # binding is safe.
    if not any(raw_bases):
        try:
            addon = _fs.xbmcaddon.Addon("plugin.video.nzbdav")
            raw_bases = (
                addon.getSetting("webdav_url"),
                addon.getSetting("nzbdav_url"),
            )
        except Exception:  # pylint: disable=broad-except
            raw_bases = ()
    if not any(raw_bases):
        raw_bases = (
            _fs._schema_setting_default("webdav_url"),
            _fs._schema_setting_default("nzbdav_url"),
        )

    bases = []
    for raw_base in raw_bases:
        # .strip() (not just .rstrip("/")): a stray trailing space in the
        # configured nzbdav_url/webdav_url (a common copy-paste artifact) lands
        # inside the netloc, and _split_http_url rejects any whitespace there —
        # silently emptying the probe-base allow-list so fallback content-length
        # probes all return 0 and byte-identical fallbacks fail validation.
        parts = _fs._split_http_url(str(raw_base or "").strip().rstrip("/"))
        if parts:
            bases.append(parts)
    return bases


def configured_stream_probe_bases():
    """Return configured stream bases with reusable origin/path checks."""
    return tuple(
        _fs._PrecomputedProbeBase(parts, _fs._origin_key(parts), parts.path or "/")
        for parts in _fs._configured_stream_bases()
    )


def _probe_base_components(base):
    """Return parsed URL parts plus cached origin/path data for one base."""
    if isinstance(base, _fs._PrecomputedProbeBase):
        return base.parts, base.origin, base.path
    return base, _fs._origin_key(base), base.path or "/"


def _validated_probe_url(url, probe_bases=None):
    """Return a probe URL constrained to the configured WebDAV origin."""
    candidate = _fs._split_http_url(url)
    if not candidate:
        return None
    bases = _fs._configured_stream_bases() if probe_bases is None else probe_bases
    candidate_origin = _fs._origin_key(candidate)
    for base in bases:
        base_parts, base_origin, base_path = _fs._probe_base_components(base)
        if candidate_origin != base_origin:
            continue
        if not _fs._path_is_under_base(candidate.path or "/", base_path):
            continue
        return urlunsplit(
            (
                base_parts.scheme.lower(),
                base_parts.netloc,
                candidate.path or "/",
                candidate.query,
                "",
            )
        )
    return None


@lru_cache(maxsize=256)
def _cached_validated_probe_url(url, probe_bases):
    """Return a configured-origin probe URL for immutable base snapshots."""
    return _fs._validated_probe_url(url, probe_bases=probe_bases)


def _validated_probe_url_for_fetch(url, probe_bases=None):
    """Return a probe URL, caching validation when bases are immutable."""
    if probe_bases is None:
        return _fs._validated_probe_url(url)
    try:
        return _fs._cached_validated_probe_url(url, tuple(probe_bases))
    except TypeError:
        return _fs._validated_probe_url(url, probe_bases=probe_bases)


def fingerprint_ranges(content_length):
    """Return byte ranges used to prove two stream URLs expose the same file."""
    return list(_fs._fingerprint_ranges_for_length(content_length))


def _fingerprint_ranges_for_length(content_length):
    """Return immutable fingerprint ranges for a content length."""
    if content_length <= 0:
        return ()
    if content_length <= _fs._FINGERPRINT_BYTES:
        return ((0, content_length - 1),)

    sample_count = _fs._fingerprint_sample_count(content_length)
    if content_length <= sample_count * _fs._FINGERPRINT_BYTES:
        ranges = []
        start = 0
        while start < content_length:
            end = min(content_length - 1, start + _fs._FINGERPRINT_BYTES - 1)
            ranges.append((start, end))
            start += _fs._FINGERPRINT_BYTES
        return tuple(ranges)

    max_start = content_length - _fs._FINGERPRINT_BYTES
    starts = {0, max_start}
    counter = 0
    while len(starts) < sample_count:
        digest = hashlib.sha256(
            "{}:{}".format(content_length, counter).encode("utf-8")
        ).digest()
        starts.add(int.from_bytes(digest[:8], "big") % (max_start + 1))
        counter += 1
    return tuple(
        (start, start + _fs._FINGERPRINT_BYTES - 1) for start in sorted(starts)
    )


def _fingerprint_sample_count(content_length):
    """Return how many sampled ranges should prove this stream length."""
    if content_length >= _fs._FINGERPRINT_DENSE_SAMPLE_MIN_BYTES:
        return _fs._FINGERPRINT_SAMPLE_COUNT
    return _fs._FINGERPRINT_SMALL_SAMPLE_COUNT


def fetch_content_length(url, auth_header, timeout=10, probe_bases=None):
    """Return Content-Length for a WebDAV stream URL, or 0."""
    probe_url = _fs._validated_probe_url_for_fetch(url, probe_bases=probe_bases)
    if not probe_url:
        return 0
    req = Request(probe_url, method="HEAD")
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        with _fs._no_redirect_urlopen(req, timeout=timeout) as resp:
            return int(resp.headers.get("Content-Length", "0") or 0)
    except (HTTPError, URLError, OSError, TypeError, ValueError):
        return 0


def _content_range_matches_request(content_range, start, end, content_length=0):
    """Return whether a Content-Range header matches a requested range."""
    if not isinstance(content_range, str):
        return False
    match = _fs._CONTENT_RANGE_RE.match(content_range.strip())
    if not match:
        return False
    try:
        if int(match.group(1)) != start or int(match.group(2)) != end:
            return False
        if content_length:
            total = match.group(3)
            return total != "*" and int(total) == int(content_length)
        return True
    except ValueError:
        return False


def _normalized_range_content_length(start, end, content_length):
    """Return a validated content_length for a range request, or None if invalid."""
    if not (isinstance(start, int) and isinstance(end, int)):
        return None
    try:
        content_length = int(content_length or 0)
    except (TypeError, ValueError):
        return None
    if not _fs._range_bounds_valid(start, end, content_length):
        return None
    return content_length


def _range_bounds_valid(start, end, content_length):
    """Return whether a byte range and its declared content length are coherent."""
    if start < 0 or end < start or content_length < 0:
        return False
    return not (content_length and end >= content_length)


def _read_validated_range(req, start, end, content_length, timeout):
    """Open ``req`` and return the verified range body, or None on any mismatch."""
    try:
        with _fs._no_redirect_urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 206:
                return None
            if not _fs._content_range_matches_request(
                resp.headers.get("Content-Range"), start, end, content_length
            ):
                return None
            body = resp.read(end - start + 1)
    except (HTTPError, URLError, OSError, ValueError):
        return None
    if len(body) != end - start + 1:
        return None
    return body


def fetch_range_bytes(
    url,
    auth_header,
    start,
    end,
    timeout=10,
    content_length=0,
    probe_bases=None,
):
    """Read a validated byte range from a configured WebDAV stream URL."""
    content_length = _fs._normalized_range_content_length(start, end, content_length)
    if content_length is None:
        return None

    probe_url = _fs._validated_probe_url_for_fetch(url, probe_bases=probe_bases)
    if not probe_url:
        return None
    req = Request(probe_url)
    if auth_header:
        req.add_header("Authorization", auth_header)
    req.add_header("Range", "bytes={}-{}".format(start, end))
    return _fs._read_validated_range(req, start, end, content_length, timeout)


def fetch_range_digest(
    url,
    auth_header,
    start,
    end,
    timeout=10,
    content_length=0,
    probe_bases=None,
):
    """Read a byte range and return a SHA-256 digest of the returned bytes."""
    body = _fs.fetch_range_bytes(
        url,
        auth_header,
        start,
        end,
        timeout=timeout,
        content_length=content_length,
        probe_bases=probe_bases,
    )
    if body is None:
        return None
    return hashlib.sha256(body).hexdigest()
