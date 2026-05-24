# Setup Wizard Centered Fields Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Center the setup wizard field list and refresh row styling to match the redesigned results selection language without changing wizard workflow.

**Architecture:** Keep the change mostly in the Kodi XML skin. Add one deterministic `ListItem.Property(helper)` value from setup wizard row metadata so each row can show a secondary line. Preserve all existing control IDs, page order, and activation behavior.

**Tech Stack:** Kodi `WindowXMLDialog` XML skin, Python 3.8-compatible addon runtime code, pytest, `xml.etree.ElementTree`.

---

## File Structure

- Modify: `repo/plugin.video.nzbdav/resources/skins/Default/1080i/setup-wizard.xml`
  - Owns visual layout, centered content column, row geometry, focus colors, accent bar, and footer placement.
- Modify: `repo/plugin.video.nzbdav/resources/lib/setup_wizard.py`
  - Adds deterministic helper text for each list item through `ListItem.Property(helper)`.
- Modify: `tests/test_repository_best_practices.py`
  - Adds XML regression tests for centered list geometry, focused accent bar, row label/helper/value fields, and no rounded texture dependency.
- Modify: `tests/test_setup_wizard.py`
  - Adds runtime regression test that populated rows expose helper text without changing existing value behavior.

---

### Task 1: Pin Skin Layout Expectations

**Files:**
- Modify: `tests/test_repository_best_practices.py`

- [ ] **Step 1: Add XML helpers and failing layout tests**

Add the following helper functions near the existing constants:

```python
def _setup_wizard_skin_root():
    skin_xml = (
        ADDON_DIR / "resources" / "skins" / "Default" / "1080i" / "setup-wizard.xml"
    )
    return ET.parse(skin_xml).getroot()


def _int_text(element, child_name):
    return int(element.findtext(child_name))
```

Add these tests after `test_setup_wizard_xml_skin_exists_with_expected_controls`:

```python
def test_setup_wizard_field_list_is_centered_single_column_layout():
    root = _setup_wizard_skin_root()
    field_list = root.find(".//control[@id='50']")

    assert field_list is not None
    left = _int_text(field_list, "left")
    width = _int_text(field_list, "width")
    center = left + (width / 2)

    assert 900 <= center <= 1020
    assert left < 700
    assert width >= 760
    assert _int_text(field_list, "top") > 250


def test_setup_wizard_rows_have_label_helper_value_columns():
    root = _setup_wizard_skin_root()
    item_layout = root.find(".//control[@id='50']/itemlayout")
    focused_layout = root.find(".//control[@id='50']/focusedlayout")

    assert item_layout is not None
    assert focused_layout is not None
    for layout in (item_layout, focused_layout):
        labels = layout.findall("control[@type='label']")
        label_infos = [label.findtext("label") for label in labels]

        assert "$INFO[ListItem.Label]" in label_infos
        assert "$INFO[ListItem.Property(helper)]" in label_infos
        assert "$INFO[ListItem.Property(value)]" in label_infos


def test_setup_wizard_focused_row_uses_results_style_left_accent():
    root = _setup_wizard_skin_root()
    focused_layout = root.find(".//control[@id='50']/focusedlayout")

    assert focused_layout is not None
    accent = None
    for image in focused_layout.findall("control[@type='image']"):
        if image.findtext("colordiffuse") == "FF6BB6FF":
            accent = image
            break

    assert accent is not None
    assert _int_text(accent, "left") == 0
    assert _int_text(accent, "width") == 6


def test_setup_wizard_skin_uses_xml_drawn_square_rows_only():
    root = _setup_wizard_skin_root()
    textures = [
        texture.text or ""
        for texture in root.findall(".//texture")
        if texture.text is not None
    ]

    assert textures
    assert set(textures) == {"white.png"}
    assert not any("round" in texture.lower() for texture in textures)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_repository_best_practices.py -k "setup_wizard_field_list_is_centered or setup_wizard_rows_have_label_helper_value_columns or setup_wizard_focused_row_uses_results_style_left_accent or setup_wizard_skin_uses_xml_drawn_square_rows_only" -v`

Expected: At least the centered layout, helper property, and accent tests fail against the old right-shifted skin.

---

### Task 2: Pin Runtime Helper Text

**Files:**
- Modify: `tests/test_setup_wizard.py`
- Modify: `repo/plugin.video.nzbdav/resources/lib/setup_wizard.py`

- [ ] **Step 1: Add failing helper-text population test**

Add this test after `test_connection_pages_have_test_actions`:

```python
def test_populated_rows_include_helper_text_property():
    addon = _addon_with_settings({"nzbdav_url": "http://nzbdav.local"})
    dialog = _wizard_dialog(addon)
    dialog.page_index = 1

    dialog._render_page()

    list_control = dialog.getControl(setup_wizard.LIST_ID)
    first_item = list_control.addItems.call_args.args[0][0]

    first_item.setProperty.assert_any_call("value", "http://nzbdav.local")
    first_item.setProperty.assert_any_call("helper", "Connection setting")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_setup_wizard.py::test_populated_rows_include_helper_text_property -v`

Expected: FAIL because `helper` is not set on the `ListItem`.

- [ ] **Step 3: Implement helper text**

In `_populate_rows`, after setting `value`, add:

```python
li.setProperty("helper", self._row_helper(row))
```

Add this method near `_row_value`:

```python
    def _row_helper(self, row):
        kind = row["kind"]
        if kind == "bool":
            return "Toggle filter"
        if kind == "provider":
            return "Choose search service"
        if kind == "text":
            return "Connection setting"
        return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_setup_wizard.py::test_populated_rows_include_helper_text_property -v`

Expected: PASS.

---

### Task 3: Implement Centered XML Skin

**Files:**
- Modify: `repo/plugin.video.nzbdav/resources/skins/Default/1080i/setup-wizard.xml`

- [ ] **Step 1: Update layout and row styling**

Change non-welcome body copy to a centered column above the list. Set the field list to a centered column, for example `left=500`, `top=330`, `width=920`, `height=500`, with row layouts `width=920` and `height=76`.

Use row colors from the results redesign:

```xml
<colordiffuse>FF11141A</colordiffuse>
<colordiffuse>FF1B2C44</colordiffuse>
<colordiffuse>FF6BB6FF</colordiffuse>
```

Each row layout must include:

```xml
<label>$INFO[ListItem.Label]</label>
<label>$INFO[ListItem.Property(helper)]</label>
<label>$INFO[ListItem.Property(value)]</label>
```

The focused layout must include a 6px left accent image:

```xml
<control type="image">
  <left>0</left><top>0</top><width>6</width><height>68</height>
  <texture>white.png</texture>
  <colordiffuse>FF6BB6FF</colordiffuse>
</control>
```

Move footer buttons into a centered group aligned with the content column while preserving IDs and navigation tags.

- [ ] **Step 2: Run focused XML tests**

Run: `pytest tests/test_repository_best_practices.py -k "setup_wizard" -v`

Expected: PASS.

---

### Task 4: Verify and Commit

**Files:**
- Modified files from prior tasks.

- [ ] **Step 1: Run focused setup tests**

Run: `pytest tests/test_setup_wizard.py tests/test_repository_best_practices.py -v`

Expected: PASS.

- [ ] **Step 2: Run repo-required verification**

Run:

```bash
just lint
just test
```

Expected: both commands PASS.

- [ ] **Step 3: Commit implementation**

Run:

```bash
git add repo/plugin.video.nzbdav/resources/skins/Default/1080i/setup-wizard.xml repo/plugin.video.nzbdav/resources/lib/setup_wizard.py tests/test_repository_best_practices.py tests/test_setup_wizard.py docs/superpowers/plans/2026-05-24-setup-wizard-centered-fields.md
git commit -m "feat(setup): center wizard fields"
```

Expected: commit succeeds after verification.

---

## Self-Review

- Spec coverage: centered single-column layout, copy above fields, centered footer, results-style focus background/accent, square XML rows, helper/value row fields, and no workflow change are each covered by tasks.
- Placeholder scan: no deferred implementation placeholders remain.
- Type consistency: helper property name is `helper` in Python and XML; existing value property remains `value`.
