# Quickstart

This guide is for users who already have nzbdav and either NZBHydra2 or
Prowlarr running. It takes you from installed backend services to first playback
through TMDBHelper.

## Before You Start

Have these ready:

| Component | What you need |
|---|---|
| Kodi | Kodi 21 Omega or later |
| TMDBHelper | Installed, or installable from the Kodi add-on repository |
| NZBHydra2 or Prowlarr | Base URL and API key |
| nzbdav | Base URL and API key |
| nzbdav WebDAV | WebDAV username and password |

## 1. Install NZB-DAV

1. In Kodi, open **Settings > File Manager > Add source**.
2. Enter `https://xbmc4lyfe.github.io/nzbdavkodi/`.
3. Name the source `nzbdav`.
4. Open **Settings > Add-ons > Install from zip file**.
5. Choose `nzbdav` > the latest `repository.nzbdav-*.zip` shown at the source root.
6. Open **Install from repository > NZB-DAV Repository > Video add-ons > NZB-DAV**.
7. Install **NZB-DAV**.

## 2. Configure NZB-DAV

Open **Add-ons > My add-ons > Video add-ons > NZB-DAV > Configure**.

Enter the connection settings for the services you use:

| Setting | Value |
|---|---|
| Enable NZBHydra2 | Enable if you use NZBHydra2 |
| NZBHydra2 URL | Your NZBHydra2 base URL, such as `http://192.168.1.100:5076` |
| NZBHydra2 API Key | NZBHydra2 API key |
| Enable Prowlarr | Enable if you use Prowlarr |
| Prowlarr URL | Your Prowlarr base URL, such as `http://192.168.1.100:9696` |
| Prowlarr API Key | Prowlarr API key |
| nzbdav URL | Your nzbdav base URL, such as `http://192.168.1.100:3333` |
| nzbdav API Key | nzbdav API key |
| WebDAV URL | Clear this field when WebDAV uses the nzbdav URL; only enter a separate WebDAV base URL if your nzbdav setup exposes one |
| WebDAV Username | nzbdav WebDAV username |
| WebDAV Password | nzbdav WebDAV password |

Use the built-in test actions for Hydra/Prowlarr, nzbdav, and WebDAV before
moving on.

## 3. Install TMDBHelper

If TMDBHelper is not installed:

1. Open **Settings > Add-ons > Install from repository**.
2. Open **Kodi Add-on repository > Video add-ons**.
3. Install **TheMovieDb Helper**.

If TMDBHelper is not available for your Kodi version, install it from the
[TMDBHelper releases page](https://github.com/jurialmunkey/plugin.video.themoviedb.helper/releases).

## 4. Install The NZB-DAV Player File

The player file is what makes TMDBHelper show NZB-DAV as a playback option.

1. Open **Add-ons > My add-ons > Video add-ons > NZB-DAV > Configure**.
2. Click **Install TMDBHelper Player**.
3. Wait for the notification that the player was installed.
4. Restart Kodi, or open TMDBHelper settings and use **Players > Update players**.

## 5. Set NZB-DAV As The Default Player

1. Open **Add-ons > My add-ons > Video add-ons > TheMovieDb Helper > Configure**.
2. Open **Players**.
3. Set **Default player (Movies)** to **NZB-DAV**.
4. Set **Default player (TV Shows)** to **NZB-DAV**.

Leave the default player as **Choose** only if you want TMDBHelper to ask which
player to use each time.

## 6. Verify First Playback

1. Open TMDBHelper.
2. Pick a known movie or episode.
3. Select play.
4. Confirm that either the NZB-DAV result dialog appears or auto-select starts
   resolving a result.
5. Pick a result and wait for nzbdav to prepare the stream.

If NZB-DAV does not appear as a player, go to
[Troubleshooting: NZB-DAV does not appear in TMDBHelper](troubleshooting.md#nzb-dav-does-not-appear-in-tmdbhelper).

If the player appears but playback does not start, go to
[Troubleshooting](troubleshooting.md).
