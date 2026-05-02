#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Generate Kodi repository metadata (addons.xml + addons.xml.md5)."""

import argparse
import hashlib
import os
import shutil
import xml.etree.ElementTree as ET
import zipfile


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


def read_addon_xml(path):
    """Read an addon.xml and return its text content."""
    tree = _parse_local_xml(path)
    root = tree.getroot()
    _strip_repo_metadata_news(root)
    return ET.tostring(root, encoding="unicode")


def _read_addon_xml_from_zip(zip_path, addon_id):
    addon_xml_name = "{}/addon.xml".format(addon_id)
    with zipfile.ZipFile(zip_path) as zf:
        xml_bytes = zf.read(addon_xml_name)
    tree = _parse_xml_bytes(xml_bytes)
    root = tree.getroot()
    _strip_repo_metadata_news(root)
    return ET.tostring(root, encoding="unicode")


def _read_addon_version_from_zip(zip_path, addon_id):
    addon_xml_name = "{}/addon.xml".format(addon_id)
    with zipfile.ZipFile(zip_path) as zf:
        xml_bytes = zf.read(addon_xml_name)
    return _parse_xml_bytes(xml_bytes).getroot().attrib["version"]


def write_pages_index(output_dir, repo_version="1.0.0"):
    """Write a Kodi-browsable directory listing for the root."""
    index_path = os.path.join(output_dir, "index.html")
    zip_name = "repository.nzbdav-{}.zip".format(repo_version)
    html = "<html><body>\n"
    html += '<a href="{z}">{z}</a><br>\n'.format(z=zip_name)
    html += "</body></html>\n"
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)

    nojekyll_path = os.path.join(output_dir, ".nojekyll")
    with open(nojekyll_path, "w", encoding="utf-8") as f:
        f.write("")


def _copy_addon_artifacts(output_dir, addon_id, main_addon, addon_zip=None):
    if addon_zip:
        version = _read_addon_version_from_zip(addon_zip, addon_id)
        zip_name = "{}-{}.zip".format(addon_id, version)
        dest_dir = os.path.join(output_dir, addon_id)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(addon_zip, os.path.join(dest_dir, zip_name))
        shutil.copy2(addon_zip, os.path.join(output_dir, zip_name))
        with zipfile.ZipFile(addon_zip) as zf:
            for member in [
                "{}/addon.xml".format(addon_id),
                "{}/resources/icon.png".format(addon_id),
                "{}/resources/fanart.jpg".format(addon_id),
            ]:
                try:
                    data = zf.read(member)
                except KeyError:
                    continue
                rel_path = member.split("/", 1)[1]
                target = os.path.join(dest_dir, rel_path)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(data)
        print("Copied addon release zip + metadata to {}".format(dest_dir))
        return

    tree = _parse_local_xml(main_addon)
    version = tree.getroot().attrib["version"]
    zip_name = "{}-{}.zip".format(addon_id, version)
    if os.path.exists(zip_name):
        dest_dir = os.path.join(output_dir, addon_id)
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(zip_name, os.path.join(dest_dir, zip_name))
        shutil.copy2(zip_name, os.path.join(output_dir, zip_name))
        shutil.copy2(main_addon, os.path.join(dest_dir, "addon.xml"))
        for asset in ["resources/icon.png", "resources/fanart.jpg"]:
            src = os.path.join(addon_id, asset)
            if os.path.exists(src):
                asset_dest = os.path.join(dest_dir, asset)
                os.makedirs(os.path.dirname(asset_dest), exist_ok=True)
                shutil.copy2(src, asset_dest)
        print("Copied addon zip + metadata to {}".format(dest_dir))


def generate_repo(output_dir="dist", addon_zip=None):
    os.makedirs(output_dir, exist_ok=True)

    addon_xmls = []

    main_addon = "plugin.video.nzbdav/addon.xml"
    main_addon_id = "plugin.video.nzbdav"
    if addon_zip:
        addon_xmls.append(_read_addon_xml_from_zip(addon_zip, main_addon_id))
    elif os.path.exists(main_addon):
        addon_xmls.append(read_addon_xml(main_addon))

    # Collect addon.xml from the repository addon
    repo_addon = "repo/repository.nzbdav/addon.xml"
    if os.path.exists(repo_addon):
        addon_xmls.append(read_addon_xml(repo_addon))

    # Write addons.xml
    addons_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n'
    for xml_text in addon_xmls:
        addons_xml += xml_text + "\n"
    addons_xml += "</addons>\n"

    addons_xml_path = os.path.join(output_dir, "addons.xml")
    with open(addons_xml_path, "w", encoding="utf-8") as f:
        f.write(addons_xml)

    # Write addons.xml.md5
    md5 = hashlib.md5(
        addons_xml.encode("utf-8")
    ).hexdigest()  # noqa: S324  # not used for security
    with open(os.path.join(output_dir, "addons.xml.md5"), "w") as f:
        f.write(md5)

    print(
        "Generated {} ({} addons, md5: {})".format(
            addons_xml_path, len(addon_xmls), md5
        )
    )

    _copy_addon_artifacts(output_dir, main_addon_id, main_addon, addon_zip)

    # Build repository addon zip and copy into output
    repo_dir = "repo/repository.nzbdav"
    if os.path.isdir(repo_dir):
        repo_out = os.path.join(output_dir, "repository.nzbdav")
        os.makedirs(repo_out, exist_ok=True)
        repo_tree = _parse_local_xml(repo_addon)
        repo_version = repo_tree.getroot().attrib["version"]
        repo_zip_path = os.path.join(
            repo_out, "repository.nzbdav-{}.zip".format(repo_version)
        )
        with zipfile.ZipFile(repo_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(repo_dir):
                for f in files:
                    filepath = os.path.join(root, f)
                    arcname = os.path.relpath(filepath, "repo").replace(os.sep, "/")
                    zf.write(filepath, arcname)
        shutil.copy2(repo_addon, os.path.join(repo_out, "addon.xml"))
        repo_icon = os.path.join(repo_dir, "icon.png")
        if os.path.exists(repo_icon):
            shutil.copy2(repo_icon, os.path.join(repo_out, "icon.png"))
        # Also copy repo zip to root so Kodi can install from the source URL
        root_repo_zip = os.path.join(
            output_dir, "repository.nzbdav-{}.zip".format(repo_version)
        )
        shutil.copy2(repo_zip_path, root_repo_zip)
        print("Built repository addon zip at {}".format(repo_zip_path))
    else:
        repo_version = "1.0.0"

    # Generate directory listing index.html for each subdirectory so Kodi's
    # file manager can browse the repo via GitHub Pages.
    for subdir in os.listdir(output_dir):
        subdir_path = os.path.join(output_dir, subdir)
        if os.path.isdir(subdir_path):
            _write_dir_index(subdir_path)

    write_pages_index(output_dir, repo_version)


def _write_dir_index(dir_path):
    """Write a simple HTML directory listing that Kodi can parse."""
    files = sorted(os.listdir(dir_path))
    links = []
    for name in files:
        if name == "index.html":
            continue
        links.append('<a href="{n}">{n}</a><br>'.format(n=name))
    html = "<html><body>\n{}\n</body></html>\n".format("\n".join(links))
    with open(os.path.join(dir_path, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir", default="dist", help="Output directory for repo"
    )
    parser.add_argument(
        "--addon-zip",
        default=None,
        help=(
            "Use this addon release zip instead of rebuilding metadata from the "
            "worktree"
        ),
    )
    args = parser.parse_args()
    generate_repo(output_dir=args.output_dir, addon_zip=args.addon_zip)
