#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Select the latest stable plugin.video.nzbdav GitHub Release asset."""

import argparse
import json
import re
import sys

_STABLE_SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

ADDON_ID = "plugin.video.nzbdav"
_ASSET_PREFIX = "{}-".format(ADDON_ID)
_ASSET_SUFFIX = ".zip"


def _release_tag(release):
    return release.get("tagName") or release.get("tag_name")


def _is_prerelease(release):
    return bool(release.get("isPrerelease", release.get("prerelease", False)))


def _is_draft(release):
    return bool(release.get("isDraft", release.get("draft", False)))


def _stable_semver_key(tag):
    match = _STABLE_SEMVER_RE.match(tag or "")
    if not match:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _matching_assets(release):
    matches = []
    for asset in release.get("assets") or ():
        name = asset.get("name", "")
        if name.startswith(_ASSET_PREFIX) and name.endswith(_ASSET_SUFFIX):
            matches.append(asset)
    return matches


def _expected_asset_name(tag):
    version = tag[1:] if tag.startswith("v") else tag
    return "{}-{}.zip".format(ADDON_ID, version)


def _asset_download_url(asset):
    return (
        asset.get("downloadUrl")
        or asset.get("browser_download_url")
        or asset.get("url")
        or ""
    )


def select_stable_release(releases):
    """Return tagName, assetName, and downloadUrl for the highest stable release."""
    stable = []
    for release in releases:
        if _is_prerelease(release) or _is_draft(release):
            continue
        tag = _release_tag(release)
        key = _stable_semver_key(tag)
        if key is None:
            continue
        stable.append((key, release, tag))

    if not stable:
        raise SystemExit("No stable {} release found".format(ADDON_ID))

    key, release, tag = max(stable, key=lambda item: item[0])
    del key
    matches = _matching_assets(release)
    if len(matches) != 1:
        raise SystemExit(
            "Stable release {} must have exactly one {}-*.zip asset; found {}".format(
                tag, ADDON_ID, len(matches)
            )
        )

    asset = matches[0]
    expected_name = _expected_asset_name(tag)
    actual_name = asset.get("name", "")
    if actual_name != expected_name:
        raise SystemExit(
            "Stable release {} asset must be named {}; found {}".format(
                tag, expected_name, actual_name
            )
        )
    download_url = _asset_download_url(asset)
    if not download_url:
        raise SystemExit(
            "Stable release {} asset {} must include a download URL".format(
                tag, actual_name
            )
        )
    return {
        "tagName": tag,
        "assetName": actual_name,
        "downloadUrl": download_url,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Select the latest stable plugin.video.nzbdav release asset."
    )
    parser.add_argument("releases_json", help="Path to gh release JSON output")
    args = parser.parse_args(argv)

    with open(args.releases_json, "r", encoding="utf-8") as fh:
        releases = json.load(fh)

    selected = select_stable_release(releases)
    print("tag={}".format(selected["tagName"]))
    print("asset_name={}".format(selected["assetName"]))
    print("download_url={}".format(selected["downloadUrl"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
