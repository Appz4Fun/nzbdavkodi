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
| nzbdav URL | `nzbdav_url` | `http://localhost:3000` | Base URL of your nzbdav server (SABnzbd-compatible API). |
| API Key | `nzbdav_api_key` | *(empty)* | nzbdav API key, from **Settings → Usenet → API Key** in nzbdav. Stored hidden. |

**Action:** *Test nzbdav Connection* — verifies the URL and API key.

### WebDAV

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| WebDAV URL | `webdav_url` | `http://localhost:8080` | Base URL of the WebDAV server. **Leave empty to reuse the nzbdav URL.** Set a value only if WebDAV is on a separate address. |
| Username | `webdav_username` | *(empty)* | WebDAV username, from **Settings → WebDAV** in nzbdav. |
| Password | `webdav_password` | *(empty)* | WebDAV password. Stored hidden. |

**Action:** *Test WebDAV Connection* — verifies WebDAV reachability and
credentials.

### NZBHydra2

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Enable NZBHydra2 | `nzbhydra_enabled` | `false` | Use NZBHydra2 as a search provider. |
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
| TMDB API key (optional, improves TV results via TVDB id) | `tmdb_api_key` | *(empty)* | When set, NZB-DAV resolves a show's TVDB id and searches indexers by id for more accurate episode results. Stored hidden. |

## NZBGet

An alternative backend to nzbdav. When enabled, NZB-DAV downloads through NZBGet
and plays from an SMB share. See [NZBGet backend](../features/nzbget-backend.md).
The URL, username, password, category, and SMB fields appear only after you
enable the backend.

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Use NZBGet instead of nzbdav for playback | `nzbget_enabled` | `false` | Switch the entire download/playback path to NZBGet. |
| NZBGet URL | `nzbget_url` | `http://localhost:6789` | NZBGet control address. |
| NZBGet Username | `nzbget_username` | `nzbget` | NZBGet control username. |
| NZBGet Password | `nzbget_password` | *(empty)* | NZBGet control password. Stored hidden. |
| NZBGet Category | `nzbget_category` | *(empty)* | Category to submit under; also used to locate the completed file. |
| SMB Completed Folder | `nzbget_smb_root` | *(empty)* | SMB URL of NZBGet's completed-downloads base. |

**Actions:** *Test NZBGet Connection*, *Test SMB Share*.

## Indexers

Direct Newznab indexers, for when you don't run NZBHydra2 or Prowlarr. The
indexer fields appear only after you enable direct indexers.

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

**Custom indexers 1–3** — for any Newznab indexer not listed above. Each has
`direct_indexer_customN_enabled`, `direct_indexer_customN_name`,
`direct_indexer_customN_url`, and `direct_indexer_customN_api_key`.

**Actions:** *Manage Indexers* (add from a 23-entry preset catalog or a custom
URL; test, edit, enable/disable, delete), and *Test Direct Indexers*.

## Player Installation

Actions only — no stored settings.

- **Install TMDBHelper Player** — installs the NZB-DAV player file into
  TMDBHelper.
- **Install Player Other** — installs the player file into another add-on that
  has a `players` folder.

See [Set up TMDBHelper](../getting-started/tmdbhelper.md).

## Quality Filters

Every toggle is a boolean, and **all default to `true`** (show everything). Turn
off what you don't want. See
[Quality filtering](../features/quality-filtering.md) for the fail-open behavior
and the HDR/SDR exception.

| Group | Settings (id) |
|-------|---------------|
| **Resolution** | `filter_2160p` (2160p/4K), `filter_1080p`, `filter_720p`, `filter_480p` |
| **HDR** | `filter_hdr10`, `filter_hdr10plus`, `filter_dolby_vision`, `filter_hlg`, `filter_sdr` |
| **Audio** | `filter_atmos`, `filter_truehd`, `filter_dtshd_ma`, `filter_dtsx`, `filter_ddplus` (DD+/EAC3), `filter_dd` (DD/AC3), `filter_aac` |
| **Video codec** | `filter_hevc` (x265/HEVC), `filter_avc` (x264/AVC), `filter_av1`, `filter_vp9`, `filter_mpeg2` |
| **Language** | `filter_english`, `filter_spanish`, `filter_french`, `filter_german`, `filter_italian`, `filter_portuguese`, `filter_dutch`, `filter_russian`, `filter_japanese`, `filter_korean`, `filter_chinese`, `filter_arabic`, `filter_hindi` |

## Keyword Filters

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Preferred release groups | `filter_release_group` | *(empty)* | Comma-separated groups to **boost** in Relevance ranking. Edited with *Configure Preferred Groups*. **Not a filter.** |
| Excluded release groups | `filter_exclude_release_group` | *(empty)* | Comma-separated groups to **remove**. Edited with *Configure Excluded Groups*. |
| Min size (MB, 0=no limit) | `filter_min_size` | `0` | Remove releases smaller than this. A size that can't be read counts as 0 MB. |
| Max size (MB, 0=no limit) | `filter_max_size` | `0` | Remove releases larger than this. If max < min, the size filter is disabled. |
| Exclude keywords (comma-separated) | `filter_exclude_keywords` | *(empty)* | Remove releases whose title contains any keyword. |
| Required keywords (comma-separated) | `filter_require_keywords` | *(empty)* | Remove releases whose title lacks any keyword. |

**Actions:** *Configure Preferred Groups*, *Configure Excluded Groups* — both
open a multi-select of ~95 known release groups.

## Sorting

| Setting | id | Default | Values |
|---------|----|---------|--------|
| Sort by | `sort_order` | `0` (Relevance) | `0` Relevance, `1` Size (largest first), `2` Size (smallest first), `3` Age (newest first), `4` Age (oldest first) |
| Max results | `max_results` | `25` | Caps results per provider and truncates the filtered list. |
| Auto-select best match (skip result list) | `auto_select_best` | `false` | Play the top-ranked result and skip the picker. |

## Advanced

These tune polling, caching, stream resilience, fallback streams, and the proxy.

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
| Cache duration (seconds, 0=disabled) | `cache_ttl` | `300` | How long to cache search results. `0` disables the cache. Clamped to 0–86400. Stores raw pre-filter results, so filter/sort changes take effect immediately. |

### Stream resilience

These drive the background playback monitor.

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Auto-retry on stream failure | `stream_auto_retry` | `true` | Retry a failed stream automatically. |
| Max retry attempts | `stream_max_retries` | `3` | How many times to retry. |
| Retry delay (seconds) | `stream_retry_delay` | `5` | Wait between retries. |

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
| Convert MP4 subtitles to SRT | `proxy_convert_subs` | `true` | Convert MP4 `mov_text` subtitles to SRT during remux so embedded subs survive. |
| Force ffmpeg remux above (MB, 0=off) | `force_remux_threshold_mb` | `15000` | Size at which the chosen remux mode applies. `0` disables remux entirely (everything streams pass-through). |
| Large non-MP4 stream mode | `force_remux_mode` | `0` (Direct pass-through) | `0` Direct pass-through (default, no ffmpeg), `1` fMP4 HLS (compatibility, experimental), `2` Matroska remux (compatibility). |
| Strict upstream contract mode | `strict_contract_mode` | `1` (Warn only) | How to react when the upstream violates the strict Range/Content-Length contract: `0` Off, `1` Warn only, `2` Enforce. Off also disables the density breaker. |
| Enable density breaker | `density_breaker_enabled` | `false` | Abort a stream when a rolling 16 MB window becomes more than 50% zero-fill (catches dead releases early). Only active when contract mode isn't Off. |
| Enable zero-fill budget | `zero_fill_budget_enabled` | `true` | Cap total per-stream zero-fill; the stream ends with a clean error when the budget is hit. |
| Enable retry ladder before skip probe | `retry_ladder_enabled` | `true` | Re-issue the original range request with backoff on transient upstream errors before skip-filling. |
| Max seconds to wait for a slow/stalled backend before giving up (0=off) | `passthrough_stall_wait` | `120` | For an established stream that stalls on a recoverable backend condition, hold the connection open up to this budget. `0` closes immediately. Clamped to 0–600. |
| Read-ahead buffer size in MB (keeps filling while paused; 0=off) | `readahead_buffer_mb` | `256` | Per-session forward read-ahead prefetch. Keeps filling while paused. `0` disables. Clamped to 0–4096. |
| Send 200 for no-range pass-through | `send_200_no_range` | `false` | Send `200 OK` instead of `206 Partial Content` when Kodi requests a full object. Off until validated on your build. |

### Hidden settings

These aren't shown in the UI but exist in `settings.xml`:

| Setting | id | Default | Description |
|---------|----|---------|-------------|
| Content-root override | `webdav_content_root` | *(empty → `content`)* | Power-user override for the nzbdav content-root path segment. Change only for a non-standard reverse-proxy mount. |
| (migration/UI-state flags) | `force_remux_mode_v2_migrated`, `cache_warning_shown`, `cache_dialog_dismissed` | `false` | Internal state, not user-editable. |
