# Search pipeline

This page traces a search from the moment you select a title to the ranked list
you see in the picker.

## Overview

```mermaid
flowchart TD
    Q[Play / search request] --> SRC{Entry path}
    SRC -->|plugin:// play or search| CACHE{Cache hit?}
    SRC -->|TMDBHelper RunScript| PLAN
    CACHE -->|yes| RANK
    CACHE -->|no| PLAN[Plan Newznab query<br/>tvdbid / imdbid / title]
    PLAN --> FAN[Run enabled providers<br/>serial if one, threaded if many]
    FAN --> NORM[Normalize to common result dict]
    NORM --> DEDUPE[De-duplicate by download link]
    DEDUPE --> STORE[Store in cache<br/>plugin:// path only]
    STORE --> RANK[Filter + rank<br/>record first rejecting filter]
    RANK --> PICK{Auto-select best?}
    PICK -->|on| PLAY[Resolve top filtered result]
    PICK -->|off| TAG[Tag already-downloaded results]
    TAG --> DLG[Picker: filtered view<br/>show-all toggle reveals rejected rows]
```

## Query planning

A shared planner builds each provider's query from the title and any ids:

- **Episodes** prefer a **TVDB id**, then an **IMDb id**, then fall back to a
  cleaned title, plus season and episode numbers.
- **Movies** use the **IMDb id**, else a cleaned title.
- When an indexer advertises its capabilities (caps), the planner honors the
  supported parameters for each search type; without caps it uses sensible
  defaults.
- If an id-based query returns nothing, NZB-DAV retries by title so a missing or
  mismatched id never leaves you empty-handed.
- When an episode request has no TVDB id, `tvdb_resolver.py` looks it up once
  from the TMDB/IMDb id through the TMDB API (cached on disk, fail-soft) so every
  provider shares it. A movie request carrying only a TMDB id gets its IMDb id
  the same way.

**Prowlarr** is a special case: its native search API doesn't take id parameters,
so NZB-DAV embeds them as tokens inside the query text — `{tvdbid:…}`,
`{imdbid:…}`, `{season:…}`, `{episode:…}`.

## Provider fan-out

Each enabled provider runs as a job. A single provider runs inline; multiple
providers run concurrently, one worker thread each. Direct indexers additionally
fan out across themselves in parallel with a bounded pool (at most four
workers) and a fan-out deadline, so one slow indexer can't stall the search.
Worker threads read settings from a snapshot taken up front, never from Kodi's
settings API directly.

## Result normalization

Every provider maps its response into one common shape:

| Field | Meaning |
|-------|---------|
| `title` | Release name (parsed for quality metadata) |
| `link` | Download URL for the NZB (the de-duplication key) |
| `size` | Size in bytes, as a string |
| `indexer` | Source indexer name |
| `pubdate` | Usenet post date, normalized to RFC-2822 |
| `age` | Human-readable age string |

!!! info "Why pubdate is normalized to RFC-2822"
    Prowlarr reports ISO-8601 dates; Newznab reports RFC-2822. NZB-DAV
    normalizes everything to RFC-2822 because post date drives sorting,
    same-repost de-duplication, and the download ledger. An unparseable date
    sorts at the epoch, which is why the format contract matters.

## De-duplication

Results are de-duplicated by **download link** — first occurrence wins, and a
result with no link is dropped as unplayable. The same release offered by two
providers with *different* download URLs is intentionally kept as two rows,
because they are genuinely different downloads.

## Title parsing (PTT)

NZB-DAV parses each release title with a vendored copy of *parse-torrent-title*
to extract resolution, HDR, audio, codec, group, languages, edition, year, and
flags such as PROPER/REPACK. If the parser throws or returns nothing useful, a
regex fallback extracts the essentials. The normalized metadata is cached on the
result and reused by filtering, ranking, and fallback matching.

## Filtering and ranking

Filtering applies your format, keyword, size, and excluded-group rules. Each
format category has a default-enabled **Other / Unknown** option. Missing HDR
is unknown, while an explicit SDR tag is SDR. Addon-owned tag normalization
corrects audio families, HDR aliases, dimensions, and language names without
editing vendored PTT. See [Quality filtering](../features/quality-filtering.md).

`filter_results()` returns both the surviving rows and the full parsed list.
Every row is tagged with the **first** filter that rejected it, checked in this
order: resolution, HDR, audio, codec, language, keyword, group, size. The
`max_results` cap applies only to the filtered list.

Under **Relevance** sort, results are ordered by this priority tuple:

```mermaid
flowchart LR
    A[Resolution: highest first]
    A --> B[HDR: DV > HDR10+ > HDR10 > HLG > other > none]
    B --> R[Hybrid REMUX > REMUX > other]
    R --> C[Preferred tier 1 > 2 > 3]
    C --> D[Audio]
    D --> E[Size: larger wins]
```

The preferred tiers apply to all releases. Explicit size or age sorting
bypasses the relevance priorities.

## Tagging and the picker

When the picker is going to open, results already present in your download backend get a
**DL** tag. A tag needs a name match plus a size match (and a consistent post
date) against nzbdav's completed history, or NZBGet's history in NZBGet mode.
An episode request can also get an already-downloaded season-pack row
prepended (see
[Playback pipeline](playback-pipeline.md#remembering-completed-season-packs)).

With **Auto-select best match (skip result list)** on (**Sorting › Auto-Select**),
the top filtered result plays with no picker and no tagging pass. Otherwise
`results_dialog.py` opens the full-screen picker on the filtered view.

The picker receives the unfiltered rows as well as the filtered ones. Pressing
**C** (context menu) switches between the filtered view and a **show all**
view. Rows that a filter rejected carry a `FILTERED: <reason>` chip naming that
filter. On devices where `results_input.py` can read the remote's key state,
holding **OK** for five seconds also switches to show-all. If nothing survives
filtering, the picker opens straight into show-all. DL tags
are computed over the full row set, so rows revealed by show-all keep them.

## Caching

The merged, **pre-filter** results are cached on disk, keyed by a SHA-256 of the
search type, title, year, season/episode, and ids. That means:

- Re-opening the same title is instant within the cache duration (default 60 s,
  capped at 86400 s).
- Changing filter or sort settings takes effect immediately — no new search
  needed, because filtering runs fresh on every read.
- The cache self-limits to 50 MB and 1000 entries, evicting the oldest first,
  and writes atomically.

The cache is used by the `plugin://` play and search routes. The TMDBHelper
RunScript path always queries providers fresh.

Set **Cache duration (seconds, 0=disabled)** (**Advanced › Search Cache**) to `0` to
disable caching, or clear it any time from the add-on's main menu.

Next: the [Playback pipeline](playback-pipeline.md).
