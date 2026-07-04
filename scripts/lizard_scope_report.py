#!/usr/bin/env python3
"""Summarize a codacy-cli Lizard SARIF file by repo scope and rule.

SARIF artifactLocation URIs from codacy-cli's lizard runner contain only
basenames, so paths are recovered via `git ls-files '*.py'`. Ambiguous
basenames are reported under AMBIG:<basename> rather than guessed.
"""

import json
import subprocess
import sys
from collections import Counter, defaultdict

RULES = ("ccn", "nloc", "parameter-count", "file-nloc")

# codacy-cli lizard rule ids look like "Lizard_<metric>-<severity>" where
# severity is minor/medium/critical depending on the configured thresholds.
_SEVERITY_SUFFIXES = ("-minor", "-medium", "-critical")


def _normalize_rule(rule_id):
    rule = rule_id.replace("Lizard_", "")
    for suffix in _SEVERITY_SUFFIXES:
        if rule.endswith(suffix):
            return rule[: -len(suffix)]
    return rule


def classify_scope(path):
    if "/ptt/" in path:
        return "vendored-ptt"
    if path.startswith("tests/"):
        return "tests"
    if path.startswith("tests-extensive/"):
        return "tests-extensive"
    if path.startswith("scripts/"):
        return "scripts"
    if "resources/lib" in path:
        return "shipped-lib"
    return "other"


def _basename_map():
    files = subprocess.check_output(["git", "ls-files", "*.py"], text=True).splitlines()
    bymap = defaultdict(list)
    for f in files:
        bymap[f.rsplit("/", 1)[-1]].append(f)
    return bymap


def summarize(sarif_path):
    with open(sarif_path) as fh:
        doc = json.load(fh)
    bymap = _basename_map()
    counts = Counter()
    for run in doc.get("runs", []):
        for result in run.get("results", []):
            uri = result["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
            rule = _normalize_rule(result.get("ruleId", ""))
            candidates = bymap.get(uri, [uri])
            if len(candidates) > 1:
                scope = "AMBIG:" + uri
            else:
                scope = classify_scope(candidates[0])
            counts[(scope, rule)] += 1
    return counts


def main(argv):
    if len(argv) != 2:
        print("usage: lizard_scope_report.py <lizard.sarif>", file=sys.stderr)
        return 2
    counts = summarize(argv[1])
    scopes = sorted({scope for scope, _ in counts})
    header = (
        "{:<28}".format("scope")
        + "".join("{:>16}".format(rule) for rule in RULES)
        + "{:>8}".format("TOTAL")
    )
    print(header)
    for scope in scopes:
        total = sum(counts[(scope, rule)] for rule in RULES)
        print(
            "{:<28}".format(scope)
            + "".join("{:>16}".format(counts[(scope, rule)]) for rule in RULES)
            + "{:>8}".format(total)
        )
    print("{:<28}{:>8}".format("ALL", sum(counts.values())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
