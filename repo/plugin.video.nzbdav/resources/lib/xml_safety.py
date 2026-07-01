# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""Shared XXE / billion-laughs safe XML parsing for indexer responses.

Kodi installs typically ship only the Python standard library. Its
``xml.etree.ElementTree`` ignores *external* entities but still expands
*internal* ones, so a hostile or compromised indexer can hand us a
"billion laughs" payload (``<!ENTITY b "&a;&a;...">``) and exhaust CPU/memory
during parsing. Disabling expat's ``ExternalEntityRefHandler`` — the pattern
several call sites hand-rolled — does **not** stop this: internal entity
expansion has no such handler.

``defusedxml`` blocks both attack classes but is optional on Kodi. This module
prefers it and, on the standard-library fallback, textually refuses any entity
declaration before the parser can act on it. Entity-free XML parses identically
to ``xml.etree.ElementTree.fromstring``.

This consolidates the per-client XXE guards (some of them ineffective) into a
single reviewed implementation.
"""

import re

# This module is the XXE guard itself: the stdlib is the documented defusedxml
# fallback and the ``ParseError`` source, and entity declarations are refused in
# ``safe_fromstring`` before any parse — so this import is safe by construction.
# nosemgrep
import xml.etree.ElementTree as _stdlib_et  # nosec B405

try:  # pragma: no cover - defusedxml import branch is env-specific
    from defusedxml import ElementTree as _ET
    from defusedxml.common import DefusedXmlException as _UnsafeXmlError

    _USING_DEFUSEDXML = True
except ImportError:  # pragma: no cover - Kodi installs may not bundle defusedxml
    _ET = _stdlib_et

    _USING_DEFUSEDXML = False

    class _UnsafeXmlError(ValueError):
        """Raised when the stdlib fallback rejects an entity declaration."""


# ``ParseError`` is the stdlib exception for malformed XML; ``defusedxml``
# re-raises the same type, so callers can catch this single name.
ParseError = _stdlib_et.ParseError
# ``UnsafeXmlError`` is a ``ValueError`` subclass (``defusedxml`` guarantees it
# too), so existing ``except ValueError`` handlers keep working.
UnsafeXmlError = _UnsafeXmlError

# A real entity declaration only exists inside a ``<!DOCTYPE ... [ ... ]>``:
# billion-laughs and XXE both require the DTD. Scanning the *logical text* for
# both markers — rather than raw bytes for ``<!ENTITY`` alone — closes two gaps:
#   * a multi-byte encoding (UTF-16/UTF-32) can't smuggle the declaration past a
#     raw ASCII byte scan (the parser would still decode and expand it), and
#   * a literal ``<!ENTITY`` sitting inertly in a comment or CDATA section (with
#     no DOCTYPE) is no longer a false positive that rejects a valid feed.
# Requiring the DOCTYPE never misses an attack: without one the parser treats a
# stray ``<!ENTITY`` as malformed and raises rather than expanding it.
_DOCTYPE_RE = re.compile(r"<!DOCTYPE\b")
_ENTITY_DECL_RE = re.compile(r"<!ENTITY\b")
_XML_DECL_ENCODING_RE = re.compile(r'encoding\s*=\s*["\']([\w.\-]+)["\']')

# Leading-bytes → codec, per the XML spec's encoding auto-detection (Appendix F):
# an explicit BOM, or the byte pattern of the mandatory ``<`` start character
# under each UTF-16/32 endianness. Ordered longest-prefix first so a 4-byte
# UTF-32 marker is never shadowed by its 2-byte UTF-16 prefix. Endianness is
# explicit — decoding with the wrong one would mangle the ASCII markup tokens.
_BOM_CODECS = (
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\x00\x00\x00<", "utf-32-be"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"<\x00\x00\x00", "utf-32-le"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\x00<", "utf-16-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"<\x00", "utf-16-le"),
)


def _xml_bytes_to_text(payload):
    """Decode XML bytes to logical text for the entity scan.

    Honours a byte-order mark and the ``<?xml encoding=...?>`` declaration so
    UTF-16/UTF-32 payloads can't hide an entity declaration from the scan.
    ``errors="replace"`` keeps this best-effort; the real parse still runs on
    the original bytes.
    """
    for prefix, codec in _BOM_CODECS:
        if payload[: len(prefix)] == prefix:
            return payload.decode(codec, "replace")
    head = payload[:200].decode("ascii", "replace")
    match = _XML_DECL_ENCODING_RE.search(head)
    encoding = match.group(1) if match else "utf-8"
    try:
        return payload.decode(encoding, "replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", "replace")


def _has_entity_declaration(text):
    # A real declaration needs both a DOCTYPE and an ENTITY; requiring both
    # avoids rejecting an inert ``<!ENTITY`` literal in a comment/CDATA.
    return bool(_DOCTYPE_RE.search(text) and _ENTITY_DECL_RE.search(text))


def _entity_scan_texts(payload):
    """Decodings of ``payload`` to scan for an entity declaration.

    The best-guess decoding (BOM/declared-encoding) plus BOTH UTF-16
    endiannesses. The UTF-16 candidates catch a BOM-less UTF-16 stream the
    prefix sniffer can't classify — e.g. one that opens with legal XML
    whitespace so the ``<`` isn't at byte 0 — which stdlib expat still
    auto-detects and would expand. A valid, null-free UTF-8/single-byte
    document cannot spell the null-interleaved markers under a UTF-16 decode,
    so scanning these extra views adds no false positives.
    """
    texts = [_xml_bytes_to_text(payload)]
    for codec in ("utf-16-le", "utf-16-be"):
        try:
            texts.append(payload.decode(codec, "replace"))
        except (LookupError, ValueError):  # pragma: no cover - codecs bundled
            pass
    return texts


def _reject_entity_declarations(xml_text):
    if isinstance(xml_text, (bytes, bytearray)):
        candidates = _entity_scan_texts(bytes(xml_text))
    else:
        candidates = [xml_text]
    for text in candidates:
        if _has_entity_declaration(text):
            raise _UnsafeXmlError("XML entity declarations are not allowed")


def safe_fromstring(xml_text):
    """Parse ``xml_text`` into an ``Element``, refusing entity declarations.

    Raises :data:`ParseError` for malformed XML and :data:`UnsafeXmlError`
    (a ``ValueError`` subclass) for any payload that declares XML entities.
    Valid, entity-free XML parses exactly like the standard library.
    """
    if _USING_DEFUSEDXML:
        # ``forbid_dtd=False`` keeps a harmless bare ``<!DOCTYPE>`` working
        # while ``forbid_entities`` (default) still blocks entity definitions.
        # nosemgrep
        return _ET.fromstring(xml_text, forbid_dtd=False)
    _reject_entity_declarations(xml_text)
    # nosemgrep
    return _ET.fromstring(xml_text)  # nosec B314 — entity declarations refused above
