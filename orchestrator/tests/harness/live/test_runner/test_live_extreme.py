"""No-Kodi live extreme harness.

This suite intentionally stops at the HTTP orchestrator boundary. It exercises
real Hydra search and, when explicitly enabled, real nzbdav-rs resolve plus
peer validation. Stream fault/cutover assertions belong here after the Rust
stream proxy exists.
"""

import os

import pytest
from live_common import (
    NZBDAV_URL,
    ORCHESTRATOR,
    corpus_sample,
    fetch_resolve_events,
    report_dir,
    resolve_candidate,
    search_movie,
    seed_nzbdav,
    validated_peer_count,
    wait,
    write_json,
    write_jsonl,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("LIVE_EXTREME", "0") != "1",
    reason="Set LIVE_EXTREME=1 to run no-Kodi live extreme tests",
)

FULL_RESOLVE = os.environ.get("LIVE_EXTREME_FULL_RESOLVE", "0") == "1"


@pytest.fixture(scope="session", autouse=True)
def stack_ready():
    wait(f"{ORCHESTRATOR}/v1/health")
    wait(f"{NZBDAV_URL}/health")
    seed_nzbdav()


@pytest.fixture(scope="session", name="run_dir")
def _live_extreme_run_dir():
    path = report_dir()
    print(f"[live-extreme] reports={path}")
    return path


def test_live_extreme_search_corpus_reports_candidates(run_dir):
    movies = corpus_sample()
    results = []

    for movie in movies:
        search = search_movie(movie)
        candidates = search.get("candidates", [])
        provider_errors = [
            "{provider}: {error}".format(
                provider=provider.get("provider", "unknown"),
                error=provider["error"],
            )
            for provider in search.get("providers", [])
            if provider.get("error")
        ]
        results.append(
            {
                "movie": movie,
                "total_candidates": search.get("total_candidates", 0),
                "candidate_count": len(candidates),
                "provider_outcomes": search.get("providers", []),
                "provider_errors": provider_errors,
                "top_candidates": [
                    {
                        "title": candidate.get("title"),
                        "size": candidate.get("size"),
                        "indexer": candidate.get("indexer"),
                    }
                    for candidate in candidates[:10]
                ],
            }
        )

    write_json(
        run_dir / "manifest.json",
        {
            "mode": "search",
            "sample_size": len(movies),
            "movies": movies,
        },
    )
    write_json(run_dir / "search_results.json", results)
    write_json(
        run_dir / "summary.json",
        {
            "movies_checked": len(results),
            "movies_with_candidates": sum(
                1 for row in results if row["candidate_count"] > 0
            ),
            "candidate_counts": {
                row["movie"]["imdb"]: row["candidate_count"] for row in results
            },
            "provider_errors": {
                row["movie"]["imdb"]: row["provider_errors"]
                for row in results
                if row["provider_errors"]
            },
        },
    )
    (run_dir / "summary.md").write_text(
        "\n".join(
            [
                "# Live No-Kodi Extreme Summary",
                "",
                "| IMDb | title | candidates |",
                "|---|---|---:|",
                *[
                    "| {imdb} | {title} | {count} |".format(
                        imdb=row["movie"]["imdb"],
                        title=row["movie"]["title"],
                        count=row["candidate_count"],
                    )
                    for row in results
                ],
                "",
            ]
        ),
        encoding="utf-8",
    )

    if not any(row["candidate_count"] > 0 for row in results):
        provider_errors = [
            error for row in results for error in row.get("provider_errors", [])
        ]
        detail = (
            "; ".join(provider_errors)
            if provider_errors
            else "provider quota or availability may be exhausted"
        )
        pytest.skip(
            "No live Hydra candidates returned for sampled corpus; "
            f"{detail}; report={run_dir}"
        )


@pytest.mark.skipif(
    not FULL_RESOLVE,
    reason="Set LIVE_EXTREME_FULL_RESOLVE=1 to run real resolve/peer validation",
)
def test_live_extreme_resolve_validates_candidate_peers(run_dir):
    # LIVE_EXTREME_MIN_VALIDATED_PEERS controls whether we hard-fail on 0 peers.
    # Default is 0 because omgwtfnzbs does not cross-post NZBs, so the Jaccard
    # gate finds no peers unless a second cross-posting indexer is configured.
    # Set to 1 to assert at least one byte-sample-validated peer exists.
    min_validated = int(os.environ.get("LIVE_EXTREME_MIN_VALIDATED_PEERS", "0"))
    last_error = None

    for movie in corpus_sample():
        search = search_movie(movie)
        candidates = [
            candidate
            for candidate in search.get("candidates", [])
            if candidate.get("nzb_url") and candidate.get("title")
        ]
        if not candidates:
            last_error = f"{movie['imdb']} returned no usable candidates"
            continue

        try:
            resolved = resolve_candidate(movie, candidates)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{movie['imdb']} resolve failed: {exc}"
            continue

        events = fetch_resolve_events(resolved["resolve_id"])
        write_json(run_dir / "resolve_response.json", resolved)
        write_jsonl(run_dir / "resolve_events.jsonl", events)

        assert resolved.get("stream_url"), "resolve did not return a stream_url"
        assert resolved.get("peers"), "resolve did not return peer metadata"

        n_validated = validated_peer_count(resolved)
        n_peers = len(resolved.get("peers", []))
        # Always print so the result is visible whether or not we assert.
        print(
            f"\n[peer-validation] {movie['imdb']} ({movie['title']}) "
            f"peers_returned={n_peers} byte_sample_validated={n_validated}"
        )
        if min_validated > 0:
            assert n_validated >= min_validated, (
                f"validated peer count {n_validated} < threshold {min_validated}. "
                "Tip: omgwtfnzbs does not cross-post; set LIVE_EXTREME_MIN_VALIDATED_PEERS=0 "
                "or add a cross-posting indexer to Hydra2 to satisfy this threshold."
            )

        assert any(event.get("event") == "resolve.completed" for event in events), (
            "resolve.completed event missing from SSE stream"
        )
        return

    pytest.fail(f"no corpus title could be resolved; last_error={last_error}")
