"""Bulk-submit duplicate uploads from IMDB Top 50 to nzbdav-rs.

For each IMDB Top 50 movie, query NZBHydra2's *internal* API with
``showSingleResultPerSearchResultGroup=false`` so duplicate Usenet
uploads of the same release are returned. Group by exact title, pick
the largest grouping, deduplicate by ``searchResultId`` (so each
submission is a different Usenet upload — different article IDs — of
byte-equivalent content), and POST each NZB URL to nzbdav-rs's
SABnzbd-style ``addurl`` endpoint. Also save a copy of every NZB body
under ``OUT_DIR`` for offline inspection.
"""

from __future__ import annotations

# Pull the movie list from the existing test fixture without importing
# the test module: that module's top-level transitively pulls in
# xbmcaddon (only present inside Kodi). AST-extract the assignment so
# the script can run with plain CPython.
import ast
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def _load_movie_list():
    src = (
        Path(__file__).resolve().parents[3]
        / "tests"
        / "test_functional_fallback_playback.py"
    ).read_text()
    module = ast.parse(src)
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "IMDB_TOP_50_MOVIES"
        ):
            return list(ast.literal_eval(node.value))
    raise RuntimeError("IMDB_TOP_50_MOVIES not found")


IMDB_TOP_50_MOVIES = _load_movie_list()

HYDRA_URL = os.environ.get("HYDRA_URL", "").rstrip("/")
if not HYDRA_URL:
    raise RuntimeError("HYDRA_URL is required")
HYDRA_API_KEY = os.environ["HYDRA_API_KEY"]
NZBDAV_URL = os.environ.get("NZBDAV_URL", "http://localhost:8180").rstrip("/")
NZBDAV_API_KEY = os.environ["NZBDAV_API_KEY"]
OUT_DIR = Path(os.environ.get("BULK_NZB_DIR", "/tmp/bulk_nzbs")).resolve()
SUBMIT_PARALLELISM = int(os.environ.get("BULK_PARALLEL", "8"))


def hydra_search(movie: dict) -> list[dict]:
    """Internal-api search for one movie. Returns raw searchResults list."""
    payload = {
        "query": "{} {}".format(movie["title"], movie["year"]),
        "imdbId": movie["imdb"].lstrip("t"),
        "mode": "movie",
        "showSingleResultPerSearchResultGroup": False,
        "loadAll": True,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "{}/internalapi/search".format(HYDRA_URL),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
        return json.load(r).get("searchResults", []) or []


# Mirrors repo/plugin.video.nzbdav/resources/lib/fallback_streams.py:
# _PEER_BYTES_TOLERANCE_FRACTION = 0.20. Two uploads of the same release
# carry near-identical playable bytes; the small slack absorbs yEnc
# segmentation noise and the cosmetic .par2/extra-script trailers some
# uploaders attach.
PEER_BYTES_TOLERANCE = 0.20


def _result_size(r: dict) -> int:
    raw = r.get("size", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def biggest_group(results: list[dict]) -> tuple[str, list[dict]]:
    """Pick the largest cluster of same-release Usenet uploads.

    Group by exact title first, then within that group keep only entries
    whose indexer size is within +/-20% of the group's median — same
    addon-side tolerance the runtime fallback peer-matcher applies.
    Deduplicate by searchResultId so each retained row is a distinct
    upload (different article IDs, byte-equivalent content).
    """
    by_title: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        title = r.get("title", "")
        if title:
            by_title[title].append(r)
    if not by_title:
        return "", []
    title, items = max(by_title.items(), key=lambda kv: len(kv[1]))

    sizes = sorted(s for s in (_result_size(r) for r in items) if s > 0)
    if sizes:
        median = sizes[len(sizes) // 2]
        tol = median * PEER_BYTES_TOLERANCE
        items = [
            r
            for r in items
            if _result_size(r) == 0 or abs(_result_size(r) - median) <= tol
        ]

    seen_ids = set()
    unique = []
    for r in items:
        rid = r.get("searchResultId")
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        unique.append(r)
    return title, unique


def fetch_nzb_url(result: dict) -> str:
    """Resolve a Hydra searchResult into a downloadable NZB URL."""
    rid = result.get("searchResultId")
    if rid is None:
        return ""
    return "{}/getnzb/user/{}?apikey={}".format(HYDRA_URL, rid, HYDRA_API_KEY)


def download_nzb(url: str) -> bytes:
    """Fetch the NZB body. Hydra serves it as text/xml gzip or plain."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as r:  # nosec B310
        return r.read()


def submit_to_nzbdav(nzb_url: str, name: str) -> tuple[bool, str]:
    """addurl-mode submit. Returns (ok, info)."""
    qs = urllib.parse.urlencode(
        {
            "mode": "addurl",
            "name": nzb_url,
            "nzbname": name,
            "apikey": NZBDAV_API_KEY,
            "output": "json",
        }
    )
    req = urllib.request.Request("{}/api?{}".format(NZBDAV_URL, qs))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310
            body = json.loads(r.read().decode("utf-8", "replace"))
        if body.get("status") and body.get("nzo_ids"):
            return True, body["nzo_ids"][0]
        return False, str(body)[:200]
    except urllib.error.HTTPError as e:
        return False, "HTTP {} {}".format(e.code, e.read()[:200])
    except Exception as e:  # pylint: disable=broad-except
        return False, str(e)[:200]


def save_nzb(out_dir: Path, group_title: str, idx: int, indexer: str, body: bytes):
    """Persist one NZB body for offline inspection."""
    safe = "".join(c if c.isalnum() or c in ".-_" else "_" for c in group_title)[:120]
    safe_idx = "".join(c if c.isalnum() else "_" for c in indexer)[:30]
    path = out_dir / "{}__upload-{:02d}__{}.nzb".format(safe, idx, safe_idx)
    path.write_bytes(body)
    return path


def process_movie(movie: dict) -> dict:
    """End-to-end: search → group → download+submit each upload."""
    out = {"movie": movie["title"], "year": movie["year"], "imdb": movie["imdb"]}
    try:
        results = hydra_search(movie)
    except Exception as e:  # pylint: disable=broad-except
        out["error"] = "hydra search failed: {}".format(e)
        return out

    title, group = biggest_group(results)
    out["group_title"] = title
    out["group_size"] = len(group)
    if not group:
        out["error"] = "no groupable results"
        return out

    movie_dir = OUT_DIR / movie["imdb"]
    movie_dir.mkdir(parents=True, exist_ok=True)

    submissions = []
    for idx, result in enumerate(group):
        url = fetch_nzb_url(result)
        if not url:
            continue
        # Download a copy for offline inspection (best-effort).
        try:
            body = download_nzb(url)
            saved = save_nzb(movie_dir, title, idx, result.get("indexer", ""), body)
        except Exception as e:  # pylint: disable=broad-except
            saved = "download-failed: {}".format(e)

        nzbname = "{} [bulk-{:02d}-{}]".format(
            title, idx, (result.get("indexer") or "")[:20]
        )
        ok, info = submit_to_nzbdav(url, nzbname)
        submissions.append(
            {
                "indexer": result.get("indexer"),
                "size": result.get("size"),
                "saved_to": str(saved),
                "ok": ok,
                "info": info,
            }
        )
    out["submissions"] = submissions
    return out


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    summary = []
    with ThreadPoolExecutor(max_workers=SUBMIT_PARALLELISM) as ex:
        futures = {ex.submit(process_movie, m): m for m in IMDB_TOP_50_MOVIES}
        for fut in as_completed(futures):
            res = fut.result()
            summary.append(res)
            count = res.get("group_size", 0)
            errors = sum(1 for s in res.get("submissions", []) if not s.get("ok"))
            print(
                "[{:>4.0f}s] {:<55} group_size={:>2} errors={} {}".format(
                    time.time() - started,
                    res["movie"][:55],
                    count,
                    errors,
                    res.get("error", ""),
                )
            )
    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    print("\nSummary: {}".format(summary_path))
    print("NZBs:    {}".format(OUT_DIR))


if __name__ == "__main__":
    main()
