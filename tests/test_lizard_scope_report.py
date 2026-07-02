"""Tests for scripts/lizard_scope_report.py scope classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lizard_scope_report import classify_scope  # noqa: E402


def test_classify_shipped_lib():
    assert (
        classify_scope("repo/plugin.video.nzbdav/resources/lib/router.py")
        == "shipped-lib"
    )


def test_classify_vendored_ptt_wins_over_lib():
    assert (
        classify_scope("repo/plugin.video.nzbdav/resources/lib/ptt/handlers.py")
        == "vendored-ptt"
    )


def test_classify_tests_and_extensive():
    assert classify_scope("tests/test_router.py") == "tests"
    assert classify_scope("tests-extensive/test_live_services.py") == "tests-extensive"


def test_classify_scripts_and_other():
    assert classify_scope("scripts/generate_repo.py") == "scripts"
    assert classify_scope("setup_something.py") == "other"
