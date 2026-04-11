#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Generate Kodi repository metadata (addons.xml + addons.xml.md5)."""

import argparse
import hashlib
import os
import shutil
import xml.etree.ElementTree as ET


def read_addon_xml(path):
    """Read an addon.xml and return its text content."""
    tree = ET.parse(path)  # nosec B314 — parsing our own addon.xml
    return ET.tostring(tree.getroot(), encoding="unicode")


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


def generate_repo(output_dir="dist"):
    os.makedirs(output_dir, exist_ok=True)

    addon_xmls = []

    # Collect addon.xml from the main addon
    main_addon = "plugin.video.nzbdav/addon.xml"
    if os.path.exists(main_addon):
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
    md5 = hashlib.md5(addons_xml.encode("utf-8")).hexdigest()  # noqa: S324  # not used for security
    with open(os.path.join(output_dir, "addons.xml.md5"), "w") as f:
        f.write(md5)

    print(
        "Generated {} ({} addons, md5: {})".format(
            addons_xml_path, len(addon_xmls), md5
        )
    )

    # Copy addon zip into output_dir/plugin.video.nzbdav/
    # Read version from addon.xml for versioned zip filename
    tree = ET.parse(main_addon)  # nosec B314 — parsing our own addon.xml
    version = tree.getroot().attrib["version"]
    addon_zip = "plugin.video.nzbdav-{}.zip".format(version)
    if os.path.exists(addon_zip):
        dest_dir = os.path.join(output_dir, "plugin.video.nzbdav")
        os.makedirs(dest_dir, exist_ok=True)
        shutil.copy2(addon_zip, os.path.join(dest_dir, addon_zip))
        # Copy addon zip to root for direct install from source URL
        shutil.copy2(addon_zip, os.path.join(output_dir, addon_zip))
        # Also copy addon.xml into the addon subfolder (Kodi expects this)
        shutil.copy2(main_addon, os.path.join(dest_dir, "addon.xml"))
        # Copy icon/fanart preserving paths declared in addon.xml <assets>
        for asset in ["resources/icon.png", "resources/fanart.jpg"]:
            src = os.path.join("plugin.video.nzbdav", asset)
            if os.path.exists(src):
                asset_dest = os.path.join(dest_dir, asset)
                os.makedirs(os.path.dirname(asset_dest), exist_ok=True)
                shutil.copy2(src, asset_dest)
        print("Copied addon zip + metadata to {}".format(dest_dir))

    # Build repository addon zip and copy into output
    repo_dir = "repo/repository.nzbdav"
    if os.path.isdir(repo_dir):
        import zipfile

        repo_out = os.path.join(output_dir, "repository.nzbdav")
        os.makedirs(repo_out, exist_ok=True)
        repo_tree = ET.parse(repo_addon)  # nosec B314 — parsing our own addon.xml
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
    args = parser.parse_args()
    generate_repo(output_dir=args.output_dir)
