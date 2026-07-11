# Completed Season-Pack Reuse Design

## Goal

When one completed NZB contains multiple episodes, play the exact requested
episode instead of the largest file, remember that specific completed job as a
season pack, and offer it as a downloaded choice for later episodes from that
season. Support both the NZBGet/SMB and nzbdav/WebDAV backends.

## Scope

This feature covers completed download folders containing episodic video files.
It does not change provider search semantics, combine downloads, rename files,
or delete remote data.

A season pack is always isolated to one completed backend job:

- For NZBGet, its identity is the exact `NZBID` plus its `DestDir`.
- For nzbdav, its identity is the exact `nzo_id` plus its storage/WebDAV folder.

Only files beneath that job's completed folder may contribute episodes to its
inventory. Files with the same release name in other jobs, folders, or NZBs
must never be merged into the pack.

## Architecture

### Explicit episode context

The router will carry the requested content identity through selection and
resolution:

- content type;
- show title;
- TVDB, TMDB, and IMDb identifiers when available;
- requested season; and
- requested episode.

Both resolvers will use the explicit requested `(season, episode)` pair as the
authoritative file-selection hint. A selected release title remains a
compatibility fallback when explicit context is unavailable.

### Shared inventory contract

SMB and WebDAV retain backend-specific tree traversal but produce the same
logical inventory:

- every playable video path found within the completed job folder;
- its size when available;
- confidently parsed episode tags; and
- the exact video selected for the current request.

Episode identity ranks above size. A file named as another episode cannot win
because it is larger. Movie and no-episode paths retain the existing
largest-video behavior.

The inventory classifies a completed job as a season pack only when at least
two distinct episodes from one season are confidently parsed within that job's
folder. Generic filenames, samples, extras, ambiguous matches, and mixed-season
content do not establish a season pack. Multi-episode filename forms already
recognized by the addon's episode parser remain supported.

### Persistent catalog

A small JSON catalog under Kodi's addon-data directory records confirmed packs.
Each record contains:

- backend type;
- exact completed job identifier;
- completed job name;
- completed folder location;
- strong show identifiers when available;
- normalized show title as a fallback identity;
- season number;
- available episode numbers; and
- last-confirmed timestamp.

The backend and completed job identifier form the pack's primary identity. A
folder path corroborates and locates that job; it is not a key for combining
records. The catalog is bounded, written atomically, and stores no credentials,
API keys, or video data.

Strong show identifiers plus season select which saved job is relevant to a
future request. A conservative normalized-title fallback is allowed only when
strong identifiers are unavailable. These content identities never cause two
completed jobs to be merged.

## Playback Flows

### First episode and pack discovery

1. Kodi requests a concrete show, season, and episode.
2. The user or auto-select chooses an ordinary NZB result.
3. The selected NZB is submitted and completes normally.
4. The resolver inventories only that completed job's SMB or WebDAV folder.
5. It selects an exact requested-episode match. For example, an S01E01 request
   selects the S01E01 file even when S01E05 is larger.
6. If the inventory contains at least two distinct episodes for the requested
   season, the resolver records or refreshes that exact completed job in the
   pack catalog.
7. Playback proceeds through the existing resolver contract.

### Later episode reuse

1. Kodi requests another episode from the same show and season.
2. Catalog lookup runs alongside the ordinary provider search.
3. A catalog record is eligible only when its recorded episode inventory
   contains the exact requested episode.
4. The results picker places a synthetic `Downloaded season pack` row above
   normal indexer results and marks it with the existing downloaded indicator.
   The row shows a compact available-episode range or count.
5. Selecting the row does not submit an NZB. It reopens the one recorded
   completed job folder, inventories it again, and requires an exact requested
   episode match before playing.
6. Successful validation refreshes the catalog record. Ordinary online results
   remain available beneath the local pack choice.

When auto-select is enabled, a downloaded pack may be preferred only if its
catalog record includes the requested episode. Playback still performs fresh
validation before use.

## Integration Boundaries

### Router and picker

The router owns content identity and pack lookup. It adds a synthetic internal
result after normal release filtering so the row is not rejected for lacking
scene metadata. Selection recognizes that internal row and dispatches directly
to completed-folder reuse rather than a provider download.

The existing downloaded-result presentation is reused. No new setting is
required: exact local reuse is preferable while ordinary results remain
available as fallback.

### NZBGet/SMB

The current SMB resolver recursively chooses the largest video and caused the
observed S01E05 playback. Its traversal will become episode aware while
preserving depth bounds, `xbmcvfs`, retry behavior, progress cancellation, and
`Monitor.waitForAbort()`.

Pack reuse is bound to the stored NZBGet `NZBID` and `DestDir`. The job must
still be a successful completed-history item for that identifier, and its SMB
folder must remain accessible.

### nzbdav/WebDAV

WebDAV already has title-derived episode scoring. It will accept the explicit
requested episode identity so selection does not depend on the chosen release
name. Existing HTTP authentication, typed errors, traversal bounds, Range
behavior, and stub guards remain unchanged.

Pack reuse is bound to the stored nzbdav `nzo_id` and storage folder. The
completed job and folder are revalidated before playback.

## Failure and Staleness Rules

- If a folder is reachable but the requested episode is absent, do not play a
  different episode. Mark or remove that catalog entry and leave ordinary
  search results usable.
- If the completed job identifier no longer exists or resolves to a different
  folder, treat the entry as stale. Do not search same-named jobs as substitutes.
- A temporary authentication, network, or server error fails soft for that
  attempt and does not erase a potentially valid catalog record.
- A malformed catalog is ignored safely and may be rebuilt from later
  successful completed-job inventories.
- Catalog writes must not make otherwise-valid playback fail.
- No failure path deletes a remote completed job or its files.

## Compatibility and Invariants

- Runtime add-on code remains Python 3.8 compatible and pure Python.
- Every handle-based resolver exit continues to call
  `xbmcplugin.setResolvedUrl(...)` exactly as required.
- Polling and retry loops continue to use `xbmc.Monitor.waitForAbort()`.
- HTTP Range and stream-proxy behavior are unchanged.
- Movies, single-video downloads, and folders without reliable episode tags
  retain current largest-video selection.
- ffmpeg behavior and non-MP4 pass-through are unchanged.
- Kodi settings are not read unsafely from worker threads.

## Testing Strategy

Implementation follows test-driven development. Focused tests will cover:

- SMB selects S01E01 when S01E05 is the largest sibling.
- WebDAV uses explicit episode identity above title and size hints.
- An eight-episode job is classified and recorded as one pack tied to one job
  identifier and folder.
- Same-named releases under different job identifiers never combine.
- A later episode request receives a downloaded-pack picker row before online
  results.
- Selecting that row plays the exact episode without submitting another NZB.
- Partial packs appear only for episodes actually recorded as available.
- Missing requested episodes never fall back to another named episode.
- Nested folders, supported multi-episode names, generic files, samples,
  ambiguous titles, and mixed-season folders behave conservatively.
- Strong-ID matching and title-only fallback remain isolated and deterministic.
- Missing jobs and folders invalidate stale entries; transient backend errors
  preserve them.
- Catalog corruption and failed writes degrade without breaking playback.
- SMB cancellation and Kodi shutdown behavior remain intact.
- Existing movie, resolver failure, and `setResolvedUrl` tests remain green.

Before any implementation commit, run `just lint` and `just test`. Before
completion, run `just ci` to include the Python 3.8 compile compatibility gate.

## Success Criteria

Given the observed completed NZBGet job whose folder contains Spider-Noir
S01E01 through S01E08 and whose S01E05 file is largest:

- requesting S01E01 plays the S01E01 file;
- the job is recorded as one season pack under its exact NZBGet `NZBID` and
  `DestDir`;
- requesting S01E02 shows that downloaded pack first;
- selecting it plays the S01E02 file without submitting another NZB; and
- no content from any other NZB or completed job is included in that pack.
