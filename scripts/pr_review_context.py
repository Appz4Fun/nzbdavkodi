#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Print a compact context packet for local or GitHub PR review."""

import argparse
import json
import subprocess
import sys

PR_JSON_FIELDS = (
    "number,title,url,baseRefName,headRefName,statusCheckRollup,reviewDecision"
)
FAILING_CHECK_STATES = ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT")
PENDING_CHECK_STATES = ("PENDING", "QUEUED", "IN_PROGRESS", "REQUESTED")
PASSING_CHECK_STATES = ("SUCCESS", "COMPLETED", "NEUTRAL", "SKIPPED")


def run_command(args, cwd=None, check=True):
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _stdout(result):
    return result.stdout.strip()


def _run_optional(args, runner=run_command, cwd=None):
    try:
        return runner(args, cwd=cwd)
    except (OSError, subprocess.CalledProcessError):
        return None


def _json_optional(args, runner=run_command, cwd=None):
    result = _run_optional(args, runner=runner, cwd=cwd)
    if result is None or result.returncode not in (0, None):
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _git_stdout(args, runner=run_command, cwd=None):
    return _stdout(runner(args, cwd=cwd))


def _split_lines(text):
    if not text:
        return []
    return [line for line in text.splitlines() if line.strip()]


def _summarize_checks(pr):
    if not pr:
        return "UNKNOWN"
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return "UNKNOWN"

    states = []
    for check in checks:
        state = check.get("state") or check.get("conclusion") or check.get("status")
        if state:
            states.append(state.upper())

    if not states:
        return "UNKNOWN"
    if any(state in FAILING_CHECK_STATES for state in states):
        return "FAILING"
    if any(state in PENDING_CHECK_STATES for state in states):
        return "PENDING"
    if all(state in PASSING_CHECK_STATES for state in states):
        return "PASSING"
    return "UNKNOWN"


def _bounded_text(text, max_lines):
    lines = text.splitlines()
    if max_lines is None or len(lines) <= max_lines:
        return text.strip(), False
    return "\n".join(lines[:max_lines]).rstrip(), True


def collect_context(
    base_ref=None, include_diff=False, max_diff_lines=400, runner=run_command
):
    root = _git_stdout(["git", "rev-parse", "--show-toplevel"], runner=runner)
    branch = _git_stdout(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], runner=runner, cwd=root
    )
    head_sha = _git_stdout(["git", "rev-parse", "HEAD"], runner=runner, cwd=root)
    pr = _json_optional(["gh", "pr", "view", "--json", PR_JSON_FIELDS], runner=runner)

    if base_ref is None:
        if pr and pr.get("baseRefName"):
            base_ref = "origin/{}".format(pr["baseRefName"])
        else:
            base_ref = "origin/main"

    merge_base = _git_stdout(["git", "merge-base", base_ref, "HEAD"], runner=runner)
    diff_range = "{}...HEAD".format(base_ref)
    changed_files = _split_lines(
        _git_stdout(
            ["git", "diff", "--name-status", diff_range], runner=runner, cwd=root
        )
    )
    diff_stat = _git_stdout(
        ["git", "diff", "--stat", diff_range], runner=runner, cwd=root
    )
    commits = _split_lines(
        _git_stdout(
            ["git", "log", "--oneline", "{}..HEAD".format(base_ref)],
            runner=runner,
            cwd=root,
        )
    )
    diff = None
    diff_truncated = False
    if include_diff:
        diff_output = _git_stdout(["git", "diff", diff_range], runner=runner, cwd=root)
        diff, diff_truncated = _bounded_text(diff_output, max_diff_lines)

    return {
        "branch": branch,
        "base_ref": base_ref,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "pr": pr,
        "status": _summarize_checks(pr),
        "changed_files": changed_files,
        "diff_stat": diff_stat,
        "commits": commits,
        "diff": diff,
        "diff_truncated": diff_truncated,
    }


def _changed_file_paths(changed_files):
    paths = []
    for line in changed_files:
        parts = line.split("\t")
        if len(parts) >= 3:
            paths.append(parts[-1])
        elif len(parts) >= 2:
            paths.append(parts[1])
    return paths


def render_markdown(data):
    pr = data.get("pr")
    lines = ["# PR Review Context", ""]
    if pr:
        lines.append(
            "- PR: #{} {} ({})".format(
                pr.get("number"), pr.get("title", ""), pr.get("url", "")
            )
        )
        lines.append(
            "- PR base/head: `{}` <- `{}`".format(
                pr.get("baseRefName", "?"), pr.get("headRefName", "?")
            )
        )
        if pr.get("reviewDecision"):
            lines.append("- Review decision: `{}`".format(pr["reviewDecision"]))
    else:
        lines.append("- PR: not detected for current branch")

    lines.extend(
        [
            "- Branch: `{}`".format(data.get("branch", "")),
            "- Base ref: `{}`".format(data.get("base_ref", "")),
            "- Merge base: `{}`".format(data.get("merge_base", "")),
            "- Head SHA: `{}`".format(data.get("head_sha", "")),
            "- Check status: `{}`".format(data.get("status", "UNKNOWN")),
            "",
            "## Changed Files",
            "",
        ]
    )

    changed_files = data.get("changed_files") or []
    if changed_files:
        lines.extend("```text\n{}\n```".format("\n".join(changed_files)).splitlines())
    else:
        lines.append("No changed files.")

    lines.extend(["", "## Diff Stat", ""])
    diff_stat = data.get("diff_stat") or "No diff stat."
    lines.extend("```text\n{}\n```".format(diff_stat).splitlines())

    lines.extend(["", "## Commits", ""])
    commits = data.get("commits") or []
    if commits:
        lines.extend("```text\n{}\n```".format("\n".join(commits)).splitlines())
    else:
        lines.append("No commits ahead of base.")

    lines.extend(["", "## Review Commands", ""])
    lines.extend(
        "```bash\n{}\n```".format(
            "\n".join(
                [
                    "git diff {}...HEAD --stat".format(data.get("base_ref", "")),
                    "git diff {}...HEAD -- <path>".format(data.get("base_ref", "")),
                    "python3 scripts/fetch_comments.py --json",
                    "just lint",
                    "just test",
                ]
            )
        ).splitlines()
    )

    file_paths = _changed_file_paths(changed_files)
    if file_paths:
        lines.extend(["", "## Per-File Diff Commands", ""])
        lines.extend(
            "```bash\n{}\n```".format(
                "\n".join(
                    "git diff {}...HEAD -- {}".format(data.get("base_ref", ""), path)
                    for path in file_paths
                )
            ).splitlines()
        )

    if data.get("diff"):
        lines.extend(["", "## Bounded Diff", ""])
        if data.get("diff_truncated"):
            lines.append(
                "Diff output was truncated. Re-run with a larger "
                "`--max-diff-lines` if needed."
            )
            lines.append("")
        lines.extend("```diff\n{}\n```".format(data["diff"]).splitlines())

    return "\n".join(lines).rstrip() + "\n"


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print branch and PR context for code review."
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Base ref for diff context. Defaults to the PR base or origin/main.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of Markdown.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Include a bounded git diff in the output.",
    )
    parser.add_argument(
        "--max-diff-lines",
        type=int,
        default=400,
        help="Maximum diff lines to emit with --diff. Defaults to 400.",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    data = collect_context(
        base_ref=args.base,
        include_diff=args.diff,
        max_diff_lines=args.max_diff_lines,
    )
    if args.json:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(data))


if __name__ == "__main__":
    main()
