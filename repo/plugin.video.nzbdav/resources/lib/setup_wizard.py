# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 nzbdav contributors

"""First-run XML setup wizard for NZB-DAV."""

from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

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

        reachable, error = probe_webdav_reachable(max_retries=0)
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
    del dialog
    if finished:
        if not _get_bool(addon, COMPLETED_SETTING, default=False):
            addon.setSetting(COMPLETED_SETTING, "true")
        _notify(_addon_name(), _string(30215), 3000)
    return finished


class SetupWizardDialog(xbmcgui.WindowXMLDialog):
    """Custom XML dialog for first-run setup."""

    def __init__(self, *args, **kwargs):
        self.addon = kwargs.get("addon") or xbmcaddon.Addon("plugin.video.nzbdav")
        self.page_index = 0
        self._focus_id = 0
        self._finished = False
        self._visible_rows = []
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

    def onFocus(self, controlId):
        self._focus_id = controlId

    def onAction(self, action):
        action_id = action.getId()
        if action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self._cancel()

    def was_finished(self):
        return self._finished

    def _page(self):
        return PAGES[self.page_index]

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
        previous_visible = self.page_index > 0
        self.setProperty(
            "wizard.previous_visible", "true" if previous_visible else "false"
        )
        self.setProperty(
            "wizard.welcome_visible", "true" if page["key"] == "welcome" else "false"
        )
        is_test = bool(page.get("test"))
        self.setProperty("wizard.next_visible", "true")
        self.setProperty("wizard.test_visible", "true" if is_test else "false")
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
        provider = _selected_provider(self.addon)
        rows = []
        for row in page.get("rows", []):
            if row.get("provider") and row.get("provider") != provider:
                continue
            rows.append(row)
        return rows

    def _row_value(self, row):
        kind = row["kind"]
        if kind == "bool":
            return _string(30230 if _get_bool(self.addon, row["setting"]) else 30231)
        if kind == "provider":
            provider_label_id = (
                30220 if _selected_provider(self.addon) == "prowlarr" else 30219
            )
            return _string(provider_label_id)
        if kind == "text":
            value = self.addon.getSetting(row["setting"])
            if row.get("secret") and value:
                return "*" * 8
            return value or _string(30223)
        return ""

    def _row_helper(self, row):
        kind = row["kind"]
        if kind == "bool":
            return "Toggle filter"
        if kind == "provider":
            return "Choose search service"
        if kind == "text":
            return "Connection setting"
        return ""

    def _activate_selected_row(self):
        list_control = self.getControl(LIST_ID)
        selected = list_control.getSelectedPosition()
        if selected < 0 or selected >= len(self._visible_rows):
            return
        row = self._visible_rows[selected]
        if row["kind"] == "bool":
            current = _get_bool(self.addon, row["setting"])
            _set_bool(self.addon, row["setting"], not current)
        elif row["kind"] == "provider":
            self._choose_provider()
        elif row["kind"] == "text":
            self._edit_text(row)
        self._render_page(selected_position=selected)

    def _edit_text(self, row):
        current = self.addon.getSetting(row["setting"])
        dialog = xbmcgui.Dialog()
        if current:
            selected = dialog.select(
                _string(row["label_id"]),
                [_string(30232), _string(30233)],
            )
            if selected < 0:
                return
            if selected == 1:
                self.addon.setSetting(row["setting"], "")
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
        self.addon.setSetting(row["setting"], value)

    def _choose_provider(self):
        choices = [_string(30219), _string(30220)]
        selected = xbmcgui.Dialog().select(_string(30206), choices)
        if selected == 0:
            _select_provider(self.addon, "hydra")
        elif selected == 1:
            _select_provider(self.addon, "prowlarr")

    def _test_current_page(self):
        test_key = self._page().get("test")
        ok, reason = _connection_check(test_key, self.addon)
        title = _string(self._page()["title_id"])
        if ok:
            xbmcgui.Dialog().ok(title, "Connection successful.")
        else:
            xbmcgui.Dialog().ok(title, "Connection failed: {}".format(reason))

    def _install_player(self):
        if not _tmdbhelper_installed():
            xbmcgui.Dialog().ok(_string(30211), _string(30213))
            self._finished = False
            self.close()
            return
        from resources.lib.player_installer import install_player

        install_player()
        xbmcgui.Dialog().ok(_string(30211), _string(30228))
        self._complete_wizard()
        self.close()

    def _previous_page(self):
        if self.page_index > 0:
            self.page_index -= 1
            self._render_page()

    def _next_or_finish(self):
        if self._is_last():
            self._complete_wizard()
            self.close()
            return
        self.page_index += 1
        self._render_page()

    def _cancel(self):
        self._finished = False
        self.close()

    def _is_last(self):
        return self.page_index == len(PAGES) - 1

    def _complete_wizard(self):
        self.addon.setSetting(COMPLETED_SETTING, "true")
        self._finished = True

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
            up = list_control if list_control is not None else control
            self._set_control_navigation(control, left, right, up, control)

        if list_control is not None:
            default_footer = footer_controls[
                footer_ids.index(self._default_footer_focus_id())
            ]
            self._set_control_navigation(
                list_control, list_control, list_control, list_control, default_footer
            )

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
        return NEXT_BUTTON_ID

    def _warning_text(self, page):
        if page["key"] == "welcome" and not _tmdbhelper_installed():
            return _string(30226)
        return ""
