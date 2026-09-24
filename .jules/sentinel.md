## 2025-05-24 - Unify XXE Protection With xml_safety.py

**Vulnerability:** Inconsistent, incomplete, and fragile XXE (XML External Entity) and Billion Laughs DoS protection across various files (`webdav.py`, `webdav_discovery.py`, `kodi_advancedsettings.py`, `nzb_manifest.py`). The standard library fallback in Python >= 3.3 does not have `.parser` attribute for `ET.XMLParser()`, resulting in `AttributeError` that was caught but silently allowed the parser to continue with entities enabled (failing open).
**Learning:** Python's C-implementation of ElementTree doesn't expose external entity resolution handlers. You cannot disable entities via `parser.parser.ExternalEntityRefHandler = lambda *_: False` because `parser` lacks a `.parser` attribute. `xml_safety.py` provides a centralized, robust text-scanning fallback for when `defusedxml` is not available.
**Prevention:** Always use `safe_fromstring` from `xml_safety.py` for parsing XML instead of ad-hoc parser customizations.

## 2025-05-25 - Fix Authentication Bypass in Stream Proxy Dispatch

**Vulnerability:** In `_prepare_token_ok` of `stream_proxy_handler_dispatch.py`, if the loopback server was missing the required `prepare_token` secret (evaluating to falsy), the token check short-circuited (`if expected_token and not ...`) instead of failing closed, bypassing authentication entirely for critical `/prepare` and `/fallbacks` endpoints.
**Learning:** Combining a truthiness precondition check with a negative comparison (e.g., `if expected_token and not compare(...)`) defaults to allowing the action if the required secret is empty, violating fail-safe defaults.
**Prevention:** Always verify the presence of the required secret first (e.g., `if not expected_token: return False`) and ensure the failure state handles missing secrets securely by failing closed.
