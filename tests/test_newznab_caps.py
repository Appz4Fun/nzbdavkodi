# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from resources.lib.newznab_caps import (
    CAPS_MAX_BYTES,
    build_caps_url,
    fetch_caps,
    normalize_api_endpoint,
    parse_caps,
)

CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server appversion="1.0" />
  <searching>
    <search available="yes" supportedParams="q" />
    <tv-search available="yes" supportedParams="q,imdbid,tvdbid,season,ep" />
    <movie-search available="yes" supportedParams="q,imdbid" />
    <audio-search available="no" supportedParams="q" />
  </searching>
  <categories>
    <category id="2000" name="Movies">
      <subcat id="2040" name="HD" />
    </category>
    <category id="5000" name="TV" />
  </categories>
</caps>
"""


def test_normalize_api_endpoint():
    assert (
        normalize_api_endpoint("https://api.nzbgeek.info")
        == "https://api.nzbgeek.info/api"
    )
    assert (
        normalize_api_endpoint("https://api.nzbgeek.info/")
        == "https://api.nzbgeek.info/api"
    )
    assert (
        normalize_api_endpoint("https://api.nzbgeek.info/api")
        == "https://api.nzbgeek.info/api"
    )
    assert (
        normalize_api_endpoint("https://api.nzbgeek.info/api/")
        == "https://api.nzbgeek.info/api"
    )
    # An explicit non-standard API path is preserved, only trailing slash trimmed.
    assert (
        normalize_api_endpoint("https://tabula-rasa.pw/api/v1/")
        == "https://tabula-rasa.pw/api/v1"
    )
    assert (
        normalize_api_endpoint("http://localhost:5076") == "http://localhost:5076/api"
    )
    assert normalize_api_endpoint("") == "/api"
    assert normalize_api_endpoint(None) == "/api"


def test_build_caps_url_appends_api_and_redacts_nothing():
    url = build_caps_url("https://api.nzbgeek.info", "secret")

    assert url.startswith("https://api.nzbgeek.info/api?")
    assert "t=caps" in url
    assert "apikey=secret" in url
    assert "o=xml" in url


def test_build_caps_url_empty_api_url():
    url_none = build_caps_url(None, "secret")
    url_empty = build_caps_url("", "secret")

    for url in (url_none, url_empty):
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        assert parts.path == "/api"
        assert query["apikey"] == ["secret"]
        assert query["t"] == ["caps"]
        assert query["o"] == ["xml"]


def test_build_caps_url_preserves_nonstandard_api_endpoint_paths():
    tabula = build_caps_url("https://tabula-rasa.pw/api/v1/", "secret")
    torbox = build_caps_url("https://torbox.app/newznab", "secret")

    assert urlsplit(tabula).path == "/api/v1"
    assert urlsplit(torbox).path == "/newznab"


def test_build_caps_url_preserves_existing_query_and_forces_caps_params():
    url = build_caps_url(
        "https://idx.example/api?foo=1&t=search&o=json&apikey=old", "secret"
    )
    parts = urlsplit(url)
    query = parse_qs(parts.query)

    assert parts.scheme == "https"
    assert parts.netloc == "idx.example"
    assert parts.path == "/api"
    assert query["foo"] == ["1"]
    assert query["apikey"] == ["secret"]
    assert query["t"] == ["caps"]
    assert query["o"] == ["xml"]


def test_parse_caps_reads_search_types_params_and_categories():
    caps = parse_caps(CAPS_XML)

    assert caps["search_types"] == ["search", "tvsearch", "movie"]
    assert caps["supported_params"]["search"] == ["q"]
    assert caps["supported_params"]["tvsearch"] == [
        "q",
        "imdbid",
        "tvdbid",
        "season",
        "ep",
    ]
    assert caps["supported_params"]["movie"] == ["q", "imdbid"]
    assert {"id": 2000, "name": "Movies"} in caps["categories"]
    assert {"id": 2040, "name": "HD"} in caps["categories"]


def test_parse_caps_invalid_xml_returns_empty_caps_and_error():
    caps = parse_caps("<html>bad")

    assert caps == {"search_types": [], "supported_params": {}, "categories": []}


def test_parse_caps_ignores_invalid_category_id():
    caps = parse_caps("""<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <categories>
    <category id="bad" name="Movies" />
    <category id="2000" name="Valid" />
  </categories>
</caps>
""")

    assert caps["categories"] == [{"id": 2000, "name": "Valid"}]


@patch("resources.lib.newznab_caps._http_get")
def test_fetch_caps_uses_caps_url(mock_http):
    mock_http.return_value = CAPS_XML

    caps, error = fetch_caps("https://api.nzbgeek.info", "secret")

    assert error is None
    assert "movie" in caps["search_types"]
    assert "t=caps" in mock_http.call_args[0][0]


@patch("resources.lib.newznab_caps._http_get")
def test_fetch_caps_limits_caps_response_size(mock_http):
    mock_http.return_value = CAPS_XML

    fetch_caps("https://api.nzbgeek.info", "secret")

    assert mock_http.call_args.kwargs["max_bytes"] == CAPS_MAX_BYTES


@patch("resources.lib.newznab_caps.xbmc")
@patch("resources.lib.newznab_caps._http_get")
def test_fetch_caps_handles_request_errors(mock_http, mock_xbmc):
    mock_http.side_effect = RuntimeError("network timeout")

    caps, error = fetch_caps("https://api.nzbgeek.info", "secret")

    assert caps == {"search_types": [], "supported_params": {}, "categories": []}
    assert "network timeout" in error
    mock_xbmc.log.assert_called_once()
    assert "network timeout" in mock_xbmc.log.call_args[0][0]
    assert mock_xbmc.log.call_args[0][1] is mock_xbmc.LOGWARNING


# --- XXE / billion-laughs hardening -----------------------------------------
# A hostile or compromised indexer can return a caps payload whose XML entities
# expand to exhaust CPU/memory. If the guard were absent, ``&yes;`` below would
# expand to ``available="yes"`` and yield a non-empty ``search_types``; the
# safe parser refuses the declaration outright and returns empty caps.
_CAPS_INTERNAL_ENTITY = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE caps [<!ENTITY yes "yes">]>'
    '<caps><searching><search available="&yes;" supportedParams="q" />'
    "</searching></caps>"
)
_CAPS_EXTERNAL_ENTITY = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE caps [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>'
    '<caps><searching><search available="&xxe;" /></searching></caps>'
)


@pytest.mark.parametrize("payload", [_CAPS_INTERNAL_ENTITY, _CAPS_EXTERNAL_ENTITY])
def test_parse_caps_rejects_entity_payloads(payload):
    assert parse_caps(payload) == {
        "search_types": [],
        "supported_params": {},
        "categories": [],
    }


@pytest.mark.parametrize("payload", [_CAPS_INTERNAL_ENTITY, _CAPS_EXTERNAL_ENTITY])
def test_parse_caps_rejects_entity_payloads_on_stdlib_fallback(monkeypatch, payload):
    # Force the no-defusedxml path packaged Kodi installs take.
    import xml.etree.ElementTree as stdlib_et

    from resources.lib import xml_safety

    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)

    assert not parse_caps(payload)["search_types"]


def test_parse_caps_still_parses_valid_caps_after_hardening():
    caps = parse_caps(CAPS_XML)

    assert "search" in caps["search_types"]
    assert "tvsearch" in caps["search_types"]
    assert {"id": 2000, "name": "Movies"} in caps["categories"]
