# Install the add-on

The recommended way to install NZB-DAV is through the **Appz4Fun Kodi
repository**, which delivers automatic updates. A manual zip install is also
available.

## Choose a channel

The [Appz4Fun Kodi repository](https://github.com/Appz4Fun/Appz4Fun-Kodi-Repo)
hosts NZB-DAV (and other Appz4Fun add-ons). It rebuilds itself from each
project's GitHub releases and publishes two channels, each with its own
repository add-on:

| Channel | Repository add-on | Name in Kodi | Contents |
|---------|-------------------|--------------|----------|
| **Stable** | `repository.appz4fun.stable` | Appz4Fun Repository | Releases **not** marked pre-release |
| **Beta** | `repository.appz4fun.beta` | Appz4Fun Repository (Beta) | Every release, including pre-releases |

Which build each channel serves right now:

| Channel | NZB-DAV version | Notes |
|---------|-----------------|-------|
| Stable | **1.2.3** | Last non-pre-release build (May 2026). |
| Beta | **2.0.0-beta.2** | The 2.0.0 line: NZBGet backend, exact season-pack reuse, the rewritten settings, and more. See [Beta channel and beta features](beta-channel.md). |

!!! note "This site documents the newest code"
    These pages follow the `main` branch, which is ahead of the Stable
    channel. Anything marked **Beta feature** needs the Beta channel. Features
    marked **Coming in the next beta** are merged but not released yet.

Pick **Stable** if you want the most-tested build. Pick **Beta** if you want
the 2.0.0 features now and are happy to report problems. You can switch later
(see [Switching channels](beta-channel.md#switching-channels)).

## Install from the Appz4Fun Kodi repository (recommended)

1. Download the channel's repository zip. Open the repository landing page,
   **[appz4fun.github.io/Appz4Fun-Kodi-Repo](https://appz4fun.github.io/Appz4Fun-Kodi-Repo/)**,
   and download the **Stable** or **Beta** zip, for example
   `repository.appz4fun.stable-1.0.1.zip` or
   `repository.appz4fun.beta-1.0.1.zip`. The version in the file name can
   change, so always download it from the landing page.
2. In Kodi, go to **Settings → System → Add-ons** and turn on **Unknown
   sources**. Kodi needs this to install any third-party repository.
3. Go to **Settings → Add-ons → Install from zip file** and select the
   repository zip you downloaded.
4. Go to **Settings → Add-ons → Install from repository**. Open **Appz4Fun
   Repository** (Stable) or **Appz4Fun Repository (Beta)**, then
   **Video add-ons → NZB-DAV → Install**.
5. From now on, Kodi updates NZB-DAV automatically on that channel.

!!! warning "Install one channel only"
    Kodi only updates a third-party add-on from the repository it was
    installed from. If you install both repository add-ons, NZB-DAV still
    follows whichever channel you installed it from. Keep things simple:
    install only the channel you want.

!!! tip "Downloading the zip on the Kodi device"
    If your Kodi device has no browser, download the repository zip on another
    computer and copy it to the device (USB drive or network share). Then use
    **Install from zip file** and browse to that copy. On CoreELEC or
    LibreELEC you can also download it over SSH, for example:

    ```bash
    wget -P /storage/downloads \
      https://appz4fun.github.io/Appz4Fun-Kodi-Repo/stable/repository.appz4fun.stable/repository.appz4fun.stable-1.0.1.zip
    ```

    Then pick it from **Install from zip file → Home folder → downloads**.
    For the Beta channel, replace both `stable`s with `beta`. Check the
    landing page for the current zip version.

## Upgrading from the old NZB-DAV repository

Older builds (up to the 1.2.x line) came from a repository add-on called
**NZB-DAV Repository** (`repository.nzbdav`), served from
`https://appz4fun.github.io/nzbdavkodi/`. That URL now hosts this
documentation site instead of add-on metadata, so installs from it no longer
get updates.

Because Kodi only updates an add-on from the repository it came from,
installing the new repository alone isn't enough. To move over:

1. Install the Appz4Fun repository zip for your channel (steps 1–3 above).
2. Open **Settings → Add-ons → My add-ons → Video add-ons → NZB-DAV** and
   choose **Versions** (called **Update** on some skins). Pick the newest
   version listed under **Appz4Fun Repository** (or **Appz4Fun Repository
   (Beta)**). NZB-DAV now updates from the new repository. Your settings are
   kept.
3. Uninstall the old **NZB-DAV Repository** add-on, and remove the old
   `nzbdav` source from **Settings → File manager**.

## Install manually from a zip

Use this if you'd rather not add a repository, or you want a specific version.

1. Download the add-on zip from the
   [NZB-DAV releases page](https://github.com/Appz4Fun/nzbdavkodi/releases).
   Each release has one asset named `plugin.video.nzbdav-<version>.zip`, for
   example `plugin.video.nzbdav-2.0.0-beta.2.zip`. Releases marked
   **Pre-release** on GitHub are beta builds.
2. In Kodi, turn on **Unknown sources** (see step 2 above).
3. Go to **Settings → Add-ons → Install from zip file** and select the file.

A manual install doesn't update automatically. To start getting updates later,
install a repository zip and reinstall NZB-DAV from it through **Versions**, as
in [Upgrading from the old repository](#upgrading-from-the-old-nzb-dav-repository).

## Verify the install

After installing, NZB-DAV appears under **Settings → Add-ons → My add-ons →
Video add-ons → NZB-DAV**. The add-on info page shows the installed version,
and it shows the repository it came from on skins that display that. A
background service also starts automatically and runs the local stream proxy
whenever Kodi is running.

## Next step

[Configure your connections](configuration.md) to your backend and search
provider.
