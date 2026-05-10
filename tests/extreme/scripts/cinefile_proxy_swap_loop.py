"""Force the addon's stream_proxy to swap from A to B mid-playback.

Kodi opens ONE proxy URL (the addon's /direct_play wraps primary +
fallbacks through stream_proxy). The runner schedules a
``connection_reset`` fault on the fault-proxy that sits between
stream_proxy and nzbdav-rs *for the primary URL only*. The fallback
URL bypasses fault-proxy and goes straight to nzbdav-rs. When the
fault hits, stream_proxy:

1. Sees the upstream connection break mid-Range.
2. HEADs each fallback to confirm it's streamable.
3. Runs the 100×4 KiB SHA256 fingerprint sweep across the file to
   verify byte-equivalence (so the swap is byte-safe).
4. Re-issues the in-flight Range against the fallback upstream and
   keeps shoving bytes back to Kodi over the same TCP connection.

Kodi never sees the upstream change. Player time keeps advancing
straight through the cutover — no Player.Stop, no rewind to t=0, no
visible buffer wait beyond the few hundred ms it takes to validate
the fallback.

Iteration N alternates which URL is primary and which is fallback so
the cutover is exercised in both directions. 20 iterations × 2 min.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Local sibling import — share the PROPFIND helper with the other
# runners so all three pick storages the same way.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _storage_discovery import discover_cinefile_storages  # noqa: E402

KODI_URL = os.environ.get("KODI_URL", "http://localhost:8082").rstrip("/")
KODI_AUTH = ("kodi", "kodi")
FAULT_PROXY_CONTROL = os.environ.get(
    "FAULT_PROXY_CONTROL", "http://localhost:8281"
).rstrip("/")
WEBDAV_USERNAME = os.environ["WEBDAV_USERNAME"]
WEBDAV_PASSWORD = os.environ["WEBDAV_PASSWORD"]
ITERATIONS = int(os.environ.get("CINEFILE_ITERATIONS", "20"))
INTERVAL_SECONDS = int(os.environ.get("CINEFILE_INTERVAL", "120"))
PLAY_BEFORE_FAULT_SECONDS = float(os.environ.get("CINEFILE_PLAY_SECONDS", "15"))
OBSERVE_SECONDS = int(os.environ.get("CINEFILE_OBSERVE_SECONDS", "60"))
OUT_DIR = Path(os.environ.get("CINEFILE_OUT_DIR", "/tmp/cinefile_proxy")).resolve()


# Each iteration assigns one path as primary-via-fault-proxy and the
# other as fallback-direct so the cutover happens. PATH_A / PATH_B
# come from runtime PROPFIND (see ``main()``) — they're not hardcoded
# any more so a re-seeded run with different UUIDs still works.
HOST_FAULTPROXY = os.environ.get("FAULT_PROXY_HOST", "nzbdav-extreme-fault-proxy:8280")
HOST_DIRECT = os.environ.get("NZBDAV_DIRECT_HOST", "nzbdav-extreme-nzbdav:8080")


def _kodi_rpc(method: str, params: dict | None = None, timeout: int = 10):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    ).encode("utf-8")
    req = urllib.request.Request(
        "{}/jsonrpc".format(KODI_URL),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    user, pw = KODI_AUTH
    auth = base64.b64encode("{}:{}".format(user, pw).encode()).decode()
    req.add_header("Authorization", "Basic " + auth)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
        return json.loads(r.read())


def url_with_auth(host: str, path: str) -> str:
    return "http://{}:{}@{}{}".format(
        urllib.parse.quote(WEBDAV_USERNAME, safe=""),
        urllib.parse.quote(WEBDAV_PASSWORD, safe=""),
        host,
        path,
    )


def redact_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.netloc:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = "{}:{}".format(host, parts.port)
    return urllib.parse.urlunsplit(
        (parts.scheme, host, parts.path, parts.query, parts.fragment)
    )


def schedule_fault(at_seconds: float, fault_type: str = "connection_reset"):
    body = json.dumps(
        {"events": [{"at_seconds": at_seconds, "fault_type": fault_type}]}
    ).encode("utf-8")
    req = urllib.request.Request(
        "{}/control/schedule".format(FAULT_PROXY_CONTROL),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # nosec B310
        return json.loads(r.read())


def clear_fault_schedule():
    body = json.dumps({"events": []}).encode("utf-8")
    req = urllib.request.Request(
        "{}/control/schedule".format(FAULT_PROXY_CONTROL),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:  # nosec B310
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def stop_player():
    try:
        for p in _kodi_rpc("Player.GetActivePlayers").get("result", []) or []:
            _kodi_rpc("Player.Stop", {"playerid": p.get("playerid", 1)})
    except Exception:  # noqa: BLE001
        pass


def trigger_direct_play(primary_url: str, fallback_urls: list[str]):
    """Tell Kodi to Player.Open the addon's /direct_play plugin URL.

    The addon's setResolvedUrl hands Kodi a stream_proxy URL — Kodi
    plays bytes from that proxy for the entire session, while the
    proxy swaps upstreams under the hood when the primary fails.
    """
    qs = urllib.parse.urlencode(
        {
            "primary_url": primary_url,
            "fallback_urls": json.dumps(fallback_urls),
        }
    )
    plugin_url = "plugin://plugin.video.nzbdav/direct_play?{}".format(qs)
    return _kodi_rpc("Player.Open", {"item": {"file": plugin_url}})


def player_status() -> dict:
    try:
        active = _kodi_rpc("Player.GetActivePlayers").get("result", []) or []
        if not active:
            return {"active": False}
        pid = active[0].get("playerid", 1)
        ptype = active[0].get("type", "")
        props = _kodi_rpc(
            "Player.GetProperties",
            {"playerid": pid, "properties": ["time", "speed"]},
        )
        return {
            "active": True,
            "playerid": pid,
            "type": ptype,
            "props": props.get("result", {}),
        }
    except Exception as exc:  # noqa: BLE001
        return {"active": False, "error": str(exc)[:120]}


def time_to_seconds(time_obj) -> float:
    if not isinstance(time_obj, dict):
        return 0.0
    return (
        time_obj.get("hours", 0) * 3600
        + time_obj.get("minutes", 0) * 60
        + time_obj.get("seconds", 0)
        + time_obj.get("milliseconds", 0) / 1000.0
    )


def run_iteration(iteration: int, path_a: str, path_b: str, log: Path) -> dict:
    primary_path = path_a if iteration % 2 == 0 else path_b
    fallback_path = path_b if iteration % 2 == 0 else path_a
    primary_url = url_with_auth(HOST_FAULTPROXY, primary_path)  # via fault-proxy
    fallback_url = url_with_auth(HOST_DIRECT, fallback_path)  # bypass fault-proxy
    summary = {
        "iteration": iteration,
        "started_at": time.time(),
        "primary_label": "A" if iteration % 2 == 0 else "B",
        "primary_url": redact_url(primary_url),
        "fallback_url": redact_url(fallback_url),
        "events": [],
    }

    def record(event_type: str, **kw):
        rec = {"t": time.time(), "type": event_type, **kw}
        summary["events"].append(rec)
        with log.open("a") as fh:
            fh.write(json.dumps({"iter": iteration, **rec}) + "\n")

    stop_player()
    clear_fault_schedule()
    record("schedule_fault", at_seconds=PLAY_BEFORE_FAULT_SECONDS)
    schedule_fault(PLAY_BEFORE_FAULT_SECONDS)
    record("trigger_direct_play")
    play_resp = trigger_direct_play(primary_url, [fallback_url])
    record("direct_play_resp", body=play_resp)

    deadline = time.time() + OBSERVE_SECONDS
    last_t_sec = -1.0
    last_progress_t = time.time()
    max_stall = 0.0
    crossed_fault = False
    progressed_after_fault = False
    fault_t_sec = None
    while time.time() < deadline:
        status = player_status()
        elapsed = time.time() - summary["started_at"]
        if status.get("active"):
            t_sec = time_to_seconds(status.get("props", {}).get("time", {}))
            if t_sec > last_t_sec + 0.05:
                stall = time.time() - last_progress_t
                if last_t_sec > 0 and stall > max_stall:
                    max_stall = stall
                last_progress_t = time.time()
                last_t_sec = t_sec
                if elapsed > PLAY_BEFORE_FAULT_SECONDS + 1:
                    progressed_after_fault = True
                    crossed_fault = True
            if elapsed >= PLAY_BEFORE_FAULT_SECONDS and fault_t_sec is None:
                fault_t_sec = t_sec
            record(
                "poll",
                elapsed=round(elapsed, 2),
                t_sec=t_sec,
                speed=status.get("props", {}).get("speed"),
                player_type=status.get("type", ""),
            )
        else:
            record("inactive", elapsed=round(elapsed, 2))
        time.sleep(0.5)

    summary["fault_t_sec"] = fault_t_sec
    summary["final_t_sec"] = round(last_t_sec, 2)
    summary["progressed_after_fault"] = progressed_after_fault
    summary["max_stall_seconds"] = round(max_stall, 2)
    summary["crossed_fault"] = crossed_fault
    summary["finished_at"] = time.time()
    return summary


def _discover_paths() -> tuple[str, str]:
    """Find two CiNEFiLE mkv paths to alternate as primary/fallback."""
    pairs = discover_cinefile_storages(limit=2)
    if len(pairs) < 2:
        raise SystemExit("FATAL: need 2 CiNEFiLE storages, got {}".format(len(pairs)))
    # _propfind_mkv_path returns an already-quoted href ("/content/...");
    # url_with_auth takes the path component verbatim, so just pass it
    # through.
    return pairs[0][1], pairs[1][1]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log = OUT_DIR / "proxy_swap.jsonl"
    if log.exists():
        log.unlink()
    path_a, path_b = _discover_paths()
    print("Primary path A: {}{}".format(HOST_FAULTPROXY, path_a))
    print("Primary path B: {}{}".format(HOST_FAULTPROXY, path_b))
    print(
        "Each iteration plays primary via fault-proxy, fault fires "
        "at +{}s, stream_proxy should swap to fallback (direct).".format(
            PLAY_BEFORE_FAULT_SECONDS
        )
    )
    summaries = []
    test_start = time.time()
    for i in range(ITERATIONS):
        target_start = test_start + i * INTERVAL_SECONDS
        wait = target_start - time.time()
        if wait > 0:
            print("[iter {}] sleeping {:.1f}s until next 2-min mark".format(i, wait))
            time.sleep(wait)
        print("[iter {}] start (+{:.0f}s)".format(i, time.time() - test_start))
        summary = run_iteration(i, path_a, path_b, log)
        summaries.append(summary)
        print(
            "[iter {}] primary={} fault_t_sec={} final_t_sec={} "
            "post_fault_progress={} max_stall={}s".format(
                i,
                summary["primary_label"],
                summary["fault_t_sec"],
                summary["final_t_sec"],
                summary["progressed_after_fault"],
                summary["max_stall_seconds"],
            )
        )
    summary_path = OUT_DIR / "proxy_swap_summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2, default=str))
    print("\nLog:     {}".format(log))
    print("Summary: {}".format(summary_path))


if __name__ == "__main__":
    main()
