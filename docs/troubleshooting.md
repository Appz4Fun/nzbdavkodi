# Troubleshooting

Start with the section that matches what you see. Remove API keys, passwords,
tokens, hostnames you do not want public, and full NZB URLs before sharing logs.

## NZB-DAV Does Not Appear In TMDBHelper

TMDBHelper only shows NZB-DAV after the NZB-DAV player file has been installed
and TMDBHelper has refreshed its player list.

Try these in order:

1. Open **Add-ons > My add-ons > Video add-ons > NZB-DAV > Configure**.
2. Click **Install TMDBHelper Player**. (This installs the player file.)
3. Open TMDBHelper settings and run **Players > Update players**.
4. Restart Kodi.
5. Confirm TMDBHelper is installed and has created its addon data directory.
6. Check `kodi.log` for NZB-DAV player install messages.

Manual player-file placement is an advanced recovery step. Use it only after the
button path and TMDBHelper player refresh fail.

## TMDBHelper Opens But The NZB-DAV Result Dialog Never Appears

Check that TMDBHelper is using NZB-DAV for the item type you selected:

1. Open **TheMovieDb Helper > Configure > Players**.
2. Confirm **Default player (Movies)** is **NZB-DAV** for movies.
3. Confirm **Default player (TV Shows)** is **NZB-DAV** for episodes.
4. If the default is **Choose**, select **NZB-DAV** from the player dialog.
5. Reinstall the player file from NZB-DAV settings and refresh players.

If TMDBHelper calls NZB-DAV but Kodi returns immediately, collect the relevant
`kodi.log` lines around the play attempt.

## The Result Dialog Appears But No Results Are Shown

Check the search backend first:

1. Confirm NZBHydra2 or Prowlarr is reachable from the Kodi device.
2. Confirm the API key is correct.
3. Run the matching test action in NZB-DAV settings.
4. Loosen quality, size, release group, required keyword, and language filters.
5. Try a popular movie or episode with a known Usenet release.

## nzbdav Submission Waits Too Long Or Fails

Check nzbdav before changing NZB-DAV settings:

1. Confirm nzbdav is running.
2. Confirm the nzbdav API key in NZB-DAV settings matches nzbdav.
3. Open the nzbdav UI and check whether the job was accepted, failed, or is
   still queued.
4. If nzbdav reports a failed import or missing articles, try another result.

## WebDAV Or Authentication Errors

NZB-DAV needs both the nzbdav API credentials and the WebDAV credentials.

1. Confirm **nzbdav URL** points to the nzbdav server.
2. Confirm **WebDAV URL** points to the WebDAV endpoint if you configured one
   separately.
3. Under **WebDAV**, confirm **Username** and **Password** match nzbdav's WebDAV
   settings.
4. Run the WebDAV test action in NZB-DAV settings.
5. Check for 401 or 403 errors in `kodi.log`.

## Playback Starts Then Fails

Playback goes through the local NZB-DAV proxy so Kodi can avoid WebDAV and large
file edge cases.

1. Try another NZB for the same title.
2. Confirm the source is still available in nzbdav.
3. Confirm ffmpeg is installed if you enabled
   **Force ffmpeg remux above (MB, 0=off)** or set
   **Large non-MP4 stream mode** to
   **fMP4 HLS (compatibility, experimental)**.
4. On CoreELEC or large-file setups, start with the default pass-through proxy
   settings before enabling experimental remux modes.
5. Check `kodi.log` for proxy, WebDAV, ffmpeg, or fallback messages.

For contributor-level proxy internals, see
[Proxy Architecture](proxy-architecture.md).

## What To Include In A Bug Report

Include:

- Kodi version and platform.
- NZB-DAV addon version.
- Whether the problem happens for all titles or one title.
- Whether you use NZBHydra2, Prowlarr, or both.
- Whether nzbdav accepted, completed, or failed the job.
- Sanitized NZB-DAV settings relevant to the failure.
- Relevant `kodi.log` lines with secrets removed.

Do not include API keys, WebDAV passwords, complete NZB URLs, or private server
hostnames you do not want public.
