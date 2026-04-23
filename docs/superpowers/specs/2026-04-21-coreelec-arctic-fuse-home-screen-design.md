# CoreELEC Arctic Fuse Home Screen Design

## Summary

Configure the CoreELEC box at `root@coreelec.local` to use a snappy `skin.arctic.fuse.3` home screen with four top-level hubs:

- `Home`
- `Movies`
- `TV`
- `Streamers`

The design keeps the landing page intentionally light, uses native Arctic Fuse hub configuration instead of a wrapper addon, caps every widget row at five visible posters, and makes `Add More` jump straight into the full TMDb Helper or Trakt-backed listing for that row.

The implementation is intentionally split into two steps:

1. create a source inventory with the exact plugin URLs, numeric IDs, and fallbacks that this addon version supports
2. apply the home hubs from that inventory

This split exists because the current addon version exposes several stable route classes but does not make every provider/network/company ID obvious from the saved skin settings alone.

## Current Context

Observed on the target box:

- active skin is `skin.arctic.fuse.3`
- `startup.enablehubpreloading` is already `false`
- `plugin.video.themoviedb.helper` is installed
- no obvious standalone `script.trakt` addon was found in `/storage/.kodi/addons`
- TMDb Helper's local database contains Trakt-related tables such as `trakt_id`, `trakt_stats`, and `imdb_top250`

Interpretation:

- the implementation should prefer TMDb Helper and native skin paths first
- TMDb Helper already exposes built-in Trakt authentication and Trakt route classes, so a separate Trakt addon is not automatically required
- Trakt-backed rows are still in scope, but implementation must verify whether TMDb Helper authentication alone is sufficient on the box

## Goals

- Make the home screen feel fast on CoreELEC.
- Surface all user-requested discovery sources through a small number of focused hubs.
- Keep the top-level menu small and easy to scan.
- Avoid spinner-heavy or over-engineered home screen behavior.
- Prefer native skin features over custom addon development.
- Guarantee that `Home` never collapses to a blank landing hub.

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

1. `Popular Movies`
   - primary route: TMDb Helper built-in Trakt popular route for movies
   - fallback route: TMDb Helper built-in TMDb popular route for movies
2. `Popular TV Shows`
   - primary route: TMDb Helper built-in Trakt popular route for TV
   - fallback route: TMDb Helper built-in TMDb popular route for TV
3. `My Calendar 24hrs`
   - primary route: TMDb Helper Trakt TV calendar route with `startdate=0` and `days=1`
   - fallback: hide only this row if Trakt auth is unavailable

Rationale:

- these are broad, daily-use entry points
- they refresh often enough to justify the main landing page
- moving service-specific rows off Home reduces clutter and background work
- `Home` must still render rows 1 and 2 when Trakt auth is missing, so the landing hub cannot go blank

### Movies

Rows:

1. `IMDb Top 250` from TMDb Helper's built-in cached IMDb Top 250 source
2. `A24` from TMDb movie discover by company
3. `Criterion Channel` from TMDb movie discover by watch provider
4. `Warner Bros.` from TMDb movie discover by company
5. `Sony Pictures` from TMDb movie discover by company

Rationale:

- all rows are movie-first discovery flows
- this hub now prefers reproducible TMDb company/provider filters over unspecified third-party lists

### TV

Rows:

1. `HBO` from TMDb TV discover by network
2. `Showtime` from TMDb TV discover by network
3. `Bravo` from TMDb TV discover by network
4. `BBC iPlayer` from TMDb TV discover by watch provider
5. `YouTube Premium` from TMDb TV discover by watch provider
6. `Peacock` from TMDb TV discover by watch provider
7. `Paramount+` from TMDb TV discover by watch provider

Rationale:

- this hub is explicitly TV-first
- linear channel/network discovery and TV-service discovery both belong here as long as the result set is primarily TV content

### Streamers

Rows:

1. `Netflix Originals` from a Trakt user list
2. `Amazon / Prime Video` from TMDb movie discover by watch provider
3. `Apple TV+` from TMDb discover by watch provider
4. `Disney+` from TMDb discover by watch provider
5. `Hulu` from TMDb discover by watch provider
6. `MGM+` from TMDb discover by watch provider
7. `Shudder` from TMDb discover by watch provider

Rationale:

- this hub is brand/service-oriented rather than media-type-oriented
- it provides the direct "show me what is on this service" path that the user asked for

## Source Resolution Rules

The design defines route class, semantics, and fallback behavior. Implementation must not configure hubs from free-text labels alone.

### Exact Route Classes

Use these route classes as the canonical basis for the final plugin URLs:

- `Popular Movies`
  - primary: `info=trakt_popular&tmdb_type=movie`
  - fallback: `info=popular&tmdb_type=movie`
- `Popular TV Shows`
  - primary: `info=trakt_popular&tmdb_type=tv`
  - fallback: `info=popular&tmdb_type=tv`
- `My Calendar 24hrs`
  - primary route class: TMDb Helper TV Trakt calendar route with `startdate=0`, `days=1`, `user=true`
  - no substitute row is required if auth is missing; only this row may be hidden on `Home`
- `IMDb Top 250`
  - route class: `info=trakt_userlist`
  - canonical source: `user_slug=justin`, `list_slug=imdb-top-rated-movies`, `tmdb_type=movie`, `sort_by=rank`, `sort_how=asc`
- `Netflix Originals`
  - route class: `info=trakt_userlist`
  - exact `user_slug` and `list_slug` are pinned by the Netflix fallback rule below
- company-backed movie rows
  - route class: `info=discover&tmdb_type=movie&with_companies=<numeric-id>&with_id=true`
- network-backed TV rows
  - route class: `info=discover&tmdb_type=tv&with_networks=<numeric-id>&with_id=true`
- watch-provider-backed rows
  - route class: `info=discover&tmdb_type=<movie|tv>&with_watch_providers=<numeric-id>&with_id=true`

Implementation note derived from the installed addon code:

- `with_companies` can be translated from company names by TMDb Helper
- `with_networks` and `with_watch_providers` must be treated as numeric-ID-backed configuration in this addon version
- free-text network/provider names are not acceptable as the final persisted hub configuration

### Source Inventory Gate

Before changing any Arctic Fuse hub settings, implementation must produce a source inventory for every row with:

- `hub`
- `row_label`
- `route_class`
- `final_plugin_url`
- `fallback_plugin_url` if one exists
- exact numeric IDs for provider/network/company filters when required
- auth requirement
- semantic source note

The home screen is configured only from that inventory. This is the mechanism that makes the design reproducible.

### Amazon Semantics

The `Amazon / Prime Video` row is explicitly a movie watch-provider row, not a network row.

Required rule:

- do not implement Amazon movie browsing with `with_networks`
- prefer US `Prime Video` watch-provider semantics
- if that cannot be made stable on the target addon version, fall back to a company-based movie row and rename the row truthfully

### Netflix Fallback Rule

`Netflix Originals` should use one Trakt user/list as primary and the other as documented fallback:

- primary: whichever of `Gary-Caniff` or `Snoaks` resolves cleanly and updates reliably during implementation
- fallback: the other list

The mapping must be written down so a future broken list can be swapped quickly without rediscovering the source.

### Trakt Availability Rule

Because the box does not currently show a standalone Trakt addon in `/storage/.kodi/addons`, implementation must verify the cheapest working route:

1. use TMDb Helper's built-in Trakt-backed paths for `Popular Movies`, `Popular TV Shows`, `My Calendar 24hrs`, `IMDb Top 250`, and `Netflix Originals`
2. prefer TMDb discover company/network/provider paths for the remaining rows
3. if a required Trakt-backed route is unavailable, enable the smallest auth/dependency surface that restores it
4. authenticate only what is necessary to support the chosen rows

The design goal is functional Trakt-backed rows, not loyalty to a particular addon.

## Performance Rules

The home screen should optimize for measured speed rather than maximum content density.

Required behavior:

- keep `Home` limited to three rows
- keep Arctic Fuse hub preloading disabled
- avoid stacked nested widgets or wrapper scripts
- avoid indirect launcher nodes when a direct plugin path exists
- keep row limits low enough that poster loads and metadata churn remain modest

Acceptance budgets on the target CoreELEC box:

- warm `Home` open: first populated poster visible in `<= 2.0 s`
- warm `Home` settle: all visible `Home` rows populated in `<= 5.0 s`
- warm secondary-hub open: first populated poster visible in `<= 2.5 s`
- warm secondary-hub settle: all visible rows populated in `<= 6.0 s`
- cold Kodi launch to first `Home` poster: `<= 6.0 s`

Explicit tradeoff:

- some sources could be normalized through custom nodes or helper wrappers, but that would make the system slower to build and more brittle to maintain
- this design chooses directness over abstraction

## Failure Behavior

If a source is broken, unauthenticated, empty, or too slow to be worth keeping on the landing surface:

- hide the row
- do not show blank placeholders
- do not keep dead rows on `Home`

Special rule for `Home`:

- `Home` must never ship as an empty hub
- rows 1 and 2 must have working fallbacks
- row 3 may be hidden if Trakt auth is unavailable

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
2. the hub resolves a direct plugin path for the row from the source inventory
3. the widget renders the first five items
4. selecting `Add More` opens the full underlying listing for that same source

This keeps the overview and deep-browse flows aligned. The user never has to learn a different path for "preview row" versus "full list."

## Testing And Verification

Implementation is complete only when the box proves the design in use.

Required checks:

1. verify the four top-level hubs exist and are ordered `Home`, `Movies`, `TV`, `Streamers`
2. verify the source inventory exists and every configured row references an exact final plugin URL
3. verify `Home` shows only the three intended logical rows and never renders blank
4. verify every configured row renders no more than five visible content items before `Add More`
5. verify `Add More` opens the full source listing for each tested row
6. verify missing or broken rows are hidden rather than left empty
7. verify Trakt-backed rows that matter most to the user actually load on the box
8. verify `Netflix Originals` has a documented primary and fallback source
9. verify the `Amazon / Prime Video` row uses provider or company semantics, never movie-network semantics
10. verify the performance budgets above with three runs and median timing recorded from the target box

## Implementation Notes

The design deliberately leaves one implementation detail open: the exact Arctic Fuse storage surface used to persist hub and widget definitions.

That uncertainty is acceptable at design time because the behavior target is fully specified:

- the target skin is known
- the target box is known
- the hub structure is fixed
- the logical row set and route classes are fixed
- the exact persisted plugin URLs are fixed by the source inventory gate
- the performance and failure rules are fixed

The implementation plan should therefore start by discovering:

1. the concrete storage/write path for Arctic Fuse custom hubs on this box
2. the exact provider/network/company IDs and final plugin URLs required by the source inventory

Only after that inventory exists should it apply the hub mapping above.
