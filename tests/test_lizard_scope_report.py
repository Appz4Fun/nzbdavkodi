"""Tests for scripts/lizard_scope_report.py scope classification."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from lizard_scope_report import _normalize_rule, classify_scope  # noqa: E402


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
    assert classify_scope("scripts/pr_agent_context.py") == "scripts"
    assert classify_scope("setup_something.py") == "other"


def test_normalize_rule_strips_every_codacy_severity_suffix():
    assert _normalize_rule("Lizard_ccn-minor") == "ccn"
    assert _normalize_rule("Lizard_nloc-medium") == "nloc"
    assert _normalize_rule("Lizard_parameter-count-medium") == "parameter-count"
    assert _normalize_rule("Lizard_file-nloc-critical") == "file-nloc"


def test_normalize_rule_leaves_unsuffixed_rule_intact():
    assert _normalize_rule("Lizard_ccn") == "ccn"
    assert _normalize_rule("SomethingElse") == "SomethingElse"
