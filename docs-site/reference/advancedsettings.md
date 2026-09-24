# advancedsettings.xml and seeking

On large files, whether you get **full seeking** depends on one Kodi setting:
the in-memory cache size. This page explains why and how to set it.

## Why it matters

NZB-DAV's default serving mode for large non-MP4 files is **direct
pass-through**, which offers native HTTP range seeking. Many CoreELEC and
Amlogic devices run 32-bit Kodi builds, though. In those builds, Kodi's
file-cache layer has a signed 32-bit offset bug. When a file's advertised size
is large (roughly above 4 GB), offsets through that cache are miscalculated
and playback or seeking fails.

There are two ways around this:

- **Set Kodi's cache memory size to `0`** with
  `<cache><memorysize>0</memorysize></cache>`. Pass-through then streams with
  **full seeking** at any size.
- **Leave the cache as it is** and set **Large non-MP4 stream mode** (Advanced
  tab) to a remux tier. NZB-DAV then serves large files through ffmpeg as an
  unsized stream, which hides the true file size from Kodi:
    - **Matroska remux** works, but seeking is **bounded** to what Kodi has
      already buffered. The pipe has no byte ranges, and ffmpeg isn't
      restarted at a new position.
    - **fMP4 HLS** gives full random seeking, but it's experimental and gated
      on Dolby Vision profile. See
      [Playback, remux, and seeking](../features/playback-and-remux.md#dolby-vision-handling).

Setting the cache to `0` gives the better experience for large files, which is
why NZB-DAV recommends it.

## How to set it

NZB-DAV never edits `advancedsettings.xml` for you, because merging into an
existing file could overwrite your other settings. You add the entry yourself.

1. Create or edit this file:

    - Standard path: `special://profile/advancedsettings.xml`
    - On CoreELEC: `/storage/.kodi/userdata/advancedsettings.xml`

2. Add the following, merging with any existing `<advancedsettings>` block:

    ```xml
    <advancedsettings>
      <cache>
        <memorysize>0</memorysize>
      </cache>
    </advancedsettings>
    ```

3. Restart Kodi for the change to take effect.

4. If you had switched **Large non-MP4 stream mode** to a remux tier, set it
   back to **Direct pass-through (default)**. NZB-DAV doesn't change the mode on
   its own when it detects the cache setting.

!!! warning "Merge, don't overwrite"
    If you already have an `advancedsettings.xml` file, add the `<cache>`
    element inside the existing `<advancedsettings>` root. Don't replace the
    file.

## The in-app prompt

When a stream is served through an ffmpeg remux, this setting could let the
file play in full-seek pass-through instead. NZB-DAV then shows a dialog that
explains this, after playback has started. A remux happens when you've chosen
a remux tier, or when an MP4 needed the ffmpeg remux rescue. The dialog offers
three choices:

| Choice | Effect |
|--------|--------|
| **Show instructions** | Opens a viewer with the exact XML snippet and file path above. |
| **Not now** | Dismisses the prompt for this Kodi session. You may see it again next session. |
| **Never ask** | Stops the prompt permanently. |

The prompt appears at most once per Kodi session. It appears only when a remux
was actually used, the cache isn't already `0`, and you haven't chosen
**Never ask**. With the default pass-through mode, you won't normally see it.

## Verifying

NZB-DAV detects the setting by reading `special://profile/advancedsettings.xml`.
It never writes to the file. The setting counts only when `<memorysize>` directly
under `<cache>` is exactly `0`. After you restart Kodi, play a large title
again. If it streams in pass-through with a working seek bar, the change took
effect.
