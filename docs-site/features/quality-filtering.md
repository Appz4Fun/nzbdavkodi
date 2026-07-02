# Quality filtering and sorting

NZB-DAV filters the combined search results down to what you actually want, then
ranks what's left. All filters live on the **Quality Filters** and **Keyword
Filters** tabs; ranking lives on the **Sorting** tab.

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
| **Resolution** | 2160p / 4K, 1080p, 720p, 480p |
| **HDR** | HDR10, HDR10+, Dolby Vision, HLG, SDR |
| **Audio** | Atmos, TrueHD, DTS-HD MA, DTS:X, DD+ (EAC3), DD (AC3), AAC |
| **Video codec** | x265/HEVC, x264/AVC, AV1, VP9, MPEG-2 |
| **Language** | English, Spanish, French, German, Italian, Portuguese, Dutch, Russian, Japanese, Korean, Chinese, Arabic, Hindi |

### How the filters treat unknown attributes

This behavior is important to understand, because it affects which releases
survive:

- **Most filters fail open.** If NZB-DAV can't detect a release's resolution,
  audio, codec, or language, the release is **kept** — a filter only removes a
  release when it can positively identify a disallowed attribute. This prevents
  poorly named releases from vanishing.
- **HDR is the exception.** A release with no detected HDR is treated as **SDR**.
  So if you disable **SDR** while leaving HDR types enabled, releases with no
  detectable HDR are **removed**. Keep SDR enabled unless you truly want only
  HDR content.

## Keyword and group filters

On the **Keyword Filters** tab:

| Setting | Effect |
|---------|--------|
| **Exclude keywords** | Comma-separated. A release is removed if any keyword appears anywhere in its title. |
| **Required keywords** | Comma-separated. A release is removed unless every keyword appears in its title. |
| **Min size** / **Max size** | In MB, `0` = no limit. A release outside the range is removed. If a release's size can't be read, it's treated as 0 MB — so a non-zero minimum removes size-less placeholder rows. If you set a maximum below the minimum, the size filter is disabled and a warning is logged. |
| **Configure Preferred Groups** | Release groups to **prefer**. |
| **Configure Excluded Groups** | Release groups to **remove**. |

!!! warning "Preferred and excluded groups behave differently"
    - **Excluded groups** are a hard filter: matching releases are removed.
    - **Preferred groups** are **not** a filter. They only **boost ranking**
      under the Relevance sort. Choosing a preferred group never hides other
      groups — if you want only certain groups, use required keywords or
      excluded groups instead.

<!--
Screenshot placeholder — Capture the Configure Preferred Groups (or Excluded
Groups) multi-select dialog.
To add: save it as docs-site/images/configure-groups.png, then replace this
comment with:  ![Configure groups dialog](../images/configure-groups.png)
-->

## Sorting and selection

On the **Sorting** tab:

| Setting | Options | Default |
|---------|---------|---------|
| **Sort by** | Relevance, Size (largest first), Size (smallest first), Age (newest first), Age (oldest first) | Relevance |
| **Max results** | 1–100 | 25 |
| **Auto-select best match** | Skip the picker and play the top-ranked result | Off |

### How Relevance ranking works

When you sort by **Relevance**, NZB-DAV ranks releases by this priority order:

1. **Resolution** — 2160p, then 1080p, then 720p, then 480p.
2. **HDR** — Dolby Vision, then HDR10+, then HDR10, then HLG, then SDR.
3. **Preferred group** — releases from a preferred group rise above the rest.
4. **Audio** — a TrueHD + Atmos combination ranks highest, then Atmos, TrueHD,
   DTS:X, DTS-HD MA, DTS, DD+, DD, and AAC.
5. **Size** — larger files win the final tie-break.

### About "Max results"

**Max results** applies in two places: it caps how many results each provider is
asked for, and it truncates the filtered list you see. With **Auto-select best
match** on, NZB-DAV plays the first release after ranking — so "best" always
means "the top item under your current sort order."

For the exact ranking math and filter internals, see
[How it works → Search pipeline](../how-it-works/search-pipeline.md).
