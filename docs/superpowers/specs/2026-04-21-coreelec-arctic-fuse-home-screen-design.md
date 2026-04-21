# CoreELEC Arctic Fuse Home Screen Design

## Summary

Configure the CoreELEC box at `root@coreelec.local` to use a snappy `skin.arctic.fuse.3` home screen with four top-level hubs:

- `Home`
- `Movies`
- `TV`
- `Streamers`

The design keeps the landing page intentionally light, uses native Arctic Fuse hub configuration instead of a wrapper addon, caps every widget row at five visible posters, and makes `Add More` jump straight into the full TMDb Helper or Trakt-backed listing for that row.

## Current Context

Observed on the target box:

- active skin is `skin.arctic.fuse.3`
- `startup.enablehubpreloading` is already `false`
- `plugin.video.themoviedb.helper` is installed
- no obvious standalone `script.trakt` addon was found in `/storage/.kodi/addons`
- TMDb Helper's local database contains Trakt-related tables such as `trakt_id`, `trakt_stats`, and `imdb_top250`

Interpretation:

- the implementation should prefer TMDb Helper and native skin paths first
- Trakt-backed rows are still in scope, but implementation must verify whether TMDb Helper alone exposes the needed rows or whether a dedicated Trakt addon/auth flow is required

## Goals

- Make the home screen feel fast on CoreELEC.
- Surface all user-requested discovery sources through a small number of focused hubs.
- Keep the top-level menu small and easy to scan.
- Avoid spinner-heavy or over-engineered home screen behavior.
- Prefer native skin features over custom addon development.

## Non-Goals

- Do not build a wrapper plugin just to launch rows.
- Do not add a `Collections` top-level hub.
- Do not preload every hub in the background.
- Do not introduce a submenu layer for `Add More`.
- Do not show placeholder or empty rows when a source is unavailable.

## Information Architecture

### Top-Level Hubs

The final home navigation is:

1. `Home`
2. `Movies`
3. `TV`
4. `Streamers`

This is the smallest menu that still keeps the sources semantically separated:

- `Home` is for broad, high-frequency entry points
- `Movies` is for movie-centric editorial and studio discovery
- `TV` is for TV-centric network and channel discovery
- `Streamers` is for service-brand browsing

### Hub Layout Rules

Each hub uses native Arctic Fuse widgets with these constraints:

- each widget row shows `5` posters/items maximum
- each row exposes an `Add More` action
- `Add More` opens the full underlying source listing directly
- no secondary submenu or nested hub is inserted between the row and the full listing

This keeps navigation shallow and predictable.

## Hub Definitions

### Home

`Home` stays intentionally minimal to preserve responsiveness.

Rows:

1. `Popular Movies` from Trakt
2. `Popular TV Shows` from Trakt
3. `My Calendar 24hrs` from Trakt

Rationale:

- these are broad, daily-use entry points
- they refresh often enough to justify the main landing page
- moving service-specific rows off Home reduces clutter and background work

### Movies

Rows:

1. `IMDb Top 250` from Trakt
2. `A24` from TMDb
3. `Criterion Channel` from Trakt
4. `Warner Bros. 100` from Trakt
5. `Sony Pictures` from TMDb

Rationale:

- all rows are movie-first discovery flows
- the mix of TMDb studio/discover and Trakt editorial lists is acceptable because the user experience is still "browse movies"

### TV

Rows:

1. `HBO` from TMDb Helper discover-by-network
2. `Showtime` from TMDb Helper discover-by-network
3. `Bravo` from TMDb Helper discover-by-network
4. `BBC iPlayer` from Trakt
5. `YouTube Premium` from Trakt
6. `Peacock Originals` from Trakt
7. `Paramount+ Originals` from Trakt

Rationale:

- this hub is explicitly TV-first
- linear channel/network discovery and streaming-service TV originals both belong here as long as the result set is primarily TV content

### Streamers

Rows:

1. `Netflix Originals` from a Trakt user list
2. `Amazon` from TMDb Helper discover-by-network for movies
3. `Apple TV+ Originals` from Trakt
4. `Disney+` from TMDb or Trakt
5. `Hulu` from Trakt
6. `MGM+` from Trakt
7. `Shudder Exclusives` from Trakt

Rationale:

- this hub is brand/service-oriented rather than media-type-oriented
- it provides the direct "show me what is on this service" path that the user asked for

## Source Resolution Rules

The design defines source intent, not only labels. Implementation must resolve each row to a direct Kodi plugin path that matches the intended content source.

### Preferred Source Types

- Use TMDb Helper direct discover paths for:
  - `Amazon`
  - `HBO`
  - `Showtime`
  - `Bravo`
  - `A24`
  - `Sony Pictures`
- Prefer TMDb-backed direct paths for `Disney+` if the result set is meaningfully "Disney+ content"; otherwise fall back to the cleanest Trakt-backed equivalent.
- Use Trakt-backed paths for:
  - `Popular Movies`
  - `Popular TV Shows`
  - `My Calendar 24hrs`
  - `IMDb Top 250`
  - `Criterion Channel`
  - `Warner Bros. 100`
  - `BBC iPlayer`
  - `YouTube Premium`
  - `Peacock Originals`
  - `Paramount+ Originals`
  - `Netflix Originals`
  - `Apple TV+ Originals`
  - `Hulu`
  - `MGM+`
  - `Shudder Exclusives`

### Netflix Fallback Rule

`Netflix Originals` should use one Trakt user/list as primary and the other as documented fallback:

- primary: whichever of `Gary-Caniff` or `Snoaks` resolves cleanly and updates reliably during implementation
- fallback: the other list

The mapping must be written down so a future broken list can be swapped quickly without rediscovering the source.

### Trakt Availability Rule

Because the box does not currently show a standalone Trakt addon in `/storage/.kodi/addons`, implementation must verify the cheapest working route:

1. use TMDb Helper's built-in Trakt-backed paths if they cover the row
2. if a required row is unavailable, install or enable the smallest Trakt dependency that exposes it cleanly
3. authenticate only what is necessary to support the chosen rows

The design goal is functional Trakt-backed rows, not loyalty to a particular addon.

## Performance Rules

The home screen should optimize for perceived speed rather than maximum content density.

Required behavior:

- keep `Home` limited to three rows
- keep Arctic Fuse hub preloading disabled
- avoid stacked nested widgets or wrapper scripts
- avoid indirect launcher nodes when a direct plugin path exists
- keep row limits low enough that poster loads and metadata churn remain modest

Explicit tradeoff:

- some sources could be normalized through custom nodes or helper wrappers, but that would make the system slower to build and more brittle to maintain
- this design chooses directness over abstraction

## Failure Behavior

If a source is broken, unauthenticated, empty, or too slow to be worth keeping on the landing surface:

- hide the row
- do not show blank placeholders
- do not keep dead rows on `Home`

If a required row cannot be made reliable from the intended source, the implementation may substitute the closest direct source only if:

- the resulting content meaning stays the same
- the row label remains truthful
- the substitution is documented in the final mapping notes

## Components

The implementation should rely on these existing components only:

- `skin.arctic.fuse.3` home hub configuration
- `plugin.video.themoviedb.helper`
- Trakt-backed plugin paths, either through TMDb Helper integration or a minimal dedicated addon if required
- Kodi's existing widget and hub presentation model

No new custom addon or wrapper service is part of this design.

## Data Flow

For each row:

1. the user lands on an Arctic Fuse hub
2. the hub resolves a direct plugin path for the row
3. the widget renders the first five items
4. selecting `Add More` opens the full underlying listing for that same source

This keeps the overview and deep-browse flows aligned. The user never has to learn a different path for "preview row" versus "full list."

## Testing And Verification

Implementation is complete only when the box proves the design in use.

Required checks:

1. verify the four top-level hubs exist and are ordered `Home`, `Movies`, `TV`, `Streamers`
2. verify `Home` opens quickly and shows only the three intended rows
3. verify every configured row renders no more than five visible content items before `Add More`
4. verify `Add More` opens the full source listing for each tested row
5. verify missing or broken rows are hidden rather than left empty
6. verify Trakt-backed rows that matter most to the user actually load on the box
7. verify `Netflix Originals` has a documented primary and fallback source

## Implementation Notes

The design deliberately leaves one implementation detail open: the exact Arctic Fuse storage surface used to persist hub and widget definitions.

That uncertainty is acceptable at design time because the behavior target is fully specified:

- the target skin is known
- the target box is known
- the hub structure is fixed
- the row mapping is fixed
- the performance and failure rules are fixed

The implementation plan should therefore start by discovering the concrete storage/write path for Arctic Fuse custom hubs on this box, then apply the row mapping above using the cheapest direct plugin paths that satisfy the design.
