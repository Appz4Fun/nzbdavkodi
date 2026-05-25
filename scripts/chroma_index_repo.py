#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Index this repository into the dev-only Chroma Cloud collection."""

import sys

from chroma_dev_search import main_index

if __name__ == "__main__":
    raise SystemExit(main_index(sys.argv[1:]))
