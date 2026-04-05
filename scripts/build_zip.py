#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Build the Kodi addon zip file."""

import argparse
import os
import zipfile


def build_zip(addon_dir="plugin.video.nzbdav", output_dir="."):
    addon_id = os.path.basename(addon_dir)
    zip_path = os.path.join(output_dir, "{}.zip".format(addon_id))

    skip_dirs = {"__pycache__", ".pytest_cache"}
    skip_files = {".DS_Store"}
    skip_ext = {".pyc"}
    FILE_ATTR = 0o100644 << 16

    os.makedirs(output_dir, exist_ok=True)
    if os.path.exists(zip_path):
        os.remove(zip_path)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(addon_dir):
            dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
            for f in sorted(files):
                if f in skip_files or os.path.splitext(f)[1] in skip_ext:
                    continue
                filepath = os.path.join(root, f)
                arcname = filepath.replace(os.sep, "/")
                info = zipfile.ZipInfo(arcname)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = FILE_ATTR
                with open(filepath, "rb") as fh:
                    zf.writestr(info, fh.read())

    size = os.path.getsize(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        entries = len(zf.namelist())
    print("Created {} ({} entries, {:.0f} KB)".format(zip_path, entries, size / 1024))
    return zip_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=".", help="Directory to write zip to")
    args = parser.parse_args()
    build_zip(output_dir=args.output_dir)
