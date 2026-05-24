#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Search this repository's dev-only Chroma Cloud collection."""

import sys

from chroma_dev_search import main_search

if __name__ == "__main__":
    raise SystemExit(main_search(sys.argv[1:]))
