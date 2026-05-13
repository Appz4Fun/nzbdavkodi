#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Build release/repository artifacts in a temp dir and validate Pages shape."""

import argparse
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_zip  # noqa: E402
import generate_repo  # noqa: E402


@dataclass(frozen=True)
class SmokeResult:
    output_dir: Path
    addon_zip: Path
    repo_zip: Path
    addons_xml: Path
    addons_md5: Path
    root_index: Path


def _find_one(path: Path, pattern: str) -> Path:
    matches = sorted(path.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(
            "repo smoke expected exactly one {!r} under {}, found {}".format(
                pattern, path, len(matches)
            )
        )
    return matches[0]


def _current_repo_zip(dist_dir: Path) -> Path:
    repo_addon = dist_dir / "repository.nzbdav" / "addon.xml"
    version = ET.parse(repo_addon).getroot().attrib["version"]
    return dist_dir / "repository.nzbdav-{}.zip".format(version)


def _validate(result: SmokeResult) -> None:
    for path in [
        result.addon_zip,
        result.repo_zip,
        result.addons_xml,
        result.addons_md5,
        result.root_index,
        result.output_dir / ".nojekyll",
    ]:
        if not path.exists():
            raise SystemExit("repo smoke missing expected artifact: {}".format(path))

    md5 = result.addons_md5.read_text(encoding="utf-8")
    if len(md5) != 32 or not md5.isalnum():
        raise SystemExit("repo smoke invalid addons.xml.md5 payload")

    tree = ET.parse(result.addons_xml)
    root = tree.getroot()
    if root.find("./addon[@id='plugin.video.nzbdav']") is None:
        raise SystemExit("repo smoke addons.xml missing plugin.video.nzbdav")
    if root.find("./addon[@id='repository.nzbdav']") is None:
        raise SystemExit("repo smoke addons.xml missing repository.nzbdav")

    index = result.root_index.read_text(encoding="utf-8")
    if result.addon_zip.name not in index:
        raise SystemExit("repo smoke root index missing addon zip link")
    if result.repo_zip.name not in index:
        raise SystemExit("repo smoke root index missing repository zip link")


def run_smoke(output_root: Path) -> SmokeResult:
    release_dir = output_root / "release"
    dist_dir = output_root / "dist"
    release_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    built_addon_zip = Path(build_zip.build_zip(output_dir=str(release_dir)))
    generate_repo.generate_repo(output_dir=str(dist_dir), addon_zip=str(built_addon_zip))

    result = SmokeResult(
        output_dir=dist_dir,
        addon_zip=_find_one(dist_dir, "plugin.video.nzbdav-*.zip"),
        repo_zip=_current_repo_zip(dist_dir),
        addons_xml=dist_dir / "addons.xml",
        addons_md5=dist_dir / "addons.xml.md5",
        root_index=dist_dir / "index.html",
    )
    _validate(result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional directory to keep generated smoke artifacts",
    )
    args = parser.parse_args(argv)

    if args.output_dir:
        result = run_smoke(Path(args.output_dir))
        print("Repo smoke passed: {}".format(result.output_dir))
        return 0

    with tempfile.TemporaryDirectory(prefix="nzbdav-repo-smoke-") as tmp:
        result = run_smoke(Path(tmp))
        print("Repo smoke passed: {}".format(result.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
