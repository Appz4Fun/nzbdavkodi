# Quality filtering and sorting

NZB-DAV filters the combined search results down to what you actually want, then
ranks what's left. Format filters live on **Quality Filters**, followed by a separate
**Languages** tab. Group preferences, size, and keywords live on **Keyword Filters**;
ranking lives on **Sorting**. Filters never lock you out: the picker can always
[show the releases they removed](#filtered-and-all-results-in-the-picker).

<!--
Screenshot placeholder — Capture the Quality Filters settings tab showing the
resolution, HDR, audio, codec, and language toggle groups.
To add: save it as docs-site/images/quality-filters.png, then replace this
comment with:  ![Quality filters](../images/quality-filters.png)
-->

## Quality filters

Every quality toggle is **on by default**, which means "show everything." Turn
off the attributes you never want. NZB-DAV reads each release's attributes by
parsing its name.

| Group | Options |
|-------|---------|
| **Resolution** | 4320p / 8K, 2160p / 4K, 1440p / QHD, 1080p(i), 720p(i), 576p(i), 540p, 480p(i), 360p, 240p |
| **HDR** | HDR / HDR10, HDR10+, Dolby Vision, HLG, SDR |
| **Audio** | Atmos, TrueHD, DTS-HD MA, DTS-HD High Resolution, DTS:X, DTS, DD+ (EAC3), DD (AC3), AAC, FLAC, PCM / LPCM, Opus, MP3, ALAC, Vorbis, MP2, WMA, AC-4 |
| **Video Codec** | x265 / HEVC, x264 / AVC, AV1, VP9, MPEG-2, MPEG-4 ASP / Xvid / DivX, VC-1, MPEG-1, VP8, WMV, H.263, VVC (also matches H.266), MJPEG, Theora |
| **Languages** (own tab) | 48 languages, including Cantonese and Urdu; names and abbreviations are matched case-insensitively |

Each group also has **Other / Unknown**, enabled by default. It allows missing
attributes and values without a dedicated option. Turning it off requires a
recognized value in that category. An unchecked known format is still excluded;
Other / Unknown does not override that choice. Turning off every known option
retains the existing unrestricted behavior for known formats, while the unknown
option remains independent.

No HDR tag now means **Unknown**, rather than SDR. `HDR` selects HDR/HDR10;
`HDR+`, `HDRPlus`, `HDR10P`, and `HDR10+` select HDR10+. Dolby Vision requires
`DV`, `DoVi`, or `Dolby Vision`. Multiple explicit HDR tags are retained, and a
release passes when any selected format matches. `HLG` and explicit `SDR` are
recognized independently. These are filename attributes, not a scan of the
actual video tracks.

Resolution aliases share their filters: `2560x1440` is 1440p, `8K` and
`7680x4320` are 4320p, and 1080i/720i use the 1080p(i)/720p(i) selections.

Language abbreviations such as `fr` select French, and Latino/Latin American
Spanish select Spanish. A multilingual release passes when any selected
language matches. Cantonese and Urdu retain their own identities and options.
Under the configured grouping, selecting Chinese also accepts Cantonese and
Urdu; the Urdu grouping is a user preference, not a linguistic classification.

## Keyword and group filters

On the **Keyword Filters** tab:

| Setting | Effect |
|---------|--------|
| **Exclude keywords** | Comma-separated. A release is removed if any keyword appears anywhere in its title. |
| **Required keywords** | Comma-separated. A release is removed unless every keyword appears in its title. |
| **Min size** / **Max size** | In MB, `0` = no limit. A release outside the range is removed. If a release's size can't be read, it's treated as 0 MB — so a non-zero minimum removes size-less placeholder rows. If you set a maximum below the minimum, the size filter is disabled and a warning is logged. |
| **Preferred groups: Tier 1 / 2 / 3** | Comma-separated preferred groups, ranked in that order under Relevance. Defaults come from the TRaSH remux tiers. |
| **Configure Excluded Groups...** | Opens a multi-select of 94 known release groups. Checked groups are **removed**. Empty means no exclusions. |

!!! warning "Preferred and excluded groups behave differently"
    - **Excluded groups** are a hard filter: matching releases are removed.
    - **Preferred groups** are **not** a filter. They only **boost ranking**
      under the Relevance sort. Choosing a preferred group never hides other
      groups — if you want only certain groups, use required keywords or
      excluded groups instead.

<!--
Screenshot placeholder — Capture the preferred tiers and Configure Excluded
Groups control.
To add: save it as docs-site/images/configure-groups.png, then replace this
comment with:  ![Configure groups dialog](../images/configure-groups.png)
-->

## Sorting and selection

On the **Sorting** tab:

| Setting | Options | Default |
|---------|---------|---------|
| **Sort by** | Relevance, Size (largest first), Size (smallest first), Age (newest first), Age (oldest first) | Relevance |
| **Max results** | Whole number, clamped to 1–10000 when sent to providers | 25 |
| **Auto-select best match (skip result list)** | Skip the picker and play the top-ranked result | Off |

### How Relevance ranking works

When you sort by **Relevance**, NZB-DAV ranks releases by this priority order:

1. **Resolution** — highest resolution first, from 8K down to 240p; unknown
   resolution last. A 2160p release always ranks above a 1080p release,
   regardless of HDR or REMUX.
2. **HDR** — Dolby Vision, HDR10+, HDR/HDR10, HLG, then SDR and other tags,
   then releases with no HDR tag. A release with several tags ranks by its best.
3. **Release type** — filenames containing both REMUX and HYBRID first,
   then other REMUX releases, then everything else.
4. **Preferred group** — **Tier 1**, then **Tier 2**, then **Tier 3**, then
   groups in no tier. Group names match exactly, without case sensitivity.
5. **Audio** — TrueHD with Atmos first, then Atmos, TrueHD, DTS:X, then
   DTS-HD MA/FLAC/PCM/ALAC, then DTS-HD High Resolution/DTS, DD+, DD, AAC,
   other formats, and finally no audio tag.
6. **Size** — larger files win the final tie-break.

The three editable, comma-separated preferred group lists default to the
[TRaSH tier 1](https://github.com/TRaSH-Guides/Guides/blob/master/docs/json/radarr/cf/remux-tier-01.json),
[tier 2](https://github.com/TRaSH-Guides/Guides/blob/master/docs/json/radarr/cf/remux-tier-02.json), and
[tier 3](https://github.com/TRaSH-Guides/Guides/blob/master/docs/json/radarr/cf/remux-tier-03.json)
definitions retrieved on September 5, 2026. Their published group expressions
are bundled locally; a search does not fetch the guides. Clearing a tier leaves
it empty. Group preferences affect ranking, never whether a release is kept.
The Size and Age modes sort only by that property. Age uses the release's post
date; a missing or unreadable date sorts as the oldest, and an unreadable size
as 0.

### About "Max results"

**Max results** applies in two places: it caps how many results each provider is
asked for, and it truncates the filtered list you see. The picker's show-all
view (below) is not truncated. With **Auto-select best match** on, NZB-DAV
plays the first release that passed your filters after ranking — so "best"
always means "the top item under your current sort order." If nothing passed
your filters, the picker opens instead.

## Filtered and all results in the picker

The picker opens on the filtered list, and its header reads *Showing N of M
sources after filters*. To see everything the search returned:

- Press **C**, or use Kodi's context-menu button (long-press on most
  remotes), to switch to all results. The header changes to *Showing all N
  sources (filters off)*. Press it again to return to the filtered view.
- On CoreELEC/Linux devices where NZB-DAV can read the remote's input device,
  you can instead **hold OK for five seconds** to turn filtering off. A short
  press still selects a result. Releasing after the five-second hold doesn't
  select or download anything, and holding again keeps filtering off.

In the all-results view, every release your filters would remove carries a
yellow **FILTERED:** chip naming the first filter that rejected it:
`resolution`, `HDR`, `audio`, `codec`, `language`, `keyword`, `group`, or
`size`, checked in that order. You can pick one of these rows and it plays
normally. Nothing here changes your saved filter settings, and the footer
always shows the shortcuts available.

If no release passes your filters, the picker opens straight into the
all-results view, so you can still choose something.

For the exact ranking math and filter internals, see
[How it works → Search pipeline](../how-it-works/search-pipeline.md).
