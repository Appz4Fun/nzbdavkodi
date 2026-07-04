# advancedsettings.xml and seeking

On large files, whether you get **full seeking** depends on one Kodi setting:
the in-memory cache size. This page explains why, and how to set it.

## Why it matters

NZB-DAV's default serving mode for large non-MP4 files is **direct
pass-through**, which offers native HTTP range seeking. But on 32-bit Kodi
builds — which includes many CoreELEC/Amlogic devices — Kodi's file-cache layer
has a signed 32-bit offset bug. When a file's advertised size is large (roughly
above 4 GB), seeking through that cache miscalculates offsets and playback fails.

There are two ways around this:

- **Turn off Kodi's memory cache** by setting `<cache><memorysize>0</memorysize></cache>`.
  Pass-through then streams with **full seeking** at any size.
- **Leave the cache on** and rely on a remux mode, where NZB-DAV hides the true
  file size behind an unsized stream. This works, but seeking is **bounded** (a
  seek respawns the encoder at an approximate keyframe).

Setting the cache to `0` is the better experience for large files, which is why
NZB-DAV recommends it.

## How to set it

NZB-DAV never edits `advancedsettings.xml` for you — merging into an existing
file could clobber your other settings. You add the entry yourself.

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

!!! warning "Merge — don't overwrite"
    If you already have an `<advancedsettings>` file, add the `<cache>` element
    inside the existing `<advancedsettings>` root rather than replacing the file.

## The in-app prompt

The first time NZB-DAV actually needs a remux tier for a large file — the moment
this setting would make a difference — it shows a dialog explaining that the
file could stream in full-seek pass-through if the cache were set to `0`. The
dialog offers three choices:

| Choice | Effect |
|--------|--------|
| **Show instructions** | Opens a viewer with the exact XML snippet and file path above. |
| **Not now** | Dismisses for this session; you may see it again next session. |
| **Never ask** | Permanently stops the prompt. |

The prompt appears only when a remux was actually selected, the cache isn't
already `0`, and you haven't dismissed it permanently — so it won't nag you.

## Verifying

After restarting Kodi, play a large title again. If it streams in pass-through
with a working seek bar, the change took effect. NZB-DAV detects the setting by
reading (never writing) your `advancedsettings.xml`.
