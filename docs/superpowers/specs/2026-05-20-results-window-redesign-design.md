# Results Window Redesign Design

## Context

NZB-DAV currently uses one custom Kodi `WindowXMLDialog` for search result
selection:

- Python behavior: `repo/plugin.video.nzbdav/resources/lib/results_dialog.py`
- Skin XML: `repo/plugin.video.nzbdav/resources/skins/Default/1080i/results-dialog.xml`
- Settings schema: `repo/plugin.video.nzbdav/resources/settings.xml`

The current dense table is useful for scanning, but it is cramped from a couch.
The redesign adds two selectable layouts:

- Ranked cards, the new default.
- Split detail, for users who want a compact list plus a larger focused-result
  detail panel.

Selection semantics stay unchanged: Enter selects the focused result, Esc/back
cancels, and context menu cancels.

## Architecture

Use one shared Python dialog class with multiple skin XML files.

`show_results_dialog()` will read a new Kodi setting named `results_layout` and
map it to a skin XML file:

- `0` or missing/invalid: `results-dialog-ranked.xml`
- `1`: `results-dialog-split.xml`

The existing `results-dialog.xml` remains in the repo as the legacy dense-table
skin and as a compatibility reference, but it is not exposed as a setting option
for this change.

`ResultsDialog` remains the common `xbmcgui.WindowXMLDialog` implementation. It
will continue to populate the list control with `xbmcgui.ListItem` instances and
will add richer normalized item properties that both new skins can consume.

## Settings

Add an enum setting in the existing Sorting category:

```xml
<setting id="results_layout" label="30196" type="enum" default="0" lvalues="30197|30198" />
```

Add English strings:

- `30196`: `Results layout`
- `30197`: `Ranked cards`
- `30198`: `Split detail`

The default is ranked cards for new installs. If Kodi returns an empty, unknown,
or malformed value, the dialog will fall back to ranked cards.

## Shared Item Properties

The dialog will preserve the existing properties used by the legacy skin where
reasonable and add display-ready properties for the new layouts.

Properties should include:

- `resolution`, `hdr`, `codec`, `audio`, `quality`, `container`
- `size`, `age`, `indexer`, `group`, `available`
- `primary_badges`: compact top-line metadata such as resolution, HDR, codec,
  audio, source, and container.
- `details_line`: supporting details such as size, age, indexer, and group.
- `detail_title`: full release title for the split detail panel.
- `detail_video`, `detail_audio`, `detail_source`, `detail_origin`,
  `detail_status`: larger grouped fields for the split detail panel.

The properties are formatted in Python rather than repeated in XML so both
layouts share the same display semantics.

## Ranked Cards Layout

`results-dialog-ranked.xml` is the default full-screen results picker.

Each row is taller than the current dense table. The focused row emphasizes:

- Full release name as the primary line.
- Badge-style technical metadata near the top.
- Size, age, indexer, and group as supporting details.
- Downloaded/available state when `_available` is true.

The list remains the main interaction surface. Scrolling and selection behave as
they do today.

## Split Detail Layout

`results-dialog-split.xml` uses the same list control id (`50`) and scrollbar id
(`60`) to preserve dialog behavior and tests.

The left side contains a compact result list. The right side uses
`Container(50).ListItem.Property(...)` bindings to show details for the focused
result:

- Full release title.
- Video metadata.
- HDR/source/container metadata.
- Audio metadata.
- Size, age, indexer, group.
- Downloaded/available status.

No extra click target is introduced. Enter still selects the focused item.

## Error Handling

If the setting cannot be read, is missing, or is malformed, use ranked cards.

If no results are available, `show_results_dialog()` keeps returning `None`.

The redesign does not touch resolver behavior, playback submission, fallback
stream lookup, or proxy behavior.

## Testing

Add focused tests for:

- `show_results_dialog()` chooses ranked cards by default.
- Setting value `1` chooses split detail.
- Invalid setting values fall back to ranked cards.
- New item-property formatting handles missing metadata without crashing.
- Both new XML skins contain list id `50`, scrollbar id `60`, and link the list
  to the scrollbar.
- Split detail contains focused-item property bindings for its detail panel.

Run the existing required checks before committing implementation:

```bash
just lint
just test
```
