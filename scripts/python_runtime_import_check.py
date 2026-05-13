#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Compile and import the shipped addon under the active Python runtime.

Run this with Python 3.8 to validate the Kodi runtime floor without pulling in
the pytest toolchain, which now requires newer Python versions.
"""

from __future__ import annotations

import argparse
import importlib
import py_compile
import sys
import traceback
from pathlib import Path
from unittest.mock import MagicMock

KODI_MODULES = ("xbmc", "xbmcgui", "xbmcplugin", "xbmcaddon", "xbmcvfs")
SIDE_EFFECT_ENTRYPOINTS = {"addon"}


class _FakePlayer:
    def __init__(self):
        self._is_playing = False

    def isPlaying(self):
        return self._is_playing

    def isPlayingVideo(self):
        return self._is_playing

    def play(self, item="", listitem=None, windowed=False, startpos=-1):
        return None


def collect_python_files(addon_dir: Path):
    return sorted(path for path in addon_dir.rglob("*.py") if path.is_file())


def collect_import_names(addon_dir: Path):
    names = []
    for path in collect_python_files(addon_dir):
        rel = path.relative_to(addon_dir).with_suffix("")
        parts = rel.parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        names.append(".".join(parts))
    return sorted(set(names))


def install_kodi_mocks():
    for module_name in KODI_MODULES:
        sys.modules[module_name] = MagicMock()

    xbmcaddon_mod = sys.modules["xbmcaddon"]
    addon_instance = xbmcaddon_mod.Addon.return_value
    addon_instance.getSetting.return_value = ""
    addon_instance.getLocalizedString.return_value = ""
    addon_instance.getAddonInfo.side_effect = lambda key, *_args, **_kwargs: {
        "id": "plugin.video.nzbdav",
        "name": "NZB-DAV",
        "version": "0.0.0",
        "path": "",
        "profile": "",
    }.get(key, "")

    xbmc_mod = sys.modules["xbmc"]
    xbmc_mod.Player = _FakePlayer
    xbmc_mod.Monitor.return_value.waitForAbort.return_value = False
    xbmc_mod.Monitor.return_value.abortRequested.return_value = False


def compile_files(addon_dir: Path):
    failures = []
    for path in collect_python_files(addon_dir):
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append((str(path), exc))
    return failures


def import_modules(addon_dir: Path):
    install_kodi_mocks()
    sys.path.insert(0, str(addon_dir))
    sys.path.insert(0, str(addon_dir / "resources" / "lib"))

    failures = []
    for module_name in collect_import_names(addon_dir):
        if module_name in SIDE_EFFECT_ENTRYPOINTS:
            continue
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - failure path prints details.
            failures.append((module_name, exc, traceback.format_exc()))
    return failures


def run_check(addon_dir: Path):
    compile_failures = compile_files(addon_dir)
    import_failures = import_modules(addon_dir)
    return compile_failures, import_failures


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "addon_dir",
        nargs="?",
        default="plugin.video.nzbdav",
        help="Addon directory to compile/import",
    )
    args = parser.parse_args(argv)
    addon_dir = Path(args.addon_dir)

    compile_failures, import_failures = run_check(addon_dir)
    for path, error in compile_failures:
        print("COMPILE FAIL {}: {}".format(path, error), file=sys.stderr)
    for module_name, error, tb in import_failures:
        print("IMPORT FAIL {}: {}".format(module_name, error), file=sys.stderr)
        print(tb, file=sys.stderr)

    if compile_failures or import_failures:
        return 1
    print(
        "Python runtime import check passed: {} files, {} imported modules".format(
            len(collect_python_files(addon_dir)),
            len(collect_import_names(addon_dir)) - len(SIDE_EFFECT_ENTRYPOINTS),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
