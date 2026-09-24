# Settings reference

This page documents every setting in NZB-DAV, grouped by the tab it appears on
in Kodi's add-on settings (**My add-ons → Video add-ons → NZB-DAV → Configure**).

For each setting you'll find its label, its internal id (useful if you edit
`settings.xml` directly), its default, and what it does. Actions — the buttons
that run a test or open a dialog — are listed with each tab.

!!! note "Defaults are chosen to be safe"
    You can run NZB-DAV by setting only the **Connection** tab. Everything else
    has a working default. The **Advanced** tab in particular should be changed
    only when you have a specific reason.

## Connection

Your links to nzbdav, WebDAV, and your search providers.

### nzbdav

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| nzbdav URL | `nzbdav_url` | `http://localhost:3000` | Base URL of your nzbdav or InfiniDysk server. |
| API Key | `nzbdav_api_key` | *(empty)* | nzbdav API key, from **Settings → Usenet → API Key** in nzbdav. Stored hidden. |

**Action:** *Test nzbdav Connection* — verifies the URL and API key.

### WebDAV

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| WebDAV URL (leave empty to use nzbdav URL) | `webdav_url` | `http://localhost:8080` | Base URL of the WebDAV server. **Clear it to reuse the nzbdav URL.** Keep a value only if WebDAV is on a separate address. |
| Username | `webdav_username` | *(empty)* | WebDAV username, from **Settings → WebDAV** in nzbdav. |
| Password | `webdav_password` | *(empty)* | WebDAV password. Stored hidden. |

**Action:** *Test WebDAV Connection* — verifies WebDAV reachability and
credentials.

### NZBHydra2

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Enable NZBHydra2 | `nzbhydra_enabled` | `false` | Use NZBHydra2 as a search provider. TMDBHelper playback honors this switch; NZB-DAV's own search menu and `plugin://` play URLs query NZBHydra2 even when it's off. |
| NZBHydra2 URL | `hydra_url` | `http://localhost:5076` | Base URL of your NZBHydra2 instance. |
| API Key | `hydra_api_key` | *(empty)* | NZBHydra2 API key. Stored hidden. |

**Action:** *Test NZBHydra Connection*.

### Prowlarr

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Enable Prowlarr | `prowlarr_enabled` | `false` | Use Prowlarr as a search provider. Only Usenet results are kept. |
| Prowlarr URL | `prowlarr_host` | `http://localhost:9696` | Base URL of your Prowlarr instance. |
| Prowlarr API Key | `prowlarr_api_key` | *(empty)* | Prowlarr API key. Stored hidden. |
| Prowlarr Indexer IDs (comma-separated) | `prowlarr_indexer_ids` | *(empty)* | Indexer IDs to query. Required for Prowlarr search — a blank value returns no results. |

**Action:** *Test Prowlarr Connection* — verifies URL, key, and indexer
reachability.

### TV search accuracy

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| TMDB API key (optional, movies and TV) | `tmdb_api_key` | *(empty)* | A TMDB key (not a TVDB key). For episodes, it resolves the show's TVDB id when TMDBHelper didn't supply one. For movies, it converts a TMDB movie id into an IMDb id when no IMDb id was supplied. IDs supplied by TMDBHelper are used directly. Without the key, or on lookup failure, the search uses whatever ids and title it already has. Stored hidden. |

!!! info "Beta feature"
    Added in 2.0.0-beta.1 (TV lookup), available on the
    [Beta channel](../getting-started/beta-channel.md).

## NZBGet

An alternative backend to nzbdav. When enabled, NZB-DAV downloads through NZBGet
and plays from an SMB share or a local/mounted path. See [NZBGet backend](../features/nzbget-backend.md).
The URL, username, password, category, and completed-folder fields appear only
after you enable the backend.

!!! info "Beta feature"
    Added in 2.0.0-beta.1, available on the
    [Beta channel](../getting-started/beta-channel.md).

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Use NZBGet instead of nzbdav for playback | `nzbget_enabled` | `false` | Switch the entire download/playback path to NZBGet. |
| NZBGet URL | `nzbget_url` | `http://localhost:6789` | NZBGet control address. |
| NZBGet Username | `nzbget_username` | `nzbget` | NZBGet control username. |
| NZBGet Password | `nzbget_password` | *(empty)* | NZBGet control password. Stored hidden. |
| NZBGet Category | `nzbget_category` | *(empty)* | Category to submit under; also used to locate the completed file. |
| Completed Folder (SMB or Local Path) | `nzbget_smb_root` | *(empty)* | `smb://` URL or local/mounted path of NZBGet's completed-downloads base. An [NFS hard mount](../features/nzbget-backend.md#recommended-mount-the-completed-folder-over-nfs) is recommended. |

**Actions:** *Test NZBGet Connection*, *Test Completed Folder*.

## Indexers

Direct Newznab indexers, for when you don't run NZBHydra2 or Prowlarr. The
indexer fields and buttons are always listed but stay greyed out until you
enable direct indexers.

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Enable direct Newznab indexers | `direct_indexers_enabled` | `false` | Master switch for direct indexers. |

**Popular indexers** — each has an *enable* toggle, an *API URL*, and an *API
key*. The URL defaults are the indexers' standard API endpoints:

| Indexer | Enable id | URL id (default) |
|---------|-----------|------------------|
| NZB.life / NZB.su | `direct_indexer_nzblife_enabled` | `direct_indexer_nzblife_url` (`https://api.nzb.su/api`) |
| NZBGeek | `direct_indexer_nzbgeek_enabled` | `direct_indexer_nzbgeek_url` (`https://api.nzbgeek.info/api`) |
| NZBFinder | `direct_indexer_nzbfinder_enabled` | `direct_indexer_nzbfinder_url` (`https://nzbfinder.ws/api`) |
| DrunkenSlug | `direct_indexer_drunkenslug_enabled` | `direct_indexer_drunkenslug_url` (`https://drunkenslug.com/api`) |
| NZBPlanet | `direct_indexer_nzbplanet_enabled` | `direct_indexer_nzbplanet_url` (`https://api.nzbplanet.net/api`) |
| DOGnzb | `direct_indexer_dognzb_enabled` | `direct_indexer_dognzb_url` (`https://api.dognzb.cr/api`) |

Each also has an API-key field (`direct_indexer_<name>_api_key`, stored hidden).

**Custom Newznab Indexers** — *Custom Indexer 1–3*, for any Newznab indexer
not listed above. Each has an enable toggle (`direct_indexer_customN_enabled`),
*Indexer Name* (`direct_indexer_customN_name`), *API URL*
(`direct_indexer_customN_url`), and *API Key* (`direct_indexer_customN_api_key`,
stored hidden). All default to off/empty.

**Actions:**

- *Manage Indexers* — add from a 22-entry preset catalog or a custom URL;
  test, edit, enable/disable, and delete managed indexers; refresh NZBHydra2
  caps. Opening it copies complete rows from this tab into the managed list
  once; a managed entry then takes precedence over the tab row with the same id.
- *Test Direct Indexers* — fetches caps from every enabled indexer.

!!! info "Beta feature"
    *Manage Indexers* was added in 2.0.0-beta.1 and is available on the
    [Beta channel](../getting-started/beta-channel.md).

## Player Installation

Actions only — no stored settings.

- **Install TMDBHelper Player** — installs the NZB-DAV player file into
  TMDBHelper.
- **Install Player Other** — installs the player file into another add-on that
  has a `players` folder.

See [Set up TMDBHelper](../getting-started/tmdbhelper.md).

## Quality Filters

Every format toggle defaults to `true`. **Other / Unknown** independently
controls missing or unlisted metadata in each category, including HDR.
See [Quality filtering](../features/quality-filtering.md) for the full options.

| Group | Settings (id) |
|-------|---------------|
| **Resolution** | `filter_4320p`, `filter_2160p`, `filter_1440p`, `filter_1080p` (p/i), `filter_720p` (p/i), `filter_576p` (p/i), `filter_540p`, `filter_480p` (p/i), `filter_360p`, `filter_240p`, `filter_unknown_resolution` |
| **HDR** | `filter_hdr10`, `filter_hdr10plus`, `filter_dolby_vision`, `filter_hlg`, `filter_sdr`, `filter_unknown_hdr` |
| **Audio** | `filter_atmos`, `filter_truehd`, `filter_dtshd_ma`, `filter_dtshd_hr`, `filter_dtsx`, `filter_dts`, `filter_ddplus`, `filter_dd`, `filter_aac`, `filter_flac`, `filter_pcm`, `filter_opus`, `filter_mp3`, `filter_alac`, `filter_vorbis`, `filter_mp2`, `filter_wma`, `filter_ac4`, `filter_unknown_audio` |
| **Video codec** | `filter_hevc`, `filter_avc`, `filter_av1`, `filter_vp9`, `filter_mpeg2`, `filter_mpeg4`, `filter_vc1`, `filter_mpeg1`, `filter_vp8`, `filter_wmv`, `filter_h263`, `filter_vvc`, `filter_mjpeg`, `filter_theora`, `filter_unknown_codec` |

## Languages

A separate tab after Quality Filters: 48 language toggles, one per language
(`filter_<language>`, from `filter_arabic` to `filter_vietnamese`), plus **Other / Unknown Language**
(`filter_unknown_language`). All default to `true`. Spanish includes Latino.
With **Chinese** enabled, releases tagged Cantonese or Urdu also pass.

## Keyword Filters

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Preferred groups: Tier 1 / 2 / 3 | `filter_remux_tier_1`, `filter_remux_tier_2`, `filter_remux_tier_3` | TRaSH remux tiers 1/2/3 | Editable comma-separated group names; empty disables that tier. Under Relevance, Tier 1 groups rank above Tier 2, then Tier 3 (after resolution, HDR, and REMUX). Ranking only — never hides a release. |
| Excluded release groups | `filter_exclude_release_group` | *(empty)* | Comma-separated groups to **remove**. Not shown as a field; edit it with *Configure Excluded Groups...*. |
| Min size (MB, 0=no limit) | `filter_min_size` | `0` | Remove releases smaller than this. A size that can't be read counts as 0 MB. |
| Max size (MB, 0=no limit) | `filter_max_size` | `0` | Remove releases larger than this. If max < min, the size filter is disabled. |
| Exclude keywords (comma-separated) | `filter_exclude_keywords` | *(empty)* | Remove releases whose title contains any keyword. |
| Required keywords (comma-separated) | `filter_require_keywords` | *(empty)* | Remove releases whose title doesn't contain every keyword. |

**Action:** *Configure Excluded Groups...* opens a multi-select of 94 known
release groups. An empty list means no exclusions.

## Sorting

| Setting | id | Default | Values |
|---------|----|---------|--------|
| Sort by | `sort_order` | `0` (Relevance) | `0` Relevance, `1` Size (largest first), `2` Size (smallest first), `3` Age (newest first), `4` Age (oldest first) |
| Max results | `max_results` | `25` | Number of results requested from each provider (clamped to 1–10000), and the length of the filtered list in the picker. The picker's show-all view is not truncated. |
| Auto-select best match (skip result list) | `auto_select_best` | `false` | Play the top-ranked result that passed your filters and skip the picker. If nothing passed, the picker opens instead. |

## Advanced

These tune polling, caching, stream resilience, fallback streams, and the proxy.

!!! info "Beta settings"
    **Clear download queue when starting a new download**, **Seconds into
    playback before submitting fallback backups**, **Max seconds to wait for a
    slow/stalled backend**, and **Read-ahead buffer size** were added in
    2.0.0-beta.1 and are available on the
    [Beta channel](../getting-started/beta-channel.md).

### Polling

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Poll interval (seconds) | `poll_interval` | `1` | Seconds between download-status checks. Clamped to 1–60. |
| Download timeout (seconds) | `download_timeout` | `3600` | Give up if the download isn't ready within this time. Clamped to 60–86400. |
| NZB submit timeout (seconds) | `submit_timeout` | `300` | Max wait for nzbdav to accept the NZB (it fetches and parses the NZB before replying). Clamped to 5–600. |
| Clear download queue when starting a new download | `clear_queue_on_submit` | `0` (Ask) | `0` Ask, `1` Always clear, `2` Never. Excludes this title's own in-flight job, and never clears a completed copy you're about to reuse. |

### Search cache

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Cache duration (seconds, 0=disabled) | `cache_ttl` | `60` | How long to cache search results. `0` disables the cache. Clamped to 0–86400. Stores raw pre-filter results, so filter/sort changes take effect immediately. The TMDBHelper player always runs a fresh search. |

### Stream resilience

These drive the background playback monitor.

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Auto-retry on stream failure | `stream_auto_retry` | `true` | Retry a failed stream automatically. |
| Max retry attempts | `stream_max_retries` | `3` | How many times to retry. Clamped to 0–10. |
| Retry delay (seconds) | `stream_retry_delay` | `5` | Wait between retries. Clamped to 1–300. |

### Fallback streams

See [Fallback streams](../features/fallback-streams.md).

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Enable fallback streams | `fallback_streams_enabled` | `true` | Master switch for mid-playback source switching. |
| Maximum standby fallback streams | `fallback_streams_max` | `5` | Backups kept ready per title. Hard ceiling 5. |
| Seconds into playback before submitting fallback backups | `fallback_submit_delay` | `120` | Delay before backups are submitted. `0` submits immediately. |

### Proxy

See [Playback and remux](../features/playback-and-remux.md) and
[How it works → Stream proxy](../how-it-works/stream-proxy.md).

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Convert MP4 subtitles to SRT | `proxy_convert_subs` | `true` | During a Matroska remux, convert MP4 `mov_text` subtitles to SRT so embedded subs survive. MKV subtitle tracks are copied unchanged. |
| Force ffmpeg remux above (MB, 0=off) | `force_remux_threshold_mb` | `15000` | Size at which the chosen remux mode applies to non-MP4 files. `0` turns size-based remux off, so those files always stream pass-through. No effect while the mode is Direct pass-through. |
| Large non-MP4 stream mode | `force_remux_mode` | `0` (Direct pass-through) | `0` Direct pass-through (default, no ffmpeg), `1` fMP4 HLS (compatibility, experimental), `2` Matroska remux (compatibility). |

### Pass-through validation

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Strict upstream contract mode | `strict_contract_mode` | `1` (Warn only) | How to react when the upstream violates the strict Range/Content-Length contract: `0` Off, `1` Warn only, `2` Enforce. Off also disables the density breaker. |
| Enable density breaker | `density_breaker_enabled` | `false` | Abort a stream when a rolling 16 MB window becomes more than 50% zero-fill (catches dead releases early). Only active when contract mode isn't Off. |
| Enable zero-fill budget | `zero_fill_budget_enabled` | `true` | Cap total per-stream zero-fill; the stream ends with a clean error when the budget is hit. |
| Enable retry ladder before skip probe | `retry_ladder_enabled` | `true` | Re-issue the original range request with backoff on transient upstream errors before skip-filling. |
| Max seconds to wait for a slow/stalled backend before giving up (0=off) | `passthrough_stall_wait` | `120` | For an established stream that stalls on a recoverable backend condition, hold the connection open up to this budget. `0` closes immediately. Clamped to 0–600. |
| Read-ahead buffer size in MB (keeps filling while paused; 0=off) | `readahead_buffer_mb` | `256` | Per-session forward read-ahead prefetch. Keeps filling while paused. `0` disables. Clamped to 0–4096. |
| Send 200 for no-range pass-through | `send_200_no_range` | `false` | Send `200 OK` instead of `206 Partial Content` when Kodi requests the whole file without a Range header. Leave off unless you've validated it on your build. |

### Hidden settings

These aren't shown in the UI but exist in `settings.xml`:

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Content-root override | `webdav_content_root` | *(empty → `content`)* | Power-user override for the nzbdav content-root path segment. Change only for a non-standard reverse-proxy mount. |
| (migration/UI-state flags) | `force_remux_mode_v2_migrated`, `cache_warning_shown`, `cache_dialog_dismissed` | `false` | Internal state, not user-editable. |
