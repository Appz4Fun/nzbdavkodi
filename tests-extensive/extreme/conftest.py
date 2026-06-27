# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

# The session-scoped harness fixtures for the extreme functional test
# (stack_ready, run_dir, env_loaded, compose_up, …) live in _fixtures.py.
# tests-extensive/test_extreme_functional.py loads them explicitly via
# pytest_plugins = ["extreme._fixtures"].  The unit tests inside
# tests-extensive/extreme/tests/ (test_measurement, test_fault_proxy,
# test_storage_discovery) are pure unit tests that do not need those
# session-scoped fixtures, so nothing is declared here.
