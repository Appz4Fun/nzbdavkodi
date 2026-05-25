#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Check local agent-side Chroma MCP and skill wiring."""

import sys

from chroma_dev_search import main_agent_check

if __name__ == "__main__":
    raise SystemExit(main_agent_check(sys.argv[1:]))
