# Adding a New Filter Criterion: Step-by-Step Guide

Adding a filter usually involves **five core code touchpoints** in the main flow below.
Miss one of these and the filter may silently do nothing (for example, a setting is read
but never applied) or fail at runtime (for example, logic expects a metadata key that was
never populated). Depending on the kind of filter, you may also need to update UI/i18n
strings such as `resources/language/resource.language.en_gb/strings.po`, and in some
cases `_fallback_parse()` when introducing new metadata fields.

## Architecture overview

```
settings.xml             ← Kodi UI control, stores user's choice
      ↓
_get_filter_settings()   ← reads setting into a Python dict
      ↓
parse_title_metadata()   ← extracts the matching attribute from the NZB title
      ↓
matches_filters()        ← applies the filter logic
      ↓
tests/test_filter.py     ← verifies all of the above
```

---

## Example: "Exclude CAM releases"

A **CAM** release is a cinema-recorded copy with poor quality. We'll add a dedicated
boolean toggle so users can ban all CAM results with a single checkbox instead of
manually typing `cam` into the exclude-keywords field.

### Step 1 — `resources/settings.xml`

Add a `<setting>` element inside the appropriate `<category>` block. Boolean filters
that exclude a specific type belong near the other quality/source controls in the
category that already contains the existing resolution/HDR/audio/codec-style filter
settings.

```xml
<setting id="filter_exclude_cam" label="30120" type="bool" default="false" />
```

> **Tip:** `label="30120"` references a string ID from
> `plugin.video.nzbdav/resources/language/resource.language.en_gb/strings.po`.
> Add the matching entry there so the Kodi UI shows a human-readable label:
>
> ```po
> msgctxt "#30120"
> msgid "Exclude CAM / Telecine"
> msgstr ""
> ```
>
> If you skip the `.po` entry Kodi will display the raw number `30120`. The filter
> still works, but the UI looks broken.

---

### Step 2 — `filter.py: _get_filter_settings()`

`_get_filter_settings()` reads every setting from the Kodi addon config and returns a
single `dict` used throughout the filter pipeline. Add a new key for your setting.

**Boolean setting** (checkbox) — use the inline comparison pattern used throughout the
function:

```python
def _get_filter_settings():
    import xbmcaddon
    addon = xbmcaddon.Addon()

    # ... existing code ...

    return {
        # ... existing keys ...
        "exclude_cam": addon.getSetting("filter_exclude_cam").lower() == "true",
    }
```

**Other setting types** use the helpers already in the file:

| Setting type | Helper | Example |
|---|---|---|
| Checkbox (bool) | inline `== "true"` | `addon.getSetting("filter_exclude_cam").lower() == "true"` |
| Integer / number | `_int_setting(addon, key, default)` | `_int_setting(addon, "filter_min_year", 0)` |
| Comma-separated list | `_csv_setting(addon, key)` | `_csv_setting(addon, "filter_require_keywords")` |
| Multi-checkbox group | `_collect_enabled(addon, pairs)` | see `resolutions` block |

---

### Step 3 — `filter.py: parse_title_metadata()`

`parse_title_metadata()` runs PTT on the NZB title and returns a `dict` with
normalised attributes. Your filter logic in Step 4 will read from this dict.

For CAM releases, PTT stores the source/quality in the `"quality"` field (e.g.
`"CAM"`, `"Telecine"`). That field is already extracted, so **no change is needed**
for this particular example.

However, if you are adding a filter for an attribute PTT does **not** already return,
you must add it here. For example, if PTT returned a `"cam"` boolean:

```python
# Inside parse_title_metadata(), in the return dict:
return {
    "resolution": resolution,
    "hdr": hdr_list,
    "audio": audio_list,
    "codec": codec,
    "languages": raw_langs,
    "group": group,
    "quality": quality,
    "edition": edition,
    "channels": channels,
    "year": year,
    "upscaled": upscaled,
    # New field — only add if PTT/fallback doesn't provide it already:
    "is_cam": "cam" in quality.lower() or "telecine" in quality.lower(),
}
```

> **Always update `_fallback_parse()` as well** (at the bottom of `filter.py`)
> when adding a new metadata attribute. The fallback runs when PTT fails or
> returns empty, so it needs to populate that value for titles that rely on the
> fallback path. If `matches_filters()` raises `KeyError`, that usually means the
> key was not added to the metadata dict returned by `parse_title_metadata()`
> (or an expected setting is missing), not simply that `_fallback_parse()` omitted it.

---

### Step 4 — `filter.py: matches_filters()`

`matches_filters(result, meta, settings)` returns `True` if the result should be
kept, `False` to discard it. Add your guard early in the function so cheap checks
run before expensive ones.

```python
def matches_filters(result, meta, settings):
    title_lower = result["title"].lower()

    # ... existing checks ...

    # Exclude CAM / Telecine releases when the setting is on
    if settings["exclude_cam"]:
        quality_lower = meta.get("quality", "").lower()
        if "cam" in quality_lower or "telecine" in quality_lower:
            return False
        # Also catch titles that PTT missed but contain the tag literally
        if "cam" in title_lower and not any(
            skip in title_lower for skip in ("camera", "camcorder")
        ):
            return False

    return True
```

> **When filters are "pass-through" by default** (empty list / zero / False),
> make sure your guard only activates when the setting is actually set. A guard that
> fires on the zero/empty/False default will silently drop results for users who
> have never touched the setting.

---

### Step 5 — `tests/test_filter.py`

Add at least three test cases: *setting off (no filtering)*, *setting on + matching
title (should be excluded)*, *setting on + non-matching title (should be kept)*.

```python
# --- filter_exclude_cam tests ---


def _cam_settings(exclude_cam=False):
    """Base settings with the exclude_cam flag configurable."""
    return {
        "resolutions": [],
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
        "sort_order": 0,
        "max_results": 25,
        "exclude_cam": exclude_cam,
    }


def test_exclude_cam_off_keeps_cam_title():
    """When exclude_cam is False, CAM releases pass through."""
    result = _make_result("The.Matrix.1999.CAM.x264-GRP")
    meta = parse_title_metadata(result["title"])
    assert matches_filters(result, meta, _cam_settings(exclude_cam=False))


def test_exclude_cam_on_removes_cam_title():
    """When exclude_cam is True, CAM releases are filtered out."""
    result = _make_result("The.Matrix.1999.CAM.x264-GRP")
    meta = parse_title_metadata(result["title"])
    assert not matches_filters(result, meta, _cam_settings(exclude_cam=True))


def test_exclude_cam_on_keeps_bluray_title():
    """When exclude_cam is True, non-CAM releases are not affected."""
    result = _make_result("The.Matrix.1999.1080p.BluRay.x264-GRP")
    meta = parse_title_metadata(result["title"])
    assert matches_filters(result, meta, _cam_settings(exclude_cam=True))


@patch("resources.lib.filter._get_filter_settings")
def test_filter_pipeline_exclude_cam(mock_settings):
    """End-to-end: filter_results() with exclude_cam enabled."""
    mock_settings.return_value = _cam_settings(exclude_cam=True)
    results = [
        _make_result("The.Matrix.1999.CAM.x264-GRP"),
        _make_result("The.Matrix.1999.1080p.BluRay.x264-GRP"),
    ]
    filtered, _ = filter_results(results)
    assert len(filtered) == 1
    assert "BluRay" in filtered[0]["title"]
```

---

## Complete checklist

Before opening a pull request, verify every item:

- [ ] `resources/settings.xml` — new `<setting id="filter_…">` element added in the
  correct category
- [ ] `resources/language/resource.language.en_gb/strings.po` — matching `msgctxt`
  entry so the label renders correctly in Kodi
- [ ] `filter.py: _get_filter_settings()` — new key added to the returned `dict`,
  using the appropriate helper (`_int_setting`, `_csv_setting`, `_collect_enabled`,
  or inline `== "true"`)
- [ ] `filter.py: parse_title_metadata()` — new metadata field added to the `return`
  dict if PTT does not already expose the value you need
- [ ] `filter.py: _fallback_parse()` — same new field added with a sensible default
  so the key always exists
- [ ] `filter.py: matches_filters()` — guard added that reads from `settings[…]` and
  `meta[…]`; default value must be "pass-through" (no filtering)
- [ ] `tests/test_filter.py` — at least: *off (no filter)*, *on + match (excluded)*,
  *on + no match (kept)*, *end-to-end pipeline*
- [ ] `just test` passes
- [ ] `just lint` passes

---

## Common pitfalls

| Symptom | Likely cause |
|---|---|
| Setting is ticked but results are not filtered | Key added to `settings.xml` and `_get_filter_settings()` but the guard in `matches_filters()` was not added |
| `KeyError` crash when filtering certain titles | New metadata field added to `parse_title_metadata()` but not to `_fallback_parse()` |
| `KeyError` crash in `matches_filters()` | New key added to `_get_filter_settings()` but the name used in `matches_filters()` is spelled differently |
| Kodi settings page shows a raw number instead of a label | `strings.po` entry for the new label ID is missing |
| Filter is always on even when the user turns it off | Default value check is inverted (e.g. `!= "false"` instead of `== "true"`) |
| Every result is excluded when filter is off | Guard fires on the zero/empty/False default instead of requiring the setting to be explicitly enabled |
