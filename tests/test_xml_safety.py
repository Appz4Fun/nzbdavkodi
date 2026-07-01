# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Tests for the shared XXE / billion-laughs safe XML parser.

These lock in the behaviour every indexer/WebDAV XML client depends on:
entity-free XML parses identically to the stdlib, while any entity
declaration (billion-laughs or external XXE) is rejected on *both* the
``defusedxml`` path and the standard-library fallback that real Kodi
installs take.
"""

import types
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
    # Whatever path the environment provides: defusedxml if bundled, else the
    # stdlib guard. CI ships no defusedxml, so this covers the stdlib guard;
    # the defusedxml branch is pinned by test_defusedxml_branch_forbids_dtd.
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(_INTERNAL_ENTITY_XML)


def test_external_entity_rejected_default_path():
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(_EXTERNAL_ENTITY_XML)


def test_defusedxml_branch_forbids_dtd(monkeypatch):
    # CI has no defusedxml, so the preferred branch (safe_fromstring's
    # ``_ET.fromstring(xml_text, forbid_dtd=False)``) would otherwise be
    # uncovered. Force it with a recording stub and assert it delegates with
    # ``forbid_dtd=False`` — a regression flipping that (which would reject
    # legitimate bare-DOCTYPE feeds) or dropping the call now ships red.
    recorded = {}

    def _fromstring(text, **kwargs):
        recorded.update(kwargs)
        return stdlib_et.fromstring(text)

    stub_et = types.SimpleNamespace(fromstring=_fromstring)
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", True)
    monkeypatch.setattr(xml_safety, "_ET", stub_et)

    root = safe_fromstring(_VALID_XML)

    assert root.tag == "rss"
    assert recorded == {"forbid_dtd": False}


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


# A BOM-less UTF-16 stream that opens with legal XML whitespace pushes the ``<``
# off byte 0, so a prefix-only sniffer misreads it as UTF-8 and misses the
# null-interleaved markers — yet stdlib expat still auto-detects UTF-16 and
# expands the entity. The guard must scan both UTF-16 endiannesses to catch it.
@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("lead", ["\n", "  \n\t", ""])
def test_bomless_utf16_entity_after_leading_whitespace_rejected(
    monkeypatch, encoding, lead
):
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    doc = lead + '<!DOCTYPE r [<!ENTITY a "x">]><r>&a;</r>'
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(doc.encode(encoding))


def test_malformed_xml_raises_parse_error():
    with pytest.raises(ParseError):
        safe_fromstring("<rss><channel></rss>")


def test_unsafe_error_is_value_error_subclass():
    # Callers that already catch ValueError keep working unchanged.
    assert issubclass(UnsafeXmlError, ValueError)


# --- multi-byte encoding decoder unit coverage --------------------------------
# White-box tests over _xml_bytes_to_text / _entity_scan_texts: every _BOM_CODECS
# branch, the declared-encoding path, and the unknown-encoding fallback. These
# lock the encoding logic the security guard depends on — a mutation that drops,
# reorders, or mis-maps a codec entry, or that swaps an endianness, turns red.


# Independent vectors: each payload is built from a FIXED encoding (not read
# back from _BOM_CODECS), so a mutation that swaps an endianness or drops an
# entry decodes to mojibake and this turns red. The sentinel is distinctive
# ASCII that only survives a *correct* decode.
_DOC = "<z>SENTINEL</z>"
_ENCODING_VECTORS = [
    ("utf-32-be+BOM", b"\x00\x00\xfe\xff" + _DOC.encode("utf-32-be")),
    ("utf-32-be+lead", _DOC.encode("utf-32-be")),
    ("utf-32-le+BOM", b"\xff\xfe\x00\x00" + _DOC.encode("utf-32-le")),
    ("utf-32-le+lead", _DOC.encode("utf-32-le")),
    ("utf-8-sig", _DOC.encode("utf-8-sig")),
    ("utf-16-be+BOM", b"\xfe\xff" + _DOC.encode("utf-16-be")),
    ("utf-16-be+lead", _DOC.encode("utf-16-be")),
    ("utf-16-le+BOM", b"\xff\xfe" + _DOC.encode("utf-16-le")),
    ("utf-16-le+lead", _DOC.encode("utf-16-le")),
]


@pytest.mark.parametrize(
    "payload", [v[1] for v in _ENCODING_VECTORS], ids=[v[0] for v in _ENCODING_VECTORS]
)
def test_xml_bytes_to_text_decodes_each_multibyte_encoding(payload):
    # The sentinel must come back intact — proving the prefix was mapped to the
    # correct codec AND endianness. A wrong codec yields null-interleaved/
    # byte-swapped mojibake that does not contain the contiguous sentinel.
    assert _DOC in xml_safety._xml_bytes_to_text(payload)


def test_bom_codecs_table_covers_every_supported_encoding():
    # Structural guard: every encoding the decoder must recognise is present, so
    # deleting an entry (which would silently drop its test case) turns red here.
    mapped = {codec for _prefix, codec in xml_safety._BOM_CODECS}
    assert mapped == {"utf-32-be", "utf-32-le", "utf-8-sig", "utf-16-be", "utf-16-le"}
    # Both endiannesses of UTF-16/32 must have BOTH a BOM and a bare-``<`` prefix.
    prefixes = {codec: [] for codec in mapped}
    for prefix, codec in xml_safety._BOM_CODECS:
        prefixes[codec].append(prefix)
    for codec in ("utf-16-be", "utf-16-le", "utf-32-be", "utf-32-le"):
        assert len(prefixes[codec]) == 2, codec


def test_xml_bytes_to_text_honours_declared_non_utf8_encoding():
    # No BOM, so the ``<?xml encoding=...?>`` declaration decides. ``\xe9`` is
    # "é" in ISO-8859-1 but invalid UTF-8 — a correct latin-1 decode yields "é".
    payload = b'<?xml version="1.0" encoding="iso-8859-1"?><d>caf\xe9</d>'
    assert "café" in xml_safety._xml_bytes_to_text(payload)


def test_xml_bytes_to_text_falls_back_to_utf8_on_unknown_encoding():
    # An unknown declared codec must not raise — it falls back to UTF-8 so the
    # entity scan still runs (covers the LookupError branch).
    payload = b'<?xml version="1.0" encoding="no-such-codec-xyz"?><d>ok</d>'
    assert "<d>ok</d>" in xml_safety._xml_bytes_to_text(payload)


def test_xml_bytes_to_text_defaults_to_utf8_without_bom_or_declaration():
    assert xml_safety._xml_bytes_to_text(b"<d>plain</d>") == "<d>plain</d>"


def test_entity_scan_texts_adds_both_utf16_endianness_views():
    # Best-guess decode + utf-16-le + utf-16-be = 3 views, so a BOM-less UTF-16
    # entity declaration is caught whichever endianness it uses.
    views = xml_safety._entity_scan_texts(b"<d/>")
    assert len(views) == 3


def test_unknown_declared_encoding_entity_still_rejected(monkeypatch):
    # End-to-end: an entity payload declaring a bogus codec must still be
    # rejected on the stdlib fallback (the UTF-8 fallback decode finds markers).
    monkeypatch.setattr(xml_safety, "_USING_DEFUSEDXML", False)
    monkeypatch.setattr(xml_safety, "_ET", stdlib_et)
    payload = (
        b'<?xml version="1.0" encoding="no-such-codec-xyz"?>'
        b'<!DOCTYPE r [<!ENTITY a "x">]><r>&a;</r>'
    )
    with pytest.raises(UnsafeXmlError):
        safe_fromstring(payload)
