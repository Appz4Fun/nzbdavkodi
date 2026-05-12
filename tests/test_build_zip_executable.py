# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Pin the +x bit on orchestrator binaries inside the addon zip.

Phase 0 of docs/rust-migration-plan.md ships per-arch binaries under
``plugin.video.nzbdav/bin/``. Kodi's addon installer respects the
POSIX mode bits stored in the zip's ``external_attr`` field; if
``build_zip.py`` writes those entries with ``0o100644`` like every
other file, the extracted binary will be missing +x and the bootstrap
will fail with PermissionError on first spawn. Test pins that
``bin/*`` always lands with mode 0o100755 inside the zip.
"""

from __future__ import annotations

import os
import stat
import sys
import zipfile
from pathlib import Path

# scripts/build_zip.py is the entry point we exercise.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_zip  # noqa: E402


def test_bin_files_are_marked_executable(tmp_path: Path, monkeypatch):
    workdir = tmp_path
    addon_dir = workdir / "plugin.video.nzbdav"
    (addon_dir / "bin").mkdir(parents=True)

    # Minimal addon.xml so build_zip can find a version.
    (addon_dir / "addon.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<addon id="plugin.video.nzbdav" version="9.9.9" />',
        encoding="utf-8",
    )
    # A non-bin file to confirm the executable-bit branch is path-scoped.
    (addon_dir / "addon.py").write_text("# noop\n", encoding="utf-8")
    # A "binary" — content doesn't matter for this test, the assertion
    # is on the stored mode bits, not on file contents.
    (addon_dir / "bin" / "orchestrator-x86_64-musl").write_bytes(b"\x7fELF stub")

    out = workdir / "out"
    monkeypatch.chdir(workdir)
    # Pass relative paths so arcnames inside the zip match what
    # build_zip.py produces when invoked from the repo root (where
    # addon_dir is "plugin.video.nzbdav").
    build_zip.build_zip(addon_dir="plugin.video.nzbdav", output_dir=str(out))

    zip_path = next(out.glob("plugin.video.nzbdav-*.zip"))
    with zipfile.ZipFile(zip_path) as zf:
        # external_attr stores the POSIX st_mode in the high 16 bits;
        # mask down to the permission triplet for a clean comparison.
        modes = {
            info.filename: (info.external_attr >> 16) & 0o7777
            for info in zf.infolist()
        }

    binary_path = "plugin.video.nzbdav/bin/orchestrator-x86_64-musl"
    plain_path = "plugin.video.nzbdav/addon.py"

    assert modes[binary_path] & stat.S_IXUSR, "bin/* must be marked executable"
    assert modes[binary_path] & stat.S_IXGRP, "bin/* must be group-executable"
    assert modes[binary_path] & stat.S_IXOTH, "bin/* must be other-executable"
    assert not (
        modes[plain_path] & stat.S_IXUSR
    ), "non-bin files must stay non-executable so Kodi extracts them 0644"

    # Sanity: full mode value is 0o755 for binaries and 0o644 for plain.
    assert modes[binary_path] == 0o755
    assert modes[plain_path] == 0o644


def test_missing_bin_dir_does_not_break_build(tmp_path: Path, monkeypatch):
    addon_dir = tmp_path / "plugin.video.nzbdav"
    addon_dir.mkdir()
    (addon_dir / "addon.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<addon id="plugin.video.nzbdav" version="9.9.9" />',
        encoding="utf-8",
    )
    (addon_dir / "addon.py").write_text("# noop\n", encoding="utf-8")

    out = tmp_path / "out"
    monkeypatch.chdir(tmp_path)
    zip_path = build_zip.build_zip(addon_dir="plugin.video.nzbdav", output_dir=str(out))
    assert os.path.exists(zip_path)
