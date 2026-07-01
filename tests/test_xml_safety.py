# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the shared XXE / billion-laughs safe XML parser.

These lock in the behaviour every indexer/WebDAV XML client depends on:
entity-free XML parses identically to the stdlib, while any entity
declaration (billion-laughs or external XXE) is rejected on *both* the
``defusedxml`` path and the standard-library fallback that real Kodi
installs take.
"""

import xml.etree.ElementTree as stdlib_et

import pytest
from resources.lib import xml_safety
from resources.lib.xml_safety import ParseError, UnsafeXmlError, safe_fromstring

# A bounded internal-entity payload. Left unchecked, ``&d;`` expands to
# 8**3 = 512 copies of "lol" — the billion-laughs shape, kept small so a
# regression that *fails* to reject it still cannot hang the suite.
_INTERNAL_ENTITY_XML = (
    '<?xml version="1.0"?>\n'
    "<!DOCTYPE root [\n"
    '  <!ENTITY a "lol">\n'
    '  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">\n'
    '  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">\n'
    '  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;">\n'
    "]>\n"
    "<root><x>&d;</x></root>"
)

_EXTERNAL_ENTITY_XML = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>'
    "<r>&xxe;</r>"
)

_VALID_XML = (
    '<?xml version="1.0"?>'
    "<rss><channel><item><title>Movie</title></item></channel></rss>"
)


def test_valid_xml_parses_identically_to_stdlib():
    root = safe_fromstring(_VALID_XML)
    assert root.tag == "rss"
    assert root.find("./channel/item/title").text == "Movie"


def test_valid_xml_accepts_bytes():
    root = safe_fromstring(_VALID_XML.encode("utf-8"))
    assert root.tag == "rss"


def test_internal_entity_rejected_default_path():
    # Dev/CI has defusedxml installed, exercising the preferred path.
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(_INTERNAL_ENTITY_XML)


def test_external_entity_rejected_default_path():
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(_EXTERNAL_ENTITY_XML)


@pytest.mark.parametrize("payload", [_INTERNAL_ENTITY_XML, _EXTERNAL_ENTITY_XML])
def test_entities_rejected_on_stdlib_fallback(monkeypatch, payload):
    # Force the no-defusedxml code path that packaged Kodi installs take.
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(payload)


def test_valid_xml_still_parses_on_stdlib_fallback(monkeypatch):
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    root = safe_fromstring(_VALID_XML)
    assert root.tag == "rss"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32"])
def test_multibyte_encoded_entity_bytes_rejected_on_stdlib_fallback(
    monkeypatch, encoding
):
    # A raw ``rb"<!ENTITY"`` byte scan misses multi-byte encodings, but the
    # stdlib parser still decodes and expands them — so the guard must decode
    # the payload to logical text before scanning. (CodeRabbit / Codacy #370.)
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    payload = _INTERNAL_ENTITY_XML.encode(encoding)
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(payload)


@pytest.mark.parametrize(
    "payload",
    [
        '<root><!-- config example: <!ENTITY x "y"> do not use --><a>ok</a></root>',
        "<root><![CDATA[ <!ENTITY evil SYSTEM 'x'> ]]></root>",
        "<root><a>&lt;!ENTITY escaped &quot;text&quot;&gt;</a></root>",
    ],
)
def test_inert_entity_literal_without_doctype_is_not_a_false_positive(
    monkeypatch, payload
):
    # A literal ``<!ENTITY`` inside a comment / CDATA / escaped text — with no
    # DOCTYPE — is inert to the parser and must not be wrongly rejected, or a
    # legitimate indexer feed silently returns zero results.
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    root = safe_fromstring(payload)
    assert root.tag == "root"


def test_valid_utf16_bytes_parse_on_stdlib_fallback(monkeypatch):
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    root = safe_fromstring(_VALID_XML.encode("utf-16"))
    assert root.tag == "rss"


# BOM-less multi-byte streams: the entity declaration must still be caught from
# the ``<``-start byte pattern alone, and the decoder must pick the right
# endianness. ``utf-16-le`` / ``-be`` here have no BOM by construction.
@pytest.mark.parametrize(
    "encoding", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"]
)
def test_bomless_multibyte_entity_bytes_rejected_on_stdlib_fallback(
    monkeypatch, encoding
):
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    payload = _INTERNAL_ENTITY_XML.encode(encoding)
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(payload)


# Only UTF-16 variants here: expat (the stdlib parser) does not support UTF-32,
# so a valid UTF-32 document raises ParseError regardless of the guard — and by
# the same token a UTF-32 entity payload can't reach entity expansion on the
# stdlib path anyway. The guard still rejects UTF-32 entity declarations
# defensively (see the rejection tests above).
@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_valid_multibyte_bytes_parse_on_stdlib_fallback(monkeypatch, encoding):
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    root = safe_fromstring(_VALID_XML.encode(encoding))
    assert root.tag == "rss"


def test_utf8_bom_entity_bytes_rejected_and_valid_parses(monkeypatch):
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    bom = b"\xef\xbb\xbf"
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(bom + _INTERNAL_ENTITY_XML.encode("utf-8"))
    root = safe_fromstring(bom + _VALID_XML.encode("utf-8"))
    assert root.tag == "rss"


def test_malformed_xml_raises_parse_error():
    with pytest.raises(ParseError):
        safe_fromstring("<rss><channel></rss>")


def test_unsafe_error_is_value_error_subclass():
    # Callers that already catch ValueError keep working unchanged.
    assert issubclass(UnsafeXmlError, ValueError)
