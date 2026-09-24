# Troubleshooting

Start with the section that matches what you see. Work through the steps in
order.

!!! danger "Redact secrets before sharing logs"
    Remove API keys, passwords, tokens, private hostnames, and full NZB URLs from
    any log you post publicly. NZB-DAV redacts credentials in its own log lines,
    but other add-ons and Kodi itself may not.

!!! tip "Finding NZB-DAV in kodi.log"
    Every NZB-DAV log line starts with `NZB-DAV:`. Kodi writes `kodi.log` to its
    temp folder (`/storage/.kodi/temp/kodi.log` on CoreELEC and LibreELEC). Some
    detail, such as the filter summary, is logged only when Kodi's debug
    logging is on (**Settings → System → Logging**).

## NZB-DAV stopped updating

If you installed NZB-DAV from the old **NZB-DAV Repository**
(`https://appz4fun.github.io/nzbdavkodi/`), you won't get any more updates. That
address now hosts this documentation site, not add-on metadata.

To fix it, install the Appz4Fun repository and move NZB-DAV over to it. The
steps are in
[Upgrading from the old NZB-DAV repository](../getting-started/installation.md#upgrading-from-the-old-nzb-dav-repository).
Your settings are kept.

A manual zip install never updates on its own. Use the same steps to move it
onto a repository.

## Going back from Beta to Stable

Kodi never downgrades an add-on automatically. If you're on
**2.0.0-beta.x**, switching to the Stable channel won't bring back **1.2.3** by
itself.

1. Make sure the Stable repository add-on (**Appz4Fun Repository**) is
   installed.
2. Open **Settings → Add-ons → My add-ons → Video add-ons → NZB-DAV** and
   choose **Versions**. Pick **1.2.3** from **Appz4Fun Repository**.
3. Uninstall the **Appz4Fun Repository (Beta)** add-on. While it's installed,
   the beta build stays in the **Versions** list, and it's easy to reinstall it
   by accident.

Settings that are new in 2.0.0 aren't in 1.2.3. For example, the NZBGet tab,
the read-ahead buffer, and the fallback submit delay don't exist there. See
[Beta channel and beta features](../getting-started/beta-channel.md).

## NZB-DAV doesn't appear in TMDBHelper

TMDBHelper lists NZB-DAV only after NZB-DAV's player file is installed.

1. Open **My add-ons → Video add-ons → NZB-DAV → Configure → Player
   Installation**, then select **Install TMDBHelper Player**. You can also open
   the add-on itself and pick **Install TMDBHelper Player** from its menu.
2. Wait for the notification. **Player installed to: TMDBHelper** means
   it worked. **Failed to install to: TMDBHelper** means the write failed.
3. Restart Kodi, **or** run **Players → Update players** in TMDBHelper.
4. Confirm the player file exists at
   `special://profile/addon_data/plugin.video.themoviedb.helper/players/nzbdav.json`.
   On CoreELEC that's
   `/storage/.kodi/userdata/addon_data/plugin.video.themoviedb.helper/players/nzbdav.json`.
5. Check `kodi.log` for these messages:
    - `NZB-DAV: Installing player to TMDBHelper at …`: the install started.
    - `NZB-DAV: Player installed successfully`: the file was written.
    - `NZB-DAV: Player already installed at schema v…; preserving existing file`:
      a current file was already there, so it was left alone.
    - `NZB-DAV: Refusing to install player outside addon_data` or
      `NZB-DAV: Failed to install player: …`: the install was blocked or
      failed.

Installing the player also switches on a TMDBHelper setting
(`only_resolve_strm`) so that TMDBHelper runs NZB-DAV's script player directly.
If you reset TMDBHelper's settings, run **Install TMDBHelper Player** again.

Placing the player file by hand is an advanced recovery step. Use it only if the
button and the player refresh both fail.

## TMDBHelper opens but the picker never appears

Confirm TMDBHelper is actually using NZB-DAV for the item type you chose.

1. Open **TheMovieDb Helper → Configure → Players**.
2. Set **Default player (Movies)** to **NZB-DAV**.
3. Set **Default player (TV Shows)** to **NZB-DAV**.
4. If the default is **Choose**, pick **NZB-DAV** from the player dialog.
5. Reinstall the player file and refresh players.

If TMDBHelper calls NZB-DAV but Kodi returns immediately, collect the `kodi.log`
lines around the play attempt.

## No results, or every result is filtered out

If you see **No results found for …**, no search provider returned anything.
Check the search backend first:

1. Confirm NZBHydra2, Prowlarr, or your direct indexers are reachable from the
   Kodi device.
2. Run the matching test action: **Test NZBHydra Connection**, **Test Prowlarr
   Connection**, or **Test Direct Indexers**. Each one shows a notification.
   **… connection OK** means it works. **… unexpected response** usually means
   a wrong API key. **… URL not configured** means the URL is empty.
3. For Prowlarr, fill in **Prowlarr Indexer IDs (comma-separated)**. The
   setting's help text says Prowlarr search needs it.
4. Search results are cached for **Cache duration** (Advanced → Search Cache;
   60 s by default). After you fix a
   provider, open the NZB-DAV add-on and choose **Clear Cache**, then search
   again.
5. Try a popular movie or episode that you know has a Usenet release.

If results were found but your filters rejected all of them, the picker opens
straight into its show-all view, with the header **Showing all N sources
(filters off)**. Each rejected row carries a **FILTERED:** tag that names the
first filter that rejected it (`resolution`, `HDR`, `audio`, `codec`,
`language`, `keyword`, `group`, or `size`). Loosen that filter. If some results
do pass, press **C** (the context-menu key) in the picker to switch between
filtered and all results. On Linux and CoreELEC, you can also hold **OK** for
five seconds to turn the filters off.

The **Other / Unknown** option in each filter group lets through releases whose
title doesn't name a recognized value. Turning it off, or turning off **SDR**,
often removes more than you expect. See
[Quality filtering](../features/quality-filtering.md).

## nzbdav submission waits too long or fails

Check nzbdav before you change any NZB-DAV settings.

1. Run **Test nzbdav Connection**. It reads nzbdav's queue with your API key.
2. Confirm the **API Key** in the NZB-DAV **Connection** settings matches
   nzbdav's key.
3. In the nzbdav UI, check whether the job was accepted, failed, or is still
   queued.
4. If nzbdav reports a failed import or missing articles, pick another release.

A slow submit isn't always a failure. nzbdav fetches and parses the NZB before
it replies, which can take a while on a large remux. NZB-DAV waits up to **NZB
submit timeout (seconds)** (Advanced → Polling, default 300) and takes over a
job that nzbdav accepted slowly.

Messages you may see:

- **nzbdav rejected the submission (HTTP …). Server message: …** Check
  nzbdav's logs.
- **… blocked the NZB download: too many requests.** Your indexer is
  rate-limiting you. Wait, or choose a release from another indexer.
- **Download timed out after N seconds.** The job didn't finish within
  **Download timeout (seconds)** (default 3600).

## WebDAV or authentication errors

NZB-DAV needs both the nzbdav API credentials and the WebDAV credentials.

1. Confirm **nzbdav URL** points to the nzbdav server.
2. If WebDAV uses the same address as **nzbdav URL**, **clear the WebDAV URL**
   so NZB-DAV reuses the nzbdav URL. On 2.0.0 builds this field defaults to
   `http://localhost:8080`. That address only works if Kodi can actually reach
   WebDAV there, and this is the most common cause of WebDAV errors. (On 1.2.3
   the field starts out empty.)
3. If you use a separate WebDAV endpoint, confirm **WebDAV URL** points to it.
4. Confirm the WebDAV **Username** and **Password** match nzbdav's WebDAV
   settings.
5. Run **Test WebDAV Connection**:
    - **WebDAV connection OK**: the connection works.
    - **WebDAV authentication failed. Check credentials.**: the username or
      password is wrong (HTTP 401/403).
    - **WebDAV server error. Check server logs.**: nzbdav returned a 5xx
      error.
    - **WebDAV connection error. Check server.**: the address is unreachable
      or wrong.
6. If a download completes but you see **Video file not found in WebDAV
   folder: …**, check the WebDAV settings above. Also make sure the download
   actually completed on nzbdav. If the message is **Download completed but
   the video file is incomplete**, nzbdav is missing articles from the middle
   of the file. Check your provider's retention, or pick another release.

## NZBGet backend problems

!!! info "Beta feature"
    The NZBGet backend was added in 2.0.0-beta.1. It's available on the
    [Beta channel](../getting-started/beta-channel.md).

- **NZBGet not configured**: **NZBGet URL** or the completed folder is empty.
- **NZBGet connection failed** (from **Test NZBGet Connection**): check the
  URL, **NZBGet Username**, and **NZBGet Password**.
- **Completed folder not reachable** (from **Test Completed Folder**): Kodi
  can't see the completed folder. Check the `smb://` URL and its credentials,
  or check your mount.
- **Video file is listed but not readable. If this persists, check the share
  or mount and restart Kodi.** The file shows up in the folder but won't open.
  After a download, NZB-DAV keeps trying to read the file for up to 60 seconds
  before it gives up. A file that stays unreadable is usually Kodi's cached SMB
  session going stale: new SMB sessions can read the file, but Kodi's cached
  one gets "Permission denied". Restarting Kodi resets that session. To avoid
  it altogether, mount the completed folder as an
  [NFS hard mount](../features/nzbget-backend.md#recommended-mount-the-completed-folder-over-nfs)
  instead of using `smb://`.
  `kodi.log` shows
  `NZB-DAV: video is listable but not readable through Kodi's VFS: …`.
- **No video file found in completed folder**: check **NZBGet Category**.
  NZBGet puts completed downloads in a subfolder named after the category.
  Also check that the completed folder points at NZBGet's completed-downloads
  folder.
- **NZBGet HealthCheck=Pause: set it to Delete, None, or Park …**: automatic
  duplicate failover can't work while NZBGet pauses broken downloads. Change
  `HealthCheck` in NZBGet.
- **Download failed in NZBGet**: NZBGet couldn't repair or unpack the release,
  and no duplicate backup took over. Pick another release.

For the full setup, see [NZBGet backend](../features/nzbget-backend.md).

## Playback starts, then fails

Playback goes through NZB-DAV's local proxy so Kodi can avoid WebDAV and
large-file edge cases.

1. Try another release for the same title.
2. Confirm the source is still available on your backend.
3. If you set **Large non-MP4 stream mode** to **Matroska remux** or **fMP4
   HLS** (Advanced → Proxy), confirm ffmpeg is installed on the Kodi device. If
   it isn't, you'll see **Failed to start ffmpeg**. The default, **Direct
   pass-through**, doesn't use ffmpeg. **Force ffmpeg remux above (MB, 0=off)**
   only sets the size at which the chosen remux mode starts.
4. On CoreELEC, or with large files, start with the default pass-through
   settings before you try the remux modes.
5. For full seeking on large files, set Kodi's cache to `0`. See
   [advancedsettings.xml and seeking](../reference/advancedsettings.md). If a
   remux starts and this setting is missing, NZB-DAV offers the snippet in a
   dialog. It shows once per Kodi session, or never again if you choose
   **Never ask**.
6. Check `kodi.log` for proxy, WebDAV, ffmpeg, or fallback messages.

Notifications during playback tell you what the proxy is doing:

| Notification | Meaning |
|---|---|
| **nzbdav unreachable — playback may glitch** | The proxy lost its connection to nzbdav. Check that nzbdav is running and reachable. |
| **nzbdav can't keep up — playback stalled** | nzbdav couldn't deliver data fast enough. Your Usenet provider or nzbdav may be overloaded. |
| **fall back to candidate #N successful** / **was a failure** | A backup release took over, or failed to. See [Fallback streams](../features/fallback-streams.md). |
| **Skipped N bytes across N recoveries** | Missing articles were skipped. You may notice a brief glitch. |
| **Stream aborted after repeated zero-fill recovery** | Too much of the file was unreadable. Pick another release. |

For the internals behind these behaviors, see
[How it works → Stream proxy](../how-it-works/stream-proxy.md).

## What to include in a bug report

- Kodi version and platform.
- NZB-DAV add-on version, and whether you're on the Stable or Beta channel.
- Whether the problem affects all titles or just one.
- Which search providers and which backend (nzbdav or NZBGet) you use.
- Whether your backend accepted, completed, or failed the job.
- Sanitized NZB-DAV settings relevant to the failure.
- Relevant `kodi.log` lines (the `NZB-DAV:` lines around the failure), with
  secrets removed.

Report issues at the
[NZB-DAV issue tracker](https://github.com/Appz4Fun/nzbdavkodi/issues).
