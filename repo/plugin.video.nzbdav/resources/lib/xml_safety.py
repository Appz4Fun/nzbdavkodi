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
import xml.etree.ElementTree as _stdlib_et

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

# Matches an ``<!ENTITY ...>`` markup declaration. Indexer/WebDAV feeds never
# legitimately declare their own XML entities, so any declaration is treated as
# hostile (XXE / billion-laughs) and refused before parsing.
_ENTITY_DECL_RE = re.compile(rb"<!ENTITY\b")


def _reject_entity_declarations(payload_bytes):
    if _ENTITY_DECL_RE.search(payload_bytes):
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
    payload = xml_text
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "ignore")
    _reject_entity_declarations(payload)
    # nosemgrep
    return _ET.fromstring(xml_text)  # nosec B314 — entity declarations refused above
