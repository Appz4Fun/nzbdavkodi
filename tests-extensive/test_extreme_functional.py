"""End-to-end extreme functional test.

Run: `just extreme-functional-test`

Spec: docs/superpowers/specs/2026-05-09-extreme-functional-test-design.md
Plan: docs/superpowers/plans/2026-05-09-extreme-functional-test.md

This test depends on session-scoped fixtures in tests-extensive/extreme/conftest.py.

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

# wrong-import-order: the test_functional_fallback_playback import is deliberately
# late (after the os.environ setup it depends on), which pylint's global import-order
# model can't reconcile with the first-party tests.extreme_harness import above.
# pylint: disable=inconsistent-return-statements,no-name-in-module,wrong-import-order

from __future__ import annotations

import base64
import json
import os
import random
import subprocess
import time
import urllib.request

import pytest
from extreme._fixtures import (
    FAULT_PROXY_CONTROL_HOST_PORT,
    KODI_HOST_PORT,
)

from tests.extreme_harness import measurement

# The session-scoped harness fixtures (stack_ready, run_dir, env_loaded, …)
# live in extreme/_fixtures.py so they can be loaded here via pytest_plugins
# without triggering pytest's "Plugin already registered under a different
# name" error that arises when conftest.py is both registered via
# pytest_plugins AND discovered hierarchically by pytest's conftest scan.
# extreme/conftest.py is a thin wrapper that re-declares the same
# pytest_plugins entry; pytest deduplicates by module name, so the second
# declaration is a no-op and no ValueError occurs.
pytest_plugins = ["extreme._fixtures"]

pytestmark = pytest.mark.extreme

# Distinct movies to try when playback never starts (a picked release can
# be dead on the provider — missing articles — and the addon fails fast by
# design). Each attempt re-picks with the tried IMDb ids excluded.
_MAX_PLAYBACK_ATTEMPTS = 3

# Wider candidate pool than just functional-test-top-imdb. Setting these via
# os.environ affects the existing helpers in test_functional_fallback_playback.
os.environ.setdefault("LIVE_FALLBACK_POOL_LIMIT", "100")
# The actual knob consumed by _required_fallback_candidate_count() is
# FUNCTIONAL_MIN_FALLBACK_CANDIDATES, not LIVE_FALLBACK_REQUIRED_COUNT.
os.environ.setdefault("FUNCTIONAL_MIN_FALLBACK_CANDIDATES", "2")

# _live_env() (imported below) requires NZBDAV_URL, WEBDAV_URL, WEBDAV_API_KEY
# in addition to HYDRA_*/WEBDAV_USERNAME/WEBDAV_PASSWORD. NZBDAV_URL is the
# live LAN nzbdav from .env (required by env_loaded); the host-side helpers
# talk to it directly, while only the Kodi container's streaming path goes
# through the fault proxy. NZBDAV_API_KEY is the same secret as
# WEBDAV_API_KEY (both come from the .env's NZBDAV_API_KEY).
if os.environ.get("NZBDAV_URL"):
    os.environ.setdefault("WEBDAV_URL", os.environ["NZBDAV_URL"])
if "WEBDAV_API_KEY" not in os.environ and os.environ.get("NZBDAV_API_KEY"):
    os.environ["WEBDAV_API_KEY"] = os.environ["NZBDAV_API_KEY"]

from test_functional_fallback_playback import (  # noqa: E402
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
# Kills the currently-streaming file path permanently (404), forcing the
# addon to promote a prevalidated standby. Two per schedule exercises
# consecutive fallback cutovers (the pick guarantees >=2 standbys).
_SOURCE_DEAD_COUNT = 2
# source_dead slots must land after the prewarm burst has armed the
# standbys (playback + 120s, plus submit/prevalidate time).
_SOURCE_DEAD_MIN_AT = 240

EXTREME_FILTER_SETTINGS = {
    "filter_2160p": "false",
    "filter_1080p": "true",
    "filter_720p": "false",
    "filter_480p": "false",
    "filter_hdr10": "false",
    "filter_hdr10plus": "false",
    "filter_dolby_vision": "false",
    "filter_hlg": "false",
    "filter_sdr": "true",
    "filter_atmos": "false",
    "filter_truehd": "false",
    "filter_dtshd_ma": "true",
    "filter_dtsx": "false",
    "filter_ddplus": "true",
    "filter_dd": "true",
    "filter_aac": "true",
    "filter_hevc": "false",
    "filter_avc": "true",
    "filter_av1": "false",
    "filter_vp9": "false",
    "filter_mpeg2": "true",
    "filter_english": "true",
    "filter_require_keywords": "1080p,web",
    "filter_release_group": "",
    "filter_exclude_keywords": "",
    "filter_exclude_release_group": "",
    "filter_min_size": "0",
    "filter_max_size": "3000",
    "max_results": "250",
    "auto_select_best": "true",
    "fallback_streams_enabled": "true",
    "fallback_streams_max": "10",
}


def _seed() -> int:
    raw = os.environ.get("EXTREME_SEED")
    return int(raw) if raw else int(time.time())


def _extreme_addon_settings(settings):
    merged = dict(settings)
    merged.update(EXTREME_FILTER_SETTINGS)
    return merged


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


def _fired_fault_types(anchor_t_wall: float) -> list[str] | None:
    """List fault types the proxy has fired since ``anchor_t_wall``.

    Reads the proxy's events.jsonl live (docker exec) so the relaunch
    watchdog can re-arm the UNFIRED remainder of the schedule after a
    Kodi death swallowed fault slots. Returns None when the log cannot
    be read (container restarting) — callers must then skip re-arming
    rather than risk double-posting already-fired faults.
    """
    proc = subprocess.run(
        [
            "docker",
            "exec",
            "nzbdav-extreme-fault-proxy",
            "cat",
            "/var/log/fault-proxy/events.jsonl",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    fired = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("t_wall", 0) >= anchor_t_wall - 1:
            fired.append(str(event.get("fault_type", "")))
    return fired


def _unfired_schedule_events(schedule: list[dict], fired: list[str]) -> list[dict]:
    """Schedule entries with no fired counterpart, matched by type.

    Type-multiset matching (not count slicing): a parked http_500 can
    fire AFTER a later midstream event, so position alone would re-post
    an already-fired fault and overshoot the expected event count.
    """
    pending = list(fired)
    remaining = []
    for entry in schedule:
        if entry["fault_type"] in pending:
            pending.remove(entry["fault_type"])
        else:
            remaining.append(entry)
    return remaining


def _rearm_unfired_faults(
    schedule: list[dict],
    schedule_post_t_wall: float,
    rearms: int,
    window_end: float,
) -> tuple[int, float, float]:
    """Re-post unfired fault slots after a playback outage, if any.

    A Kodi death (exit-255 class) or an ineligible-traffic stretch can
    swallow scheduled fault slots — the run then fails the 5-fired-events
    assert even though playback recovered. Once the relaunched player has
    settled (or the posted schedule has gone overdue), re-post the
    UNFIRED remainder shifted to start 90s out (original spacing
    preserved) and stretch the window so every fault still fires and
    gets measured. Skipped when the events log is unreadable —
    double-posting a fired fault would break the count the other way.
    Returns (rearms, window_end, posted_final_at); ``posted_final_at``
    is the re-posted batch's last at_seconds (0.0 when nothing posted).
    """
    fired = _fired_fault_types(schedule_post_t_wall)
    remaining = _unfired_schedule_events(schedule, fired) if fired is not None else []
    if not remaining or rearms >= 3:
        return rearms, window_end, 0.0
    base = remaining[0]["at_seconds"]
    shifted = [
        {
            "at_seconds": 90.0 + (e["at_seconds"] - base),
            "fault_type": e["fault_type"],
        }
        for e in remaining
    ]
    try:
        _post_schedule(shifted)
    except Exception as exc:  # noqa: BLE001
        print(f"[extreme] re-arm failed: {exc}")
        return rearms, window_end, 0.0
    window_end = max(window_end, time.monotonic() + shifted[-1]["at_seconds"] + 240)
    print(
        "[extreme] re-armed {} unfired fault(s) "
        "(re-arm {} of 3, fired so far: {})".format(
            len(shifted), rearms + 1, len(fired)
        )
    )
    return rearms + 1, window_end, shifted[-1]["at_seconds"]


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
    """5 events: 3 recoverable faults + 2 source_dead cutover forcers.

    Times land in [60, 1020] with >=90s spacing (a source_dead cutover
    needs runway to complete before the next event). source_dead events
    only occupy slots past _SOURCE_DEAD_MIN_AT so the prewarm burst has
    already armed the standbys they force promotion onto.
    """
    while True:
        candidates = sorted(rng.sample(range(60, 1000), 5))
        if all(b - a >= 90 for a, b in zip(candidates, candidates[1:])):
            late_slots = [t for t in candidates if _SOURCE_DEAD_MIN_AT <= t <= 900]
            if len(late_slots) >= _SOURCE_DEAD_COUNT:
                break
    dead_slots = set(rng.sample(late_slots, _SOURCE_DEAD_COUNT))
    recoverable = rng.sample(FAULT_TYPES, 5 - _SOURCE_DEAD_COUNT)
    schedule = []
    for t in candidates:
        if t in dead_slots:
            fault_type = "source_dead"
        else:
            fault_type = recoverable.pop(0)
        schedule.append({"at_seconds": float(t), "fault_type": fault_type})
    return schedule


# Episode mode (the soak default): a series whose WEB-DL episodes are
# reposted byte-identically at different dates — every episode carries an
# exact-size mirror pair (proxy-validatable cutover standby) plus smaller
# distinct encodes (cross-release standbys), all under ~2.5 GB. Movie
# mode remains available via EXTREME_TEST_IMDB_ID.
_TV_SERIES = {
    "title": "The Good the Bad and the Ugly",
    "year": "2025",
    "tmdb_id": "285768",
    "season": 1,
    "episodes": list(range(1, 25)),
}


def _mirror_cluster_len(filtered):
    """Size of the largest byte-identical mirror cluster in a pool.

    Same-release reposts report NEAR-identical indexer sizes (the
    underlying mkv is byte-identical; posting overhead shifts the
    reported NZB size by a few KB), so cluster with a tolerance of
    max(1 MB, 0.1%) instead of exact equality — the addon's cutover
    validation probes the INGESTED file's length and digests, which
    are exact for true reposts. Only rows with distinct links count.
    """
    sized = sorted(
        (
            (int(r.get("size") or 0), r)
            for r in filtered
            if str(r.get("size", "")).isdigit()
        ),
        key=lambda x: x[0],
    )
    best = 0
    cluster_links = set()
    cluster_dates = set()
    prev_size = None
    for size, row in sized:
        tolerance = max(1_000_000, (prev_size or size) // 1000)
        if prev_size is None or size - prev_size > tolerance:
            cluster_links = set()
            cluster_dates = set()
        cluster_links.add(row.get("link"))
        cluster_dates.add(str(row.get("pubdate", "")).strip())
        prev_size = size
        if len(cluster_links) >= 2 and len(cluster_dates) >= 2:
            best = max(best, len(cluster_links))
    return best


def _pick_episode_with_mirror_pool(rng: random.Random, settings, exclude=frozenset()):
    """Pick the episode with the LARGEST byte-identical mirror cluster.

    Scans up to 8 seeded-random episodes and keeps the one whose
    filtered pool clusters the most distinct-link, distinct-pubdate
    reposts of one file (the proxy cutover's promotable standbys).
    Requires a cluster of >=2 plus >=3 filtered rows total so diverse
    cross-release standbys exist for the second source_dead. Returns
    (episode_number, filtered_rows).
    """
    from resources.lib.filter import filter_results, partition_series_rows
    from resources.lib.hydra import search_hydra

    def getter(k, d=""):
        return str(settings.get(k, d))

    episodes = [e for e in _TV_SERIES["episodes"] if e not in exclude]
    last_error = "no episodes left"
    # Indexers behind Hydra bounce transiently (rate-limit cooldowns,
    # brief outages) and Hydra re-queries ALL of them on every search —
    # so a thin scan is retried after a pause rather than failing the
    # run on one bad window.
    for scan_pass in range(1, 4):
        rng.shuffle(episodes)
        best = None  # (cluster_len, pool_len, ep, filtered)
        for ep in episodes[:8]:
            try:
                results, error = search_hydra(
                    "episode",
                    _TV_SERIES["title"],
                    season=str(_TV_SERIES["season"]),
                    episode=str(ep),
                    settings_getter=getter,
                )
                if error or not results:
                    last_error = f"E{ep:02d}: search failed: {error}"
                    continue
                filtered, _all = filter_results(results, settings_getter=getter)
                # Keep ONLY rows parsed as the requested series: an
                # episode search matches the phrase anywhere in the
                # release name, and The Rookie has an EPISODE titled
                # exactly like this series — its huge repost cluster
                # otherwise wins the largest-cluster pick and the run
                # plays the wrong show (run 2026-07-18T20-08-47Z).
                filtered, wrong_show = partition_series_rows(
                    filtered, _TV_SERIES["title"]
                )
                cluster = _mirror_cluster_len(filtered)
                pool = len(filtered)
                if cluster < 2 or pool < 3:
                    last_error = (
                        f"E{ep:02d}: filtered={pool} cluster={cluster} "
                        f"(dropped {len(wrong_show)} wrong-show)"
                    )
                    continue
                print(f"[extreme] E{ep:02d}: pool={pool} mirror_cluster={cluster}")
                if best is None or (cluster, pool) > (best[0], best[1]):
                    best = (cluster, pool, ep, filtered)
            except Exception as exc:  # noqa: BLE001
                last_error = f"E{ep:02d}: {exc}"
                continue
        if best is not None:
            return best[2], best[3]
        if scan_pass < 3:
            print(
                f"[extreme] scan pass {scan_pass}: no mirror pool "
                f"({last_error}); retrying in 90s"
            )
            time.sleep(90)
    pytest.fail(f"no episode with a mirror pool: {last_error}")


def _pick_movie_with_fallback_pool(rng: random.Random, settings, exclude=frozenset()):
    """Try up to 3 random movies; return (movie, primary_pair, fallback_pairs).

    ``exclude`` drops already-attempted IMDb ids so a playback retry (dead
    release on the provider) re-picks a different movie.

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
        pool_movies = [
            m for m in IMDB_TOP_50_MOVIES if m.get("imdb") not in (exclude or ())
        ]
        rng.shuffle(pool_movies)
    last_error = None
    # Mirror the regular functional test's selection: try a FraMeSToR-tagged
    # query first (BluRay rips by FraMeSToR have proper .mkv files that
    # nzbdav-rs's deobfuscator handles cleanly), then fall back to the
    # most-duplicated release group. The original extreme-test pool used
    # only the latter, which often picked WEB-DL rips that nzbdav-rs
    # rejects with "no importable video file found".
    # require_filtered keeps the candidate pool aligned with the addon's own
    # play-time filter: without it a movie whose releases all fail the
    # addon filter (run 2026-07-18T04-05-30Z: Saving Private Ryan, addon
    # filtered=0/250) builds its pool from unfiltered results, the addon
    # plays an unfiltered heavy pick, and the run has no fallback pool to
    # test. Try more movies since the strict check rejects more of them.
    for movie in pool_movies[:10]:
        try:
            _profile, pairs = _movie_selections_with_fallbacks(
                settings, movie, require_filtered=True
            )
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

    settings = _extreme_addon_settings(_addon_settings(_live_env()))
    schedule = _generate_fault_schedule(rng)

    def _capture_diagnostics():
        """Pull Kodi + addon logs out of the container before teardown.

        Called on any orchestrator failure so we can debug why playback
        didn't start (crash, hang, timeout, dialog lockup) after the
        compose_up finalizer wipes the volumes.
        """
        # Kernel ring buffer first: OOM kills leave their only trace here
        # (SIGKILL bypasses gdb; a cgroup group-kill takes gdb down too).
        subprocess.run(
            [
                "docker",
                "exec",
                "nzbdav-extreme-kodi",
                "sh",
                "-c",
                (
                    "dmesg 2>/dev/null | tail -200 "
                    "> /var/log/supervisor/dmesg.log || true"
                ),
            ],
            check=False,
        )
        for src, dst_name in [
            ("/root/.kodi/temp/kodi.log", "kodi.log"),
            ("/root/.kodi/temp/kodi.old.log", "kodi.old.log"),
            ("/var/log/supervisor/kodi.err.log", "kodi.err.log"),
            ("/var/log/supervisor/supervisord.log", "supervisord.log"),
            ("/var/log/supervisor/memwatch.log", "memwatch.log"),
            ("/var/log/supervisor/dmesg.log", "dmesg.log"),
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

    def _clear_kodi_dialogs():
        """Cancel a still-running resolve and dismiss leftover modals.

        A timed-out attempt's resolve keeps polling addon-side (its own
        timeout is 3600s); launching the next attempt underneath it means
        neither ever reaches the player. Input.Back cancels the addon's
        DialogProgress (the resolver aborts and cancels its nzbdav job),
        then Input.Select confirms any 'Download failed' OK modal, and a
        settle wait lets the cancel path unwind before the relaunch.
        """
        for _ in range(3):
            try:
                _kodi_rpc("Input.Back")
            except Exception:  # noqa: BLE001,S110
                pass  # best-effort input; Kodi may be mid-restart
            time.sleep(1.0)
        for _ in range(2):
            try:
                _kodi_rpc("Input.Select")
            except Exception:  # noqa: BLE001,S110
                pass  # best-effort input; Kodi may be mid-restart
            time.sleep(0.5)
        try:
            _kodi_rpc("Input.Home")
        except Exception:  # noqa: BLE001,S110
            pass  # best-effort input; Kodi may be mid-restart
        time.sleep(10)

    # Launch playback via TMDBHelper, retrying with a DIFFERENT target when
    # playback never starts: a release can be dead on the provider (missing
    # articles) and the addon fails fast by design, so one dead pick must
    # not fail the whole 20-minute run. Episode mode (default) rotates
    # mirror-rich episodes of _TV_SERIES; EXTREME_TEST_IMDB_ID pins a
    # movie and restores the movie flow.
    movie_pin = os.environ.get("EXTREME_TEST_IMDB_ID", "").strip()
    episode_mode = not movie_pin

    def _pick_target(exclude):
        if episode_mode:
            ep, pool = _pick_episode_with_mirror_pool(rng, settings, exclude=exclude)
            return ep, f"{_TV_SERIES['title']} S01E{ep:02d}", pool
        m, primary_pair, fb = _pick_movie_with_fallback_pool(
            rng, settings, exclude=exclude
        )
        return m["imdb"], m["title"], (m, primary_pair, fb)

    def _launch_params(target):
        if episode_mode:
            return {
                "info": "play",
                "tmdb_type": "tv",
                "type": "episode",
                "tmdb_id": _TV_SERIES["tmdb_id"],
                "season": str(_TV_SERIES["season"]),
                "episode": str(target),
            }
        return {"info": "play", "type": "movie", "imdb_id": target}

    tried_targets = set()
    target = None
    target_label = ""
    target_pool = None
    pid = None
    schedule_post_t_wall = time.time()
    for attempt in range(1, _MAX_PLAYBACK_ATTEMPTS + 1):
        target, target_label, target_pool = _pick_target(frozenset(tried_targets))
        tried_targets.add(target)
        rpc_resp = _kodi_rpc(
            "Addons.ExecuteAddon",
            {
                "addonid": "plugin.video.themoviedb.helper",
                "params": _launch_params(target),
            },
        )
        print(
            "[extreme] attempt {}/{}: TMDBHelper playback launch "
            "({}) response: {}".format(
                attempt,
                _MAX_PLAYBACK_ATTEMPTS,
                target_label,
                rpc_resp,
            )
        )
        _dismiss_tmdbhelper_player_choosers()
        try:
            # nzbdav's resolver polls until the NZB download completes
            # before invoking the player; for a multi-GB release over NNTP
            # that wait can be several minutes, so 600s.
            pid = _wait_for_player(timeout=600)
        except Exception:  # noqa: BLE001
            # Connection reset / refused while polling = Kodi crashed.
            # Snapshot diagnostics so the cause survives teardown.
            try:
                _capture_diagnostics()
            except Exception as exc:  # noqa: BLE001
                print(f"[extreme] diagnostic capture failed: {exc}")
            raise
        if pid is not None:
            break
        print(
            "[extreme] attempt {}/{}: playback never started for '{}' "
            "(dead release?); retrying with a different target".format(
                attempt, _MAX_PLAYBACK_ATTEMPTS, target_label
            )
        )
        _clear_kodi_dialogs()

    if pid is not None:
        # Post the schedule only now that playback is live: the proxy run
        # clock resets at post time, so at_seconds counts seconds OF
        # PLAYBACK — a slow resolve can no longer let pre-playback probes
        # consume early faults (and source_dead slots stay anchored after
        # the prewarm burst as intended).
        schedule_post_t_wall = time.time()
        _post_schedule(schedule)

    if pid is None:
        # Save what we have and bail.
        measurement.write_manifest(
            run_dir / "manifest.json",
            {
                "seed": seed_value,
                "playback_started": False,
                "attempted_targets": sorted(str(t) for t in tried_targets),
            },
        )
        try:
            _capture_diagnostics()
        except Exception as exc:  # noqa: BLE001
            print(f"[extreme] diagnostic capture failed: {exc}")
        pytest.fail(
            "playback never started after {} attempts".format(_MAX_PLAYBACK_ATTEMPTS)
        )

    if episode_mode:
        target_info = {
            "series": _TV_SERIES["title"],
            "tmdb_id": _TV_SERIES["tmdb_id"],
            "season": _TV_SERIES["season"],
            "episode": target,
        }
        pool_rows = target_pool or []
        primary_title = pool_rows[0].get("title") if pool_rows else None
        fallback_count = max(0, len(pool_rows) - 1)
    else:
        m, primary_pair, fb = target_pool
        target_info = {"title": m["title"], "year": m["year"], "imdb": m["imdb"]}
        primary_title = primary_pair[0].get("title") if primary_pair else None
        fallback_count = len(fb)
    measurement.write_manifest(
        run_dir / "manifest.json",
        {
            "seed": seed_value,
            "target": target_info,
            "movie": target_info,  # legacy report tooling reads .movie
            "primary_nzb": primary_title,
            "fallback_count": fallback_count,
            "schedule": schedule,
            "playback_attempts": len(tried_targets),
            "started_at_wall": time.time(),
        },
    )

    poller = measurement.PlayerPoller(
        url=f"http://localhost:{KODI_HOST_PORT}/jsonrpc",
        auth=("kodi", "kodi"),
        interval=0.25,
        output_path=run_dir / "timeline.jsonl",
    )
    poller.start()

    # 20-minute measured window with a relaunch watchdog: this container's
    # Kodi sporadically dies (exit 255, no signal, no OOM — a rig-specific
    # kill with no obtainable trace) during retry/re-resolve GUI
    # transitions after hard faults. supervisord respawns it in ~2s; when
    # the player has been gone >20s we relaunch playback via TMDBHelper
    # (history adoption makes the re-resolve fast) so the interruption is
    # MEASURED as a long freeze instead of starving the remaining faults.
    # Production CoreELEC does not exhibit the 255 death; its numbers
    # would only be better.
    window_end = time.monotonic() + 1200
    player_gone_since = None
    relaunches = 0
    rearms = 0
    rearm_check_after = None
    # Overdue watch: faults can also starve WITHOUT a relaunch trigger
    # (an ineligible-traffic stretch, or a re-armed batch that only
    # partially fired — http_500 needs a fresh request start). Track when
    # the most recently POSTED schedule should have fully fired; once
    # it is 90s overdue, probe and re-arm even though no relaunch
    # happened. Updated on every successful re-arm.
    sched_final_at = schedule[-1]["at_seconds"]
    sched_posted_mono = time.monotonic()
    try:
        while time.monotonic() < window_end:
            time.sleep(5)
            try:
                active = _kodi_rpc("Player.GetActivePlayers").get("result", [])
            except Exception:  # noqa: BLE001
                active = None  # Kodi down/restarting
            if active:
                player_gone_since = None
                overdue = time.monotonic() - sched_posted_mono > sched_final_at + 90
                if overdue or (
                    rearm_check_after is not None
                    and time.monotonic() >= rearm_check_after
                ):
                    rearm_check_after = None
                    prior_rearms = rearms
                    rearms, window_end, posted_final = _rearm_unfired_faults(
                        schedule, schedule_post_t_wall, rearms, window_end
                    )
                    if rearms != prior_rearms:
                        sched_final_at = posted_final
                        sched_posted_mono = time.monotonic()
                    elif overdue:
                        # Nothing unfired (or log unreadable): push the
                        # overdue horizon out so the probe doesn't run
                        # on every 5s tick.
                        sched_posted_mono = time.monotonic() - sched_final_at
                continue
            now = time.monotonic()
            if player_gone_since is None:
                player_gone_since = now
                continue
            # 8 lives: the exit-255 death can strike several times per
            # run; at cap the player stays gone, and the overdue re-arm
            # (which needs an ACTIVE player) can never recover the
            # remaining fault slots (attempt-6 run-1 burned all 4 and
            # finished 4/5).
            if now - player_gone_since < 20 or relaunches >= 8:
                continue
            relaunches += 1
            # After two failed same-movie relaunches, fail over to a
            # DIFFERENT movie: a source_dead may have killed a primary
            # whose only standby is a poisoned release (a mislabeled NZB
            # carrying different content — seen live: a 'Modern Times'
            # post whose storage held Monsters University), which no
            # amount of relaunching can fix. A fresh movie restores
            # eligible traffic so the remaining faults still fire.
            relaunch_target = target
            relaunch_label = target_label
            if relaunches > 2:
                try:
                    relaunch_target, relaunch_label, _pool = _pick_target(
                        frozenset(tried_targets)
                    )
                    tried_targets.add(relaunch_target)
                except Exception as exc:  # noqa: BLE001
                    print(f"[extreme] failover pick failed: {exc}")
            print(
                "[extreme] player gone {:.0f}s — relaunching playback "
                "({} of 8, target: {})".format(
                    now - player_gone_since, relaunches, relaunch_label
                )
            )
            _clear_kodi_dialogs()
            try:
                _kodi_rpc(
                    "Addons.ExecuteAddon",
                    {
                        "addonid": "plugin.video.themoviedb.helper",
                        "params": _launch_params(relaunch_target),
                    },
                )
                _dismiss_tmdbhelper_player_choosers()
            except Exception as exc:  # noqa: BLE001
                print(f"[extreme] relaunch failed: {exc}")
            player_gone_since = None
            # Once the relaunched player has been back and stable for a
            # bit, check whether the outage swallowed fault slots and
            # re-arm them (see the re-arm block above).
            rearm_check_after = time.monotonic() + 45
    finally:
        poller.stop()
        poller.join(timeout=5)
        try:
            _kodi_rpc("Player.Stop", {"playerid": pid})
        except Exception:
            pass

    # Pull container logs into the run dir
    for container, name in [
        # NOT "kodi.log": _capture_diagnostics writes the in-container
        # application log under that name and would clobber this one.
        ("nzbdav-extreme-kodi", "kodi-container-stdout.log"),
        ("nzbdav-extreme-fault-proxy", "fault-proxy-container.log"),
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

    # Always snapshot the full diagnostics set (kodi.old.log, supervisord
    # log, gdb backtraces, addon data) before any assertion can raise:
    # two mid-run Kodi crashes went unexplained because the gdb log was
    # only captured on the playback-start failure paths, and compose_up's
    # teardown wiped the containers after the measurement asserts failed.
    try:
        _capture_diagnostics()
    except Exception as exc:  # noqa: BLE001
        print(f"[extreme] diagnostic capture failed: {exc}")

    # Read fault-proxy events.jsonl from the bind-mounted reports dir.
    # The file appends across playback attempts while replace_schedule only
    # resets in-memory state, so drop any events fired before the FINAL
    # attempt's schedule post — a doomed first attempt must not inflate the
    # expected-5-events count.
    fault_log = run_dir / "fault-proxy" / "events.jsonl"
    fault_events = []
    if fault_log.exists():
        for line in fault_log.read_text().splitlines():
            line = line.strip()
            if line:
                event = json.loads(line)
                if event.get("t_wall", 0) >= schedule_post_t_wall - 1:
                    fault_events.append(event)

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
    assert len(correlated) == len(
        fault_events
    ), "expected {} correlated events, got {}".format(
        len(fault_events), len(correlated)
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
