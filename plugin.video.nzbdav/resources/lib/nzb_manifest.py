# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZB manifest inspection for fallback grouping."""

import hashlib
import re
import xml.etree.ElementTree as ET
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

_MAX_NZB_BYTES = 2 * 1024 * 1024
_VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv")
_IGNORED_VIDEO_PREFIXES = ("sample.", "sample-", "sample_")
_FILENAME_RE = re.compile(r'"([^"]+\.(?:mkv|mp4|m4v|avi|ts|m2ts|mov|wmv))"', re.I)
_BARE_FILENAME_RE = re.compile(
    r"([^\s\"']+\.(?:mkv|mp4|m4v|avi|ts|m2ts|mov|wmv))", re.I
)
_ARCHIVE_RE = re.compile(r'"?([^"\\/]+?)(?:\.part\d+)?\.(?:rar|r\d{2,3})\b', re.I)
_ALLOWED_NZB_SCHEMES = frozenset(("http", "https"))


def make_empty_manifest(reason, skipped_candidates=None):
    """Return the shared unsupported manifest shape."""
    skipped_candidates = skipped_candidates or []
    return {
        "payload_kind": "",
        "group_name": "",
        "group_bytes": 0,
        "video_name": "",
        "normalized_video_name": "",
        "video_bytes": 0,
        "archive_base_name": "",
        "article_digest": "",
        "article_count": 0,
        "skipped_candidate_count": len(skipped_candidates),
        "skipped_candidates": skipped_candidates,
        "unsupported_reason": reason,
    }


def _empty_manifest(reason, skipped_candidates=None):
    """Return an unsupported manifest for internal parser call sites."""
    return make_empty_manifest(reason, skipped_candidates)


def normalize_video_filename(value):
    """Normalize a video filename for fallback grouping."""
    if not isinstance(value, str):
        return ""
    value = value.strip().strip('"').strip("'")
    if not value:
        return ""
    if "." in value:
        stem, ext = value.rsplit(".", 1)
        stem = re.sub(r"[\W_]+", " ", stem.lower())
        return "{}.{}".format(" ".join(stem.split()), ext.lower())
    return " ".join(re.sub(r"[\W_]+", " ", value.lower()).split())


def _strip_namespace(tag):
    """Return an XML tag name without its namespace prefix."""
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _children_by_name(elem, name):
    """Return direct XML children whose local tag name matches name."""
    return [child for child in list(elem) if _strip_namespace(child.tag) == name]


def _find_video_name(subject):
    """Extract a supported non-sample video filename from an NZB subject."""
    if not isinstance(subject, str):
        return ""
    match = _FILENAME_RE.search(subject) or _BARE_FILENAME_RE.search(subject)
    if not match:
        return ""
    name = match.group(1).strip()
    lower = name.lower()
    base = lower.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if base.startswith(_IGNORED_VIDEO_PREFIXES):
        return ""
    if not lower.endswith(_VIDEO_EXTENSIONS):
        return ""
    return name


def _find_archive_base(subject):
    """Extract a normalized archive base name from a RAR-style subject."""
    if not isinstance(subject, str):
        return ""
    match = _ARCHIVE_RE.search(subject)
    if not match:
        return ""
    base = match.group(1).strip().strip('"').strip("'")
    base = re.sub(r"[\W_]+", " ", base.lower())
    return " ".join(base.split())


def _segment_rows(file_elem):
    """Return sorted NZB segment rows as number, byte size, and Message-ID."""
    rows = []
    for segments in _children_by_name(file_elem, "segments"):
        for segment in _children_by_name(segments, "segment"):
            try:
                number = int(segment.attrib.get("number", "0") or 0)
                size = int(segment.attrib.get("bytes", "0") or 0)
            except ValueError:
                continue
            msgid = (segment.text or "").strip().strip("<>").lower()
            if size <= 0 or not msgid:
                continue
            rows.append((number, size, msgid))
    rows.sort(key=lambda item: (item[0], item[2]))
    return rows


def _digest_articles(rows):
    """Return a stable digest over sorted article Message-IDs."""
    digest = hashlib.sha256()
    for _number, _size, msgid in rows:
        digest.update(msgid.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest() if rows else ""


def _candidate_name(candidate):
    """Return a human-readable candidate name for skip diagnostics."""
    return (
        candidate.get("video_name")
        or candidate.get("archive_base_name")
        or candidate.get("group_name")
        or ""
    )


def _candidate_is_healthy(candidate, health_check):
    """Return whether a candidate passes the optional health callback."""
    if health_check is None:
        return True
    try:
        return bool(health_check(candidate))
    except Exception:  # pylint: disable=broad-except
        return False


def _public_manifest(candidate, skipped_candidates):
    """Return a public manifest without transient parser-only fields."""
    manifest = dict(candidate)
    manifest.pop("message_ids", None)
    manifest["skipped_candidate_count"] = len(skipped_candidates)
    manifest["skipped_candidates"] = skipped_candidates
    return manifest


def _select_healthy_candidate(candidates, health_check):
    """Return the first healthy manifest candidate or an unsupported manifest."""
    skipped_candidates = []
    for candidate in candidates:
        if _candidate_is_healthy(candidate, health_check):
            return _public_manifest(candidate, skipped_candidates)
        skipped_candidates.append(
            {"name": _candidate_name(candidate), "reason": "message_id_health_failed"}
        )
    if skipped_candidates:
        return _empty_manifest(
            "all_candidate_files_failed_health_check",
            skipped_candidates=skipped_candidates,
        )
    return _empty_manifest("no_video_file")


def extract_nzb_video_manifest(nzb_bytes, health_check=None):
    """Return main video-file or provisional archive metadata from an NZB XML."""
    try:
        root = ET.fromstring(nzb_bytes)
    except (ET.ParseError, TypeError):
        return _empty_manifest("invalid_xml")

    video_candidates = []
    archive_groups = {}
    for file_elem in root.iter():
        if _strip_namespace(file_elem.tag) != "file":
            continue
        rows = _segment_rows(file_elem)
        video_name = _find_video_name(file_elem.attrib.get("subject", ""))
        if video_name:
            video_bytes = sum(row[1] for row in rows)
            article_digest = _digest_articles(rows)
            if video_bytes <= 0 or not article_digest:
                continue
            normalized = normalize_video_filename(video_name)
            video_candidates.append(
                {
                    "payload_kind": "video",
                    "group_name": normalized,
                    "group_bytes": video_bytes,
                    "video_name": video_name,
                    "normalized_video_name": normalized,
                    "video_bytes": video_bytes,
                    "archive_base_name": "",
                    "article_digest": article_digest,
                    "article_count": len(rows),
                    "message_ids": [row[2] for row in rows],
                    "unsupported_reason": "",
                }
            )
            continue
        archive_base = _find_archive_base(file_elem.attrib.get("subject", ""))
        if archive_base and rows:
            archive_groups.setdefault(archive_base, []).extend(rows)

    archive_candidates = []
    for archive_base, rows in archive_groups.items():
        rows.sort(key=lambda item: (item[0], item[2]))
        article_digest = _digest_articles(rows)
        if not article_digest:
            continue
        archive_candidates.append(
            {
                "payload_kind": "archive",
                "group_name": archive_base,
                "group_bytes": 0,
                "video_name": "",
                "normalized_video_name": "",
                "video_bytes": 0,
                "archive_base_name": archive_base,
                "article_digest": article_digest,
                "article_count": len(rows),
                "message_ids": [row[2] for row in rows],
                "unsupported_reason": "",
            }
        )

    video_candidates.sort(
        key=lambda item: (item["video_bytes"], item["article_count"]),
        reverse=True,
    )
    archive_candidates.sort(key=lambda item: item["article_count"], reverse=True)
    return _select_healthy_candidate(
        video_candidates + archive_candidates, health_check
    )


def _valid_nzb_url(url):
    """Return True for simple HTTP(S) NZB URLs that are safe to fetch."""
    if not isinstance(url, str) or any(ord(char) < 0x20 for char in url):
        return False
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in _ALLOWED_NZB_SCHEMES:
            return False
        if not parts.netloc or not parts.hostname:
            return False
        if parts.username or parts.password:
            return False
        _port = parts.port
    except ValueError:
        return False
    return True


def fetch_nzb_video_manifest(
    url, timeout=5, max_bytes=_MAX_NZB_BYTES, health_check=None
):
    """Fetch and parse a candidate NZB manifest."""
    if not _valid_nzb_url(url):
        return _empty_manifest("invalid_url")
    req = Request(url)
    req.add_header("User-Agent", "NZB-DAV Kodi")
    try:
        # nosemgrep
        with urlopen(req, timeout=timeout) as resp:  # nosec B310
            try:
                content_length = int(resp.headers.get("Content-Length", "0") or 0)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > max_bytes:
                return _empty_manifest("too_large")
            body = resp.read(max_bytes + 1)
    except (HTTPError, URLError, OSError, ValueError):
        return _empty_manifest("fetch_error")
    if len(body) > max_bytes:
        return _empty_manifest("too_large")
    return extract_nzb_video_manifest(body, health_check=health_check)
