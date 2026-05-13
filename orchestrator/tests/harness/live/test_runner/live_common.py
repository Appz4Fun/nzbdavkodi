"""Shared helpers for the live no-Kodi harness tests."""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ORCHESTRATOR = os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:4000")
NZBDAV_URL = os.environ.get("NZBDAV_URL", "http://nzbdav-rs:8080")
HYDRA_URL = os.environ.get("HYDRA_URL", "http://hydra2:5076")
HYDRA_API_KEY = os.environ.get("HYDRA_API_KEY", "")
NZBDAV_API_KEY = os.environ.get("NZBDAV_API_KEY", "extreme-api-key")
WEBDAV_USERNAME = os.environ.get("WEBDAV_USERNAME", "kodi")
WEBDAV_PASSWORD = os.environ.get("WEBDAV_PASSWORD", "")
NNTP_HOST = os.environ.get("NNTP_HOST", "")
NNTP_PORT = int(os.environ.get("NNTP_PORT", "563"))
NNTP_USE_SSL = os.environ.get("NNTP_USE_SSL", "true").lower() == "true"
NNTP_USER = os.environ.get("NNTP_USER", "")
NNTP_PASS = os.environ.get("NNTP_PASS", "")
NNTP_CONNS = int(os.environ.get("NNTP_CONNS", "80"))

HYDRA_PROVIDERS = {
    "hydra": {
        "base_url": HYDRA_URL,
        "api_key": HYDRA_API_KEY,
        "max_results": int(os.environ.get("LIVE_EXTREME_MAX_RESULTS", "100")),
    }
}

NZBDAV_CFG = {
    "base_url": NZBDAV_URL,
    "api_key": NZBDAV_API_KEY,
    "webdav_url": NZBDAV_URL,
    "webdav_content_root": "content",
}

IMDB_CORPUS = [
    {"rank": 1, "title": "The Shawshank Redemption", "year": 1994, "imdb": "tt0111161"},
    {"rank": 2, "title": "The Godfather", "year": 1972, "imdb": "tt0068646"},
    {"rank": 3, "title": "The Dark Knight", "year": 2008, "imdb": "tt0468569"},
    {"rank": 4, "title": "The Godfather Part II", "year": 1974, "imdb": "tt0071562"},
    {"rank": 5, "title": "12 Angry Men", "year": 1957, "imdb": "tt0050083"},
    {
        "rank": 6,
        "title": "The Lord of the Rings: The Return of the King",
        "year": 2003,
        "imdb": "tt0167260",
    },
    {"rank": 7, "title": "Schindler's List", "year": 1993, "imdb": "tt0108052"},
    {
        "rank": 8,
        "title": "The Lord of the Rings: The Fellowship of the Ring",
        "year": 2001,
        "imdb": "tt0120737",
    },
    {"rank": 9, "title": "Pulp Fiction", "year": 1994, "imdb": "tt0110912"},
    {
        "rank": 10,
        "title": "The Lord of the Rings: The Two Towers",
        "year": 2002,
        "imdb": "tt0167261",
    },
    {
        "rank": 11,
        "title": "The Good the Bad and the Ugly",
        "year": 1966,
        "imdb": "tt0060196",
    },
    {"rank": 12, "title": "Forrest Gump", "year": 1994, "imdb": "tt0109830"},
    {"rank": 13, "title": "Fight Club", "year": 1999, "imdb": "tt0137523"},
    {"rank": 14, "title": "Inception", "year": 2010, "imdb": "tt1375666"},
    {
        "rank": 15,
        "title": "Star Wars: Episode V - The Empire Strikes Back",
        "year": 1980,
        "imdb": "tt0080684",
    },
    {"rank": 16, "title": "The Matrix", "year": 1999, "imdb": "tt0133093"},
    {"rank": 17, "title": "Goodfellas", "year": 1990, "imdb": "tt0099685"},
    {"rank": 18, "title": "Interstellar", "year": 2014, "imdb": "tt0816692"},
    {
        "rank": 19,
        "title": "One Flew Over the Cuckoo's Nest",
        "year": 1975,
        "imdb": "tt0073486",
    },
    {"rank": 20, "title": "Se7en", "year": 1995, "imdb": "tt0114369"},
]


def wait(url: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            response = requests.get(url, timeout=3)
            if response.ok:
                return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_err}")


def seed_nzbdav() -> None:
    provider_config = json.dumps(
        {
            "Providers": [
                {
                    "Type": 1,
                    "Host": NNTP_HOST,
                    "Port": NNTP_PORT,
                    "UseSsl": NNTP_USE_SSL,
                    "User": NNTP_USER,
                    "Pass": NNTP_PASS,
                    "MaxConnections": NNTP_CONNS,
                }
            ]
        },
        separators=(",", ":"),
    )
    response = requests.post(
        f"{NZBDAV_URL}/api/update-config",
        data={
            "api.key": NZBDAV_API_KEY,
            "webdav.user": WEBDAV_USERNAME,
            "webdav.pass": WEBDAV_PASSWORD,
            "usenet.providers": provider_config,
        },
        headers={"X-Api-Key": NZBDAV_API_KEY},
        timeout=10,
    )
    response.raise_for_status()


def report_dir() -> Path:
    root = Path(os.environ.get("LIVE_REPORT_ROOT", "/reports"))
    explicit = os.environ.get("LIVE_EXTREME_REPORT_DIR")
    if explicit:
        path = root / explicit
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        path = root / f"live-harness-{stamp}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def corpus_sample() -> list[dict]:
    pinned = os.environ.get("LIVE_EXTREME_IMDB_ID", "").strip()
    if pinned:
        return [movie for movie in IMDB_CORPUS if movie["imdb"] == pinned] or [
            {"rank": 0, "title": "The Dark Knight", "year": 2008, "imdb": pinned}
        ]
    sample_size = int(os.environ.get("LIVE_EXTREME_SAMPLE_SIZE", "3"))
    return IMDB_CORPUS[: max(1, min(sample_size, len(IMDB_CORPUS)))]


def search_movie(movie: dict) -> dict:
    response = requests.post(
        f"{ORCHESTRATOR}/v1/search",
        json={
            "search": {
                "kind": "movie",
                "title": movie["title"],
                "year": movie["year"],
                "imdb_id": movie["imdb"],
            },
            "providers": HYDRA_PROVIDERS,
        },
        timeout=120,
    )
    response.raise_for_status()
    return response.json()


def resolve_candidate(movie: dict, candidates: list[dict]) -> dict:
    primary = candidates[0]
    peer_limit = int(os.environ.get("LIVE_EXTREME_CANDIDATE_PEERS", "5"))
    peers = [
        {"nzb_url": peer["nzb_url"], "title": peer["title"]}
        for peer in candidates[1 : peer_limit + 1]
        if peer.get("nzb_url") and peer.get("title")
    ]
    timeout = int(os.environ.get("LIVE_EXTREME_DOWNLOAD_TIMEOUT_SECS", "2400"))
    response = requests.post(
        f"{ORCHESTRATOR}/v1/resolve",
        json={
            "nzb_url": primary["nzb_url"],
            "title": primary["title"],
            "resolve_id": f"live-extreme-{movie['imdb']}",
            "candidate_peers": peers,
            "peer_pool_cache_key": f"live-extreme:{movie['imdb']}",
            "nzbdav": NZBDAV_CFG,
            "poll_interval_secs": 10,
            "download_timeout_secs": timeout,
        },
        timeout=timeout + 60,
    )
    response.raise_for_status()
    return response.json()


def fetch_resolve_events(resolve_id: str) -> list[dict]:
    response = requests.get(
        f"{ORCHESTRATOR}/v1/resolve/{resolve_id}/events", timeout=30
    )
    response.raise_for_status()
    events = []
    for line in response.text.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line.split("data:", 1)[1].strip()))
    return events


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def validated_peer_count(resolve_response: dict) -> int:
    return sum(
        1
        for peer in resolve_response.get("peers", [])
        if peer.get("validation_state") == "byte_sample_validated_phase_3"
    )
