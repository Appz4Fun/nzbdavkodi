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

try:
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


def _xml_bytes_to_text(payload):
    """Decode XML bytes to logical text for the entity scan.

    Honours a byte-order mark and the ``<?xml encoding=...?>`` declaration so
    UTF-16/UTF-32 payloads can't hide an entity declaration from the scan.
    Endianness is resolved explicitly — decoding with the wrong one would mangle
    the ASCII markup tokens we look for. ``errors="replace"`` keeps this
    best-effort; the real parse still runs on the original bytes.
    """
    if payload[:4] in (b"\x00\x00\xfe\xff", b"\x00\x00\x00<"):
        return payload.decode("utf-32-be", "replace")
    if payload[:4] in (b"\xff\xfe\x00\x00", b"<\x00\x00\x00"):
        return payload.decode("utf-32-le", "replace")
    if payload[:2] in (b"\xfe\xff", b"\x00<"):
        return payload.decode("utf-16-be", "replace")
    if payload[:2] in (b"\xff\xfe", b"<\x00"):
        return payload.decode("utf-16-le", "replace")
    if payload[:3] == b"\xef\xbb\xbf":
        return payload.decode("utf-8-sig", "replace")
    head = payload[:200].decode("ascii", "replace")
    match = _XML_DECL_ENCODING_RE.search(head)
    encoding = match.group(1) if match else "utf-8"
    try:
        return payload.decode(encoding, "replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", "replace")


def _reject_entity_declarations(xml_text):
    text = xml_text
    if isinstance(text, (bytes, bytearray)):
        text = _xml_bytes_to_text(bytes(text))
    if _DOCTYPE_RE.search(text) and _ENTITY_DECL_RE.search(text):
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
