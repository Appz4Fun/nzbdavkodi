# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Conservative grouping for duplicate releases usable as fallback streams."""

import hashlib
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import xbmc
import xbmcaddon

_SAFE_JOB_RE = re.compile(r"^[A-Za-z0-9._ \[\]-]+$")
_CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$")
_SIZE_TOLERANCE_RATIO = 0.002
_FINGERPRINT_OFFSETS = (0.0, 0.25, 0.5, 0.75, 0.98)
_FINGERPRINT_BYTES = 4096

_MIN_SIZE_TOLERANCE = 8 * 1024 * 1024
_MAX_FALLBACKS = 5
_ALLOWED_STREAM_SCHEMES = frozenset(("http", "https"))


def _setting_bool(addon, key, default=False):
    """Read a Kodi boolean setting with a safe fallback."""
    raw = addon.getSetting(key)
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off", ""):
            return False
    return bool(default)


def _setting_int(addon, key, default=0):
    """Read a Kodi integer setting with a safe fallback."""
    raw = addon.getSetting(key)
    try:
        return int(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        return int(default)


def _valid_stream_url(url):
    """Return True for HTTP(S) stream URLs that are safe to probe."""
    return _validated_probe_url(url) is not None


def _split_http_url(url):
    """Parse a URL and return parts only for simple HTTP(S) URLs."""
    if not isinstance(url, str) or any(ord(char) < 0x20 for char in url):
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in _ALLOWED_STREAM_SCHEMES:
            return False
        if not parts.netloc or not parts.hostname:
            return False
        if parts.username or parts.password:
            return False
        # Accessing .port validates that any explicit port is numeric/ranged.
        _port = parts.port
    except ValueError:
        return False
    return parts


def _origin_key(parts):
    scheme = parts.scheme.lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parts.hostname.lower(), port


def _path_is_under_base(path, base_path):
    prefix = (base_path or "").rstrip("/")
    if not prefix:
        return True
    return path == prefix or path.startswith(prefix + "/")


def _configured_stream_bases():
    """Return configured WebDAV/nzbdav bases that fallback probes may hit."""
    try:
        addon = xbmcaddon.Addon()
        raw_bases = (
            addon.getSetting("webdav_url"),
            addon.getSetting("nzbdav_url"),
        )
    except Exception:  # pylint: disable=broad-except
        raw_bases = ()

    bases = []
    for raw_base in raw_bases:
        parts = _split_http_url(str(raw_base or "").rstrip("/"))
        if parts:
            bases.append(parts)
    return bases


def _validated_probe_url(url):
    """Return a probe URL constrained to the configured WebDAV origin."""
    candidate = _split_http_url(url)
    if not candidate:
        return None
    for base in _configured_stream_bases():
        if _origin_key(candidate) != _origin_key(base):
            continue
        if not _path_is_under_base(candidate.path or "/", base.path):
            continue
        return urlunsplit(
            (
                base.scheme.lower(),
                base.netloc,
                candidate.path or "/",
                candidate.query,
                "",
            )
        )
    return None


def _normalize_title(value):
    """Normalize release titles for conservative duplicate grouping."""
    if not isinstance(value, str):
        return ""
    normalized = re.sub(r"[\W_]+", " ", value.lower())
    return " ".join(normalized.split())


def _size_int(value):
    """Return a positive integer size, or None for missing/invalid values."""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


def _same_size(left, right):
    """Return True when two release sizes are present and within tolerance."""
    left_size = _size_int(left)
    right_size = _size_int(right)
    if left_size is None or right_size is None:
        return False
    tolerance = max(
        _MIN_SIZE_TOLERANCE,
        int(max(left_size, right_size) * _SIZE_TOLERANCE_RATIO),
    )
    return abs(left_size - right_size) <= tolerance


def _quality_key(result):
    """Return the conservative duplicate-grouping quality key for a result."""
    meta = result.get("_meta") if isinstance(result, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    return (
        _normalize_title(result.get("title", "") if isinstance(result, dict) else ""),
        str(meta.get("resolution", "") or "").strip().lower(),
        str(meta.get("quality", "") or "").strip().lower(),
        str(meta.get("codec", "") or "").strip().lower(),
        str(meta.get("group", "") or "").strip().lower(),
        str(meta.get("container", "") or "").strip().lower(),
    )


def _fallback_settings():
    """Return (enabled, max_candidates) from Kodi settings."""
    addon = xbmcaddon.Addon()
    enabled = _setting_bool(addon, "fallback_streams_enabled", False)
    max_candidates = _setting_int(addon, "fallback_streams_max", 2)
    if max_candidates < 0 or max_candidates > _MAX_FALLBACKS:
        xbmc.log(
            "NZB-DAV: fallback_streams_max={} clamped to 0..{}".format(
                max_candidates, _MAX_FALLBACKS
            ),
            xbmc.LOGWARNING,
        )
    return enabled, max(0, min(max_candidates, _MAX_FALLBACKS))


def attach_fallback_candidates(results):
    """Attach duplicate fallback candidates to each result in-place.

    Every result receives ``_fallback_candidates``. When fallback streams are
    disabled, the cap is zero, or a result cannot be conservatively matched,
    the attached list is empty.
    """
    for result in results:
        result["_fallback_candidates"] = []

    enabled, max_candidates = _fallback_settings()
    if not enabled or max_candidates <= 0:
        return results

    groups = {}
    for result in results:
        link = result.get("link", "")
        if not isinstance(link, str) or not link.strip():
            continue
        key = _quality_key(result)
        if not key[0]:
            continue
        groups.setdefault(key, []).append(result)

    for group in groups.values():
        if len(group) < 2:
            continue
        for result in group:
            link = result.get("link", "")
            candidates = []
            seen_links = {link}
            for candidate in group:
                candidate_link = candidate.get("link", "")
                if (
                    candidate is result
                    or not candidate_link
                    or candidate_link in seen_links
                ):
                    continue
                if not _same_size(result.get("size"), candidate.get("size")):
                    continue
                candidates.append(candidate)
                seen_links.add(candidate_link)
                if len(candidates) >= max_candidates:
                    break
            result["_fallback_candidates"] = candidates

    return results


def build_fallback_job_name(title, nzb_url, index):
    """Return a stable, traceable nzbdav job name for a fallback candidate."""
    clean_title = title if isinstance(title, str) else ""
    clean_title = re.sub(r"[^A-Za-z0-9._ -]+", " ", clean_title)
    clean_title = " ".join(clean_title.split())[:180].strip()
    if not clean_title:
        clean_title = "fallback"

    digest = hashlib.sha256(str(nzb_url).encode("utf-8")).hexdigest()[:8]
    job_name = "{} [fallback-{}-{}]".format(clean_title, index, digest)
    if not _SAFE_JOB_RE.match(job_name):
        job_name = re.sub(r"[^A-Za-z0-9._ -]+", " ", job_name)
        job_name = " ".join(job_name.split())
    return job_name


def build_prepare_fallback_payload(fallback_jobs):
    """Build the service prepare manifest payload for fallback jobs."""
    payload = []
    for job in fallback_jobs:
        nzo_id = job.get("nzo_id") if isinstance(job, dict) else None
        if not nzo_id:
            continue
        payload.append(
            {
                "title": job.get("title", ""),
                "nzb_url": job.get("nzb_url", ""),
                "job_name": job.get("job_name", ""),
                "nzo_id": nzo_id,
                "stream_url": job.get("stream_url") or "",
                "stream_headers": job.get("stream_headers") or {},
                "content_length": job.get("content_length") or 0,
            }
        )
    return payload


def fingerprint_ranges(content_length):
    """Return byte ranges used to prove two stream URLs expose the same file."""
    if content_length <= 0:
        return []
    if content_length <= _FINGERPRINT_BYTES:
        return [(0, content_length - 1)]

    ranges = []
    for ratio in _FINGERPRINT_OFFSETS:
        start = int(content_length * ratio)
        start = min(start, max(0, content_length - _FINGERPRINT_BYTES))
        end = min(content_length - 1, start + _FINGERPRINT_BYTES - 1)
        pair = (start, end)
        if pair not in ranges:
            ranges.append(pair)
    return ranges


def fetch_content_length(url, auth_header, timeout=2):
    """Return Content-Length for a WebDAV stream URL, or 0."""
    probe_url = _validated_probe_url(url)
    if not probe_url:
        return 0
    req = Request(probe_url, method="HEAD")
    if auth_header:
        req.add_header("Authorization", auth_header)
    try:
        # nosemgrep
        with urlopen(req, timeout=timeout) as resp:  # nosec B310
            return int(resp.headers.get("Content-Length", "0") or 0)
    except (HTTPError, URLError, OSError, TypeError, ValueError):
        return 0


def _content_range_matches_request(content_range, start, end, content_length=0):
    if not isinstance(content_range, str):
        return False
    match = _CONTENT_RANGE_RE.match(content_range.strip())
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


def fetch_range_digest(url, auth_header, start, end, timeout=2, content_length=0):
    """Read a byte range and return a SHA-256 digest of the returned bytes."""
    probe_url = _validated_probe_url(url)
    if not probe_url:
        return None
    req = Request(probe_url)
    if auth_header:
        req.add_header("Authorization", auth_header)
    req.add_header("Range", "bytes={}-{}".format(start, end))
    try:
        # nosemgrep
        with urlopen(req, timeout=timeout) as resp:  # nosec B310
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 206:
                return None
            if not _content_range_matches_request(
                resp.headers.get("Content-Range"), start, end, content_length
            ):
                return None
            body = resp.read(end - start + 1)
    except (HTTPError, URLError, OSError, ValueError):
        return None
    if len(body) != end - start + 1:
        return None
    return hashlib.sha256(body).hexdigest()
