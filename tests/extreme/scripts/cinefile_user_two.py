"""Alternate between two user-supplied 12 Angry Men streams.

The user picked the two URLs directly. Skip the streamability probe
and just play A → wait 60s → play B → wait 60s, looped 20 times every
2 minutes.

Auth and the inter-container hostname are added automatically since
the user-provided URLs are written for the host (localhost:8180) but
Kodi runs inside the kodi container and reaches nzbdav-rs as
``nzbdav-extreme-nzbdav:8080``.
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

# Local sibling import — same PROPFIND helper the other runners use.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _storage_discovery import discover_cinefile_storages  # noqa: E402

KODI_URL = os.environ.get("KODI_URL", "http://localhost:8082").rstrip("/")
KODI_AUTH = ("kodi", "kodi")
NZBDAV_INTERNAL_HOST = os.environ.get(
    "NZBDAV_INTERNAL_HOST", "nzbdav-extreme-nzbdav:8080"
)
WEBDAV_USERNAME = os.environ["WEBDAV_USERNAME"]
WEBDAV_PASSWORD = os.environ["WEBDAV_PASSWORD"]
ITERATIONS = int(os.environ.get("CINEFILE_ITERATIONS", "20"))
INTERVAL_SECONDS = int(os.environ.get("CINEFILE_INTERVAL", "120"))
PER_STREAM_PLAY_SECONDS = int(os.environ.get("CINEFILE_PLAY_SECONDS", "60"))
OUT_DIR = Path(os.environ.get("CINEFILE_OUT_DIR", "/tmp/cinefile_user_two")).resolve()


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


def _build_kodi_url(mkv_path: str) -> str:
    """Compose a Kodi-reachable URL from a PROPFIND-derived mkv href.

    ``mkv_path`` is already URL-quoted (e.g. ``/dav/content/...mkv``)
    so we pass it straight into urlunsplit's path slot.
    """
    netloc = "{}:{}@{}".format(
        urllib.parse.quote(WEBDAV_USERNAME, safe=""),
        urllib.parse.quote(WEBDAV_PASSWORD, safe=""),
        NZBDAV_INTERNAL_HOST,
    )
    return urllib.parse.urlunsplit(("http", netloc, mkv_path, "", ""))


def player_status() -> dict:
    try:
        active = _kodi_rpc("Player.GetActivePlayers").get("result", []) or []
        if not active:
            return {"active": False}
        pid = active[0].get("playerid", 1)
        ptype = active[0].get("type", "")
        props = _kodi_rpc(
            "Player.GetProperties",
            {"playerid": pid, "properties": ["time", "speed", "percentage"]},
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


def stop_player():
    try:
        for p in _kodi_rpc("Player.GetActivePlayers").get("result", []) or []:
            _kodi_rpc("Player.Stop", {"playerid": p.get("playerid", 1)})
    except Exception:  # noqa: BLE001
        pass


def play(url: str):
    return _kodi_rpc("Player.Open", {"item": {"file": url}})


def play_window(url: str, label: str, log: Path, iteration: int) -> dict:
    play_resp = play(url)
    started_at = time.time()
    deadline = started_at + PER_STREAM_PLAY_SECONDS
    last_t_sec = -1.0
    last_progress_t = started_at
    max_stall = 0.0
    progressed = False
    player_type = ""
    with log.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "iter": iteration,
                    "label": label,
                    "type": "play",
                    "url": url[:200],
                    "play_resp": play_resp,
                    "t": started_at,
                }
            )
            + "\n"
        )
    while time.time() < deadline:
        status = player_status()
        if status.get("active"):
            player_type = status.get("type", player_type)
            t_sec = time_to_seconds(status.get("props", {}).get("time", {}))
            if t_sec > last_t_sec + 0.05:
                stall = time.time() - last_progress_t
                if last_t_sec > 0 and stall > max_stall:
                    max_stall = stall
                last_progress_t = time.time()
                last_t_sec = t_sec
                if last_t_sec > 0.5:
                    progressed = True
        time.sleep(0.5)
    return {
        "label": label,
        "url": url[:200],
        "started_at": started_at,
        "duration": round(time.time() - started_at, 2),
        "final_t_sec": round(last_t_sec, 2),
        "max_stall_seconds": round(max_stall, 2),
        "progressed": progressed,
        "player_type": player_type,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log = OUT_DIR / "user_two.jsonl"
    if log.exists():
        log.unlink()
    pairs = discover_cinefile_storages(limit=2)
    if len(pairs) < 2:
        raise SystemExit(
            "FATAL: need 2 CiNEFiLE storages, got {}".format(len(pairs))
        )
    a = _build_kodi_url(pairs[0][1])
    b = _build_kodi_url(pairs[1][1])
    print("A: {}".format(a))
    print("B: {}".format(b))
    summaries = []
    test_start = time.time()
    for i in range(ITERATIONS):
        target_start = test_start + i * INTERVAL_SECONDS
        wait = target_start - time.time()
        if wait > 0:
            print(
                "[iter {}] sleeping {:.1f}s until next 2-min mark".format(i, wait)
            )
            time.sleep(wait)
        print("[iter {}] start (+{:.0f}s)".format(i, time.time() - test_start))
        stop_player()
        a_metrics = play_window(a, "A", log, i)
        stop_player()
        b_metrics = play_window(b, "B", log, i)
        stop_player()
        summary = {
            "iteration": i,
            "a": a_metrics,
            "b": b_metrics,
            "started_at": target_start,
        }
        summaries.append(summary)
        print(
            "[iter {}] A: t_final={}s player={} progressed={} | B: t_final={}s player={} progressed={}".format(
                i,
                a_metrics["final_t_sec"],
                a_metrics["player_type"],
                a_metrics["progressed"],
                b_metrics["final_t_sec"],
                b_metrics["player_type"],
                b_metrics["progressed"],
            )
        )
    summary_path = OUT_DIR / "user_two_summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2, default=str))
    print("\nLog:     {}".format(log))
    print("Summary: {}".format(summary_path))


if __name__ == "__main__":
    main()
