"""
SABnzbd-compatible API stub + WebDAV file server.

State machine: jobs are created "Completed" the moment addurl is called.
This means poll_until_terminal returns on the very first get_job_history
probe, so tests run in under a second with no artificial sleeping.

The pre-resolve find_completed_by_name check starts on an empty history,
so the orchestrator always goes through the full submit→poll→stream path.
"""

import os
import threading
import time
import uuid

from flask import Flask, Response, request

app = Flask(__name__)

FIXTURE = "/fixtures/sample.mkv"

_jobs = {}  # nzo_id -> {"title": str, "submitted_at": float}
_lock = threading.Lock()


def _fixture_size():
    try:
        return os.path.getsize(FIXTURE)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# SABnzbd API
# ---------------------------------------------------------------------------


@app.route("/api")
def api():
    mode = request.args.get("mode", "")

    if mode == "addurl":
        raw_name = request.args.get("name", "")
        nzbname = request.args.get("nzbname") or raw_name or "unknown"
        nzo_id = "NZO-" + uuid.uuid4().hex[:8].upper()
        with _lock:
            _jobs[nzo_id] = {"title": nzbname, "submitted_at": time.time()}
        return {"status": True, "nzo_id": nzo_id}

    if mode == "queue":
        # Jobs complete immediately — queue is always empty.
        return {"queue": {"slots": []}}

    if mode == "history":
        nzo_ids_param = request.args.get("nzo_ids", "")
        search_term = request.args.get("search", "")
        filter_ids = set(nzo_ids_param.split(",")) - {""} if nzo_ids_param else set()

        with _lock:
            slots = []
            for nzo_id, job in _jobs.items():
                if filter_ids and nzo_id not in filter_ids:
                    continue
                if search_term and job["title"] != search_term:
                    continue
                safe_name = job["title"].replace("/", "_").replace("\\", "_")
                slots.append(
                    {
                        "nzo_id": nzo_id,
                        "name": job["title"],
                        "status": "Completed",
                        # storage path → WebDAV via storage_to_webdav_path():
                        # /content/uncategorized/<name>/ → PROPFIND /content/uncategorized/<name>/
                        "storage": f"/content/uncategorized/{safe_name}",
                        "fail_message": None,
                        "completed": int(job["submitted_at"]),
                    }
                )
        return {"history": {"slots": slots}}

    return {"error": f"unknown mode: {mode!r}"}, 400


# ---------------------------------------------------------------------------
# WebDAV
# ---------------------------------------------------------------------------


@app.route("/content/<path:rest>", methods=["PROPFIND"])
def webdav_propfind(rest):
    folder = "/" + rest.rstrip("/") + "/"
    file_href = folder + "sample.mkv"
    size = _fixture_size()
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:multistatus xmlns:D="DAV:">'
        f"<D:response>"
        f"  <D:href>{folder}</D:href>"
        f"  <D:propstat>"
        f"    <D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop>"
        f"    <D:status>HTTP/1.1 200 OK</D:status>"
        f"  </D:propstat>"
        f"</D:response>"
        f"<D:response>"
        f"  <D:href>{file_href}</D:href>"
        f"  <D:propstat>"
        f"    <D:prop>"
        f"      <D:resourcetype/>"
        f"      <D:getcontentlength>{size}</D:getcontentlength>"
        f"    </D:prop>"
        f"    <D:status>HTTP/1.1 200 OK</D:status>"
        f"  </D:propstat>"
        f"</D:response>"
        f"</D:multistatus>"
    )
    return Response(xml, status=207, mimetype="application/xml")


@app.route("/content/<path:rest>", methods=["GET", "HEAD"])
def webdav_get(rest):
    if not rest.lower().endswith(".mkv"):
        return "Not found", 404
    size = _fixture_size()
    if request.method == "HEAD":
        return Response(
            "",
            headers={"Content-Length": str(size), "Accept-Ranges": "bytes"},
            mimetype="video/x-matroska",
        )

    range_header = request.headers.get("Range", "")
    if range_header.startswith("bytes="):
        spec = range_header[6:]
        parts = spec.split("-", 1)
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else size - 1
        end = min(end, size - 1)
        length = end - start + 1
        with open(FIXTURE, "rb") as fh:
            fh.seek(start)
            data = fh.read(length)
        return Response(
            data,
            status=206,
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(len(data)),
                "Accept-Ranges": "bytes",
            },
            mimetype="video/x-matroska",
        )

    with open(FIXTURE, "rb") as fh:
        data = fh.read()
    return Response(
        data,
        headers={"Content-Length": str(size), "Accept-Ranges": "bytes"},
        mimetype="video/x-matroska",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
