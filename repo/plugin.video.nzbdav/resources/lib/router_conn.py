# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors
# pylint: disable=cyclic-import

"""Service connection-test helpers (``/test_*`` routes) split out of ``router``.

The suite patches several of these via ``resources.lib.router.<name>``
(``_test_connection``, ``_string``, ``xbmcaddon.Addon``, …) and re-imports
others, so every router-resident or router-patched name is reached at call time
through ``import resources.lib.router as _router``. ``router`` re-exports these
functions so the existing ``@patch`` / ``from resources.lib.router import``
references keep resolving.
"""

from urllib.parse import urlencode

from resources.lib.xml_safety import ParseError as _XmlParseError
from resources.lib.xml_safety import safe_fromstring as _safe_fromstring


def _test_connection(label, url, test_url, ok_condition):
    """Test a service connection and notify the user of the result.

    If url is empty, notifies "<label> URL not configured". Otherwise
    issues a GET to test_url, notifies "<label> connection OK" when
    ok_condition(response) is True, "<label>: unexpected response" when
    False, and "<label>: <error>" (truncated to 60 chars) on exception.
    """
    import resources.lib.router as _router
    from resources.lib.http_util import http_get, notify, redact_url

    if not url:
        notify(_router._addon_name(), "{} URL not configured".format(label), 3000)
        return
    try:
        response = http_get(test_url)
        if ok_condition(response):
            notify(_router._addon_name(), "{} connection OK".format(label), 3000)
        else:
            notify(
                _router._addon_name(),
                "{}: unexpected response".format(label),
                5000,
            )
    except Exception as e:
        # urllib exceptions often embed the full URL (with apikey!) in
        # str(e). The verbatim-URL substitution catches the most common
        # case; ``redact_text`` handles the residue (apikey embedded in
        # an error phrase, percent-encoded variants, etc.) — TODO.md §H.2-M31.
        from resources.lib.http_util import redact_text

        err_msg = str(e).replace(test_url, redact_url(test_url))
        err_msg = redact_text(err_msg)
        notify(_router._addon_name(), "{}: {}".format(label, err_msg[:60]), 5000)


def _json_object(response):
    """Parse a JSON object response, returning an empty dict on bad shape."""
    import json

    try:
        data = json.loads(response)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _xml_root_name(response):
    """Return the unqualified root XML tag name, lowercased.

    The payload is a user-configured Hydra/Newznab service response, so it is
    parsed through :func:`resources.lib.xml_safety.safe_fromstring`, which
    refuses entity declarations (XXE / billion-laughs) on both the
    ``defusedxml`` path and the stdlib fallback that Kodi installs take. A
    hostile payload falls through to the empty-string result.
    """
    try:
        root = _safe_fromstring(response)
    except (TypeError, ValueError, _XmlParseError):
        return ""
    return root.tag.rsplit("}", 1)[-1].lower()


def _hydra_search_response_ok(response):
    """True when NZBHydra/Newznab returned an authenticated search RSS payload."""
    return _xml_root_name(response) == "rss"


def _nzbdav_queue_response_ok(response):
    """True when nzbdav returned an authenticated queue payload."""
    data = _json_object(response)
    return isinstance(data.get("queue"), dict)


def _prowlarr_indexers_response_ok(response):
    """True when Prowlarr returned the authenticated indexer list."""
    import json

    try:
        data = json.loads(response)
    except (TypeError, ValueError):
        return False
    return isinstance(data, list)


def _test_hydra_connection():
    """Test NZBHydra2 connection and API-key auth with a lightweight search."""
    import resources.lib.router as _router

    addon = _router.xbmcaddon.Addon("plugin.video.nzbdav")
    url = addon.getSetting("hydra_url").rstrip("/")
    api_key = addon.getSetting("hydra_api_key")
    params = {
        "apikey": api_key,
        "t": "search",
        "q": "__nzbdav_connection_test__",
        "o": "xml",
        "limit": "1",
    }
    test_url = "{}/api?{}".format(url, urlencode(params))
    _router._test_connection("NZBHydra", url, test_url, _hydra_search_response_ok)


def _test_prowlarr_connection():
    """Test Prowlarr connection by hitting the indexer endpoint."""
    import resources.lib.router as _router

    addon = _router.xbmcaddon.Addon("plugin.video.nzbdav")
    host = addon.getSetting("prowlarr_host").rstrip("/")
    api_key = addon.getSetting("prowlarr_api_key")

    test_url = "{}/api/v1/indexer?apikey={}".format(host, api_key)
    _router._test_connection("Prowlarr", host, test_url, _prowlarr_indexers_response_ok)


def _test_webdav_connection():
    """Test WebDAV reachability and credentials with the shared probe."""
    import resources.lib.router as _router
    from resources.lib.http_util import notify
    from resources.lib.webdav import probe_webdav_reachable

    reachable, error = probe_webdav_reachable(max_retries=0)
    if reachable:
        notify(_router._addon_name(), _router._string(30189), 3000)
    elif error == "auth_failed":
        notify(_router._addon_name(), _router._string(30190), 5000)
    elif error == "server_error":
        notify(_router._addon_name(), _router._string(30191), 5000)
    else:
        notify(_router._addon_name(), _router._string(30192), 5000)


def _test_direct_indexers_connection():
    """Test configured direct Newznab indexer caps endpoints."""
    import resources.lib.router as _router
    from resources.lib.direct_indexers import test_configured_indexers
    from resources.lib.http_util import notify

    ok_count, total_count, errors = test_configured_indexers()
    if total_count == 0:
        notify(_router._addon_name(), _router._string(30176), 3000)
    elif ok_count == total_count:
        notify(_router._addon_name(), _router._fmt(30177, ok_count, total_count), 3000)
    else:
        notify(
            _router._addon_name(),
            _router._fmt(30178, errors[0] if errors else "unknown"),
            5000,
        )


def _test_nzbdav_connection():
    """Test nzbdav connection and API-key auth by reading the queue."""
    import resources.lib.router as _router

    addon = _router.xbmcaddon.Addon("plugin.video.nzbdav")
    url = addon.getSetting("nzbdav_url").rstrip("/")
    api_key = addon.getSetting("nzbdav_api_key")
    params = {
        "mode": "queue",
        "start": "0",
        "limit": "0",
        "apikey": api_key,
        "output": "json",
    }
    test_url = "{}/api?{}".format(url, urlencode(params))
    _router._test_connection("nzbdav", url, test_url, _nzbdav_queue_response_ok)


def _test_nzbget_connection():
    """Test NZBGet JSON-RPC reachability + auth via the version method."""
    import resources.lib.router as _router
    from resources.lib.http_util import notify
    from resources.lib.nzbget_api import test_connection

    ok, _error = test_connection()
    if ok:
        notify(_router._addon_name(), _router._string(30224), 3000)
    else:
        notify(_router._addon_name(), _router._string(30225), 5000)


def _test_nzbget_smb():
    """Test the SMB completed-folder root is listable via xbmcvfs."""
    import xbmcvfs

    import resources.lib.router as _router
    from resources.lib.http_util import notify

    addon = _router.xbmcaddon.Addon("plugin.video.nzbdav")
    smb_root = addon.getSetting("nzbget_smb_root").strip()
    # xbmcvfs.listdir() does NOT raise for an unreachable/typo'd/wrong-
    # credentials SMB path — it returns ([], []) and only logs at the C++
    # VFS layer — so a non-raising listdir is a false "reachable". Gate on
    # xbmcvfs.exists(), which returns False for those paths (the same
    # positive-signal check player_installer.py uses).
    reachable = False
    if smb_root:
        try:
            reachable = bool(xbmcvfs.exists(smb_root))
        except Exception:  # pylint: disable=broad-except
            reachable = False
    if reachable:
        notify(_router._addon_name(), _router._string(30226), 3000)
    else:
        notify(_router._addon_name(), _router._string(30227), 5000)
