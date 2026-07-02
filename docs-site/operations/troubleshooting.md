# Troubleshooting

Start with the section that matches what you see. Work through the steps in
order.

!!! danger "Redact secrets before sharing logs"
    Remove API keys, passwords, tokens, private hostnames, and full NZB URLs from
    any log you post publicly.

## NZB-DAV doesn't appear in TMDBHelper

TMDBHelper shows NZB-DAV only after the player file is installed and TMDBHelper
has refreshed its player list.

1. Open **My add-ons → Video add-ons → NZB-DAV → Configure**.
2. Select **Install TMDBHelper Player**.
3. In TMDBHelper, run **Players → Update players**.
4. Restart Kodi.
5. Confirm TMDBHelper is installed and has created its add-on data folder.
6. Check `kodi.log` for NZB-DAV player-install messages.

Placing the player file by hand is an advanced recovery step — use it only if
the button and the player refresh both fail.

## TMDBHelper opens but the picker never appears

Confirm TMDBHelper is actually using NZB-DAV for the item type you chose.

1. Open **TheMovieDb Helper → Configure → Players**.
2. Set **Default player (Movies)** to **NZB-DAV**.
3. Set **Default player (TV Shows)** to **NZB-DAV**.
4. If the default is **Choose**, pick **NZB-DAV** from the player dialog.
5. Reinstall the player file and refresh players.

If TMDBHelper calls NZB-DAV but Kodi returns immediately, collect the `kodi.log`
lines around the play attempt.

## The picker appears but there are no results

Check the search backend first.

1. Confirm NZBHydra2, Prowlarr, or your direct indexers are reachable from the
   Kodi device.
2. Confirm the API key is correct.
3. Run the matching **Test** action in NZB-DAV settings.
4. Loosen your quality, size, release-group, required-keyword, and language
   filters. Remember that disabling **SDR** removes releases with no detectable
   HDR — see [Quality filtering](../features/quality-filtering.md).
5. Try a popular movie or episode with a known Usenet release.

## nzbdav submission waits too long or fails

Check nzbdav before changing NZB-DAV settings.

1. Confirm nzbdav is running.
2. Confirm the nzbdav API key in NZB-DAV matches nzbdav's.
3. In the nzbdav UI, check whether the job was accepted, failed, or is still
   queued.
4. If nzbdav reports a failed import or missing articles, pick another release.

A slow submit isn't always a failure — nzbdav fetches and parses the NZB before
replying, which can take a while on a large remux. NZB-DAV waits up to the
**NZB submit timeout** (default 300 s) and adopts a slow-but-successful submit.

## WebDAV or authentication errors

NZB-DAV needs both the nzbdav API credentials and the WebDAV credentials.

1. Confirm **nzbdav URL** points to the nzbdav server.
2. If WebDAV uses the same endpoint as **nzbdav URL**, **clear the WebDAV URL**
   so NZB-DAV reuses the nzbdav URL. Don't leave the default
   `http://localhost:8080` unless Kodi can actually reach WebDAV there — this is
   the most common cause of WebDAV errors.
3. If you use a separate WebDAV endpoint, confirm **WebDAV URL** points to it.
4. Confirm **WebDAV Username** and **Password** match nzbdav's WebDAV settings.
5. Run the **Test WebDAV Connection** action.
6. Look for 401 or 403 errors in `kodi.log`.

## Playback starts, then fails

Playback goes through the local proxy so Kodi can avoid WebDAV and large-file
edge cases.

1. Try another release for the same title.
2. Confirm the source is still available on your backend.
3. If you enabled **Force ffmpeg remux above** or set **Large non-MP4 stream
   mode** to **fMP4 HLS**, confirm ffmpeg is installed on the Kodi device.
4. On CoreELEC or with large files, start with the default pass-through settings
   before enabling experimental remux modes.
5. For large-file seeking, set Kodi's cache to `0` — see
   [advancedsettings.xml and seeking](../reference/advancedsettings.md).
6. Check `kodi.log` for proxy, WebDAV, ffmpeg, or fallback messages.

For the internals behind these behaviors, see
[How it works → Stream proxy](../how-it-works/stream-proxy.md).

## What to include in a bug report

- Kodi version and platform.
- NZB-DAV add-on version.
- Whether the problem affects all titles or just one.
- Which search providers you use.
- Whether your backend accepted, completed, or failed the job.
- Sanitized NZB-DAV settings relevant to the failure.
- Relevant `kodi.log` lines, with secrets removed.

Report issues at the
[NZB-DAV issue tracker](https://github.com/Appz4Fun/nzbdavkodi/issues).
