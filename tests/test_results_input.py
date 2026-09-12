# SPDX-License-Identifier: GPL-3.0-or-later
"""Physical hold timing must not launch a result or accumulate separate taps."""

from unittest.mock import MagicMock, patch

from resources.lib.results_input import OkHold, PressedOkKeys


def test_hold_fires_at_five_seconds_once_and_suppresses_release_selection():
    held, selected = MagicMock(), MagicMock()
    hold = OkHold(held, selected)
    key = {(4, 28)}
    assert hold._advance(key, 100.0) is None
    hold._pending_select = True
    assert hold._advance(key, 104.999) is None
    callback = hold._advance(key, 105.0)
    callback()
    assert hold._advance(key, 110.0) is None
    assert hold._advance(set(), 110.1) is None
    held.assert_called_once()
    selected.assert_not_called()


def test_short_press_selects_on_release():
    selected = MagicMock()
    hold = OkHold(MagicMock(), selected)
    hold._advance({(4, 28)}, 10.0)
    hold._pending_select = True
    callback = hold._advance(set(), 10.2)
    assert callback is selected
    assert hold._advance(set(), 10.3) is None


def test_separate_presses_and_different_ok_buttons_do_not_accumulate():
    hold = OkHold(MagicMock(), MagicMock())
    for start in (10, 20, 30):
        assert hold._advance({(4, 28)}, start) is None
        assert hold._advance({(4, 28)}, start + 4.9) is None
        assert hold._advance(set(), start + 4.99) is None
    assert hold._advance({(4, 28)}, 40) is None
    assert hold._advance({(5, 352)}, 44) is None
    assert hold._advance({(5, 352)}, 45) is None


def test_kodi_early_context_and_select_are_deferred_until_release():
    selected = MagicMock()
    hold = OkHold(MagicMock(), selected)
    hold._reader = MagicMock(available=True)
    hold._reader.pressed.side_effect = [{(4, 28)}, {(4, 28)}, set(), set()]
    with patch(
        "resources.lib.results_input.time.monotonic", side_effect=[0, 0.6, 1, 1.1]
    ):
        assert hold.defer_selection()
        assert hold.defer_selection()
        selected.assert_not_called()
        assert hold.defer_selection()
        selected.assert_called_once()
        assert hold.defer_selection()  # queued click from the same release


def test_unavailable_or_stopped_input_keeps_normal_selection():
    hold = OkHold(MagicMock(), MagicMock())
    assert not hold.defer_selection()
    hold._reader = MagicMock(available=True)
    hold.close()
    assert not hold.defer_selection()


def test_input_probe_ignores_other_keys_and_never_consumes_events():
    def ioctl(_fd, request, bitmap, _mutate):
        # Advertise Enter, and report Enter plus an unrelated letter pressed.
        bitmap[28 // 8] |= 1 << (28 % 8)
        if request & 0xFF == 0x18:
            bitmap[46 // 8] |= 1 << (46 % 8)

    with patch(
        "resources.lib.results_input.glob.glob", return_value=["/dev/input/event4"]
    ), patch("resources.lib.results_input.os.open", return_value=7), patch(
        "resources.lib.results_input.os.close"
    ) as close, patch(
        "resources.lib.results_input.fcntl", create=True
    ) as ioctl_module:
        ioctl_module.ioctl.side_effect = ioctl
        reader = PressedOkKeys()
        assert reader.available
        assert reader.pressed() == {(7, 28)}
        reader.close()
        close.assert_called_once_with(7)


def test_inaccessible_device_does_not_break_picker():
    with patch(
        "resources.lib.results_input.glob.glob", return_value=["/dev/input/event4"]
    ), patch("resources.lib.results_input.os.open", side_effect=PermissionError):
        reader = PressedOkKeys()
        assert not reader.available
        assert reader.pressed() == set()
        reader.close()
