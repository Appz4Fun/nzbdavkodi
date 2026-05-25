#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Check or initialize this repo's dev-only Chroma Cloud configuration."""

import sys

from chroma_dev_search import main_check_config

if __name__ == "__main__":
    raise SystemExit(main_check_config(sys.argv[1:]))
