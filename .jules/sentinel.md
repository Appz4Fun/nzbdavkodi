## 2025-05-24 - Unify XXE Protection With xml_safety.py

**Vulnerability:** Inconsistent, incomplete, and fragile XXE (XML External Entity) and Billion Laughs DoS protection across various files (`webdav.py`, `webdav_discovery.py`, `kodi_advancedsettings.py`, `nzb_manifest.py`). The standard library fallback in Python >= 3.3 does not have `.parser` attribute for `ET.XMLParser()`, resulting in `AttributeError` that was caught but silently allowed the parser to continue with entities enabled (failing open).
**Learning:** Python's C-implementation of ElementTree doesn't expose external entity resolution handlers. You cannot disable entities via `parser.parser.ExternalEntityRefHandler = lambda *_: False` because `parser` lacks a `.parser` attribute. `xml_safety.py` provides a centralized, robust text-scanning fallback for when `defusedxml` is not available.
**Prevention:** Always use `safe_fromstring` from `xml_safety.py` for parsing XML instead of ad-hoc parser customizations.
## 2025-05-24 - Fix Auth Bypass when Loopback Proxy Secret is Empty

**Vulnerability:** The loopback proxy authentication check (`_prepare_token_ok`) used the logic `if expected_token and not ...compare_digest(...)`. This caused the check to short-circuit and return `True` (authorized) if the expected secret token was empty or uninitialized.
**Learning:** Security checks that use `and not` with a precondition (like `expected_token`) will fail-open if the precondition is false.
**Prevention:** Explicitly return `False` if required authentication secrets are missing or empty before attempting comparison, ensuring a fail-secure design.
