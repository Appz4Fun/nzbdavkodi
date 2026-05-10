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

DEVIATIONS from the spec/plan:

- Spec line 230 says "XBMC.RunScript(plugin.video.themoviedb.helper, mode=play,
  type=movie, tmdb_id=...)".
  Implementation uses JSON-RPC `Addons.ExecuteAddon` with `info=play` (TMDBHelper's
  actual routing param). XBMC.RunScript is a Kodi builtin, not a JSON-RPC method
  in Kodi 21 — Addons.ExecuteAddon with the addon's plugin params is the correct
  JSON-RPC invocation.

- Spec/plan implies `tmdb_id` parameter; we use `imdb_id` because IMDB_TOP_50_MOVIES
  has imdb tt-IDs (no tmdb_ids). TMDBHelper accepts both.

- _most_duplicated_group_pool returns a 2-tuple (group_str, pool_list) — plan
  treated it as a flat list. Adjusted unpacking accordingly.

- Plan's LIVE_FALLBACK_REQUIRED_COUNT env var doesn't exist in the helpers; the
  actual knob is FUNCTIONAL_MIN_FALLBACK_CANDIDATES.
"""

# pylint: disable=inconsistent-return-statements,no-name-in-module

from __future__ import annotations

import base64
import json
import os
import random
import subprocess
import time
import urllib.request

import pytest

from tests.extreme import measurement
from tests.extreme.conftest import (
    FAULT_PROXY_CONTROL_HOST_PORT,
    KODI_HOST_PORT,
    NZBDAV_HOST_PORT,
)

# tests/extreme/conftest.py is a sibling of this file, so its fixtures
# (stack_ready, run_dir, env_loaded, etc.) are not visible via pytest's
# hierarchical conftest discovery. Register it as a plugin so they are.
pytest_plugins = ["tests.extreme.conftest"]

pytestmark = pytest.mark.extreme

# Wider candidate pool than just functional-test-top-imdb. Setting these via
# os.environ affects the existing helpers in test_functional_fallback_playback.
os.environ.setdefault("LIVE_FALLBACK_POOL_LIMIT", "100")
# The actual knob consumed by _required_fallback_candidate_count() is
# FUNCTIONAL_MIN_FALLBACK_CANDIDATES, not LIVE_FALLBACK_REQUIRED_COUNT.
os.environ.setdefault("FUNCTIONAL_MIN_FALLBACK_CANDIDATES", "2")

# _live_env() (imported below) requires NZBDAV_URL, WEBDAV_URL, WEBDAV_API_KEY
# in addition to HYDRA_*/WEBDAV_USERNAME/WEBDAV_PASSWORD. The extreme test
# brings up its own nzbdav-rs in docker-compose with a fixed host port, so we
# point the live-env helpers at that host-mapped port instead of asking the
# user to put it in .env. NZBDAV_API_KEY is the same secret as WEBDAV_API_KEY
# in this stack (both come from the .env's NZBDAV_API_KEY).
_NZBDAV_HOST_URL = f"http://localhost:{NZBDAV_HOST_PORT}"
os.environ.setdefault("NZBDAV_URL", _NZBDAV_HOST_URL)
os.environ.setdefault("WEBDAV_URL", _NZBDAV_HOST_URL)
if "WEBDAV_API_KEY" not in os.environ and os.environ.get("NZBDAV_API_KEY"):
    os.environ["WEBDAV_API_KEY"] = os.environ["NZBDAV_API_KEY"]

from tests.test_functional_fallback_playback import (  # noqa: E402
    IMDB_TOP_50_MOVIES,
    _addon_settings,
    _live_env,
    _movie_selections_with_fallbacks,
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
        {"at_seconds": float(t), "fault_type": ft} for t, ft in zip(candidates, types)
    ]


def _pick_movie_with_fallback_pool(rng: random.Random, settings):
    """Try up to 3 random movies; return (movie, primary_pair, fallback_pairs).

    _most_duplicated_group_pool returns (group_str, pool_list); we unpack it
    and check the pool list length, not the 2-tuple length.

    Honour ``EXTREME_TEST_IMDB_ID`` to pin the candidate to a specific title
    when set — otherwise we get a random movie via the seeded RNG and any
    nzbdav-rs release-pattern issues (e.g. the "no importable video file
    found" rejection on certain BluRay rips) make the test flaky.
    """
    pinned_imdb = os.environ.get("EXTREME_TEST_IMDB_ID", "").strip()
    if pinned_imdb:
        pool_movies = [m for m in IMDB_TOP_50_MOVIES if m.get("imdb") == pinned_imdb]
        if not pool_movies:
            pytest.fail(f"EXTREME_TEST_IMDB_ID={pinned_imdb} not in IMDB_TOP_50_MOVIES")
    else:
        pool_movies = list(IMDB_TOP_50_MOVIES)
        rng.shuffle(pool_movies)
    last_error = None
    # Mirror the regular functional test's selection: try a FraMeSToR-tagged
    # query first (BluRay rips by FraMeSToR have proper .mkv files that
    # nzbdav-rs's deobfuscator handles cleanly), then fall back to the
    # most-duplicated release group. The original extreme-test pool used
    # only the latter, which often picked WEB-DL rips that nzbdav-rs
    # rejects with "no importable video file found".
    for movie in pool_movies[:3]:
        try:
            _profile, pairs = _movie_selections_with_fallbacks(settings, movie)
            if not pairs:
                last_error = f"no selection pairs for {movie['title']}"
                continue
            primary, fallbacks = pairs[0], pairs[1:]
            return movie, primary, fallbacks
        except Exception as exc:
            last_error = f"{movie['title']}: {exc}"
            continue
    pytest.fail(f"could not find a movie with a fallback pool: {last_error}")


def _wait_for_dialog_select(timeout=30):
    """Poll currentwindow until DialogSelect (id 12000) is up, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = _kodi_rpc("GUI.GetProperties", {"properties": ["currentwindow"]})
            window = resp.get("result", {}).get("currentwindow", {})
            if int(window.get("id", -1)) == 12000:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def _dismiss_tmdbhelper_player_choosers():
    """Drive TMDBHelper's two-stage DialogSelect picker by polling the
    actual current Kodi window before each ``Input.Select``. The earlier
    fixed-timing implementation sent the two Selects on a sleep(4),
    sleep(2) cadence; under bridge networking + software GL the first
    chooser doesn't appear until ~5-10s after ExecuteAddon, so the
    Selects fired into thin air and TMDBHelper's own ~10-minute
    wait-for-user timeout exhausted the test's ``_wait_for_player``
    window. Polling for window id 12000 (DialogSelect) keeps the
    Selects on-target without the timing race.
    """
    if _wait_for_dialog_select(timeout=30):
        try:
            _kodi_rpc("Input.Select")  # pick NZB-DAV from the player list
        except Exception:  # noqa: BLE001
            pass
        # The second chooser ("Play with NZB-DAV" / Cancel) replaces the
        # first so window id stays 12000 — give it a beat to actually
        # transition before re-polling.
        time.sleep(0.8)
        if _wait_for_dialog_select(timeout=10):
            try:
                _kodi_rpc("Input.Select")  # confirm "Play with NZB-DAV"
            except Exception:  # noqa: BLE001
                pass


def _wait_for_player(timeout=30):
    """Poll Player.GetActivePlayers passively. Caller is responsible for
    dismissing TMDBHelper's player choosers via
    _dismiss_tmdbhelper_player_choosers before calling this; sending
    Input.Select inside the poll loop will Cancel nzbdav's
    DialogProgress and abort resolve.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = _kodi_rpc("Player.GetActivePlayers")
        if resp.get("result"):
            return resp["result"][0]["playerid"]
        time.sleep(1)
    return None


def test_extreme_fallback_run(stack_ready, run_dir):
    seed_value = _seed()
    rng = random.Random(seed_value)

    # Pre-flight health checks
    hydra = os.environ["HYDRA_URL"].rstrip("/")
    api_key = os.environ["HYDRA_API_KEY"]
    with urllib.request.urlopen(
        f"{hydra}/api?apikey={api_key}&t=caps",
        timeout=10,
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
            "seed": seed_value,
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
    # TMDBHelper accepts imdb_id; use it so the test actually exercises the
    # TMDBHelper -> player JSON -> nzbdav resolver path (per spec). The
    # IMDB_TOP_50_MOVIES list pinned in test_functional_fallback_playback.py
    # carries imdb tt-IDs, not tmdb_ids, so we route via imdb_id.
    imdb_id = movie["imdb"]  # tt-format id like "tt0111161"
    rpc_resp = _kodi_rpc(
        "Addons.ExecuteAddon",
        {
            "addonid": "plugin.video.themoviedb.helper",
            "params": {
                "info": "play",
                "type": "movie",
                "imdb_id": imdb_id,
            },
        },
    )
    print(
        f"[extreme] TMDBHelper playback launch (imdb_id={imdb_id}) response: {rpc_resp}"
    )

    _dismiss_tmdbhelper_player_choosers()

    def _capture_diagnostics():
        """Pull Kodi + addon logs out of the container before teardown.

        Called on any orchestrator failure so we can debug why playback
        didn't start (crash, hang, timeout, dialog lockup) after the
        compose_up finalizer wipes the volumes.
        """
        for src, dst_name in [
            ("/root/.kodi/temp/kodi.log", "kodi.log"),
            ("/root/.kodi/temp/kodi.old.log", "kodi.old.log"),
            ("/var/log/supervisor/kodi.err.log", "kodi.err.log"),
            ("/var/log/supervisor/supervisord.log", "supervisord.log"),
            ("/var/log/kodi-gdb.log", "kodi-gdb.log"),
            ("/tmp/nzbdav-faulthandler.log", "nzbdav-faulthandler.log"),
            (
                "/tmp/nzbdav-faulthandler-service.log",
                "nzbdav-faulthandler-service.log",
            ),
            (
                "/root/.kodi/temp/nzbdav-script-play-stage.log",
                "nzbdav-script-play-stage.log",
            ),
        ]:
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"nzbdav-extreme-kodi:{src}",
                    str(run_dir / dst_name),
                ],
                check=False,
            )
        for src, dst_name in [
            (
                "/root/.kodi/userdata/addon_data/plugin.video.themoviedb.helper",
                "tmdbhelper_addon_data",
            ),
            (
                "/root/.kodi/userdata/addon_data/plugin.video.nzbdav",
                "nzbdav_addon_data",
            ),
        ]:
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"nzbdav-extreme-kodi:{src}",
                    str(run_dir / dst_name),
                ],
                check=False,
            )

    try:
        # nzbdav's resolver polls until the NZB download completes before
        # invoking the player (see plugin.video.nzbdav/resources/lib/
        # resolver.py:_poll_until_ready). For a 1080p release that's
        # multiple GB over NNTP the wait can be several minutes; 60s is
        # too tight. The 20-min test body tolerates most of that wait
        # since faults start at t=60s into playback, not into the test.
        pid = _wait_for_player(timeout=600)
    except Exception:  # noqa: BLE001
        # Connection reset / refused while polling = Kodi crashed mid-test.
        # Snapshot diagnostics first so the cause is reachable post-teardown.
        try:
            _capture_diagnostics()
        except Exception as exc:  # noqa: BLE001
            print(f"[extreme] diagnostic capture failed: {exc}")
        raise

    if pid is None:
        # Save what we have and bail.
        measurement.write_manifest(
            run_dir / "manifest.json",
            {"seed": seed_value, "playback_started": False},
        )
        try:
            _capture_diagnostics()
        except Exception as exc:  # noqa: BLE001
            print(f"[extreme] diagnostic capture failed: {exc}")
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
    assert (
        len(fault_events) == 5
    ), f"expected 5 fault events, proxy log has {len(fault_events)}"
    assert len(correlated) == len(fault_events), (
        "expected {} correlated events, got {}".format(
            len(fault_events), len(correlated)
        )
    )
    for ev in correlated:
        assert (
            ev["resume_seconds"] is not None
        ), f"event {ev['fault_index']} ({ev['fault_type']}) never resumed"

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
