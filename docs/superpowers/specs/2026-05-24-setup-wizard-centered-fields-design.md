# Setup Wizard Centered Fields Design

## Goal

Refresh the first-run setup wizard so editable fields no longer feel shifted to the far right. The wizard should use the visual language from the redesigned results selection rows while staying simple, Kodi-native, and behaviorally unchanged.

## Selected Direction

Use the C1 mockup direction:

- Center the editable field list in a single-column page layout.
- Place the page title and body copy above the fields.
- Center footer actions with the page content instead of anchoring them against the far-right edge.
- Style each selectable row with a dark surface, a blue focused state, and a left accent bar on focus.
- Keep square XML-drawn rows. Do not add rounded-corner PNG textures or a texture generation script.

## Layout

Pages with editable rows use a centered content column sized for TV readability. The body copy sits above the row list, and the list remains wide enough for labels, helper text, and right-aligned values without needing the old right-side placement.

Welcome and final pages remain centered and may continue to omit the row list. The overall full-screen chrome stays aligned with the results redesign branch: dark background, dark header, subtle separators, restrained blue accent color.

## Row Design

Each row shows:

- Main label, from the existing setting label.
- Helper line, derived from row kind or setting purpose.
- Right-aligned value, using the existing value logic for booleans, provider, masked secrets, text fields, and empty values.

Focused rows use:

- Dark blue focused background.
- Blue left accent bar.
- Brighter label/value colors.

Unfocused rows use:

- Dark neutral background.
- No visible left accent bar.
- Muted helper text.

## Behavior

No workflow or settings behavior changes:

- Same pages and page order.
- Same row activation behavior for bool, provider, and text rows.
- Same connection test buttons and footer navigation.
- Same cancellation and finish behavior.
- Same Kodi `WindowXMLDialog` control IDs.

## Implementation Notes

Prefer XML-only skin changes plus small Python additions only where needed to expose helper text properties. Runtime addon code must remain Python 3.8 compatible and pure Python.

If helper text is added through `ListItem.Property`, keep it deterministic from the row metadata. Do not add new user-facing settings for this refresh.

## Tests

Update or add focused tests for:

- Setup wizard skin keeps the row list centered rather than right-anchored.
- Focused row layout includes a left accent bar.
- Row layouts expose label, helper text, and value fields with stable widths.
- Existing setup wizard behavior tests continue to pass.

Run `just lint` and `just test` before committing.
