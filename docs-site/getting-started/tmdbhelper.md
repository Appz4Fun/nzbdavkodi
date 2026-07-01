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
and registers **NZB-DAV** as a selectable playback source. NZB-DAV protects your
data while doing this:

- It refuses to write anywhere outside Kodi's add-on data folder.
- If a compatible player file is already present, it's preserved; an older
  version is backed up to a `.bak` file before being replaced.
- If the write fails, you get a clear "Failed to install" message rather than a
  false success.

> **📷 Screenshot needed** — Capture the NZB-DAV **Player Installation** settings
> tab and the "Player installed to: TMDBHelper" notification. Save it to
> `docs-site/images/install-player.png` and replace this note with
> `![Install TMDBHelper Player](../images/install-player.png)`.

### Install into another add-on's player list

If you use a different front end that reads TMDBHelper-style player files, select
**Install Player Other**. NZB-DAV scans your installed add-ons for any that
already have a `players` folder, lists them, and installs `nzbdav.json` into the
one you choose. There's no fixed list of supported add-ons — the targets are
whatever compatible player add-ons you have installed.

## Set NZB-DAV as your default player

1. Restart Kodi, **or** open TMDBHelper and select **Players → Update players**.
2. In TMDBHelper settings, set **Default player (Movies)** and **Default player
   (TV Shows)** to **NZB-DAV**.

> **📷 Screenshot needed** — Capture TMDBHelper's player selection or the Default
> player (Movies/TV) dropdown showing NZB-DAV selected. Save it to
> `docs-site/images/tmdbhelper-default-player.png` and replace this note with
> `![TMDBHelper default player](../images/tmdbhelper-default-player.png)`.

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
