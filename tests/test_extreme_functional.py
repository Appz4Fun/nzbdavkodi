"""End-to-end extreme functional test.

Run: `just extreme-functional-test`

Spec: docs/superpowers/specs/2026-05-09-extreme-functional-test-design.md
Plan: docs/superpowers/plans/2026-05-09-extreme-functional-test.md

This test depends on session-scoped fixtures in tests/extreme/conftest.py.

Import notes (adjusted from plan):
- _most_duplicated_group_pool() returns a 2-tuple (group_str, pool_list),
  not a flat list. The plan code was corrected to unpack it.
- LIVE_FALLBACK_REQUIRED_COUNT has no effect in the existing helpers; the
  actual knob is FUNCTIONAL_MIN_FALLBACK_CANDIDATES (used by
  _required_fallback_candidate_count()). The setdefault call below uses
  the correct env var name.
- KODI_HOST_PORT / FAULT_PROXY_CONTROL_HOST_PORT from conftest are strings.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from tests.extreme import measurement
from tests.extreme.conftest import (
    EXTREME_DIR,
    FAULT_PROXY_CONTROL_HOST_PORT,
    KODI_HOST_PORT,
    NZBDAV_HOST_PORT,
)

pytestmark = pytest.mark.extreme

# Wider candidate pool than just functional-test-top-imdb. Setting these via
# os.environ affects the existing helpers in test_functional_fallback_playback.
os.environ.setdefault("LIVE_FALLBACK_POOL_LIMIT", "100")
# The actual knob consumed by _required_fallback_candidate_count() is
# FUNCTIONAL_MIN_FALLBACK_CANDIDATES, not LIVE_FALLBACK_REQUIRED_COUNT.
os.environ.setdefault("FUNCTIONAL_MIN_FALLBACK_CANDIDATES", "2")

from tests.test_functional_fallback_playback import (  # noqa: E402
    IMDB_TOP_50_MOVIES,
    _addon_settings,
    _live_env,
    _most_duplicated_group_pool,
    _movie_search_pool,
    _selection_pairs_for_targets,
)


FAULT_TYPES = [
    "connection_reset",
    "http_500",
    "slow_upstream",
    "truncated_response",
    "corrupted_bytes",
]


def _seed() -> int:
    raw = os.environ.get("EXTREME_SEED")
    return int(raw) if raw else int(time.time())


def _post_schedule(events: list[dict]) -> None:
    body = json.dumps({"events": events}).encode("utf-8")
    req = urllib.request.Request(
        f"http://localhost:{FAULT_PROXY_CONTROL_HOST_PORT}/control/schedule",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200, r.status


def _kodi_rpc(method: str, params: dict | None = None, request_id: int = 1) -> dict:
    import base64

    body = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": request_id}
    ).encode("utf-8")
    auth = "Basic " + base64.b64encode(b"kodi:kodi").decode()
    req = urllib.request.Request(
        f"http://localhost:{KODI_HOST_PORT}/jsonrpc",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": auth},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _generate_fault_schedule(rng: random.Random) -> list[dict]:
    """5 random times in [60, 1140], min 60s apart, with shuffled fault types."""
    while True:
        candidates = sorted(rng.sample(range(60, 1140), 5))
        if all(b - a >= 60 for a, b in zip(candidates, candidates[1:])):
            break
    types = FAULT_TYPES.copy()
    rng.shuffle(types)
    return [
        {"at_seconds": float(t), "fault_type": ft}
        for t, ft in zip(candidates, types)
    ]


def _pick_movie_with_fallback_pool(rng: random.Random, settings):
    """Try up to 3 random movies; return (movie, primary_pair, fallback_pairs).

    _most_duplicated_group_pool returns (group_str, pool_list); we unpack it
    and check the pool list length, not the 2-tuple length.
    """
    pool_movies = list(IMDB_TOP_50_MOVIES)
    rng.shuffle(pool_movies)
    last_error = None
    for movie in pool_movies[:3]:
        try:
            pool = _movie_search_pool(settings, movie)
            group_str, group_pool = _most_duplicated_group_pool(pool)
            if not group_pool or len(group_pool) < 2:
                last_error = f"no fallback group for {movie['title']}"
                continue
            pairs = _selection_pairs_for_targets(settings, group_pool, group_pool)
            if not pairs:
                last_error = f"no selection pairs for {movie['title']}"
                continue
            primary, fallbacks = pairs[0], pairs[1:]
            return movie, primary, fallbacks
        except Exception as exc:
            last_error = f"{movie['title']}: {exc}"
            continue
    pytest.fail(f"could not find a movie with a fallback pool: {last_error}")


def _wait_for_player(timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _kodi_rpc("Player.GetActivePlayers")
        if resp.get("result"):
            return resp["result"][0]["playerid"]
        time.sleep(1)
    return None


def test_extreme_fallback_run(stack_ready, run_dir):
    rng = random.Random(_seed())

    # Pre-flight health checks
    hydra = os.environ["HYDRA_URL"].rstrip("/")
    api_key = os.environ["HYDRA_API_KEY"]
    with urllib.request.urlopen(
        f"{hydra}/api?apikey={api_key}&t=caps", timeout=10,
    ) as r:
        assert r.status == 200, "Hydra not reachable"
    with urllib.request.urlopen(
        f"http://localhost:{FAULT_PROXY_CONTROL_HOST_PORT}/control/health",
        timeout=5,
    ) as r:
        assert r.status == 200, "Fault proxy not reachable"

    settings = _addon_settings(_live_env())
    movie, primary, fallbacks = _pick_movie_with_fallback_pool(rng, settings)

    schedule = _generate_fault_schedule(rng)
    _post_schedule(schedule)

    measurement.write_manifest(
        run_dir / "manifest.json",
        {
            "seed": _seed(),
            "movie": {
                "title": movie["title"],
                "year": movie["year"],
                "imdb": movie["imdb"],
            },
            "primary_nzb": primary[0].get("title") if primary else None,
            "fallback_count": len(fallbacks),
            "schedule": schedule,
            "started_at_wall": time.time(),
        },
    )

    # Start playback via TMDBHelper.
    tmdb_id = movie.get("tmdb_id")
    if tmdb_id:
        rpc_resp = _kodi_rpc(
            "Addons.ExecuteAddon",
            {
                "addonid": "plugin.video.themoviedb.helper",
                "params": {"info": "play", "type": "movie", "tmdb_id": str(tmdb_id)},
            },
        )
    else:
        # No tmdb_id in the pinned IMDb top-50 list — fall through to plugin URL.
        rpc_resp = _kodi_rpc(
            "Player.Open",
            {
                "item": {
                    "file": (
                        f"plugin://plugin.video.nzbdav/play"
                        f"?title={movie['title']}&year={movie['year']}"
                        f"&imdb={movie['imdb']}"
                    )
                },
            },
        )
    print(f"[extreme] playback launch response: {rpc_resp}")

    pid = _wait_for_player(timeout=60)
    if pid is None:
        # Save what we have and bail.
        measurement.write_manifest(
            run_dir / "manifest.json",
            {"seed": _seed(), "playback_started": False},
        )
        pytest.fail("playback never started")

    poller = measurement.PlayerPoller(
        url=f"http://localhost:{KODI_HOST_PORT}/jsonrpc",
        auth=("kodi", "kodi"),
        interval=0.25,
        output_path=run_dir / "timeline.jsonl",
    )
    poller.start()

    try:
        time.sleep(1200)  # 20 minutes
    finally:
        poller.stop()
        poller.join(timeout=5)
        try:
            _kodi_rpc("Player.Stop", {"playerid": pid})
        except Exception:
            pass

    # Pull container logs into the run dir
    for container, name in [
        ("nzbdav-extreme-kodi", "kodi.log"),
        ("nzbdav-extreme-nzbdav", "nzbdav-rs.log"),
    ]:
        with (run_dir / name).open("wb") as fh:
            subprocess.run(
                ["docker", "logs", container], check=False, stdout=fh, stderr=fh
            )

    # Pull kodi.log out of the container too
    subprocess.run(
        [
            "docker",
            "cp",
            "nzbdav-extreme-kodi:/root/.kodi/temp/kodi.log",
            str(run_dir / "kodi-temp.log"),
        ],
        check=False,
    )

    # Read fault-proxy events.jsonl from the bind-mounted reports dir
    fault_log = run_dir / "fault-proxy" / "events.jsonl"
    fault_events = []
    if fault_log.exists():
        for line in fault_log.read_text().splitlines():
            line = line.strip()
            if line:
                fault_events.append(json.loads(line))

    # Read timeline
    timeline = []
    tl = run_dir / "timeline.jsonl"
    if tl.exists():
        for line in tl.read_text().splitlines():
            line = line.strip()
            if line:
                timeline.append(json.loads(line))

    correlated = measurement.correlate(timeline, fault_events)
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e, default=str) for e in correlated) + "\n"
    )
    measurement.write_summary(
        correlated,
        run_dir / "summary.json",
        run_dir / "summary.md",
    )

    # Assertions (observability mode: only check basics + opt-in bounds)
    assert len(fault_events) == 5, (
        f"expected 5 fault events, proxy log has {len(fault_events)}"
    )
    for ev in correlated:
        assert ev["resume_seconds"] is not None, (
            f"event {ev['fault_index']} ({ev['fault_type']}) never resumed"
        )

    max_resume = os.environ.get("EXTREME_MAX_RESUME_SECONDS")
    if max_resume:
        for ev in correlated:
            assert ev["resume_seconds"] <= float(max_resume), (
                f"event {ev['fault_index']} resume {ev['resume_seconds']:.2f}s "
                f"> {max_resume}s"
            )
    max_freeze = os.environ.get("EXTREME_MAX_FREEZE_SECONDS")
    if max_freeze:
        for ev in correlated:
            assert ev["max_freeze_seconds"] <= float(max_freeze), (
                f"event {ev['fault_index']} freeze "
                f"{ev['max_freeze_seconds']:.2f}s > {max_freeze}s"
            )

    print(f"[extreme] Reports: {run_dir}")
