#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Print GitHub PR review threads in a Codex-friendly format."""

import argparse
import json
import subprocess
import sys
import textwrap

PR_JSON_FIELDS = "number,title,url,comments"

REVIEW_THREADS_QUERY = """
query(
  $owner: String!,
  $name: String!,
  $number: Int!,
  $review_threads_after: String
) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $review_threads_after) {
        nodes {
          id
          isResolved
          path
          line
          startLine
          comments(first: 100) {
            nodes {
              id
              author {
                login
              }
              body
              createdAt
              url
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""

REVIEW_THREAD_COMMENTS_QUERY = """
query($thread_id: ID!, $comments_after: String) {
  node(id: $thread_id) {
    ... on PullRequestReviewThread {
      comments(first: 100, after: $comments_after) {
        nodes {
          id
          author {
            login
          }
          body
          createdAt
          url
        }
        pageInfo {
          hasNextPage
          endCursor
        }
      }
    }
  }
}
"""


def run_command(args, cwd=None, check=True):
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def load_json(stdout, description):
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit("Failed to parse {} JSON: {}".format(description, exc))


def split_repo(name_with_owner):
    parts = (name_with_owner or "").split("/")
    if len(parts) != 2 or not all(parts):
        raise SystemExit("GitHub repository must be in OWNER/REPO form")
    return parts[0], parts[1]


def current_pr(pr_number=None, runner=run_command):
    args = ["gh", "pr", "view"]
    if pr_number is not None:
        args.append(str(pr_number))
    args.extend(["--json", PR_JSON_FIELDS])
    return load_json(runner(args).stdout, "PR")


def current_repo(runner=run_command):
    result = runner(["gh", "repo", "view", "--json", "nameWithOwner"])
    payload = load_json(result.stdout, "repository")
    return split_repo(payload.get("nameWithOwner"))


def _graphql(query, variables, runner=run_command):
    args = ["gh", "api", "graphql"]
    for name, value in variables:
        if value is None:
            continue
        flag = "-F" if isinstance(value, int) else "-f"
        args.extend([flag, "{}={}".format(name, value)])
    args.extend(["-f", "query={}".format(query)])
    return load_json(runner(args).stdout, "review thread")


def _page_info(connection):
    return connection.get("pageInfo") or {}


def _has_next_page(connection):
    return bool(_page_info(connection).get("hasNextPage"))


def _end_cursor(connection):
    return _page_info(connection).get("endCursor")


def _fetch_remaining_comments(thread, runner=run_command):
    comments = thread.get("comments") or {}
    if not thread.get("id") or not _has_next_page(comments):
        return thread

    nodes = list(comments.get("nodes") or [])
    comments_after = _end_cursor(comments)
    while comments_after:
        data = _graphql(
            REVIEW_THREAD_COMMENTS_QUERY,
            [
                ("thread_id", thread["id"]),
                ("comments_after", comments_after),
            ],
            runner=runner,
        )
        next_comments = (
            data.get("data", {}).get("node", {}).get("comments", {})
        )
        nodes.extend(next_comments.get("nodes") or [])
        if not _has_next_page(next_comments):
            break
        comments_after = _end_cursor(next_comments)

    comments["nodes"] = nodes
    comments["pageInfo"] = {"hasNextPage": False, "endCursor": None}
    thread["comments"] = comments
    return thread


def review_threads(owner, name, number, runner=run_command):
    all_threads = []
    review_threads_after = None
    last_payload = None

    while True:
        last_payload = _graphql(
            REVIEW_THREADS_QUERY,
            [
                ("owner", owner),
                ("name", name),
                ("number", number),
                ("review_threads_after", review_threads_after),
            ],
            runner=runner,
        )
        pull_request = (
            last_payload.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
        )
        connection = pull_request.get("reviewThreads") or {}
        for thread in connection.get("nodes") or []:
            all_threads.append(_fetch_remaining_comments(thread, runner=runner))

        if not _has_next_page(connection):
            break
        review_threads_after = _end_cursor(connection)
        if not review_threads_after:
            break

    if last_payload is None:
        last_payload = {"data": {"repository": {"pullRequest": {}}}}
    pull_request = (
        last_payload.setdefault("data", {})
        .setdefault("repository", {})
        .setdefault("pullRequest", {})
    )
    pull_request["reviewThreads"] = {
        "nodes": all_threads,
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
    return last_payload


def _thread_line(thread):
    return thread.get("line") or thread.get("startLine") or "?"


def _comment_author(comment):
    author = comment.get("author") or {}
    return author.get("login") or "unknown"


def _comment_payload(comment, fallback_id):
    return {
        "id": comment.get("id") or fallback_id,
        "author": _comment_author(comment),
        "body": (comment.get("body") or "").strip(),
        "created_at": comment.get("createdAt", ""),
        "url": comment.get("url", ""),
    }


def _thread_payload(thread, index):
    comments = []
    for comment_index, comment in enumerate(
        thread.get("comments", {}).get("nodes") or [], 1
    ):
        comments.append(
            _comment_payload(
                comment, "thread-{}-comment-{}".format(index, comment_index)
            )
        )

    return {
        "id": thread.get("id") or "thread-{}".format(index),
        "is_resolved": bool(thread.get("isResolved")),
        "path": thread.get("path") or "(unknown path)",
        "line": _thread_line(thread),
        "comments": comments,
    }


def build_agent_payload(pr, thread_data, include_resolved=False):
    pull_request = (
        thread_data.get("data", {}).get("repository", {}).get("pullRequest", {})
    )
    raw_threads = pull_request.get("reviewThreads", {}).get("nodes") or []
    if not include_resolved:
        raw_threads = [thread for thread in raw_threads if not thread.get("isResolved")]

    issue_comments = []
    for index, comment in enumerate(pr.get("comments") or [], 1):
        issue_comments.append(
            _comment_payload(comment, "issue-comment-{}".format(index))
        )

    thread_payloads = []
    for index, thread in enumerate(raw_threads, 1):
        thread_payloads.append(_thread_payload(thread, index))

    return {
        "pr": {
            "number": pr.get("number"),
            "title": pr.get("title", ""),
            "url": pr.get("url", ""),
        },
        "summary": {
            "issue_comment_count": len(issue_comments),
            "review_thread_count": len(thread_payloads),
            "include_resolved": include_resolved,
        },
        "issue_comments": issue_comments,
        "review_threads": thread_payloads,
    }


def render_comments(pr, thread_data, include_resolved=False):
    payload = build_agent_payload(pr, thread_data, include_resolved=include_resolved)

    lines = [
        "PR #{}: {}".format(pr.get("number"), pr.get("title", "")),
        pr.get("url", ""),
        "",
    ]

    comments = payload["issue_comments"]
    if comments:
        lines.append("Issue comments:")
        for index, comment in enumerate(comments, 1):
            lines.append(
                "[I{}] {} {} at {}".format(
                    index,
                    comment["id"],
                    comment["author"],
                    comment["created_at"],
                )
            )
            if comment["url"]:
                lines.append(comment["url"])
            lines.append(textwrap.indent(comment["body"], "  "))
            lines.append("")

    threads = payload["review_threads"]
    if not threads:
        lines.append("No review threads found.")
        return "\n".join(lines).rstrip() + "\n"

    lines.append("Review threads:")
    for index, thread in enumerate(threads, 1):
        resolved = "resolved" if thread["is_resolved"] else "unresolved"
        lines.append(
            "[{}] {} {}:{} ({})".format(
                index, thread["id"], thread["path"], thread["line"], resolved
            )
        )
        for comment in thread["comments"]:
            lines.append(
                "{} {} at {}".format(
                    comment["id"], comment["author"], comment["created_at"]
                )
            )
            if comment["url"]:
                lines.append(comment["url"])
            lines.append(textwrap.indent(comment["body"], "  "))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_parser():
    parser = argparse.ArgumentParser(
        description="Print PR review comments and review threads."
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="PR number. Defaults to the PR for the current branch.",
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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    pr = current_pr(args.pr)
    owner, name = current_repo()
    threads = review_threads(owner, name, pr["number"])
    if args.json:
        payload = build_agent_payload(pr, threads, args.include_resolved)
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_comments(pr, threads, args.include_resolved))


if __name__ == "__main__":
    main()
