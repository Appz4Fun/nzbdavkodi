"""Shared PROPFIND-based storage discovery for the CiNEFiLE runners.

Replaces the per-script hard-coded UUIDs with a runtime lookup so a
fresh seed (different deobfuscated basenames) still works.
"""

from __future__ import annotations

import base64
import os
import re
import urllib.parse
import urllib.request

DEFAULT_TARGET_PREFIX = "12.Angry.Men.1957.1080p.BluRay.x264-CiNEFiLE"


def _basic_auth_header() -> str:
    user = os.environ["WEBDAV_USERNAME"]
    pw = os.environ["WEBDAV_PASSWORD"]
    auth = base64.b64encode("{}:{}".format(user, pw).encode()).decode()
    return "Basic " + auth


def _propfind(url: str, depth: str) -> str:
    req = urllib.request.Request(
        url,
        method="PROPFIND",
        headers={"Depth": depth, "Authorization": _basic_auth_header()},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
            return r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _propfind_mkv_path(storage: str, nzbdav_url: str) -> str:
    """Return the first non-sample .mkv href under ``storage`` or ''."""
    safe = urllib.parse.quote(storage, safe="/") + "/"
    xml_text = _propfind("{}/dav{}".format(nzbdav_url.rstrip("/"), safe), "1")
    candidates = re.findall(r"<D:href>([^<]+\.mkv)</D:href>", xml_text)
    for c in candidates:
        if "sample" not in c.lower():
            return c
    return candidates[0] if candidates else ""


def discover_cinefile_storages(
    nzbdav_url: str | None = None,
    target_prefix: str = DEFAULT_TARGET_PREFIX,
    limit: int = 2,
) -> list[tuple[str, str]]:
    """Return up to ``limit`` ``(storage, mkv_path)`` pairs whose
    release-folder basename starts with ``target_prefix``."""
    nzbdav_url = (
        nzbdav_url or os.environ.get("NZBDAV_URL", "http://localhost:8180")
    ).rstrip("/")
    xml_text = _propfind("{}/dav/content/".format(nzbdav_url), "2")
    if not xml_text:
        return []
    folder_hrefs = re.findall(r"<D:href>(/dav/content/[^<]+/)</D:href>", xml_text)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href in folder_hrefs:
        unquoted = urllib.parse.unquote(href)
        if not unquoted.startswith("/dav/content/"):
            continue
        storage = unquoted[len("/dav") :].rstrip("/")
        basename = storage.rsplit("/", 1)[-1]
        if not basename.startswith(target_prefix) or storage in seen:
            continue
        seen.add(storage)
        mkv_path = _propfind_mkv_path(storage, nzbdav_url)
        if not mkv_path:
            continue
        out.append((storage, mkv_path))
        if len(out) >= limit:
            break
    return out
