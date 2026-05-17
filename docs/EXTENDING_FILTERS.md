# Adding a New Filter: Step-by-Step Guide

This guide walks through adding a new filter criterion to the NZB-DAV Kodi addon. Adding a filter requires modifying exactly **5 locations** in the codebase. Missing any step causes silent failures where the filter is read but never applied, or applied but crashes on missing keys.

## Overview

The filter system flow:
1. **User configures filter** in Kodi settings UI (`resources/settings.xml`)
2. **Addon reads settings** when searching (`filter.py: _get_filter_settings()`)
3. **PTT parses NZB titles** to extract metadata (`filter.py: parse_title_metadata()`)
4. **Filter logic applies** to each result (`filter.py: matches_filters()`)
5. **Tests verify** the filter works correctly (`tests/test_filter.py`)

## Example: Exclude CAM Releases

We'll add a filter to exclude CAM quality releases (theater recordings). This is a boolean setting: when enabled, any result with "CAM" in the title is rejected.

### Step 1: Add UI Control (`resources/settings.xml`)

Add a boolean setting in the "Advanced Filters" category (around line 60-72):

```xml
<category label="30052">
    <setting label="30053" type="lsep" />
    <setting id="filter_release_group" label="30054" type="text" default="" visible="false" />
    <setting id="filter_exclude_release_group" label="30055" type="text" default="" visible="false" />
    <setting label="30054" type="action" action="RunPlugin(plugin://plugin.video.nzbdav/configure_preferred_groups)" option="close" />
    <setting label="30055" type="action" action="RunPlugin(plugin://plugin.video.nzbdav/configure_excluded_groups)" option="close" />
    <setting label="30056" type="lsep" />
    <setting id="filter_min_size" label="30057" type="number" default="0" />
    <setting id="filter_max_size" label="30058" type="number" default="0" />
    <setting label="30059" type="lsep" />
    <setting id="filter_exclude_keywords" label="30060" type="text" default="" />
    <setting id="filter_require_keywords" label="30061" type="text" default="" />
    <!-- ADD THIS LINE: -->
    <setting id="filter_exclude_cam" label="Exclude CAM releases" type="bool" default="true" />
</category>
```

**Notes:**
- `id` must be unique and follow the `filter_*` naming convention
- `label` can be plain text for internal settings, or use localization strings like `30XXX`
- `type="bool"` for checkboxes, `"text"` for strings, `"number"` for integers
- `default="true"` means the filter is enabled by default

### Step 2: Read Setting (`filter.py: _get_filter_settings()`)

Add code to read the new setting (around line 241-333):

```python
def _get_filter_settings():
    """Read filter settings from Kodi addon config."""
    import xbmcaddon

    addon = xbmcaddon.Addon()

    resolutions = _collect_enabled(
        addon,
        [
            ("filter_2160p", "2160p"),
            ("filter_1080p", "1080p"),
            ("filter_720p", "720p"),
            ("filter_480p", "480p"),
        ],
    )

    # ... existing code for hdr, audio, codecs, languages ...

    return {
        "resolutions": resolutions,
        "hdr": hdr,
        "audio": audio,
        "codecs": codecs,
        "languages": languages,
        "exclude_keywords": [
            k.lower() for k in _csv_setting(addon, "filter_exclude_keywords")
        ],
        "require_keywords": [
            k.lower() for k in _csv_setting(addon, "filter_require_keywords")
        ],
        "release_group": [
            g.lower() for g in _csv_setting(addon, "filter_release_group")
        ],
        "exclude_release_group": [
            g.lower() for g in _csv_setting(addon, "filter_exclude_release_group")
        ],
        "min_size": _int_setting(addon, "filter_min_size", 0),
        "max_size": _int_setting(addon, "filter_max_size", 0),
        # ADD THIS LINE:
        "exclude_cam": addon.getSetting("filter_exclude_cam").lower() == "true",
        "sort_order": _int_setting(addon, "sort_order", 0),
        "max_results": _int_setting(addon, "max_results", 25),
    }
```

**Notes:**
- For boolean settings: use `addon.getSetting("setting_id").lower() == "true"`
- For CSV settings: use `_csv_setting(addon, "setting_id")`
- For integer settings: use `_int_setting(addon, "setting_id", default_value)`
- Store the value in the returned dict with a descriptive key

### Step 3: Parse Value from NZB Title (`filter.py: parse_title_metadata()`)

**For this example, we don't need to modify `parse_title_metadata()`** because we're checking the title directly in `matches_filters()`, not parsing a specific field.

However, if you were adding a filter for something PTT extracts (like `language`, `edition`, `year`), you would ensure that field is included in the returned dict (around line 336-397):

```python
def parse_title_metadata(title):
    """Parse a scene title and return normalized metadata dict."""
    try:
        from resources.lib.ptt import parse_title

        parsed = parse_title(title)
    except Exception as e:
        xbmc.log(
            "NZB-DAV: PTT parse failed for '{}': {}".format(title, e), xbmc.LOGERROR
        )
        parsed = _fallback_parse(title)

    # ... existing parsing code ...

    return {
        "resolution": resolution,
        "hdr": hdr_list,
        "audio": audio_list,
        "codec": codec,
        "languages": raw_langs,
        "group": group,
        "quality": quality,  # <-- PTT extracts quality, which includes "CAM"
        "edition": edition,
        "channels": channels,
        "year": year,
        "upscaled": upscaled,
    }
```

**When to modify `parse_title_metadata()`:**
- Adding a filter for a field PTT already extracts (`year`, `edition`, `quality`)
- Adding a filter for a new field that requires custom regex parsing
- Normalizing values (e.g., mapping "4K" → "2160p")

### Step 4: Apply Filter Logic (`filter.py: matches_filters()`)

Add the filter check in `matches_filters()` (around line 400-449):

```python
def matches_filters(result, meta, settings):
    """Check if a result passes all filter criteria."""
    title_lower = result["title"].lower()

    if settings["resolutions"] and meta["resolution"]:
        if meta["resolution"] not in settings["resolutions"]:
            return False

    if settings["hdr"] and meta["hdr"]:
        if not any(h in settings["hdr"] for h in meta["hdr"]):
            return False
    if settings["hdr"] and not meta["hdr"] and "SDR" not in settings["hdr"]:
        return False

    # ... existing filter checks ...

    for kw in settings["exclude_keywords"]:
        if kw in title_lower:
            return False

    for kw in settings["require_keywords"]:
        if kw not in title_lower:
            return False

    # ADD THIS BLOCK:
    if settings["exclude_cam"]:
        # Check both the parsed quality field and the raw title
        if meta.get("quality", "").upper() == "CAM" or "cam" in title_lower:
            return False

    if meta["group"] and meta["group"].lower() in [
        g.lower() for g in settings["exclude_release_group"]
    ]:
        return False

    # ... size checks ...

    return True
```

**Notes:**
- Return `False` to **reject** the result
- Return `True` to **accept** the result
- Check both parsed metadata (`meta`) and raw title (`result["title"]`)
- Use `.lower()` for case-insensitive matching
- Add your check **before** the final `return True`

**Common patterns:**

```python
# Exclude if keyword present in title
if settings["exclude_bad_keyword"]:
    if "bad" in title_lower:
        return False

# Require minimum year
if settings["min_year"] > 0:
    if meta.get("year", 0) < settings["min_year"]:
        return False

# Match any value in a list
if settings["allowed_editions"]:
    if not any(ed in meta.get("edition", "") for ed in settings["allowed_editions"]):
        return False
```

### Step 5: Add Test Cases (`tests/test_filter.py`)

Add test cases to verify the filter works (append to the end of the file):

```python
@patch("resources.lib.filter._get_filter_settings")
def test_filter_exclude_cam(mock_settings):
    """CAM releases should be filtered out when exclude_cam is enabled."""
    mock_settings.return_value = {
        "resolutions": ["2160p", "1080p", "720p", "480p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "exclude_cam": True,  # New setting enabled
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Movie.2024.CAM.x264-GRP"),
        _make_result("Movie.2024.1080p.BluRay.x264-GRP"),
        _make_result("Movie.2024.HDCAM.x264-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1, "CAM releases should be filtered out"
    assert "BluRay" in filtered[0]["title"], "Only BluRay release should remain"


@patch("resources.lib.filter._get_filter_settings")
def test_filter_exclude_cam_disabled(mock_settings):
    """CAM releases should pass when exclude_cam is disabled."""
    mock_settings.return_value = {
        "resolutions": ["2160p", "1080p", "720p", "480p"],
        "hdr": [],
        "audio": [],
        "codecs": [],
        "languages": [],
        "exclude_keywords": [],
        "require_keywords": [],
        "release_group": [],
        "exclude_release_group": [],
        "min_size": 0,
        "max_size": 0,
        "exclude_cam": False,  # New setting disabled
        "sort_order": 0,
        "max_results": 25,
    }
    results = [
        _make_result("Movie.2024.CAM.x264-GRP"),
        _make_result("Movie.2024.1080p.BluRay.x264-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 2, "CAM releases should pass when filter is disabled"
```

**Test patterns:**

```python
# Test basic functionality
@patch("resources.lib.filter._get_filter_settings")
def test_filter_my_feature_enabled(mock_settings):
    mock_settings.return_value = {
        # ... all settings ...
        "my_new_filter": True,
    }
    results = [
        _make_result("Should.Pass-GRP"),
        _make_result("Should.Fail-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1
    assert "Should.Pass" in filtered[0]["title"]

# Test edge cases
@patch("resources.lib.filter._get_filter_settings")
def test_filter_my_feature_empty_metadata(mock_settings):
    """Filter should handle missing metadata gracefully."""
    mock_settings.return_value = {
        # ...
        "my_new_filter": True,
    }
    results = [_make_result("No.Metadata.At.All-GRP")]
    filtered, _ = filter_results(results)
    # Should not crash, verify expected behavior
    assert len(filtered) >= 0
```

## Running Tests

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run only filter tests
python3 -m pytest tests/test_filter.py -v

# Run a specific test
python3 -m pytest tests/test_filter.py::test_filter_exclude_cam -v
```

## Troubleshooting

### Filter Not Applied

**Symptom:** Setting is visible in Kodi UI, but results are not filtered.

**Causes:**
1. Forgot to add setting to `_get_filter_settings()` return dict
2. Forgot to add filter logic in `matches_filters()`
3. Typo in setting ID (e.g., `filter_exclude_cam` vs `filter_cam_exclude`)

**Debug:** Add logging in `matches_filters()`:

```python
if settings.get("exclude_cam"):
    xbmc.log("NZB-DAV: Checking CAM filter for '{}'".format(result["title"]), xbmc.LOGDEBUG)
    if "cam" in title_lower:
        xbmc.log("NZB-DAV: Rejected CAM release", xbmc.LOGDEBUG)
        return False
```

### Filter Crashes

**Symptom:** Addon stops working, Kodi log shows KeyError or TypeError.

**Causes:**
1. Accessing `settings["my_filter"]` without adding it to `_get_filter_settings()`
2. Accessing `meta["new_field"]` without adding it to `parse_title_metadata()`
3. Not handling `None` or empty values

**Fix:** Use `.get()` with defaults:

```python
# Safe access with default
if settings.get("my_filter", False):
    year = meta.get("year", 0)
    if year > 0:
        # ... filter logic ...
```

### Tests Fail

**Symptom:** `pytest` reports AssertionError or test failures.

**Causes:**
1. Mock settings dict missing the new key
2. Test expectations don't match actual filter behavior
3. Existing tests broke due to new filter affecting results

**Fix:** Update all mock settings dicts:

```python
# Add to helper function at top of test file
def _all_pass_settings():
    """Settings that accept everything."""
    return {
        "resolutions": ["2160p", "1080p", "720p", "480p"],
        # ... all existing keys ...
        "exclude_cam": False,  # ADD NEW KEY with permissive default
        "sort_order": 0,
        "max_results": 25,
    }
```

### Setting Not Visible in Kodi

**Symptom:** New setting doesn't appear in addon settings.

**Causes:**
1. XML syntax error in `resources/settings.xml`
2. Forgot to rebuild addon zip
3. Kodi cached old settings

**Fix:**
1. Validate XML syntax
2. Rebuild: `python3 scripts/build_zip.py`
3. Reinstall addon in Kodi
4. Force refresh: Addons → My Add-ons → NZB-DAV → Configure

## Advanced Examples

### Multi-Select Filter (e.g., Allowed Editions)

```xml
<!-- settings.xml -->
<setting id="filter_allowed_editions" label="Allowed Editions (CSV)" type="text" default="Theatrical,Extended,Director's Cut" />
```

```python
# filter.py: _get_filter_settings()
"allowed_editions": _csv_setting(addon, "filter_allowed_editions"),
```

```python
# filter.py: matches_filters()
if settings["allowed_editions"]:
    edition = meta.get("edition", "").lower()
    allowed_lower = [e.lower() for e in settings["allowed_editions"]]
    if edition and edition not in allowed_lower:
        return False
```

### Numeric Range Filter (e.g., Minimum Year)

```xml
<!-- settings.xml -->
<setting id="filter_min_year" label="Minimum Release Year" type="number" default="0" />
```

```python
# filter.py: _get_filter_settings()
"min_year": _int_setting(addon, "filter_min_year", 0),
```

```python
# filter.py: matches_filters()
if settings["min_year"] > 0:
    year = meta.get("year", 0)
    if year > 0 and year < settings["min_year"]:
        return False
```

### Require All Values in List (e.g., Must Have HDR + Atmos)

```python
# filter.py: matches_filters()
if settings.get("require_hdr_and_atmos"):
    has_hdr = any(h in ["HDR10", "HDR10+", "Dolby Vision"] for h in meta.get("hdr", []))
    has_atmos = "Atmos" in meta.get("audio", [])
    if not (has_hdr and has_atmos):
        return False
```

## Checklist: Did You Remember to...?

- [ ] Add setting to `resources/settings.xml` with unique ID
- [ ] Read setting in `filter.py: _get_filter_settings()` and add to return dict
- [ ] Parse required fields in `filter.py: parse_title_metadata()` (if needed)
- [ ] Implement filter logic in `filter.py: matches_filters()`
- [ ] Add test cases in `tests/test_filter.py` (enabled, disabled, edge cases)
- [ ] Run `python3 -m pytest tests/test_filter.py -v` and verify all pass
- [ ] Test in Kodi UI to ensure setting appears and works

## Further Reading

- **PTT Fields:** See `plugin.video.nzbdav/resources/lib/ptt/` for all parsed fields
- **Settings Schema:** Kodi wiki for `settings.xml` format
- **Filter Flow:** `filter.py` docstrings explain the pipeline
- **Test Patterns:** `tests/test_filter.py` has examples for every filter type
