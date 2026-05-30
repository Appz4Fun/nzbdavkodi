#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Generate the GitHub Pages Kodi repository artifact."""

import argparse
import gzip
import hashlib
import os
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import urlparse

_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_ZIP_FILE_MODE = 0o100644 << 16


def _parse_local_xml(path):
    """Parse trusted repo XML without enabling DTD/entity declarations."""
    with open(path, "rb") as fh:
        xml_bytes = fh.read()
    upper_xml = xml_bytes.upper()
    if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
        raise ET.ParseError("DTD/entity declarations are not supported")
    return ET.ElementTree(ET.fromstring(xml_bytes))


def _parse_xml_bytes(xml_bytes):
    """Parse trusted XML bytes without enabling DTD/entity declarations."""
    upper_xml = xml_bytes.upper()
    if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
        raise ET.ParseError("DTD/entity declarations are not supported")
    return ET.ElementTree(ET.fromstring(xml_bytes))


def _strip_repo_metadata_news(root):
    for metadata in root.findall("extension"):
        if metadata.attrib.get("point") in {
            "xbmc.addon.metadata",
            "kodi.addon.metadata",
        }:
            for news in list(metadata.findall("news")):
                metadata.remove(news)


def _addon_zip_relative_path(addon_id, version):
    zip_name = "{}-{}.zip".format(addon_id, version)
    return "{}/{}".format(addon_id, zip_name)


def _is_repository_relative_path(path):
    if urlparse(path).scheme or os.path.isabs(path):
        return False
    normalized_input = path.replace("\\", "/")
    if os.pardir in normalized_input.split("/"):
        return False
    normalized = os.path.normpath(normalized_input)
    if normalized == os.pardir or normalized.startswith(os.pardir + os.sep):
        return False
    return True


def _write_sha256_file(path):
    sha256 = hashlib.sha256(open(path, "rb").read()).hexdigest()
    checksum_path = "{}.sha256".format(path)
    with open(checksum_path, "w", encoding="ascii") as f:
        f.write("{}  {}".format(sha256, os.path.basename(path)))
    return sha256


def _verify_sha256_file(path):
    checksum_path = "{}.sha256".format(path)
    filename = os.path.basename(path)
    if not os.path.isfile(checksum_path):
        raise SystemExit(
            "generate_repo: missing sha256 checksum for {}".format(filename)
        )
    expected = hashlib.sha256(open(path, "rb").read()).hexdigest()
    checksum = open(checksum_path, "r", encoding="ascii").read().strip()
    checksum_parts = checksum.split()
    if not checksum_parts or checksum_parts[0] != expected:
        raise SystemExit(
            "generate_repo: sha256 checksum does not match {}".format(filename)
        )


def _copy_addon_zip_for_pages(output_dir, addon_zip, addon_id, version):
    relative_path = _addon_zip_relative_path(addon_id, version)
    output_path = os.path.join(output_dir, relative_path)
    output_addon_dir = os.path.dirname(output_path)
    os.makedirs(output_addon_dir, exist_ok=True)
    shutil.copy2(addon_zip, output_path)
    _write_sha256_file(output_path)
    with zipfile.ZipFile(addon_zip) as zf:
        for member in (
            "{}/resources/icon.png".format(addon_id),
            "{}/resources/fanart.jpg".format(addon_id),
        ):
            try:
                data = zf.read(member)
            except KeyError:
                continue
            target = os.path.join(output_dir, member)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
    _write_dir_index(output_addon_dir)
    return relative_path


def _set_metadata_path(root, path):
    updated = False
    for metadata in root.findall("extension"):
        if metadata.attrib.get("point") in {
            "xbmc.addon.metadata",
            "kodi.addon.metadata",
        }:
            path_element = metadata.find("path")
            if path_element is None:
                path_element = ET.Element("path")
                metadata.insert(0, path_element)
            path_element.text = path
            if path_element.tail is None:
                path_element.tail = metadata.text or "\n        "
            updated = True

    if updated:
        return

    metadata = ET.SubElement(root, "extension", {"point": "xbmc.addon.metadata"})
    path_element = ET.SubElement(metadata, "path")
    path_element.text = path


def read_addon_xml(path, metadata_path=None):
    """Read an addon.xml and return its repository metadata text."""
    tree = _parse_local_xml(path)
    root = tree.getroot()
    _strip_repo_metadata_news(root)
    if metadata_path:
        _set_metadata_path(root, metadata_path)
    return ET.tostring(root, encoding="unicode")


def _read_addon_xml_from_zip(zip_path, addon_id, metadata_path=None):
    addon_xml_name = "{}/addon.xml".format(addon_id)
    with zipfile.ZipFile(zip_path) as zf:
        xml_bytes = zf.read(addon_xml_name)
    tree = _parse_xml_bytes(xml_bytes)
    root = tree.getroot()
    _strip_repo_metadata_news(root)
    if metadata_path:
        _set_metadata_path(root, metadata_path)
    return ET.tostring(root, encoding="unicode")


def _read_addon_version_from_zip(zip_path, addon_id):
    addon_xml_name = "{}/addon.xml".format(addon_id)
    with zipfile.ZipFile(zip_path) as zf:
        xml_bytes = zf.read(addon_xml_name)
    return _parse_xml_bytes(xml_bytes).getroot().attrib["version"]


def _write_html_index(path, links):
    html = '<!doctype html>\n<html>\n<head>\n<meta charset="utf-8">\n</head>\n<body>\n'
    for name in links:
        html += '<a href="{n}">{n}</a><br>\n'.format(n=name)
    html += "</body>\n</html>\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def write_pages_index(output_dir, repo_id="repository.nzbdav", repo_version="1.0.0"):
    """Write a Kodi-browsable Pages root for repository installation."""
    index_path = os.path.join(output_dir, "index.html")
    zip_name = "{}-{}.zip".format(repo_id, repo_version)
    links = [
        name
        for name in sorted(os.listdir(output_dir))
        if os.path.isfile(os.path.join(output_dir, name))
        and name not in (".nojekyll", "index.html")
        and not (name.startswith("plugin.video.nzbdav-") and name.endswith(".zip"))
    ]
    if zip_name not in links:
        links.append(zip_name)
    _write_html_index(index_path, links)

    nojekyll_path = os.path.join(output_dir, ".nojekyll")
    with open(nojekyll_path, "w", encoding="utf-8") as f:
        f.write("")


def _write_dir_index(dir_path):
    """Write a simple HTML directory listing that Kodi can parse."""
    files = sorted(os.listdir(dir_path))
    links = []
    for name in files:
        if name == "index.html":
            continue
        links.append(name)
    _write_html_index(os.path.join(dir_path, "index.html"), links)


def _write_addons_xml(output_dir, addon_xmls):
    addons_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n'
    for xml_text in addon_xmls:
        addons_xml += xml_text + "\n"
    addons_xml += "</addons>\n"

    addons_xml_path = os.path.join(output_dir, "addons.xml")
    with open(addons_xml_path, "w", encoding="utf-8") as f:
        f.write(addons_xml)

    gzip_path = os.path.join(output_dir, "addons.xml.gz")
    with gzip.GzipFile(gzip_path, "wb", mtime=0) as f:
        f.write(addons_xml.encode("utf-8"))

    sha256 = _write_sha256_file(gzip_path)

    print(
        "Generated {} ({} addons, sha256: {})".format(
            gzip_path, len(addon_xmls), sha256
        )
    )


def _build_repository_zip(output_dir, repository_addon_dir):
    repo_addon = os.path.join(repository_addon_dir, "addon.xml")
    repo_tree = _parse_local_xml(repo_addon)
    repo_root = repo_tree.getroot()
    repo_id = repo_root.attrib["id"]
    repo_version = repo_root.attrib["version"]
    repo_out = os.path.join(output_dir, repo_id)
    os.makedirs(repo_out, exist_ok=True)

    for name in ("addon.xml", "icon.png"):
        source = os.path.join(repository_addon_dir, name)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(repo_out, name))

    repo_zip_name = "{}-{}.zip".format(repo_id, repo_version)
    repo_zip_path = os.path.join(repo_out, repo_zip_name)
    with zipfile.ZipFile(repo_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(repository_addon_dir):
            dirs.sort()
            for filename in sorted(files):
                filepath = os.path.join(root, filename)
                arcname = os.path.relpath(
                    filepath, os.path.dirname(repository_addon_dir)
                ).replace(os.sep, "/")
                info = zipfile.ZipInfo(arcname)
                info.date_time = _ZIP_EPOCH
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = _ZIP_FILE_MODE
                with open(filepath, "rb") as fh:
                    zf.writestr(info, fh.read())

    shutil.copy2(repo_zip_path, os.path.join(output_dir, repo_zip_name))
    _write_sha256_file(repo_zip_path)
    _write_sha256_file(os.path.join(output_dir, repo_zip_name))
    print("Built repository addon zip at {}".format(repo_zip_path))
    return repo_id, repo_version


def generate_repo(
    output_dir="pages-dist",
    addon_zip=None,
    repository_addon_dir="repo/repository.nzbdav",
):
    """Generate Pages metadata."""

    if not os.path.isdir(repository_addon_dir):
        raise SystemExit(
            "generate_repo: repository addon directory not found: {!r}".format(
                repository_addon_dir
            )
        )

    repo_addon = os.path.join(repository_addon_dir, "addon.xml")
    if not os.path.exists(repo_addon):
        raise SystemExit(
            "generate_repo: repository addon.xml not found: {!r}".format(repo_addon)
        )

    os.makedirs(output_dir, exist_ok=True)

    addon_xmls = []
    main_addon = "repo/plugin.video.nzbdav/addon.xml"
    main_addon_id = "plugin.video.nzbdav"
    if addon_zip:
        release_version = _read_addon_version_from_zip(addon_zip, main_addon_id)
        metadata_url = _copy_addon_zip_for_pages(
            output_dir,
            addon_zip,
            main_addon_id,
            release_version,
        )
        addon_xmls.append(
            _read_addon_xml_from_zip(
                addon_zip,
                main_addon_id,
                metadata_url,
            )
        )
    elif os.path.exists(main_addon):
        release_version = _parse_local_xml(main_addon).getroot().attrib["version"]
        local_addon_zip = "{}-{}.zip".format(main_addon_id, release_version)
        if os.path.isfile(local_addon_zip):
            metadata_url = _copy_addon_zip_for_pages(
                output_dir,
                local_addon_zip,
                main_addon_id,
                release_version,
            )
            addon_xmls.append(read_addon_xml(main_addon, metadata_url))
        else:
            print(
                "generate_repo: skipping {} metadata because addon zip "
                "not found: {!r}".format(main_addon_id, local_addon_zip),
                file=sys.stderr,
            )

    addon_xmls.append(read_addon_xml(repo_addon))
    _write_addons_xml(output_dir, addon_xmls)

    repo_id, repo_version = _build_repository_zip(output_dir, repository_addon_dir)
    _write_dir_index(os.path.join(output_dir, repo_id))
    write_pages_index(output_dir, repo_id, repo_version)


def _read_repository_identity(repository_addon_dir):
    repo_addon = os.path.join(repository_addon_dir, "addon.xml")
    if not os.path.exists(repo_addon):
        raise SystemExit(
            "generate_repo: repository addon.xml not found: {!r}".format(repo_addon)
        )
    repo_root = _parse_local_xml(repo_addon).getroot()
    return repo_root.attrib["id"], repo_root.attrib["version"]


def smoke_check_pages(output_dir, repository_addon_dir="repo/repository.nzbdav"):
    """Validate the generated Pages artifact before deployment."""
    index_path = os.path.join(output_dir, "index.html")
    addons_xml_path = os.path.join(output_dir, "addons.xml.gz")
    checksum_path = os.path.join(output_dir, "addons.xml.gz.sha256")

    if not os.path.isfile(index_path):
        raise SystemExit("generate_repo: missing Pages index.html")
    if not os.path.isfile(addons_xml_path):
        raise SystemExit("generate_repo: missing addons.xml.gz")
    if not os.path.isfile(checksum_path):
        raise SystemExit("generate_repo: missing addons.xml.gz.sha256")

    sha256 = hashlib.sha256(open(addons_xml_path, "rb").read()).hexdigest()
    checksum = open(checksum_path, "r", encoding="utf-8").read().strip()
    checksum_parts = checksum.split()
    if not checksum_parts or checksum_parts[0] != sha256:
        raise SystemExit(
            "generate_repo: addons.xml.gz.sha256 does not match addons.xml.gz"
        )

    root_addon_zips = [
        name
        for name in os.listdir(output_dir)
        if name.startswith("plugin.video.nzbdav-") and name.endswith(".zip")
    ]
    if root_addon_zips:
        raise SystemExit(
            "generate_repo: Pages root must not contain plugin.video.nzbdav zip files"
        )

    with gzip.open(addons_xml_path, "rb") as fh:
        tree = _parse_xml_bytes(fh.read())
    addon = tree.getroot().find("./addon[@id='plugin.video.nzbdav']")
    if addon is not None:
        metadata = addon.find("./extension[@point='xbmc.addon.metadata']")
        path = metadata.findtext("path") if metadata is not None else ""
        if path and not _is_repository_relative_path(path):
            raise SystemExit(
                "generate_repo: plugin.video.nzbdav path must be relative "
                "to repository datadir"
            )
        if path:
            addon_zip_path = os.path.join(output_dir, path)
        else:
            addon_zip_path = os.path.join(
                output_dir,
                "plugin.video.nzbdav",
                "plugin.video.nzbdav-{}.zip".format(addon.attrib["version"]),
            )
        if not os.path.isfile(addon_zip_path):
            raise SystemExit(
                "generate_repo: plugin.video.nzbdav zip missing from repository datadir"
            )
        _verify_sha256_file(addon_zip_path)

    repo_id, _repo_version = _read_repository_identity(repository_addon_dir)
    index = open(index_path, "r", encoding="utf-8").read()
    repo_zip_names = [
        name
        for name in os.listdir(output_dir)
        if name.startswith("{}-".format(repo_id)) and name.endswith(".zip")
    ]
    if len(repo_zip_names) != 1:
        raise SystemExit("generate_repo: index.html must link one repository zip")
    repo_zip_link = '<a href="{0}">{0}</a>'.format(repo_zip_names[0])
    if repo_zip_link not in index:
        raise SystemExit(
            "generate_repo: index.html must link {}".format(repo_zip_names[0])
        )

    repo_zip_path = os.path.join(output_dir, repo_zip_names[0])
    _verify_sha256_file(repo_zip_path)
    expected_index_links = [
        "addons.xml",
        "addons.xml.gz",
        "addons.xml.gz.sha256",
        repo_zip_names[0],
        repo_zip_names[0] + ".sha256",
    ]
    for name in expected_index_links:
        if '<a href="{0}">{0}</a>'.format(name) not in index:
            raise SystemExit("generate_repo: index.html must link {}".format(name))
    repo_addon_xml_member = "{}/addon.xml".format(repo_id)
    with zipfile.ZipFile(repo_zip_path) as zf:
        if repo_addon_xml_member not in zf.namelist():
            raise SystemExit(
                "generate_repo: repository zip missing {}".format(repo_addon_xml_member)
            )

    repo_dir = os.path.join(output_dir, repo_id)
    if os.path.isdir(repo_dir):
        repo_dir_zip_names = [
            name
            for name in os.listdir(repo_dir)
            if name.startswith("{}-".format(repo_id)) and name.endswith(".zip")
        ]
        if repo_dir_zip_names != repo_zip_names:
            raise SystemExit(
                "generate_repo: {} directory must contain one matching "
                "repository zip".format(repo_id)
            )
        _verify_sha256_file(os.path.join(repo_dir, repo_dir_zip_names[0]))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default="pages-dist",
        help="Output directory for Pages artifact",
    )
    parser.add_argument(
        "--addon-zip", default=None, help="Use this addon release zip for metadata"
    )
    parser.add_argument(
        "--repository-addon-dir",
        default="repo/repository.nzbdav",
        help="Repository addon directory to include and package",
    )
    parser.add_argument(
        "--smoke-check",
        action="store_true",
        help="Validate generated Pages artifact after generation",
    )
    args = parser.parse_args(argv)
    generate_repo(
        output_dir=args.output_dir,
        addon_zip=args.addon_zip,
        repository_addon_dir=args.repository_addon_dir,
    )
    if args.smoke_check:
        smoke_check_pages(
            args.output_dir, repository_addon_dir=args.repository_addon_dir
        )


if __name__ == "__main__":
    main(sys.argv[1:])
