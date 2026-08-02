## 2025-05-24 - Unify XXE Protection With xml_safety.py

**Vulnerability:** Inconsistent, incomplete, and fragile XXE (XML External Entity) and Billion Laughs DoS protection across various files (`webdav.py`, `webdav_discovery.py`, `kodi_advancedsettings.py`, `nzb_manifest.py`). The standard library fallback in Python >= 3.3 does not have `.parser` attribute for `ET.XMLParser()`, resulting in `AttributeError` that was caught but silently allowed the parser to continue with entities enabled (failing open).
**Learning:** Python's C-implementation of ElementTree doesn't expose external entity resolution handlers. You cannot disable entities via `parser.parser.ExternalEntityRefHandler = lambda *_: False` because `parser` lacks a `.parser` attribute. `xml_safety.py` provides a centralized, robust text-scanning fallback for when `defusedxml` is not available.
**Prevention:** Always use `safe_fromstring` from `xml_safety.py` for parsing XML instead of ad-hoc parser customizations.

## 2025-05-24 - Fix Fail-Open Authentication in Proxy Loopback

**Vulnerability:** Authorization bypass (fail-open) in loopback proxy authentication. The truthiness check `if expected_token and not compare(...)` bypassed the digest comparison and returned True when the expected token was unexpectedly empty/falsy.
**Learning:** Combining a truthy check and a negative condition directly can result in a fail-open authorization model when the primary required constraint (the secret) is missing.
**Prevention:** Fail-closed explicitly. Verify the presence of required secrets via a separate `if not expected_token: return False` condition before checking the provided credentials against the expected ones.
