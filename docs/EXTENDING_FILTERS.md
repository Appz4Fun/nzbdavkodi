# Adding a New Filter: Step-by-Step

Adding a filter touches several spots. Use this checklist so the setting is visible, read, parsed, enforced, and tested:

1) `plugin.video.nzbdav/resources/settings.xml` — add the UI control (and a label in `plugin.video.nzbdav/resources/language/.../strings.po`).
2) `plugin.video.nzbdav/resources/lib/filter.py:_get_filter_settings()` — read the new setting and normalize it into the settings dict.
3) `plugin.video.nzbdav/resources/lib/filter.py:parse_title_metadata()` (and `_fallback_parse()` if needed) — extract the metadata needed by the filter.
4) `plugin.video.nzbdav/resources/lib/filter.py:matches_filters()` — apply the filter logic and return `False` when it should be excluded.
5) `tests/test_filter.py` — add tests that cover the new setting, metadata parsing, and the acceptance/rejection path.

Tests in this repo mock the Kodi `xbmc*` modules, so lazy imports are not required just to keep tests working; follow the existing import style in the module you are editing, and only keep imports lazy where that module specifically depends on it.

## Worked Example: Exclude CAM Releases

Goal: add a toggle that drops CAM/TS results.

### 1) Add the setting

In `plugin.video.nzbdav/resources/settings.xml`, create a boolean in the Quality Filters category.  Use the next unused `#30xxx` integer as the label ID (check the highest `msgctxt` value already in `strings.po` to find it):

```xml
<setting id="filter_exclude_cam" label="30120" type="bool" default="true" />
```

Add the matching label in `plugin.video.nzbdav/resources/language/resource.language.en_gb/strings.po` (and other locales as needed):

```po
msgctxt "#30120"
msgid "Exclude CAM / TS"
msgstr "Exclude CAM / TS"
```

Replace `30120` with the actual next available integer in your checkout (the example above assumes `#30119` is the current highest ID).

### 2) Read it in `_get_filter_settings()`

Normalize the boolean so downstream code gets a Python `bool`:

```python
exclude_cam = addon.getSetting("filter_exclude_cam").lower() == "true"
return {
    # ...existing keys...
    "exclude_cam": exclude_cam,
}
```

Keep the return shape consistent — every filter-specific key should always exist.

### 3) Parse metadata in `parse_title_metadata()`

Expose a field that signals CAM quality, using both PTT output and a fallback pattern:

```python
import re

quality = parsed.get("quality", "") or ""
is_cam = quality.upper() in ("CAM", "TS")

if not is_cam and re.search(r"\b(?:CAM|TS)\b", title, re.IGNORECASE):
    is_cam = True  # fallback for loose titles

return {
    # ...existing fields...
    "quality": quality,
    "is_cam": is_cam,
}
```

If `_fallback_parse()` supplies metadata for this, update it too so tests pass even when PTT misses the pattern.

### 4) Enforce it in `matches_filters()`

Short-circuit early when the new setting is on:

```python
if settings["exclude_cam"] and meta.get("is_cam"):
    return False
```

Add any complementary logic here (e.g., allowing TS but not CAM if you split them).

### 5) Test it in `tests/test_filter.py`

Add two tests:

- A parsing test that asserts `parse_title_metadata("Movie.2024.CAM.x264-GRP")["is_cam"] is True`.
- A filtering test that patches `_get_filter_settings` to set `exclude_cam=True` and verifies a CAM result is removed while a normal WEB-DL passes.

Run the suite to confirm everything works:

```bash
just test  # or: python -m pytest tests/test_filter.py
```

When all five pieces line up, the filter appears in settings, metadata contains the signal, filtering enforces it, and the behavior is covered by tests.
