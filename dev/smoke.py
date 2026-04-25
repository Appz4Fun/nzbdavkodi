#!/usr/bin/env python3
"""End-to-end stream validator for the nzbdavkodi addon, minus Kodi.

Drops the search/Hydra half of the addon. Takes an NZB (local file
or URL), submits it to nzbdav using the addon's *real* nzbdav_api
module, polls using the addon's *real* webdav + resolver helpers, and
prints a VLC-playable URL once the file is available.

Nothing about the network path is faked: every HTTP request the addon
would make in Kodi is made here, against a real nzbdav container.
Only the Kodi runtime modules (xbmc, xbmcgui, xbmcplugin, xbmcaddon,
xbmcvfs) are mocked — matching tests/conftest.py.

Usage:
    # 1. Stand up the stack and complete nzbdav first-run setup.
    # 2. Drop an NZB into dev/nzbs/, then:
    python3 dev/smoke.py dev/nzbs/sintel.nzb \\
        --nzbdav-api-key ... --webdav-user ... --webdav-pass ...

    # Or pull from a URL (no local HTTP server spun up):
    python3 dev/smoke.py --nzb-url https://example.com/foo.nzb ...

Env vars are accepted for every flag:
    NZBDAV_URL, NZBDAV_API_KEY, WEBDAV_URL, WEBDAV_USER, WEBDAV_PASS
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / "plugin.video.nzbdav"
sys.path.insert(0, str(ADDON))
sys.path.insert(0, str(ADDON / "resources/lib"))

# Mock Kodi modules BEFORE any addon import.
for _m in ("xbmc", "xbmcgui", "xbmcplugin", "xbmcaddon", "xbmcvfs"):
    sys.modules[_m] = MagicMock()

import xbmc  # noqa: E402
import xbmcaddon  # noqa: E402

# Log-level ints the addon references as module constants.
xbmc.LOGDEBUG = 0
xbmc.LOGINFO = 1
xbmc.LOGWARNING = 2
xbmc.LOGERROR = 3

_LEVELS = {0: "DBG", 1: "INFO", 2: "WARN", 3: "ERR"}


def _log(msg, level=1):
    print("[{}] {}".format(_LEVELS.get(level, "?"), msg))


xbmc.log = _log


class _Monitor:
    def waitForAbort(self, _secs):
        return False


xbmc.Monitor = _Monitor


def _install_settings(settings: dict):
    """Wire xbmcaddon.Addon().getSetting(key) to return from settings dict."""
    addon_mock = MagicMock()
    addon_mock.getSetting.side_effect = lambda k: settings.get(k, "")
    xbmcaddon.Addon.return_value = addon_mock
    # Some nzbdav_api paths call xbmcaddon.Addon() freshly; side_effect on
    # the class call returns the same mock.
    xbmcaddon.Addon = MagicMock(return_value=addon_mock)


def _detect_host_gateway() -> str:
    """Return an address nzbdav (in Docker) can use to reach this host.

    host.docker.internal is wired via the compose file's extra_hosts.
    Fall back to the primary LAN IP if someone runs this script against
    a remote nzbdav.
    """
    return "host.docker.internal"


def _serve_nzb(nzb_path: Path) -> tuple[HTTPServer, str]:
    """Start a background HTTP server serving the NZB's parent dir.

    Returns (server, fetch_url). The server is a daemon thread; it dies
    with the script. Only one request from nzbdav is expected but we
    leave it running for the lifetime of the script just in case nzbdav
    re-fetches.
    """
    handler = partial(SimpleHTTPRequestHandler, directory=str(nzb_path.parent))
    srv = HTTPServer(("0.0.0.0", 0), handler)  # nosec B104 — dev only
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://{}:{}/{}".format(_detect_host_gateway(), port, quote(nzb_path.name))
    return srv, url


def _main():
    ap = argparse.ArgumentParser(
        description="Drive the addon's real submit/poll/resolve path against nzbdav."
    )
    ap.add_argument(
        "nzb", nargs="?", help="Path to local .nzb (served via a local HTTP server)"
    )
    ap.add_argument("--nzb-url", help="URL of an NZB (alternative to positional file)")
    ap.add_argument(
        "--title", default=None, help="Job name (default: derived from NZB filename)"
    )
    ap.add_argument(
        "--nzbdav-url",
        default=os.environ.get("NZBDAV_URL", "http://localhost:8180"),
        help="default: %(default)s (nzbdav-rs container listens on 8080; host-published on NZBDAV_PORT, default 8180)",
    )
    ap.add_argument(
        "--nzbdav-api-key",
        default=os.environ.get("NZBDAV_API_KEY", ""),
        help="nzbdav web UI > Settings > SABnzbd > API key",
    )
    ap.add_argument(
        "--webdav-url",
        default=os.environ.get("WEBDAV_URL", ""),
        help="(optional) defaults to --nzbdav-url",
    )
    ap.add_argument(
        "--webdav-user",
        default=os.environ.get("WEBDAV_USER", ""),
        help="nzbdav web UI > Settings > WebDAV > Username",
    )
    ap.add_argument(
        "--webdav-pass",
        default=os.environ.get("WEBDAV_PASS", ""),
        help="nzbdav web UI > Settings > WebDAV > Password",
    )
    ap.add_argument("--poll-interval", type=int, default=5)
    ap.add_argument(
        "--timeout", type=int, default=1800, help="Overall poll timeout (s)"
    )
    args = ap.parse_args()

    if not args.nzb and not args.nzb_url:
        ap.error("supply a local NZB path or --nzb-url")
    if not args.nzbdav_api_key:
        ap.error("--nzbdav-api-key is required (or NZBDAV_API_KEY env)")

    # nzbdav-rs mounts the WebDAV tree under /dav/ (e.g. /dav/content/...),
    # while the SABnzbd API lives at the root (/api?mode=...). Upstream
    # nzbdav serves WebDAV at the root. Default webdav_url to
    # nzbdav_url + /dav so the nzbdav-rs layout works out of the box; an
    # explicit --webdav-url wins for upstream or non-default layouts.
    nzbdav_url = args.nzbdav_url.rstrip("/")
    webdav_url = (args.webdav_url or "{}/dav".format(nzbdav_url)).rstrip("/")

    # Settings the addon reads via xbmcaddon.Addon().getSetting(...)
    settings = {
        "nzbdav_url": nzbdav_url,
        "nzbdav_api_key": args.nzbdav_api_key,
        "webdav_url": webdav_url,
        "webdav_username": args.webdav_user,
        "webdav_password": args.webdav_pass,
        "submit_timeout": "120",
    }
    _install_settings(settings)

    # Resolve the NZB URL nzbdav will fetch.
    if args.nzb_url:
        nzb_url = args.nzb_url
        title = args.title or "smoke-test-" + str(int(time.time()))
    else:
        nzb_path = Path(args.nzb).expanduser().resolve()
        if not nzb_path.is_file():
            print("error: {} is not a file".format(nzb_path))
            sys.exit(2)
        _, nzb_url = _serve_nzb(nzb_path)
        title = args.title or nzb_path.stem
        _log("serving {} to nzbdav at {}".format(nzb_path, nzb_url))

    # Import AFTER mocks + settings are installed (modules run xbmc.log
    # at import time in some cases).
    from resources.lib.nzbdav_api import (  # noqa: E402
        get_job_history,
        get_job_status,
        submit_nzb,
    )
    from resources.lib.resolver import _storage_to_webdav_path  # noqa: E402
    from resources.lib.webdav import (  # noqa: E402
        find_video_file,
        get_webdav_stream_url_for_path,
    )

    _log("submitting NZB (title={!r}) to {}".format(title, settings["nzbdav_url"]))
    nzo_id, err = submit_nzb(nzb_url, title)
    if err:
        _log("submit error: {}".format(err), level=3)
        sys.exit(1)
    if not nzo_id:
        _log("submit returned no nzo_id — check nzbdav logs", level=3)
        sys.exit(1)
    _log("accepted, nzo_id={}".format(nzo_id))

    start = time.time()
    last_status = None
    while True:
        if time.time() - start > args.timeout:
            _log("timed out after {}s".format(args.timeout), level=3)
            sys.exit(1)

        hist = get_job_history(nzo_id)
        if hist and hist.get("status") == "Completed":
            storage = hist["storage"]
            _log("job Completed; storage={}".format(storage))
            folder = _storage_to_webdav_path(storage)
            _log("resolving video under {}".format(folder))
            video_path = find_video_file(folder)
            if not video_path:
                _log(
                    "completed but no playable file found at {}".format(folder), level=3
                )
                sys.exit(1)
            url, headers = get_webdav_stream_url_for_path(video_path)
            print("\n=== STREAM READY ===")
            print("webdav file: {}".format(video_path))
            print("stream url : {}".format(url))
            if headers:
                print("auth header: <redacted>")
            print("\nplay in vlc:")
            print('  vlc "{}"'.format(url))
            if args.webdav_user:
                print(
                    "\nIf WebDAV auth is enabled, pass the username/password "
                    "from your shell or password manager instead of logging "
                    "them here."
                )
            return

        if hist and hist.get("status") == "Failed":
            _log("download failed: {}".format(hist.get("fail_message", "")), level=3)
            sys.exit(1)

        q = get_job_status(nzo_id)
        if q:
            status = q.get("status", "?")
            pct = q.get("percentage", "0")
            line = "  status={} {}%".format(status, pct)
            if status != last_status:
                _log(line)
                last_status = status
            else:
                _log(line, level=0)
        else:
            _log("  (not in queue yet — nzbdav still fetching/parsing)", level=0)

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    try:
        _main()
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
