## 2025-05-24 - Unify XXE Protection With xml_safety.py

**Vulnerability:** Inconsistent, incomplete, and fragile XXE (XML External Entity) and Billion Laughs DoS protection across various files (`webdav.py`, `webdav_discovery.py`, `kodi_advancedsettings.py`, `nzb_manifest.py`). The standard library fallback in Python >= 3.3 does not have `.parser` attribute for `ET.XMLParser()`, resulting in `AttributeError` that was caught but silently allowed the parser to continue with entities enabled (failing open).
**Learning:** Python's C-implementation of ElementTree doesn't expose external entity resolution handlers. You cannot disable entities via `parser.parser.ExternalEntityRefHandler = lambda *_: False` because `parser` lacks a `.parser` attribute. `xml_safety.py` provides a centralized, robust text-scanning fallback for when `defusedxml` is not available.
**Prevention:** Always use `safe_fromstring` from `xml_safety.py` for parsing XML instead of ad-hoc parser customizations.
## 2025-05-24 - Fail-Closed Authentication Check

**Vulnerability:** Combining a precondition truthiness check with a negative comparison (e.g., `if expected_token and not compare(...)`) results in failing open when `expected_token` is missing or empty.
**Learning:** The loopback proxy endpoints `/prepare` and `/stream/<id>/fallbacks` bypass authentication entirely if the server's `prepare_token` isn't initialized or evaluates to a falsy value.
**Prevention:** Explicitly fail-closed by verifying the secret's presence first (e.g., `if not expected_token: return False`).
