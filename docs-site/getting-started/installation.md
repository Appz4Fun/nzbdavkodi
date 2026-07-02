# Install the add-on

The recommended way to install NZB-DAV is through the **Appz4Fun Kodi
repository**, which delivers automatic updates. A manual zip install is also
available.

## Install from the Appz4Fun Kodi repository (recommended)

The [Appz4Fun Kodi repository](https://github.com/Appz4Fun/Appz4Fun-Kodi-Repo)
hosts NZB-DAV (and other Appz4Fun add-ons) and rebuilds itself from each
project's GitHub releases. It offers two channels:

| Channel | Repository add-on | Contents |
|---------|-------------------|----------|
| **Stable** | `repository.appz4fun.stable` | Releases that are **not** marked pre-release. |
| **Beta** | `repository.appz4fun.beta` | All releases, including pre-releases. |

Pick **Stable** unless you specifically want early builds. You install a
channel's repository add-on once, and Kodi then keeps NZB-DAV updated along that
channel.

### Steps

1. Open the repository landing page:
   **[https://appz4fun.github.io/Appz4Fun-Kodi-Repo/](https://appz4fun.github.io/Appz4Fun-Kodi-Repo/)**
   and download the channel zip you want — for example
   `repository.appz4fun.stable-1.0.0.zip`.
2. In Kodi, go to **Settings → System → Add-ons** and turn on **Unknown
   sources** (Kodi requires this to install any third-party repository).
3. Go to **Settings → Add-ons → Install from zip file** and select the channel
   zip you downloaded.
4. Go to **Settings → Add-ons → Install from repository → Appz4Fun Repository**,
   then install **NZB-DAV** from **Video add-ons**.
5. Kodi installs future NZB-DAV updates automatically.

!!! info "Upgrading from the old repository?"
    Earlier versions were distributed from a Kodi repository at
    `https://appz4fun.github.io/nzbdavkodi/`. That URL now hosts this
    documentation site, not add-on metadata, so installs made from it no longer
    auto-update. Install the Appz4Fun repository (above) once to resume updates,
    then remove the old `nzbdav` file-manager source and the old **NZB-DAV
    Repository** add-on.

!!! tip "Downloading the zip on the Kodi device"
    If your Kodi device has no browser, download the channel zip on another
    computer, copy it to the device (USB drive or network share), and install
    it with **Install from zip file** by browsing to that local copy.

<!--
Screenshot placeholder — Capture the Appz4Fun repository landing page showing
the Stable and Beta channel download buttons.
To add: save it as docs-site/images/appz4fun-repo-landing.png, then replace this
comment with:  ![Appz4Fun Kodi repository landing page](../images/appz4fun-repo-landing.png)
-->

## Install manually from a zip

Use this if you prefer not to add a repository, or you want a specific version.

1. Download `plugin.video.nzbdav.zip` from the
   [NZB-DAV releases page](https://github.com/Appz4Fun/nzbdavkodi/releases).
2. In Kodi, go to **Settings → Add-ons → Install from zip file** and select the
   file.

With a manual install you won't get automatic updates — you'll need to repeat
these steps for each new release.

## Verify the install

After installation, NZB-DAV appears under **Settings → Add-ons → My add-ons →
Video add-ons → NZB-DAV**. A background service also starts automatically and
runs the local stream proxy whenever Kodi is running.

## Next step

[Configure your connections](configuration.md) to nzbdav and your search
provider.
