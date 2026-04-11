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


def write_pages_index(output_dir):
    """Write a simple GitHub Pages landing page for the Kodi repository."""
    index_path = os.path.join(output_dir, "index.html")
    html = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>NZB-DAV Kodi Repository</title>
    <style>
      :root {
        color-scheme: light;
        --bg: #f4f1e8;
        --panel: #fffdf8;
        --ink: #1d2a38;
        --muted: #5b6b79;
        --accent: #005f73;
        --accent-2: #0a9396;
        --border: #d8d2c3;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "Avenir Next", "Segoe UI", sans-serif;
        background:
          radial-gradient(
            circle at top left,
            rgba(10, 147, 150, 0.15),
            transparent 32rem
          ),
          linear-gradient(180deg, #f8f5ee 0%%, var(--bg) 100%%);
        color: var(--ink);
      }
      main {
        max-width: 52rem;
        margin: 0 auto;
        padding: 3rem 1.25rem 4rem;
      }
      .card {
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 18px;
        padding: 1.5rem;
        box-shadow: 0 16px 40px rgba(29, 42, 56, 0.08);
      }
      h1 {
        margin: 0 0 0.75rem;
        font-size: clamp(2rem, 4vw, 3.2rem);
        line-height: 1;
      }
      p {
        color: var(--muted);
        line-height: 1.6;
      }
      ul {
        list-style: none;
        padding: 0;
        margin: 1.5rem 0 0;
      }
      li + li { margin-top: 0.75rem; }
      a {
        display: block;
        padding: 0.95rem 1rem;
        border: 1px solid var(--border);
        border-radius: 12px;
        color: var(--accent);
        text-decoration: none;
        font-weight: 600;
        background: #ffffff;
      }
      a:hover {
        border-color: var(--accent-2);
        color: var(--accent-2);
      }
      .meta {
        margin-top: 1.5rem;
        font-size: 0.95rem;
      }
      code {
        font-family: "SFMono-Regular", "Consolas", monospace;
        color: var(--ink);
      }
    </style>
  </head>
  <body>
    <main>
      <div class="card">
        <h1>NZB-DAV Kodi Repository</h1>
        <p>
          This site hosts the Kodi repository metadata and release artifacts for
          the NZB-DAV add-on.
        </p>
        <ul>
          <li><a href="addons.xml">addons.xml</a></li>
          <li><a href="addons.xml.md5">addons.xml.md5</a></li>
          <li>
            <a href="repository.nzbdav/">repository.nzbdav (browse)</a>
          </li>
          <li>
            <a href="plugin.video.nzbdav/">plugin.video.nzbdav (browse)</a>
          </li>
          <li>
            <a href="https://github.com/xbmc4lyfe/nzbdavkodi/releases">
              GitHub Releases
            </a>
          </li>
        </ul>
        <p class="meta">
          <strong>File source URL:</strong>
          <code>https://xbmc4lyfe.github.io/nzbdavkodi/</code>
        </p>
        <p class="meta">
          Add this URL in Kodi &rarr; File Manager &rarr; Add source, then
          install <code>repository.nzbdav</code> from zip.
        </p>
      </div>
    </main>
  </body>
</html>
"""
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
        print("Built repository addon zip at {}".format(repo_zip_path))

    # Generate directory listing index.html for each subdirectory so Kodi's
    # file manager can browse the repo via GitHub Pages.
    for subdir in os.listdir(output_dir):
        subdir_path = os.path.join(output_dir, subdir)
        if os.path.isdir(subdir_path):
            _write_dir_index(subdir_path)

    write_pages_index(output_dir)


def _write_dir_index(dir_path):
    """Write a simple HTML directory listing that Kodi can parse."""
    files = sorted(os.listdir(dir_path))
    links = []
    for name in files:
        if name == "index.html":
            continue
        links.append('<a href="{}">{}</a><br>'.format(name, name))
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
