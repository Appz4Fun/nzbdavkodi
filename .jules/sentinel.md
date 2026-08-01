## 2025-05-24 - Unify XXE Protection With xml_safety.py

**Vulnerability:** Inconsistent, incomplete, and fragile XXE (XML External Entity) and Billion Laughs DoS protection across various files (`webdav.py`, `webdav_discovery.py`, `kodi_advancedsettings.py`, `nzb_manifest.py`). The standard library fallback in Python >= 3.3 does not have `.parser` attribute for `ET.XMLParser()`, resulting in `AttributeError` that was caught but silently allowed the parser to continue with entities enabled (failing open).
**Learning:** Python's C-implementation of ElementTree doesn't expose external entity resolution handlers. You cannot disable entities via `parser.parser.ExternalEntityRefHandler = lambda *_: False` because `parser` lacks a `.parser` attribute. `xml_safety.py` provides a centralized, robust text-scanning fallback for when `defusedxml` is not available.
**Prevention:** Always use `safe_fromstring` from `xml_safety.py` for parsing XML instead of ad-hoc parser customizations.
## 2025-05-24 - Fix Auth Bypass When Token is Empty

**Vulnerability:** Authentication bypass in the background proxy service's loopback verification (`_prepare_token_ok`). The original logic checked `if expected_token and not compare_digest(...)`. If `expected_token` was empty, the comparison was entirely skipped and the function implicitly returned `True`, granting access.
**Learning:** Combining a truthiness precondition with a negative check (`if token and not valid(...)`) is dangerous for authentication logic. It leads to a fail-open scenario when the required token is legitimately empty or missing from settings/configuration.
**Prevention:** Always use explicit, fail-closed logic for authentication. Check for the token's presence first, and return `False` immediately if it is missing, before proceeding to perform cryptographic comparisons.
