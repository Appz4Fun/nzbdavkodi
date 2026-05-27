# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

import json
import subprocess

import pytest

from scripts import fetch_comments, pr_agent_context, pr_review_context


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


def test_fetch_comments_paginates_review_threads_and_comments():
    calls = []

    def runner(args, cwd=None, check=True):
        calls.append(args)
        if "thread_id=THREAD_two" in args:
            assert "comments_after=COMMENT_CURSOR" in args
            return completed(
                json.dumps(
                    {
                        "data": {
                            "node": {
                                "comments": {
                                    "nodes": [
                                        {
                                            "id": "COMMENT_two_b",
                                            "author": {"login": "reviewer"},
                                            "body": "Second page comment.",
                                            "createdAt": "2026-05-26T12:02:00Z",
                                            "url": "https://comment-two-b",
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                }
                            }
                        }
                    }
                )
            )
        if "review_threads_after=THREAD_CURSOR" in args:
            return completed(
                json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [
                                            {
                                                "id": "THREAD_two",
                                                "isResolved": False,
                                                "path": "scripts/two.py",
                                                "line": 20,
                                                "comments": {
                                                    "nodes": [
                                                        {
                                                            "id": "COMMENT_two_a",
                                                            "author": {
                                                                "login": "reviewer"
                                                            },
                                                            "body": (
                                                                "First page comment."
                                                            ),
                                                            "createdAt": (
                                                                "2026-05-26T12:01:00Z"
                                                            ),
                                                            "url": (
                                                                "https://comment-two-a"
                                                            ),
                                                        }
                                                    ],
                                                    "pageInfo": {
                                                        "hasNextPage": True,
                                                        "endCursor": "COMMENT_CURSOR",
                                                    },
                                                },
                                            }
                                        ],
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                    }
                                }
                            }
                        }
                    }
                )
            )
        return completed(
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "THREAD_one",
                                            "isResolved": False,
                                            "path": "scripts/one.py",
                                            "line": 10,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "id": "COMMENT_one",
                                                        "author": {"login": "reviewer"},
                                                        "body": "Only comment.",
                                                        "createdAt": (
                                                            "2026-05-26T12:00:00Z"
                                                        ),
                                                        "url": "https://comment-one",
                                                    }
                                                ],
                                                "pageInfo": {
                                                    "hasNextPage": False,
                                                    "endCursor": None,
                                                },
                                            },
                                        }
                                    ],
                                    "pageInfo": {
                                        "hasNextPage": True,
                                        "endCursor": "THREAD_CURSOR",
                                    },
                                }
                            }
                        }
                    }
                }
            )
        )

    data = fetch_comments.review_threads("owner", "repo", 7, runner=runner)
    threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]

    assert [thread["id"] for thread in threads] == ["THREAD_one", "THREAD_two"]
    assert [comment["id"] for comment in threads[1]["comments"]["nodes"]] == [
        "COMMENT_two_a",
        "COMMENT_two_b",
    ]
    assert len(calls) == 3


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


def test_pr_review_context_quotes_diff_command_paths():
    data = {
        "branch": "feature/pr-tools",
        "base_ref": "origin/main",
        "head_sha": "abc123",
        "merge_base": "def456",
        "pr": None,
        "status": "UNKNOWN",
        "changed_files": ["A\tpath with spaces/review helper.py"],
        "diff_stat": " 1 file changed",
        "commits": [],
    }

    output = pr_review_context.render_markdown(data)

    assert (
        "git diff origin/main...HEAD -- 'path with spaces/review helper.py'" in output
    )


def test_pr_review_context_quotes_base_ref_in_review_commands():
    data = {
        "branch": "feature/pr-tools",
        "base_ref": "origin/base&branch",
        "head_sha": "abc123",
        "merge_base": "def456",
        "pr": None,
        "status": "UNKNOWN",
        "changed_files": [],
        "diff_stat": " 1 file changed",
        "commits": [],
    }

    output = pr_review_context.render_markdown(data)

    assert "git diff 'origin/base&branch...HEAD' --stat" in output
    assert "git diff 'origin/base&branch...HEAD' -- <path>" in output


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


def test_pr_review_context_falls_back_when_status_rollup_is_unavailable(tmp_path):
    calls = []

    def runner(args, cwd=None, check=True):
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return completed(str(tmp_path))
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return completed("feature/pr-tools")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return completed("abc123")
        if (
            args[:3] == ["gh", "pr", "view"]
            and pr_review_context.PR_JSON_FIELDS in args
        ):
            raise subprocess.CalledProcessError(
                1, args, stderr="Field 'statusCheckRollup' doesn't exist\n"
            )
        if (
            args[:3] == ["gh", "pr", "view"]
            and pr_review_context.PR_FALLBACK_JSON_FIELDS in args
        ):
            return completed(
                json.dumps(
                    {
                        "number": 7,
                        "title": "Add PR helpers",
                        "url": "https://pr",
                        "baseRefName": "main",
                        "headRefName": "feature/pr-tools",
                        "reviewDecision": "REVIEW_REQUIRED",
                    }
                )
            )
        if args[:4] == ["git", "merge-base", "origin/main", "HEAD"]:
            return completed("def456")
        if args[:3] == ["git", "diff", "--name-status"]:
            return completed("A\tscripts/fetch_comments.py")
        if args[:3] == ["git", "diff", "--stat"]:
            return completed(" 1 file changed, 100 insertions(+)")
        if args[:3] == ["git", "log", "--oneline"]:
            return completed("abc123 Add PR helpers")
        raise AssertionError("unexpected command: {}".format(args))

    data = pr_review_context.collect_context(runner=runner)

    assert data["pr"]["number"] == 7
    assert data["pr"]["baseRefName"] == "main"
    assert data["status"] == "UNKNOWN"
    assert ["gh", "pr", "view", "--json", pr_review_context.PR_JSON_FIELDS] in calls
    assert [
        "gh",
        "pr",
        "view",
        "--json",
        pr_review_context.PR_FALLBACK_JSON_FIELDS,
    ] in calls


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


def test_pr_review_context_rejects_negative_max_diff_lines():
    with pytest.raises(ValueError, match="max_diff_lines"):
        pr_review_context._bounded_text("line0\nline1", -1)

    with pytest.raises(SystemExit):
        pr_review_context.build_parser().parse_args(["--max-diff-lines", "-1"])


def test_pr_agent_context_rejects_negative_max_diff_lines():
    with pytest.raises(SystemExit):
        pr_agent_context.build_parser().parse_args(["--max-diff-lines", "-1"])


def test_pr_review_context_treats_waiting_checks_as_pending():
    status = pr_review_context._summarize_checks(
        {"statusCheckRollup": [{"state": "WAITING"}]}
    )

    assert status == "PENDING"


def test_pr_agent_context_emits_unified_agent_json(tmp_path):
    def runner(args, cwd=None, check=True):
        if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return completed(str(tmp_path))
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return completed("feature/pr-tools")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return completed("abc123")
        if args[:4] == ["git", "merge-base", "origin/main", "HEAD"]:
            return completed("def456")
        if args[:3] == ["git", "diff", "--name-status"]:
            return completed(
                "\n".join(
                    [
                        "A\tscripts/pr_agent_context.py",
                        "R100\tscripts/old.py\tscripts/new.py",
                    ]
                )
            )
        if args[:3] == ["git", "diff", "--stat"]:
            return completed(" 2 files changed, 100 insertions(+)")
        if args[:3] == ["git", "log", "--oneline"]:
            return completed("abc123 Add agent PR wrapper")
        if (
            args[:3] == ["gh", "pr", "view"]
            and pr_review_context.PR_JSON_FIELDS in args
        ):
            return completed(
                json.dumps(
                    {
                        "number": 7,
                        "title": "Add PR helpers",
                        "url": "https://pr",
                        "baseRefName": "main",
                        "headRefName": "feature/pr-tools",
                        "reviewDecision": "REVIEW_REQUIRED",
                        "statusCheckRollup": [{"state": "SUCCESS"}],
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
                )
            )
        if args[:3] == ["gh", "pr", "view"] and fetch_comments.PR_JSON_FIELDS in args:
            raise AssertionError("wrapper should reuse PR data from local context")
        if args[:3] == ["gh", "repo", "view"]:
            return completed(json.dumps({"nameWithOwner": "owner/repo"}))
        if args[:4] == ["gh", "api", "graphql", "-f"]:
            return completed(
                json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequest": {
                                    "reviewThreads": {
                                        "nodes": [
                                            {
                                                "id": "THREAD_one",
                                                "isResolved": False,
                                                "path": "scripts/pr_agent_context.py",
                                                "line": 12,
                                                "comments": {
                                                    "nodes": [
                                                        {
                                                            "id": "COMMENT_one",
                                                            "author": {
                                                                "login": "reviewer"
                                                            },
                                                            "body": "Add tests.",
                                                            "createdAt": (
                                                                "2026-05-26T12:00:00Z"
                                                            ),
                                                            "url": "https://comment",
                                                        }
                                                    ]
                                                },
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    }
                )
            )
        raise AssertionError("unexpected command: {}".format(args))

    payload = pr_agent_context.collect_agent_context(runner=runner)

    assert payload["schema_version"] == 1
    assert payload["pr"]["number"] == 7
    assert payload["git"]["branch"] == "feature/pr-tools"
    assert payload["checks"]["status"] == "PASSING"
    assert payload["files"] == [
        {"status": "A", "path": "scripts/pr_agent_context.py"},
        {"status": "R100", "path": "scripts/new.py", "previous_path": "scripts/old.py"},
    ]
    assert payload["comments"]["summary"]["issue_comment_count"] == 1
    assert payload["comments"]["review_threads"][0]["id"] == "THREAD_one"
    assert payload["commands"]["preferred_json"] == (
        "python3 scripts/pr_agent_context.py --json"
    )
    assert not payload["errors"]


def test_pr_agent_context_preserves_issue_comments_when_threads_fail(tmp_path):
    def runner(args, cwd=None, check=True):
        if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return completed(str(tmp_path))
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return completed("feature/pr-tools")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return completed("abc123")
        if args[:4] == ["git", "merge-base", "origin/main", "HEAD"]:
            return completed("def456")
        if args[:3] == ["git", "diff", "--name-status"]:
            return completed("M\tscripts/pr_agent_context.py")
        if args[:3] == ["git", "diff", "--stat"]:
            return completed(" 1 file changed, 20 insertions(+)")
        if args[:3] == ["git", "log", "--oneline"]:
            return completed("abc123 Add agent PR wrapper")
        if (
            args[:3] == ["gh", "pr", "view"]
            and pr_review_context.PR_JSON_FIELDS in args
        ):
            return completed(
                json.dumps(
                    {
                        "number": 7,
                        "title": "Add PR helpers",
                        "url": "https://pr",
                        "baseRefName": "main",
                        "headRefName": "feature/pr-tools",
                        "statusCheckRollup": [],
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
                )
            )
        if args[:3] == ["gh", "repo", "view"]:
            return completed(json.dumps({"nameWithOwner": "owner/repo"}))
        if args[:4] == ["gh", "api", "graphql", "-f"]:
            raise subprocess.CalledProcessError(
                1, args, stderr="GraphQL rate limit exceeded\n"
            )
        raise AssertionError("unexpected command: {}".format(args))

    payload = pr_agent_context.collect_agent_context(runner=runner)

    assert payload["comments"]["summary"]["issue_comment_count"] == 1
    assert payload["comments"]["issue_comments"][0]["id"] == "ISSUE_comment"
    assert payload["comments"]["summary"]["review_thread_count"] == 0
    assert payload["errors"][0]["stage"] == "review_threads"
    assert payload["errors"][0]["command"].startswith(
        "gh api graphql -f owner=owner -f name=repo -F number=7 -f "
    )
    assert payload["errors"][0]["message"] == "GraphQL rate limit exceeded"


def test_pr_agent_context_quotes_displayed_commands():
    commands = pr_agent_context._commands("origin/base branch")

    assert commands["diff_stat"] == "git diff 'origin/base branch...HEAD' --stat"
    assert commands["diff"] == "git diff 'origin/base branch...HEAD'"


def test_pr_agent_context_keeps_local_context_when_comments_fail(tmp_path):
    def runner(args, cwd=None, check=True):
        if args[:3] == ["git", "rev-parse", "--show-toplevel"]:
            return completed(str(tmp_path))
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return completed("feature/pr-tools")
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return completed("abc123")
        if args[:4] == ["git", "merge-base", "origin/main", "HEAD"]:
            return completed("def456")
        if args[:3] == ["git", "diff", "--name-status"]:
            return completed("M\tscripts/pr_review_context.py")
        if args[:3] == ["git", "diff", "--stat"]:
            return completed(" 1 file changed, 20 insertions(+)")
        if args[:3] == ["git", "log", "--oneline"]:
            return completed("abc123 Add agent PR wrapper")
        if (
            args[:3] == ["gh", "pr", "view"]
            and pr_review_context.PR_JSON_FIELDS in args
        ):
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
        if args[:3] == ["gh", "pr", "view"] and fetch_comments.PR_JSON_FIELDS in args:
            raise subprocess.CalledProcessError(
                1, args, stderr="gh auth token is unavailable\n"
            )
        raise AssertionError("unexpected command: {}".format(args))

    payload = pr_agent_context.collect_agent_context(runner=runner)

    assert payload["git"]["head_sha"] == "abc123"
    assert payload["comments"]["summary"]["issue_comment_count"] == 0
    assert payload["comments"]["summary"]["review_thread_count"] == 0
    assert payload["errors"] == [
        {
            "stage": "comments",
            "command": "gh pr view --json number,title,url,comments",
            "message": "gh auth token is unavailable",
        }
    ]


def test_pr_agent_context_renders_markdown_packet():
    payload = {
        "schema_version": 1,
        "pr": {"number": 7, "title": "Add PR helpers", "url": "https://pr"},
        "git": {
            "branch": "feature/pr-tools",
            "base_ref": "origin/main",
            "head_sha": "abc123",
            "merge_base": "def456",
            "commits": ["abc123 Add agent PR wrapper"],
            "diff_stat": " 1 file changed, 20 insertions(+)",
        },
        "checks": {"status": "PASSING", "review_decision": "REVIEW_REQUIRED"},
        "files": [{"status": "A", "path": "scripts/pr_agent_context.py"}],
        "comments": {
            "summary": {"issue_comment_count": 1, "review_thread_count": 1},
            "issue_comments": [{"id": "ISSUE_comment", "body": "Check CI."}],
            "review_threads": [
                {
                    "id": "THREAD_one",
                    "path": "scripts/pr_agent_context.py",
                    "line": 12,
                    "is_resolved": False,
                    "comments": [{"id": "COMMENT_one", "body": "Add tests."}],
                }
            ],
        },
        "commands": {
            "preferred_json": "python3 scripts/pr_agent_context.py --json",
            "comments_json": "python3 scripts/fetch_comments.py --json",
        },
        "errors": [],
    }

    output = pr_agent_context.render_markdown(payload)

    assert "# Agent PR Context" in output
    assert "PR #7 Add PR helpers" in output
    assert "## Files" in output
    assert "A\tscripts/pr_agent_context.py" in output
    assert "## Comments" in output
    assert "Issue comments: 1" in output
    assert "Review threads: 1" in output
    assert "THREAD_one scripts/pr_agent_context.py:12" in output
    assert "python3 scripts/pr_agent_context.py --json" in output


def test_agents_mentions_preferred_pr_agent_context_wrapper():
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "python3 scripts/pr_agent_context.py --json" in agents
    assert "preferred unified agent context packet" in agents
    assert "fetch_comments.py" in agents
    assert "skill or workflow expects that helper by name" in agents


def completed(stdout):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout + "\n")


PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
