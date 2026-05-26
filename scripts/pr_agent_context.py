#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Print a unified PR context packet for AI agent review workflows."""

import argparse
import json
import subprocess
import sys

try:
    from scripts import fetch_comments, pr_review_context
except ImportError:  # pragma: no cover - supports direct script execution.
    import fetch_comments
    import pr_review_context

SCHEMA_VERSION = 1


def _command_text(args):
    return " ".join(str(arg) for arg in args)


def _error_payload(stage, exc):
    command = ""
    message = str(exc).strip()
    if isinstance(exc, subprocess.CalledProcessError):
        command = _command_text(exc.cmd)
        message = (exc.stderr or exc.stdout or str(exc)).strip()
    if isinstance(exc, SystemExit):
        message = str(exc).strip()
    return {
        "stage": stage,
        "command": command,
        "message": message,
    }


def _empty_comments(include_resolved=False):
    return {
        "pr": {"number": None, "title": "", "url": ""},
        "summary": {
            "issue_comment_count": 0,
            "review_thread_count": 0,
            "include_resolved": include_resolved,
        },
        "issue_comments": [],
        "review_threads": [],
    }


def _file_payloads(changed_files):
    files = []
    for line in changed_files:
        parts = line.split("\t")
        if len(parts) >= 3:
            files.append(
                {
                    "status": parts[0],
                    "path": parts[-1],
                    "previous_path": parts[-2],
                }
            )
        elif len(parts) >= 2:
            files.append({"status": parts[0], "path": parts[1]})
    return files


def _pr_payload(context):
    pr = context.get("pr") or {}
    return {
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "url": pr.get("url", ""),
        "base_ref": pr.get("baseRefName", ""),
        "head_ref": pr.get("headRefName", ""),
        "review_decision": pr.get("reviewDecision"),
    }


def _git_payload(context):
    payload = {
        "branch": context.get("branch", ""),
        "base_ref": context.get("base_ref", ""),
        "head_sha": context.get("head_sha", ""),
        "merge_base": context.get("merge_base", ""),
        "commits": context.get("commits") or [],
        "diff_stat": context.get("diff_stat", ""),
    }
    if context.get("diff") is not None:
        payload["diff"] = context.get("diff")
        payload["diff_truncated"] = bool(context.get("diff_truncated"))
    return payload


def _checks_payload(context):
    pr = context.get("pr") or {}
    return {
        "status": context.get("status", "UNKNOWN"),
        "review_decision": pr.get("reviewDecision"),
        "rollup": pr.get("statusCheckRollup") or [],
    }


def _commands(base_ref):
    diff_range = "{}...HEAD".format(base_ref or "origin/main")
    return {
        "preferred_json": "python3 scripts/pr_agent_context.py --json",
        "preferred_markdown": "python3 scripts/pr_agent_context.py",
        "local_context_json": "python3 scripts/pr_review_context.py --json",
        "comments_json": "python3 scripts/fetch_comments.py --json",
        "diff_stat": "git diff {} --stat".format(diff_range),
        "diff": "git diff {}".format(diff_range),
        "lint": "just lint",
        "test": "just test",
    }


def _collect_comments(pr_number, include_resolved, runner):
    pr = fetch_comments.current_pr(pr_number, runner=runner)
    owner, name = fetch_comments.current_repo(runner=runner)
    threads = fetch_comments.review_threads(owner, name, pr["number"], runner=runner)
    return fetch_comments.build_agent_payload(
        pr, threads, include_resolved=include_resolved
    )


def collect_agent_context(
    base_ref=None,
    pr_number=None,
    include_resolved=False,
    include_diff=False,
    max_diff_lines=400,
    runner=pr_review_context.run_command,
):
    context = pr_review_context.collect_context(
        base_ref=base_ref,
        include_diff=include_diff,
        max_diff_lines=max_diff_lines,
        runner=runner,
    )
    errors = []
    try:
        comments = _collect_comments(pr_number, include_resolved, runner)
    except (OSError, subprocess.CalledProcessError, SystemExit) as exc:
        comments = _empty_comments(include_resolved=include_resolved)
        errors.append(_error_payload("comments", exc))

    return {
        "schema_version": SCHEMA_VERSION,
        "pr": _pr_payload(context),
        "git": _git_payload(context),
        "checks": _checks_payload(context),
        "files": _file_payloads(context.get("changed_files") or []),
        "comments": comments,
        "commands": _commands(context.get("base_ref")),
        "errors": errors,
    }


def render_markdown(payload):
    pr = payload.get("pr") or {}
    git = payload.get("git") or {}
    checks = payload.get("checks") or {}
    comments = payload.get("comments") or _empty_comments()
    summary = comments.get("summary") or {}
    lines = ["# Agent PR Context", ""]

    if pr.get("number"):
        lines.append(
            "- PR #{} {} ({})".format(
                pr.get("number"), pr.get("title", ""), pr.get("url", "")
            )
        )
    else:
        lines.append("- PR: not detected for current branch")
    if pr.get("base_ref") or pr.get("head_ref"):
        lines.append(
            "- PR base/head: `{}` <- `{}`".format(
                pr.get("base_ref", "?"), pr.get("head_ref", "?")
            )
        )

    lines.extend(
        [
            "- Branch: `{}`".format(git.get("branch", "")),
            "- Base ref: `{}`".format(git.get("base_ref", "")),
            "- Merge base: `{}`".format(git.get("merge_base", "")),
            "- Head SHA: `{}`".format(git.get("head_sha", "")),
            "- Check status: `{}`".format(checks.get("status", "UNKNOWN")),
        ]
    )
    if checks.get("review_decision"):
        lines.append("- Review decision: `{}`".format(checks["review_decision"]))

    lines.extend(["", "## Files", ""])
    files = payload.get("files") or []
    if files:
        file_lines = []
        for item in files:
            if item.get("previous_path"):
                file_lines.append(
                    "{}\t{}\t{}".format(
                        item.get("status", ""),
                        item["previous_path"],
                        item.get("path", ""),
                    )
                )
            else:
                file_lines.append("{}\t{}".format(item.get("status", ""), item["path"]))
        lines.extend("```text\n{}\n```".format("\n".join(file_lines)).splitlines())
    else:
        lines.append("No changed files.")

    lines.extend(["", "## Comments", ""])
    lines.append("- Issue comments: {}".format(summary.get("issue_comment_count", 0)))
    lines.append("- Review threads: {}".format(summary.get("review_thread_count", 0)))
    for thread in comments.get("review_threads") or []:
        resolved = "resolved" if thread.get("is_resolved") else "unresolved"
        lines.append(
            "- {} {}:{} ({})".format(
                thread.get("id", ""),
                thread.get("path", ""),
                thread.get("line", "?"),
                resolved,
            )
        )

    errors = payload.get("errors") or []
    if errors:
        lines.extend(["", "## Errors", ""])
        for error in errors:
            lines.append(
                "- {}: {}".format(error.get("stage", ""), error.get("message", ""))
            )

    lines.extend(["", "## Commands", ""])
    command_lines = []
    commands = payload.get("commands") or {}
    for key in sorted(commands):
        command_lines.append("{}: {}".format(key, commands[key]))
    lines.extend("```text\n{}\n```".format("\n".join(command_lines)).splitlines())

    return "\n".join(lines).rstrip() + "\n"


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print unified PR context for AI agent review workflows."
    )
    parser.add_argument(
        "--base",
        default=None,
        help="Base ref for diff context. Defaults to the PR base or origin/main.",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="PR number for GitHub comments. Defaults to the current branch PR.",
    )
    parser.add_argument(
        "--include-resolved",
        action="store_true",
        help="Include resolved review threads.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of Markdown.",
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
    payload = collect_agent_context(
        base_ref=args.base,
        pr_number=args.pr,
        include_resolved=args.include_resolved,
        include_diff=args.diff,
        max_diff_lines=args.max_diff_lines,
    )
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_markdown(payload))


if __name__ == "__main__":
    main()
