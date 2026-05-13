"""
Live harness tests — real NZBHydra2, real nzbdav-rs, orchestrator under test.

No mocks. Every service is real:
  hydra2      → NZBHydra2 (must have indexers configured; see just harness-live-init)
  nzbdav-rs   → real SABnzbd-compatible downloader + WebDAV
  orchestrator → our Rust binary under test

The nzbdav-rs is seeded with NNTP credentials at session startup via its
/api/update-config endpoint. Hydra2 must already have indexers configured
in its persistent volume (one-time setup via just harness-live-init).

Test IMDb target: The Dark Knight (2008), tt0468569 by default.
Change via LIVE_IMDB_ID env var.

Fast tests (always run):
  test_health, test_hydra_search_returns_candidates,
  test_hydra_search_top_candidate_has_nzb_url

Slow tests (real download, 10-30 min):
  test_full_resolve_returns_stream_url
  Opt-in: set LIVE_FULL_RESOLVE=1
"""

import json
import os
import time

import pytest
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
LIVE_IMDB_ID = os.environ.get("LIVE_IMDB_ID", "tt0468569")
FULL_RESOLVE = os.environ.get("LIVE_FULL_RESOLVE", "0") == "1"

_HYDRA_PROVIDERS = {
    "hydra": {
        "base_url": HYDRA_URL,
        "api_key": HYDRA_API_KEY,
        "max_results": 50,
    }
}

_NZBDAV_CFG = {
    "base_url": NZBDAV_URL,
    "api_key": NZBDAV_API_KEY,
    "webdav_url": NZBDAV_URL,
    "webdav_content_root": "content",
}


def _wait(url: str, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=3)
            if r.ok:
                return
        except Exception as exc:
            last_err = exc
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_err}")


def _seed_nzbdav() -> None:
    """Configure nzbdav-rs with NNTP credentials via /api/update-config."""
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

    r = requests.post(
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
    r.raise_for_status()


@pytest.fixture(scope="session", autouse=True)
def wait_for_stack():
    """Wait for all services and seed nzbdav-rs with NNTP credentials."""
    _wait(f"{ORCHESTRATOR}/v1/health")
    _wait(f"{NZBDAV_URL}/health")
    # Hydra2 startup is slower than other services — poll its root page.
    _wait(f"{HYDRA_URL}/", timeout=120)
    _seed_nzbdav()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


def test_health():
    r = requests.get(f"{ORCHESTRATOR}/v1/health", timeout=5)
    assert r.ok, r.text
    d = r.json()
    assert d["status"] == "ok"


# ---------------------------------------------------------------------------
# search (real Hydra fan-out to configured indexers)
# ---------------------------------------------------------------------------


def _search_dark_knight():
    r = requests.post(
        f"{ORCHESTRATOR}/v1/search",
        json={
            "search": {
                "kind": "movie",
                "title": "The Dark Knight",
                "year": 2008,
                "imdb_id": LIVE_IMDB_ID,
            },
            "providers": _HYDRA_PROVIDERS,
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def test_hydra_search_returns_candidates():
    """Real Hydra search — expects at least one candidate."""
    d = _search_dark_knight()
    assert d["total_candidates"] > 0, (
        f"Expected >0 candidates from Hydra for imdb_id={LIVE_IMDB_ID}, got 0. "
        f"Make sure Hydra2 has at least one indexer configured at {HYDRA_URL}"
    )


def test_hydra_search_candidate_shape():
    """Top candidates have required fields and a usable nzb_url."""
    d = _search_dark_knight()
    assert d["candidates"], "No candidates"
    for c in d["candidates"][:5]:
        assert c.get("nzb_url"), f"Missing nzb_url in candidate: {c}"
        assert c.get("title"), f"Missing title in candidate: {c}"
        assert c.get("size", 0) > 0, f"Zero size in candidate: {c}"
        assert c["nzb_url"].startswith("http"), f"nzb_url not HTTP: {c['nzb_url']}"


def test_hydra_search_includes_large_releases():
    """Expect ≥1 result ≥ 1 GiB — confirms real HD results are being returned."""
    d = _search_dark_knight()
    large = [c for c in d["candidates"] if c.get("size", 0) >= 1_073_741_824]
    assert large, (
        "No candidates ≥ 1 GiB — indexer may only have samples or low-quality results. "
        f"All sizes: {sorted(c.get('size', 0) for c in d['candidates'])[:10]}"
    )


# ---------------------------------------------------------------------------
# resolve (real download — slow, opt-in via LIVE_FULL_RESOLVE=1)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not FULL_RESOLVE, reason="Set LIVE_FULL_RESOLVE=1 to run full download test")
def test_full_resolve_returns_stream_url():
    """
    Full pipeline: Hydra search → submit top candidate to nzbdav-rs →
    poll until download completes → WebDAV byte-sample → assert stream_url.

    Warning: real HD NZBs take 10-30 minutes to download. The orchestrator
    blocks until nzbdav-rs marks the job Completed and WebDAV probe succeeds.
    """
    d = _search_dark_knight()
    assert d["candidates"], "No candidates to resolve"
    primary = d["candidates"][0]

    r = requests.post(
        f"{ORCHESTRATOR}/v1/resolve",
        json={
            "nzb_url": primary["nzb_url"],
            "title": primary["title"],
            "nzbdav": _NZBDAV_CFG,
            "poll_interval_secs": 10,
            "download_timeout_secs": 2400,
        },
        timeout=2460,
    )
    assert r.ok, r.text
    d = r.json()

    assert d.get("resolve_id"), "resolve_id missing"
    assert d.get("stream_url"), "stream_url missing"
    assert d["stream_url"].startswith("http"), f"unexpected stream_url: {d['stream_url']}"

    # Verify the stream URL actually serves bytes.
    stream_r = requests.get(
        d["stream_url"],
        headers=d.get("stream_headers", {}),
        timeout=30,
    )
    assert stream_r.ok, f"stream_url {d['stream_url']} returned {stream_r.status_code}"
    cl = int(stream_r.headers.get("content-length", 0))
    assert cl > 0, "stream_url returned zero content-length"
