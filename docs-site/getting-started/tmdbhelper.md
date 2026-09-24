# Set up TMDBHelper

NZB-DAV plays titles you choose in **TMDBHelper**. To connect the two, you
install an NZB-DAV *player file* into TMDBHelper and set it as your default
player.

## Install the player file

1. Make sure TMDBHelper is installed and you've
   [configured NZB-DAV's connections](configuration.md).
2. In NZB-DAV settings, open the **Player Installation** tab.
3. Select **Install TMDBHelper Player**.

This writes a small `nzbdav.json` player file into TMDBHelper's players folder
(`addon_data/plugin.video.themoviedb.helper/players/`) and registers
**NZB-DAV** as a selectable playback source. It also turns on TMDBHelper's
`only_resolve_strm` setting. Without that setting, TMDBHelper doesn't run
NZB-DAV's script action directly (see [below](#why-nzb-dav-uses-a-script-player)).
You get a **Player installed to: TMDBHelper** notification.

NZB-DAV protects your data while doing this:

- It refuses to write anywhere outside Kodi's add-on data folder.
- If a player file with the same schema version is already present, it's kept
  as is, so your manual edits survive. A file from an older schema version is
  backed up to `nzbdav.bak` and then replaced. If the backup can't be written,
  the install stops and your existing file stays in place.
- If the write fails, you get a **Failed to install to: …** message rather than
  a false success.

!!! tip "Re-run the install after updating NZB-DAV"
    Updating the add-on doesn't update an installed player file. Select
    **Install TMDBHelper Player** again after an update to pick up player file
    changes.

<!--
Screenshot placeholder — Capture the NZB-DAV Player Installation settings tab
and the "Player installed to: TMDBHelper" notification.
To add: save it as docs-site/images/install-player.png, then replace this
comment with:  ![Install TMDBHelper Player](../images/install-player.png)
-->

### Install into another add-on's player list

If you use a different front end that reads TMDBHelper-style player files,
select **Install Player Other** on the same tab. NZB-DAV scans Kodi's add-on
data folders (`addon_data/*/players/`) for add-ons that already have a
`players` folder, apart from TMDBHelper and NZB-DAV itself. It lists them and
installs `nzbdav.json` into the one you choose, with the same safeguards as
above. There's no fixed list of supported add-ons. If no add-on has a
`players` folder yet, you get a notification and nothing is written. This
route doesn't change any setting in the other add-on.

## Set NZB-DAV as your default player

1. Restart Kodi, **or** open TMDBHelper and select **Players → Update players**.
2. In TMDBHelper settings, set **Default player (Movies)** and **Default player
   (TV Shows)** to **NZB-DAV**.

<!--
Screenshot placeholder — Capture TMDBHelper's player selection or the Default
player (Movies/TV) dropdown showing NZB-DAV selected.
To add: save it as docs-site/images/tmdbhelper-default-player.png, then replace
this comment with:  ![TMDBHelper default player](../images/tmdbhelper-default-player.png)
-->

## Why NZB-DAV uses a script player

The NZB-DAV player file launches playback with a `RunScript` action instead of a
`plugin://` URL. This is deliberate: on CoreELEC and Kodi 21, asking Kodi to
open a `plugin://` URL as a playable item can crash the player before NZB-DAV's
code even runs. `RunScript` enters the add-on directly, shows the source picker,
and then starts playback — which is stable on those devices. You don't need to
configure anything for this; the installed player file already does it.

## Verify

Open any movie or episode in TMDBHelper and start playback. If the NZB-DAV
source picker appears, setup is complete. If **NZB-DAV** doesn't show up as a
player, see
[Troubleshooting → NZB-DAV doesn't appear in TMDBHelper](../operations/troubleshooting.md).

## Next step

[Play your first title](first-playback.md).
