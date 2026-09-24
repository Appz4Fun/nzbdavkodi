# Play your first title

With the connections configured and TMDBHelper set up, you're ready to stream.

## Start playback

1. Open **TMDBHelper** and browse to a movie or TV episode.
2. Start playback. If TMDBHelper asks which player to use, choose
   **NZB-DAV**.
3. NZB-DAV searches your providers and shows the **source picker**. If no
   provider returns anything, you get a **No results found** notification
   instead.

## Choose a source

The picker is a full-screen list of the releases that passed your filters,
ranked by your **Sort by** setting and capped at **Max results** (both on the
**Sorting** tab). Each row shows the release name, file size, age, indexer,
release group, resolution, HDR format, video codec, audio format, source type,
and container.

![NZB-DAV results picker](../images/results-dialog.png)

- A green **DL** tag marks a release that's already completed on your backend
  (nzbdav, or NZBGet in NZBGet mode). NZB-DAV matches it by exact name and a
  close size match, so it can play almost immediately without downloading again.
- The status bar shows **Showing N of M sources after filters**.
- Press ++enter++ (OK) on a row to download and play it.
- Press ++esc++ (Back) to close the picker without playing anything.

If you turn on **Auto-select best match (skip result list)** on the **Sorting**
tab, NZB-DAV skips the picker and plays the top-ranked result that passed your
filters. If nothing passed, the picker opens anyway.

### Show filtered-out releases

Press ++c++ (or your remote's context-menu button) to switch between the
filtered list and **all** results. The footer shows how many releases are
hidden. In the all-results view, each release your filters would have removed
carries a yellow **FILTERED:** chip naming the first filter that rejected it:
`resolution`, `HDR`, `audio`, `codec`, `language`, `keyword`, `group`, or
`size`. This view isn't capped by **Max results**. You can still pick any of
those rows, and NZB-DAV plays exactly that release. Press ++c++ again to return to the
filtered list. Your saved filter settings don't change.

- If every release was filtered out, the picker opens directly in the
  all-results view.
- On CoreELEC and other Linux devices where NZB-DAV can read the remote's
  input device, you can also **hold OK for five seconds** to turn filters off.
  The footer shows **[Hold OK 5s] Filters off** when this works. A short
  press still selects the row.

See [Quality filtering and sorting](../features/quality-filtering.md) for the
filters themselves.

## Reuse a completed season pack

!!! info "Beta feature"
    Added in 2.0.0-beta.2 — available on the [Beta channel](beta-channel.md).

When a completed backend job contains multiple reliably named episodes,
NZB-DAV remembers the episodes in that job. A later request for one of them
shows **Already downloaded season pack - Episodes …** as the first row in the
picker. Auto-select plays that row first. Selecting that row reuses the completed
download without submitting another NZB.

Before playback, NZB-DAV rechecks the exact backend job and its original
completed folder, inventories it again, and selects the requested
season/episode from the filename. A missing requested episode never falls back
to a differently named episode. If the job or folder was conclusively removed,
the saved row becomes stale; temporary network, authentication, or server
errors leave it saved for a later attempt while normal online results remain
available.

## Watch the download progress

After you pick a source, NZB-DAV submits it to nzbdav and shows a progress
dialog that reflects the real download state. NZBGet mode uses its own progress
dialog. See [NZBGet backend](../features/nzbget-backend.md).

| Stage | What it means |
|-------|---------------|
| Submitting NZB… | The NZB is being sent to nzbdav. |
| Queued… | Accepted, waiting to start. |
| Fetching NZB… | nzbdav is retrieving and parsing the NZB. |
| Waiting for propagation… | Waiting for article availability. |
| Downloading… *n*% | Actively downloading. |
| Paused | The backend paused the job. |

Playback starts automatically as soon as enough of the file is ready. If the
release turns out to be a placeholder or has missing article bodies, NZB-DAV
detects it and moves on rather than playing a broken file.

<!--
Screenshot placeholder — Capture the download progress dialog mid-download (for
example, showing "Downloading... 42%").
To add: save it as docs-site/images/progress-dialog.png, then replace this
comment with:  ![Download progress dialog](../images/progress-dialog.png)
-->

## Resume where you left off

If you've watched part of a title before, NZB-DAV offers Kodi's native
**Resume from…** / **Start from beginning** prompt when you replay it. The
prompt follows Kodi's own default play action setting. Resume
points are tracked per release, so they survive even though the underlying
stream URL changes each session.

## One-time seeking setup for large files

If a stream you started from TMDBHelper is served through an ffmpeg remux,
NZB-DAV may show a dialog about `advancedsettings.xml`, at most once per Kodi
session. It suggests setting Kodi's cache memory size to `0` so large files can
use pass-through with full seeking on 32-bit Kodi builds. NZB-DAV only reads
that file to decide whether to show the dialog. It never edits it or changes
modes based on it. See
[advancedsettings.xml and seeking](../reference/advancedsettings.md) for the
exact steps.

## What happens behind the scenes

If you're curious how a pick becomes a playing stream — search, submission,
polling, the local proxy, and mid-playback recovery — read
[How it works](../how-it-works/architecture.md).

## Something didn't work?

See [Troubleshooting](../operations/troubleshooting.md) for the most common
setup, search, WebDAV, and playback issues.
