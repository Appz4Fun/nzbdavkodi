# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""NZB manifest inspection for fallback grouping."""

import hashlib
import re

try:
    from defusedxml import ElementTree as ET
    from defusedxml.common import DefusedXmlException as _UnsafeXmlError

    _USING_DEFUSEDXML = True
except ImportError:  # pragma: no cover - Kodi installs may not bundle defusedxml
    import xml.etree.ElementTree as ET

    _USING_DEFUSEDXML = False

    class _UnsafeXmlError(ValueError):
        """Raised when the stdlib fallback rejects an entity declaration."""


from urllib.parse import urlsplit

from resources.lib.http_util import HttpResponseTooLarge, http_get

_MAX_NZB_BYTES = 100 * 1024 * 1024
_VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv")
_IGNORED_VIDEO_PREFIXES = ("sample.", "sample-", "sample_")
_VIDEO_EXTENSION_PATTERN = r"(?:mkv|mp4|m4v|avi|ts|m2ts|mov|wmv)"
_VIDEO_EXTENSION_BOUNDARY = r"(?=$|[^A-Za-z0-9.])"
_VIDEO_EXTENSION_TOKEN_RE = re.compile(
    r"\.{}{}".format(_VIDEO_EXTENSION_PATTERN, _VIDEO_EXTENSION_BOUNDARY), re.I
)
_FILENAME_RE = re.compile(r'"([^"]+\.{})"'.format(_VIDEO_EXTENSION_PATTERN), re.I)
_BARE_FILENAME_RE = re.compile(
    r"([^\s\"']+\.{}){}".format(_VIDEO_EXTENSION_PATTERN, _VIDEO_EXTENSION_BOUNDARY),
    re.I,
)
_ARCHIVE_RE = re.compile(r'"?([^"\\/]+?)(?:\.part\d+)?\.(?:rar|r\d{2,3})\b', re.I)
_ALLOWED_NZB_SCHEMES = frozenset(("http", "https"))
_METADATA_EXTENSION_RE = re.compile(
    r"\.(par2?|nfo|sfv|jpg|jpeg|png|gif|txt|url|lnk|srt|sub|idx|md5|sha\d*)\b",
    re.I,
)
_NON_WORD_RE = re.compile(r"[\W_]+")
_DOMINANT_BLOB_THRESHOLD_FRACTION = 0.80
_SPLIT_PAYLOAD_MIN_FILE_COUNT = 10
_SPLIT_PAYLOAD_MAX_SIZE_RATIO = 5
_SYNTHETIC_VIDEO_MIN_PAYLOAD_BYTES = 100 * 1024 * 1024


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
        stem = _NON_WORD_RE.sub(" ", stem.lower())
        return "{}.{}".format(" ".join(stem.split()), ext.lower())
    return " ".join(_NON_WORD_RE.sub(" ", value.lower()).split())


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
    match = _FILENAME_RE.search(subject) if '"' in subject else None
    match = match or _BARE_FILENAME_RE.search(subject)
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


def _subject_may_be_video(subject):
    """Return whether a subject is worth checking with the video regex."""
    if not isinstance(subject, str):
        return False
    if "." not in subject:
        return False
    return _VIDEO_EXTENSION_TOKEN_RE.search(subject) is not None


def _find_archive_base(subject):
    """Extract a normalized archive base name from a RAR-style subject."""
    if not isinstance(subject, str):
        return ""
    match = _ARCHIVE_RE.search(subject)
    if not match:
        return ""
    base = match.group(1).strip().strip('"').strip("'")
    base = _NON_WORD_RE.sub(" ", base.lower())
    return " ".join(base.split())


def _is_token_boundary(text, index):
    """Return whether index ends a token (out of range or a non-word char)."""
    if index >= len(text):
        return True
    return not (text[index].isalnum() or text[index] == "_")


def _has_rar_extension(lower):
    """Return whether the subject carries a boundary-delimited ``.rar`` token."""
    start = lower.find(".rar")
    while start != -1:
        boundary_index = start + 4
        if _is_token_boundary(lower, boundary_index):
            return True
        start = lower.find(".rar", boundary_index)
    return False


def _has_rar_volume_extension(lower):
    """Return whether the subject carries a boundary-delimited ``.rNN`` token."""
    start = lower.find(".r")
    while start != -1:
        digits_start = start + 2
        digits_end = digits_start
        max_digits_end = min(len(lower), digits_start + 3)
        while digits_end < max_digits_end and lower[digits_end].isdigit():
            digits_end += 1
        if digits_end - digits_start >= 2 and _is_token_boundary(lower, digits_end):
            return True
        start = lower.find(".r", start + 2)
    return False


def _subject_may_be_archive(subject):
    """Return whether a subject is worth checking with the archive regex."""
    if not isinstance(subject, str):
        return False
    lower = subject.lower()
    if ".r" not in lower:
        return False
    return _has_rar_extension(lower) or _has_rar_volume_extension(lower)


def _segment_row_sort_key(row):
    """Return the stable article ordering key for NZB segment rows."""
    return row[0], row[2]


def _segment_rows(file_elem):
    """Return sorted NZB segment rows as number, byte size, and Message-ID."""
    rows = []
    rows_ordered = True
    previous_key = None
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
            row_key = (number, msgid)
            if previous_key is not None and row_key < previous_key:
                rows_ordered = False
            previous_key = row_key
            rows.append((number, size, msgid))
    if not rows_ordered:
        rows.sort(key=_segment_row_sort_key)
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


def _select_healthy_candidate(candidates, health_check, skipped_candidates=None):
    """Return the first healthy manifest candidate or an unsupported manifest."""
    skipped_candidates = list(skipped_candidates or [])
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


def _subject_looks_like_metadata(subject):
    """Return whether a subject names a metadata sidecar (par2/nfo/sfv/...)."""
    if not isinstance(subject, str):
        return False
    return _METADATA_EXTENSION_RE.search(subject) is not None


def _non_metadata_payload_files(file_elems):
    """Yield (rows, total_bytes) for each non-metadata file with real payload."""
    for elem in file_elems:
        subject = elem.attrib.get("subject", "")
        if _subject_looks_like_metadata(subject):
            continue
        rows = _segment_rows(elem)
        if not rows:
            continue
        total = sum(row[1] for row in rows)
        if total <= 0:
            continue
        yield rows, total


def _synthetic_video_candidate(rows, payload_total, health_check):
    """Build a synthetic obfuscated-payload video candidate, or None."""
    article_digest = _digest_articles(rows)
    if not article_digest:
        return None
    candidate = {
        "payload_kind": "video",
        "group_name": "",
        "group_bytes": payload_total,
        "video_name": "",
        "normalized_video_name": "",
        "video_bytes": payload_total,
        "archive_base_name": "",
        "article_digest": article_digest,
        "article_count": len(rows),
        "unsupported_reason": "",
    }
    if health_check is not None:
        candidate["message_ids"] = [row[2] for row in rows]
    return candidate


def _dominant_blob_video_candidates(file_elems, health_check):
    """Return a synthetic video candidate when one obfuscated file dominates.

    Heavily obfuscated NZBs strip filename extensions off every file but still
    ship a single dominant payload that holds the actual movie. When that
    blob accounts for at least the configured fraction of non-metadata bytes,
    treat it as a video manifest so the fallback peer matcher can pair it
    against other releases via the size-tolerance band.
    """
    payload_total = 0
    largest = None  # (rows, total_bytes)
    for rows, total in _non_metadata_payload_files(file_elems):
        payload_total += total
        if largest is None or total > largest[1]:
            largest = (rows, total)
    if largest is None or payload_total <= 0:
        return []
    if largest[1] < payload_total * _DOMINANT_BLOB_THRESHOLD_FRACTION:
        return []
    if largest[1] < _SYNTHETIC_VIDEO_MIN_PAYLOAD_BYTES:
        return []
    rows, total = largest
    candidate = _synthetic_video_candidate(rows, total, health_check)
    if candidate is None:
        return []
    return [candidate]


def _split_payload_video_candidates(file_elems, health_check):
    """Return a synthetic video candidate when the payload is split across many
    quasi-uniform obfuscated files.

    Heavily obfuscated uploads chunk the movie into dozens of similarly-sized
    files (numeric extensions, ``.7z.NNN``, etc.). When the non-metadata files
    are uniform enough that the largest is at most ``_SPLIT_PAYLOAD_MAX_SIZE_RATIO``
    times the smallest and the count clears the floor, sum their bytes and
    treat the result as a video manifest. The +/-20% peer matcher upstream then
    groups these uploads against each other and against real video manifests
    of the same release. Title and profile gates upstream guard against
    unrelated releases collapsing together.
    """
    payload_rows = []
    sizes = []
    for rows, total in _non_metadata_payload_files(file_elems):
        payload_rows.append(rows)
        sizes.append(total)
    if len(sizes) < _SPLIT_PAYLOAD_MIN_FILE_COUNT:
        return []
    smallest = min(sizes)
    largest = max(sizes)
    if smallest <= 0:
        return []
    if largest > smallest * _SPLIT_PAYLOAD_MAX_SIZE_RATIO:
        return []
    payload_total = sum(sizes)
    if payload_total < _SYNTHETIC_VIDEO_MIN_PAYLOAD_BYTES:
        return []
    combined_rows = []
    for rows in payload_rows:
        combined_rows.extend(rows)
    candidate = _synthetic_video_candidate(combined_rows, payload_total, health_check)
    if candidate is None:
        return []
    return [candidate]


# Matches an ``<!ENTITY ...>`` markup declaration. XML keywords are
# case-sensitive, so the literal upper-case form is the only valid spelling.
# Standard NZB files only carry an (external) ``<!DOCTYPE nzb PUBLIC ...>``
# declaration and never define their own entities, so any entity declaration
# is treated as hostile.
_ENTITY_DECL_RE = re.compile(rb"<!ENTITY\b")


def _reject_entity_declarations(nzb_bytes):
    """Reject NZB payloads that declare XML entities (XXE / billion-laughs).

    ``defusedxml`` does this for us when it is installed. On Kodi installs that
    only ship the standard library, ``xml.etree`` still expands internal
    entities and will resolve some external ones, so we refuse outright any
    document containing an entity declaration before it reaches the parser.
    """
    payload = nzb_bytes
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "ignore")
    if _ENTITY_DECL_RE.search(payload):
        raise _UnsafeXmlError("entity declarations are not allowed in NZB XML")


def _parse_nzb_root(nzb_bytes):
    """Parse NZB bytes into an XML root, or None on parse/safety failure."""
    try:
        if _USING_DEFUSEDXML:
            # forbid_dtd=False keeps the standard NZB ``<!DOCTYPE nzb ...>``
            # working; entity/external-reference defenses stay enabled.
            return ET.fromstring(nzb_bytes, forbid_dtd=False)
        _reject_entity_declarations(nzb_bytes)
        return ET.fromstring(nzb_bytes)
    except (ET.ParseError, TypeError, _UnsafeXmlError):
        return None


def _video_file_candidate(file_elem, video_name, health_check):
    """Build a named-video-file candidate from a file element, or None."""
    rows = _segment_rows(file_elem)
    video_bytes = sum(row[1] for row in rows)
    article_digest = _digest_articles(rows)
    if video_bytes <= 0 or not article_digest:
        return None
    normalized = normalize_video_filename(video_name)
    candidate = {
        "payload_kind": "video",
        "group_name": normalized,
        "group_bytes": video_bytes,
        "video_name": video_name,
        "normalized_video_name": normalized,
        "video_bytes": video_bytes,
        "archive_base_name": "",
        "article_digest": article_digest,
        "article_count": len(rows),
        "unsupported_reason": "",
    }
    if health_check is not None:
        candidate["message_ids"] = [row[2] for row in rows]
    return candidate


def _partition_video_files(root, health_check):
    """Split files into named-video candidates and remaining file elements."""
    video_candidates = []
    non_video_file_candidates = []
    for file_elem in _children_by_name(root, "file"):
        subject = file_elem.attrib.get("subject", "")
        video_name = _find_video_name(subject) if _subject_may_be_video(subject) else ""
        candidate = (
            _video_file_candidate(file_elem, video_name, health_check)
            if video_name
            else None
        )
        if candidate is not None:
            video_candidates.append(candidate)
        elif not video_name:
            non_video_file_candidates.append(file_elem)
    video_candidates.sort(
        key=lambda item: (item["video_bytes"], item["article_count"]),
        reverse=True,
    )
    return video_candidates, non_video_file_candidates


def _group_archive_rows(file_elems):
    """Group RAR-style file rows by normalized archive base name."""
    archive_groups = {}
    archive_group_file_counts = {}
    for file_elem in file_elems:
        subject = file_elem.attrib.get("subject", "")
        if not _subject_may_be_archive(subject):
            continue
        archive_base = _find_archive_base(subject)
        if not archive_base:
            continue
        rows = _segment_rows(file_elem)
        if not rows:
            continue
        archive_groups.setdefault(archive_base, []).extend(rows)
        archive_group_file_counts[archive_base] = (
            archive_group_file_counts.get(archive_base, 0) + 1
        )
    return archive_groups, archive_group_file_counts


def _archive_candidates(file_elems, health_check):
    """Build sorted archive candidates from non-video file elements."""
    archive_groups, archive_group_file_counts = _group_archive_rows(file_elems)
    archive_candidates = []
    for archive_base, rows in archive_groups.items():
        if archive_group_file_counts.get(archive_base, 0) > 1:
            rows.sort(key=_segment_row_sort_key)
        article_digest = _digest_articles(rows)
        if not article_digest:
            continue
        candidate = {
            "payload_kind": "archive",
            "group_name": archive_base,
            "group_bytes": sum(row[1] for row in rows),
            "video_name": "",
            "normalized_video_name": "",
            "video_bytes": 0,
            "archive_base_name": archive_base,
            "article_digest": article_digest,
            "article_count": len(rows),
            "unsupported_reason": "",
        }
        if health_check is not None:
            candidate["message_ids"] = [row[2] for row in rows]
        archive_candidates.append(candidate)
    archive_candidates.sort(key=lambda item: item["article_count"], reverse=True)
    return archive_candidates


def extract_nzb_video_manifest(nzb_bytes, health_check=None):
    """Return main video-file or provisional archive metadata from an NZB XML."""
    root = _parse_nzb_root(nzb_bytes)
    if root is None:
        return _empty_manifest("invalid_xml")

    video_candidates, non_video_file_candidates = _partition_video_files(
        root, health_check
    )

    video_skipped_candidates = []
    if video_candidates:
        video_manifest = _select_healthy_candidate(video_candidates, health_check)
        if not video_manifest.get("unsupported_reason"):
            return video_manifest
        video_skipped_candidates = video_manifest.get("skipped_candidates", [])

    archive_candidates = _archive_candidates(non_video_file_candidates, health_check)
    if archive_candidates:
        return _select_healthy_candidate(
            archive_candidates,
            health_check,
            skipped_candidates=video_skipped_candidates,
        )

    blob_candidates = _dominant_blob_video_candidates(
        non_video_file_candidates, health_check
    )
    if blob_candidates:
        return _select_healthy_candidate(
            blob_candidates,
            health_check,
            skipped_candidates=video_skipped_candidates,
        )

    split_candidates = _split_payload_video_candidates(
        non_video_file_candidates, health_check
    )
    if split_candidates:
        return _select_healthy_candidate(
            split_candidates,
            health_check,
            skipped_candidates=video_skipped_candidates,
        )

    return _select_healthy_candidate(
        archive_candidates, health_check, skipped_candidates=video_skipped_candidates
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


def _fetch_nzb_bytes(url, timeout, max_bytes):
    """Fetch raw NZB bytes with a small LRU keyed by fetch parameters."""
    return http_get(url, timeout=timeout, max_bytes=max_bytes)


def fetch_nzb_video_manifest(
    url, timeout=20, max_bytes=_MAX_NZB_BYTES, health_check=None
):
    """Fetch and parse a candidate NZB manifest."""
    if not _valid_nzb_url(url):
        return _empty_manifest("invalid_url")
    try:
        body = _fetch_nzb_bytes(url, timeout, max_bytes)
    except HttpResponseTooLarge:
        return _empty_manifest("too_large")
    except (OSError, ValueError):
        return _empty_manifest("fetch_error")
    return extract_nzb_video_manifest(body, health_check=health_check)
