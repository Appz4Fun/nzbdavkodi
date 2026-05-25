# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""First-run XML setup wizard for NZB-DAV."""

import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from xml.etree import ElementTree as element_tree

import xbmc
import xbmcaddon
import xbmcgui

from resources.lib.http_util import notify as _notify
from resources.lib.http_util import redact_text, redact_url
from resources.lib.i18n import addon_name as _addon_name
from resources.lib.i18n import fmt as _fmt
from resources.lib.i18n import string as _string
from resources.lib.player_installer import TMDBHELPER_ADDON_ID

LIST_ID = 50
PREVIOUS_BUTTON_ID = 101
NEXT_BUTTON_ID = 102
CANCEL_BUTTON_ID = 103
TEST_BUTTON_ID = 104
INSTALL_BUTTON_ID = 105
ADDON_SETTINGS_DIALOG_ID = 10140
ADDON_INFO_DIALOG_ID = 10146

ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92

COMPLETED_SETTING = "setup_wizard_completed"
DIRECT_INDEXER_KEYS = (
    "nzblife",
    "nzbgeek",
    "nzbfinder",
    "drunkenslug",
    "nzbplanet",
    "dognzb",
    "custom1",
    "custom2",
    "custom3",
)
PROVIDER_SETTING_IDS = {
    "nzbhydra_enabled",
    "prowlarr_enabled",
    "hydra_url",
    "hydra_api_key",
    "prowlarr_host",
    "prowlarr_api_key",
}
PROVIDER_CHOICE_SETTING_IDS = {"nzbhydra_enabled", "prowlarr_enabled"}

PAGES = [
    {
        "key": "welcome",
        "title_id": 30198,
        "body_id": 30199,
        "rows": [],
    },
    {
        "key": "nzbdav",
        "title_id": 30216,
        "body_id": 30204,
        "test": "nzbdav",
        "rows": [
            {"kind": "text", "setting": "nzbdav_url", "label_id": 30005},
            {
                "kind": "text",
                "setting": "nzbdav_api_key",
                "label_id": 30003,
                "secret": True,
            },
        ],
    },
    {
        "key": "webdav",
        "title_id": 30217,
        "body_id": 30204,
        "test": "webdav",
        "rows": [
            {"kind": "text", "setting": "webdav_url", "label_id": 30007},
            {"kind": "text", "setting": "webdav_username", "label_id": 30008},
            {
                "kind": "text",
                "setting": "webdav_password",
                "label_id": 30009,
                "secret": True,
            },
        ],
    },
    {
        "key": "index_manager",
        "title_id": 30218,
        "body_id": 30206,
        "test": "index_manager",
        "rows": [
            {"kind": "provider", "label_id": 30206},
            {
                "kind": "text",
                "setting": "hydra_url",
                "label_id": 30002,
                "provider": "hydra",
            },
            {
                "kind": "text",
                "setting": "hydra_api_key",
                "label_id": 30003,
                "secret": True,
                "provider": "hydra",
            },
            {
                "kind": "text",
                "setting": "prowlarr_host",
                "label_id": 30128,
                "provider": "prowlarr",
            },
            {
                "kind": "text",
                "setting": "prowlarr_api_key",
                "label_id": 30129,
                "secret": True,
                "provider": "prowlarr",
            },
        ],
    },
    {
        "key": "resolution",
        "title_id": 30207,
        "body_id": 30224,
        "rows": [
            {"kind": "bool", "setting": "filter_2160p", "label_id": 30014},
            {"kind": "bool", "setting": "filter_1080p", "label_id": 30015},
            {"kind": "bool", "setting": "filter_720p", "label_id": 30016},
            {"kind": "bool", "setting": "filter_480p", "label_id": 30017},
        ],
    },
    {
        "key": "hdr",
        "title_id": 30208,
        "body_id": 30224,
        "rows": [
            {"kind": "bool", "setting": "filter_hdr10", "label_id": 30019},
            {"kind": "bool", "setting": "filter_hdr10plus", "label_id": 30020},
            {"kind": "bool", "setting": "filter_dolby_vision", "label_id": 30021},
            {"kind": "bool", "setting": "filter_hlg", "label_id": 30022},
            {"kind": "bool", "setting": "filter_sdr", "label_id": 30023},
        ],
    },
    {
        "key": "audio",
        "title_id": 30225,
        "body_id": 30224,
        "rows": [
            {"kind": "bool", "setting": "filter_atmos", "label_id": 30025},
            {"kind": "bool", "setting": "filter_truehd", "label_id": 30026},
            {"kind": "bool", "setting": "filter_dtshd_ma", "label_id": 30027},
            {"kind": "bool", "setting": "filter_dtsx", "label_id": 30028},
            {"kind": "bool", "setting": "filter_ddplus", "label_id": 30029},
            {"kind": "bool", "setting": "filter_dd", "label_id": 30030},
            {"kind": "bool", "setting": "filter_aac", "label_id": 30031},
        ],
    },
    {
        "key": "video_codec",
        "title_id": 30209,
        "body_id": 30224,
        "rows": [
            {"kind": "bool", "setting": "filter_hevc", "label_id": 30033},
            {"kind": "bool", "setting": "filter_avc", "label_id": 30034},
            {"kind": "bool", "setting": "filter_av1", "label_id": 30035},
            {"kind": "bool", "setting": "filter_vp9", "label_id": 30036},
            {"kind": "bool", "setting": "filter_mpeg2", "label_id": 30037},
        ],
    },
    {
        "key": "languages",
        "title_id": 30210,
        "body_id": 30224,
        "rows": [
            {"kind": "bool", "setting": "filter_english", "label_id": 30039},
            {"kind": "bool", "setting": "filter_spanish", "label_id": 30040},
            {"kind": "bool", "setting": "filter_french", "label_id": 30041},
            {"kind": "bool", "setting": "filter_german", "label_id": 30042},
            {"kind": "bool", "setting": "filter_italian", "label_id": 30043},
            {"kind": "bool", "setting": "filter_portuguese", "label_id": 30044},
            {"kind": "bool", "setting": "filter_dutch", "label_id": 30045},
            {"kind": "bool", "setting": "filter_russian", "label_id": 30046},
            {"kind": "bool", "setting": "filter_japanese", "label_id": 30047},
            {"kind": "bool", "setting": "filter_korean", "label_id": 30048},
            {"kind": "bool", "setting": "filter_chinese", "label_id": 30049},
            {"kind": "bool", "setting": "filter_arabic", "label_id": 30050},
            {"kind": "bool", "setting": "filter_hindi", "label_id": 30051},
        ],
    },
    {
        "key": "tmdbhelper",
        "title_id": 30211,
        "body_id": 30212,
        "rows": [],
    },
]


def _set_bool(addon, setting_id, enabled):
    addon.setSetting(setting_id, "true" if enabled else "false")


def _get_bool(addon, setting_id, default=True):
    raw = addon.getSetting(setting_id)
    if raw == "":
        return default
    return str(raw).lower() == "true"


def _selected_provider(addon):
    if _get_bool(addon, "prowlarr_enabled", default=False):
        return "prowlarr"
    return "hydra"


def _select_provider(addon, provider):
    if provider == "hydra":
        _set_bool(addon, "nzbhydra_enabled", True)
        _set_bool(addon, "prowlarr_enabled", False)
        return
    if provider == "prowlarr":
        _set_bool(addon, "prowlarr_enabled", True)
        _set_bool(addon, "nzbhydra_enabled", False)
        return
    raise ValueError("unknown setup wizard provider: {}".format(provider))


def _tmdbhelper_installed():
    try:
        xbmcaddon.Addon(TMDBHELPER_ADDON_ID)
        return True
    except Exception:  # pylint: disable=broad-except
        return False


def _tmdbhelper_player_installed():
    from resources.lib.player_installer import tmdbhelper_player_installed

    return tmdbhelper_player_installed()


def _webdav_failure_reason(error):
    if error == "auth_failed":
        return "Authentication failed"
    if error == "server_error":
        return "Server error"
    if error == "connection_error":
        return "Could not connect"
    return "Unexpected response"


def _http_failure_reason(error):
    if isinstance(error, HTTPError):
        if error.code in (401, 403):
            return "API key denied"
        if error.code >= 500:
            return "Server error: HTTP {}".format(error.code)
        return "Unexpected response: HTTP {}".format(error.code)
    if isinstance(error, URLError):
        return "Could not connect: {}".format(str(error.reason)[:80])
    return "Could not connect: {}".format(str(error)[:80])


def _redact_connection_log_value(value):
    return redact_text(redact_url(str(value)))


def _has_setting_value(addon, setting_id):
    return bool(str(addon.getSetting(setting_id) or "").strip())


def _has_existing_configuration(addon):
    if _has_setting_value(addon, "nzbdav_api_key"):
        return True
    if _has_setting_value(addon, "webdav_username"):
        return True
    if _has_setting_value(addon, "webdav_password"):
        return True
    if _get_bool(addon, "nzbhydra_enabled", default=False):
        return True
    if _has_setting_value(addon, "hydra_api_key"):
        return True
    if _get_bool(addon, "prowlarr_enabled", default=False):
        return True
    if _has_setting_value(addon, "prowlarr_api_key"):
        return True
    if _get_bool(addon, "direct_indexers_enabled", default=False):
        return True
    for key in DIRECT_INDEXER_KEYS:
        if _get_bool(addon, "direct_indexer_{}_enabled".format(key), default=False):
            return True
        if _has_setting_value(addon, "direct_indexer_{}_api_key".format(key)):
            return True
    return False


def _connection_check(test_key, addon):
    from resources.lib.http_util import http_get

    if test_key == "webdav":
        from resources.lib.webdav import probe_webdav_reachable

        reachable, error = probe_webdav_reachable(
            max_retries=0, settings_getter=addon.getSetting
        )
        if reachable:
            return True, ""
        return False, _webdav_failure_reason(error)

    if test_key == "index_manager":
        test_key = "prowlarr" if _selected_provider(addon) == "prowlarr" else "hydra"

    try:
        if test_key == "nzbdav":
            from resources.lib.router import _nzbdav_queue_response_ok

            url = addon.getSetting("nzbdav_url").rstrip("/")
            api_key = addon.getSetting("nzbdav_api_key")
            params = {
                "mode": "queue",
                "start": "0",
                "limit": "0",
                "apikey": api_key,
                "output": "json",
            }
            test_url = "{}/api?{}".format(url, urlencode(params))
            ok_condition = _nzbdav_queue_response_ok
        elif test_key == "hydra":
            from resources.lib.router import _hydra_search_response_ok

            url = addon.getSetting("hydra_url").rstrip("/")
            api_key = addon.getSetting("hydra_api_key")
            params = {
                "apikey": api_key,
                "t": "search",
                "q": "__nzbdav_connection_test__",
                "o": "xml",
                "limit": "1",
            }
            test_url = "{}/api?{}".format(url, urlencode(params))
            ok_condition = _hydra_search_response_ok
        elif test_key == "prowlarr":
            from resources.lib.router import _prowlarr_indexers_response_ok

            url = addon.getSetting("prowlarr_host").rstrip("/")
            api_key = addon.getSetting("prowlarr_api_key")
            test_url = "{}/api/v1/indexer?{}".format(
                url, urlencode({"apikey": api_key})
            )
            ok_condition = _prowlarr_indexers_response_ok
        else:
            return False, "Unknown connection type"

        if not url:
            return False, "URL not configured"
        if ok_condition(http_get(test_url)):
            return True, ""
        return False, "API key denied or service returned an unexpected response"
    except Exception as e:  # pylint: disable=broad-except
        xbmc.log(
            "NZB-DAV: setup wizard connection check failed for {}: {}".format(
                _redact_connection_log_value(test_key),
                _redact_connection_log_value(e),
            ),
            xbmc.LOGDEBUG,
        )
        return False, _http_failure_reason(e)


def should_auto_run(addon=None):
    if addon is None:
        addon = xbmcaddon.Addon("plugin.video.nzbdav")
    if _get_bool(addon, COMPLETED_SETTING, default=False):
        return False
    return not _has_existing_configuration(addon)


def maybe_auto_run(addon=None):
    if addon is None:
        addon = xbmcaddon.Addon("plugin.video.nzbdav")
    if not should_auto_run(addon):
        return False
    run_setup_wizard(addon)
    return True


def run_setup_wizard(addon=None):
    """Run the XML setup wizard. Return True only when Finish is selected."""
    if addon is None:
        addon = xbmcaddon.Addon("plugin.video.nzbdav")
    addon_path = addon.getAddonInfo("path")
    dialog = SetupWizardDialog(
        "setup-wizard.xml",
        addon_path,
        "Default",
        "1080i",
        addon=addon,
    )
    dialog.doModal()
    finished = dialog.was_finished()
    changed_setting_ids = _dialog_changed_setting_ids(dialog)
    changed_settings = _dialog_changed_settings(dialog)
    del dialog
    if finished:
        reopen_settings = _should_reopen_fresh_settings_dialog()
        if not _get_bool(addon, COMPLETED_SETTING, default=False):
            addon.setSetting(COMPLETED_SETTING, "true")
        _ensure_native_settings_dialog_closed()
        _replay_changed_settings(addon, changed_setting_ids, changed_settings)
        _close_stale_addon_info_dialog()
        if reopen_settings:
            _reopen_fresh_settings_dialog()
        _notify(_addon_name(), _string(30215), 3000)
    return finished


def _dialog_changed_setting_ids(dialog):
    changed = getattr(dialog, "changed_setting_ids", None)
    if not callable(changed):
        return []
    try:
        return list(changed() or [])
    except TypeError:
        return []


def _dialog_changed_settings(dialog):
    changed = getattr(dialog, "changed_settings", None)
    if not callable(changed):
        return {}
    try:
        values = changed() or {}
    except TypeError:
        return {}
    return dict(values) if hasattr(values, "items") else {}


def _native_settings_dialog_active():
    return _window_active(ADDON_SETTINGS_DIALOG_ID)


def _addon_info_dialog_active():
    return _window_active(ADDON_INFO_DIALOG_ID)


def _window_active(window_id):
    try:
        visible = xbmc.getCondVisibility("Window.IsActive({})".format(window_id))
    except (AttributeError, RuntimeError, TypeError):
        return False
    if isinstance(visible, str):
        return visible.lower() == "true"
    return visible is True


def _wait_for_native_settings_dialog_to_close(timeout_seconds=1.0):
    """Let Kodi finish closing its settings dialog before replaying values.

    Kodi routes ``xbmcaddon.Addon().setSetting`` into the active add-on
    settings dialog instead of saving immediately. The setup wizard can be
    launched from that dialog, so wait briefly before doing the final replay.
    """
    monitor = xbmc.Monitor()
    interval = 0.05
    remaining = timeout_seconds
    while remaining > 0 and _native_settings_dialog_active():
        if monitor.waitForAbort(interval):
            return False
        remaining -= interval
    return not _native_settings_dialog_active()


def _ensure_native_settings_dialog_closed():
    """Force-close a lingering native settings dialog before final setSetting calls."""
    if _wait_for_native_settings_dialog_to_close():
        return True
    try:
        xbmc.executebuiltin("Dialog.Close(addonsettings,true)")
    except (AttributeError, RuntimeError, TypeError):
        return False
    return _wait_for_native_settings_dialog_to_close(timeout_seconds=2.0)


def _close_stale_addon_info_dialog():
    """Close add-on info so its Configure button cannot reopen stale settings."""
    try:
        xbmc.executebuiltin("Dialog.Close(addoninformation,true)")
    except (AttributeError, RuntimeError, TypeError):
        return False
    return True


def _should_reopen_fresh_settings_dialog():
    return _native_settings_dialog_active() or _addon_info_dialog_active()


def _reopen_fresh_settings_dialog():
    """Open a new settings dialog after Kodi's stale add-on window is gone."""
    try:
        monitor = xbmc.Monitor()
        monitor.waitForAbort(0.1)
        xbmc.executebuiltin("Addon.OpenSettings(plugin.video.nzbdav)")
    except (AttributeError, RuntimeError, TypeError):
        return False
    return True


def _fresh_addon_instance():
    try:
        return xbmcaddon.Addon("plugin.video.nzbdav")
    except TypeError:
        return xbmcaddon.Addon()


def _replay_changed_settings(source_addon, setting_ids, setting_values=None):
    """Rewrite touched values after the wizard closes to refresh Kodi's dialog cache."""
    setting_ids = sorted(set(setting_ids or []))
    if not setting_ids:
        return
    missing = object()
    setting_values = dict(setting_values or {})
    fresh_addon = _fresh_addon_instance()
    for setting_id in setting_ids:
        value = setting_values.get(setting_id, missing)
        if value is missing:
            value = source_addon.getSetting(setting_id)
        fresh_addon.setSetting(setting_id, value)


class _AddonSettingsOverlay:
    def __init__(self, addon, values):
        self._addon = addon
        self._values = dict(values or {})

    def getSetting(self, setting_id):  # pylint: disable=invalid-name
        if setting_id in self._values:
            return self._values[setting_id]
        return self._addon.getSetting(setting_id)

    def getAddonInfo(self, key):  # pylint: disable=invalid-name
        return self._addon.getAddonInfo(key)


def _profile_settings_path(addon):
    try:
        profile = addon.getAddonInfo("profile")
    except (AttributeError, RuntimeError, TypeError):
        profile = ""
    if not isinstance(profile, str) or not profile:
        return ""
    try:
        import xbmcvfs

        translated = xbmcvfs.translatePath(profile)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        translated = ""
    if isinstance(translated, str) and translated:
        profile = translated
    return os.path.join(profile, "settings.xml")


def _persisted_setting(addon, setting_id):
    settings_path = _profile_settings_path(addon)
    if not settings_path:
        return None
    try:
        # Kodi owns this local profile settings file; it is not remote XML.
        root = element_tree.parse(settings_path).getroot()  # nosemgrep
    except (OSError, element_tree.ParseError):
        return None
    for setting in root.findall(".//setting"):
        if setting.get("id") == setting_id:
            value = setting.text
            return value if isinstance(value, str) else ""
    return None


class SetupWizardDialog(xbmcgui.WindowXMLDialog):
    """Custom XML dialog for first-run setup."""

    def __init__(self, *args, **kwargs):
        self.addon = kwargs.get("addon") or xbmcaddon.Addon("plugin.video.nzbdav")
        self.page_index = 0
        self._focus_id = 0
        self._finished = False
        self._changed_setting_ids = set()
        self._changed_settings = {}
        self._original_settings = {}
        self._visible_rows = []
        self._current_install_button_visible = False
        self._current_tmdb_missing_visible = False
        self._current_tmdb_player_installed_visible = False
        super().__init__(*args)

    def onInit(self):
        self._render_page()

    def onClick(self, controlId):
        if controlId == LIST_ID:
            self._activate_selected_row()
        elif controlId == PREVIOUS_BUTTON_ID:
            self._previous_page()
        elif controlId == NEXT_BUTTON_ID:
            self._next_or_finish()
        elif controlId == CANCEL_BUTTON_ID:
            self._cancel()
        elif controlId == TEST_BUTTON_ID:
            self._test_current_page()
        elif controlId == INSTALL_BUTTON_ID:
            self._install_player()

    def onFocus(self, controlId):
        self._focus_id = controlId

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self._cancel()

    def was_finished(self):
        return self._finished

    def changed_setting_ids(self):
        return sorted(self._changed_setting_ids)

    def changed_settings(self):
        return dict(self._changed_settings)

    def _set_setting(self, setting_id, value):
        self._remember_original_setting(setting_id)
        self.addon.setSetting(setting_id, value)
        self._changed_setting_ids.add(setting_id)
        self._changed_settings[setting_id] = value

    def _set_bool(self, setting_id, enabled):
        self._set_setting(setting_id, "true" if enabled else "false")

    def _select_provider(self, provider):
        self._remember_original_setting("nzbhydra_enabled")
        self._remember_original_setting("prowlarr_enabled")
        _select_provider(self.addon, provider)
        if provider == "hydra":
            values = {"nzbhydra_enabled": "true", "prowlarr_enabled": "false"}
        else:
            values = {"nzbhydra_enabled": "false", "prowlarr_enabled": "true"}
        self._changed_setting_ids.update(values)
        self._changed_settings.update(values)

    def _page(self):
        return PAGES[self.page_index]

    def _setting_value(self, setting_id):
        if setting_id in self._changed_settings:
            return self._changed_settings[setting_id]
        persisted = _persisted_setting(self.addon, setting_id)
        if persisted is not None:
            return persisted
        return self.addon.getSetting(setting_id)

    def _bool_value(self, setting_id, default=True):
        raw = self._setting_value(setting_id)
        if raw == "":
            return default
        return str(raw).lower() == "true"

    def _selected_provider(self):
        prowlarr = self._changed_settings.get("prowlarr_enabled")
        hydra = self._changed_settings.get("nzbhydra_enabled")
        if prowlarr is not None or hydra is not None:
            return "prowlarr" if str(prowlarr).lower() == "true" else "hydra"
        return _selected_provider(self.addon)

    def _render_page(self, selected_position=None):
        page = self._page()
        self.setProperty("wizard.title", _string(page["title_id"]))
        self.setProperty("wizard.body", _string(page["body_id"]))
        self.setProperty("wizard.page", _fmt(30227, self.page_index + 1, len(PAGES)))
        self.setProperty("wizard.previous_label", _string(30200))
        next_label_id = 30203 if self._is_last() else 30201
        self.setProperty("wizard.next_label", _string(next_label_id))
        self.setProperty("wizard.cancel_label", _string(30202))
        self.setProperty("wizard.action_label", _string(30205))
        self.setProperty("wizard.install_label", _string(30011))
        self.setProperty("wizard.tmdb_missing_text", _string(30213))
        self.setProperty("wizard.tmdb_player_installed_text", _string(30234))
        previous_visible = self.page_index > 0
        self.setProperty(
            "wizard.previous_visible", "true" if previous_visible else "false"
        )
        self.setProperty(
            "wizard.welcome_visible", "true" if page["key"] == "welcome" else "false"
        )
        is_test = bool(page.get("test"))
        self._set_tmdbhelper_page_state(page)
        self.setProperty("wizard.next_visible", "true")
        self.setProperty("wizard.test_visible", "true" if is_test else "false")
        self.setProperty(
            "wizard.install_visible",
            "true" if self._install_button_visible(page) else "false",
        )
        self.setProperty(
            "wizard.tmdb_missing_visible",
            "true" if self._tmdb_missing_message_visible(page) else "false",
        )
        self.setProperty(
            "wizard.tmdb_player_installed_visible",
            "true" if self._tmdb_player_installed_message_visible(page) else "false",
        )
        self.setProperty("wizard.cancel_visible", "true")
        self.setProperty("wizard.warning", self._warning_text(page))
        self._populate_rows(page, selected_position)
        self._sync_native_navigation(page)

    def _populate_rows(self, page, selected_position=None):
        self._visible_rows = self._rows_for_page(page)
        list_control = self.getControl(LIST_ID)
        list_control.reset()
        items = []
        for row in self._visible_rows:
            li = xbmcgui.ListItem(label=_string(row["label_id"]))
            li.setProperty("value", self._row_value(row))
            li.setProperty("helper", self._row_helper(row))
            items.append(li)
        list_control.addItems(items)
        if items:
            if selected_position is not None:
                selected_position = max(0, min(selected_position, len(items) - 1))
                list_control.selectItem(selected_position)
            self.setFocusId(LIST_ID)
        else:
            self.setFocusId(self._default_footer_focus_id())

    def _rows_for_page(self, page):
        provider = self._selected_provider()
        rows = []
        for row in page.get("rows", []):
            if row.get("provider") and row.get("provider") != provider:
                continue
            rows.append(row)
        return rows

    def _row_value(self, row):
        kind = row["kind"]
        if kind == "bool":
            return _string(30230 if self._bool_value(row["setting"]) else 30231)
        if kind == "provider":
            provider_label_id = (
                30220 if self._selected_provider() == "prowlarr" else 30219
            )
            return _string(provider_label_id)
        if kind == "text":
            value = self._setting_value(row["setting"])
            if row.get("secret") and value:
                return "*" * 8
            return value or _string(30223)
        return ""

    def _row_helper(self, _row):
        return ""

    def _activate_selected_row(self):
        list_control = self.getControl(LIST_ID)
        selected = list_control.getSelectedPosition()
        if selected < 0 or selected >= len(self._visible_rows):
            return
        row = self._visible_rows[selected]
        if row["kind"] == "bool":
            current = self._bool_value(row["setting"])
            self._set_bool(row["setting"], not current)
        elif row["kind"] == "provider":
            self._choose_provider()
        elif row["kind"] == "text":
            self._edit_text(row)
        self._render_page(selected_position=selected)

    def _edit_text(self, row):
        current = self._setting_value(row["setting"])
        dialog = xbmcgui.Dialog()
        if current:
            selected = dialog.select(
                _string(row["label_id"]),
                [_string(30232), _string(30233)],
            )
            if selected < 0:
                return
            if selected == 1:
                self._set_setting(row["setting"], "")
                return
        input_type = getattr(xbmcgui, "INPUT_ALPHANUM", 0)
        option = 0
        if row.get("secret"):
            option = getattr(xbmcgui, "ALPHANUM_HIDE_INPUT", 0)
        value = dialog.input(
            _string(row["label_id"]),
            defaultt=current,
            type=input_type,
            option=option,
        )
        if value is None or (value == "" and current != ""):
            return
        self._set_setting(row["setting"], value)

    def _choose_provider(self):
        choices = [_string(30219), _string(30220)]
        selected = xbmcgui.Dialog().select(_string(30206), choices)
        if selected == 0:
            self._select_provider("hydra")
        elif selected == 1:
            self._select_provider("prowlarr")

    def _test_current_page(self):
        test_key = self._page().get("test")
        settings = _AddonSettingsOverlay(self.addon, self._changed_settings)
        ok, reason = _connection_check(test_key, settings)
        title = _string(self._page()["title_id"])
        if ok:
            xbmcgui.Dialog().ok(title, "Connection successful.")
        else:
            xbmcgui.Dialog().ok(title, "Connection failed: {}".format(reason))

    def _install_player(self):
        if not _tmdbhelper_installed():
            xbmcgui.Dialog().ok(_string(30211), _string(30213))
            self._finished = False
            return
        from resources.lib.player_installer import install_player

        if install_player():
            xbmcgui.Dialog().ok(_string(30211), _string(30228))
        self._render_page()

    def _previous_page(self):
        if self.page_index > 0:
            self.page_index -= 1
            self._render_page()

    def _next_or_finish(self):
        if self._is_last():
            self._persist_selected_provider()
            self._complete_wizard()
            self.close()
            return
        self.page_index += 1
        self._render_page()

    def _cancel(self):
        self._revert_changed_settings()
        self._finished = False
        self.close()

    def _is_last(self):
        return self.page_index == len(PAGES) - 1

    def _complete_wizard(self):
        self._set_setting(COMPLETED_SETTING, "true")
        self._finished = True

    def _persist_selected_provider(self):
        if _get_bool(self.addon, COMPLETED_SETTING, default=False):
            if self._changed_setting_ids.isdisjoint(PROVIDER_CHOICE_SETTING_IDS):
                return
        self._select_provider(self._selected_provider())

    def _remember_original_setting(self, setting_id):
        if setting_id not in self._original_settings:
            self._original_settings[setting_id] = self._setting_value(setting_id)

    def _revert_changed_settings(self):
        for setting_id in sorted(self._original_settings):
            self.addon.setSetting(setting_id, self._original_settings[setting_id])

    def _page_has_visible_rows(self, page):
        if not page.get("rows"):
            return False
        return bool(getattr(self, "_visible_rows", None) or self._rows_for_page(page))

    def _sync_native_navigation(self, page):
        footer_ids = self._footer_control_ids(page)
        footer_controls = []
        for control_id in footer_ids:
            try:
                footer_controls.append(self.getControl(control_id))
            except Exception as e:  # pylint: disable=broad-except
                xbmc.log(
                    "NZB-DAV: setup wizard could not read footer control {}: {}".format(
                        control_id, e
                    ),
                    xbmc.LOGDEBUG,
                )
                return

        if not footer_controls:
            return

        has_rows = self._page_has_visible_rows(page)
        list_control = None
        if has_rows:
            try:
                list_control = self.getControl(LIST_ID)
            except Exception as e:  # pylint: disable=broad-except
                xbmc.log(
                    "NZB-DAV: setup wizard could not read list control: {}".format(e),
                    xbmc.LOGDEBUG,
                )

        for index, control in enumerate(footer_controls):
            left = footer_controls[index - 1] if index > 0 else control
            if index < len(footer_controls) - 1:
                right = footer_controls[index + 1]
            else:
                right = control
            up = self._up_control_for_footer(page, list_control, control)
            self._set_control_navigation(control, left, right, up, control)

        if list_control is not None:
            default_footer_id = TEST_BUTTON_ID if page.get("test") else NEXT_BUTTON_ID
            default_footer = footer_controls[footer_ids.index(default_footer_id)]
            self._set_control_navigation(
                list_control, list_control, list_control, list_control, default_footer
            )

        if self._install_button_visible(page):
            try:
                install_control = self.getControl(INSTALL_BUTTON_ID)
            except Exception as e:  # pylint: disable=broad-except
                xbmc.log(
                    "NZB-DAV: setup wizard could not read install control: {}".format(
                        e
                    ),
                    xbmc.LOGDEBUG,
                )
                return
            finish_control = footer_controls[footer_ids.index(NEXT_BUTTON_ID)]
            self._set_control_navigation(
                install_control,
                install_control,
                install_control,
                install_control,
                finish_control,
            )

    def _up_control_for_footer(self, page, list_control, fallback_control):
        if list_control is not None:
            return list_control
        if self._install_button_visible(page):
            try:
                return self.getControl(INSTALL_BUTTON_ID)
            except Exception as e:  # pylint: disable=broad-except
                xbmc.log(
                    "NZB-DAV: setup wizard could not read install control: {}".format(
                        e
                    ),
                    xbmc.LOGDEBUG,
                )
        return fallback_control

    def _set_control_navigation(self, control, left, right, up, down):
        try:
            control.controlLeft(left)
            control.controlRight(right)
            control.controlUp(up)
            control.controlDown(down)
        except Exception as e:  # pylint: disable=broad-except
            xbmc.log(
                "NZB-DAV: setup wizard could not sync control navigation: {}".format(e),
                xbmc.LOGDEBUG,
            )

    def _footer_control_ids(self, page):
        footer_ids = []
        if self.page_index > 0:
            footer_ids.append(PREVIOUS_BUTTON_ID)
        if page.get("test"):
            footer_ids.append(TEST_BUTTON_ID)
        footer_ids.append(NEXT_BUTTON_ID)
        footer_ids.append(CANCEL_BUTTON_ID)
        return footer_ids

    def _default_footer_focus_id(self):
        if self._install_button_visible(self._page()):
            return INSTALL_BUTTON_ID
        return NEXT_BUTTON_ID

    def _warning_text(self, page):
        if page["key"] == "welcome" and not _tmdbhelper_installed():
            return _string(30226)
        return ""

    def _install_button_visible(self, page):
        if page["key"] != "tmdbhelper":
            return False
        return self._current_install_button_visible

    def _tmdb_missing_message_visible(self, page):
        if page["key"] != "tmdbhelper":
            return False
        return self._current_tmdb_missing_visible

    def _tmdb_player_installed_message_visible(self, page):
        if page["key"] != "tmdbhelper":
            return False
        return self._current_tmdb_player_installed_visible

    def _set_tmdbhelper_page_state(self, page):
        self._current_install_button_visible = False
        self._current_tmdb_missing_visible = False
        self._current_tmdb_player_installed_visible = False
        if page["key"] != "tmdbhelper":
            return
        if not _tmdbhelper_installed():
            self._current_tmdb_missing_visible = True
            return
        if _tmdbhelper_player_installed():
            self._current_tmdb_player_installed_visible = True
            return
        self._current_install_button_visible = True
