# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import os
import sys

# Add resources/lib/ to sys.path so vendored libraries (PTT) can resolve
# their internal imports (e.g. "from ptt.handlers import ...").
addon_dir = os.path.dirname(os.path.abspath(__file__))
lib_path = os.path.join(addon_dir, "resources", "lib")
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)

from resources.lib.router import route  # noqa: E402

route(sys.argv)
