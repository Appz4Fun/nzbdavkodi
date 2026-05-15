#!/usr/bin/env python3
"""Codex-driven review/fix loop with CI monitoring and merge automation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import List, Optional, Sequence, Tuple

COAUTHOR_LINE = "Co-Authored-By: Oz <oz-agent@warp.dev>"


class CommandError(RuntimeError):
    """Raised when a subprocess command fails."""


def _print_step(message: str):
    sys.stdout.write("\n==> {}\n".format(message))
    sys.stdout.flush()


def _run_command(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    cwd: Optional[str] = None,
) -> subprocess.CompletedProcess:
    printable = shlex.join(cmd)
    sys.stdout.write("$ {}\n".format(printable))
    sys.stdout.flush()
    completed = subprocess.run(  # nosec B603
        list(cmd),
        cwd=cwd,
        text=True,
        capture_output=capture_output,
        check=False,
    )
    if capture_output:
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
    if check and completed.returncode != 0:
        raise CommandError(
            "Command failed (exit={}): {}".format(completed.returncode, printable)
        )
    return completed


def _git_output(args: Sequence[str]) -> str:
    completed = _run_command(["git"] + list(args), check=True, capture_output=True)
    return (completed.stdout or "").strip()


def _gh_json(args: Sequence[str]):
    completed = _run_command(["gh"] + list(args), check=True, capture_output=True)
    raw = (completed.stdout or "").strip()
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CommandError(
            "Failed to parse JSON from gh command: {}".format(exc)
        ) from exc


def _gh_json_allow_empty(args: Sequence[str], *, empty_error_text: str = ""):
    completed = _run_command(["gh"] + list(args), check=False, capture_output=True)
    raw = (completed.stdout or "").strip()
    stderr_text = (completed.stderr or "").strip()
    if completed.returncode in (0, 8):
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(
                "Failed to parse JSON from gh command: {}".format(exc)
            ) from exc
    if empty_error_text and (
        empty_error_text in stderr_text or empty_error_text in raw
    ):
        return []
    raise CommandError(
        "gh command failed (exit={}): {}".format(
            completed.returncode, stderr_text or "<no stderr>"
        )
    )


def _git_branch() -> str:
    branch = _git_output(["rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        raise CommandError("Detached HEAD is not supported for this workflow.")
    return branch


def _git_head_sha() -> str:
    return _git_output(["rev-parse", "HEAD"])


def _working_tree_dirty() -> bool:
    return bool(_git_output(["status", "--porcelain"]))


def _checkout_branch(branch: str):
    current_branch = _git_branch()
    if current_branch == branch:
        return
    _print_step("Switching to PR branch {}".format(branch))
    _run_command(["git", "fetch", "origin", branch], check=True, capture_output=True)
    has_local_branch = bool(_git_output(["branch", "--list", branch]))
    if has_local_branch:
        _run_command(["git", "checkout", branch], check=True, capture_output=True)
    else:
        _run_command(
            ["git", "checkout", "-b", branch, "--track", "origin/{}".format(branch)],
            check=True,
            capture_output=True,
        )


def _commit_and_push(iteration_label: str, branch: str):
    if not _working_tree_dirty():
        _print_step("No changes to commit.")
        return
    _print_step("Running local checks (just lint + just test) before commit/push")
    _run_command(["just", "lint"], check=True, capture_output=False)
    _run_command(["just", "test"], check=True, capture_output=False)
    _print_step("Committing Codex-generated changes")
    _run_command(["git", "add", "-A"], check=True, capture_output=True)
    _run_command(
        [
            "git",
            "commit",
            "-m",
            "fix: codex loop {}".format(iteration_label),
            "-m",
            COAUTHOR_LINE,
        ],
        check=True,
        capture_output=True,
    )
    _print_step("Pushing branch {}".format(branch))
    _run_command(["git", "push", "origin", branch], check=True, capture_output=True)


def _codex_exec_with_marker(
    *,
    prompt: str,
    marker_regex: str,
    model: Optional[str],
) -> Tuple[bool, str]:
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="codex-last-msg-", suffix=".txt", delete=False
    ) as temp_file:
        temp_path = temp_file.name
    cmd: List[str] = ["codex", "--yolo", "exec", "-o", temp_path]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    _run_command(cmd, check=True, capture_output=True)
    try:
        with open(temp_path, "r", encoding="utf-8") as handle:
            last_message = handle.read().strip()
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    match = re.search(marker_regex, last_message, flags=re.IGNORECASE | re.MULTILINE)
    return bool(match and match.group(1).lower() == "yes"), last_message


def _run_review_fix_round(round_number: int, base: str, model: Optional[str]) -> bool:
    _print_step("Codex review/fix round {}".format(round_number))
    prompt = textwrap.dedent("""
        Run `/review --base {base}`.
        If `/review` finds actionable issues, fix them in the current repository.
        Then run `/review --base {base}` exactly one more time.
        If no actionable issues remain after that second review, return:
        REVIEW_PASS=yes
        Otherwise return:
        REVIEW_PASS=no
        Respond with exactly one line and nothing else.
        """).strip().format(base=base)
    passed, last_message = _codex_exec_with_marker(
        prompt=prompt,
        marker_regex=r"REVIEW_PASS=(yes|no)",
        model=model,
    )
    _print_step("Codex marker output: {}".format(last_message or "<empty>"))
    return passed


def _pr_view(pr_ref: str):
    return _gh_json(
        [
            "pr",
            "view",
            pr_ref,
            "--json",
            "number,url,state,headRefName,baseRefName,isDraft",
        ]
    )


def _pr_checks(branch: str, required_only: bool):
    args = [
        "pr",
        "checks",
        branch,
        "--json",
        "name,bucket,state,link,workflow",
    ]
    if required_only:
        args.insert(3, "--required")
    return _gh_json_allow_empty(
        args,
        empty_error_text="no required checks reported",
    )


def _required_checks(branch: str):
    checks = _pr_checks(branch, required_only=True)
    if checks:
        return checks
    return _pr_checks(branch, required_only=False)


def _bucket_summary(checks) -> str:
    counts = {}
    for check in checks:
        bucket = check.get("bucket", "unknown")
        counts[bucket] = counts.get(bucket, 0) + 1
    parts = []
    for key in sorted(counts):
        parts.append("{}={}".format(key, counts[key]))
    return ", ".join(parts)


def _failing_checks(checks):
    failing = []
    for check in checks:
        if check.get("bucket") in ("fail", "cancel"):
            name = check.get("name", "<unknown>")
            state = check.get("state", "<unknown>")
            link = check.get("link", "")
            failing.append("- {} [{}] {}".format(name, state, link))
    return "\n".join(failing) if failing else "- <none>"


def _wait_for_required_checks_green(
    *,
    branch: str,
    poll_seconds: int,
    timeout_seconds: int,
) -> Tuple[bool, list]:
    _print_step("Waiting for required checks on PR branch {}".format(branch))
    started = time.time()
    while True:
        checks = _required_checks(branch)
        if not checks:
            _print_step("No required checks reported; treating as green.")
            return True, checks
        summary = _bucket_summary(checks)
        _print_step("Required check buckets: {}".format(summary))
        buckets = {check.get("bucket") for check in checks}
        if buckets.issubset({"pass", "skipping"}):
            return True, checks
        if "pending" not in buckets:
            return False, checks
        if (time.time() - started) > timeout_seconds:
            raise CommandError(
                "Timed out waiting for required checks after {}s.".format(
                    timeout_seconds
                )
            )
        time.sleep(poll_seconds)


def _run_ci_fix_round(
    *,
    round_number: int,
    checks: list,
    model: Optional[str],
) -> bool:
    _print_step("Codex CI repair round {}".format(round_number))
    failing_summary = _failing_checks(checks)
    prompt = textwrap.dedent("""
        Required GitHub checks are failing on this branch.
        Failing checks:
        {failing_summary}

        Diagnose the failures (using gh/github logs as needed), fix the underlying
        code or test issues in this repository, and run any local verification needed.
        Do not commit or push.
        Return exactly one line:
        CI_FIX_READY=yes
        if you made a concrete fix and are ready for commit/push, otherwise:
        CI_FIX_READY=no
        """).strip().format(failing_summary=failing_summary)
    ready, last_message = _codex_exec_with_marker(
        prompt=prompt,
        marker_regex=r"CI_FIX_READY=(yes|no)",
        model=model,
    )
    _print_step("Codex marker output: {}".format(last_message or "<empty>"))
    return ready


def _rebase_onto_base(branch: str, base: str):
    _print_step("Rebasing {} onto origin/{}".format(branch, base))
    _run_command(["git", "fetch", "origin", base], check=True, capture_output=True)
    _run_command(
        ["git", "rebase", "origin/{}".format(base)], check=True, capture_output=True
    )
    _run_command(
        ["git", "push", "--force-with-lease", "origin", branch],
        check=True,
        capture_output=True,
    )


def _merge_pr(branch: str):
    head_sha = _git_head_sha()
    _print_step("Merging PR with rebase strategy")
    _run_command(
        [
            "gh",
            "pr",
            "merge",
            branch,
            "--rebase",
            "--delete-branch",
            "--match-head-commit",
            head_sha,
        ],
        check=True,
        capture_output=True,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a Codex /review repair loop, then CI repair, then rebase+merge."
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="Target PR number. If provided, run against that PR and its head branch.",
    )
    parser.add_argument(
        "--base", default="main", help="Base branch for review and rebase."
    )
    parser.add_argument(
        "--max-review-rounds",
        type=int,
        default=10,
        help="Maximum Codex review/fix rounds before aborting.",
    )
    parser.add_argument(
        "--max-ci-rounds",
        type=int,
        default=10,
        help="Maximum Codex CI fix rounds before aborting.",
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=20,
        help="Polling interval for required checks.",
    )
    parser.add_argument(
        "--checks-timeout-seconds",
        type=int,
        default=5400,
        help="Timeout for a single required-check wait cycle.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional Codex model override passed as --model.",
    )
    parser.add_argument(
        "--skip-rebase",
        action="store_true",
        help="Skip both initial and final rebase steps.",
    )
    parser.add_argument(
        "--skip-merge",
        action="store_true",
        help="Stop after CI is green (and optional rebase), without merging.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if _working_tree_dirty():
        raise CommandError(
            "Working tree is dirty. Commit/stash changes before running this loop."
        )
    current_branch = _git_branch()
    pr_ref = str(args.pr) if args.pr is not None else current_branch
    pr_data = _pr_view(pr_ref)
    if not pr_data:
        raise CommandError("No PR found for reference '{}'.".format(pr_ref))
    if pr_data.get("state") != "OPEN":
        raise CommandError(
            "PR {} is not open (state={}).".format(
                pr_data.get("number"), pr_data.get("state")
            )
        )
    if pr_data.get("isDraft"):
        raise CommandError(
            "PR is in draft state; mark it ready before merge automation."
        )
    pr_base = pr_data.get("baseRefName")
    if pr_base and pr_base != args.base:
        raise CommandError(
            "PR #{} targets base '{}' but --base is '{}'. Use the PR base branch.".format(
                pr_data.get("number"), pr_base, args.base
            )
        )

    branch = pr_data.get("headRefName")
    if not branch:
        raise CommandError("Could not resolve PR head branch.")
    if branch == args.base:
        raise CommandError(
            "PR head branch '{}' matches base '{}'; aborting.".format(branch, args.base)
        )

    _checkout_branch(branch)

    _print_step("Using PR #{} {}".format(pr_data.get("number"), pr_data.get("url", "")))
    if not args.skip_rebase:
        _print_step("Initial rebase before review/fix loop")
        _rebase_onto_base(branch, args.base)

    review_passed = False
    for round_number in range(1, args.max_review_rounds + 1):
        review_passed = _run_review_fix_round(round_number, args.base, args.model)
        _commit_and_push("review round {}".format(round_number), branch)
        if review_passed:
            break
    if not review_passed:
        raise CommandError(
            "Review loop exhausted {} rounds without pass.".format(
                args.max_review_rounds
            )
        )

    ci_green = False
    for round_number in range(1, args.max_ci_rounds + 1):
        ci_green, checks = _wait_for_required_checks_green(
            branch=branch,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.checks_timeout_seconds,
        )
        if ci_green:
            break
        ready = _run_ci_fix_round(
            round_number=round_number,
            checks=checks,
            model=args.model,
        )
        if not ready:
            raise CommandError(
                "Codex reported CI fix not ready in round {}.".format(round_number)
            )
        if not _working_tree_dirty():
            raise CommandError(
                "CI fix round {} produced no file changes; aborting.".format(
                    round_number
                )
            )
        _commit_and_push("ci round {}".format(round_number), branch)
    if not ci_green:
        raise CommandError(
            "CI loop exhausted {} rounds without green checks.".format(
                args.max_ci_rounds
            )
        )

    if not args.skip_rebase:
        _rebase_onto_base(branch, args.base)
        ci_green_after_rebase, _ = _wait_for_required_checks_green(
            branch=branch,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.checks_timeout_seconds,
        )
        if not ci_green_after_rebase:
            raise CommandError("Required checks failed after rebase.")

    if not args.skip_merge:
        _merge_pr(str(pr_data.get("number") or branch))
    _print_step("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CommandError as exc:
        sys.stderr.write("ERROR: {}\n".format(exc))
        raise SystemExit(1)
