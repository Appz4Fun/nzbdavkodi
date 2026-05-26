# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
import subprocess

import pytest

from scripts import fetch_comments, pr_review_context


def test_fetch_comments_formats_unresolved_review_threads():
    pr = {"number": 7, "title": "Tighten proxy fallback", "url": "https://pr"}
    thread_data = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "isResolved": False,
                                "path": (
                                    "repo/plugin.video.nzbdav/resources/lib/resolver.py"
                                ),
                                "line": 42,
                                "id": "THREAD_unresolved",
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "COMMENT_timeout",
                                            "author": {"login": "reviewer"},
                                            "body": "This misses the timeout path.",
                                            "createdAt": "2026-05-26T12:00:00Z",
                                            "url": "https://comment",
                                        }
                                    ]
                                },
                            },
                            {
                                "isResolved": True,
                                "path": "tests/test_resolver.py",
                                "line": 9,
                                "comments": {"nodes": []},
                            },
                        ]
                    }
                }
            }
        }
    }

    output = fetch_comments.render_comments(pr, thread_data, include_resolved=False)

    assert "PR #7: Tighten proxy fallback" in output
    assert (
        "[1] THREAD_unresolved repo/plugin.video.nzbdav/resources/lib/resolver.py:42"
        in output
    )
    assert "COMMENT_timeout reviewer at 2026-05-26T12:00:00Z" in output
    assert "reviewer at 2026-05-26T12:00:00Z" in output
    assert "This misses the timeout path." in output
    assert "tests/test_resolver.py" not in output


def test_fetch_comments_reports_no_comments():
    pr = {"number": 7, "title": "Tighten proxy fallback", "url": "https://pr"}
    thread_data = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}
    }

    output = fetch_comments.render_comments(pr, thread_data)

    assert "No review threads found." in output


def test_fetch_comments_emits_agent_json_with_stable_ids():
    pr = {
        "number": 7,
        "title": "Tighten proxy fallback",
        "url": "https://pr",
        "comments": [
            {
                "id": "ISSUE_comment",
                "author": {"login": "maintainer"},
                "body": "Please check CI.",
                "createdAt": "2026-05-26T11:00:00Z",
                "url": "https://issue-comment",
            }
        ],
    }
    thread_data = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "id": "THREAD_unresolved",
                                "isResolved": False,
                                "path": (
                                    "repo/plugin.video.nzbdav/resources/lib/resolver.py"
                                ),
                                "line": 42,
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "COMMENT_timeout",
                                            "author": {"login": "reviewer"},
                                            "body": "This misses the timeout path.",
                                            "createdAt": "2026-05-26T12:00:00Z",
                                            "url": "https://comment",
                                        }
                                    ]
                                },
                            },
                            {
                                "id": "THREAD_resolved",
                                "isResolved": True,
                                "path": "tests/test_resolver.py",
                                "line": 9,
                                "comments": {"nodes": []},
                            },
                        ]
                    }
                }
            }
        }
    }

    payload = fetch_comments.build_agent_payload(
        pr, thread_data, include_resolved=False
    )

    assert payload["pr"]["number"] == 7
    assert payload["summary"]["issue_comment_count"] == 1
    assert payload["summary"]["review_thread_count"] == 1
    assert payload["issue_comments"][0]["id"] == "ISSUE_comment"
    assert payload["review_threads"][0]["id"] == "THREAD_unresolved"
    assert payload["review_threads"][0]["comments"][0]["id"] == "COMMENT_timeout"
    assert payload["review_threads"][0]["path"] == (
        "repo/plugin.video.nzbdav/resources/lib/resolver.py"
    )
    assert payload["review_threads"][0]["line"] == 42


def test_fetch_comments_rejects_malformed_repo_name():
    with pytest.raises(SystemExit, match="OWNER/REPO"):
        fetch_comments.split_repo("not-a-slash-repo")


def test_pr_review_context_renders_markdown_packet():
    data = {
        "branch": "feature/pr-tools",
        "base_ref": "origin/main",
        "head_sha": "abc123",
        "merge_base": "def456",
        "pr": {
            "number": 7,
            "title": "Add PR helpers",
            "url": "https://pr",
            "baseRefName": "main",
            "headRefName": "feature/pr-tools",
            "reviewDecision": "CHANGES_REQUESTED",
        },
        "status": "PASSING",
        "changed_files": [
            "A\tscripts/fetch_comments.py",
            "A\tscripts/pr_review_context.py",
        ],
        "diff_stat": " 2 files changed, 200 insertions(+)",
        "commits": ["abc123 Add PR helpers"],
        "diff": "diff --git a/scripts/a.py b/scripts/a.py\n+print('hello')",
        "diff_truncated": False,
    }

    output = pr_review_context.render_markdown(data)

    assert "# PR Review Context" in output
    assert "- PR: #7 Add PR helpers (https://pr)" in output
    assert "- Review decision: `CHANGES_REQUESTED`" in output
    assert "- Branch: `feature/pr-tools`" in output
    assert "A\tscripts/fetch_comments.py" in output
    assert "2 files changed" in output
    assert "abc123 Add PR helpers" in output
    assert "## Review Commands" in output
    assert "git diff origin/main...HEAD -- scripts/fetch_comments.py" in output
    assert "## Bounded Diff" in output
    assert "+print('hello')" in output


def test_pr_review_context_omits_diff_by_default():
    data = {
        "branch": "feature/pr-tools",
        "base_ref": "origin/main",
        "head_sha": "abc123",
        "merge_base": "def456",
        "pr": None,
        "status": "UNKNOWN",
        "changed_files": [],
        "diff_stat": "",
        "commits": [],
    }

    output = pr_review_context.render_markdown(data)

    assert "## Bounded Diff" not in output


def test_pr_review_context_collects_data_with_injected_runner(tmp_path):
    calls = []

    def runner(args, cwd=None, check=True):
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return completed(str(tmp_path))
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return completed("feature/pr-tools")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return completed("abc123")
        if args[:4] == ["git", "merge-base", "origin/main", "HEAD"]:
            return completed("def456")
        if args[:3] == ["git", "diff", "--name-status"]:
            return completed("A\tscripts/fetch_comments.py")
        if args[:3] == ["git", "diff", "--stat"]:
            return completed(" 1 file changed, 100 insertions(+)")
        if args[:3] == ["git", "log", "--oneline"]:
            return completed("abc123 Add PR helpers")
        if args[:3] == ["gh", "pr", "view"] and "--json" in args:
            return completed(
                json.dumps(
                    {
                        "number": 7,
                        "title": "Add PR helpers",
                        "url": "https://pr",
                        "baseRefName": "main",
                        "headRefName": "feature/pr-tools",
                        "statusCheckRollup": [],
                    }
                )
            )
        raise AssertionError("unexpected command: {}".format(args))

    data = pr_review_context.collect_context(runner=runner)

    assert data["branch"] == "feature/pr-tools"
    assert data["base_ref"] == "origin/main"
    assert data["head_sha"] == "abc123"
    assert data["merge_base"] == "def456"
    assert data["status"] == "UNKNOWN"
    assert data["changed_files"] == ["A\tscripts/fetch_comments.py"]
    assert ["gh", "pr", "view", "--json", pr_review_context.PR_JSON_FIELDS] in calls


def test_pr_review_context_collects_bounded_diff_when_requested(tmp_path):
    def runner(args, cwd=None, check=True):
        if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return completed(str(tmp_path))
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return completed("feature/pr-tools")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return completed("abc123")
        if args[:3] == ["gh", "pr", "view"]:
            raise subprocess.CalledProcessError(1, args)
        if args[:4] == ["git", "merge-base", "origin/main", "HEAD"]:
            return completed("def456")
        if args[:3] == ["git", "diff", "--name-status"]:
            return completed("M\tscripts/pr_review_context.py")
        if args[:3] == ["git", "diff", "--stat"]:
            return completed(" 1 file changed, 100 insertions(+)")
        if args[:3] == ["git", "log", "--oneline"]:
            return completed("abc123 Add PR helpers")
        if args[:2] == ["git", "diff"] and "--" not in args:
            return completed("\n".join(["line{}".format(index) for index in range(5)]))
        raise AssertionError("unexpected command: {}".format(args))

    data = pr_review_context.collect_context(
        include_diff=True, max_diff_lines=3, runner=runner
    )

    assert data["diff"] == "line0\nline1\nline2"
    assert data["diff_truncated"] is True


def completed(stdout):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout + "\n")
