# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import faulthandler
import os
import sys

# Capture Python+C stack on any signal that aborts the interpreter (SIGSEGV,
# SIGFPE, SIGABRT, SIGBUS, SIGILL). The trace is written to a fixed path so
# the extreme functional test can pull it out of the kodi container even if
# stderr/stdout are dropped on the floor when Kodi crashes.
try:
    _fh = open("/tmp/nzbdav-faulthandler.log", "a", buffering=1)
    faulthandler.enable(file=_fh, all_threads=True)
except OSError:
    pass

# Add resources/lib/ to sys.path so vendored libraries (PTT) can resolve
# their internal imports (e.g. "from ptt.handlers import ...").
addon_dir = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(addon_dir, "resources", "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

from resources.lib.router import route  # noqa: E402
from resources.lib.script_player import run_tmdb_play  # noqa: E402

if len(sys.argv) > 1 and sys.argv[1] == "tmdb_play":
    run_tmdb_play(sys.argv[2:])
else:
    route(sys.argv)
