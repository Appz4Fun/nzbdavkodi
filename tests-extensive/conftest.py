# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Kodi mocks for the tests-extensive/ suites.

``tests-extensive`` contains a hyphen and is therefore not a valid Python
package name.  To work around this:

* The repo root is inserted into sys.path so ``tests.kodi_mocks`` is
  importable (``tests/`` is a namespace package).
* This directory itself is inserted into sys.path so the ``extreme/``
  sub-package is importable as ``extreme`` (without the ``tests-extensive``
  prefix).

This conftest is discovered hierarchically before any test file under
``tests-extensive/``, so sys.path is ready before any import in those files.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent  # nzbdavkodi/
_EXTENSIVE_DIR = Path(__file__).resolve().parent  # nzbdavkodi/tests-extensive/

# Ensure repo root is on sys.path so ``tests.kodi_mocks`` is importable.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Ensure tests-extensive/ is on sys.path so ``extreme`` is importable as a
# top-level package (``extreme/__init__.py`` exists inside this dir).
if str(_EXTENSIVE_DIR) not in sys.path:
    sys.path.insert(0, str(_EXTENSIVE_DIR))

from tests.kodi_mocks import install_kodi_mocks  # noqa: E402

install_kodi_mocks()
