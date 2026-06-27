"""Drive 20 fallback-cutover trials against the 7 CiNEFiLE uploads.

For each iteration:
1. Pick the next CiNEFiLE storage as primary, the rest as fallbacks.
2. Schedule a ``connection_reset`` fault on fault-proxy at t+10s.
3. Tell Kodi (via JSON-RPC) to Player.Open the *primary* URL routed
   through the fault-proxy. Direct WebDAV play, not addon-routed —
   the addon's stream_proxy fingerprint cutover requires going through
   resolve_and_play; this runner is a complement that exercises just
   nzbdav-rs's per-storage WebDAV serving and the fault-proxy schedule
   API. When the primary's connection drops, the runner immediately
   issues a second Player.Open at the next storage so we measure how
   fast a same-content cut-over can land in Kodi when the URLs are
   already known.
4. Sample Player.GetProperties every 0.25 s; flag any iteration where
   playback never reached non-zero ``time_sec`` (a true stall).
5. Wait until the next 2 min mark and repeat 20 times.

Outputs a JSONL timeline at ``OUT_DIR/cinefile_loop.jsonl`` plus a
summary line per iteration.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

NZBDAV_URL = os.environ.get("NZBDAV_URL", "http://localhost:8180").rstrip("/")
NZBDAV_API_KEY = os.environ["NZBDAV_API_KEY"]
KODI_URL = os.environ.get("KODI_URL", "http://localhost:8082").rstrip("/")
KODI_AUTH = ("kodi", "kodi")
FAULT_PROXY_CONTROL = os.environ.get(
    "FAULT_PROXY_CONTROL", "http://localhost:8281"
).rstrip("/")
WEBDAV_BASE = os.environ.get(
    "WEBDAV_BASE", "http://nzbdav-extreme-fault-proxy:8280"
).rstrip("/")
ITERATIONS = int(os.environ.get("CINEFILE_ITERATIONS", "20"))
INTERVAL_SECONDS = int(os.environ.get("CINEFILE_INTERVAL", "120"))
PRIMARY_PLAY_SECONDS = float(os.environ.get("CINEFILE_PLAY_SECONDS", "10"))
OUT_DIR = Path(os.environ.get("CINEFILE_OUT_DIR", "/tmp/cinefile_loop")).resolve()


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
    auth = urllib.parse.quote(user) + ":" + urllib.parse.quote(pw)
    import base64

    req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
        return json.loads(r.read())


def list_completed_cinefile_storages() -> list[str]:
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
    # nzbdav-rs's SAB-style ``search`` param doesn't actually filter the
    # response, so we filter client-side: only Completed rows whose name
    # starts with the 12 Angry Men + CiNEFiLE prefix.
    target_prefix = "12.Angry.Men.1957.1080p.BluRay.x264-CiNEFiLE"
    out = []
    for slot in data.get("history", {}).get("slots", []) or []:
        if slot.get("status") != "Completed":
            continue
        if not slot.get("name", "").startswith(target_prefix):
            continue
        storage = slot.get("storage", "").rstrip("/")
        if storage:
            out.append(storage)
    return out


def _propfind_mkv_name(storage: str) -> str:
    """PROPFIND nzbdav-rs to find the actual .mkv filename in a storage.

    nzbdav-rs writes the playable file under an obfuscated UUID-style
    basename (e.g. ``2764ae48...mkv``); the folder carries the human
    name, the file does not. We have to ask the WebDAV server which
    .mkv lives in the folder before we can build a stream URL.

    Goes direct to nzbdav-rs (HOST_NZBDAV_URL = ``localhost:8180``) so
    we don't burn the fault-proxy's per-request control plane on these
    setup calls.
    """
    user = os.environ["WEBDAV_USERNAME"]
    pw = os.environ["WEBDAV_PASSWORD"]
    import base64

    auth = base64.b64encode("{}:{}".format(user, pw).encode()).decode()
    safe_storage = urllib.parse.quote(storage, safe="/") + "/"
    url = "{}/content{}".format(NZBDAV_URL, safe_storage)
    req = urllib.request.Request(
        url, method="PROPFIND", headers={"Depth": "1", "Authorization": "Basic " + auth}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
            xml_text = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    # Cheap parse — pull the first `<...mkv` href.
    import re

    for href in re.findall(r"<D:href>([^<]+\.mkv)</D:href>", xml_text):
        return href
    return ""


def storage_to_stream_url(storage: str) -> str:
    """Map nzbdav storage path to a fault-proxy WebDAV URL with auth."""
    mkv_path = _propfind_mkv_name(storage)
    if not mkv_path:
        return ""
    user = os.environ["WEBDAV_USERNAME"]
    pw = os.environ["WEBDAV_PASSWORD"]
    parsed = urllib.parse.urlsplit(WEBDAV_BASE)
    netloc = "{}:{}@{}".format(
        urllib.parse.quote(user, safe=""),
        urllib.parse.quote(pw, safe=""),
        parsed.netloc,
    )
    base_with_auth = urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
    return "{}/content{}".format(base_with_auth, urllib.parse.quote(mkv_path, safe="/"))


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
    """POST an empty schedule so prior iterations' faults don't fire."""
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
        resp = _kodi_rpc("Player.GetActivePlayers")
        for p in resp.get("result", []) or []:
            _kodi_rpc("Player.Stop", {"playerid": p.get("playerid", 1)})
    except Exception:  # noqa: BLE001
        pass


def play_via_direct_play(primary_url: str, fallback_urls: list[str]):
    """Play through the addon's /direct_play route.

    Hands the primary URL plus the list of validated fallback URLs to
    the addon's stream_proxy in a single shot. The proxy fingerprint-
    validates each fallback (100×4 KiB SHA256 sweep) before it ever
    swaps the upstream — Kodi reads from one proxy URL the whole time,
    so a primary article failure surfaces to the user as nothing more
    than an extra ~100 ms of buffer wait while the swap completes.
    """
    qs = urllib.parse.urlencode(
        {
            "primary_url": primary_url,
            "fallback_urls": json.dumps(fallback_urls),
        }
    )
    plugin_url = "plugin://plugin.video.nzbdav/direct_play?{}".format(qs)
    # Addons.ExecuteAddon doesn't dispatch a route URL — only Player.Open
    # actually triggers setResolvedUrl + start playback for plugin://
    # paths. Match the pattern used by cinefile_proxy_swap_loop.py and
    # cinefile_user_two.py.
    return _kodi_rpc("Player.Open", {"item": {"file": plugin_url}})


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
                "properties": ["time", "totaltime", "speed", "percentage"],
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


def run_iteration(iteration: int, urls: list[str], log: Path) -> dict:
    primary = urls[iteration % len(urls)]
    fallback_pool = [u for u in urls if u != primary]
    summary = {
        "iteration": iteration,
        "started_at": time.time(),
        "primary": redact_url(primary),
        "fallback_pool": [redact_url(url) for url in fallback_pool],
        "events": [],
    }

    def record(event_type: str, **kw):
        rec = {"t": time.time(), "type": event_type, **kw}
        summary["events"].append(rec)
        with log.open("a") as fh:
            fh.write(json.dumps({"iter": iteration, **rec}) + "\n")

    stop_player()
    clear_fault_schedule()
    record("schedule_fault")
    schedule_fault(PRIMARY_PLAY_SECONDS, "connection_reset")
    record(
        "play_via_direct_play",
        primary=redact_url(primary),
        fallback_count=len(fallback_pool),
    )
    play_resp = play_via_direct_play(primary, fallback_pool)
    record("play_response", body=play_resp)

    # Observe-only loop — the addon's stream_proxy is the actor. We just
    # record whether playback advances past the fault window and whether
    # it ever stalls long enough to be visible to the user.
    deadline = time.time() + PRIMARY_PLAY_SECONDS + 30
    last_status = {}
    last_progress_t = time.time()
    last_t_sec = -1.0
    progress_at_fault = None
    progress_post_fault = False
    max_stall_seconds = 0.0
    while time.time() < deadline:
        status = player_status()
        last_status = status
        elapsed = time.time() - summary["started_at"]
        if status.get("active"):
            props = status.get("props", {})
            t_sec = time_to_seconds(props.get("time", {}))
            if t_sec > last_t_sec + 0.05:
                stall = time.time() - last_progress_t
                if last_t_sec > 0:
                    max_stall_seconds = max(max_stall_seconds, stall)
                last_progress_t = time.time()
                last_t_sec = t_sec
                if elapsed > PRIMARY_PLAY_SECONDS + 1:
                    progress_post_fault = True
            elif elapsed > PRIMARY_PLAY_SECONDS + 1:
                stall = time.time() - last_progress_t
                max_stall_seconds = max(max_stall_seconds, stall)
            if elapsed >= PRIMARY_PLAY_SECONDS and progress_at_fault is None:
                progress_at_fault = t_sec
            record(
                "poll",
                elapsed=round(elapsed, 2),
                t_sec=t_sec,
                speed=props.get("speed"),
                percentage=props.get("percentage"),
            )
        else:
            record("inactive", elapsed=round(elapsed, 2))
        time.sleep(0.25)
    summary["progress_at_fault"] = progress_at_fault
    summary["progress_post_fault"] = progress_post_fault
    summary["max_stall_seconds"] = round(max_stall_seconds, 2)
    summary["final_t_sec"] = last_t_sec

    summary["last_status"] = last_status
    summary["finished_at"] = time.time()
    return summary


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    log = OUT_DIR / "cinefile_loop.jsonl"
    if log.exists():
        log.unlink()

    storages = list_completed_cinefile_storages()
    if len(storages) < 2:
        print("FATAL: need at least 2 CiNEFiLE storages, got {}".format(len(storages)))
        sys.exit(2)
    urls = [storage_to_stream_url(s) for s in storages if storage_to_stream_url(s)]
    print("Found {} CiNEFiLE WebDAV URLs:".format(len(urls)))
    for u in urls:
        print("  {}".format(redact_url(u)))

    summaries = []
    test_start = time.time()
    for i in range(ITERATIONS):
        target_start = test_start + i * INTERVAL_SECONDS
        wait = target_start - time.time()
        if wait > 0:
            print("[iter {}] sleeping {:.1f}s until next 2-min mark".format(i, wait))
            time.sleep(wait)
        print("[iter {}] starting at +{:.0f}s".format(i, time.time() - test_start))
        s = run_iteration(i, urls, log)
        summaries.append(s)
        elapsed = s["finished_at"] - s["started_at"]
        print(
            "[iter {}] done in {:.1f}s — t_at_fault={} t_final={:.1f}s "
            "post_fault_progress={} max_stall={:.1f}s".format(
                i,
                elapsed,
                s.get("progress_at_fault"),
                s.get("final_t_sec", 0) or 0,
                s.get("progress_post_fault"),
                s.get("max_stall_seconds", 0) or 0,
            )
        )

    summary_path = OUT_DIR / "cinefile_summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2, default=str))
    print("\nLog:     {}".format(log))
    print("Summary: {}".format(summary_path))


if __name__ == "__main__":
    main()
