# First-Run Setup Wizard Design

## Goal

Add a first-run setup wizard for `plugin.video.nzbdav` that guides new users through the minimum configuration needed to search, submit, and play NZB-DAV streams. The wizard must open automatically the first time the addon launches, and users must also be able to rerun it later from the addon menu.

## User Flow

The wizard is a page-by-page custom Kodi XML dialog flow with Previous, Next, and Cancel buttons on each page. The final page uses Finish instead of Next. Cancel exits the wizard without marking setup complete.

Pages:

1. Welcome
   - Explain that users should have service IP addresses or hostnames, ports, usernames/passwords, and API keys available.
   - Tell users they should already have TMDBHelper installed before continuing.
   - Show a red warning at the bottom saying "please install tmdb helper before continuing" when `plugin.video.themoviedb.helper` is not detected.
2. nzbdav
   - Edit `nzbdav_url`.
   - Edit hidden `nzbdav_api_key`.
   - Test connection against the existing authenticated nzbdav queue endpoint.
3. WebDAV
   - Edit `webdav_url`.
   - Edit `webdav_username`.
   - Edit hidden `webdav_password`.
   - Test connection with the existing WebDAV probe behavior.
4. Search provider
   - Select either NZBHydra2 or Prowlarr.
   - For NZBHydra2, edit `hydra_url` and hidden `hydra_api_key`.
   - For Prowlarr, edit `prowlarr_host` and hidden `prowlarr_api_key`.
   - Enable the selected provider and disable the other provider.
   - Test connection using the selected provider's existing authenticated test endpoint.
5. Resolutions
   - Toggle `filter_2160p`, `filter_1080p`, `filter_720p`, and `filter_480p`.
6. HDR
   - Toggle `filter_hdr10`, `filter_hdr10plus`, `filter_dolby_vision`, `filter_hlg`, and `filter_sdr`.
7. Video codecs
   - Toggle `filter_hevc`, `filter_avc`, `filter_av1`, `filter_vp9`, and `filter_mpeg2`.
8. Audio
   - Toggle `filter_atmos`, `filter_truehd`, `filter_dtshd_ma`, `filter_dtsx`, `filter_ddplus`, `filter_dd`, and `filter_aac`.
9. Languages
   - Toggle the existing language filter settings from `filter_english` through `filter_hindi`.
10. TMDBHelper player
   - Show an Install Player button for TMDBHelper.
   - Check whether `plugin.video.themoviedb.helper` is installed before attempting installation.
   - If TMDBHelper is installed, run the existing TMDBHelper player install path.
   - If TMDBHelper is not installed, show a message telling the user to install TMDBHelper before installing the NZB-DAV player.
   - Finish saves the wizard-completed marker.

## Architecture

Implement the wizard in a new runtime module, `resources/lib/setup_wizard.py`, backed by a new skin file at `resources/skins/Default/1080i/setup-wizard.xml`.

The module will:

- Use `xbmcgui.WindowXMLDialog` for the wizard frame, page rendering, page navigation, focused buttons, status text, and first-run TMDBHelper warning.
- Use standard `xbmcgui.Dialog().input()` only for text/password field entry, because Kodi XML controls do not provide a secure, portable password editor on their own.
- Read and write existing settings with `xbmcaddon.Addon("plugin.video.nzbdav")`.
- Keep all runtime code Python 3.8 compatible and pure Python.
- Reuse the existing connection-test behavior instead of duplicating HTTP details.
- Reuse the existing TMDBHelper player installer instead of duplicating player JSON behavior.
- Avoid changing resolver, playback, WebDAV Range, or stream proxy behavior.

Add a hidden setting, `setup_wizard_completed`, to `resources/settings.xml`. The addon will auto-run the wizard when the setting is not `true`.

Add a menu item to `_handle_main_menu()` that runs `plugin://plugin.video.nzbdav/setup_wizard`, so users can rerun the wizard later.

Add a route handler for `/setup_wizard` in `router.py`. The route is an action route and must still fall through to `_safe_resolve_handle()`.

For first launch, `_handle_main_menu()` should call a lightweight helper before rendering menu items:

- If `setup_wizard_completed` is not `true`, run the wizard.
- If the user cancels, continue showing the normal menu.
- If the user finishes, continue showing the normal menu with saved settings.

## XML Dialog Model

The XML dialog will render one page at a time with fixed control IDs for:

- Page title.
- Page body text.
- Field/action list.
- Previous button.
- Next or Finish button.
- Cancel button.
- Test Connection button, visible only on connection pages.
- Install Player button, visible only on the TMDBHelper player page.
- Red TMDBHelper warning label on the welcome page when TMDBHelper is missing.
- Status/feedback text near the footer.

The Python dialog class will set `Window.Property(...)` values for page title, body, button labels, warning visibility/text, status text, and page count. A list control will hold page-specific editable rows. Selecting a text row opens a standard Kodi input dialog and saves the corresponding existing setting immediately. Selecting a toggle row flips the existing boolean setting and refreshes the list row label.

Previous, Next, Cancel, Test Connection, Install Player, and Finish are true XML buttons handled through `onClick()`. Remote back/menu actions are treated as Cancel.

## Connection Tests

The wizard should expose the same success and error behavior as the current settings actions:

- nzbdav: authenticated `mode=queue` test via the existing nzbdav helper path.
- WebDAV: `probe_webdav_reachable(max_retries=0)`.
- NZBHydra2: authenticated lightweight search test.
- Prowlarr: authenticated `/api/v1/indexer` test.

Connection-test messages must redact API keys and credentials just like existing settings actions.

Because wizard fields may be edited before final completion, tests should operate on the current saved settings after each field edit. This avoids carrying a parallel unsaved credential model and matches Kodi settings behavior.

## Settings And Strings

Add localized strings for:

- Setup Wizard menu item.
- Wizard page titles.
- Welcome instructions, including service connection details and the TMDBHelper prerequisite.
- Generic navigation labels.
- Edit/test field action labels where existing strings are not enough.
- TMDBHelper install page title and install action.
- TMDBHelper missing message.
- Finish and cancellation messages if needed.

All visible labels in `settings.xml` must continue to use localized string IDs.

## Error Handling

- Cancel must not mark setup complete.
- Connection-test failures must keep the user on the current page and show the existing error message.
- Empty URLs should produce the existing "URL not configured" style messages.
- The TMDBHelper player page must not attempt to write player files when TMDBHelper is not installed.
- A missing TMDBHelper install should show a clear message and leave the user on the player page so they can go back, finish, or cancel.
- Finish marks `setup_wizard_completed` as `true`.
- Unexpected wizard exceptions should be logged and shown as a short dialog error without exposing credentials.

## Tests

Add focused tests for:

- `/setup_wizard` dispatches and resolves action handles.
- Main menu auto-runs the wizard when `setup_wizard_completed` is not `true`.
- Main menu does not auto-run the wizard once completed.
- Cancel does not set `setup_wizard_completed`.
- Finish sets `setup_wizard_completed`.
- Search provider selection enables one provider and disables the other.
- nzbdav, WebDAV, NZBHydra2, and Prowlarr wizard test actions call the same underlying test paths or shared helpers.
- TMDBHelper player page calls the existing player installer when TMDBHelper is installed.
- TMDBHelper player page shows the missing-addon message and does not call the installer when TMDBHelper is not installed.
- Repository best-practice checks cover localized labels for any new settings entries.

Run `just lint` and `just test` before committing implementation work.
