## 2025-05-24 - Unify XXE Protection With xml_safety.py

**Vulnerability:** Inconsistent, incomplete, and fragile XXE (XML External Entity) and Billion Laughs DoS protection across various files (`webdav.py`, `webdav_discovery.py`, `kodi_advancedsettings.py`, `nzb_manifest.py`). The standard library fallback in Python >= 3.3 does not have `.parser` attribute for `ET.XMLParser()`, resulting in `AttributeError` that was caught but silently allowed the parser to continue with entities enabled (failing open).
**Learning:** Python's C-implementation of ElementTree doesn't expose external entity resolution handlers. You cannot disable entities via `parser.parser.ExternalEntityRefHandler = lambda *_: False` because `parser` lacks a `.parser` attribute. `xml_safety.py` provides a centralized, robust text-scanning fallback for when `defusedxml` is not available.
**Prevention:** Always use `safe_fromstring` from `xml_safety.py` for parsing XML instead of ad-hoc parser customizations.
## 2026-08-01 - Fail-Open Authentication Check
**Vulnerability:** The `_prepare_token_ok` method combined a precondition truthiness check (`expected_token`) with a negative comparison (`not hmac.compare_digest(...)`), causing it to fail-open and return `True` when the expected token was empty or missing.
**Learning:** Combining a truthiness check and a negative comparison (`if expected_token and not ...`) evaluates to falsy when the token is missing, bypassing the check entirely and allowing unauthorized access.
**Prevention:** Explicitly fail-closed by verifying the secret's presence first (`if not expected_token: return False`) before comparing.
