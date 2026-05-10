"""Alternate between two pre-validated 12 Angry Men CiNEFiLE streams.

Picks two storages whose .mkv head fetches return 206 + a non-zero
body, then loops 20 iterations × 2 min apart. Each iteration plays A,
sleeps 60 s, plays B, sleeps 60 s. Records the timeline and a per-
iteration summary so you can confirm both URLs play end-to-end (and
without fault-proxy interference, since the goal here is just baseline
streamability — not the cutover yet).

Routes through fault-proxy on port 8280 so the URLs match what the
addon's stream_proxy would consume; fault-proxy with no schedule
simply forwards bytes upstream.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

NZBDAV_URL = os.environ.get("NZBDAV_URL", "http://localhost:8180").rstrip("/")
NZBDAV_API_KEY = os.environ["NZBDAV_API_KEY"]
KODI_URL = os.environ.get("KODI_URL", "http://localhost:8082").rstrip("/")
KODI_AUTH = ("kodi", "kodi")
WEBDAV_BASE = os.environ.get(
    "WEBDAV_BASE", "http://nzbdav-extreme-fault-proxy:8280"
).rstrip("/")
WEBDAV_USERNAME = os.environ["WEBDAV_USERNAME"]
WEBDAV_PASSWORD = os.environ["WEBDAV_PASSWORD"]
ITERATIONS = int(os.environ.get("CINEFILE_ITERATIONS", "20"))
INTERVAL_SECONDS = int(os.environ.get("CINEFILE_INTERVAL", "120"))
PER_STREAM_PLAY_SECONDS = int(os.environ.get("CINEFILE_PLAY_SECONDS", "60"))
OUT_DIR = Path(os.environ.get("CINEFILE_OUT_DIR", "/tmp/cinefile_two")).resolve()


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


def _basic_auth_header() -> str:
    auth = base64.b64encode(
        "{}:{}".format(WEBDAV_USERNAME, WEBDAV_PASSWORD).encode()
    ).decode()
    return "Basic " + auth


def _build_base_with_auth() -> str:
    parsed = urllib.parse.urlsplit(WEBDAV_BASE)
    netloc = "{}:{}@{}".format(
        urllib.parse.quote(WEBDAV_USERNAME, safe=""),
        urllib.parse.quote(WEBDAV_PASSWORD, safe=""),
        parsed.netloc,
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )


WEBDAV_BASE_WITH_AUTH = _build_base_with_auth()


def _propfind_mkv_path(storage: str) -> str:
    safe = urllib.parse.quote(storage, safe="/") + "/"
    url = "{}/dav{}".format(NZBDAV_URL, safe)
    req = urllib.request.Request(
        url,
        method="PROPFIND",
        headers={"Depth": "1", "Authorization": _basic_auth_header()},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
            xml_text = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    # First href that ends in .mkv (skip sample.mkv if present)
    candidates = re.findall(r"<D:href>([^<]+\.mkv)</D:href>", xml_text)
    for c in candidates:
        if "sample" not in c.lower():
            return c
    return candidates[0] if candidates else ""


def _verify_streamable(mkv_path: str) -> bool:
    """Check the .mkv exposes both video AND audio.

    nzbdav-rs occasionally reports a job as Completed when its
    deobfuscator reconstructed an audio-only stub — the user-visible
    failure mode is "file plays as music in Kodi". Open the URL in
    Kodi briefly, ask for ``streamdetails``, and reject any file with
    an empty ``video`` array."""
    safe = urllib.parse.quote(mkv_path, safe="/")
    play_url = "{}/dav{}".format(WEBDAV_BASE_WITH_AUTH, safe)
    stop_player()
    play(play_url)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            active = _kodi_rpc("Player.GetActivePlayers").get("result", []) or []
            if active:
                pid = active[0].get("playerid", 1)
                resp = _kodi_rpc(
                    "Player.GetItem",
                    {"playerid": pid, "properties": ["streamdetails"]},
                )
                details = (
                    resp.get("result", {})
                    .get("item", {})
                    .get("streamdetails", {})
                )
                video = details.get("video", []) or []
                audio = details.get("audio", []) or []
                if video and audio:
                    stop_player()
                    return True
                if active and details:
                    # Kodi has the file open and reported streamdetails;
                    # if video is still empty here, this upload's mkv has
                    # no video track. No need to wait further.
                    if details and not video:
                        stop_player()
                        return False
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    stop_player()
    return False


def _stream_url_with_auth(mkv_path: str) -> str:
    parsed = urllib.parse.urlsplit(WEBDAV_BASE)
    netloc = "{}:{}@{}".format(
        urllib.parse.quote(WEBDAV_USERNAME, safe=""),
        urllib.parse.quote(WEBDAV_PASSWORD, safe=""),
        parsed.netloc,
    )
    base = urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return "{}/dav{}".format(base, urllib.parse.quote(mkv_path, safe="/"))


def find_two_streamable_urls() -> list[str]:
    qs = urllib.parse.urlencode(
        {
            "mode": "history",
            "apikey": NZBDAV_API_KEY,
            "output": "json",
            "limit": 500,
        }
    )
    with urllib.request.urlopen(  # nosec B310
        "{}/api?{}".format(NZBDAV_URL, qs), timeout=10
    ) as r:
        data = json.load(r)
    target_prefix = os.environ.get(
        "CINEFILE_TARGET_PREFIX",
        "12.Angry.Men.1957.1080p.BluRay.x264-CiNEFiLE",
    )
    candidates = [
        s.get("storage", "").rstrip("/")
        for s in data.get("history", {}).get("slots", []) or []
        if s.get("status") == "Completed"
        and s.get("name", "").startswith(target_prefix)
    ]
    streamable = []
    seen_mkv = set()
    for storage in candidates:
        if not storage:
            continue
        mkv_path = _propfind_mkv_path(storage)
        if not mkv_path:
            print("  PROPFIND miss:  {}".format(storage))
            continue
        # Skip duplicates (multiple bulk submits sometimes share the
        # same nzbdav-rs deobfuscated UUID) — same file isn't a
        # different upload from the user's POV.
        if mkv_path in seen_mkv:
            continue
        seen_mkv.add(mkv_path)
        ok = _verify_streamable(mkv_path)
        print(
            "  {} streamable={}  {}".format(
                "OK" if ok else "BAD", ok, mkv_path
            )
        )
        if ok:
            streamable.append(mkv_path)
        if len(streamable) >= 2:
            break
    return [_stream_url_with_auth(p) for p in streamable]


def player_status() -> dict:
    try:
        active = _kodi_rpc("Player.GetActivePlayers").get("result", []) or []
        if not active:
            return {"active": False}
        pid = active[0].get("playerid", 1)
        props = _kodi_rpc(
            "Player.GetProperties",
            {
                "playerid": pid,
                "properties": ["time", "speed", "percentage"],
            },
        )
        return {"active": True, "playerid": pid, "props": props.get("result", {})}
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
    samples = 0
    progressed = False
    with log.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "iter": iteration,
                    "label": label,
                    "type": "play",
                    "url": url[:160],
                    "play_resp": play_resp,
                    "t": started_at,
                }
            )
            + "\n"
        )
    while time.time() < deadline:
        status = player_status()
        if status.get("active"):
            t_sec = time_to_seconds(status.get("props", {}).get("time", {}))
            if t_sec > last_t_sec + 0.05:
                stall = time.time() - last_progress_t
                if last_t_sec > 0 and stall > max_stall:
                    max_stall = stall
                last_progress_t = time.time()
                last_t_sec = t_sec
                if last_t_sec > 0.5:
                    progressed = True
            samples += 1
        time.sleep(0.5)
    return {
        "label": label,
        "url": url[:160],
        "started_at": started_at,
        "duration": round(time.time() - started_at, 2),
        "final_t_sec": round(last_t_sec, 2),
        "max_stall_seconds": round(max_stall, 2),
        "progressed": progressed,
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log = OUT_DIR / "two_stream.jsonl"
    if log.exists():
        log.unlink()

    print("Finding 2 streamable 12 Angry Men CiNEFiLE uploads...")
    urls = find_two_streamable_urls()
    if len(urls) < 2:
        print("FATAL: need 2 streamable URLs, got {}".format(len(urls)))
        sys.exit(2)
    a, b = urls[0], urls[1]
    print("\nA: {}".format(a))
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
            "[iter {}] A: t_final={}s stall_max={}s progressed={} | B: t_final={}s stall_max={}s progressed={}".format(
                i,
                a_metrics["final_t_sec"],
                a_metrics["max_stall_seconds"],
                a_metrics["progressed"],
                b_metrics["final_t_sec"],
                b_metrics["max_stall_seconds"],
                b_metrics["progressed"],
            )
        )

    summary_path = OUT_DIR / "two_stream_summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2, default=str))
    print("\nLog:     {}".format(log))
    print("Summary: {}".format(summary_path))


if __name__ == "__main__":
    main()
